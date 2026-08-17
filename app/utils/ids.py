"""Identifier helpers.

Gateway-internal correlation IDs are always prefixed so they can never be
confused with an upstream Qwen request/response ID.
"""

from __future__ import annotations

import secrets
import time
import uuid

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _short(n: int = 16) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def gateway_request_id() -> str:
    """Correlation ID for one inbound gateway request (``gwreq_...``)."""
    return f"gwreq_{int(time.time() * 1000):x}{_short(10)}"


def completion_id() -> str:
    return f"chatcmpl_{_short(24)}"


def tool_call_id() -> str:
    return f"call_{_short(20)}"


def new_uuid() -> str:
    return str(uuid.uuid4())
