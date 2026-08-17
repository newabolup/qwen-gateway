"""Encryption at rest for provider credentials.

Credentials are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using a key
derived from ``GATEWAY_SECRET_KEY`` via PBKDF2-HMAC-SHA256. The derived key is
cached per secret so the KDF cost is paid once per process.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_KDF_ITERATIONS = 240_000
_KDF_SALT = b"qwen-gateway/credential-encryption/v1"


class DecryptionError(RuntimeError):
    """Raised when a stored credential cannot be decrypted."""


@lru_cache(maxsize=8)
def _fernet_for(secret_key: str) -> Fernet:
    derived = hashlib.pbkdf2_hmac(
        "sha256", secret_key.encode("utf-8"), _KDF_SALT, _KDF_ITERATIONS, dklen=32
    )
    return Fernet(base64.urlsafe_b64encode(derived))


def _fernet() -> Fernet:
    return _fernet_for(get_settings().resolved_secret_key())


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a provider secret for storage."""
    if not plaintext:
        raise ValueError("refusing to encrypt an empty secret")
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored provider secret."""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise DecryptionError(
            "Stored credential could not be decrypted. This usually means "
            "GATEWAY_SECRET_KEY changed since the credential was saved."
        ) from exc


def generate_secret_key() -> str:
    """Generate a fresh value suitable for GATEWAY_SECRET_KEY."""
    return base64.urlsafe_b64encode(hashlib.sha256(Fernet.generate_key()).digest()).decode("ascii")
