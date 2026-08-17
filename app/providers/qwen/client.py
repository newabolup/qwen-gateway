"""Qwen provider adapter.

This is the only module in the project that knows how to talk to Qwen. It
translates the gateway's public request model into whichever upstream dialect
the credential belongs to, streams the response, and hands raw events to
:class:`~app.providers.qwen.parser.QwenEventParser` for classification.

Supported upstream dialects (see ``parser.py`` for the event shapes):

* ``portal`` — ``POST {base}/chat/completions`` with ``Authorization: Bearer``.
  OpenAI-shaped protocol, native ``tools`` support.
* ``web`` — ``POST {base}/api/v2/chat/completions?chat_id=...`` with the
  session cookie. Streaming-only, ``phase``-tagged deltas, no native tool API,
  so tool definitions are injected as an instruction and parsed back out.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.api.schemas import ChatMessage
from app.config import get_settings
from app.gateway import errors
from app.gateway.errors import GatewayError
from app.providers.base import (
    AuthResult,
    HealthResult,
    Provider,
    ProviderCredential,
    ProviderModelInfo,
    ProviderRequest,
)
from app.providers.events import EventType, NormalizedEvent
from app.providers.qwen import auth as qwen_auth
from app.providers.qwen.parser import QwenEventParser
from app.providers.qwen.tools import (
    build_tool_choice_instruction,
    build_tool_instruction,
)
from app.utils.ids import new_uuid
from app.utils.logging import get_logger
from app.utils.sse import SSEDecoder

log = get_logger(__name__)

#: Fallback catalogue used when upstream discovery is unavailable (no
#: credential yet, or the models endpoint is unreachable). Kept small and
#: overridable through the admin API / DB rather than being authoritative.
FALLBACK_MODELS: tuple[ProviderModelInfo, ...] = (
    ProviderModelInfo("qwen3-max", "Qwen3 Max", 262144, True, False),
    ProviderModelInfo("qwen3-max-thinking", "Qwen3 Max (Thinking)", 262144, True, True),
    ProviderModelInfo("qwen3-coder-plus", "Qwen3 Coder Plus", 1048576, True, False),
    ProviderModelInfo("qwen3-vl-plus", "Qwen3 VL Plus", 262144, True, False),
    ProviderModelInfo("qwen-plus", "Qwen Plus", 131072, True, False),
    ProviderModelInfo("qwen-turbo", "Qwen Turbo", 131072, True, False),
)

_REFRESH_WINDOW_SECONDS = 300.0


class QwenProvider(Provider):
    """Production adapter for Qwen."""

    name = "qwen"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._settings = get_settings()
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------
    # Infrastructure
    # ------------------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            settings = self._settings
            timeout = httpx.Timeout(
                settings.qwen_request_timeout,
                connect=settings.qwen_connect_timeout,
                read=settings.qwen_request_timeout,
                write=settings.qwen_connect_timeout,
            )
            self._client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                proxy=settings.http_proxy_url or None,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                headers={"Accept-Encoding": "gzip, deflate"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _mode(self, credential: ProviderCredential) -> str:
        configured = self._settings.qwen_mode
        if configured in {"portal", "web"}:
            return configured
        return qwen_auth.detect_auth_mode(credential.secret, credential.auth_mode)

    def _base_url(self, credential: ProviderCredential, mode: str) -> str:
        if mode == "web":
            return (credential.base_url or self._settings.qwen_web_base_url).rstrip("/")
        return qwen_auth.normalize_base_url(
            credential.base_url, self._settings.qwen_portal_base_url
        )

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------
    async def authenticate(self, credential: ProviderCredential) -> AuthResult:
        """Validate the credential, refreshing it when possible."""
        info = qwen_auth.inspect_token(credential.secret)
        mode = self._mode(credential)

        if (
            mode == "portal"
            and credential.refresh_secret
            and (info.expired or info.expiring_within(_REFRESH_WINDOW_SECONDS))
        ):
            try:
                payload = await qwen_auth.refresh_access_token(
                    self._http(), credential.refresh_secret
                )
            except httpx.HTTPStatusError as exc:
                return AuthResult(
                    ok=False,
                    detail=f"refresh rejected with status {exc.response.status_code}",
                )
            except httpx.HTTPError as exc:
                return AuthResult(ok=False, detail=f"refresh transport error: {exc}")

            access_token = str(payload["access_token"])
            new_info = qwen_auth.inspect_token(access_token)
            expires_at = new_info.expires_at
            if expires_at is None and payload.get("expires_in"):
                import time

                try:
                    expires_at = time.time() + float(payload["expires_in"])
                except (TypeError, ValueError):
                    expires_at = None
            log.info("credential_refreshed", credential_id=credential.id)
            return AuthResult(
                ok=True,
                detail="refreshed",
                rotated_secret=access_token,
                rotated_refresh_secret=(
                    str(payload["refresh_token"]) if payload.get("refresh_token") else None
                ),
                expires_at=expires_at,
                base_url=(
                    qwen_auth.normalize_base_url(
                        str(payload.get("resource_url")), self._settings.qwen_portal_base_url
                    )
                    if payload.get("resource_url")
                    else None
                ),
            )

        if info.expired:
            return AuthResult(ok=False, detail="credential expired", expires_at=info.expires_at)

        base_url = None
        if mode == "portal" and info.resource_url and not credential.base_url:
            base_url = qwen_auth.normalize_base_url(
                info.resource_url, self._settings.qwen_portal_base_url
            )
        return AuthResult(ok=True, expires_at=info.expires_at, base_url=base_url)

    async def list_models(
        self, credential: ProviderCredential | None = None
    ) -> list[ProviderModelInfo]:
        """Discover models upstream, falling back to the built-in catalogue."""
        if credential is None:
            return list(FALLBACK_MODELS)

        mode = self._mode(credential)
        base = self._base_url(credential, mode)
        try:
            if mode == "web":
                url = f"{base}/api/v2/models"
                headers = qwen_auth.build_web_headers(
                    credential.secret, chat_id=None, base_url=base
                )
            else:
                url = f"{base}/models"
                headers = qwen_auth.build_portal_headers(credential.secret)
            response = await self._http().get(url, headers=headers)
            if response.status_code >= 400:
                log.warning(
                    "model_discovery_failed",
                    status_code=response.status_code,
                    credential_id=credential.id,
                )
                return list(FALLBACK_MODELS)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("model_discovery_error", detail=str(exc), credential_id=credential.id)
            return list(FALLBACK_MODELS)

        models = _parse_model_list(payload)
        return models or list(FALLBACK_MODELS)

    async def health_check(self, credential: ProviderCredential | None = None) -> HealthResult:
        """Probe upstream reachability without consuming a completion quota."""
        import time

        started = time.perf_counter()
        settings = self._settings
        if credential is None:
            url = settings.qwen_portal_base_url.rstrip("/") + "/models"
            headers = {"Accept": "application/json"}
        else:
            mode = self._mode(credential)
            base = self._base_url(credential, mode)
            if mode == "web":
                url = f"{base}/api/v2/models"
                headers = qwen_auth.build_web_headers(
                    credential.secret, chat_id=None, base_url=base
                )
            else:
                url = f"{base}/models"
                headers = qwen_auth.build_portal_headers(credential.secret)
        try:
            response = await self._http().get(url, headers=headers, timeout=15.0)
        except httpx.HTTPError as exc:
            return HealthResult(healthy=False, detail=f"{type(exc).__name__}: {exc}")
        latency = (time.perf_counter() - started) * 1000
        healthy = response.status_code < 400
        detail = None if healthy else f"upstream status {response.status_code}"
        if response.status_code in (401, 403):
            detail = "credential rejected by upstream"
        return HealthResult(healthy=healthy, detail=detail, latency_ms=latency)

    async def create_completion(self, req: ProviderRequest) -> list[NormalizedEvent]:
        """Non-streaming completion.

        The web dialect is streaming-only upstream, so this consumes the stream
        internally and returns the collected events.
        """
        events: list[NormalizedEvent] = []
        async for event in self.stream_completion(req):
            events.append(event)
        return events

    async def stream_completion(self, req: ProviderRequest) -> AsyncIterator[NormalizedEvent]:
        mode = self._mode(req.credential)
        if mode == "web":
            async for event in self._stream_web(req):
                yield event
        else:
            async for event in self._stream_portal(req):
                yield event

    # ------------------------------------------------------------------
    # Portal dialect (OpenAI-shaped)
    # ------------------------------------------------------------------
    async def _stream_portal(self, req: ProviderRequest) -> AsyncIterator[NormalizedEvent]:
        base = self._base_url(req.credential, "portal")
        url = f"{base}/chat/completions"
        headers = qwen_auth.build_portal_headers(req.credential.secret)
        headers["X-Request-Id"] = new_uuid()
        payload = self._build_portal_payload(req)
        parser = QwenEventParser(parse_text_tool_calls=False)

        async for event in self._consume_sse(url, headers, payload, parser, req):
            yield event

    def _build_portal_payload(self, req: ProviderRequest) -> dict[str, Any]:
        request = req.request
        payload: dict[str, Any] = {
            "model": req.upstream_model,
            "messages": [_message_to_upstream(m) for m in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        max_tokens = request.effective_max_tokens()
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if request.stop:
            payload["stop"] = request.stop
        if request.presence_penalty is not None:
            payload["presence_penalty"] = request.presence_penalty
        if request.frequency_penalty is not None:
            payload["frequency_penalty"] = request.frequency_penalty
        if request.seed is not None:
            payload["seed"] = request.seed

        tools = request.effective_tools()
        if tools:
            payload["tools"] = [t.model_dump(exclude_none=True) for t in tools]
            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice

        if request.wants_reasoning():
            payload["enable_thinking"] = True
        elif request.enable_thinking is False:
            payload["enable_thinking"] = False
        return payload

    # ------------------------------------------------------------------
    # Web dialect (chat.qwen.ai)
    # ------------------------------------------------------------------
    async def _stream_web(self, req: ProviderRequest) -> AsyncIterator[NormalizedEvent]:
        base = self._base_url(req.credential, "web")
        chat_id = await self._create_web_chat(req, base)
        url = f"{base}/api/v2/chat/completions"
        if chat_id:
            url = f"{url}?chat_id={chat_id}"
        headers = qwen_auth.build_web_headers(req.credential.secret, chat_id=chat_id, base_url=base)
        headers["x-request-id"] = new_uuid()

        tools = req.request.effective_tools()
        payload = self._build_web_payload(req, chat_id)
        parser = QwenEventParser(
            parse_text_tool_calls=bool(tools),
            tool_names=[t.function.name for t in tools],
        )
        async for event in self._consume_sse(url, headers, payload, parser, req):
            yield event

    async def _create_web_chat(self, req: ProviderRequest, base: str) -> str | None:
        """Create an upstream conversation, as the web client does.

        Failure is non-fatal: the completion is attempted without a chat_id and
        the condition is recorded for diagnostics.
        """
        url = f"{base}/api/v2/chats/new"
        headers = qwen_auth.build_web_headers(req.credential.secret, chat_id=None, base_url=base)
        body = {
            "title": "New Chat",
            "models": [req.upstream_model],
            "chat_mode": "normal",
            "chat_type": "t2t",
            "timestamp": int(__import__("time").time() * 1000),
        }
        try:
            response = await self._http().post(url, headers=headers, json=body, timeout=30.0)
            if response.status_code >= 400:
                log.warning(
                    "web_chat_create_failed",
                    status_code=response.status_code,
                    credential_id=req.credential.id,
                    request_id=req.request_id,
                )
                if response.status_code in (401, 403, 429):
                    raise errors.from_upstream_status(
                        response.status_code,
                        detail="chat creation rejected",
                        retry_after=_retry_after(response),
                    )
                return None
            data = response.json()
        except GatewayError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("web_chat_create_error", detail=str(exc), request_id=req.request_id)
            return None
        chat_id = None
        if isinstance(data, dict):
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            chat_id = payload.get("id") or payload.get("chat_id")
        return str(chat_id) if chat_id else None

    def _build_web_payload(self, req: ProviderRequest, chat_id: str | None) -> dict[str, Any]:
        """Collapse the OpenAI transcript into the web client's message format."""
        request = req.request
        tools = request.effective_tools()
        instruction = build_tool_instruction(tools)
        if instruction:
            instruction += build_tool_choice_instruction(request.tool_choice)

        prompt = _flatten_transcript(request.messages, extra_system=instruction)
        thinking = request.wants_reasoning()
        message: dict[str, Any] = {
            "role": "user",
            "content": prompt,
            "user_action": "chat",
            "files": [],
            "timestamp": int(__import__("time").time()),
            "models": [req.upstream_model],
            "chat_type": "t2t",
            "feature_config": {
                "thinking_enabled": thinking,
                "output_schema": "phase",
                "thinking_budget": 81920 if thinking else 0,
            },
            "extra": {},
            "sub_chat_type": "t2t",
            "parent_id": None,
        }
        return {
            "stream": True,
            "incremental_output": True,
            "chat_id": chat_id,
            "chat_mode": "normal",
            "model": req.upstream_model,
            "parent_id": None,
            "messages": [message],
            "timestamp": int(__import__("time").time()),
        }

    # ------------------------------------------------------------------
    # Shared SSE consumption
    # ------------------------------------------------------------------
    async def _consume_sse(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        parser: QwenEventParser,
        req: ProviderRequest,
    ) -> AsyncIterator[NormalizedEvent]:
        decoder = SSEDecoder()
        saw_any_event = False
        try:
            async with self._http().stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    body = await _read_error_body(response)
                    raise errors.from_upstream_status(
                        response.status_code,
                        retry_after=_retry_after(response),
                        detail=f"upstream body: {body[:400]}",
                    )

                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type and "json" in content_type:
                    # Upstream answered with a single JSON document.
                    raw = await response.aread()
                    for event in _parse_json_body(parser, raw):
                        saw_any_event = True
                        yield event
                else:
                    async for chunk in response.aiter_bytes():
                        for frame in decoder.push(chunk):
                            if frame.is_done:
                                continue
                            for event in _frame_to_events(parser, frame):
                                saw_any_event = True
                                yield event
                    for frame in decoder.close():
                        if frame.is_done:
                            continue
                        for event in _frame_to_events(parser, frame):
                            saw_any_event = True
                            yield event
        except GatewayError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise errors.from_exception(exc) from exc

        if not saw_any_event:
            raise errors.parse_error("upstream produced no parsable events")

        for event in parser.close():
            yield event


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _frame_to_events(parser: QwenEventParser, frame: Any) -> list[NormalizedEvent]:
    payload = frame.json()
    if payload is None:
        text = frame.data.strip()
        if not text or text == "[DONE]":
            return []
        return [
            NormalizedEvent(
                type=EventType.UNKNOWN, text="", note="unparsable_sse_frame", raw=text[:500]
            )
        ]
    return parser.feed_json(payload)


def _parse_json_body(parser: QwenEventParser, raw: bytes) -> list[NormalizedEvent]:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return [
            NormalizedEvent(
                type=EventType.UNKNOWN,
                note="unparsable_json_body",
                raw=raw[:500].decode("utf-8", errors="replace"),
            )
        ]
    return parser.feed_json(payload)


async def _read_error_body(response: httpx.Response) -> str:
    try:
        raw = await response.aread()
    except (httpx.HTTPError, OSError):
        return ""
    return raw.decode("utf-8", errors="replace")


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return None


def _message_to_upstream(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role}
    if isinstance(message.content, list):
        payload["content"] = message.content
    else:
        payload["content"] = message.content or ""
    if message.name:
        payload["name"] = message.name
    if message.tool_calls:
        payload["tool_calls"] = [tc.model_dump() for tc in message.tool_calls]
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _flatten_transcript(messages: list[ChatMessage], extra_system: str = "") -> str:
    """Render an OpenAI transcript as a single prompt for the web dialect.

    The web endpoint accepts one user turn, so prior turns (including tool
    results) are serialised with explicit role markers. This keeps multi-turn
    agent loops working without inventing upstream semantics.
    """
    blocks: list[str] = []
    system_parts: list[str] = []
    if extra_system:
        system_parts.append(extra_system)

    for message in messages:
        role = message.role
        text = message.text()
        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
            continue
        if role == "user":
            blocks.append(f"[user]\n{text}")
        elif role == "assistant":
            if message.tool_calls:
                calls = "\n".join(
                    json.dumps(
                        {"name": tc.function.name, "arguments": tc.function.arguments},
                        ensure_ascii=False,
                    )
                    for tc in message.tool_calls
                )
                blocks.append(f"[assistant tool_call]\n{calls}")
            if text:
                blocks.append(f"[assistant]\n{text}")
        elif role in ("tool", "function"):
            label = message.name or message.tool_call_id or "tool"
            blocks.append(f"[tool_result {label}]\n{text}")

    prompt = "\n\n".join(blocks)
    if system_parts:
        prompt = "[system]\n" + "\n\n".join(system_parts) + "\n\n" + prompt
    return prompt.strip()


def _parse_model_list(payload: Any) -> list[ProviderModelInfo]:
    entries: list[Any] = []
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict) and isinstance(data.get("models"), list):
            entries = data["models"]
    elif isinstance(payload, list):
        entries = payload

    models: list[ProviderModelInfo] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id") or entry.get("model") or entry.get("name")
        if not model_id:
            continue
        info = entry.get("info") if isinstance(entry.get("info"), dict) else {}
        meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
        capabilities = (
            meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
        )
        context = meta.get("max_context_length") or entry.get("context_window")
        models.append(
            ProviderModelInfo(
                id=str(model_id),
                display_name=str(entry.get("name") or model_id),
                context_window=int(context) if isinstance(context, (int, float)) else None,
                supports_tools=bool(capabilities.get("function_call", True)),
                supports_reasoning=bool(capabilities.get("thinking", False)),
            )
        )
    return models
