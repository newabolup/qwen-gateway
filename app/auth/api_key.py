"""Client API-key authentication for the public API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.gateway import errors
from app.models.db import ApiKey
from app.security.hashing import hash_api_key
from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class AuthenticatedKey:
    id: int
    name: str


def extract_bearer_token(request: Request) -> str | None:
    """Read the client key from the standard places OpenAI clients use."""
    header = request.headers.get("authorization")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        if header.strip() and not _:
            return header.strip()
    api_key_header = request.headers.get("x-api-key")
    if api_key_header:
        return api_key_header.strip()
    return None


async def authenticate_api_key(
    request: Request, session: AsyncSession = Depends(get_session)
) -> AuthenticatedKey:
    """FastAPI dependency enforcing gateway API-key auth."""
    token = extract_bearer_token(request)
    if not token:
        raise errors.unauthorized("Missing API key. Send 'Authorization: Bearer qwg_...'.")

    digest = hash_api_key(token)
    record = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == digest))
    ).scalar_one_or_none()

    if record is None:
        log.warning("auth_failed", reason="unknown_key")
        raise errors.unauthorized()
    if record.revoked:
        log.warning("auth_failed", reason="revoked_key", api_key_id=record.id)
        raise errors.unauthorized("This API key has been revoked.")
    if not record.enabled:
        log.warning("auth_failed", reason="disabled_key", api_key_id=record.id)
        raise errors.unauthorized("This API key is disabled.")
    if record.expires_at is not None:
        expires = (
            record.expires_at
            if record.expires_at.tzinfo
            else record.expires_at.replace(tzinfo=timezone.utc)
        )
        if expires <= datetime.now(timezone.utc):
            log.warning("auth_failed", reason="expired_key", api_key_id=record.id)
            raise errors.unauthorized("This API key has expired.")

    return AuthenticatedKey(id=record.id, name=record.name)
