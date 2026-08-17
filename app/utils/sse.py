"""Incremental Server-Sent Events decoding/encoding.

TCP chunk boundaries can fall anywhere — inside a UTF-8 character, a field
name, a JSON body or the blank line between frames — so decoding must be
incremental and stateful.
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class SSEFrame:
    event: str | None
    data: str
    id: str | None = None
    retry: int | None = None

    def json(self) -> Any | None:
        """Parse ``data`` as JSON, returning ``None`` when it is not JSON."""
        payload = self.data.strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None

    @property
    def is_done(self) -> bool:
        return self.data.strip() == "[DONE]"


class SSEDecoder:
    """Feed bytes in, get complete frames out."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""

    def push(self, chunk: bytes | str | None) -> list[SSEFrame]:
        if chunk:
            self._buffer += chunk if isinstance(chunk, str) else self._decoder.decode(chunk)
        return self._drain(flush=False)

    def close(self) -> list[SSEFrame]:
        self._buffer += self._decoder.decode(b"", True)
        return self._drain(flush=True)

    def _drain(self, flush: bool) -> list[SSEFrame]:
        frames: list[SSEFrame] = []
        while True:
            index, length = _find_boundary(self._buffer)
            if index < 0:
                break
            raw = self._buffer[:index]
            self._buffer = self._buffer[index + length :]
            frame = parse_frame(raw)
            if frame is not None:
                frames.append(frame)
        if flush and self._buffer.strip():
            frame = parse_frame(self._buffer)
            if frame is not None:
                frames.append(frame)
            self._buffer = ""
        return frames


def _find_boundary(buffer: str) -> tuple[int, int]:
    candidates = [
        (buffer.find("\r\n\r\n"), 4),
        (buffer.find("\n\n"), 2),
        (buffer.find("\r\r"), 2),
    ]
    best = (-1, 0)
    for index, length in candidates:
        if index >= 0 and (best[0] < 0 or index < best[0]):
            best = (index, length)
    return best


def parse_frame(raw: str) -> SSEFrame | None:
    """Parse one raw SSE frame; tolerates a bare-JSON body (seen on WAF errors)."""
    if not raw:
        return None

    event: str | None = None
    frame_id: str | None = None
    retry: int | None = None
    data_lines: list[str] = []
    saw_field = False

    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
            saw_field = True
        elif field == "event":
            event = value or None
            saw_field = True
        elif field == "id":
            frame_id = value
            saw_field = True
        elif field == "retry":
            try:
                retry = int(value)
            except ValueError:
                retry = None
            saw_field = True

    if not saw_field:
        # Some upstream failures are returned as HTTP 200 + bare JSON body
        # instead of SSE. Surface it so the caller can classify the error
        # rather than seeing "a stream with zero events".
        stripped = raw.strip()
        if stripped.startswith(("{", "[")):
            try:
                json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return None
            return SSEFrame(event=None, data=stripped)
        return None

    return SSEFrame(event=event, data="\n".join(data_lines), id=frame_id, retry=retry)


def format_sse(data: str | dict[str, Any], event: str | None = None) -> str:
    """Encode one outbound SSE frame."""
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lines = []
    if event:
        lines.append(f"event: {event}")
    for line in data.split("\n"):
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


DONE_FRAME = "data: [DONE]\n\n"
