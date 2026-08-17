"""Admin API.

Every route below (except ``/login`` and ``/session``) requires an
authenticated admin session. No endpoint here ever returns a Qwen credential or
a full client API key.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ApiKeyUpdate,
    CredentialCreate,
    CredentialOut,
    CredentialTestResult,
    CredentialUpdate,
    LogEntryOut,
    LoginRequest,
    LoginResponse,
    ModelOut,
    ModelUpsert,
    OperationResult,
    PaginatedLogs,
    RequestLogOut,
    SettingsOut,
    SettingsUpdate,
)
from app.auth.admin import (
    AdminIdentity,
    admin_configured,
    clear_session,
    current_admin_optional,
    issue_session,
    require_admin,
    verify_admin_credentials,
)
from app.database import get_session
from app.gateway import errors
from app.gateway.scheduler import get_scheduler
from app.models.db import RequestLog
from app.providers.registry import available_providers
from app.services import (
    api_key_service,
    credential_service,
    metrics,
    model_service,
    settings_service,
)
from app.utils.logging import get_logger, get_recent_logs

log = get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["Admin API"])


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------
@router.post("/login", response_model=LoginResponse, summary="Admin login")
async def login(payload: LoginRequest, response: Response, request: Request) -> LoginResponse:
    if not admin_configured():
        raise errors.GatewayError(
            message=(
                "Admin access is not configured. Set ADMIN_USERNAME and "
                "ADMIN_PASSWORD, then restart the gateway."
            ),
            category=errors.ErrorCategory.PERMISSION,
            code="admin_not_configured",
            status_code=503,
        )
    if not verify_admin_credentials(payload.username, payload.password):
        client = request.client.host if request.client else "unknown"
        log.warning("admin_login_failed", client=client)
        raise errors.unauthorized("Invalid administrator credentials.")
    issue_session(response, payload.username)
    log.info("admin_login_succeeded", username=payload.username)
    return LoginResponse(username=payload.username)


@router.post("/logout", response_model=OperationResult, summary="Admin logout")
async def logout(response: Response) -> OperationResult:
    clear_session(response)
    return OperationResult(detail="signed out")


@router.get("/session", summary="Current admin session")
async def session_info(request: Request) -> dict[str, Any]:
    identity = current_admin_optional(request)
    return {
        "authenticated": identity is not None,
        "username": identity.username if identity else None,
        "admin_configured": admin_configured(),
    }


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------
@router.get("/overview", summary="Dashboard data")
async def overview(
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stats = await metrics.gather_stats(session)
    scheduler_stats = await get_scheduler().stats(session)
    stats["scheduler"] = {
        "strategy": settings_service.public_settings_snapshot()["scheduler_strategy"],
        "in_flight": scheduler_stats.in_flight,
    }
    stats["providers"] = available_providers()
    return stats


# --------------------------------------------------------------------------
# Credentials (tokens)
# --------------------------------------------------------------------------
@router.get("/credentials", response_model=list[CredentialOut], summary="List credentials")
async def list_credentials(
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[CredentialOut]:
    scheduler = get_scheduler()
    rows = await credential_service.list_credentials(session)
    out = []
    for row in rows:
        item = CredentialOut.model_validate(row)
        item.in_flight = scheduler.in_flight(row.id)
        out.append(item)
    return out


@router.post(
    "/credentials",
    response_model=CredentialOut,
    status_code=201,
    summary="Add a Qwen credential",
    description=(
        "Stores a credential the operator is authorized to use, encrypted at "
        "rest. The secret is never returned by any endpoint afterwards."
    ),
)
async def create_credential(
    payload: CredentialCreate,
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> CredentialOut:
    credential = await credential_service.create_credential(
        session,
        name=payload.name,
        secret=payload.secret,
        refresh_secret=payload.refresh_secret,
        auth_mode=payload.auth_mode,
        base_url=payload.base_url,
        provider=payload.provider,
        enabled=payload.enabled,
    )
    return CredentialOut.model_validate(credential)


@router.patch(
    "/credentials/{credential_id}",
    response_model=CredentialOut,
    summary="Update a credential",
)
async def update_credential(
    credential_id: int,
    payload: CredentialUpdate,
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> CredentialOut:
    credential = await credential_service.update_credential(
        session,
        credential_id,
        name=payload.name,
        enabled=payload.enabled,
        secret=payload.secret,
        refresh_secret=payload.refresh_secret,
        base_url=payload.base_url,
        auth_mode=payload.auth_mode,
        clear_cooldown=payload.clear_cooldown,
    )
    return CredentialOut.model_validate(credential)


@router.delete(
    "/credentials/{credential_id}",
    response_model=OperationResult,
    summary="Delete a credential",
)
async def delete_credential(
    credential_id: int,
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> OperationResult:
    await credential_service.delete_credential(session, credential_id)
    return OperationResult(detail=f"credential {credential_id} deleted")


@router.post(
    "/credentials/{credential_id}/test",
    response_model=CredentialTestResult,
    summary="Test a credential against the provider",
)
async def test_credential(
    credential_id: int,
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> CredentialTestResult:
    result = await credential_service.test_credential(session, credential_id)
    return CredentialTestResult(**result)


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------
@router.get("/api-keys", response_model=list[ApiKeyOut], summary="List API keys")
async def list_api_keys(
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ApiKeyOut]:
    rows = await api_key_service.list_api_keys(session)
    return [ApiKeyOut.model_validate(row) for row in rows]


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=201,
    summary="Create an API key",
    description="The plaintext key is returned exactly once; only a hash is stored.",
)
async def create_api_key(
    payload: ApiKeyCreate,
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreated:
    record, plaintext = await api_key_service.create_api_key(
        session,
        name=payload.name,
        description=payload.description,
        expires_in_days=payload.expires_in_days,
    )
    return ApiKeyCreated(**ApiKeyOut.model_validate(record).model_dump(), api_key=plaintext)


@router.patch("/api-keys/{key_id}", response_model=ApiKeyOut, summary="Update an API key")
async def update_api_key(
    key_id: int,
    payload: ApiKeyUpdate,
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyOut:
    record = await api_key_service.update_api_key(
        session,
        key_id,
        name=payload.name,
        description=payload.description,
        enabled=payload.enabled,
    )
    return ApiKeyOut.model_validate(record)


@router.post("/api-keys/{key_id}/revoke", response_model=ApiKeyOut, summary="Revoke an API key")
async def revoke_api_key(
    key_id: int,
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyOut:
    record = await api_key_service.revoke_api_key(session, key_id)
    return ApiKeyOut.model_validate(record)


@router.delete("/api-keys/{key_id}", response_model=OperationResult, summary="Delete an API key")
async def delete_api_key(
    key_id: int,
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> OperationResult:
    await api_key_service.delete_api_key(session, key_id)
    return OperationResult(detail=f"api key {key_id} deleted")


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
@router.get("/models", response_model=list[ModelOut], summary="List configured models")
async def list_models(
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ModelOut]:
    rows = await model_service.list_models(session)
    return [_model_out(row) for row in rows]


@router.post("/models", response_model=ModelOut, summary="Create or update a model")
async def upsert_model(
    payload: ModelUpsert,
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ModelOut:
    row = await model_service.upsert_model(
        session,
        model_id=payload.model_id,
        provider=payload.provider,
        display_name=payload.display_name,
        aliases=payload.aliases,
        enabled=payload.enabled,
        context_window=payload.context_window,
        supports_tools=payload.supports_tools,
        supports_reasoning=payload.supports_reasoning,
    )
    return _model_out(row)


@router.post("/models/discover", response_model=list[ModelOut], summary="Discover models upstream")
async def discover_models(
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ModelOut]:
    rows = await model_service.discover_models(session)
    return [_model_out(row) for row in rows]


@router.delete("/models/{model_pk}", response_model=OperationResult, summary="Delete a model")
async def delete_model(
    model_pk: int,
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> OperationResult:
    removed = await model_service.delete_model(session, model_pk)
    if not removed:
        raise errors.not_found(f"Model {model_pk} not found.")
    return OperationResult(detail=f"model {model_pk} deleted")


# --------------------------------------------------------------------------
# Requests & logs
# --------------------------------------------------------------------------
@router.get("/requests", response_model=PaginatedLogs, summary="Search request history")
async def list_requests(
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status: str | None = Query(None, pattern="^(success|error|pending)$"),
    model: str | None = Query(None, max_length=128),
    search: str | None = Query(None, max_length=128),
) -> PaginatedLogs:
    stmt = select(RequestLog)
    count_stmt = select(func.count(RequestLog.id))
    if status:
        stmt = stmt.where(RequestLog.status == status)
        count_stmt = count_stmt.where(RequestLog.status == status)
    if model:
        stmt = stmt.where(RequestLog.model == model)
        count_stmt = count_stmt.where(RequestLog.model == model)
    if search:
        pattern = f"%{search}%"
        condition = (
            RequestLog.request_id.like(pattern)
            | RequestLog.model.like(pattern)
            | RequestLog.error_message.like(pattern)
            | RequestLog.api_key_name.like(pattern)
        )
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = int(await session.scalar(count_stmt) or 0)
    rows = list(
        (
            await session.execute(
                stmt.order_by(RequestLog.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
    )
    return PaginatedLogs(
        items=[RequestLogOut.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/requests/purge", response_model=OperationResult, summary="Purge old request logs")
async def purge_requests(
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> OperationResult:
    removed = await metrics.purge_old_logs(session)
    return OperationResult(detail=f"{removed} request logs removed")


@router.get("/logs", response_model=list[LogEntryOut], summary="Recent structured logs")
async def recent_logs(
    _: AdminIdentity = Depends(require_admin),
    limit: int = Query(200, ge=1, le=1000),
    level: str | None = Query(None, pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$"),
) -> list[LogEntryOut]:
    entries = get_recent_logs(limit=limit, level=level)
    out: list[LogEntryOut] = []
    for entry in entries:
        extra = {
            k: v
            for k, v in entry.items()
            if k not in {"ts", "level", "event", "request_id", "logger"}
        }
        out.append(
            LogEntryOut(
                ts=str(entry.get("ts", "")),
                level=str(entry.get("level", "INFO")),
                event=str(entry.get("event", "")),
                request_id=entry.get("request_id"),
                extra=extra,
            )
        )
    return out


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
@router.get("/settings", response_model=SettingsOut, summary="Effective settings")
async def read_settings(
    _: AdminIdentity = Depends(require_admin),
) -> SettingsOut:
    return SettingsOut(**settings_service.public_settings_snapshot())


@router.patch("/settings", response_model=SettingsOut, summary="Update settings")
async def update_settings(
    payload: SettingsUpdate,
    _: AdminIdentity = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SettingsOut:
    await settings_service.update_settings(session, payload.model_dump(exclude_none=True))
    return SettingsOut(**settings_service.public_settings_snapshot())


def _model_out(row) -> ModelOut:
    return ModelOut(
        id=row.id,
        provider=row.provider,
        model_id=row.model_id,
        display_name=row.display_name,
        aliases=[a for a in (row.aliases or "").split(",") if a],
        enabled=row.enabled,
        context_window=row.context_window,
        supports_tools=row.supports_tools,
        supports_reasoning=row.supports_reasoning,
        discovered_at=row.discovered_at,
    )
