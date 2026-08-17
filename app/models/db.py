"""SQLAlchemy ORM entities."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for all gateway tables."""


class QwenCredential(Base):
    """A user-provided upstream credential (Qwen session token / OAuth token)."""

    __tablename__ = "qwen_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="qwen", nullable=False)
    #: "portal" (OAuth bearer, portal.qwen.ai) or "web" (chat.qwen.ai session cookie)
    auth_mode: Mapped[str] = mapped_column(String(16), default="portal", nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    #: Optional encrypted OAuth refresh token.
    encrypted_refresh_secret: Mapped[str | None] = mapped_column(Text, default=None)
    #: Per-credential upstream base URL override (e.g. resource_url from OAuth).
    base_url: Mapped[str | None] = mapped_column(String(255), default=None)
    secret_hint: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    status: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    weight: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (Index("ix_credentials_enabled", "enabled", "provider"),)


class ApiKey(Base):
    """A gateway client API key (``qwg_...``). Only a keyed hash is stored."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), default=None)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    key_preview: Mapped[str] = mapped_column(String(64), nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    logs: Mapped[list[RequestLog]] = relationship(back_populates="api_key")

    __table_args__ = (Index("ix_api_keys_hash", "key_hash"),)


class RequestLog(Base):
    """One inbound gateway request. Never contains secrets."""

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    api_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), default=None
    )
    api_key_name: Mapped[str | None] = mapped_column(String(128), default=None)
    credential_id: Mapped[int | None] = mapped_column(Integer, default=None)
    credential_name: Mapped[str | None] = mapped_column(String(128), default=None)

    provider: Mapped[str] = mapped_column(String(32), default="qwen", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    upstream_model: Mapped[str | None] = mapped_column(String(128), default=None)
    endpoint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    streaming: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, default=None)
    error_category: Mapped[str | None] = mapped_column(String(64), default=None)
    error_message: Mapped[str | None] = mapped_column(String(512), default=None)

    latency_ms: Mapped[float | None] = mapped_column(Float, default=None)
    first_token_ms: Mapped[float | None] = mapped_column(Float, default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    total_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    #: Only populated when STORE_REQUEST_BODIES=true; redacted before storage.
    request_preview: Mapped[str | None] = mapped_column(Text, default=None)

    api_key: Mapped[ApiKey | None] = relationship(back_populates="logs")

    __table_args__ = (Index("ix_request_logs_created_status", "created_at", "status"),)


class ProviderModel(Base):
    """A model exposed by the gateway (discovered upstream or configured)."""

    __tablename__ = "provider_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="qwen", nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), default=None)
    aliases: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    context_window: Mapped[int | None] = mapped_column(Integer, default=None)
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_reasoning: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("provider", "model_id", name="uq_provider_model"),)


class GatewaySetting(Base):
    """Persistent key/value settings editable from the admin UI."""

    __tablename__ = "gateway_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
