"""Admin authentication.

The admin surface is protected by a signed, HttpOnly session cookie issued
after a username/password login. Credentials come from ``ADMIN_USERNAME`` /
``ADMIN_PASSWORD``; the password is never stored or echoed anywhere.

An unauthenticated caller can never read, add or delete Qwen credentials.
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass

from fastapi import Depends, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings
from app.gateway import errors
from app.utils.logging import get_logger

log = get_logger(__name__)

SESSION_COOKIE = "qwg_admin_session"
_SALT = "qwen-gateway/admin-session/v1"


@dataclass(slots=True)
class AdminIdentity:
    username: str


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().resolved_secret_key(), salt=_SALT)


def admin_configured() -> bool:
    settings = get_settings()
    return bool(settings.admin_username and settings.admin_password)


def verify_admin_credentials(username: str, password: str) -> bool:
    settings = get_settings()
    if not admin_configured():
        return False
    user_ok = hmac.compare_digest(username or "", settings.admin_username)
    pass_ok = hmac.compare_digest(password or "", settings.admin_password)
    return user_ok and pass_ok


def issue_session(response: Response, username: str) -> str:
    settings = get_settings()
    token = _serializer().dumps({"u": username, "t": int(time.time())})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )
    return token


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _read_token(request: Request) -> str | None:
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie
    header = request.headers.get("x-admin-session")
    if header:
        return header.strip()
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "admin" and value.strip():
        return value.strip()
    return None


def current_admin_optional(request: Request) -> AdminIdentity | None:
    token = _read_token(request)
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=get_settings().session_ttl_seconds)
    except (SignatureExpired, BadSignature):
        return None
    username = payload.get("u") if isinstance(payload, dict) else None
    if not username:
        return None
    return AdminIdentity(username=str(username))


async def require_admin(request: Request) -> AdminIdentity:
    """FastAPI dependency protecting every admin endpoint."""
    if not admin_configured():
        raise errors.GatewayError(
            message=(
                "Admin access is not configured. Set ADMIN_USERNAME and "
                "ADMIN_PASSWORD, then restart the gateway."
            ),
            category=errors.ErrorCategory.PERMISSION,
            code="admin_not_configured",
            status_code=503,
        )
    identity = current_admin_optional(request)
    if identity is None:
        raise errors.unauthorized("Admin authentication required.")
    return identity


AdminDep = Depends(require_admin)
