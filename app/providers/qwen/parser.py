"""Qwen protocol parser.

Two upstream dialects are recognised and normalized here:

``portal``
    ``https://portal.qwen.ai/v1/chat/completions`` — an OpenAI-shaped protocol
    (``choices[].delta.content`` / ``reasoning_content`` / ``tool_calls``).

``web``
    ``https://chat.qwen.ai/api/v2/chat/completions`` — the web client protocol.
    Deltas carry a ``phase`` field (``think`` / ``thinking_summary`` /
    ``answer``) plus ``extra`` payloads (``summary_thought``,
    ``web_search_info``, ``image_list``).

Detection is per-event and structural, so a mixed or upgraded upstream still
parses. Anything unrecognised becomes an ``UNKNOWN`` event with a note; it is
never promoted to assistant text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.providers.events import EventType, NormalizedEvent, ToolCallPayload
from app.providers.qwen.markup import Segment, SegmentKind, StreamingMarkupSplitter
from app.providers.qwen.tools import ToolCallTextParser
from app.utils.ids import tool_call_id

#: ``phase`` values that carry private reasoning rather than the final answer.
THINK_PHASES = frozenset({"think", "thinking", "thinking_summary"})
#: ``phase`` values that carry user-visible answer text.
ANSWER_PHASES = frozenset({"answer", "final", "final_answer", "response"})

_FINISH_ALIASES = {
    "end_turn": "stop",
    "stop": "stop",
    "max_tokens": "length",
    "length": "length",
    "tool_use": "tool_calls",
    "tool_calls": "tool_calls",
    "content_filter": "content_filter",
    "function_call": "tool_calls",
}


def normalize_finish_reason(value: Any, *, has_tool_calls: bool = False) -> str | None:
    if has_tool_calls:
        return "tool_calls"
    if isinstance(value, str) and value:
        return _FINISH_ALIASES.get(value.lower())
    return None


@dataclass(slots=True)
class _ToolAccumulator:
    """Reassembles OpenAI-style streamed ``tool_calls`` deltas."""

    calls: dict[int, ToolCallPayload] = field(default_factory=dict)

    def push(self, deltas: list[dict[str, Any]]) -> list[ToolCallPayload]:
        touched: list[ToolCallPayload] = []
        for item in deltas:
            if not isinstance(item, dict):
                continue
            index = int(item.get("index") or 0)
            current = self.calls.get(index)
            if current is None:
                current = ToolCallPayload(
                    id=str(item.get("id") or tool_call_id()),
                    name="",
                    arguments="",
                    index=index,
                    complete=False,
                )
                self.calls[index] = current
            if item.get("id"):
                current.id = str(item["id"])
            function = item.get("function") or {}
            if isinstance(function, dict):
                if function.get("name"):
                    current.name = str(function["name"])
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    current.arguments += arguments
            touched.append(current)
        return touched

    def finalize(self) -> list[ToolCallPayload]:
        out: list[ToolCallPayload] = []
        for index in sorted(self.calls):
            call = self.calls[index]
            if not call.name:
                continue
            call.complete = True
            if not call.arguments.strip():
                call.arguments = "{}"
            out.append(call)
        return out


class QwenEventParser:
    """Stateful parser turning raw Qwen events into normalized events.

    One instance handles exactly one upstream response (streaming or not).
    """

    def __init__(
        self,
        *,
        parse_text_tool_calls: bool = False,
        tool_names: list[str] | None = None,
    ) -> None:
        self._answer_splitter = StreamingMarkupSplitter()
        self._reasoning_splitter = StreamingMarkupSplitter()
        self._tool_accumulator = _ToolAccumulator()
        self._text_tool_parser = (
            ToolCallTextParser(tool_names or []) if parse_text_tool_calls else None
        )
        self._summary_thought_count = 0
        self._emitted_answer_text = False
        self._emitted_reasoning_text = False
        self._finish_reason: str | None = None
        self._saw_tool_call = False
        self._closed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def feed_json(self, payload: Any) -> list[NormalizedEvent]:
        """Parse one decoded upstream event object."""
        if payload is None:
            return []
        if isinstance(payload, list):
            events: list[NormalizedEvent] = []
            for item in payload:
                events.extend(self.feed_json(item))
            return events
        if not isinstance(payload, dict):
            return [
                NormalizedEvent(
                    type=EventType.UNKNOWN,
                    raw=payload,
                    note="non_object_event",
                )
            ]

        error_event = self._detect_error(payload)
        if error_event is not None:
            return [error_event]

        events = []
        usage = payload.get("usage")
        if isinstance(usage, dict):
            events.append(NormalizedEvent.usage(_clean_usage(usage)))

        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            for choice in choices:
                if isinstance(choice, dict):
                    events.extend(self._parse_choice(choice))
        elif any(k in payload for k in ("content", "reasoning_content", "phase", "delta")):
            # Some web-protocol frames put the delta at the top level.
            delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else payload
            events.extend(self._parse_delta(delta, payload))
        elif not events:
            events.append(
                NormalizedEvent(
                    type=EventType.UNKNOWN, raw=payload, note="unrecognised_event_shape"
                )
            )

        return events

    def feed_raw_text(self, text: str) -> list[NormalizedEvent]:
        """Parse a non-SSE plain-text body (defensive fallback)."""
        return self._emit_answer_text(text)

    def close(self) -> list[NormalizedEvent]:
        """Flush buffered state and emit the terminal event."""
        if self._closed:
            return []
        self._closed = True

        events: list[NormalizedEvent] = []
        events.extend(self._segments_to_events(self._reasoning_splitter.close(), reasoning=True))
        events.extend(self._segments_to_events(self._answer_splitter.close(), reasoning=False))

        if self._text_tool_parser is not None:
            for payload in self._text_tool_parser.close():
                self._saw_tool_call = True
                events.append(NormalizedEvent.tool(payload))

        for call in self._tool_accumulator.finalize():
            self._saw_tool_call = True
            events.append(NormalizedEvent.tool(call))

        finish = normalize_finish_reason(
            self._finish_reason, has_tool_calls=self._saw_tool_call
        ) or ("tool_calls" if self._saw_tool_call else "stop")
        events.append(NormalizedEvent.done(finish))
        return events

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _detect_error(self, payload: dict[str, Any]) -> NormalizedEvent | None:
        error = payload.get("error")
        if isinstance(error, dict):
            return NormalizedEvent.error(
                {
                    "message": str(error.get("message") or "Upstream error"),
                    "code": str(error.get("code") or error.get("type") or "upstream_error"),
                    "status": error.get("status") or payload.get("status"),
                },
                raw=payload,
            )
        if isinstance(error, str) and error:
            return NormalizedEvent.error({"message": error, "code": "upstream_error"}, raw=payload)

        # chat.qwen.ai style: {"success": false, "data": {...}, "code": "RateLimited"}
        if payload.get("success") is False:
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            message = (
                payload.get("msg")
                or payload.get("message")
                or data.get("details")
                or data.get("message")
                or "Upstream request failed"
            )
            return NormalizedEvent.error(
                {
                    "message": str(message),
                    "code": str(payload.get("code") or data.get("code") or "upstream_error"),
                    "status": payload.get("status"),
                },
                raw=payload,
            )
        return None

    def _parse_choice(self, choice: dict[str, Any]) -> list[NormalizedEvent]:
        finish = choice.get("finish_reason") or choice.get("stop_reason")
        if isinstance(finish, str) and finish:
            self._finish_reason = finish

        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        return self._parse_delta(delta, choice)

    def _parse_delta(
        self, delta: dict[str, Any], container: dict[str, Any]
    ) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        if not isinstance(delta, dict):
            return events

        status = delta.get("status") or container.get("status")
        if isinstance(status, str) and status in {"typing", "finished", "created"}:
            events.append(NormalizedEvent.system_event({"upstream_status": status}, raw=None))

        # -- native tool calls (portal dialect) ---------------------------
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            self._tool_accumulator.push(tool_calls)
            self._saw_tool_call = True
        function_call = delta.get("function_call")
        if isinstance(function_call, dict) and function_call:
            self._tool_accumulator.push(
                [{"index": 0, "id": function_call.get("id"), "function": function_call}]
            )
            self._saw_tool_call = True

        # -- upstream extras become metadata, never assistant text --------
        extra = delta.get("extra")
        if isinstance(extra, dict) and extra:
            events.extend(self._parse_extra(extra, delta))

        if delta.get("name") == "web_search":
            events.append(
                NormalizedEvent.metadata({"kind": "web_search", "info": _jsonable(extra or {})})
            )

        # -- text channels -------------------------------------------------
        phase = delta.get("phase")
        reasoning_text = delta.get("reasoning_content")
        content = delta.get("content")

        if isinstance(reasoning_text, str) and reasoning_text and phase not in ANSWER_PHASES:
            events.extend(self._emit_reasoning_text(reasoning_text))

        if phase == "thinking_summary":
            summary_text = self._consume_summary_thought(extra)
            if summary_text:
                events.extend(self._emit_reasoning_text(summary_text))
            content = None if not isinstance(content, str) else content
            if isinstance(content, str) and content:
                events.extend(self._emit_reasoning_text(content))
            content = None

        if isinstance(content, list):
            content = _flatten_content_parts(content)

        if isinstance(content, str) and content:
            if isinstance(phase, str) and phase in THINK_PHASES:
                events.extend(self._emit_reasoning_text(content))
            elif isinstance(phase, str) and phase and phase not in ANSWER_PHASES:
                # A phase we do not know about: classify explicitly instead of
                # guessing it is assistant prose.
                events.append(
                    NormalizedEvent(
                        type=EventType.UNKNOWN,
                        text=content,
                        data={"phase": phase},
                        note=f"unknown_phase:{phase}",
                        raw=None,
                    )
                )
            else:
                events.extend(self._emit_answer_text(content))

        finish = normalize_finish_reason(self._finish_reason, has_tool_calls=self._saw_tool_call)
        if finish:
            self._finish_reason = finish
        return events

    def _parse_extra(self, extra: dict[str, Any], delta: dict[str, Any]) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        images = extra.get("image_list")
        if isinstance(images, list) and images:
            urls = [str(i.get("image")) for i in images if isinstance(i, dict) and i.get("image")]
            if urls:
                events.append(NormalizedEvent.metadata({"kind": "images", "urls": urls}))
        search = extra.get("web_search_info")
        if search:
            events.append(
                NormalizedEvent.metadata({"kind": "web_search", "info": _jsonable(search)})
            )
        for key in ("response_id", "request_id", "chat_id", "parent_id", "message_id"):
            if extra.get(key):
                events.append(
                    NormalizedEvent.metadata({"kind": "upstream_ids", key: str(extra[key])})
                )
        return events

    def _consume_summary_thought(self, extra: Any) -> str:
        if not isinstance(extra, dict):
            return ""
        summary = extra.get("summary_thought")
        if not isinstance(summary, dict):
            return ""
        thoughts = summary.get("content")
        if not isinstance(thoughts, list):
            return ""
        if len(thoughts) <= self._summary_thought_count:
            return ""
        fresh = thoughts[self._summary_thought_count :]
        self._summary_thought_count = len(thoughts)
        return "\n".join(str(t) for t in fresh if t)

    def _emit_answer_text(self, text: str) -> list[NormalizedEvent]:
        segments = self._answer_splitter.feed(text)
        return self._segments_to_events(segments, reasoning=False)

    def _emit_reasoning_text(self, text: str) -> list[NormalizedEvent]:
        segments = self._reasoning_splitter.feed(text)
        return self._segments_to_events(segments, reasoning=True)

    def _segments_to_events(
        self, segments: list[Segment], *, reasoning: bool
    ) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        for segment in segments:
            if segment.kind is SegmentKind.METADATA:
                events.append(
                    NormalizedEvent.metadata(
                        {"kind": "ui_metadata", **segment.data},
                        note=segment.note,
                    )
                )
            elif segment.kind is SegmentKind.AMBIGUOUS:
                # Preserve the payload for diagnostics; do NOT corrupt output.
                events.append(
                    NormalizedEvent.system_event(
                        {
                            "kind": "unclassified_ui_wrapper",
                            "text": segment.text,
                            **segment.data,
                        },
                        note=segment.note or "ambiguous_segment",
                    )
                )
            elif segment.kind is SegmentKind.UI:
                events.append(
                    NormalizedEvent.system_event({"kind": "ui_affordance"}, note=segment.note)
                )
            elif segment.text:
                text = segment.text
                # Strip whitespace left behind by a removed UI wrapper so the
                # answer does not start with stray blank lines.
                if (reasoning and not self._emitted_reasoning_text) or (
                    not reasoning and not self._emitted_answer_text
                ):
                    text = text.lstrip()
                if not text:
                    continue
                if reasoning:
                    self._emitted_reasoning_text = True
                    events.append(NormalizedEvent.reasoning(text))
                elif self._text_tool_parser is not None:
                    produced = self._parse_text_for_tools(text)
                    if any(e.type is EventType.ASSISTANT_TEXT for e in produced):
                        self._emitted_answer_text = True
                    events.extend(produced)
                else:
                    self._emitted_answer_text = True
                    events.append(NormalizedEvent.text_event(text))
        return events

    def _parse_text_for_tools(self, text: str) -> list[NormalizedEvent]:
        assert self._text_tool_parser is not None
        events: list[NormalizedEvent] = []
        for kind, value in self._text_tool_parser.feed(text):
            if kind == "text" and value:
                events.append(NormalizedEvent.text_event(str(value)))
            elif kind == "tool_call":
                self._saw_tool_call = True
                events.append(NormalizedEvent.tool(value))  # type: ignore[arg-type]
            elif kind == "warning":
                events.append(
                    NormalizedEvent.system_event(
                        {"kind": "tool_parse_warning", "detail": str(value)},
                        note="tool_classification_warning",
                    )
                )
        return events


def _flatten_content_parts(parts: list[Any]) -> str:
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
    return "".join(out)


def _clean_usage(usage: dict[str, Any]) -> dict[str, int]:
    def _as_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    prompt = _as_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion = _as_int(usage.get("completion_tokens") or usage.get("output_tokens"))
    total = _as_int(usage.get("total_tokens")) or (prompt + completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)
