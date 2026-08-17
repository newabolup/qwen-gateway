"""Model routing.

Maps a client-supplied model name onto an upstream model, using (in order):

1. an explicit alias stored in the database (``ProviderModel.aliases``),
2. an alias from ``MODEL_ALIASES`` in the environment,
3. an exact match against a known upstream model,
4. the configured default model.

Nothing is hard-coded to a single Qwen model name.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.db import ProviderModel
from app.providers.base import ProviderModelInfo
from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class RouteDecision:
    requested_model: str
    upstream_model: str
    provider: str
    matched_by: str


class ModelRouter:
    """Resolves public model names to upstream models."""

    def __init__(self) -> None:
        self._settings = get_settings()

    async def resolve(
        self, session: AsyncSession, requested: str, *, provider: str | None = None
    ) -> RouteDecision:
        settings = self._settings
        provider = provider or settings.default_provider
        requested = (requested or settings.default_model).strip()
        key = requested.lower()

        # Preserve a scenario suffix (mock provider) while routing on the base.
        suffix = ""
        base = requested
        if "#" in requested:
            base, suffix = requested.split("#", 1)
            key = base.lower()
            suffix = f"#{suffix}"

        rows = list((await session.execute(select(ProviderModel))).scalars())

        for row in rows:
            if row.model_id.lower() == key and row.enabled:
                return RouteDecision(requested, row.model_id + suffix, row.provider, "exact")

        for row in rows:
            aliases = {a.strip().lower() for a in (row.aliases or "").split(",") if a.strip()}
            if key in aliases and row.enabled:
                return RouteDecision(requested, row.model_id + suffix, row.provider, "db_alias")

        env_aliases = settings.alias_map
        if key in env_aliases:
            return RouteDecision(requested, env_aliases[key] + suffix, provider, "env_alias")

        if provider == "mock":
            return RouteDecision(requested, base + suffix, provider, "passthrough")

        if not rows and base:
            # No catalogue yet (fresh install, discovery not run): pass through
            # rather than silently rewriting the caller's model.
            return RouteDecision(requested, base + suffix, provider, "passthrough")

        log.warning("model_alias_fallback", requested_model=requested)
        return RouteDecision(requested, settings.default_model + suffix, provider, "default")

    async def catalogue(
        self, session: AsyncSession, provider: str | None = None
    ) -> list[ProviderModelInfo]:
        stmt = select(ProviderModel).where(ProviderModel.enabled.is_(True))
        if provider:
            stmt = stmt.where(ProviderModel.provider == provider)
        rows = list((await session.execute(stmt)).scalars())
        return [
            ProviderModelInfo(
                id=row.model_id,
                display_name=row.display_name,
                context_window=row.context_window,
                supports_tools=row.supports_tools,
                supports_reasoning=row.supports_reasoning,
                aliases=[a.strip() for a in (row.aliases or "").split(",") if a.strip()],
            )
            for row in rows
        ]


_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


def reset_router() -> None:
    global _router
    _router = None
