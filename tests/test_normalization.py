"""Response-normalization tests.

The central requirement: UI-only markup and diagnostic metadata emitted by the
Qwen web surface must never become assistant text.
"""

from __future__ import annotations

import pytest

from app.gateway.normalizer import ResponseAggregator
from app.providers.events import EventType
from app.providers.qwen.markup import (
    SegmentKind,
    StreamingMarkupSplitter,
    segment_text,
)
from app.providers.qwen.parser import QwenEventParser

# The exact payload class described in the requirements.
UNWANTED_METADATA = (
    "<details>\n"
    "<summary></summary>\n\n"
    "Response ID: 4c2f0f0f-1d3a-4b9e-9f0a-2b7f7d1f0d21\n"
    "Request ID: 8b1c7a52-2f43-4a1d-9d1b-7d0d2c1a3e55\n"
    "Copy\n\n"
    "</details>\n\n"
    "I am ready to assist you..."
)


def _delta(content: str, phase: str = "answer") -> dict:
    return {"choices": [{"delta": {"role": "assistant", "content": content, "phase": phase}}]}


class TestMarkupSegmentation:
    def test_details_block_classified_as_metadata(self) -> None:
        segments = segment_text(UNWANTED_METADATA)
        kinds = [s.kind for s in segments]
        assert SegmentKind.METADATA in kinds

        metadata = next(s for s in segments if s.kind is SegmentKind.METADATA)
        assert metadata.data["response_id"] == "4c2f0f0f-1d3a-4b9e-9f0a-2b7f7d1f0d21"
        assert metadata.data["request_id"] == "8b1c7a52-2f43-4a1d-9d1b-7d0d2c1a3e55"

        text = "".join(s.text for s in segments if s.kind is SegmentKind.TEXT)
        assert text.strip() == "I am ready to assist you..."
        assert "Response ID" not in text
        assert "<details>" not in text
        assert "Copy" not in text

    def test_prose_is_never_deleted_by_keyword_matching(self) -> None:
        """A sentence mentioning 'Response ID' outside a wrapper stays intact."""
        prose = "The Response ID: 123 field is documented in section 4."
        segments = segment_text(prose)
        assert len(segments) == 1
        assert segments[0].kind is SegmentKind.TEXT
        assert segments[0].text == prose

    def test_wrapper_with_real_prose_is_ambiguous_not_text(self) -> None:
        payload = "<details><summary>Note</summary>\nActual explanation here.\n</details>"
        segments = segment_text(payload)
        assert segments[0].kind is SegmentKind.AMBIGUOUS
        assert "Actual explanation" in segments[0].text
        assert segments[0].note == "ambiguous_ui_wrapper_content"

    def test_unterminated_wrapper_is_not_assistant_text(self) -> None:
        segments = segment_text("Hello <details>\nResponse ID: abc")
        assert segments[0].kind is SegmentKind.TEXT
        assert segments[0].text == "Hello "
        assert segments[1].kind is SegmentKind.AMBIGUOUS


class TestStreamingSplitter:
    def test_wrapper_split_across_chunks_is_never_leaked(self) -> None:
        splitter = StreamingMarkupSplitter()
        chunks = [
            "Hi. <deta",
            "ils>\n<summary></summary>\nResponse ",
            "ID: xyz\n</details>",
            " Done.",
        ]
        emitted_text = ""
        metadata_seen = False
        for chunk in chunks:
            for segment in splitter.feed(chunk):
                if segment.kind is SegmentKind.TEXT:
                    emitted_text += segment.text
                elif segment.kind is SegmentKind.METADATA:
                    metadata_seen = True
        for segment in splitter.close():
            if segment.kind is SegmentKind.TEXT:
                emitted_text += segment.text

        assert metadata_seen
        assert "<details>" not in emitted_text
        assert "Response ID" not in emitted_text
        assert emitted_text.strip() == "Hi.  Done.".strip().replace("  ", "  ")

    def test_plain_text_streams_through(self) -> None:
        splitter = StreamingMarkupSplitter()
        out = ""
        for chunk in ["Hello", ", ", "world!"]:
            for segment in splitter.feed(chunk):
                out += segment.text
        for segment in splitter.close():
            out += segment.text
        assert out == "Hello, world!"


class TestQwenEventParser:
    def test_metadata_event_does_not_become_content(self) -> None:
        parser = QwenEventParser()
        events = parser.feed_json(_delta(UNWANTED_METADATA))
        events += parser.close()

        aggregator = ResponseAggregator()
        aggregator.add_all(events)
        result = aggregator.result

        assert "Response ID" not in result.content
        assert "Request ID" not in result.content
        assert "<details>" not in result.content
        assert "Copy" not in result.content
        assert result.content == "I am ready to assist you..."  # no leading blank lines
        assert any(m.get("kind") == "ui_metadata" for m in result.metadata)

    def test_reasoning_is_separated_from_answer(self) -> None:
        parser = QwenEventParser()
        events = parser.feed_json(_delta("Let me think. ", "think"))
        events += parser.feed_json(_delta("The answer is 4.", "answer"))
        events += parser.close()

        aggregator = ResponseAggregator(expose_reasoning=True)
        aggregator.add_all(events)
        assert aggregator.result.content == "The answer is 4."
        assert aggregator.result.reasoning == "Let me think. "

    def test_reasoning_hidden_by_default(self) -> None:
        parser = QwenEventParser()
        events = parser.feed_json(_delta("secret chain of thought", "think"))
        events += parser.feed_json(_delta("Answer.", "answer"))
        events += parser.close()

        response = ResponseAggregator(expose_reasoning=False)
        response.add_all(events)
        public = response.to_response("qwen", "chatcmpl_x")
        dumped = public.model_dump()
        assert dumped["choices"][0]["message"].get("reasoning_content") is None
        assert "secret chain of thought" not in str(dumped)

    def test_reasoning_content_field(self) -> None:
        parser = QwenEventParser()
        events = parser.feed_json(
            {"choices": [{"delta": {"reasoning_content": "internal", "content": ""}}]}
        )
        events += parser.feed_json(_delta("visible"))
        events += parser.close()
        aggregator = ResponseAggregator(expose_reasoning=True)
        aggregator.add_all(events)
        assert aggregator.result.reasoning == "internal"
        assert aggregator.result.content == "visible"

    def test_thinking_summary_increments_only_new_thoughts(self) -> None:
        parser = QwenEventParser()
        first = parser.feed_json(
            {
                "choices": [
                    {
                        "delta": {
                            "phase": "thinking_summary",
                            "content": "",
                            "extra": {"summary_thought": {"content": ["step one"]}},
                        }
                    }
                ]
            }
        )
        second = parser.feed_json(
            {
                "choices": [
                    {
                        "delta": {
                            "phase": "thinking_summary",
                            "content": "",
                            "extra": {"summary_thought": {"content": ["step one", "step two"]}},
                        }
                    }
                ]
            }
        )
        parser.close()
        reasoning_first = "".join(e.text for e in first if e.type is EventType.REASONING)
        reasoning_second = "".join(e.text for e in second if e.type is EventType.REASONING)
        assert reasoning_first == "step one"
        assert reasoning_second == "step two"

    def test_unknown_phase_is_not_assistant_text(self) -> None:
        parser = QwenEventParser()
        events = parser.feed_json(_delta("internal agent chatter", "agent_scratchpad"))
        events += parser.close()
        aggregator = ResponseAggregator()
        aggregator.add_all(events)
        assert aggregator.result.content == ""
        assert any("unknown_phase" in w for w in aggregator.result.warnings)

    def test_usage_is_normalized(self) -> None:
        parser = QwenEventParser()
        events = parser.feed_json(
            {
                "choices": [{"delta": {"content": "hi", "phase": "answer"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            }
        )
        events += parser.close()
        aggregator = ResponseAggregator()
        aggregator.add_all(events)
        assert aggregator.result.usage.total_tokens == 7

    def test_upstream_error_event(self) -> None:
        parser = QwenEventParser()
        events = parser.feed_json(
            {"success": False, "code": "RateLimited", "msg": "too many requests"}
        )
        assert events[0].type is EventType.ERROR
        assert events[0].data["code"] == "RateLimited"

    def test_malformed_event_does_not_crash_or_leak(self) -> None:
        parser = QwenEventParser()
        events = parser.feed_json({"totally": "unexpected"})
        assert events[0].type is EventType.UNKNOWN
        events += parser.feed_json(_delta("recovered"))
        events += parser.close()
        aggregator = ResponseAggregator()
        aggregator.add_all(events)
        assert aggregator.result.content == "recovered"

    def test_finish_reason_aliases(self) -> None:
        parser = QwenEventParser()
        parser.feed_json(
            {
                "choices": [
                    {"delta": {"content": "x", "phase": "answer"}, "finish_reason": "end_turn"}
                ]
            }
        )
        events = parser.close()
        done = next(e for e in events if e.type is EventType.DONE)
        assert done.finish_reason == "stop"

    @pytest.mark.parametrize("payload", [None, 42, "text", []])
    def test_non_dict_payloads_are_safe(self, payload) -> None:
        parser = QwenEventParser()
        events = parser.feed_json(payload)
        assert all(e.type in (EventType.UNKNOWN,) for e in events)
