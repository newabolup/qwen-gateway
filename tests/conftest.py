"""Shared test fixtures.

The suite never requires a real Qwen credential: everything runs against the
mock provider or against directly-constructed parser inputs.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

TEST_SECRET_KEY = "test-secret-key-do-not-use-in-production"
TEST_ADMIN_USER = "admin"
TEST_ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture(scope="session", autouse=True)
def _test_environment() -> Iterator[Path]:
    """Isolate the test run from any developer .env / data directory."""
    tmpdir = Path(tempfile.mkdtemp(prefix="qwen-gateway-tests-"))
    os.environ.update(
        {
            "APP_ENV": "test",
            "GATEWAY_SECRET_KEY": TEST_SECRET_KEY,
            "ADMIN_USERNAME": TEST_ADMIN_USER,
            "ADMIN_PASSWORD": TEST_ADMIN_PASSWORD,
            "DEFAULT_PROVIDER": "mock",
            "ENABLE_MOCK_PROVIDER": "true",
            "DEFAULT_MODEL": "mock-qwen",
            "MODEL_ALIASES": "qwen=mock-qwen,qwen-default=mock-qwen",
            "LOG_LEVEL": "WARNING",
            "REQUEST_LOG_RETENTION_DAYS": "7",
            "EXPOSE_REASONING": "false",
            "ADMIN_RATE_LIMIT_PER_MINUTE": "0",
            "PUBLIC_RATE_LIMIT_PER_MINUTE": "0",
            "MAX_FAILOVER_ATTEMPTS": "3",
        }
    )
    os.environ.pop("DATABASE_URL", None)
    yield tmpdir


@pytest_asyncio.fixture
async def app_context(_test_environment: Path, tmp_path: Path) -> AsyncIterator[dict]:
    """A fully initialised app with a fresh database per test."""
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"

    from app import config, database
    from app.gateway import router as router_module
    from app.gateway.scheduler import get_scheduler
    from app.providers import registry

    config.reload_settings()
    await database.dispose_db()
    registry.reset_registry()
    router_module.reset_router()
    get_scheduler().reset()

    from app.main import create_app

    application = create_app()

    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        # Trigger lifespan manually so startup/shutdown logic is exercised.
        async with application.router.lifespan_context(application):
            yield {"app": application, "client": client}

    await database.dispose_db()
    registry.reset_registry()
    get_scheduler().reset()


@pytest_asyncio.fixture
async def client(app_context: dict):
    return app_context["client"]


@pytest_asyncio.fixture
async def db_session(app_context: dict) -> AsyncIterator:
    from app.database import session_scope

    async with session_scope() as session:
        yield session


@pytest_asyncio.fixture
async def api_key(app_context: dict) -> str:
    """A working client API key."""
    from app.database import session_scope
    from app.services.api_key_service import create_api_key

    async with session_scope() as session:
        _, plaintext = await create_api_key(session, name="test-key")
    return plaintext


@pytest_asyncio.fixture
async def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


@pytest_asyncio.fixture
async def mock_credential(app_context: dict):
    """One healthy mock credential."""
    from app.database import session_scope
    from app.services.credential_service import create_credential

    async with session_scope() as session:
        credential = await create_credential(
            session, name="mock-1", secret="mock:normal", provider="mock", auth_mode="portal"
        )
    return credential


@pytest_asyncio.fixture
async def admin_client(app_context: dict):
    """An HTTP client with an authenticated admin session."""
    client = app_context["client"]
    response = await client.post(
        "/api/admin/login",
        json={"username": TEST_ADMIN_USER, "password": TEST_ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return client


def make_credential(secret: str = "mock:normal", credential_id: int = 1):
    from app.providers.base import ProviderCredential

    return ProviderCredential(id=credential_id, name=f"cred-{credential_id}", secret=secret)


def make_provider_request(
    messages: list[dict] | None = None,
    *,
    model: str = "mock-qwen",
    secret: str = "mock:normal",
    tools: list[dict] | None = None,
):
    from app.api.schemas import ChatCompletionRequest
    from app.providers.base import ProviderRequest

    request = ChatCompletionRequest(
        model=model,
        messages=messages or [{"role": "user", "content": "hi"}],
        tools=tools,
    )
    return ProviderRequest(
        request=request,
        upstream_model=model,
        credential=make_credential(secret),
        request_id="gwreq_test",
    )
