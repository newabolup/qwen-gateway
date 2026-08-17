"""UI-markup / metadata layer separation for Qwen text streams.

Qwen's web surface mixes three different things into what looks like one text
channel:

1. real assistant prose,
2. HTML/Markdown *UI* wrappers (``<details>``/``<summary>`` blocks, copy
   affordances) that the web front-end renders as chrome, and
3. diagnostic metadata (``Response ID: ...``, ``Request ID: ...``).

Forwarding (2) and (3) as ``message.content`` is what makes naive proxies emit
garbage like::

    <details>
    <summary></summary>
    Response ID: ...
    Request ID: ...
    </details>

This module segments a text stream into explicitly classified regions. It is a
*structural* parser: it recognises the wrapper elements and then classifies the
wrapper's contents. It never deletes arbitrary prose by string matching, and
anything it cannot confidently classify is preserved (as a system event with a
diagnostic note) rather than silently dropped or leaked into assistant text.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum

#: Structural UI wrappers Qwen is known to emit around non-prose payloads.
_OPEN_DETAILS = re.compile(r"<details\b[^>]*>", re.IGNORECASE)
_CLOSE_DETAILS = re.compile(r"</details\s*>", re.IGNORECASE)
_SUMMARY = re.compile(r"<summary\b[^>]*>(.*?)</summary\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")

#: Whole-line ``Key: value`` diagnostics. Only ever applied *inside* a detected
#: UI wrapper, never to free prose.
_METADATA_LINE = re.compile(
    r"^\s*(?P<key>response[ _-]?id|request[ _-]?id|trace[ _-]?id|session[ _-]?id|"
    r"chat[ _-]?id|message[ _-]?id|parent[ _-]?id|model|created|latency|tokens?)"
    r"\s*[:：]\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)

#: UI affordance labels rendered as buttons by the web client.
_UI_AFFORDANCE = re.compile(
    r"^\s*(copy|copied|copy code|share|regenerate|retry|thumbs? up|thumbs? down|"
    r"like|dislike|edit|expand|collapse|show more|show less)\s*$",
    re.IGNORECASE,
)

_METADATA_KEY_NORMALISER = re.compile(r"[ _-]+")


class SegmentKind(str, Enum):
    TEXT = "text"
    METADATA = "metadata"
    UI = "ui"
    AMBIGUOUS = "ambiguous"


@dataclass(slots=True)
class Segment:
    """One classified region of the upstream text channel."""

    kind: SegmentKind
    text: str = ""
    data: dict[str, str] = field(default_factory=dict)
    note: str | None = None


def _normalise_key(key: str) -> str:
    return _METADATA_KEY_NORMALISER.sub("_", key.strip().lower())


def _strip_tags(value: str) -> str:
    return _TAG.sub("", value).strip()


def classify_wrapper_body(body: str, summary: str = "") -> Segment:
    """Classify the inner content of a UI wrapper such as ``<details>``.

    Returns a METADATA segment when every meaningful line is a ``Key: value``
    diagnostic or a UI affordance label, otherwise an AMBIGUOUS segment that
    preserves the payload for diagnostics without promoting it to assistant
    text.
    """
    metadata: dict[str, str] = {}
    leftovers: list[str] = []

    if summary.strip():
        metadata["summary"] = _strip_tags(summary)

    for raw_line in body.splitlines():
        line = _strip_tags(raw_line)
        if not line:
            continue
        match = _METADATA_LINE.match(line)
        if match:
            metadata[_normalise_key(match.group("key"))] = match.group("value").strip()
            continue
        if _UI_AFFORDANCE.match(line):
            continue
        leftovers.append(line)

    if not leftovers:
        return Segment(kind=SegmentKind.METADATA, data=metadata)

    return Segment(
        kind=SegmentKind.AMBIGUOUS,
        text="\n".join(leftovers),
        data=metadata,
        note="ambiguous_ui_wrapper_content",
    )


def segment_text(text: str) -> list[Segment]:
    """Split a *complete* text payload into classified segments."""
    return list(_iter_segments(text))


def _iter_segments(text: str) -> Iterator[Segment]:
    cursor = 0
    while cursor < len(text):
        opening = _OPEN_DETAILS.search(text, cursor)
        if opening is None:
            remainder = text[cursor:]
            if remainder:
                yield Segment(kind=SegmentKind.TEXT, text=remainder)
            return

        prefix = text[cursor : opening.start()]
        if prefix:
            yield Segment(kind=SegmentKind.TEXT, text=prefix)

        closing = _CLOSE_DETAILS.search(text, opening.end())
        if closing is None:
            # Unterminated wrapper: keep it out of assistant text but preserve it.
            yield Segment(
                kind=SegmentKind.AMBIGUOUS,
                text=text[opening.end() :],
                note="unterminated_ui_wrapper",
            )
            return

        inner = text[opening.end() : closing.start()]
        summary_match = _SUMMARY.search(inner)
        summary = summary_match.group(1) if summary_match else ""
        body = _SUMMARY.sub("", inner) if summary_match else inner
        yield classify_wrapper_body(body, summary)
        cursor = closing.end()


class StreamingMarkupSplitter:
    """Incremental version of :func:`segment_text` for streaming responses.

    Text is emitted as soon as it is provably outside a UI wrapper. A partial
    ``<details`` prefix at the end of a chunk is held back until the next chunk
    disambiguates it, so a wrapper split across TCP frames is never leaked as
    assistant text.
    """

    _MAX_HOLDBACK = 64 * 1024

    def __init__(self) -> None:
        self._buffer = ""
        self._in_wrapper = False
        self._wrapper_body = ""

    def feed(self, chunk: str) -> list[Segment]:
        if not chunk:
            return []
        segments: list[Segment] = []
        self._buffer += chunk
        while True:
            produced = self._step(segments)
            if not produced:
                break
        return segments

    def close(self) -> list[Segment]:
        """Flush anything still buffered at end of stream."""
        segments: list[Segment] = []
        if self._in_wrapper:
            leftover = self._wrapper_body + self._buffer
            self._wrapper_body = ""
            self._buffer = ""
            self._in_wrapper = False
            if leftover.strip():
                segments.append(
                    Segment(
                        kind=SegmentKind.AMBIGUOUS,
                        text=_strip_tags(leftover),
                        note="unterminated_ui_wrapper",
                    )
                )
            return segments
        if self._buffer:
            segments.append(Segment(kind=SegmentKind.TEXT, text=self._buffer))
            self._buffer = ""
        return segments

    # -- internals --------------------------------------------------------
    def _step(self, out: list[Segment]) -> bool:
        if self._in_wrapper:
            closing = _CLOSE_DETAILS.search(self._buffer)
            if closing is None:
                # Keep accumulating; guard against unbounded growth.
                self._wrapper_body += self._buffer
                self._buffer = ""
                if len(self._wrapper_body) > self._MAX_HOLDBACK:
                    out.append(
                        Segment(
                            kind=SegmentKind.AMBIGUOUS,
                            text=_strip_tags(self._wrapper_body),
                            note="oversized_ui_wrapper",
                        )
                    )
                    self._wrapper_body = ""
                    self._in_wrapper = False
                return False
            inner = self._wrapper_body + self._buffer[: closing.start()]
            self._buffer = self._buffer[closing.end() :]
            self._wrapper_body = ""
            self._in_wrapper = False
            summary_match = _SUMMARY.search(inner)
            summary = summary_match.group(1) if summary_match else ""
            body = _SUMMARY.sub("", inner) if summary_match else inner
            out.append(classify_wrapper_body(body, summary))
            return True

        opening = _OPEN_DETAILS.search(self._buffer)
        if opening is not None:
            prefix = self._buffer[: opening.start()]
            if prefix:
                out.append(Segment(kind=SegmentKind.TEXT, text=prefix))
            self._buffer = self._buffer[opening.end() :]
            self._in_wrapper = True
            return True

        # No wrapper in sight: emit everything except a possible partial tag.
        safe_upto = _safe_emit_boundary(self._buffer)
        if safe_upto > 0:
            out.append(Segment(kind=SegmentKind.TEXT, text=self._buffer[:safe_upto]))
            self._buffer = self._buffer[safe_upto:]
        return False


_PARTIAL_OPEN = "<details"


def _safe_emit_boundary(buffer: str) -> int:
    """Return how much of ``buffer`` can be emitted without splitting a tag."""
    idx = buffer.rfind("<")
    if idx < 0:
        return len(buffer)
    tail = buffer[idx:]
    if ">" in tail:
        return len(buffer)
    # Could this tail still grow into "<details ...>"?
    if _PARTIAL_OPEN.startswith(tail.lower()[: len(_PARTIAL_OPEN)]) and len(tail) <= len(
        _PARTIAL_OPEN
    ):
        return idx
    return len(buffer)
