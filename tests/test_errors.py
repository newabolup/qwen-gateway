"""Error-normalization tests."""

from __future__ import annotations

import httpx
import pytest

from app.gateway import errors
from app.gateway.errors import ErrorCategory


class TestStatusMapping:
    @pytest.mark.parametrize(
        ("status", "category", "retryable"),
        [
            (401, ErrorCategory.AUTHENTICATION, True),
            (403, ErrorCategory.PERMISSION, True),
            (408, ErrorCategory.TIMEOUT, True),
            (429, ErrorCategory.RATE_LIMIT, True),
            (500, ErrorCategory.UPSTREAM, True),
            (502, ErrorCategory.UPSTREAM, True),
            (503, ErrorCategory.UPSTREAM, True),
            (504, ErrorCategory.TIMEOUT, True),
            (400, ErrorCategory.UPSTREAM, False),
            (404, ErrorCategory.UPSTREAM, False),
        ],
    )
    def test_upstream_status_mapping(self, status, category, retryable) -> None:
        error = errors.from_upstream_status(status, detail="x")
        assert error.category == category
        assert error.retryable is retryable
        # Upstream 5xx must not be echoed verbatim as the gateway's own status.
        assert 400 <= error.status_code <= 504

    def test_rate_limit_carries_retry_after(self) -> None:
        error = errors.from_upstream_status(429, retry_after=42)
        assert error.retry_after == 42
        assert error.status_code == 429


class TestExceptionMapping:
    def test_timeout(self) -> None:
        request = httpx.Request("POST", "https://example.invalid")
        error = errors.from_exception(httpx.ReadTimeout("slow", request=request))
        assert error.category == ErrorCategory.TIMEOUT
        assert error.retryable

    def test_connection_reset(self) -> None:
        error = errors.from_exception(ConnectionResetError("reset by peer"))
        assert error.category == ErrorCategory.NETWORK
        assert error.retryable

    def test_remote_protocol_error_is_parse_error(self) -> None:
        request = httpx.Request("POST", "https://example.invalid")
        error = errors.from_exception(httpx.RemoteProtocolError("bad chunk", request=request))
        assert error.category == ErrorCategory.PARSE

    def test_unknown_exception_is_internal(self) -> None:
        error = errors.from_exception(RuntimeError("boom"))
        assert error.category == ErrorCategory.INTERNAL
        assert error.status_code == 500


class TestPublicShape:
    def test_public_payload_structure(self) -> None:
        error = errors.upstream_unavailable("internal detail with token=secret123")
        payload = error.to_public_dict()
        assert set(payload["error"]) >= {"message", "type", "code"}
        assert payload["error"]["code"] == "provider_unavailable"
        assert payload["error"]["message"] == "Qwen provider temporarily unavailable."
        # Internal diagnostics never appear in the public body.
        assert "secret123" not in str(payload)
        assert "internal detail" not in str(payload)

    def test_secrets_redacted_from_messages(self) -> None:
        error = errors.GatewayError(
            message="failed with Bearer eyJhbGciOi.JzdWIiOiIx.abc123def456",
            category=ErrorCategory.UPSTREAM,
            code="x",
        )
        assert "eyJhbGciOi" not in str(error.to_public_dict())


class TestErrorScenariosEndToEnd:
    @pytest.mark.parametrize(
        ("scenario", "expected_status"),
        [
            ("mock:401", 502),
            ("mock:403", 502),
            ("mock:429", 429),
            ("mock:500", 502),
            ("mock:timeout", 504),
            ("mock:empty", 502),
        ],
    )
    async def test_upstream_failures_are_normalized(
        self, client, auth_headers, app_context, scenario, expected_status
    ) -> None:
        from app.database import session_scope
        from app.gateway.scheduler import get_scheduler
        from app.services.credential_service import create_credential

        get_scheduler().reset()
        async with session_scope() as session:
            await create_credential(session, name="only", secret=scenario, provider="mock")

        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == expected_status, response.text
        body = response.json()
        assert "error" in body
        assert {"message", "type", "code"} <= set(body["error"])
        assert "Traceback" not in response.text

    async def test_malformed_upstream_event_recovers(
        self, client, auth_headers, app_context
    ) -> None:
        from app.database import session_scope
        from app.gateway.scheduler import get_scheduler
        from app.services.credential_service import create_credential

        get_scheduler().reset()
        async with session_scope() as session:
            await create_credential(
                session, name="malformed", secret="mock:malformed", provider="mock"
            )

        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert "Recovered" in response.json()["choices"][0]["message"]["content"]

    async def test_stream_disconnect_terminates_cleanly(
        self, client, auth_headers, app_context
    ) -> None:
        """An upstream drop mid-stream ends with an error frame plus [DONE]."""
        from app.database import session_scope
        from app.gateway.scheduler import get_scheduler
        from app.services.credential_service import create_credential

        get_scheduler().reset()
        async with session_scope() as session:
            await create_credential(session, name="drop", secret="mock:disconnect", provider="mock")

        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "mock-qwen",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

        assert body.rstrip().endswith("data: [DONE]")
        assert '"error"' in body
        assert "partial answer before" in body
