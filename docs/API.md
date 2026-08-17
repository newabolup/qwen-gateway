# API Reference

Interactive documentation is generated at runtime:

* Swagger UI — `http://localhost:8787/docs`
* ReDoc — `http://localhost:8787/redoc`
* OpenAPI schema — `http://localhost:8787/openapi.json`

This document summarises the same surface for offline reading.

---

## Authentication

| Surface | Mechanism | Header |
|---|---|---|
| Public API (`/v1/*`) | Gateway API key | `Authorization: Bearer qwg_…` (or `X-Api-Key`) |
| Admin API (`/api/admin/*`) | Session cookie from `POST /api/admin/login` | `Cookie: qwg_admin_session=…` |
| Health (`/health`, `/api/health`, `/api/stats`) | None (contains no secrets) | — |

Every response carries `X-Gateway-Request-Id: gwreq_…` for correlation with logs.

---

## Public API

### `GET /v1/models`

Lists available models plus configured aliases, in OpenAI's format.

```json
{
  "object": "list",
  "data": [
    {
      "id": "qwen3-max",
      "object": "model",
      "created": 1786996068,
      "owned_by": "qwen",
      "aliases": ["qwen", "qwen-default"],
      "supports_tools": true,
      "supports_reasoning": false
    }
  ]
}
```

### `POST /v1/chat/completions`

Request fields: `model`, `messages` (required), `stream`, `stream_options`,
`temperature`, `top_p`, `max_tokens` / `max_completion_tokens`, `stop`,
`presence_penalty`, `frequency_penalty`, `seed`, `user`, `tools`, `tool_choice`,
`functions`, `function_call`, `enable_thinking`, `reasoning_effort`.

Non-streaming response:

```json
{
  "id": "chatcmpl_l6pv6a3iavm0jlkzzn58otqt",
  "object": "chat.completion",
  "created": 1786996068,
  "model": "qwen",
  "choices": [
    {
      "index": 0,
      "message": { "role": "assistant", "content": "Hello! How can I help you?" },
      "finish_reason": "stop"
    }
  ],
  "usage": { "prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16 }
}
```

`finish_reason` is one of `stop`, `length`, `tool_calls`, `content_filter`.

Streaming (`"stream": true`) returns `text/event-stream` chunks of type
`chat.completion.chunk`, terminated by `data: [DONE]`.

---

## Health and statistics

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness. `{"status":"ok","service":"…"}` |
| `GET /api/health` | Readiness: database state, provider, token-pool counts, active streams |
| `GET /api/stats` | Requests today/total, success/failure, average latency, token and key counts, recent errors |

---

## Admin API

All routes require an admin session except `login` and `session`.

### Session
| Method | Path | Description |
|---|---|---|
| `POST` | `/api/admin/login` | Sign in; sets an HttpOnly session cookie |
| `POST` | `/api/admin/logout` | Sign out |
| `GET` | `/api/admin/session` | Current session state |

### Overview
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/overview` | Dashboard payload (stats + scheduler + providers) |

### Credentials
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/credentials` | List credentials (secrets masked) |
| `POST` | `/api/admin/credentials` | Add a credential (encrypted at rest) |
| `PATCH` | `/api/admin/credentials/{id}` | Rename, enable/disable, rotate secret, clear cooldown |
| `DELETE` | `/api/admin/credentials/{id}` | Delete |
| `POST` | `/api/admin/credentials/{id}/test` | Authenticate + health-check upstream |

### API keys
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/api-keys` | List keys (previews only) |
| `POST` | `/api/admin/api-keys` | Create; returns the plaintext **once** |
| `PATCH` | `/api/admin/api-keys/{id}` | Rename / enable / disable |
| `POST` | `/api/admin/api-keys/{id}/revoke` | Revoke |
| `DELETE` | `/api/admin/api-keys/{id}` | Delete |

### Models
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/models` | List catalogue |
| `POST` | `/api/admin/models` | Create or update (including aliases) |
| `POST` | `/api/admin/models/discover` | Refresh from upstream |
| `DELETE` | `/api/admin/models/{id}` | Delete |

### Requests, logs and settings
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/admin/requests` | Paginated history; filter by `status`, `model`, `search` |
| `POST` | `/api/admin/requests/purge` | Apply the retention policy now |
| `GET` | `/api/admin/logs` | Recent structured logs (redacted); filter by `level` |
| `GET` | `/api/admin/settings` | Effective configuration (no secrets) |
| `PATCH` | `/api/admin/settings` | Update runtime settings |

---

## Error format

Every error uses the same envelope:

```json
{
  "error": {
    "message": "Qwen provider temporarily unavailable.",
    "type": "upstream_error",
    "code": "provider_unavailable"
  }
}
```

| HTTP | `type` | Typical `code` |
|---|---|---|
| 400 | `invalid_request` | `invalid_request_error` |
| 401 | `authentication_error` | `invalid_api_key` |
| 403 | `permission_error` | `permission_denied` |
| 404 | `not_found` | `not_found` |
| 413 | `invalid_request` | `request_too_large` |
| 429 | `rate_limit_error` | `rate_limit_exceeded` (may include `Retry-After`) |
| 502 | `upstream_error` | `provider_unavailable`, `upstream_unauthorized`, `upstream_malformed_response` |
| 503 | `no_credentials` | `no_available_credential` |
| 504 | `timeout_error` | `upstream_timeout` |
| 500 | `internal_error` | `internal_error` |

Upstream status codes are never echoed verbatim; they are mapped to this table.
Errors that occur mid-stream are delivered as a terminal SSE error frame
followed by `data: [DONE]`.
