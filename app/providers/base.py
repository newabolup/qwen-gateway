"""Provider interface.

A provider is anything that can authenticate with a credential, list models and
produce normalized events for a chat request. The gateway core depends only on
this interface, never on a concrete upstream protocol.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.api.schemas import ChatCompletionRequest
from app.gateway.errors import GatewayError
from app.providers.events import NormalizedEvent


@dataclass(slots=True)
class ProviderCredential:
    """A decrypted credential handed to a provider for a single operation.

    Instances are short-lived and must never be logged or persisted.
    """

    id: int
    name: str
    secret: str = field(repr=False)
    auth_mode: str = "portal"
    refresh_secret: str | None = field(default=None, repr=False)
    base_url: str | None = None
    expires_at: float | None = None

    def __str__(self) -> str:  # pragma: no cover - defensive
        return f"ProviderCredential(id={self.id}, name={self.name!r})"


@dataclass(slots=True)
class ProviderModelInfo:
    id: str
    display_name: str | None = None
    context_window: int | None = None
    supports_tools: bool = True
    supports_reasoning: bool = False
    aliases: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AuthResult:
    ok: bool
    detail: str | None = None
    #: Refreshed credential material the caller should persist, if any.
    rotated_secret: str | None = field(default=None, repr=False)
    rotated_refresh_secret: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    base_url: str | None = None


@dataclass(slots=True)
class HealthResult:
    healthy: bool
    detail: str | None = None
    latency_ms: float | None = None


@dataclass(slots=True)
class ProviderRequest:
    """Everything a provider needs to serve one completion."""

    request: ChatCompletionRequest
    upstream_model: str
    credential: ProviderCredential
    request_id: str
    expose_reasoning: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class Provider(abc.ABC):
    """Abstract upstream provider."""

    name: str = "base"

    @abc.abstractmethod
    async def authenticate(self, credential: ProviderCredential) -> AuthResult:
        """Validate (and if supported refresh) a credential."""

    @abc.abstractmethod
    async def list_models(
        self, credential: ProviderCredential | None = None
    ) -> list[ProviderModelInfo]:
        """Discover models available upstream."""

    @abc.abstractmethod
    async def create_completion(self, req: ProviderRequest) -> list[NormalizedEvent]:
        """Non-streaming completion, returned as normalized events."""

    @abc.abstractmethod
    def stream_completion(self, req: ProviderRequest) -> AsyncIterator[NormalizedEvent]:
        """Streaming completion, yielding normalized events incrementally."""

    @abc.abstractmethod
    async def health_check(self, credential: ProviderCredential | None = None) -> HealthResult:
        """Lightweight upstream reachability probe."""

    def normalize_error(self, exc: BaseException) -> GatewayError:
        """Map a provider-specific failure onto a normalized gateway error."""
        from app.gateway import errors

        return errors.from_exception(exc)

    async def aclose(self) -> None:
        """Release provider resources (HTTP clients, etc.)."""
        return None
