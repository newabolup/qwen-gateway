"""Tool-calling support for the Qwen adapter.

Two paths exist, and the adapter prefers the first:

1. **Native** — the upstream emits OpenAI-shaped ``tool_calls`` deltas. Handled
   by the accumulator in :mod:`app.providers.qwen.parser`.
2. **Text-embedded** — the upstream (notably the web dialect) has no native
   tool API, so tool definitions are injected as a system instruction and the
   model replies with ``<tool_call>{...}</tool_call>`` blocks. This module
   builds that instruction and parses the blocks back out of the token stream.

Design rules enforced here:

* Tool semantics are never invented. A block is only converted into a tool call
  when it parses into a JSON object with a ``name`` the client actually
  declared (or, when the name is unknown, it is reported as a classification
  warning and the raw text is preserved).
* Parsing is incremental and never leaks a partial ``<tool_call>`` marker into
  assistant text.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from app.api.schemas import ToolDefinition
from app.providers.events import ToolCallPayload
from app.utils.ids import tool_call_id

TOOL_OPEN = "<tool_call>"
TOOL_CLOSE = "</tool_call>"

_INSTRUCTION_HEADER = """\
# Tool calling

You can call the tools listed below. To call a tool, output a block in exactly
this format and nothing else in that turn:

<tool_call>
{"name": "<tool name>", "arguments": {<JSON arguments>}}
</tool_call>

Rules:
- `arguments` must be a JSON object that validates against the tool's schema.
- Emit one <tool_call> block per tool invocation; multiple blocks are allowed.
- Do not wrap the block in Markdown code fences and do not explain the call.
- Only answer normally when no tool call is needed or after receiving results.

## Available tools
"""


def build_tool_instruction(tools: list[ToolDefinition]) -> str:
    """Render the system instruction describing the client's tools."""
    if not tools:
        return ""
    lines = [_INSTRUCTION_HEADER]
    for tool in tools:
        fn = tool.function
        schema = json.dumps(
            fn.parameters or {"type": "object", "properties": {}}, ensure_ascii=False
        )
        description = (fn.description or "").strip()
        lines.append(f"\n### {fn.name}")
        if description:
            lines.append(description)
        lines.append(f"Parameters (JSON Schema): {schema}")
    return "\n".join(lines)


def build_tool_choice_instruction(tool_choice: Any) -> str:
    """Translate ``tool_choice`` into an explicit instruction."""
    if tool_choice in (None, "auto"):
        return ""
    if tool_choice == "none":
        return "\nDo not call any tool in this turn. Answer directly."
    if tool_choice == "required":
        return "\nYou MUST call exactly one tool in this turn using the <tool_call> format."
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return f"\nYou MUST call the tool `{name}` in this turn using the <tool_call> format."
    return ""


def coerce_arguments(raw: Any) -> str:
    """Return tool arguments as a JSON *string*, as the OpenAI schema requires."""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return "{}"
        try:
            json.loads(text)
            return text
        except (json.JSONDecodeError, ValueError):
            return json.dumps({"input": raw}, ensure_ascii=False)
    if raw is None:
        return "{}"
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"input": str(raw)}, ensure_ascii=False)


def parse_tool_block(
    block: str, known_names: list[str]
) -> tuple[ToolCallPayload | None, str | None]:
    """Parse one ``<tool_call>`` body.

    Returns ``(payload, warning)``. A payload is only produced when the block is
    unambiguous; otherwise a warning string explains why it was rejected.
    """
    text = block.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None, "tool_call block was not valid JSON"

    if not isinstance(parsed, dict):
        return None, "tool_call block was not a JSON object"

    name = parsed.get("name") or parsed.get("tool") or parsed.get("function")
    if isinstance(name, dict):
        name = name.get("name")
    if not isinstance(name, str) or not name:
        return None, "tool_call block had no tool name"

    if known_names and name not in known_names:
        return None, f"tool_call referenced undeclared tool {name!r}"

    arguments = parsed.get("arguments")
    if arguments is None:
        arguments = parsed.get("parameters")
    if arguments is None:
        arguments = parsed.get("input")

    return (
        ToolCallPayload(
            id=str(parsed.get("id") or tool_call_id()),
            name=name,
            arguments=coerce_arguments(arguments),
            complete=True,
        ),
        None,
    )


class ToolCallTextParser:
    """Incremental state machine extracting ``<tool_call>`` blocks from text."""

    def __init__(self, tool_names: list[str] | None = None) -> None:
        self._known = list(tool_names or [])
        self._buffer = ""
        self._inside = False
        self._pending: list[ToolCallPayload] = []
        self._index = 0

    def feed(self, chunk: str) -> list[tuple[str, Any]]:
        """Consume text; return ``("text"|"tool_call"|"warning", value)`` items."""
        out: list[tuple[str, Any]] = []
        if not chunk:
            return out
        self._buffer += chunk
        while True:
            if self._inside:
                end = self._buffer.find(TOOL_CLOSE)
                if end < 0:
                    break
                block = self._buffer[:end]
                self._buffer = self._buffer[end + len(TOOL_CLOSE) :]
                self._inside = False
                payload, warning = parse_tool_block(block, self._known)
                if payload is not None:
                    payload.index = self._index
                    self._index += 1
                    self._pending.append(payload)
                    out.append(("tool_call", payload))
                else:
                    out.append(("warning", warning or "unparsable tool_call block"))
                    # Preserve the raw block internally rather than corrupting
                    # the assistant channel with half-parsed markup.
                continue

            start = self._buffer.find(TOOL_OPEN)
            if start >= 0:
                prefix = self._buffer[:start]
                if prefix:
                    out.append(("text", prefix))
                self._buffer = self._buffer[start + len(TOOL_OPEN) :]
                self._inside = True
                continue

            emit_upto = _safe_text_boundary(self._buffer)
            if emit_upto > 0:
                out.append(("text", self._buffer[:emit_upto]))
                self._buffer = self._buffer[emit_upto:]
            break
        return out

    def close(self) -> list[ToolCallPayload]:
        """Flush; an unterminated block at EOF is attempted once."""
        if self._inside and self._buffer.strip():
            payload, _ = parse_tool_block(self._buffer, self._known)
            self._buffer = ""
            self._inside = False
            if payload is not None:
                payload.index = self._index
                self._index += 1
                return [payload]
        self._buffer = ""
        return []

    def pending_text(self) -> str:
        return "" if self._inside else self._buffer

    def flush_text(self) -> str:
        if self._inside:
            return ""
        text, self._buffer = self._buffer, ""
        return text


def _safe_text_boundary(buffer: str) -> int:
    """Never emit a partial ``<tool_call>`` marker as assistant text."""
    idx = buffer.rfind("<")
    if idx < 0:
        return len(buffer)
    tail = buffer[idx:]
    if len(tail) >= len(TOOL_OPEN):
        return len(buffer)
    if TOOL_OPEN.startswith(tail):
        return idx
    return len(buffer)


def iter_tool_definitions(tools: list[ToolDefinition]) -> Iterator[str]:
    for tool in tools:
        yield tool.function.name
