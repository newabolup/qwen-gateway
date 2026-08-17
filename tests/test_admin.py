"""Admin API tests."""

from __future__ import annotations


class TestSession:
    async def test_login_and_session(self, client) -> None:
        info = (await client.get("/api/admin/session")).json()
        assert info["authenticated"] is False
        assert info["admin_configured"] is True

        response = await client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "test-admin-password"},
        )
        assert response.status_code == 200
        assert response.json()["username"] == "admin"

        info = (await client.get("/api/admin/session")).json()
        assert info["authenticated"] is True


class TestCredentialManagement:
    async def test_full_lifecycle(self, admin_client) -> None:
        created = await admin_client.post(
            "/api/admin/credentials",
            json={"name": "acct-1", "secret": "mock:normal", "provider": "mock"},
        )
        assert created.status_code == 201
        credential = created.json()
        assert credential["name"] == "acct-1"
        assert credential["enabled"] is True
        assert credential["status"] == "unknown"
        cid = credential["id"]

        renamed = await admin_client.patch(
            f"/api/admin/credentials/{cid}", json={"name": "renamed"}
        )
        assert renamed.json()["name"] == "renamed"

        disabled = await admin_client.patch(
            f"/api/admin/credentials/{cid}", json={"enabled": False}
        )
        assert disabled.json()["enabled"] is False

        tested = await admin_client.post(f"/api/admin/credentials/{cid}/test")
        assert tested.status_code == 200
        assert tested.json()["healthy"] is True

        listed = await admin_client.get("/api/admin/credentials")
        assert len(listed.json()) == 1

        deleted = await admin_client.delete(f"/api/admin/credentials/{cid}")
        assert deleted.status_code == 200
        assert (await admin_client.get("/api/admin/credentials")).json() == []

    async def test_test_endpoint_reports_unhealthy(self, admin_client) -> None:
        created = await admin_client.post(
            "/api/admin/credentials",
            json={"name": "bad", "secret": "mock:invalid", "provider": "mock"},
        )
        cid = created.json()["id"]
        result = await admin_client.post(f"/api/admin/credentials/{cid}/test")
        assert result.json()["healthy"] is False

    async def test_clear_cooldown(self, admin_client, app_context) -> None:
        from datetime import datetime, timedelta, timezone

        from app.database import session_scope
        from app.models.db import QwenCredential

        created = await admin_client.post(
            "/api/admin/credentials", json={"name": "cd", "secret": "mock:normal"}
        )
        cid = created.json()["id"]
        async with session_scope() as session:
            row = await session.get(QwenCredential, cid)
            row.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=1)
            row.status = "cooldown"

        cleared = await admin_client.patch(
            f"/api/admin/credentials/{cid}", json={"clear_cooldown": True}
        )
        assert cleared.json()["cooldown_until"] is None

    async def test_delete_missing_returns_404(self, admin_client) -> None:
        response = await admin_client.delete("/api/admin/credentials/999")
        assert response.status_code == 404

    async def test_short_secret_rejected(self, admin_client) -> None:
        response = await admin_client.post(
            "/api/admin/credentials", json={"name": "x", "secret": "abc"}
        )
        assert response.status_code in (400, 422)


class TestApiKeyManagement:
    async def test_lifecycle(self, admin_client) -> None:
        created = await admin_client.post(
            "/api/admin/api-keys", json={"name": "cli", "description": "Claude Code"}
        )
        assert created.status_code == 201
        body = created.json()
        assert body["api_key"].startswith("qwg_")
        key_id = body["id"]

        updated = await admin_client.patch(f"/api/admin/api-keys/{key_id}", json={"enabled": False})
        assert updated.json()["enabled"] is False

        revoked = await admin_client.post(f"/api/admin/api-keys/{key_id}/revoke")
        assert revoked.json()["revoked"] is True

        deleted = await admin_client.delete(f"/api/admin/api-keys/{key_id}")
        assert deleted.status_code == 200

    async def test_usage_statistics_tracked(self, admin_client, app_context) -> None:
        from app.database import session_scope
        from app.services.credential_service import create_credential

        async with session_scope() as session:
            await create_credential(session, name="c", secret="mock:normal", provider="mock")

        created = await admin_client.post("/api/admin/api-keys", json={"name": "stats"})
        plaintext = created.json()["api_key"]

        await admin_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {plaintext}"},
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        keys = (await admin_client.get("/api/admin/api-keys")).json()
        entry = next(k for k in keys if k["name"] == "stats")
        assert entry["request_count"] == 1
        assert entry["success_count"] == 1
        assert entry["last_used_at"] is not None


class TestModels:
    async def test_seeded_and_upsert(self, admin_client) -> None:
        listing = (await admin_client.get("/api/admin/models")).json()
        assert listing, "models should be seeded on startup"

        created = await admin_client.post(
            "/api/admin/models",
            json={
                "model_id": "custom-model",
                "aliases": ["fast", "default-fast"],
                "provider": "mock",
            },
        )
        assert created.status_code == 200
        assert set(created.json()["aliases"]) == {"fast", "default-fast"}

    async def test_alias_routes_request(self, admin_client, auth_headers, app_context) -> None:
        from app.database import session_scope
        from app.services.credential_service import create_credential

        async with session_scope() as session:
            await create_credential(session, name="c", secret="mock:normal", provider="mock")

        await admin_client.post(
            "/api/admin/models",
            json={"model_id": "mock-qwen", "aliases": ["house-model"], "provider": "mock"},
        )
        response = await admin_client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "house-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.json()["model"] == "house-model"

    async def test_discovery(self, admin_client) -> None:
        discovered = await admin_client.post("/api/admin/models/discover")
        assert discovered.status_code == 200
        assert any(m["model_id"] == "mock-qwen" for m in discovered.json())


class TestRequestsAndLogs:
    async def test_request_search_and_filter(self, admin_client, auth_headers, app_context) -> None:
        from app.database import session_scope
        from app.services.credential_service import create_credential

        async with session_scope() as session:
            await create_credential(session, name="c", secret="mock:normal", provider="mock")

        await admin_client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        listing = (await admin_client.get("/api/admin/requests")).json()
        assert listing["total"] == 1
        item = listing["items"][0]
        assert item["status"] == "success"
        assert item["model"] == "mock-qwen"

        filtered = (await admin_client.get("/api/admin/requests?status=error")).json()
        assert filtered["total"] == 0

        searched = (await admin_client.get("/api/admin/requests?search=mock-qwen")).json()
        assert searched["total"] == 1

    async def test_logs_endpoint(self, admin_client) -> None:
        logs = await admin_client.get("/api/admin/logs?limit=50")
        assert logs.status_code == 200
        assert isinstance(logs.json(), list)

    async def test_purge(self, admin_client) -> None:
        purged = await admin_client.post("/api/admin/requests/purge")
        assert purged.status_code == 200


class TestSettings:
    async def test_read_and_update(self, admin_client) -> None:
        current = (await admin_client.get("/api/admin/settings")).json()
        assert current["default_provider"] == "mock"
        assert current["secret_key_configured"] is True
        assert current["admin_configured"] is True

        updated = await admin_client.patch(
            "/api/admin/settings",
            json={"scheduler_strategy": "least_recently_used", "expose_reasoning": True},
        )
        assert updated.json()["scheduler_strategy"] == "least_recently_used"
        assert updated.json()["expose_reasoning"] is True

    async def test_expose_reasoning_setting_takes_effect(
        self, admin_client, auth_headers, app_context
    ) -> None:
        from app.database import session_scope
        from app.services.credential_service import create_credential

        async with session_scope() as session:
            await create_credential(session, name="r", secret="mock:reasoning", provider="mock")

        hidden = await admin_client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert "reasoning_content" not in hidden.text

        await admin_client.patch("/api/admin/settings", json={"expose_reasoning": True})
        shown = await admin_client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        message = shown.json()["choices"][0]["message"]
        assert message.get("reasoning_content")
        assert "greeted me" in message["reasoning_content"]
        assert "greeted me" not in (message["content"] or "")

    async def test_invalid_setting_value_rejected(self, admin_client) -> None:
        response = await admin_client.patch(
            "/api/admin/settings", json={"scheduler_strategy": "nonsense"}
        )
        assert response.status_code == 422


class TestOverview:
    async def test_overview_payload(self, admin_client) -> None:
        overview = (await admin_client.get("/api/admin/overview")).json()
        for key in (
            "requests_today",
            "successful_requests",
            "failed_requests",
            "average_latency_ms",
            "active_streams",
            "tokens",
            "api_keys",
            "recent_errors",
            "scheduler",
            "providers",
        ):
            assert key in overview


class TestTimestampSerialization:
    """SQLite returns naive datetimes; the API must emit explicit UTC."""

    async def test_credential_timestamps_are_utc_aware(self, admin_client) -> None:
        await admin_client.post(
            "/api/admin/credentials", json={"name": "tz", "secret": "mock:normal"}
        )
        listing = (await admin_client.get("/api/admin/credentials")).json()
        created = listing[0]["created_at"]
        assert created.endswith("+00:00"), created

    async def test_api_key_timestamps_are_utc_aware(self, admin_client) -> None:
        created = await admin_client.post("/api/admin/api-keys", json={"name": "tz"})
        assert created.json()["created_at"].endswith("+00:00")

    async def test_request_log_timestamps_are_utc_aware(
        self, admin_client, auth_headers, app_context
    ) -> None:
        from app.database import session_scope
        from app.services.credential_service import create_credential

        async with session_scope() as session:
            await create_credential(session, name="c", secret="mock:normal", provider="mock")
        await admin_client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        items = (await admin_client.get("/api/admin/requests")).json()["items"]
        assert items[0]["created_at"].endswith("+00:00")
