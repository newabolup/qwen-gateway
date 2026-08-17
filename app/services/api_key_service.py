"""Client API-key management."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway import errors
from app.models.db import ApiKey
from app.security.hashing import api_key_preview, generate_api_key, hash_api_key
from app.utils.logging import get_logger

log = get_logger(__name__)


async def list_api_keys(session: AsyncSession) -> list[ApiKey]:
    return list((await session.execute(select(ApiKey).order_by(ApiKey.id.asc()))).scalars())


async def create_api_key(
    session: AsyncSession,
    *,
    name: str,
    description: str | None = None,
    expires_in_days: int | None = None,
) -> tuple[ApiKey, str]:
    """Create a key. The plaintext value is returned once and never stored."""
    plaintext = generate_api_key()
    record = ApiKey(
        name=name.strip(),
        description=(description or "").strip() or None,
        key_hash=hash_api_key(plaintext),
        key_preview=api_key_preview(plaintext),
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=expires_in_days)
            if expires_in_days
            else None
        ),
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    # Only the preview is logged — never the key itself.
    log.info("api_key_created", api_key_id=record.id, key_preview=record.key_preview)
    return record, plaintext


async def update_api_key(
    session: AsyncSession,
    key_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
) -> ApiKey:
    record = await session.get(ApiKey, key_id)
    if record is None:
        raise errors.not_found(f"API key {key_id} not found.")
    if name is not None:
        record.name = name.strip()
    if description is not None:
        record.description = description.strip() or None
    if enabled is not None:
        record.enabled = enabled
    await session.commit()
    await session.refresh(record)
    log.info("api_key_updated", api_key_id=key_id)
    return record


async def revoke_api_key(session: AsyncSession, key_id: int) -> ApiKey:
    record = await session.get(ApiKey, key_id)
    if record is None:
        raise errors.not_found(f"API key {key_id} not found.")
    record.revoked = True
    record.enabled = False
    await session.commit()
    await session.refresh(record)
    log.info("api_key_revoked", api_key_id=key_id)
    return record


async def delete_api_key(session: AsyncSession, key_id: int) -> None:
    record = await session.get(ApiKey, key_id)
    if record is None:
        raise errors.not_found(f"API key {key_id} not found.")
    await session.delete(record)
    await session.commit()
    log.info("api_key_deleted", api_key_id=key_id)
