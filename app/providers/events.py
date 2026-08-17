"""Provider-agnostic normalized event model.

Every provider adapter converts its native protocol into this small, explicit
event vocabulary. The rest of the gateway (streaming layer, aggregator, public
formatter) only ever sees these events — that is what keeps Qwen protocol
changes contained inside ``app/providers/qwen``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """Explicit classification of an upstream event."""

    ASSISTANT_TEXT = "assistant_text"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    METADATA = "metadata"
    SYSTEM_EVENT = "system_event"
    USAGE = "usage"
    ERROR = "error"
    DONE = "done"
    #: Recognised as an event but not confidently classifiable. Never surfaced
    #: as assistant text; retained for diagnostics.
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ToolCallPayload:
    """A normalized tool invocation."""

    id: str
    name: str
    #: Arguments as a JSON string fragment (may be partial while streaming).
    arguments: str = ""
    index: int = 0
    complete: bool = True


@dataclass(slots=True)
class NormalizedEvent:
    """One classified event produced by a provider parser."""

    type: EventType
    #: Incremental text for ASSISTANT_TEXT / REASONING events.
    text: str = ""
    tool_call: ToolCallPayload | None = None
    #: Structured payload for METADATA / SYSTEM_EVENT / USAGE / ERROR events.
    data: dict[str, Any] = field(default_factory=dict)
    #: Set on the final event of a turn.
    finish_reason: str | None = None
    #: Kept for logs/diagnostics only; never serialized to clients.
    raw: Any | None = field(default=None, repr=False)
    #: Parser confidence note, e.g. "ambiguous_details_block".
    note: str | None = None

    # -- convenience constructors -----------------------------------------
    @classmethod
    def text_event(cls, text: str, **kw: Any) -> NormalizedEvent:
        return cls(type=EventType.ASSISTANT_TEXT, text=text, **kw)

    @classmethod
    def reasoning(cls, text: str, **kw: Any) -> NormalizedEvent:
        return cls(type=EventType.REASONING, text=text, **kw)

    @classmethod
    def metadata(cls, data: dict[str, Any], **kw: Any) -> NormalizedEvent:
        return cls(type=EventType.METADATA, data=data, **kw)

    @classmethod
    def system_event(cls, data: dict[str, Any], **kw: Any) -> NormalizedEvent:
        return cls(type=EventType.SYSTEM_EVENT, data=data, **kw)

    @classmethod
    def tool(cls, payload: ToolCallPayload, **kw: Any) -> NormalizedEvent:
        return cls(type=EventType.TOOL_CALL, tool_call=payload, **kw)

    @classmethod
    def usage(cls, data: dict[str, Any]) -> NormalizedEvent:
        return cls(type=EventType.USAGE, data=data)

    @classmethod
    def done(cls, finish_reason: str | None = "stop") -> NormalizedEvent:
        return cls(type=EventType.DONE, finish_reason=finish_reason)

    @classmethod
    def error(cls, data: dict[str, Any], **kw: Any) -> NormalizedEvent:
        return cls(type=EventType.ERROR, data=data, **kw)
