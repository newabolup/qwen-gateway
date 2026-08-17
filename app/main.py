"""FastAPI application factory and entry point."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.api import admin as admin_api
from app.api import public as public_api
from app.api.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
)
from app.config import REPO_ROOT, get_settings
from app.database import dispose_db, init_db, session_scope
from app.gateway import errors
from app.gateway.errors import GatewayError
from app.providers.registry import shutdown_providers
from app.services import metrics, model_service, settings_service
from app.utils.logging import configure_logging, get_logger

log = get_logger(__name__)

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
STATIC_FALLBACK = REPO_ROOT / "app" / "static"

DESCRIPTION = """
An OpenAI-compatible gateway in front of Qwen.

* **Public API** — `/v1/models`, `/v1/chat/completions` (API-key authenticated)
* **Admin API** — `/api/admin/*` (session authenticated)
* **Health** — `/health`, `/api/health`, `/api/stats`

Clients authenticate with a gateway key (`qwg_...`) and never see the
underlying Qwen credential.
"""


async def _retention_loop() -> None:
    """Background task enforcing request-log retention."""
    while True:
        try:
            await asyncio.sleep(3600)
            async with session_scope() as session:
                await metrics.purge_old_logs(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("retention_task_error", detail=str(exc))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    log.info(
        "gateway_starting",
        env=settings.app_env,
        provider=settings.default_provider,
        database="postgresql" if not settings.is_sqlite else "sqlite",
    )

    await init_db()
    async with session_scope() as session:
        await settings_service.apply_overrides(session)
        await model_service.ensure_seed_models(session)

    if not settings.gateway_secret_key and settings.is_production:
        raise RuntimeError("GATEWAY_SECRET_KEY is required in production")
    if not settings.admin_password:
        log.warning(
            "admin_not_configured",
            detail="ADMIN_PASSWORD is unset; the admin API/UI is disabled",
        )

    task = asyncio.create_task(_retention_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await shutdown_providers()
        await dispose_db()
        log.info("gateway_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=settings.cors_origin_list != ["*"],
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Gateway-Request-Id"],
    )

    app.include_router(public_api.router)
    app.include_router(public_api.health_router)
    app.include_router(admin_api.router)

    _register_exception_handlers(app)
    _mount_frontend(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
        headers = {}
        if exc.retry_after:
            headers["Retry-After"] = str(exc.retry_after)
        log.warning("request_rejected", path=request.url.path, **exc.log_fields())
        return JSONResponse(
            status_code=exc.status_code, content=exc.to_public_dict(), headers=headers
        )

    @app.exception_handler(ValidationError)
    async def _validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
        error = errors.invalid_request("Request validation failed.")
        return JSONResponse(status_code=400, content=error.to_public_dict())

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # Stack traces go to the (redacting) logger, never to the client.
        log.exception("unhandled_exception", path=request.url.path)
        error = errors.internal_error(f"{type(exc).__name__}")
        return JSONResponse(status_code=500, content=error.to_public_dict())


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built admin UI when present, else a helpful placeholder."""
    dist = FRONTEND_DIST if (FRONTEND_DIST / "index.html").exists() else None
    if dist is not None:
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/", include_in_schema=False)
        async def _index() -> FileResponse:
            return FileResponse(dist / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa(full_path: str) -> FileResponse:
            candidate = dist / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

        return

    fallback = STATIC_FALLBACK / "index.html"

    @app.get("/", include_in_schema=False, response_model=None)
    async def _placeholder() -> FileResponse | JSONResponse:
        if fallback.exists():
            return FileResponse(fallback)
        return JSONResponse(
            {
                "service": get_settings().app_name,
                "docs": "/docs",
                "health": "/health",
                "note": "Admin UI not built. Run: npm --prefix frontend install && npm --prefix frontend run build",
            }
        )


app = create_app()


def run() -> None:
    """Console entry point (``python -m app``)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=False,
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover
    run()
