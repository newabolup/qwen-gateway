"""Tool-calling normalization tests."""

from __future__ import annotations

import json

from app.api.schemas import FunctionDefinition, ToolDefinition
from app.gateway.normalizer import ResponseAggregator, StreamFormatter
from app.providers.events import EventType
from app.providers.qwen.parser import QwenEventParser
from app.providers.qwen.tools import (
    ToolCallTextParser,
    build_tool_instruction,
    coerce_arguments,
    parse_tool_block,
)

POWERSHELL_TOOL = ToolDefinition(
    function=FunctionDefinition(
        name="powershell",
        description="Run a PowerShell command",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
    )
)


def _delta(content: str) -> dict:
    return {"choices": [{"delta": {"role": "assistant", "content": content, "phase": "answer"}}]}


class TestToolBlockParsing:
    def test_pwsh_style_action_becomes_tool_call(self) -> None:
        block = '{"name": "powershell", "arguments": {"command": "Get-Location"}}'
        payload, warning = parse_tool_block(block, ["powershell"])
        assert warning is None
        assert payload is not None
        assert payload.name == "powershell"
        assert json.loads(payload.arguments) == {"command": "Get-Location"}
        assert payload.id.startswith("call_")

    def test_undeclared_tool_is_rejected_with_warning(self) -> None:
        block = '{"name": "rm_rf", "arguments": {}}'
        payload, warning = parse_tool_block(block, ["powershell"])
        assert payload is None
        assert warning is not None and "undeclared" in warning

    def test_invalid_json_is_reported_not_invented(self) -> None:
        payload, warning = parse_tool_block("Pwsh\nShow current working directory", ["powershell"])
        assert payload is None
        assert warning is not None

    def test_arguments_always_serialized_as_json_string(self) -> None:
        assert coerce_arguments({"a": 1}) == '{"a": 1}'
        assert coerce_arguments(None) == "{}"
        assert coerce_arguments('{"a":1}') == '{"a":1}'
        assert json.loads(coerce_arguments("plain text"))["input"] == "plain text"


class TestStreamedToolCalls:
    def test_tool_call_split_across_chunks(self) -> None:
        parser = ToolCallTextParser(["powershell"])
        results = []
        for chunk in [
            "Working. <tool_",
            'call>{"name": "powershell", "arg',
            'uments": {"command": "Get-Location"}}</tool_call>',
        ]:
            results.extend(parser.feed(chunk))

        text = "".join(v for k, v in results if k == "text")
        calls = [v for k, v in results if k == "tool_call"]
        assert "<tool_" not in text
        assert text.strip() == "Working."
        assert len(calls) == 1
        assert calls[0].name == "powershell"

    def test_multiple_tool_calls(self) -> None:
        parser = ToolCallTextParser(["a", "b"])
        results = parser.feed(
            '<tool_call>{"name":"a","arguments":{}}</tool_call>'
            '<tool_call>{"name":"b","arguments":{"x":1}}</tool_call>'
        )
        calls = [v for k, v in results if k == "tool_call"]
        assert [c.name for c in calls] == ["a", "b"]
        assert [c.index for c in calls] == [0, 1]

    def test_native_streamed_tool_call_accumulates(self) -> None:
        parser = QwenEventParser()
        parser.feed_json(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "get_weather", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            }
        )
        parser.feed_json(
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}}
                ]
            }
        )
        parser.feed_json(
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"Dubai"}'}}]}}
                ]
            }
        )
        events = parser.close()
        calls = [e.tool_call for e in events if e.type is EventType.TOOL_CALL]
        assert len(calls) == 1
        assert calls[0].name == "get_weather"
        assert json.loads(calls[0].arguments) == {"city": "Dubai"}

    def test_public_response_exposes_openai_tool_calls(self) -> None:
        parser = QwenEventParser(parse_text_tool_calls=True, tool_names=["powershell"])
        events = parser.feed_json(
            _delta(
                '<tool_call>{"name":"powershell","arguments":{"command":"Get-Location"}}</tool_call>'
            )
        )
        events += parser.close()
        aggregator = ResponseAggregator()
        aggregator.add_all(events)
        response = aggregator.to_response("qwen", "chatcmpl_x")
        dumped = response.model_dump(exclude_none=True)
        choice = dumped["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        tool_calls = choice["message"]["tool_calls"]
        assert tool_calls[0]["type"] == "function"
        assert tool_calls[0]["function"]["name"] == "powershell"
        assert "<tool_call>" not in json.dumps(dumped)

    def test_stream_formatter_emits_tool_deltas(self) -> None:
        formatter = StreamFormatter(completion_id="chatcmpl_x", model="qwen")
        parser = QwenEventParser(parse_text_tool_calls=True, tool_names=["powershell"])
        chunks = []
        for event in parser.feed_json(
            _delta('<tool_call>{"name":"powershell","arguments":{"command":"ls"}}</tool_call>')
        ):
            chunks.extend(formatter.handle(event))
        for event in parser.close():
            chunks.extend(formatter.handle(event))

        payloads = [c.model_dump(exclude_none=True) for c in chunks]
        starts = [
            p
            for p in payloads
            if p["choices"]
            and p["choices"][0]["delta"].get("tool_calls")
            and p["choices"][0]["delta"]["tool_calls"][0].get("id")
        ]
        assert starts, "expected an opening tool_call delta with an id"
        assert payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"


class TestToolInstruction:
    def test_instruction_lists_declared_tools_only(self) -> None:
        instruction = build_tool_instruction([POWERSHELL_TOOL])
        assert "powershell" in instruction
        assert "<tool_call>" in instruction
        assert "Run a PowerShell command" in instruction

    def test_no_tools_no_instruction(self) -> None:
        assert build_tool_instruction([]) == ""
