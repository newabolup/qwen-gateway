"""Qwen credential handling.

Only credentials the operator already possesses and is authorized to use are
supported. The gateway never attempts to obtain credentials on a user's behalf:
there is no browser-cookie extraction, no scraping of a login page, no CAPTCHA
handling and no credential-sharing mechanism. The two supported inputs are:

``portal``
    An OAuth access token the user obtained themselves through Qwen's official
    device-code login (for example the token stored by ``qwen-code`` in
    ``~/.qwen/oauth_creds.json``). If the user also supplies the matching
    refresh token, the gateway can perform the standard RFC 6749 refresh grant.

``web``
    A ``chat.qwen.ai`` session token the user copied from their own logged-in
    session, used as the ``token=`` cookie exactly as the official web client
    does.

Both are user-provided secrets; the gateway just stores them encrypted and
presents them to the upstream.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings
from app.utils.logging import get_logger

log = get_logger(__name__)

OAUTH_TOKEN_PATH = "/api/v1/oauth2/token"  # noqa: S105 - URL path, not a secret
REFRESH_GRANT = "refresh_token"


@dataclass(slots=True)
class TokenInfo:
    """Non-sensitive facts derived from a credential."""

    expires_at: float | None = None
    subject: str | None = None
    issuer: str | None = None
    resource_url: str | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time()

    def expiring_within(self, seconds: float) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time() + seconds


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT payload. Never raises; signature not verified.

    Used only to read non-secret metadata (``exp``) so the gateway can refresh
    proactively and mark expired credentials.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded)
        return claims if isinstance(claims, dict) else {}
    except (ValueError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def inspect_token(token: str) -> TokenInfo:
    claims = decode_jwt_claims(token)
    expires_at = claims.get("exp")
    if isinstance(expires_at, (int, float)):
        expires = float(expires_at)
    else:
        expires = None
    return TokenInfo(
        expires_at=expires,
        subject=str(claims["sub"]) if claims.get("sub") else None,
        issuer=str(claims["iss"]) if claims.get("iss") else None,
        resource_url=str(claims["resource_url"]) if claims.get("resource_url") else None,
    )


def detect_auth_mode(secret: str, declared: str | None = None) -> str:
    """Decide whether a credential is a portal bearer token or a web cookie.

    The declared mode always wins; detection only fills in ``auto``.
    """
    if declared in {"portal", "web"}:
        return declared
    claims = decode_jwt_claims(secret)
    issuer = str(claims.get("iss") or "")
    if claims.get("resource_url") or "portal" in issuer:
        return "portal"
    if claims.get("scope") and "model.completion" in str(claims.get("scope")):
        return "portal"
    # chat.qwen.ai session tokens are JWTs carrying an "id"/"sub" user claim
    # without OAuth scope information.
    if claims:
        return "web"
    return "portal"


def normalize_base_url(raw: str | None, fallback: str) -> str:
    """Turn a bare ``resource_url`` such as ``portal.qwen.ai`` into a full URL."""
    if not raw:
        return fallback.rstrip("/")
    value = raw.strip().rstrip("/")
    if not value:
        return fallback.rstrip("/")
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    if not value.endswith("/v1") and "/v1" not in value:
        value = f"{value}/v1"
    return value


async def refresh_access_token(client: httpx.AsyncClient, refresh_token: str) -> dict[str, Any]:
    """Perform the standard OAuth refresh grant against Qwen's token endpoint.

    Raises :class:`httpx.HTTPError` on transport failures and returns the token
    document on success. The caller persists the rotated secrets.
    """
    settings = get_settings()
    url = f"{settings.qwen_oauth_base_url.rstrip('/')}{OAUTH_TOKEN_PATH}"
    data = {
        "grant_type": REFRESH_GRANT,
        "refresh_token": refresh_token,
    }
    if settings.qwen_oauth_client_id:
        data["client_id"] = settings.qwen_oauth_client_id

    response = await client.post(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    if response.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"token refresh failed with status {response.status_code}",
            request=response.request,
            response=response,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise httpx.HTTPError("token refresh returned a non-JSON body") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise httpx.HTTPError("token refresh response did not contain an access_token")
    return payload


def build_portal_headers(secret: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "User-Agent": "qwen-token-gateway/1.0",
    }


def build_web_headers(secret: str, *, chat_id: str | None, base_url: str) -> dict[str, str]:
    """Headers matching the official chat.qwen.ai web client.

    The web surface authenticates via the ``token`` cookie (the browser sends no
    Authorization header) and requires its client-identification headers.
    """
    settings = get_settings()
    origin = base_url.rstrip("/")
    referer = f"{origin}/c/{chat_id}" if chat_id else f"{origin}/"
    return {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "source": "web",
        "version": settings.qwen_web_client_version,
        "Origin": origin,
        "Referer": referer,
        "Cookie": f"token={secret}",
        "x-accel-buffering": "no",
    }
