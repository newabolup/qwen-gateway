"""Request recording, statistics and retention."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import Usage
from app.config import get_settings
from app.gateway.errors import GatewayError
from app.models.db import ApiKey, QwenCredential, RequestLog, utcnow
from app.utils.logging import get_logger
from app.utils.redaction import redact_value

log = get_logger(__name__)

#: Live counter of in-flight streaming responses (process-local).
_ACTIVE_STREAMS = {"count": 0}


def stream_started() -> None:
    _ACTIVE_STREAMS["count"] += 1


def stream_finished() -> None:
    _ACTIVE_STREAMS["count"] = max(0, _ACTIVE_STREAMS["count"] - 1)


def active_streams() -> int:
    return _ACTIVE_STREAMS["count"]


@dataclass(slots=True)
class RequestRecorder:
    """Accumulates facts about one inbound request, then persists them."""

    request_id: str
    endpoint: str
    api_key_id: int | None = None
    api_key_name: str | None = None
    provider: str = "qwen"
    model: str = ""
    upstream_model: str | None = None
    credential_id: int | None = None
    credential_name: str | None = None
    streaming: bool = False
    attempts: int = 1
    status: str = "pending"
    status_code: int | None = None
    error_category: str | None = None
    error_message: str | None = None
    latency_ms: float | None = None
    first_token_ms: float | None = None
    usage: Usage = field(default_factory=Usage)
    warnings: list[str] = field(default_factory=list)
    request_preview: str | None = None

    def set_model(self, model: str, upstream_model: str, provider: str) -> None:
        self.model = model
        self.upstream_model = upstream_model
        self.provider = provider

    def set_credential(self, credential_id: int, name: str) -> None:
        self.credential_id = credential_id
        self.credential_name = name

    def set_streaming(self, streaming: bool) -> None:
        self.streaming = streaming

    def set_attempts(self, attempts: int) -> None:
        self.attempts = attempts

    def set_first_token(self, ms: float) -> None:
        if self.first_token_ms is None:
            self.first_token_ms = ms

    def set_success(
        self, *, latency_ms: float, usage: Usage, warnings: list[str] | None = None
    ) -> None:
        self.status = "success"
        self.status_code = 200
        self.latency_ms = latency_ms
        self.usage = usage
        self.error_category = None
        self.error_message = None
        if warnings:
            self.warnings = warnings[:10]

    def set_error(self, error: GatewayError) -> None:
        self.status = "error"
        self.status_code = error.status_code
        self.error_category = error.category
        # Only the client-safe message is stored; internal_detail stays in logs.
        self.error_message = error.message[:500]

    def capture_request_body(self, payload: dict[str, Any]) -> None:
        """Store a redacted, truncated preview when explicitly enabled."""
        if not get_settings().store_request_bodies:
            return
        safe = redact_value(payload)
        try:
            text = json.dumps(safe, ensure_ascii=False)[:4000]
        except (TypeError, ValueError):
            return
        self.request_preview = text

    async def persist(self, session: AsyncSession) -> None:
        entry = RequestLog(
            request_id=self.request_id,
            api_key_id=self.api_key_id,
            api_key_name=self.api_key_name,
            credential_id=self.credential_id,
            credential_name=self.credential_name,
            provider=self.provider,
            model=self.model,
            upstream_model=self.upstream_model,
            endpoint=self.endpoint,
            streaming=self.streaming,
            status=self.status,
            status_code=self.status_code,
            error_category=self.error_category,
            error_message=self.error_message,
            latency_ms=self.latency_ms,
            first_token_ms=self.first_token_ms,
            attempts=self.attempts,
            prompt_tokens=self.usage.prompt_tokens or None,
            completion_tokens=self.usage.completion_tokens or None,
            total_tokens=self.usage.total_tokens or None,
            request_preview=self.request_preview,
        )
        session.add(entry)

        if self.api_key_id is not None:
            api_key = await session.get(ApiKey, self.api_key_id)
            if api_key is not None:
                api_key.request_count += 1
                api_key.last_used_at = utcnow()
                if self.status == "success":
                    api_key.success_count += 1
                elif self.status == "error":
                    api_key.failure_count += 1
        await session.commit()


async def gather_stats(session: AsyncSession) -> dict[str, Any]:
    """Dashboard statistics."""
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)

    total_today = await session.scalar(
        select(func.count(RequestLog.id)).where(RequestLog.created_at >= day_ago)
    )
    success_today = await session.scalar(
        select(func.count(RequestLog.id)).where(
            RequestLog.created_at >= day_ago, RequestLog.status == "success"
        )
    )
    failed_today = await session.scalar(
        select(func.count(RequestLog.id)).where(
            RequestLog.created_at >= day_ago, RequestLog.status == "error"
        )
    )
    avg_latency = await session.scalar(
        select(func.avg(RequestLog.latency_ms)).where(
            RequestLog.created_at >= day_ago, RequestLog.status == "success"
        )
    )
    total_all = await session.scalar(select(func.count(RequestLog.id)))

    credentials = list((await session.execute(select(QwenCredential))).scalars())
    healthy = cooling = disabled = expired = 0
    for cred in credentials:
        if not cred.enabled:
            disabled += 1
            continue
        cooldown = _aware(cred.cooldown_until)
        expires = _aware(cred.expires_at)
        if expires is not None and expires <= now:
            expired += 1
        elif cooldown is not None and cooldown > now:
            cooling += 1
        elif cred.status != "invalid":
            healthy += 1

    api_keys_total = await session.scalar(select(func.count(ApiKey.id)))
    api_keys_active = await session.scalar(
        select(func.count(ApiKey.id)).where(ApiKey.enabled.is_(True), ApiKey.revoked.is_(False))
    )

    recent_errors_rows = list(
        (
            await session.execute(
                select(RequestLog)
                .where(RequestLog.status == "error")
                .order_by(RequestLog.created_at.desc())
                .limit(10)
            )
        ).scalars()
    )

    return {
        "requests_today": int(total_today or 0),
        "requests_total": int(total_all or 0),
        "successful_requests": int(success_today or 0),
        "failed_requests": int(failed_today or 0),
        "average_latency_ms": round(float(avg_latency), 2) if avg_latency else 0.0,
        "active_streams": active_streams(),
        "tokens": {
            "total": len(credentials),
            "healthy": healthy,
            "cooldown": cooling,
            "disabled": disabled,
            "expired": expired,
        },
        "api_keys": {
            "total": int(api_keys_total or 0),
            "active": int(api_keys_active or 0),
        },
        "recent_errors": [
            {
                "request_id": row.request_id,
                "created_at": _iso(row.created_at),
                "model": row.model,
                "category": row.error_category,
                "message": row.error_message,
                "credential_id": row.credential_id,
            }
            for row in recent_errors_rows
        ],
    }


async def purge_old_logs(session: AsyncSession) -> int:
    """Delete request logs older than the configured retention window."""
    days = get_settings().request_log_retention_days
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(delete(RequestLog).where(RequestLog.created_at < cutoff))
    await session.commit()
    removed = int(result.rowcount or 0)
    if removed:
        log.info("request_logs_purged", removed=removed, retention_days=days)
    return removed


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    aware = _aware(value)
    return aware.isoformat() if aware else None
