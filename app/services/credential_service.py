"""Credential management (admin-side business logic)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway import errors
from app.models.db import QwenCredential, utcnow
from app.providers.base import ProviderCredential
from app.providers.qwen import auth as qwen_auth
from app.providers.registry import get_provider
from app.security.crypto import DecryptionError, decrypt_secret, encrypt_secret
from app.utils.logging import get_logger
from app.utils.redaction import mask_secret

log = get_logger(__name__)


async def list_credentials(session: AsyncSession) -> list[QwenCredential]:
    return list(
        (await session.execute(select(QwenCredential).order_by(QwenCredential.id.asc()))).scalars()
    )


async def create_credential(
    session: AsyncSession,
    *,
    name: str,
    secret: str,
    refresh_secret: str | None = None,
    auth_mode: str = "auto",
    base_url: str | None = None,
    provider: str = "qwen",
    enabled: bool = True,
) -> QwenCredential:
    """Store a user-provided credential, encrypted at rest."""
    secret = secret.strip()
    if not secret:
        raise errors.invalid_request("Credential secret must not be empty.", param="secret")

    resolved_mode = qwen_auth.detect_auth_mode(secret) if auth_mode == "auto" else auth_mode
    info = qwen_auth.inspect_token(secret)
    expires_at = (
        datetime.fromtimestamp(info.expires_at, tz=timezone.utc) if info.expires_at else None
    )
    resolved_base = base_url
    if resolved_base is None and resolved_mode == "portal" and info.resource_url:
        resolved_base = qwen_auth.normalize_base_url(info.resource_url, "")

    credential = QwenCredential(
        name=name.strip(),
        provider=provider,
        auth_mode=resolved_mode,
        encrypted_secret=encrypt_secret(secret),
        encrypted_refresh_secret=(
            encrypt_secret(refresh_secret.strip()) if refresh_secret else None
        ),
        base_url=resolved_base or None,
        secret_hint=mask_secret(secret),
        status="unknown",
        enabled=enabled,
        expires_at=expires_at,
    )
    session.add(credential)
    await session.commit()
    await session.refresh(credential)
    log.info(
        "credential_created",
        credential_id=credential.id,
        auth_mode=resolved_mode,
        has_refresh=bool(refresh_secret),
    )
    return credential


async def update_credential(
    session: AsyncSession,
    credential_id: int,
    *,
    name: str | None = None,
    enabled: bool | None = None,
    secret: str | None = None,
    refresh_secret: str | None = None,
    base_url: str | None = None,
    auth_mode: str | None = None,
    clear_cooldown: bool = False,
) -> QwenCredential:
    credential = await session.get(QwenCredential, credential_id)
    if credential is None:
        raise errors.not_found(f"Credential {credential_id} not found.")

    if name is not None:
        credential.name = name.strip()
    if enabled is not None:
        credential.enabled = enabled
    if base_url is not None:
        credential.base_url = base_url.strip() or None
    if auth_mode is not None:
        credential.auth_mode = (
            qwen_auth.detect_auth_mode(decrypt_secret(credential.encrypted_secret))
            if auth_mode == "auto"
            else auth_mode
        )
    if secret:
        cleaned = secret.strip()
        credential.encrypted_secret = encrypt_secret(cleaned)
        credential.secret_hint = mask_secret(cleaned)
        info = qwen_auth.inspect_token(cleaned)
        credential.expires_at = (
            datetime.fromtimestamp(info.expires_at, tz=timezone.utc) if info.expires_at else None
        )
        credential.status = "unknown"
        credential.status_reason = None
        credential.consecutive_failures = 0
        credential.cooldown_until = None
    if refresh_secret is not None:
        credential.encrypted_refresh_secret = (
            encrypt_secret(refresh_secret.strip()) if refresh_secret.strip() else None
        )
    if clear_cooldown:
        credential.cooldown_until = None
        credential.consecutive_failures = 0
        if credential.status in {"cooldown", "degraded", "invalid"}:
            credential.status = "unknown"
            credential.status_reason = None

    credential.updated_at = utcnow()
    await session.commit()
    await session.refresh(credential)
    log.info("credential_updated", credential_id=credential.id)
    return credential


async def delete_credential(session: AsyncSession, credential_id: int) -> None:
    credential = await session.get(QwenCredential, credential_id)
    if credential is None:
        raise errors.not_found(f"Credential {credential_id} not found.")
    await session.delete(credential)
    await session.commit()
    log.info("credential_deleted", credential_id=credential_id)


async def to_provider_credential(credential: QwenCredential) -> ProviderCredential:
    secret = decrypt_secret(credential.encrypted_secret)
    refresh = (
        decrypt_secret(credential.encrypted_refresh_secret)
        if credential.encrypted_refresh_secret
        else None
    )
    return ProviderCredential(
        id=credential.id,
        name=credential.name,
        secret=secret,
        auth_mode=credential.auth_mode,
        refresh_secret=refresh,
        base_url=credential.base_url,
        expires_at=credential.expires_at.timestamp() if credential.expires_at else None,
    )


async def test_credential(session: AsyncSession, credential_id: int) -> dict:
    """Probe a credential against its provider and persist the outcome."""
    credential = await session.get(QwenCredential, credential_id)
    if credential is None:
        raise errors.not_found(f"Credential {credential_id} not found.")

    try:
        provider_credential = await to_provider_credential(credential)
    except DecryptionError as exc:
        credential.status = "invalid"
        credential.status_reason = "decryption failed"
        await session.commit()
        return {"id": credential_id, "healthy": False, "detail": str(exc)}

    provider = get_provider(credential.provider)
    auth = await provider.authenticate(provider_credential)
    if not auth.ok:
        credential.status = "invalid"
        credential.status_reason = (auth.detail or "authentication failed")[:255]
        credential.last_error_at = utcnow()
        await session.commit()
        return {"id": credential_id, "healthy": False, "detail": auth.detail}

    if auth.rotated_secret:
        credential.encrypted_secret = encrypt_secret(auth.rotated_secret)
        credential.secret_hint = mask_secret(auth.rotated_secret)
        provider_credential.secret = auth.rotated_secret
    if auth.rotated_refresh_secret:
        credential.encrypted_refresh_secret = encrypt_secret(auth.rotated_refresh_secret)
    if auth.expires_at:
        credential.expires_at = datetime.fromtimestamp(auth.expires_at, tz=timezone.utc)
    if auth.base_url:
        credential.base_url = auth.base_url
        provider_credential.base_url = auth.base_url

    health = await provider.health_check(provider_credential)
    models = 0
    if health.healthy:
        try:
            discovered = await provider.list_models(provider_credential)
            models = len(discovered)
        except Exception as exc:
            log.warning("credential_test_model_discovery_failed", detail=str(exc))

    credential.status = "healthy" if health.healthy else "degraded"
    credential.status_reason = None if health.healthy else (health.detail or "")[:255]
    if health.healthy:
        credential.last_success_at = utcnow()
        credential.cooldown_until = None
        credential.consecutive_failures = 0
    else:
        credential.last_error_at = utcnow()
    await session.commit()

    log.info("credential_tested", credential_id=credential_id, healthy=health.healthy)
    return {
        "id": credential_id,
        "healthy": health.healthy,
        "detail": health.detail,
        "latency_ms": health.latency_ms,
        "models_discovered": models,
    }
