"""Model catalogue management and discovery."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.db import ProviderModel, QwenCredential, utcnow
from app.providers.base import ProviderModelInfo
from app.providers.registry import get_provider
from app.security.crypto import DecryptionError
from app.utils.logging import get_logger

log = get_logger(__name__)


async def list_models(session: AsyncSession) -> list[ProviderModel]:
    return list(
        (await session.execute(select(ProviderModel).order_by(ProviderModel.model_id))).scalars()
    )


async def upsert_model(
    session: AsyncSession,
    *,
    model_id: str,
    provider: str = "qwen",
    display_name: str | None = None,
    aliases: list[str] | None = None,
    enabled: bool = True,
    context_window: int | None = None,
    supports_tools: bool = True,
    supports_reasoning: bool = False,
) -> ProviderModel:
    existing = (
        await session.execute(
            select(ProviderModel).where(
                ProviderModel.provider == provider, ProviderModel.model_id == model_id
            )
        )
    ).scalar_one_or_none()

    alias_text = ",".join(sorted({a.strip() for a in (aliases or []) if a.strip()}))
    if existing is None:
        existing = ProviderModel(
            provider=provider,
            model_id=model_id,
            display_name=display_name or model_id,
            aliases=alias_text,
            enabled=enabled,
            context_window=context_window,
            supports_tools=supports_tools,
            supports_reasoning=supports_reasoning,
        )
        session.add(existing)
    else:
        existing.display_name = display_name or existing.display_name
        if aliases is not None:
            existing.aliases = alias_text
        existing.enabled = enabled
        existing.context_window = context_window or existing.context_window
        existing.supports_tools = supports_tools
        existing.supports_reasoning = supports_reasoning
        existing.discovered_at = utcnow()
    await session.commit()
    await session.refresh(existing)
    return existing


async def delete_model(session: AsyncSession, model_pk: int) -> bool:
    model = await session.get(ProviderModel, model_pk)
    if model is None:
        return False
    await session.delete(model)
    await session.commit()
    return True


async def discover_models(session: AsyncSession) -> list[ProviderModel]:
    """Refresh the catalogue from upstream using the first usable credential."""
    settings = get_settings()
    provider_name = settings.default_provider
    provider = get_provider(provider_name)

    credential = None
    rows = list(
        (
            await session.execute(
                select(QwenCredential).where(
                    QwenCredential.provider == provider_name,
                    QwenCredential.enabled.is_(True),
                )
            )
        ).scalars()
    )
    from app.services.credential_service import to_provider_credential

    for row in rows:
        try:
            credential = await to_provider_credential(row)
            break
        except DecryptionError:
            continue

    discovered: list[ProviderModelInfo] = await provider.list_models(credential)
    alias_map = settings.alias_map
    reverse: dict[str, list[str]] = {}
    for alias, target in alias_map.items():
        reverse.setdefault(target, []).append(alias)

    saved: list[ProviderModel] = []
    for info in discovered:
        saved.append(
            await upsert_model(
                session,
                model_id=info.id,
                provider=provider_name,
                display_name=info.display_name,
                aliases=sorted(set(info.aliases) | set(reverse.get(info.id, []))),
                enabled=True,
                context_window=info.context_window,
                supports_tools=info.supports_tools,
                supports_reasoning=info.supports_reasoning,
            )
        )
    log.info("models_discovered", count=len(saved), provider=provider_name)
    return saved


async def ensure_seed_models(session: AsyncSession) -> None:
    """Populate an empty catalogue so /v1/models is useful on first boot."""
    existing = await session.execute(select(ProviderModel.id).limit(1))
    if existing.first() is not None:
        return

    settings = get_settings()
    provider_name = settings.default_provider
    if provider_name == "mock":
        from app.providers.mock.client import MOCK_MODELS

        catalogue = list(MOCK_MODELS)
    else:
        from app.providers.qwen.client import FALLBACK_MODELS

        catalogue = list(FALLBACK_MODELS)

    reverse: dict[str, list[str]] = {}
    for alias, target in settings.alias_map.items():
        reverse.setdefault(target, []).append(alias)

    for info in catalogue:
        await upsert_model(
            session,
            model_id=info.id,
            provider=provider_name,
            display_name=info.display_name,
            aliases=reverse.get(info.id, []),
            context_window=info.context_window,
            supports_tools=info.supports_tools,
            supports_reasoning=info.supports_reasoning,
        )
    log.info("seed_models_created", count=len(catalogue), provider=provider_name)
