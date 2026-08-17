"""Mock provider.

Generates every upstream condition the gateway must survive, using *the same
raw event shapes the real Qwen adapter parses* — so the mock exercises the
production parser rather than bypassing it.

Behaviour is selected by the credential secret (``mock:<scenario>``) or by the
requested model suffix, e.g. ``mock-429`` or ``qwen3-max#tool_call``.

Scenarios: ``echo`` (default), ``normal``, ``tool_call``, ``multi_tool_call``,
``reasoning``, ``metadata``, ``429``, ``401``, ``403``, ``500``, ``timeout``,
``malformed``, ``empty``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.gateway import errors
from app.providers.base import (
    AuthResult,
    HealthResult,
    Provider,
    ProviderCredential,
    ProviderModelInfo,
    ProviderRequest,
)
from app.providers.events import NormalizedEvent
from app.providers.qwen.parser import QwenEventParser

MOCK_MODELS: tuple[ProviderModelInfo, ...] = (
    ProviderModelInfo("mock-qwen", "Mock Qwen", 32768, True, True),
    ProviderModelInfo("mock-qwen-thinking", "Mock Qwen Thinking", 32768, True, True),
)

DEFAULT_REPLY = "Hello! How can I help you?"


def _delta(content: str, phase: str = "answer", **extra: Any) -> dict[str, Any]:
    """One raw upstream event in the web dialect."""
    delta: dict[str, Any] = {"role": "assistant", "content": content, "phase": phase}
    delta.update(extra)
    return {"choices": [{"delta": delta}]}


def _finish(reason: str = "stop", usage: dict[str, int] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "choices": [
            {
                "delta": {"role": "assistant", "content": "", "phase": "answer"},
                "finish_reason": reason,
            }
        ]
    }
    if usage:
        payload["usage"] = usage
    return payload


#: Raw upstream event scripts, keyed by scenario.
SCENARIOS: dict[str, list[dict[str, Any]]] = {
    "normal": [
        _delta("Hello", "answer"),
        _delta("! How can I help you?", "answer"),
        _finish("stop", {"prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16}),
    ],
    "metadata": [
        # The exact class of payload that must NOT become assistant text.
        _delta(
            "<details>\n<summary></summary>\n\n"
            "Response ID: 4c2f0f0f-1d3a-4b9e-9f0a-2b7f7d1f0d21\n"
            "Request ID: 8b1c7a52-2f43-4a1d-9d1b-7d0d2c1a3e55\n"
            "Copy\n\n</details>\n\n",
            "answer",
        ),
        _delta("I am ready to assist you.", "answer"),
        _finish("stop"),
    ],
    "reasoning": [
        _delta("The user greeted me. ", "think"),
        _delta("I should greet back.", "think"),
        _delta("Hello there!", "answer"),
        _finish("stop", {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}),
    ],
    "tool_call": [
        _delta("", "answer"),
        _delta('<tool_call>\n{"name": "powershell", "arguments": ', "answer"),
        _delta('{"command": "Get-Location"}}\n</tool_call>', "answer"),
        _finish("stop"),
    ],
    "multi_tool_call": [
        _delta(
            '<tool_call>{"name": "powershell", "arguments": {"command": "Get-Location"}}</tool_call>',
            "answer",
        ),
        _delta(
            '<tool_call>{"name": "read_file", "arguments": {"path": "README.md"}}</tool_call>',
            "answer",
        ),
        _finish("stop"),
    ],
    "native_tool_call": [
        {
            "choices": [
                {
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_mock_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}}
            ]
        },
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"Dubai"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ],
    "malformed": [
        {"unexpected": "shape", "no_choices": True},
        _delta("Recovered after a malformed event.", "answer"),
        _finish("stop"),
    ],
    "empty": [],
}

_STATUS_SCENARIOS = {"401": 401, "403": 403, "429": 429, "500": 500, "502": 502}


def resolve_scenario(req: ProviderRequest) -> str:
    secret = req.credential.secret or ""
    if secret.startswith("mock:"):
        return secret.split(":", 1)[1].strip() or "normal"
    model = req.upstream_model or ""
    if "#" in model:
        return model.split("#", 1)[1]
    if model.startswith("mock-") and model[5:] in SCENARIOS:
        return model[5:]
    for key in _STATUS_SCENARIOS:
        if model.endswith(f"-{key}"):
            return key
    # Default: echo the prompt so a developer pointing a real client at the
    # mock provider sees their input reflected back.
    return "echo"


class MockProvider(Provider):
    """Deterministic provider used for development and the test suite."""

    name = "mock"

    def __init__(self, chunk_delay: float = 0.0) -> None:
        self.chunk_delay = chunk_delay

    async def authenticate(self, credential: ProviderCredential) -> AuthResult:
        if credential.secret.endswith(":401") or credential.secret == "mock:invalid":  # noqa: S105
            return AuthResult(ok=False, detail="mock credential rejected")
        return AuthResult(ok=True, detail="mock credential accepted")

    async def list_models(
        self, credential: ProviderCredential | None = None
    ) -> list[ProviderModelInfo]:
        return list(MOCK_MODELS)

    async def health_check(self, credential: ProviderCredential | None = None) -> HealthResult:
        if credential is not None and credential.secret == "mock:unhealthy":  # noqa: S105
            return HealthResult(healthy=False, detail="mock provider marked unhealthy")
        return HealthResult(healthy=True, latency_ms=1.0)

    async def create_completion(self, req: ProviderRequest) -> list[NormalizedEvent]:
        return [event async for event in self.stream_completion(req)]

    async def stream_completion(self, req: ProviderRequest) -> AsyncIterator[NormalizedEvent]:
        scenario = resolve_scenario(req)

        if scenario in _STATUS_SCENARIOS:
            raise errors.from_upstream_status(
                _STATUS_SCENARIOS[scenario],
                retry_after=2 if scenario == "429" else None,
                detail=f"mock scenario {scenario}",
            )
        if scenario == "timeout":
            raise errors.upstream_timeout("mock scenario timeout")
        if scenario == "network":
            raise errors.network_error("mock scenario network failure")
        if scenario == "disconnect":
            yield NormalizedEvent.text_event("partial answer before ")
            raise errors.network_error("mock scenario stream disconnect")

        tools = req.request.effective_tools()
        parser = QwenEventParser(
            parse_text_tool_calls=bool(tools) or scenario in {"tool_call", "multi_tool_call"},
            tool_names=[t.function.name for t in tools],
        )

        script = SCENARIOS.get(scenario)
        if script is None:
            script = _echo_script(req)

        if not script:
            raise errors.parse_error("mock scenario produced no events")

        for raw in script:
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            for event in parser.feed_json(raw):
                yield event
        for event in parser.close():
            yield event


def _echo_script(req: ProviderRequest) -> list[dict[str, Any]]:
    """Default: echo the last user message so dev clients see something real."""
    last_user = ""
    for message in reversed(req.request.messages):
        if message.role == "user":
            last_user = message.text()
            break
    reply = f"Mock reply to: {last_user}" if last_user else DEFAULT_REPLY
    events = [_delta(word + " ", "answer") for word in reply.split()]
    events.append(
        _finish(
            "stop",
            {
                "prompt_tokens": max(1, len(last_user) // 4),
                "completion_tokens": max(1, len(reply) // 4),
                "total_tokens": max(2, (len(last_user) + len(reply)) // 4),
            },
        )
    )
    return events
