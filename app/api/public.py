"""Public OpenAI-compatible API."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelCard,
    ModelList,
)
from app.auth.api_key import AuthenticatedKey, authenticate_api_key
from app.config import get_settings
from app.database import get_session, session_scope
from app.gateway import errors
from app.gateway.errors import GatewayError
from app.gateway.router import get_router
from app.gateway.scheduler import get_scheduler
from app.gateway.streaming import SSE_HEADERS, sse_response_body
from app.providers.qwen.client import FALLBACK_MODELS
from app.services import metrics
from app.services.completion_service import CompletionService
from app.services.metrics import RequestRecorder
from app.utils.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["OpenAI-compatible API"])


@router.get(
    "/models",
    response_model=ModelList,
    summary="List available models",
    description="Returns the models this gateway exposes, in OpenAI's format.",
)
async def list_models(
    api_key: AuthenticatedKey = Depends(authenticate_api_key),
    session: AsyncSession = Depends(get_session),
) -> ModelList:
    settings = get_settings()
    catalogue = await get_router().catalogue(session)
    if not catalogue:
        catalogue = list(FALLBACK_MODELS)
        if settings.default_provider == "mock":
            from app.providers.mock.client import MOCK_MODELS

            catalogue = list(MOCK_MODELS)

    alias_map = settings.alias_map
    reverse_aliases: dict[str, list[str]] = {}
    for alias, target in alias_map.items():
        reverse_aliases.setdefault(target, []).append(alias)

    created = int(time.time())
    cards = [
        ModelCard(
            id=info.id,
            created=created,
            owned_by=settings.default_provider,
            aliases=sorted(set(info.aliases) | set(reverse_aliases.get(info.id, []))),
            supports_tools=info.supports_tools,
            supports_reasoning=info.supports_reasoning,
        )
        for info in catalogue
    ]
    # Aliases are also exposed as first-class model ids so clients that only
    # accept a listed id (e.g. "qwen") work without extra configuration.
    known = {card.id for card in cards}
    for alias, target in sorted(alias_map.items()):
        if alias not in known:
            cards.append(
                ModelCard(
                    id=alias,
                    created=created,
                    owned_by=settings.default_provider,
                    aliases=[target],
                )
            )
    return ModelList(data=cards)


@router.post(
    "/chat/completions",
    summary="Create a chat completion",
    description=(
        "OpenAI-compatible chat completion. Set `stream: true` for Server-Sent "
        "Events. Tool calling and (when enabled) reasoning are normalized from "
        "the upstream provider."
    ),
    response_model=None,
    responses={
        200: {"description": "Completion, or an SSE stream when `stream` is true."},
        401: {"description": "Missing/invalid gateway API key."},
        429: {"description": "Rate limited."},
        502: {"description": "Upstream provider error."},
        503: {"description": "No healthy upstream credential available."},
    },
)
async def create_chat_completion(
    request: Request,
    api_key: AuthenticatedKey = Depends(authenticate_api_key),
    session: AsyncSession = Depends(get_session),
):
    payload = await _read_json_body(request)
    try:
        body = ChatCompletionRequest.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        raise errors.invalid_request(
            f"Invalid request: {first.get('msg', 'validation failed')}",
            param=location or None,
        ) from exc

    recorder = RequestRecorder(
        request_id=getattr(request.state, "request_id", "-"),
        endpoint="/v1/chat/completions",
        api_key_id=api_key.id,
        api_key_name=api_key.name,
    )
    recorder.capture_request_body(payload)
    service = CompletionService(session, recorder)

    if body.stream:
        return StreamingResponse(
            _stream_with_recording(service, body, recorder),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    try:
        response: ChatCompletionResponse = await service.complete(body)
    except GatewayError as exc:
        recorder.set_error(exc)
        await _persist(recorder)
        raise
    except Exception as exc:
        normalized = errors.from_exception(exc)
        log.exception("request_failed", **normalized.log_fields())
        recorder.set_error(normalized)
        await _persist(recorder)
        raise normalized from exc

    await _persist(recorder)
    return JSONResponse(content=response.model_dump(exclude_none=True))


async def _stream_with_recording(
    service: CompletionService,
    body: ChatCompletionRequest,
    recorder: RequestRecorder,
) -> AsyncIterator[bytes]:
    metrics.stream_started()

    async def chunk_source() -> AsyncIterator[ChatCompletionChunk]:
        try:
            async for chunk in service.stream(body):
                yield chunk
        except GatewayError:
            raise
        except Exception as exc:
            normalized = errors.from_exception(exc)
            log.exception("stream_failed", **normalized.log_fields())
            recorder.set_error(normalized)
            raise normalized from exc

    try:
        async for piece in sse_response_body(chunk_source()):
            yield piece
    finally:
        metrics.stream_finished()
        await _persist(recorder)


async def _persist(recorder: RequestRecorder) -> None:
    """Write the request record on its own session/transaction."""
    try:
        async with session_scope() as session:
            await recorder.persist(session)
    except Exception as exc:
        log.error("request_log_persist_failed", detail=str(exc))


async def _read_json_body(request: Request) -> dict:
    raw = await request.body()
    limit = get_settings().max_request_bytes
    if len(raw) > limit:
        raise errors.GatewayError(
            message=f"Request body exceeds the {limit} byte limit.",
            category=errors.ErrorCategory.INVALID_REQUEST,
            code="request_too_large",
            status_code=413,
        )
    if not raw:
        raise errors.invalid_request("Request body is empty.")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise errors.invalid_request("Request body is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise errors.invalid_request("Request body must be a JSON object.")
    return payload


# --------------------------------------------------------------------------
# Health / stats (unauthenticated, no secrets)
# --------------------------------------------------------------------------
health_router = APIRouter(tags=["Health"])


@health_router.get("/health", summary="Liveness probe")
async def health() -> dict:
    return {"status": "ok", "service": get_settings().app_name}


@health_router.get("/api/health", summary="Readiness probe with provider status")
async def api_health(session: AsyncSession = Depends(get_session)) -> dict:
    settings = get_settings()
    scheduler = get_scheduler()
    try:
        stats = await scheduler.stats(session, provider=settings.default_provider)
        database_ok = True
    except Exception as exc:
        log.error("health_db_error", detail=str(exc))
        database_ok = False
        stats = None

    healthy_tokens = stats.healthy if stats else 0
    status = "ok" if database_ok and healthy_tokens > 0 else "degraded"
    return {
        "status": status,
        "database": "ok" if database_ok else "error",
        "provider": settings.default_provider,
        "tokens": {
            "total": stats.total if stats else 0,
            "healthy": healthy_tokens,
            "cooldown": stats.cooling_down if stats else 0,
            "disabled": stats.disabled if stats else 0,
            "expired": stats.expired if stats else 0,
        },
        "active_streams": metrics.active_streams(),
        "version": "1.0.0",
    }


@health_router.get("/api/stats", summary="Aggregate gateway statistics")
async def api_stats(session: AsyncSession = Depends(get_session)) -> dict:
    return await metrics.gather_stats(session)
