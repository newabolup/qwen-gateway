"""Mock-provider tests.

Confirms the mock covers every condition the gateway must handle, so the whole
suite can run without a real Qwen account.
"""

from __future__ import annotations

import pytest

from app.gateway.errors import ErrorCategory, GatewayError
from app.gateway.normalizer import ResponseAggregator
from app.providers.mock.client import SCENARIOS, MockProvider, resolve_scenario
from tests.conftest import make_credential, make_provider_request


async def _collect(scenario: str, **kwargs):
    provider = MockProvider()
    request = make_provider_request(secret=f"mock:{scenario}", **kwargs)
    return [event async for event in provider.stream_completion(request)]


class TestScenarioSelection:
    def test_secret_selects_scenario(self) -> None:
        request = make_provider_request(secret="mock:reasoning")
        assert resolve_scenario(request) == "reasoning"

    def test_model_suffix_selects_scenario(self) -> None:
        request = make_provider_request(secret="anything", model="mock-qwen#429")
        assert resolve_scenario(request) == "429"

    def test_default_is_echo(self) -> None:
        request = make_provider_request(secret="plain")
        assert resolve_scenario(request) == "echo"


class TestSuccessScenarios:
    async def test_normal(self) -> None:
        aggregator = ResponseAggregator()
        aggregator.add_all(await _collect("normal"))
        assert aggregator.result.content == "Hello! How can I help you?"
        assert aggregator.result.usage.total_tokens == 16

    async def test_reasoning_separated(self) -> None:
        aggregator = ResponseAggregator(expose_reasoning=True)
        aggregator.add_all(await _collect("reasoning"))
        assert aggregator.result.content == "Hello there!"
        assert "greeted me" in aggregator.result.reasoning

    async def test_metadata_not_in_content(self) -> None:
        aggregator = ResponseAggregator()
        aggregator.add_all(await _collect("metadata"))
        assert aggregator.result.content.strip() == "I am ready to assist you."
        assert aggregator.result.metadata

    async def test_tool_call(self) -> None:
        aggregator = ResponseAggregator()
        aggregator.add_all(await _collect("tool_call"))
        assert aggregator.result.tool_calls[0].name == "powershell"
        assert aggregator.result.effective_finish_reason() == "tool_calls"

    async def test_multiple_tool_calls(self) -> None:
        aggregator = ResponseAggregator()
        aggregator.add_all(await _collect("multi_tool_call"))
        assert [c.name for c in aggregator.result.tool_calls] == ["powershell", "read_file"]

    async def test_native_tool_call(self) -> None:
        aggregator = ResponseAggregator()
        aggregator.add_all(await _collect("native_tool_call"))
        assert aggregator.result.tool_calls[0].name == "get_weather"

    async def test_malformed_recovers(self) -> None:
        aggregator = ResponseAggregator()
        aggregator.add_all(await _collect("malformed"))
        assert "Recovered" in aggregator.result.content
        assert aggregator.result.warnings

    async def test_echo_uses_prompt(self) -> None:
        provider = MockProvider()
        request = make_provider_request([{"role": "user", "content": "ping"}], secret="plain")
        aggregator = ResponseAggregator()
        aggregator.add_all([e async for e in provider.stream_completion(request)])
        assert "ping" in aggregator.result.content


class TestFailureScenarios:
    @pytest.mark.parametrize(
        ("scenario", "category"),
        [
            ("401", ErrorCategory.AUTHENTICATION),
            ("403", ErrorCategory.PERMISSION),
            ("429", ErrorCategory.RATE_LIMIT),
            ("500", ErrorCategory.UPSTREAM),
            ("timeout", ErrorCategory.TIMEOUT),
            ("network", ErrorCategory.NETWORK),
            ("empty", ErrorCategory.PARSE),
        ],
    )
    async def test_error_scenarios(self, scenario, category) -> None:
        with pytest.raises(GatewayError) as exc:
            await _collect(scenario)
        assert exc.value.category == category

    async def test_disconnect_yields_partial_then_fails(self) -> None:
        provider = MockProvider()
        request = make_provider_request(secret="mock:disconnect")
        collected = []
        with pytest.raises(GatewayError):
            async for event in provider.stream_completion(request):
                collected.append(event)
        assert collected, "partial output should be emitted before the failure"


class TestProviderInterface:
    async def test_authenticate(self) -> None:
        provider = MockProvider()
        assert (await provider.authenticate(make_credential())).ok
        assert not (await provider.authenticate(make_credential("mock:invalid"))).ok

    async def test_health_check(self) -> None:
        provider = MockProvider()
        assert (await provider.health_check()).healthy
        assert not (await provider.health_check(make_credential("mock:unhealthy"))).healthy

    async def test_list_models(self) -> None:
        models = await MockProvider().list_models()
        assert any(m.id == "mock-qwen" for m in models)

    async def test_create_completion_matches_stream(self) -> None:
        provider = MockProvider()
        events = await provider.create_completion(make_provider_request(secret="mock:normal"))
        aggregator = ResponseAggregator()
        aggregator.add_all(events)
        assert aggregator.result.content == "Hello! How can I help you?"

    def test_required_scenarios_exist(self) -> None:
        required = {"normal", "tool_call", "reasoning", "metadata", "malformed", "empty"}
        assert required <= set(SCENARIOS)
