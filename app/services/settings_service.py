"""Persistent settings layer.

Environment variables provide defaults; values stored in ``gateway_settings``
override them at runtime so an operator can change behaviour from the admin UI
without redeploying. Secrets are never stored here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.db import GatewaySetting
from app.utils.logging import get_logger

log = get_logger(__name__)

#: Only these keys may be overridden at runtime.
MUTABLE_KEYS = {
    "scheduler_strategy": str,
    "expose_reasoning": bool,
    "default_model": str,
    "default_provider": str,
    "request_log_retention_days": int,
    "store_request_bodies": bool,
}


def _parse(value: str, kind: type) -> Any:
    if kind is bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if kind is int:
        try:
            return int(value)
        except ValueError:
            return 0
    return value


async def load_overrides(session: AsyncSession) -> dict[str, Any]:
    rows = list((await session.execute(select(GatewaySetting))).scalars())
    return {
        row.key: _parse(row.value, MUTABLE_KEYS[row.key]) for row in rows if row.key in MUTABLE_KEYS
    }


async def apply_overrides(session: AsyncSession) -> None:
    """Push persisted overrides onto the in-process settings object."""
    settings = get_settings()
    overrides = await load_overrides(session)
    for key, value in overrides.items():
        try:
            object.__setattr__(settings, key, value)
        except (AttributeError, ValueError) as exc:  # pragma: no cover - defensive
            log.warning("setting_override_failed", key=key, detail=str(exc))
    if overrides:
        log.info("settings_overrides_applied", keys=sorted(overrides))


async def update_settings(session: AsyncSession, values: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    applied: dict[str, Any] = {}
    for key, value in values.items():
        if value is None or key not in MUTABLE_KEYS:
            continue
        row = await session.get(GatewaySetting, key)
        text = str(value).lower() if isinstance(value, bool) else str(value)
        if row is None:
            session.add(GatewaySetting(key=key, value=text))
        else:
            row.value = text
        object.__setattr__(settings, key, value)
        applied[key] = value
    await session.commit()
    if applied:
        log.info("settings_updated", keys=sorted(applied))
    return applied


def public_settings_snapshot() -> dict[str, Any]:
    """Non-sensitive view of the effective configuration."""
    settings = get_settings()
    from app.auth.admin import admin_configured

    return {
        "app_env": settings.app_env,
        "default_provider": settings.default_provider,
        "scheduler_strategy": settings.scheduler_strategy,
        "expose_reasoning": settings.expose_reasoning,
        "default_model": settings.default_model,
        "model_aliases": settings.alias_map,
        "qwen_mode": settings.qwen_mode,
        "request_log_retention_days": settings.request_log_retention_days,
        "store_request_bodies": settings.store_request_bodies,
        "max_failover_attempts": settings.max_failover_attempts,
        "default_cooldown_seconds": settings.default_cooldown_seconds,
        "rate_limit_cooldown_seconds": settings.rate_limit_cooldown_seconds,
        "mock_provider_enabled": settings.enable_mock_provider,
        "secret_key_configured": bool(settings.gateway_secret_key),
        "admin_configured": admin_configured(),
    }
