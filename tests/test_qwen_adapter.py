"""Qwen adapter tests.

The upstream is simulated with an httpx MockTransport so the real adapter code
path (payload construction, SSE consumption, error mapping) is exercised
without a Qwen account.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.api.schemas import ChatCompletionRequest, FunctionDefinition, ToolDefinition
from app.gateway.errors import ErrorCategory, GatewayError
from app.gateway.normalizer import ResponseAggregator
from app.providers.base import ProviderCredential, ProviderRequest
from app.providers.qwen import auth as qwen_auth
from app.providers.qwen.client import QwenProvider
from app.utils.sse import SSEDecoder, format_sse

PORTAL_TOKEN = "portal-token-value"
WEB_TOKEN = "web-session-token-value"


def _provider_request(
    *, secret: str = PORTAL_TOKEN, auth_mode: str = "portal", tools=None, model="qwen3-max"
) -> ProviderRequest:
    return ProviderRequest(
        request=ChatCompletionRequest(
            model=model, messages=[{"role": "user", "content": "hi"}], tools=tools
        ),
        upstream_model=model,
        credential=ProviderCredential(id=1, name="c1", secret=secret, auth_mode=auth_mode),
        request_id="gwreq_test",
    )


def _sse(*events: dict) -> bytes:
    body = "".join(format_sse(e) for e in events) + "data: [DONE]\n\n"
    return body.encode()


class TestPortalDialect:
    async def test_streaming_completion(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {"choices": [{"delta": {"role": "assistant", "content": "Hello"}}]},
                    {"choices": [{"delta": {"content": "!"}}]},
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
                    },
                ),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = QwenProvider(client=client)
        events = [e async for e in provider.stream_completion(_provider_request())]
        await client.aclose()

        aggregator = ResponseAggregator()
        aggregator.add_all(events)
        assert aggregator.result.content == "Hello!"
        assert aggregator.result.usage.total_tokens == 4
        assert captured["url"].endswith("/chat/completions")
        assert captured["auth"] == f"Bearer {PORTAL_TOKEN}"
        assert captured["body"]["stream"] is True
        assert captured["body"]["model"] == "qwen3-max"

    async def test_tools_forwarded_natively(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
            )

        tools = [
            ToolDefinition(
                function=FunctionDefinition(
                    name="get_weather", parameters={"type": "object", "properties": {}}
                )
            )
        ]
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = QwenProvider(client=client)
        [e async for e in provider.stream_completion(_provider_request(tools=tools))]
        await client.aclose()
        assert captured["body"]["tools"][0]["function"]["name"] == "get_weather"

    @pytest.mark.parametrize(
        ("status", "category"),
        [
            (401, ErrorCategory.AUTHENTICATION),
            (403, ErrorCategory.PERMISSION),
            (429, ErrorCategory.RATE_LIMIT),
            (500, ErrorCategory.UPSTREAM),
            (503, ErrorCategory.UPSTREAM),
        ],
    )
    async def test_http_errors_are_normalized(self, status, category) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"error": "upstream said no"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = QwenProvider(client=client)
        with pytest.raises(GatewayError) as exc:
            [e async for e in provider.stream_completion(_provider_request())]
        await client.aclose()
        assert exc.value.category == category

    async def test_retry_after_header_is_used(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "37"}, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = QwenProvider(client=client)
        with pytest.raises(GatewayError) as exc:
            [e async for e in provider.stream_completion(_provider_request())]
        await client.aclose()
        assert exc.value.retry_after == 37

    async def test_network_failure_is_normalized(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = QwenProvider(client=client)
        with pytest.raises(GatewayError) as exc:
            [e async for e in provider.stream_completion(_provider_request())]
        await client.aclose()
        assert exc.value.category == ErrorCategory.NETWORK

    async def test_empty_stream_is_parse_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n\n"
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = QwenProvider(client=client)
        with pytest.raises(GatewayError) as exc:
            [e async for e in provider.stream_completion(_provider_request())]
        await client.aclose()
        assert exc.value.category == ErrorCategory.PARSE


class TestWebDialect:
    async def test_web_flow_uses_cookie_and_phase_events(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/chats/new"):
                return httpx.Response(200, json={"data": {"id": "chat-123"}})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {"choices": [{"delta": {"phase": "think", "content": "hmm"}}]},
                    {"choices": [{"delta": {"phase": "answer", "content": "Hi there"}}]},
                    {
                        "choices": [
                            {"delta": {"phase": "answer", "content": ""}, "finish_reason": "stop"}
                        ]
                    },
                ),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = QwenProvider(client=client)
        events = [
            e
            async for e in provider.stream_completion(
                _provider_request(secret=WEB_TOKEN, auth_mode="web")
            )
        ]
        await client.aclose()

        aggregator = ResponseAggregator(expose_reasoning=True)
        aggregator.add_all(events)
        assert aggregator.result.content == "Hi there"
        assert aggregator.result.reasoning == "hmm"

        chat_request = seen[-1]
        assert "chat_id=chat-123" in str(chat_request.url)
        assert f"token={WEB_TOKEN}" in chat_request.headers.get("cookie", "")
        assert "authorization" not in {k.lower() for k in chat_request.headers}
        assert chat_request.headers["source"] == "web"

    async def test_web_tool_instruction_injected(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/chats/new"):
                return httpx.Response(200, json={"data": {"id": "c1"}})
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "phase": "answer",
                                    "content": '<tool_call>{"name":"lookup","arguments":{"q":"x"}}</tool_call>',
                                }
                            }
                        ]
                    }
                ),
            )

        tools = [
            ToolDefinition(
                function=FunctionDefinition(
                    name="lookup", parameters={"type": "object", "properties": {}}
                )
            )
        ]
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = QwenProvider(client=client)
        events = [
            e
            async for e in provider.stream_completion(
                _provider_request(secret=WEB_TOKEN, auth_mode="web", tools=tools)
            )
        ]
        await client.aclose()

        prompt = captured["body"]["messages"][0]["content"]
        assert "<tool_call>" in prompt and "lookup" in prompt

        aggregator = ResponseAggregator()
        aggregator.add_all(events)
        assert aggregator.result.tool_calls[0].name == "lookup"
        assert aggregator.result.content == ""

    async def test_bare_json_error_body_classified(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/chats/new"):
                return httpx.Response(200, json={"data": {"id": "c1"}})
            # HTTP 200 with a business failure body (observed WAF behaviour).
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(
                    {"success": False, "code": "RateLimited", "msg": "too fast"}
                ).encode(),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = QwenProvider(client=client)
        events = [
            e
            async for e in provider.stream_completion(
                _provider_request(secret=WEB_TOKEN, auth_mode="web")
            )
        ]
        await client.aclose()
        assert any(e.type.value == "error" for e in events)


class TestAuthHelpers:
    def test_jwt_claims_decoded_without_verification(self) -> None:
        import base64

        payload = (
            base64.urlsafe_b64encode(json.dumps({"exp": 1893456000, "sub": "user-1"}).encode())
            .decode()
            .rstrip("=")
        )
        token = f"header.{payload}.signature"
        info = qwen_auth.inspect_token(token)
        assert info.expires_at == 1893456000
        assert info.subject == "user-1"

    def test_garbage_token_does_not_raise(self) -> None:
        info = qwen_auth.inspect_token("not-a-jwt")
        assert info.expires_at is None

    def test_base_url_normalisation(self) -> None:
        assert (
            qwen_auth.normalize_base_url("portal.qwen.ai", "https://fallback")
            == "https://portal.qwen.ai/v1"
        )
        assert (
            qwen_auth.normalize_base_url(None, "https://portal.qwen.ai/v1/")
            == "https://portal.qwen.ai/v1"
        )

    def test_web_headers_contain_no_authorization(self) -> None:
        headers = qwen_auth.build_web_headers("tok", chat_id="c1", base_url="https://chat.qwen.ai")
        assert "Authorization" not in headers
        assert headers["Cookie"] == "token=tok"
        assert headers["Referer"].endswith("/c/c1")


class TestSSEDecoder:
    def test_frames_split_across_chunks(self) -> None:
        decoder = SSEDecoder()
        frames = decoder.push(b'data: {"a": ')
        assert frames == []
        frames = decoder.push(b'1}\n\ndata: {"b": 2}\n\n')
        assert [f.json() for f in frames] == [{"a": 1}, {"b": 2}]

    def test_multibyte_split_across_chunks(self) -> None:
        decoder = SSEDecoder()
        payload = json.dumps({"content": "你好"}).encode()
        decoder.push(b"data: " + payload[:8])
        frames = decoder.push(payload[8:] + b"\n\n")
        assert frames[0].json()["content"] == "你好"

    def test_done_marker(self) -> None:
        decoder = SSEDecoder()
        frames = decoder.push(b"data: [DONE]\n\n")
        assert frames[0].is_done

    def test_crlf_and_comments(self) -> None:
        decoder = SSEDecoder()
        frames = decoder.push(b': keepalive\r\ndata: {"x":1}\r\n\r\n')
        assert frames[0].json() == {"x": 1}

    def test_bare_json_without_data_field(self) -> None:
        decoder = SSEDecoder()
        frames = decoder.push(b'{"success": false}\n\n')
        assert frames[0].json() == {"success": False}

    def test_flush_on_close(self) -> None:
        decoder = SSEDecoder()
        decoder.push(b'data: {"tail": true}')
        frames = decoder.close()
        assert frames[0].json() == {"tail": True}
