"""Token-pool tests: round robin, LRU, failover, cooldown, concurrency."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.gateway.errors import ErrorCategory, GatewayError
from app.gateway.scheduler import get_scheduler
from app.models.db import QwenCredential


async def _add(session, name: str, secret: str = "mock:normal", **kwargs) -> QwenCredential:
    from app.services.credential_service import create_credential

    return await create_credential(session, name=name, secret=secret, provider="mock", **kwargs)


class TestSelectionStrategies:
    async def test_round_robin_cycles_through_tokens(self, app_context) -> None:
        from app.database import session_scope

        scheduler = get_scheduler()
        scheduler.reset()
        async with session_scope() as session:
            for i in range(3):
                await _add(session, f"token-{i}")

            picked = []
            for _ in range(6):
                credential = await scheduler.acquire(
                    session, provider="mock", strategy="round_robin"
                )
                picked.append(credential.id)
                await scheduler.release(credential.id)

        # Every token used equally, in a rotating order.
        assert sorted(set(picked)) == sorted({1, 2, 3})
        assert len(picked) == 6
        assert all(picked.count(i) == 2 for i in {1, 2, 3})
        assert picked[0] != picked[1] != picked[2]

    async def test_least_recently_used_prefers_idle_token(self, app_context) -> None:
        from app.database import session_scope
        from app.models.db import utcnow

        scheduler = get_scheduler()
        scheduler.reset()
        async with session_scope() as session:
            busy = await _add(session, "busy")
            idle = await _add(session, "idle")
            busy.last_used_at = utcnow()
            idle.last_used_at = utcnow() - timedelta(hours=2)
            await session.commit()

            credential = await scheduler.acquire(
                session, provider="mock", strategy="least_recently_used"
            )
            assert credential.id == idle.id

    async def test_disabled_token_never_selected(self, app_context) -> None:
        from app.database import session_scope

        scheduler = get_scheduler()
        scheduler.reset()
        async with session_scope() as session:
            await _add(session, "off", enabled=False)
            with pytest.raises(GatewayError) as exc:
                await scheduler.acquire(session, provider="mock")
        assert exc.value.code == "no_available_credential"

    async def test_expired_token_never_selected(self, app_context) -> None:
        from app.database import session_scope

        scheduler = get_scheduler()
        scheduler.reset()
        async with session_scope() as session:
            credential = await _add(session, "expired")
            credential.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            await session.commit()
            with pytest.raises(GatewayError):
                await scheduler.acquire(session, provider="mock")

    async def test_exclusion_supports_failover(self, app_context) -> None:
        from app.database import session_scope

        scheduler = get_scheduler()
        scheduler.reset()
        async with session_scope() as session:
            first = await _add(session, "a")
            second = await _add(session, "b")
            credential = await scheduler.acquire(session, provider="mock", exclude_ids=[first.id])
            assert credential.id == second.id


class TestCooldown:
    async def test_rate_limit_applies_cooldown(self, app_context) -> None:
        from app.database import session_scope

        scheduler = get_scheduler()
        scheduler.reset()
        async with session_scope() as session:
            credential = await _add(session, "limited")
            await scheduler.mark_failure(
                session,
                credential.id,
                category=ErrorCategory.RATE_LIMIT,
                detail="429",
                retry_after=30,
            )
            row = await session.get(QwenCredential, credential.id)
            assert row.status == "cooldown"
            assert row.cooldown_until is not None
            assert row.failure_count == 1

            with pytest.raises(GatewayError):
                await scheduler.acquire(session, provider="mock")

    async def test_cooldown_expiry_restores_token(self, app_context) -> None:
        from app.database import session_scope

        scheduler = get_scheduler()
        scheduler.reset()
        async with session_scope() as session:
            credential = await _add(session, "recovering")
            row = await session.get(QwenCredential, credential.id)
            row.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            row.status = "cooldown"
            await session.commit()

            acquired = await scheduler.acquire(session, provider="mock")
            assert acquired.id == credential.id

    async def test_success_clears_cooldown_state(self, app_context) -> None:
        from app.database import session_scope

        scheduler = get_scheduler()
        scheduler.reset()
        async with session_scope() as session:
            credential = await _add(session, "healing")
            await scheduler.mark_failure(
                session, credential.id, category=ErrorCategory.UPSTREAM, detail="500"
            )
            await scheduler.mark_success(session, credential.id)
            row = await session.get(QwenCredential, credential.id)
            assert row.status == "healthy"
            assert row.cooldown_until is None
            assert row.consecutive_failures == 0

    async def test_repeated_auth_failures_mark_invalid(self, app_context) -> None:
        from app.database import session_scope

        scheduler = get_scheduler()
        scheduler.reset()
        async with session_scope() as session:
            credential = await _add(session, "bad")
            for _ in range(3):
                await scheduler.mark_failure(
                    session, credential.id, category=ErrorCategory.AUTHENTICATION
                )
            row = await session.get(QwenCredential, credential.id)
            assert row.status == "invalid"


class TestConcurrency:
    async def test_concurrent_requests_spread_across_tokens(self, app_context) -> None:
        """100 simultaneous acquisitions must not all pick the same token."""
        from app.database import session_scope

        scheduler = get_scheduler()
        scheduler.reset()
        async with session_scope() as session:
            for i in range(4):
                await _add(session, f"c-{i}")

        async def worker() -> int:
            async with session_scope() as session:
                credential = await scheduler.acquire(session, provider="mock")
                await asyncio.sleep(0.01)
                await scheduler.release(credential.id)
                return credential.id

        results = await asyncio.gather(*[worker() for _ in range(100)])
        counts = {cid: results.count(cid) for cid in set(results)}
        assert len(counts) == 4, f"expected all tokens used, got {counts}"
        # No token should absorb a disproportionate share.
        assert max(counts.values()) <= 40, counts

    async def test_lease_counter_tracks_in_flight(self, app_context) -> None:
        from app.database import session_scope

        scheduler = get_scheduler()
        scheduler.reset()
        async with session_scope() as session:
            credential = await _add(session, "single")
            acquired = await scheduler.acquire(session, provider="mock")
            assert scheduler.in_flight(acquired.id) == 1
            await scheduler.release(acquired.id)
            assert scheduler.in_flight(credential.id) == 0


class TestFailoverIntegration:
    async def test_failover_to_healthy_token(self, client, auth_headers, app_context) -> None:
        """A 429 on the first token must transparently retry on another."""
        from app.database import session_scope
        from app.services.credential_service import create_credential

        get_scheduler().reset()
        async with session_scope() as session:
            bad = await create_credential(
                session, name="rate-limited", secret="mock:429", provider="mock"
            )
            await create_credential(session, name="good", secret="mock:normal", provider="mock")

        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"]

        async with session_scope() as session:
            row = await session.get(QwenCredential, bad.id)
            assert row.status == "cooldown"
            assert row.cooldown_until is not None

    async def test_all_tokens_failing_returns_normalized_error(
        self, client, auth_headers, app_context
    ) -> None:
        from app.database import session_scope
        from app.services.credential_service import create_credential

        get_scheduler().reset()
        async with session_scope() as session:
            for i in range(2):
                await create_credential(
                    session, name=f"bad-{i}", secret="mock:500", provider="mock"
                )

        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code in (502, 503)
        error = response.json()["error"]
        assert error["type"] in {"upstream_error", "no_credentials"}
        assert "mock" not in error["message"].lower() or "unavailable" in error["message"].lower()

    async def test_failover_records_attempts(self, client, auth_headers, app_context) -> None:
        from sqlalchemy import select

        from app.database import session_scope
        from app.models.db import RequestLog
        from app.services.credential_service import create_credential

        get_scheduler().reset()
        async with session_scope() as session:
            await create_credential(session, name="t1", secret="mock:429", provider="mock")
            await create_credential(session, name="t2", secret="mock:normal", provider="mock")

        await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        async with session_scope() as session:
            rows = list((await session.execute(select(RequestLog))).scalars())
        assert rows[0].attempts >= 2
        assert rows[0].status == "success"
