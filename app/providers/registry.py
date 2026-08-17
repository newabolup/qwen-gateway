"""Provider registry.

Keeps one long-lived provider instance per name so HTTP connection pools are
reused. Adding a new upstream means adding a factory here plus a package under
``app/providers/``.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import get_settings
from app.providers.base import Provider

_FACTORIES: dict[str, Callable[[], Provider]] = {}
_INSTANCES: dict[str, Provider] = {}


def register(name: str, factory: Callable[[], Provider]) -> None:
    _FACTORIES[name] = factory


def _bootstrap() -> None:
    if _FACTORIES:
        return
    from app.providers.mock.client import MockProvider
    from app.providers.qwen.client import QwenProvider

    register("qwen", QwenProvider)
    register("mock", MockProvider)


def get_provider(name: str | None = None) -> Provider:
    _bootstrap()
    settings = get_settings()
    name = name or settings.default_provider
    if name == "mock" and not settings.enable_mock_provider:
        raise KeyError("mock provider is disabled (ENABLE_MOCK_PROVIDER=false)")
    if name not in _FACTORIES:
        raise KeyError(f"unknown provider {name!r}")
    if name not in _INSTANCES:
        _INSTANCES[name] = _FACTORIES[name]()
    return _INSTANCES[name]


def available_providers() -> list[str]:
    _bootstrap()
    settings = get_settings()
    return [name for name in _FACTORIES if name != "mock" or settings.enable_mock_provider]


async def shutdown_providers() -> None:
    for provider in list(_INSTANCES.values()):
        await provider.aclose()
    _INSTANCES.clear()


def reset_registry() -> None:
    """Test hook."""
    _INSTANCES.clear()
