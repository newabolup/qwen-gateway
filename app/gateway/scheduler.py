"""Concurrency-safe credential scheduler.

Responsibilities:

* pick a healthy credential (round-robin or least-recently-used),
* never hand the same credential to a burst of concurrent requests simply
  because the DB has not been updated yet (an in-memory lease counter and an
  asyncio lock make selection atomic),
* apply cooldowns after rate limits / upstream faults,
* support failover by excluding already-tried credentials.

State that must survive a restart (counters, cooldown_until, status) lives in
the database; hot state (lease counts, in-flight usage) lives here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.gateway import errors
from app.models.db import QwenCredential, utcnow
from app.providers.base import ProviderCredential
from app.security.crypto import DecryptionError, decrypt_secret
from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class SchedulerStats:
    total: int = 0
    enabled: int = 0
    healthy: int = 0
    cooling_down: int = 0
    disabled: int = 0
    expired: int = 0
    in_flight: dict[int, int] = field(default_factory=dict)


class CredentialScheduler:
    """Selects credentials for outbound requests."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cursor = 0
        #: credential_id -> number of in-flight requests currently using it
        self._leases: dict[int, int] = {}
        #: credential_id -> monotonic-ish selection order (for LRU tie-breaks)
        self._last_selected: dict[int, float] = {}
        self._tick = 0.0

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    async def acquire(
        self,
        session: AsyncSession,
        *,
        provider: str = "qwen",
        exclude_ids: Iterable[int] = (),
        strategy: str | None = None,
    ) -> ProviderCredential:
        """Return a decrypted credential ready for use.

        Raises :class:`GatewayError` (``no_credentials``) when nothing is
        eligible.
        """
        settings = get_settings()
        strategy = strategy or settings.scheduler_strategy
        excluded = set(exclude_ids)

        async with self._lock:
            candidates = await self._eligible(session, provider=provider)
            usable = [c for c in candidates if c.id not in excluded]
            if not usable:
                detail = (
                    f"provider={provider} candidates={len(candidates)} excluded={len(excluded)}"
                )
                raise errors.no_credentials(detail)

            chosen = self._choose(usable, strategy)
            self._tick += 1.0
            self._last_selected[chosen.id] = self._tick
            self._leases[chosen.id] = self._leases.get(chosen.id, 0) + 1
            chosen.last_used_at = utcnow()
            chosen.request_count += 1
            # Commit immediately: holding an open write transaction for the
            # duration of a (possibly minutes-long) upstream call would block
            # every other writer, and locks SQLite entirely.
            await session.commit()

        try:
            secret = decrypt_secret(chosen.encrypted_secret)
            refresh = (
                decrypt_secret(chosen.encrypted_refresh_secret)
                if chosen.encrypted_refresh_secret
                else None
            )
        except DecryptionError as exc:
            await self.release(chosen.id)
            await self.mark_failure(
                session,
                chosen.id,
                category=errors.ErrorCategory.AUTHENTICATION,
                detail="credential could not be decrypted",
                cooldown_seconds=3600,
            )
            raise errors.no_credentials(str(exc)) from exc

        log.info(
            "token_selected",
            credential_id=chosen.id,
            credential_name=chosen.name,
            strategy=strategy,
            in_flight=self._leases.get(chosen.id, 0),
        )
        return ProviderCredential(
            id=chosen.id,
            name=chosen.name,
            secret=secret,
            auth_mode=chosen.auth_mode,
            refresh_secret=refresh,
            base_url=chosen.base_url,
            expires_at=chosen.expires_at.timestamp() if chosen.expires_at else None,
        )

    def _choose(self, candidates: Sequence[QwenCredential], strategy: str) -> QwenCredential:
        if strategy == "least_recently_used":

            def lru_key(cred: QwenCredential) -> tuple[int, float, int]:
                last_used = cred.last_used_at.timestamp() if cred.last_used_at else 0.0
                return (
                    self._leases.get(cred.id, 0),
                    max(last_used, self._last_selected.get(cred.id, 0.0) * 1e-6),
                    cred.id,
                )

            return min(candidates, key=lru_key)

        # Round robin: rotate a shared cursor, biased away from busy credentials.
        ordered = sorted(candidates, key=lambda c: c.id)
        count = len(ordered)
        best: QwenCredential | None = None
        best_load = None
        for offset in range(count):
            candidate = ordered[(self._cursor + offset) % count]
            load = self._leases.get(candidate.id, 0)
            if load == 0:
                self._cursor = (self._cursor + offset + 1) % count
                return candidate
            if best_load is None or load < best_load:
                best, best_load = candidate, load
        self._cursor = (self._cursor + 1) % count
        assert best is not None
        return best

    async def _eligible(self, session: AsyncSession, *, provider: str) -> list[QwenCredential]:
        now = utcnow()
        stmt = select(QwenCredential).where(
            QwenCredential.provider == provider,
            QwenCredential.enabled.is_(True),
        )
        rows = list((await session.execute(stmt)).scalars())
        eligible: list[QwenCredential] = []
        for row in rows:
            if row.cooldown_until is not None and _aware(row.cooldown_until) > now:
                continue
            if row.expires_at is not None and _aware(row.expires_at) <= now:
                continue
            if row.status == "invalid":
                continue
            eligible.append(row)
        return eligible

    # ------------------------------------------------------------------
    # Outcome reporting
    # ------------------------------------------------------------------
    async def release(self, credential_id: int) -> None:
        async with self._lock:
            current = self._leases.get(credential_id, 0)
            if current <= 1:
                self._leases.pop(credential_id, None)
            else:
                self._leases[credential_id] = current - 1

    async def mark_success(self, session: AsyncSession, credential_id: int) -> None:
        await session.execute(
            update(QwenCredential)
            .where(QwenCredential.id == credential_id)
            .values(
                status="healthy",
                status_reason=None,
                last_success_at=utcnow(),
                success_count=QwenCredential.success_count + 1,
                consecutive_failures=0,
                cooldown_until=None,
            )
        )
        await session.commit()

    async def mark_failure(
        self,
        session: AsyncSession,
        credential_id: int,
        *,
        category: str,
        detail: str | None = None,
        cooldown_seconds: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        """Record a failure and apply the appropriate cooldown."""
        settings = get_settings()
        credential = await session.get(QwenCredential, credential_id)
        if credential is None:
            return

        credential.failure_count += 1
        credential.consecutive_failures += 1
        credential.last_error_at = utcnow()
        credential.status_reason = (detail or category)[:255]

        cooldown = cooldown_seconds
        if cooldown is None:
            if category == errors.ErrorCategory.RATE_LIMIT:
                cooldown = retry_after or settings.rate_limit_cooldown_seconds
            elif category in (
                errors.ErrorCategory.AUTHENTICATION,
                errors.ErrorCategory.PERMISSION,
            ):
                cooldown = settings.default_cooldown_seconds * 2
            elif category in (
                errors.ErrorCategory.UPSTREAM,
                errors.ErrorCategory.TIMEOUT,
                errors.ErrorCategory.NETWORK,
                errors.ErrorCategory.PARSE,
            ):
                # Exponential-ish backoff on repeated transport failures.
                cooldown = min(
                    settings.default_cooldown_seconds,
                    15 * (2 ** min(credential.consecutive_failures - 1, 4)),
                )
            else:
                cooldown = 0

        if category == errors.ErrorCategory.RATE_LIMIT:
            credential.status = "cooldown"
        elif category in (
            errors.ErrorCategory.AUTHENTICATION,
            errors.ErrorCategory.PERMISSION,
        ):
            credential.status = "invalid" if credential.consecutive_failures >= 3 else "degraded"
        else:
            credential.status = "degraded"

        if cooldown and cooldown > 0:
            credential.cooldown_until = utcnow() + timedelta(seconds=cooldown)
            log.warning(
                "token_cooldown",
                credential_id=credential_id,
                cooldown_seconds=cooldown,
                error_category=category,
            )
        await session.commit()

    async def persist_rotation(
        self,
        session: AsyncSession,
        credential_id: int,
        *,
        encrypted_secret: str | None = None,
        encrypted_refresh_secret: str | None = None,
        expires_at: datetime | None = None,
        base_url: str | None = None,
    ) -> None:
        """Persist refreshed credential material returned by a provider."""
        values: dict[str, object] = {}
        if encrypted_secret:
            values["encrypted_secret"] = encrypted_secret
        if encrypted_refresh_secret:
            values["encrypted_refresh_secret"] = encrypted_refresh_secret
        if expires_at is not None:
            values["expires_at"] = expires_at
        if base_url:
            values["base_url"] = base_url
        if not values:
            return
        await session.execute(
            update(QwenCredential).where(QwenCredential.id == credential_id).values(**values)
        )
        await session.commit()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    async def stats(self, session: AsyncSession, provider: str = "qwen") -> SchedulerStats:
        rows = list(
            (
                await session.execute(
                    select(QwenCredential).where(QwenCredential.provider == provider)
                )
            ).scalars()
        )
        now = utcnow()
        stats = SchedulerStats(total=len(rows), in_flight=dict(self._leases))
        for row in rows:
            if not row.enabled:
                stats.disabled += 1
                continue
            stats.enabled += 1
            if row.expires_at is not None and _aware(row.expires_at) <= now:
                stats.expired += 1
            elif row.cooldown_until is not None and _aware(row.cooldown_until) > now:
                stats.cooling_down += 1
            elif row.status != "invalid":
                stats.healthy += 1
        return stats

    def in_flight(self, credential_id: int) -> int:
        return self._leases.get(credential_id, 0)

    def reset(self) -> None:
        """Test hook: clear hot state."""
        self._leases.clear()
        self._last_selected.clear()
        self._cursor = 0
        self._tick = 0.0


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


_scheduler: CredentialScheduler | None = None


def get_scheduler() -> CredentialScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = CredentialScheduler()
    return _scheduler
