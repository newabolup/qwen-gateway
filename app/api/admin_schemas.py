"""Admin API request/response models.

Response models deliberately contain no secret material: credentials are only
ever represented by a masked hint, and API keys by a preview.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer


def _as_utc_iso(value: datetime | None) -> str | None:
    """Serialize timestamps as explicit UTC.

    SQLite hands back naive datetimes; without a timezone marker a browser in
    any non-UTC locale would render them shifted. Everything the gateway
    stores is UTC, so annotate it as such on the way out.
    """
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


#: A datetime that always serializes as an explicit-UTC ISO 8601 string.
UtcDateTime = Annotated[datetime, PlainSerializer(_as_utc_iso, return_type=str | None)]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    ok: bool = True
    username: str


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    #: The user's own Qwen token. Never returned by any endpoint after saving.
    secret: str = Field(min_length=8, max_length=8192)
    refresh_secret: str | None = Field(default=None, max_length=8192)
    auth_mode: Literal["auto", "portal", "web"] = "auto"
    base_url: str | None = Field(default=None, max_length=255)
    provider: str = Field(default="qwen", max_length=32)
    enabled: bool = True


class CredentialUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    #: Optional secret rotation.
    secret: str | None = Field(default=None, min_length=8, max_length=8192)
    refresh_secret: str | None = Field(default=None, max_length=8192)
    base_url: str | None = Field(default=None, max_length=255)
    auth_mode: Literal["auto", "portal", "web"] | None = None
    clear_cooldown: bool = False


class CredentialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: str
    auth_mode: str
    secret_hint: str
    status: str
    status_reason: str | None
    enabled: bool
    base_url: str | None
    created_at: UtcDateTime
    expires_at: UtcDateTime | None
    last_used_at: UtcDateTime | None
    last_success_at: UtcDateTime | None
    last_error_at: UtcDateTime | None
    cooldown_until: UtcDateTime | None
    request_count: int
    success_count: int
    failure_count: int
    consecutive_failures: int
    in_flight: int = 0


class CredentialTestResult(BaseModel):
    id: int
    healthy: bool
    detail: str | None = None
    latency_ms: float | None = None
    models_discovered: int | None = None


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    key_preview: str
    enabled: bool
    revoked: bool
    created_at: UtcDateTime
    expires_at: UtcDateTime | None
    last_used_at: UtcDateTime | None
    request_count: int
    success_count: int
    failure_count: int


class ApiKeyCreated(ApiKeyOut):
    #: Returned exactly once, at creation time.
    api_key: str


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    model_id: str
    display_name: str | None
    aliases: list[str]
    enabled: bool
    context_window: int | None
    supports_tools: bool
    supports_reasoning: bool
    discovered_at: UtcDateTime


class ModelUpsert(BaseModel):
    model_id: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    aliases: list[str] = Field(default_factory=list)
    enabled: bool = True
    provider: str = Field(default="qwen", max_length=32)
    context_window: int | None = Field(default=None, ge=1)
    supports_tools: bool = True
    supports_reasoning: bool = False


class RequestLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    request_id: str
    created_at: UtcDateTime
    api_key_name: str | None
    credential_id: int | None
    credential_name: str | None
    provider: str
    model: str
    upstream_model: str | None
    endpoint: str
    streaming: bool
    status: str
    status_code: int | None
    error_category: str | None
    error_message: str | None
    latency_ms: float | None
    first_token_ms: float | None
    attempts: int
    total_tokens: int | None


class PaginatedLogs(BaseModel):
    items: list[RequestLogOut]
    total: int
    page: int
    page_size: int


class SettingsOut(BaseModel):
    app_env: str
    default_provider: str
    scheduler_strategy: str
    expose_reasoning: bool
    default_model: str
    model_aliases: dict[str, str]
    qwen_mode: str
    request_log_retention_days: int
    store_request_bodies: bool
    max_failover_attempts: int
    default_cooldown_seconds: int
    rate_limit_cooldown_seconds: int
    mock_provider_enabled: bool
    #: True when GATEWAY_SECRET_KEY was supplied explicitly.
    secret_key_configured: bool
    admin_configured: bool


class SettingsUpdate(BaseModel):
    scheduler_strategy: Literal["round_robin", "least_recently_used"] | None = None
    expose_reasoning: bool | None = None
    default_model: str | None = Field(default=None, max_length=128)
    default_provider: Literal["qwen", "mock"] | None = None
    request_log_retention_days: int | None = Field(default=None, ge=0, le=3650)
    store_request_bodies: bool | None = None


class LogEntryOut(BaseModel):
    ts: str
    level: str
    event: str
    request_id: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class OperationResult(BaseModel):
    ok: bool = True
    detail: str | None = None
