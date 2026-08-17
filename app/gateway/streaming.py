"""Streaming helpers.

Wraps an async iterator of OpenAI chunks into a correctly framed SSE body:
role chunk -> deltas -> final chunk -> optional usage chunk -> ``[DONE]``.

Errors that occur *after* the stream has started cannot change the HTTP status,
so they are emitted as a terminal error frame followed by ``[DONE]`` — clients
see a clean end of stream instead of a truncated connection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.api.schemas import ChatCompletionChunk
from app.gateway.errors import GatewayError
from app.utils.sse import DONE_FRAME, format_sse

SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def encode_chunk(chunk: ChatCompletionChunk) -> str:
    return format_sse(chunk.model_dump(exclude_none=True))


def encode_error(error: GatewayError) -> str:
    return format_sse(error.to_public_dict())


async def sse_response_body(
    chunks: AsyncIterator[ChatCompletionChunk],
) -> AsyncIterator[bytes]:
    """Encode chunks as SSE bytes, always terminating with ``[DONE]``."""
    try:
        async for chunk in chunks:
            yield encode_chunk(chunk).encode("utf-8")
    except GatewayError as exc:
        yield encode_error(exc).encode("utf-8")
    finally:
        yield DONE_FRAME.encode("utf-8")
