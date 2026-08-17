"""Structured logging with automatic secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from app.utils.redaction import redact_value

request_id_ctx: ContextVar[str] = ContextVar("gateway_request_id", default="-")

#: In-memory ring buffer so the admin UI can show recent logs without needing
#: access to the container's stdout. Bounded to avoid unbounded memory growth.
_LOG_BUFFER: list[dict[str, Any]] = []
_LOG_BUFFER_MAX = 1000


def get_recent_logs(limit: int = 200, level: str | None = None) -> list[dict[str, Any]]:
    items = _LOG_BUFFER
    if level:
        wanted = level.upper()
        items = [i for i in items if i["level"] == wanted]
    return list(reversed(items[-limit:]))


def clear_log_buffer() -> None:
    _LOG_BUFFER.clear()


class _RedactingFormatter(logging.Formatter):
    def __init__(self, json_mode: bool) -> None:
        super().__init__()
        self.json_mode = json_mode

    def format(self, record: logging.LogRecord) -> str:
        payload = _record_to_payload(record)
        if self.json_mode:
            return json.dumps(payload, ensure_ascii=False, default=str)
        extras = " ".join(
            f"{k}={v}"
            for k, v in payload.items()
            if k not in {"ts", "level", "logger", "event", "request_id"}
        )
        base = f"{payload['ts']} {payload['level']:<7} [{payload['request_id']}] {payload['event']}"
        return f"{base} {extras}".rstrip()


def _record_to_payload(record: logging.LogRecord) -> dict[str, Any]:
    standard = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }
    extras = {key: value for key, value in record.__dict__.items() if key not in standard}
    payload: dict[str, Any] = {
        "ts": logging.Formatter().formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        "level": record.levelname,
        "logger": record.name,
        "event": record.getMessage(),
        "request_id": extras.pop("request_id", None) or request_id_ctx.get(),
    }
    if record.exc_info:
        payload["error"] = str(record.exc_info[1])
    payload.update(extras)
    return redact_value(payload)  # type: ignore[return-value]


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = _record_to_payload(record)
        except Exception:  # pragma: no cover - logging must never explode
            return
        _LOG_BUFFER.append(payload)
        if len(_LOG_BUFFER) > _LOG_BUFFER_MAX:
            del _LOG_BUFFER[: len(_LOG_BUFFER) - _LOG_BUFFER_MAX]


def configure_logging(level: str = "INFO", json_mode: bool = False) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(_RedactingFormatter(json_mode))
    root.addHandler(stream)
    root.addHandler(_BufferHandler())

    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel("WARNING")


class GatewayLogger:
    """Thin wrapper enforcing the ``event`` + structured-fields log style."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _log(self, level: int, event: str, **fields: Any) -> None:
        self._logger.log(level, event, extra=redact_value(fields))

    def debug(self, event: str, **fields: Any) -> None:
        self._log(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._log(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._log(logging.ERROR, event, **fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._logger.exception(event, extra=redact_value(fields))


def get_logger(name: str) -> GatewayLogger:
    return GatewayLogger(name)
