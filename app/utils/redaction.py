"""Secret redaction helpers.

Everything that can end up in a log line, a stored request record or an API
response goes through here first. The rules are intentionally conservative:
it is better to over-redact than to leak a Qwen credential.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"

_SENSITIVE_KEYS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "secret",
    "secret_value",
    "credential",
    "credential_secret",
    "encrypted_secret",
    "password",
    "admin_password",
    "api_key",
    "apikey",
    "gateway_secret_key",
    "x-api-key",
    "ssxmod_itna",
    "ssxmod_itna2",
}

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bearer tokens / authorization headers
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/=]{8,}"),
    # Gateway API keys
    re.compile(r"\bqwg_[A-Za-z0-9_\-]{8,}"),
    # Cookie style token=...
    re.compile(r"(?i)\btoken=[A-Za-z0-9\-._~+/=]{8,}"),
    # JWTs (Qwen web session tokens and OAuth access tokens are JWTs)
    re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"),
)


def redact_text(value: str) -> str:
    """Replace any secret-looking substring in free text."""
    if not value:
        return value
    out = value
    for pattern in _PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)} {MASK}" if m.re.groups else MASK, out)
    return out


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive keys of a mapping (recursively)."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
            result[key] = MASK
        else:
            result[key] = redact_value(value)
    return result


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def mask_secret(secret: str, visible: int = 4) -> str:
    """Return a display-safe fingerprint such as ``••••••••••••abcd``."""
    if not secret:
        return ""
    tail = secret[-visible:] if len(secret) > visible else ""
    return "•" * 12 + tail
