"""Public API tests: health, models, completions, streaming, authentication."""

from __future__ import annotations

import json

import pytest


class TestHealth:
    async def test_health(self, client) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_api_health_reports_token_pool(self, client, mock_credential) -> None:
        response = await client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["tokens"]["total"] == 1
        assert body["tokens"]["healthy"] == 1
        assert body["status"] == "ok"

    async def test_api_health_degraded_without_tokens(self, client) -> None:
        body = (await client.get("/api/health")).json()
        assert body["status"] == "degraded"
        assert body["tokens"]["healthy"] == 0

    async def test_stats(self, client) -> None:
        body = (await client.get("/api/stats")).json()
        assert "requests_today" in body
        assert "tokens" in body


class TestModels:
    async def test_requires_auth(self, client) -> None:
        assert (await client.get("/v1/models")).status_code == 401

    async def test_lists_models_and_aliases(self, client, auth_headers) -> None:
        response = await client.get("/v1/models", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "list"
        ids = {m["id"] for m in body["data"]}
        assert "mock-qwen" in ids
        assert "qwen" in ids  # alias exposed as a first-class id
        for model in body["data"]:
            assert model["object"] == "model"


class TestAuthentication:
    async def test_missing_key(self, client) -> None:
        response = await client.post("/v1/chat/completions", json={"messages": []})
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "authentication_error"

    async def test_invalid_key(self, client) -> None:
        response = await client.get(
            "/v1/models", headers={"Authorization": "Bearer qwg_not_a_real_key"}
        )
        assert response.status_code == 401

    async def test_disabled_key_rejected(self, client, app_context) -> None:
        from app.database import session_scope
        from app.services.api_key_service import create_api_key, update_api_key

        async with session_scope() as session:
            record, plaintext = await create_api_key(session, name="temp")
            await update_api_key(session, record.id, enabled=False)

        response = await client.get("/v1/models", headers={"Authorization": f"Bearer {plaintext}"})
        assert response.status_code == 401
        assert "disabled" in response.json()["error"]["message"].lower()

    async def test_revoked_key_rejected(self, client, app_context) -> None:
        from app.database import session_scope
        from app.services.api_key_service import create_api_key, revoke_api_key

        async with session_scope() as session:
            record, plaintext = await create_api_key(session, name="temp2")
            await revoke_api_key(session, record.id)

        response = await client.get("/v1/models", headers={"Authorization": f"Bearer {plaintext}"})
        assert response.status_code == 401

    async def test_expired_key_rejected(self, client, app_context) -> None:
        from datetime import datetime, timedelta, timezone

        from app.database import session_scope
        from app.models.db import ApiKey
        from app.services.api_key_service import create_api_key

        async with session_scope() as session:
            record, plaintext = await create_api_key(session, name="temp3")
            row = await session.get(ApiKey, record.id)
            row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)

        response = await client.get("/v1/models", headers={"Authorization": f"Bearer {plaintext}"})
        assert response.status_code == 401
        assert "expired" in response.json()["error"]["message"].lower()


class TestChatCompletions:
    async def test_non_streaming_completion(self, client, auth_headers, mock_credential) -> None:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "qwen",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["id"].startswith("chatcmpl_")
        assert body["model"] == "qwen"
        choice = body["choices"][0]
        assert choice["index"] == 0
        assert choice["message"]["role"] == "assistant"
        assert choice["message"]["content"]
        assert choice["finish_reason"] == "stop"
        assert "usage" in body

    async def test_metadata_scenario_not_leaked_via_api(
        self, client, auth_headers, app_context
    ) -> None:
        from app.database import session_scope
        from app.services.credential_service import create_credential

        async with session_scope() as session:
            await create_credential(session, name="meta", secret="mock:metadata", provider="mock")

        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "mock-qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        assert "Response ID" not in content
        assert "Request ID" not in content
        assert "<details>" not in content
        assert content.strip() == "I am ready to assist you."

    async def test_streaming_completion(self, client, auth_headers, mock_credential) -> None:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "qwen",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        ) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            body = ""
            async for chunk in response.aiter_text():
                body += chunk

        lines = [line for line in body.split("\n") if line.startswith("data: ")]
        assert lines[-1].strip() == "data: [DONE]"

        payloads = [json.loads(line[6:]) for line in lines if line[6:].strip() != "[DONE]"]
        assert payloads[0]["object"] == "chat.completion.chunk"
        assert payloads[0]["choices"][0]["delta"]["role"] == "assistant"
        content = "".join(
            p["choices"][0]["delta"].get("content") or "" for p in payloads if p["choices"]
        )
        assert content.strip()
        finishes = [
            p["choices"][0]["finish_reason"]
            for p in payloads
            if p["choices"] and p["choices"][0].get("finish_reason")
        ]
        assert finishes == ["stop"]
        usage_chunks = [p for p in payloads if p.get("usage")]
        assert usage_chunks, "include_usage should produce a usage chunk"

    async def test_streaming_tool_call(self, client, auth_headers, app_context) -> None:
        from app.database import session_scope
        from app.services.credential_service import create_credential

        async with session_scope() as session:
            await create_credential(session, name="tools", secret="mock:tool_call", provider="mock")

        async with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "mock-qwen",
                "messages": [{"role": "user", "content": "where am I"}],
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "powershell",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            },
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

        payloads = [
            json.loads(line[6:])
            for line in body.split("\n")
            if line.startswith("data: ") and line[6:].strip() != "[DONE]"
        ]
        tool_deltas = [
            p for p in payloads if p["choices"] and p["choices"][0]["delta"].get("tool_calls")
        ]
        assert tool_deltas
        assert (
            tool_deltas[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"]
            == "powershell"
        )
        assert payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"

    async def test_no_credentials_returns_503(self, client, auth_headers) -> None:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "no_available_credential"


class TestValidation:
    async def test_empty_messages_rejected(self, client, auth_headers) -> None:
        response = await client.post(
            "/v1/chat/completions", headers=auth_headers, json={"model": "qwen", "messages": []}
        )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request"

    async def test_malformed_json_rejected(self, client, auth_headers) -> None:
        response = await client.post(
            "/v1/chat/completions",
            headers={**auth_headers, "Content-Type": "application/json"},
            content=b"{not json",
        )
        assert response.status_code == 400

    async def test_bad_temperature_rejected(self, client, auth_headers) -> None:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "qwen",
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 99,
            },
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("role", ["user", "system", "assistant", "tool"])
    async def test_roles_accepted(self, client, auth_headers, mock_credential, role) -> None:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "qwen",
                "messages": [
                    {"role": role, "content": "text", "tool_call_id": "call_1"},
                    {"role": "user", "content": "hi"},
                ],
            },
        )
        assert response.status_code == 200


class TestRequestLogging:
    async def test_request_is_recorded(self, client, auth_headers, mock_credential) -> None:
        await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        from sqlalchemy import select

        from app.database import session_scope
        from app.models.db import RequestLog

        async with session_scope() as session:
            rows = list((await session.execute(select(RequestLog))).scalars())
        assert len(rows) == 1
        assert rows[0].status == "success"
        assert rows[0].request_id.startswith("gwreq_")
        assert rows[0].credential_id == mock_credential.id
        assert rows[0].latency_ms is not None

    async def test_correlation_id_header(self, client) -> None:
        response = await client.get("/health")
        assert response.headers["X-Gateway-Request-Id"].startswith("gwreq_")
