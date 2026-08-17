"""Normalized events -> OpenAI-compatible payloads.

This is the second half of the normalization pipeline:

    Qwen raw event -> Parser -> NormalizedEvent -> [this module] -> OpenAI JSON

It is provider-agnostic on purpose: it only understands
:class:`~app.providers.events.NormalizedEvent`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.api.schemas import (
    ChatCompletionChunk,
    ChatCompletionResponse,
    Choice,
    ChoiceDelta,
    ChunkChoice,
    FunctionCall,
    ResponseMessage,
    ToolCall,
    ToolCallDelta,
    Usage,
)
from app.gateway.errors import GatewayError
from app.providers.events import EventType, NormalizedEvent, ToolCallPayload
from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class AggregatedResult:
    """Everything collected from one upstream turn."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCallPayload] = field(default_factory=list)
    metadata: list[dict[str, Any]] = field(default_factory=list)
    system_events: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    error: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def effective_finish_reason(self) -> str:
        if self.tool_calls:
            return "tool_calls"
        return self.finish_reason or "stop"


class ResponseAggregator:
    """Collects normalized events into a single completion result."""

    def __init__(self, *, expose_reasoning: bool = False) -> None:
        self.expose_reasoning = expose_reasoning
        self.result = AggregatedResult()

    def add(self, event: NormalizedEvent) -> None:
        result = self.result
        if event.type is EventType.ASSISTANT_TEXT:
            result.content += event.text
        elif event.type is EventType.REASONING:
            result.reasoning += event.text
        elif event.type is EventType.TOOL_CALL and event.tool_call is not None:
            _merge_tool_call(result.tool_calls, event.tool_call)
        elif event.type is EventType.TOOL_RESULT:
            result.metadata.append({"kind": "tool_result", **event.data})
        elif event.type is EventType.METADATA:
            result.metadata.append(event.data)
        elif event.type is EventType.SYSTEM_EVENT:
            result.system_events.append(event.data)
            if event.note:
                result.warnings.append(event.note)
        elif event.type is EventType.USAGE:
            result.usage = Usage(
                **{k: int(v) for k, v in event.data.items() if k in Usage.model_fields}
            )
        elif event.type is EventType.ERROR:
            result.error = event.data
        elif event.type is EventType.DONE:
            result.finish_reason = event.finish_reason
        elif event.type is EventType.UNKNOWN:
            result.warnings.append(event.note or "unknown_event")
            if event.text:
                result.system_events.append(
                    {"kind": "unclassified_text", "text": event.text, **event.data}
                )

    def add_all(self, events: list[NormalizedEvent]) -> None:
        for event in events:
            self.add(event)

    def to_response(self, model: str, completion_id: str) -> ChatCompletionResponse:
        result = self.result
        message = ResponseMessage(
            role="assistant",
            content=result.content if result.content else ("" if not result.tool_calls else None),
        )
        if result.tool_calls:
            message.tool_calls = [_to_public_tool_call(tc) for tc in result.tool_calls]
        if self.expose_reasoning and result.reasoning:
            message.reasoning_content = result.reasoning

        return ChatCompletionResponse(
            id=completion_id,
            created=int(time.time()),
            model=model,
            choices=[
                Choice(
                    index=0,
                    message=message,
                    finish_reason=result.effective_finish_reason(),  # type: ignore[arg-type]
                )
            ],
            usage=result.usage,
        )


def _merge_tool_call(calls: list[ToolCallPayload], incoming: ToolCallPayload) -> None:
    for existing in calls:
        if existing.id == incoming.id or (
            existing.index == incoming.index and existing.name == incoming.name
        ):
            if incoming.name:
                existing.name = incoming.name
            if incoming.arguments and incoming.arguments != existing.arguments:
                existing.arguments = incoming.arguments
            existing.complete = existing.complete or incoming.complete
            return
    calls.append(incoming)


def _to_public_tool_call(payload: ToolCallPayload) -> ToolCall:
    return ToolCall(
        id=payload.id,
        type="function",
        function=FunctionCall(name=payload.name, arguments=payload.arguments or "{}"),
    )


class StreamFormatter:
    """Turns normalized events into OpenAI-compatible SSE chunk objects.

    Emits the opening role chunk, incremental content / reasoning / tool-call
    deltas, the final chunk carrying ``finish_reason`` and, when requested, a
    usage-only chunk.
    """

    def __init__(
        self,
        *,
        completion_id: str,
        model: str,
        expose_reasoning: bool = False,
        include_usage: bool = False,
    ) -> None:
        self.completion_id = completion_id
        self.model = model
        self.expose_reasoning = expose_reasoning
        self.include_usage = include_usage
        self.created = int(time.time())
        self._role_sent = False
        self._finished = False
        self._tool_indexes: dict[str, int] = {}
        self._tool_started: set[str] = set()
        self.aggregator = ResponseAggregator(expose_reasoning=expose_reasoning)

    # -- helpers ----------------------------------------------------------
    def _chunk(self, delta: ChoiceDelta, finish_reason: str | None = None) -> ChatCompletionChunk:
        return ChatCompletionChunk(
            id=self.completion_id,
            created=self.created,
            model=self.model,
            choices=[ChunkChoice(index=0, delta=delta, finish_reason=finish_reason)],
        )

    def opening_chunk(self) -> ChatCompletionChunk:
        self._role_sent = True
        return self._chunk(ChoiceDelta(role="assistant", content=""))

    def handle(self, event: NormalizedEvent) -> list[ChatCompletionChunk]:
        """Translate one normalized event into zero or more public chunks."""
        self.aggregator.add(event)
        chunks: list[ChatCompletionChunk] = []

        if event.type is EventType.ASSISTANT_TEXT and event.text:
            chunks.append(self._chunk(ChoiceDelta(content=event.text)))

        elif event.type is EventType.REASONING and event.text and self.expose_reasoning:
            chunks.append(self._chunk(ChoiceDelta(reasoning_content=event.text)))

        elif event.type is EventType.TOOL_CALL and event.tool_call is not None:
            chunks.extend(self._tool_chunks(event.tool_call))

        elif event.type is EventType.DONE:
            chunks.append(self.final_chunk(event.finish_reason))

        return chunks

    def _tool_chunks(self, payload: ToolCallPayload) -> list[ChatCompletionChunk]:
        index = self._tool_indexes.setdefault(payload.id, len(self._tool_indexes))
        chunks: list[ChatCompletionChunk] = []
        if payload.id not in self._tool_started:
            self._tool_started.add(payload.id)
            chunks.append(
                self._chunk(
                    ChoiceDelta(
                        tool_calls=[
                            ToolCallDelta(
                                index=index,
                                id=payload.id,
                                type="function",
                                function={"name": payload.name, "arguments": ""},
                            )
                        ]
                    )
                )
            )
        if payload.arguments:
            chunks.append(
                self._chunk(
                    ChoiceDelta(
                        tool_calls=[
                            ToolCallDelta(index=index, function={"arguments": payload.arguments})
                        ]
                    )
                )
            )
        return chunks

    def final_chunk(self, finish_reason: str | None = None) -> ChatCompletionChunk:
        self._finished = True
        reason = finish_reason or self.aggregator.result.effective_finish_reason()
        if self.aggregator.result.tool_calls:
            reason = "tool_calls"
        return self._chunk(ChoiceDelta(), finish_reason=reason)

    def usage_chunk(self) -> ChatCompletionChunk | None:
        if not self.include_usage:
            return None
        return ChatCompletionChunk(
            id=self.completion_id,
            created=self.created,
            model=self.model,
            choices=[],
            usage=self.aggregator.result.usage,
        )

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def role_sent(self) -> bool:
        return self._role_sent


def error_chunk_payload(error: GatewayError) -> dict[str, Any]:
    """A terminal error frame for an already-started SSE stream."""
    return error.to_public_dict()
