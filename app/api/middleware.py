"""HTTP middleware: correlation IDs, request size limits, admin rate limiting."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.config import get_settings
from app.gateway import errors
from app.utils.ids import gateway_request_id
from app.utils.logging import get_logger, request_id_ctx

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Gateway-Request-Id"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a gateway correlation ID and logs request start/finish."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = gateway_request_id()
        token = request_id_ctx.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()

        is_api = request.url.path.startswith(("/v1", "/api"))
        if is_api:
            log.info(
                "request_started",
                method=request.method,
                path=request.url.path,
                client=_client_ip(request),
            )
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)

        response.headers[REQUEST_ID_HEADER] = request_id
        if is_api:
            log.info(
                "request_finished",
                request_id=request_id,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects oversized bodies before they are parsed."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        limit = get_settings().max_request_bytes
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    error = errors.GatewayError(
                        message=f"Request body exceeds the {limit} byte limit.",
                        category=errors.ErrorCategory.INVALID_REQUEST,
                        code="request_too_large",
                        status_code=413,
                    )
                    return JSONResponse(status_code=413, content=error.to_public_dict())
            except ValueError:
                pass
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-process sliding-window limiter.

    Applies to admin endpoints by default (protecting login/token management
    from brute force); the public API limit is opt-in via
    ``PUBLIC_RATE_LIMIT_PER_MINUTE``.
    """

    def __init__(self, app) -> None:
        super().__init__(app)
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        path = request.url.path
        if path.startswith("/api/admin"):
            limit = settings.admin_rate_limit_per_minute
        elif path.startswith("/v1"):
            limit = settings.public_rate_limit_per_minute
        else:
            limit = 0

        if limit > 0:
            bucket = (
                f"{_client_ip(request)}:{'admin' if path.startswith('/api/admin') else 'public'}"
            )
            now = time.monotonic()
            window = self._hits[bucket]
            while window and now - window[0] > 60.0:
                window.popleft()
            if len(window) >= limit:
                log.warning("rate_limited", path=path, bucket_size=len(window))
                error = errors.GatewayError(
                    message="Too many requests. Please slow down.",
                    category=errors.ErrorCategory.RATE_LIMIT,
                    code="gateway_rate_limited",
                    status_code=429,
                )
                return JSONResponse(
                    status_code=429,
                    content=error.to_public_dict(),
                    headers={"Retry-After": "60"},
                )
            window.append(now)
            if len(self._hits) > 10_000:  # bound memory
                self._hits.clear()
        return await call_next(request)


def _client_ip(request: Request) -> str:
    settings = get_settings()
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
