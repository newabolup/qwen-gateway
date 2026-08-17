"""Chat-completion orchestration.

One place that owns the full request lifecycle:

    validate -> route model -> acquire credential -> call provider
             -> normalize events -> format response -> record metrics

Failover: a retryable failure that happens *before any bytes were produced* is
retried with a different credential. Once output has been emitted the stream is
terminated cleanly with an error frame instead (silently switching credentials
mid-answer would corrupt the response).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from app.config import get_settings
from app.gateway import errors
from app.gateway.errors import ErrorCategory, GatewayError
from app.gateway.normalizer import StreamFormatter
from app.gateway.router import get_router
from app.gateway.scheduler import get_scheduler
from app.providers.base import Provider, ProviderCredential, ProviderRequest
from app.providers.events import EventType, NormalizedEvent
from app.providers.registry import get_provider
from app.security.crypto import encrypt_secret
from app.services.metrics import RequestRecorder
from app.utils.ids import completion_id as new_completion_id
from app.utils.logging import get_logger

log = get_logger(__name__)


class CompletionService:
    """Executes chat completions with scheduling, failover and normalization."""

    def __init__(self, session: AsyncSession, recorder: RequestRecorder) -> None:
        self.session = session
        self.recorder = recorder
        self.settings = get_settings()
        self.scheduler = get_scheduler()
        self.router = get_router()

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------
    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        decision = await self.router.resolve(self.session, request.model)
        self.recorder.set_model(request.model, decision.upstream_model, decision.provider)
        provider = self._provider(decision.provider)

        attempts = 0
        tried: list[int] = []
        last_error: GatewayError | None = None
        max_attempts = max(1, self.settings.max_failover_attempts)

        while attempts < max_attempts:
            attempts += 1
            try:
                credential = await self._acquire(provider, tried, decision.provider)
            except GatewayError as exc:
                # The pool is exhausted. Surface the last upstream failure
                # rather than a generic "no credentials" if we have one.
                raise (last_error or exc) from None
            tried.append(credential.id)
            self.recorder.set_credential(credential.id, credential.name)
            self.recorder.set_attempts(attempts)

            started = time.perf_counter()
            try:
                events = await provider.create_completion(
                    self._provider_request(request, decision.upstream_model, credential)
                )
                result = await self._finish_success(
                    provider, credential, events, request, decision.upstream_model, started
                )
                return result
            except GatewayError as exc:
                last_error = exc
                await self._handle_failure(credential, exc)
                if not (exc.retryable and attempts < max_attempts):
                    raise
                log.warning(
                    "failover_retry",
                    attempt=attempts,
                    credential_id=credential.id,
                    **exc.log_fields(),
                )
            except Exception as exc:
                normalized = provider.normalize_error(exc)
                last_error = normalized
                await self._handle_failure(credential, normalized)
                if not (normalized.retryable and attempts < max_attempts):
                    raise normalized from exc
            finally:
                await self.scheduler.release(credential.id)

        raise last_error or errors.upstream_unavailable("all failover attempts exhausted")

    async def _finish_success(
        self,
        provider: Provider,
        credential: ProviderCredential,
        events: list[NormalizedEvent],
        request: ChatCompletionRequest,
        upstream_model: str,
        started: float,
    ) -> ChatCompletionResponse:
        from app.gateway.normalizer import ResponseAggregator

        aggregator = ResponseAggregator(expose_reasoning=self._expose_reasoning(request))
        aggregator.add_all(events)
        result = aggregator.result

        if result.error:
            raise _error_event_to_gateway_error(result.error)

        if not result.content and not result.tool_calls:
            # Upstream produced only metadata/UI events: that is a normalization
            # failure, not a valid empty answer.
            raise errors.parse_error(
                f"upstream produced no assistant content (warnings={result.warnings[:5]})"
            )

        await self.scheduler.mark_success(self.session, credential.id)
        latency = (time.perf_counter() - started) * 1000
        response = aggregator.to_response(request.model, new_completion_id())
        self.recorder.set_success(
            latency_ms=latency,
            usage=response.usage,
            warnings=result.warnings,
        )
        log.info(
            "request_completed",
            credential_id=credential.id,
            model=request.model,
            upstream_model=upstream_model,
            latency_ms=round(latency, 2),
            streaming=False,
            warnings=result.warnings[:5] or None,
        )
        return response

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------
    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]:
        decision = await self.router.resolve(self.session, request.model)
        self.recorder.set_model(request.model, decision.upstream_model, decision.provider)
        self.recorder.set_streaming(True)
        provider = self._provider(decision.provider)

        include_usage = bool(request.stream_options and request.stream_options.include_usage)
        expose_reasoning = self._expose_reasoning(request)

        attempts = 0
        tried: list[int] = []
        max_attempts = max(1, self.settings.max_failover_attempts)
        last_error: GatewayError | None = None

        while attempts < max_attempts:
            attempts += 1
            try:
                credential = await self._acquire(provider, tried, decision.provider)
            except GatewayError as exc:
                raise (last_error or exc) from None
            tried.append(credential.id)
            self.recorder.set_credential(credential.id, credential.name)
            self.recorder.set_attempts(attempts)

            formatter = StreamFormatter(
                completion_id=new_completion_id(),
                model=request.model,
                expose_reasoning=expose_reasoning,
                include_usage=include_usage,
            )
            started = time.perf_counter()
            emitted_any = False

            try:
                log.info(
                    "stream_started",
                    credential_id=credential.id,
                    model=request.model,
                    upstream_model=decision.upstream_model,
                    attempt=attempts,
                )
                iterator = provider.stream_completion(
                    self._provider_request(request, decision.upstream_model, credential)
                )
                async for event in iterator:
                    if event.type is EventType.ERROR:
                        raise _error_event_to_gateway_error(event.data)
                    for chunk in formatter.handle(event):
                        if not emitted_any:
                            emitted_any = True
                            self.recorder.set_first_token((time.perf_counter() - started) * 1000)
                            yield formatter.opening_chunk()
                        yield chunk

                if not emitted_any:
                    raise errors.parse_error("upstream stream produced no content")

                if not formatter.finished:
                    yield formatter.final_chunk()
                usage_chunk = formatter.usage_chunk()
                if usage_chunk is not None:
                    yield usage_chunk

                await self.scheduler.mark_success(self.session, credential.id)
                latency = (time.perf_counter() - started) * 1000
                self.recorder.set_success(
                    latency_ms=latency,
                    usage=formatter.aggregator.result.usage,
                    warnings=formatter.aggregator.result.warnings,
                )
                log.info(
                    "request_completed",
                    credential_id=credential.id,
                    model=request.model,
                    latency_ms=round(latency, 2),
                    streaming=True,
                )
                return

            except GatewayError as exc:
                await self._handle_failure(credential, exc)
                last_error = exc
                if emitted_any or not exc.retryable or attempts >= max_attempts:
                    raise
                log.warning(
                    "failover_retry",
                    attempt=attempts,
                    credential_id=credential.id,
                    **exc.log_fields(),
                )
            except Exception as exc:
                normalized = provider.normalize_error(exc)
                await self._handle_failure(credential, normalized)
                last_error = normalized
                if emitted_any or not normalized.retryable or attempts >= max_attempts:
                    raise normalized from exc
            finally:
                await self.scheduler.release(credential.id)

        raise last_error or errors.upstream_unavailable("all failover attempts exhausted")

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    def _provider(self, name: str) -> Provider:
        try:
            return get_provider(name)
        except KeyError as exc:
            raise errors.invalid_request(f"Unknown provider {name!r}.", param="model") from exc

    def _expose_reasoning(self, request: ChatCompletionRequest) -> bool:
        """Reasoning is only ever exposed when the operator opts in."""
        return self.settings.expose_reasoning

    def _provider_request(
        self,
        request: ChatCompletionRequest,
        upstream_model: str,
        credential: ProviderCredential,
    ) -> ProviderRequest:
        return ProviderRequest(
            request=request,
            upstream_model=upstream_model,
            credential=credential,
            request_id=self.recorder.request_id,
            expose_reasoning=self._expose_reasoning(request),
        )

    async def _acquire(
        self, provider: Provider, tried: list[int], provider_name: str
    ) -> ProviderCredential:
        """Acquire a credential that passes pre-flight authentication.

        Credentials rejected at authentication are skipped (and marked) rather
        than failing the request, but the reason is preserved so the caller can
        report something better than "no credentials" if the pool runs out.
        """
        auth_error: GatewayError | None = None
        # Bounded loop: every iteration adds to `tried`, so it terminates.
        while True:
            try:
                credential = await self.scheduler.acquire(
                    self.session, provider=provider_name, exclude_ids=tried
                )
            except GatewayError as exc:
                raise (auth_error or exc) from None

            auth = await provider.authenticate(credential)
            if not auth.ok:
                await self.scheduler.mark_failure(
                    self.session,
                    credential.id,
                    category=ErrorCategory.AUTHENTICATION,
                    detail=auth.detail or "authentication failed",
                )
                await self.scheduler.release(credential.id)
                tried.append(credential.id)
                auth_error = errors.invalid_credential(
                    auth.detail or "credential rejected during pre-flight authentication"
                )
                self.recorder.set_error(auth_error)
                log.warning(
                    "credential_auth_failed",
                    credential_id=credential.id,
                    detail=auth.detail,
                )
                continue

            if auth.rotated_secret or auth.rotated_refresh_secret or auth.base_url:
                await self.scheduler.persist_rotation(
                    self.session,
                    credential.id,
                    encrypted_secret=(
                        encrypt_secret(auth.rotated_secret) if auth.rotated_secret else None
                    ),
                    encrypted_refresh_secret=(
                        encrypt_secret(auth.rotated_refresh_secret)
                        if auth.rotated_refresh_secret
                        else None
                    ),
                    expires_at=(
                        datetime.fromtimestamp(auth.expires_at, tz=timezone.utc)
                        if auth.expires_at
                        else None
                    ),
                    base_url=auth.base_url,
                )
                if auth.rotated_secret:
                    credential.secret = auth.rotated_secret
                if auth.rotated_refresh_secret:
                    credential.refresh_secret = auth.rotated_refresh_secret
                if auth.base_url:
                    credential.base_url = auth.base_url
            return credential

    async def _handle_failure(self, credential: ProviderCredential, error: GatewayError) -> None:
        await self.scheduler.mark_failure(
            self.session,
            credential.id,
            category=error.category,
            detail=error.internal_detail or error.message,
            retry_after=error.retry_after,
        )
        self.recorder.set_error(error)
        log.error(
            "upstream_error",
            credential_id=credential.id,
            **error.log_fields(),
        )


def _error_event_to_gateway_error(data: dict[str, object]) -> GatewayError:
    """Map a normalized upstream ERROR event onto a gateway error."""
    code = str(data.get("code") or "").lower()
    message = str(data.get("message") or "upstream error")
    status = data.get("status")
    if isinstance(status, (int, float)):
        return errors.from_upstream_status(int(status), detail=message)
    if "ratelimit" in code or "rate_limit" in code or "too many" in message.lower():
        return errors.rate_limited(detail=message)
    if "unauthor" in code or ("token" in code and "expire" in code):
        return errors.invalid_credential(detail=message)
    if "forbidden" in code or "permission" in code:
        return errors.from_upstream_status(403, detail=message)
    return errors.upstream_unavailable(detail=message)
