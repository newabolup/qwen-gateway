"""Password and API-key hashing.

* Admin passwords use PBKDF2-HMAC-SHA256 with a per-password random salt.
* Client API keys use a keyed SHA-256 (HMAC) digest so they remain *lookupable*
  (constant-time DB index lookup) while never being stored in plaintext.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from app.config import get_settings

_PBKDF2_ITERATIONS = 200_000
_API_KEY_PREFIX = "qwg_"


# --------------------------------------------------------------------------
# Admin passwords
# --------------------------------------------------------------------------
def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except ValueError:
        return False
    return hmac.compare_digest(computed.hex(), digest_hex)


# --------------------------------------------------------------------------
# Client API keys
# --------------------------------------------------------------------------
def generate_api_key() -> str:
    """Create a new client API key (``qwg_...``). Shown to the user only once."""
    return f"{_API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    """Keyed digest of an API key; safe to store and to index."""
    key = get_settings().resolved_secret_key().encode("utf-8")
    return hmac.new(key, api_key.encode("utf-8"), hashlib.sha256).hexdigest()


def api_key_preview(api_key: str) -> str:
    """Non-reversible display hint, e.g. ``qwg_...a1b2``."""
    if len(api_key) <= 8:
        return f"{_API_KEY_PREFIX}..."
    return f"{api_key[:8]}...{api_key[-4:]}"


def looks_like_api_key(value: str) -> bool:
    return value.startswith(_API_KEY_PREFIX) and len(value) > 12
