"""Application configuration.

All settings come from environment variables (optionally loaded from a .env
file). Nothing security relevant is hard-coded: secrets must be supplied by the
operator through the environment.
"""

from __future__ import annotations

import contextlib
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


class Settings(BaseSettings):
    """Runtime configuration for the gateway."""

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -----------------------------------------------------
    app_env: Literal["development", "production", "test"] = "development"
    app_name: str = "Qwen Token Gateway"
    host: str = "0.0.0.0"  # noqa: S104 - containers/PaaS require binding all interfaces
    port: int = 8787
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = False

    # --- Persistence -----------------------------------------------------
    database_url: str = f"sqlite+aiosqlite:///{(DATA_DIR / 'gateway.db').as_posix()}"
    request_log_retention_days: int = 14
    store_request_bodies: bool = False

    # --- Secrets ---------------------------------------------------------
    gateway_secret_key: str = ""
    admin_username: str = "admin"
    admin_password: str = ""
    session_ttl_seconds: int = 60 * 60 * 12

    # --- Upstream (Qwen) -------------------------------------------------
    qwen_mode: Literal["auto", "portal", "web"] = "auto"
    qwen_portal_base_url: str = "https://portal.qwen.ai/v1"
    qwen_web_base_url: str = "https://chat.qwen.ai"
    qwen_request_timeout: float = 120.0
    qwen_connect_timeout: float = 15.0
    qwen_max_retries: int = 2
    qwen_web_client_version: str = "0.2.81"
    qwen_oauth_client_id: str = "f0304373b74a44d2b584a3fb70ca9e56"
    qwen_oauth_base_url: str = "https://chat.qwen.ai"
    http_proxy_url: str | None = None

    # --- Provider selection ---------------------------------------------
    #: "qwen" is the production adapter; "mock" is offline/dev/testing only.
    default_provider: Literal["qwen", "mock"] = "qwen"
    enable_mock_provider: bool = True

    # --- Gateway behaviour ----------------------------------------------
    scheduler_strategy: Literal["round_robin", "least_recently_used"] = "round_robin"
    default_cooldown_seconds: int = 300
    rate_limit_cooldown_seconds: int = 900
    max_failover_attempts: int = 3
    expose_reasoning: bool = False
    reasoning_field: Literal["reasoning_content", "reasoning"] = "reasoning_content"
    max_request_bytes: int = 8 * 1024 * 1024
    stream_idle_timeout: float = 180.0

    # --- Security --------------------------------------------------------
    cors_allow_origins: str = "*"
    admin_rate_limit_per_minute: int = 120
    public_rate_limit_per_minute: int = 0  # 0 disables the public limiter
    trust_forwarded_for: bool = False

    # --- Models ----------------------------------------------------------
    default_model: str = "qwen3-max"
    model_aliases: str = (
        "qwen=qwen3-max,qwen-default=qwen3-max,"
        "qwen-coder=qwen3-coder-plus,qwen-thinking=qwen3-max-thinking"
    )

    @field_validator("database_url")
    @classmethod
    def _normalise_database_url(cls, value: str) -> str:
        """Accept the sync URLs people paste from Railway/Heroku and async-ify them."""
        if value.startswith("postgres://"):
            value = value.replace("postgres://", "postgresql://", 1)
        if value.startswith("postgresql://"):
            value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if value.startswith("sqlite://") and "+aiosqlite" not in value:
            value = value.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return value

    @field_validator("cors_allow_origins")
    @classmethod
    def _strip_origins(cls, value: str) -> str:
        return value.strip()

    # --- Derived helpers -------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_allow_origins in {"", "*"}:
            return ["*"]
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def alias_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for pair in self.model_aliases.split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            alias, target = pair.split("=", 1)
            alias, target = alias.strip(), target.strip()
            if alias and target:
                mapping[alias.lower()] = target
        return mapping

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def sqlite_path(self) -> Path | None:
        if not self.is_sqlite:
            return None
        raw = self.database_url.split("///", 1)[-1]
        return Path(raw)

    def resolved_secret_key(self) -> str:
        """Return the encryption key, generating an ephemeral one in dev.

        In production an explicit ``GATEWAY_SECRET_KEY`` is mandatory: without
        it, credentials encrypted in a previous run cannot be decrypted.
        """
        if self.gateway_secret_key:
            return self.gateway_secret_key
        if self.is_production:
            raise RuntimeError(
                "GATEWAY_SECRET_KEY must be set when APP_ENV=production. "
                "Generate one with: python -m app.cli generate-key"
            )
        key_file = DATA_DIR / ".dev_secret_key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        if key_file.exists():
            return key_file.read_text(encoding="utf-8").strip()
        generated = secrets.token_urlsafe(48)
        key_file.write_text(generated, encoding="utf-8")
        # Best-effort permission tightening; unsupported on some filesystems.
        with contextlib.suppress(OSError):
            key_file.chmod(0o600)
        return generated


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sqlite_path = settings.sqlite_path()
    if sqlite_path is not None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return settings


def reload_settings() -> Settings:
    """Clear the settings cache (used by tests)."""
    get_settings.cache_clear()
    return get_settings()


settings_field = Field  # re-export to keep imports tidy for callers
