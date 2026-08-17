"""Normalized gateway errors.

Every failure — inbound validation, auth, scheduling, or upstream — is turned
into a :class:`GatewayError`. That guarantees a stable public error contract
and prevents upstream/internal details (stack traces, credentials, raw HTML)
from ever reaching a client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.utils.redaction import redact_text


class ErrorCategory:
    """Stable machine-readable error categories used by logs, stats and tests."""

    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION = "authentication_error"
    PERMISSION = "permission_error"
    NOT_FOUND = "not_found"
    RATE_LIMIT = "rate_limit_error"
    UPSTREAM = "upstream_error"
    TIMEOUT = "timeout_error"
    NETWORK = "network_error"
    PARSE = "parse_error"
    NO_CREDENTIALS = "no_credentials"
    INTERNAL = "internal_error"


#: Categories for which trying another credential can plausibly help.
RETRYABLE_CATEGORIES = frozenset(
    {
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.UPSTREAM,
        ErrorCategory.TIMEOUT,
        ErrorCategory.NETWORK,
        ErrorCategory.AUTHENTICATION,
        ErrorCategory.PERMISSION,
    }
)

#: Categories that indicate the *credential itself* is bad, not the request.
CREDENTIAL_FAULT_CATEGORIES = frozenset(
    {
        ErrorCategory.AUTHENTICATION,
        ErrorCategory.PERMISSION,
        ErrorCategory.RATE_LIMIT,
    }
)


@dataclass(slots=True)
class GatewayError(Exception):
    """A normalized, client-safe error."""

    message: str
    category: str = ErrorCategory.INTERNAL
    code: str = "internal_error"
    status_code: int = 500
    retryable: bool = False
    #: Diagnostic detail kept internally (logs only) — never serialized to clients.
    internal_detail: str | None = field(default=None, repr=False)
    param: str | None = None
    retry_after: int | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def credential_fault(self) -> bool:
        return self.category in CREDENTIAL_FAULT_CATEGORIES

    def to_public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "message": redact_text(self.message),
            "type": self.category,
            "code": self.code,
        }
        if self.param:
            payload["param"] = self.param
        return {"error": payload}

    def log_fields(self) -> dict[str, Any]:
        return {
            "error_category": self.category,
            "error_code": self.code,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "detail": redact_text(self.internal_detail or ""),
        }


# --------------------------------------------------------------------------
# Constructors for common conditions
# --------------------------------------------------------------------------
def invalid_request(message: str, param: str | None = None) -> GatewayError:
    return GatewayError(
        message=message,
        category=ErrorCategory.INVALID_REQUEST,
        code="invalid_request_error",
        status_code=400,
        param=param,
    )


def unauthorized(message: str = "Invalid or missing API key.") -> GatewayError:
    return GatewayError(
        message=message,
        category=ErrorCategory.AUTHENTICATION,
        code="invalid_api_key",
        status_code=401,
    )


def forbidden(
    message: str = "This API key is not permitted to perform that action.",
) -> GatewayError:
    return GatewayError(
        message=message,
        category=ErrorCategory.PERMISSION,
        code="permission_denied",
        status_code=403,
    )


def not_found(message: str, code: str = "not_found") -> GatewayError:
    return GatewayError(
        message=message,
        category=ErrorCategory.NOT_FOUND,
        code=code,
        status_code=404,
    )


def no_credentials(detail: str | None = None) -> GatewayError:
    return GatewayError(
        message=(
            "No healthy Qwen credential is available. Add or re-enable a "
            "credential in the admin dashboard."
        ),
        category=ErrorCategory.NO_CREDENTIALS,
        code="no_available_credential",
        status_code=503,
        internal_detail=detail,
    )


def rate_limited(retry_after: int | None = None, detail: str | None = None) -> GatewayError:
    return GatewayError(
        message="Upstream rate limit reached. Please retry shortly.",
        category=ErrorCategory.RATE_LIMIT,
        code="rate_limit_exceeded",
        status_code=429,
        retryable=True,
        retry_after=retry_after,
        internal_detail=detail,
    )


def upstream_unavailable(detail: str | None = None, status_code: int = 502) -> GatewayError:
    return GatewayError(
        message="Qwen provider temporarily unavailable.",
        category=ErrorCategory.UPSTREAM,
        code="provider_unavailable",
        status_code=status_code,
        retryable=True,
        internal_detail=detail,
    )


def upstream_timeout(detail: str | None = None) -> GatewayError:
    return GatewayError(
        message="The upstream Qwen request timed out.",
        category=ErrorCategory.TIMEOUT,
        code="upstream_timeout",
        status_code=504,
        retryable=True,
        internal_detail=detail,
    )


def network_error(detail: str | None = None) -> GatewayError:
    return GatewayError(
        message="Could not reach the Qwen provider.",
        category=ErrorCategory.NETWORK,
        code="upstream_connection_error",
        status_code=502,
        retryable=True,
        internal_detail=detail,
    )


def invalid_credential(detail: str | None = None) -> GatewayError:
    return GatewayError(
        message="The stored Qwen credential was rejected by the provider.",
        category=ErrorCategory.AUTHENTICATION,
        code="upstream_unauthorized",
        status_code=502,
        retryable=True,
        internal_detail=detail,
    )


def parse_error(detail: str | None = None) -> GatewayError:
    return GatewayError(
        message="The Qwen provider returned a response the gateway could not parse.",
        category=ErrorCategory.PARSE,
        code="upstream_malformed_response",
        status_code=502,
        retryable=True,
        internal_detail=detail,
    )


def internal_error(detail: str | None = None) -> GatewayError:
    return GatewayError(
        message="Internal gateway error.",
        category=ErrorCategory.INTERNAL,
        code="internal_error",
        status_code=500,
        internal_detail=detail,
    )


def from_upstream_status(
    status_code: int,
    *,
    retry_after: int | None = None,
    detail: str | None = None,
) -> GatewayError:
    """Map an upstream HTTP status onto a normalized gateway error."""
    if status_code in (401, 498):
        return invalid_credential(detail)
    if status_code == 403:
        return GatewayError(
            message="The Qwen provider refused this credential (forbidden).",
            category=ErrorCategory.PERMISSION,
            code="upstream_forbidden",
            status_code=502,
            retryable=True,
            internal_detail=detail,
        )
    if status_code in (408, 504):
        return upstream_timeout(detail)
    if status_code == 429:
        return rate_limited(retry_after, detail)
    if status_code in (502, 503):
        return upstream_unavailable(detail, status_code=502)
    if status_code >= 500:
        return upstream_unavailable(detail, status_code=502)
    if status_code == 404:
        return GatewayError(
            message="The requested model or upstream endpoint was not found.",
            category=ErrorCategory.UPSTREAM,
            code="upstream_not_found",
            status_code=502,
            retryable=False,
            internal_detail=detail,
        )
    if status_code == 400:
        return GatewayError(
            message="The Qwen provider rejected the request as invalid.",
            category=ErrorCategory.UPSTREAM,
            code="upstream_invalid_request",
            status_code=502,
            retryable=False,
            internal_detail=detail,
        )
    return upstream_unavailable(detail)


def from_exception(exc: BaseException) -> GatewayError:
    """Map transport-level exceptions (httpx and friends) to gateway errors."""
    if isinstance(exc, GatewayError):
        return exc

    import httpx

    if isinstance(
        exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)
    ):
        return upstream_timeout(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, httpx.RemoteProtocolError):
        return parse_error(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, httpx.HTTPError):
        return network_error(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, (ConnectionResetError, ConnectionError, OSError)):
        return network_error(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, TimeoutError):
        return upstream_timeout(f"{type(exc).__name__}: {exc}")
    return internal_error(f"{type(exc).__name__}: {exc}")
