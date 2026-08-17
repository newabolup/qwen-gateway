# Qwen Token Gateway

An **OpenAI-compatible API gateway** that sits in front of Qwen. It accepts a
standard `POST /v1/chat/completions` request from any OpenAI-compatible client,
authenticates upstream with **your own** Qwen credentials, normalizes Qwen's
internal events into a clean public contract, and returns a standard response.

It is an **adapter/proxy**, not a token converter. Clients talk to the gateway
with a gateway API key (`qwg_...`) and never see the underlying Qwen credential.

```
Claude Code / Codex / OpenCode / Cline / Roo Code / OpenAI SDK / your agent
                              │
                              │  OpenAI-compatible HTTP  (Bearer qwg_…)
                              ▼
                     Qwen Token Gateway
                              │  your Qwen credentials (encrypted at rest)
                              ▼
                        Qwen backend
```

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [Requirements](#3-requirements)
4. [Windows installation](#4-windows-installation)
5. [Linux installation](#5-linux-installation)
6. [Docker installation](#6-docker-installation)
7. [Railway deployment](#7-railway-deployment)
8. [Environment variables](#8-environment-variables)
9. [Adding a Qwen credential](#9-adding-a-qwen-credential)
10. [Creating an API key](#10-creating-an-api-key)
11. [Configuring a client](#11-configuring-a-client)
12. [Streaming](#12-streaming)
13. [Tool calling](#13-tool-calling)
14. [Reasoning](#14-reasoning)
15. [Troubleshooting](#15-troubleshooting)
16. [Security notes](#16-security-notes)
17. [Development and testing](#17-development-and-testing)
18. [Credential policy](#18-credential-policy)

---

## 1. What it does

| Capability | Details |
|---|---|
| OpenAI-compatible API | `GET /v1/models`, `POST /v1/chat/completions`, streaming and non-streaming |
| Multiple credentials | A pool of Qwen tokens with round-robin or least-recently-used scheduling |
| Automatic failover | A retryable failure transparently retries on a different credential |
| Rate-limit cooldown | HTTP 429 puts a credential to sleep (honouring `Retry-After`) instead of hammering it |
| Response normalization | Qwen UI markup (`<details>`), `Response ID` / `Request ID` metadata and internal agent events are classified — never emitted as assistant text |
| Tool calling | Native and text-embedded (`<tool_call>`) invocations normalized into OpenAI `tool_calls`, including streamed deltas |
| Reasoning separation | Upstream thinking is parsed into its own channel and only exposed when you opt in |
| API-key auth | `qwg_…` keys with enable/disable, revoke, expiry and per-key statistics; only a hash is stored |
| Admin dashboard | React UI for tokens, keys, models, requests, logs and settings, with dark mode |
| Observability | Structured logs with a `gwreq_…` correlation ID per request and automatic secret redaction |
| Deployment | Windows, Linux (systemd), Docker, Railway; SQLite by default, PostgreSQL supported |

---

## 2. Architecture

```
Client
  │
  ▼
API Gateway (FastAPI)
  ├── Authentication middleware   app/auth/
  ├── Request validation          app/api/schemas.py     (Pydantic)
  ├── Model router                app/gateway/router.py  (aliases → upstream model)
  ├── Token scheduler             app/gateway/scheduler.py (pool, cooldown, failover)
  ├── Provider adapter            app/providers/qwen/    ← the only Qwen-aware code
  ├── Event normalizer            app/gateway/normalizer.py
  ├── Streaming layer             app/gateway/streaming.py
  ├── Error normalizer            app/gateway/errors.py
  └── Usage / metrics             app/services/metrics.py
  │
  ▼
Qwen backend
```

Every provider implements one interface (`app/providers/base.py`):

```python
class Provider:
    async def authenticate(credential) -> AuthResult
    async def list_models(credential) -> list[ProviderModelInfo]
    async def create_completion(req) -> list[NormalizedEvent]
    def   stream_completion(req) -> AsyncIterator[NormalizedEvent]
    async def health_check(credential) -> HealthResult
    def   normalize_error(exc) -> GatewayError
```

The rest of the application only ever sees `NormalizedEvent`
(`assistant_text` · `reasoning` · `tool_call` · `tool_result` · `metadata` ·
`system_event` · `usage` · `error` · `done` · `unknown`). **If Qwen changes its
protocol, only `app/providers/qwen/` needs to change.**

### The normalization pipeline

Qwen's web surface mixes real prose, HTML/Markdown UI chrome and diagnostics
into one channel. Forwarding it verbatim is what makes naive proxies emit:

```
<details>
<summary></summary>
Response ID: 4c2f…
Request ID: 8b1c…
Copy
</details>

I am ready to assist you...
```

The gateway instead runs a *structural* parser:

```
Qwen raw event → Parser → classification → OpenAI formatter
                              ├── assistant_text  → message.content
                              ├── reasoning       → reasoning_content (opt-in)
                              ├── tool_call       → message.tool_calls
                              ├── metadata        → internal only
                              ├── system_event    → internal only
                              └── unknown         → internal + logged warning
```

Key design rules, all covered by tests:

* UI wrappers are detected **structurally**, and the contents are then
  classified. Arbitrary text is never deleted by keyword matching — a sentence
  that merely mentions "Response ID:" in normal prose is preserved intact.
* A wrapper split across TCP chunks is buffered until it can be resolved, so a
  partial `<details` tag never leaks into the stream.
* Anything ambiguous is preserved internally as a `system_event` with a
  diagnostic note rather than being dropped or promoted to assistant text.

### Repository layout

```
qwen-gateway/
├── app/
│   ├── main.py               FastAPI app, lifespan, exception handlers, UI mount
│   ├── config.py             Environment configuration
│   ├── database.py           Async engine/session management
│   ├── cli.py                Operational CLI
│   ├── api/                  public.py · admin.py · schemas.py · middleware.py
│   ├── auth/                 api_key.py (clients) · admin.py (dashboard)
│   ├── providers/
│   │   ├── base.py           Provider interface
│   │   ├── events.py         Normalized event vocabulary
│   │   ├── registry.py       Provider registry
│   │   ├── qwen/             client.py · parser.py · markup.py · tools.py · auth.py
│   │   └── mock/             Offline provider for dev and tests
│   ├── gateway/              router · scheduler · normalizer · streaming · errors
│   ├── models/db.py          SQLAlchemy entities
│   ├── services/             completion · credential · api_key · model · metrics · settings
│   ├── security/             crypto.py (Fernet) · hashing.py (PBKDF2/HMAC)
│   └── utils/                logging · redaction · sse · ids
├── frontend/                 React + TypeScript + Vite admin dashboard
├── tests/                    192 tests, no real credentials required
├── docker/                   systemd unit
├── scripts/                  setup · start · dev · test · lint · format · build (.sh + .bat)
├── Dockerfile · docker-compose.yml · railway.json
└── .env.example · pyproject.toml · requirements.txt
```

---

## 3. Requirements

* **Python 3.11+** (3.12 recommended)
* **Node.js 18+** — only to build the admin UI; the API runs without it
* A Qwen credential you are authorized to use (not needed for development —
  see the [mock provider](#mock-provider))

---

## 4. Windows installation

```bat
git clone https://github.com/your-org/qwen-gateway.git
cd qwen-gateway

scripts\setup.bat
```

`setup.bat` creates `.venv`, installs dependencies, writes a `.env` with a
freshly generated `GATEWAY_SECRET_KEY` and admin password, and builds the UI.

Start it:

```bat
scripts\start.bat
```

Or manually:

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m app.cli generate-key
REM paste the key into .env as GATEWAY_SECRET_KEY, set ADMIN_PASSWORD
.venv\Scripts\python -m app
```

Open <http://localhost:8787>.

**Run at login (optional).** Create a shortcut to `scripts\start.bat` in:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

Or register a Task Scheduler job:

```bat
schtasks /create /tn "Qwen Gateway" /tr "\"%CD%\scripts\start.bat\"" /sc onlogon /rl highest
```

---

## 5. Linux installation

```bash
git clone https://github.com/your-org/qwen-gateway.git
cd qwen-gateway

./scripts/setup.sh     # venv + deps + .env + UI build
./scripts/start.sh
```

Manual equivalent:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
./.venv/bin/python -m app.cli generate-key   # paste into .env
chmod 600 .env
./.venv/bin/python -m app
```

### systemd service

```bash
sudo useradd --system --create-home --home-dir /opt/qwen-gateway qwen-gateway
sudo cp -r . /opt/qwen-gateway
sudo chown -R qwen-gateway:qwen-gateway /opt/qwen-gateway
sudo chmod 600 /opt/qwen-gateway/.env

sudo cp docker/qwen-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qwen-gateway
sudo systemctl status qwen-gateway
sudo journalctl -u qwen-gateway -f
```

### Behind nginx (TLS termination)

```nginx
server {
    listen 443 ssl http2;
    server_name gateway.example.com;

    ssl_certificate     /etc/letsencrypt/live/gateway.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/gateway.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Required for SSE streaming:
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

Then set `TRUST_FORWARDED_FOR=true` so rate limiting sees real client IPs.

---

## 6. Docker installation

```bash
cp .env.example .env
docker run --rm python:3.12-slim python -c \
  "import base64,hashlib,os;print(base64.urlsafe_b64encode(hashlib.sha256(os.urandom(64)).digest()).decode())"
# put the value in .env as GATEWAY_SECRET_KEY, and set ADMIN_PASSWORD

docker compose up -d --build
docker compose logs -f gateway
```

The gateway is on <http://localhost:8787>. Data persists in the `gateway-data`
volume.

Plain Docker:

```bash
docker build -t qwen-token-gateway .
docker run -d --name qwen-gateway -p 8787:8787 \
  -e GATEWAY_SECRET_KEY="$GATEWAY_SECRET_KEY" \
  -e ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  -e APP_ENV=production \
  -v qwen-gateway-data:/app/data \
  qwen-token-gateway
```

With PostgreSQL:

```bash
docker compose --profile postgres up -d
# then set in .env:
# DATABASE_URL=postgresql+asyncpg://gateway:gateway@postgres:5432/gateway
```

The image runs as a non-root user, needs no graphical desktop, and includes a
`/health` healthcheck.

---

## 7. Railway deployment

1. Push the repository to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo**. `railway.json` selects
   the Dockerfile builder and the `/health` healthcheck automatically.
3. Set variables under **Variables**:

   | Variable | Value |
   |---|---|
   | `GATEWAY_SECRET_KEY` | *(generate — see below)* |
   | `ADMIN_USERNAME` | `admin` |
   | `ADMIN_PASSWORD` | a strong password |
   | `APP_ENV` | `production` |
   | `LOG_JSON` | `true` |
   | `DEFAULT_PROVIDER` | `qwen` |
   | `TRUST_FORWARDED_FOR` | `true` |

   Generate a key locally with `python -m app.cli generate-key`.

4. **Persistence.** Railway containers have ephemeral filesystems, so SQLite
   would be lost on redeploy. Choose one:
   * **PostgreSQL (recommended):** add the Postgres plugin and set
     `DATABASE_URL=${{Postgres.DATABASE_URL}}`. Plain `postgres://` URLs are
     converted to the async driver automatically.
   * **Volume:** attach a volume mounted at `/app/data` and keep the default
     SQLite URL.

5. `PORT` is injected by Railway and honoured by the start command; the server
   always binds `0.0.0.0`.

6. Generate a public domain under **Settings → Networking**, then open
   `https://<your-app>.up.railway.app`.

---

## 8. Environment variables

Every variable, its default and its purpose. See `.env.example` for a
copy-pasteable template.

### Application

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | `development`, `production` or `test`. Production requires `GATEWAY_SECRET_KEY`. |
| `APP_NAME` | `Qwen Token Gateway` | Shown in docs and health output. |
| `HOST` | `0.0.0.0` | Bind address. Keep `0.0.0.0` for Docker/PaaS. |
| `PORT` | `8787` | Listen port; PaaS platforms override it. |
| `LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL`. |
| `LOG_JSON` | `false` | One JSON object per log line. Recommended in production. |

### Secrets

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_SECRET_KEY` | *(none)* | **Required in production.** Encrypts stored credentials and signs admin sessions. Changing it makes existing credentials undecryptable. |
| `ADMIN_USERNAME` | `admin` | Dashboard username. |
| `ADMIN_PASSWORD` | *(none)* | Dashboard password. **The admin API/UI stays disabled until this is set.** |
| `SESSION_TTL_SECONDS` | `43200` | Admin session lifetime. |

### Database

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/gateway.db` | SQLite or PostgreSQL. `postgres://` and `postgresql://` are upgraded to `postgresql+asyncpg://`. |
| `REQUEST_LOG_RETENTION_DAYS` | `14` | Auto-purge age for request logs; `0` disables. |
| `STORE_REQUEST_BODIES` | `false` | Store a redacted, truncated request preview. |

### Provider

| Variable | Default | Description |
|---|---|---|
| `DEFAULT_PROVIDER` | `qwen` | `qwen` (real) or `mock` (offline). |
| `ENABLE_MOCK_PROVIDER` | `true` | Allow the mock provider at all. |
| `QWEN_MODE` | `auto` | `auto`, `portal` (OAuth bearer) or `web` (session cookie). |
| `QWEN_PORTAL_BASE_URL` | `https://portal.qwen.ai/v1` | Portal dialect endpoint. |
| `QWEN_WEB_BASE_URL` | `https://chat.qwen.ai` | Web dialect endpoint. |
| `QWEN_REQUEST_TIMEOUT` | `120` | Upstream read timeout (seconds). |
| `QWEN_CONNECT_TIMEOUT` | `15` | Upstream connect timeout. |
| `QWEN_MAX_RETRIES` | `2` | Transport-level retries. |
| `QWEN_WEB_CLIENT_VERSION` | `0.2.81` | Client version header for the web dialect. |
| `QWEN_OAUTH_CLIENT_ID` | `f0304373…` | Used only for the OAuth refresh grant. |
| `QWEN_OAUTH_BASE_URL` | `https://chat.qwen.ai` | OAuth token endpoint host. |
| `HTTP_PROXY_URL` | *(none)* | Optional outbound proxy. |

### Gateway behaviour

| Variable | Default | Description |
|---|---|---|
| `SCHEDULER_STRATEGY` | `round_robin` | Or `least_recently_used`. |
| `DEFAULT_COOLDOWN_SECONDS` | `300` | Cooldown after a generic failure. |
| `RATE_LIMIT_COOLDOWN_SECONDS` | `900` | Cooldown after a 429 (upstream `Retry-After` wins). |
| `MAX_FAILOVER_ATTEMPTS` | `3` | Credentials one request may try. |
| `EXPOSE_REASONING` | `false` | Return `reasoning_content` to clients. |
| `MAX_REQUEST_BYTES` | `8388608` | Inbound body limit (8 MiB). |
| `STREAM_IDLE_TIMEOUT` | `180` | Max idle seconds inside a stream. |

### Models

| Variable | Default | Description |
|---|---|---|
| `DEFAULT_MODEL` | `qwen3-max` | Used when a client requests an unknown model. |
| `MODEL_ALIASES` | `qwen=qwen3-max,…` | Comma-separated `alias=target` pairs. |

### Security

| Variable | Default | Description |
|---|---|---|
| `CORS_ALLOW_ORIGINS` | `*` | `*` or a comma-separated origin list. Use an explicit list in production. |
| `ADMIN_RATE_LIMIT_PER_MINUTE` | `120` | Per-IP admin limit; `0` disables. |
| `PUBLIC_RATE_LIMIT_PER_MINUTE` | `0` | Per-IP public API limit; `0` disables. |
| `TRUST_FORWARDED_FOR` | `false` | Honour `X-Forwarded-For`. Enable **only** behind a trusted proxy. |

---

## 9. Adding a Qwen credential

> Only add credentials you own or are explicitly authorized to use. See
> [Credential policy](#18-credential-policy).

The gateway accepts two kinds of user-supplied credential:

| Mode | What it is | Where you get it |
|---|---|---|
| `portal` | An OAuth access token from your own Qwen device-code login | e.g. `access_token` in `~/.qwen/oauth_creds.json` after you run the official `qwen` CLI and log in |
| `web` | Your own `chat.qwen.ai` session token | Copied from your own logged-in session |

`auto` inspects the token and picks the right dialect.

### Via the dashboard

1. Open <http://localhost:8787> and sign in.
2. **Tokens → Add token**.
3. Give it a name, paste the secret, optionally add a refresh token
   (enables automatic OAuth refresh), and save.
4. Click **Test** to verify it against the upstream.

After saving, the value is only ever shown masked (`••••••••••••abcd`).

### Via the CLI (no secret in shell history)

```bash
export QWEN_TOKEN='...'        # or omit --secret-env to be prompted
./.venv/bin/python -m app.cli add-credential --name "Account 1" --secret-env QWEN_TOKEN

./.venv/bin/python -m app.cli list-credentials
./.venv/bin/python -m app.cli test-credential --id 1
```

Add several credentials to enable load balancing and failover.

---

## 10. Creating an API key

Dashboard: **API Keys → Create API key**. The plaintext `qwg_…` value is shown
**once** — copy it immediately. Only a keyed hash is stored.

CLI:

```bash
./.venv/bin/python -m app.cli create-api-key --name "Claude Code"
```

Keys can be disabled, revoked, given an expiry, and inspected for per-key usage
statistics.

---

## 11. Configuring a client

The gateway base URL is `http://localhost:8787/v1` (or your public HTTPS URL).

### OpenAI SDK (Python)

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8787/v1", api_key="qwg_your_key")

response = client.chat.completions.create(
    model="qwen",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

### OpenAI SDK (Node)

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://localhost:8787/v1",
  apiKey: "qwg_your_key",
});
```

### curl

```bash
curl http://localhost:8787/v1/chat/completions \
  -H "Authorization: Bearer qwg_your_key" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen","messages":[{"role":"user","content":"hi"}]}'
```

### Claude Code

Claude Code speaks the Anthropic protocol natively, so route it through a
translation layer such as `claude-code-router` and point that at this gateway:

```json
{
  "Providers": [
    {
      "name": "qwen-gateway",
      "api_base_url": "http://localhost:8787/v1/chat/completions",
      "api_key": "qwg_your_key",
      "models": ["qwen", "qwen-coder"]
    }
  ],
  "Router": { "default": "qwen-gateway,qwen-coder" }
}
```

### Codex CLI

`~/.codex/config.toml`:

```toml
model = "qwen"
model_provider = "qwen-gateway"

[model_providers.qwen-gateway]
name = "Qwen Gateway"
base_url = "http://localhost:8787/v1"
env_key = "QWEN_GATEWAY_API_KEY"
wire_api = "chat"
```

```bash
export QWEN_GATEWAY_API_KEY=qwg_your_key
```

### OpenCode

`opencode.json`:

```json
{
  "provider": {
    "qwen-gateway": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Qwen Gateway",
      "options": {
        "baseURL": "http://localhost:8787/v1",
        "apiKey": "qwg_your_key"
      },
      "models": { "qwen": { "name": "Qwen (gateway)" } }
    }
  }
}
```

### Cline / Roo Code (VS Code)

Select **OpenAI Compatible** and set:

* Base URL: `http://localhost:8787/v1`
* API Key: `qwg_your_key`
* Model ID: `qwen`

### Generic environment variables

Many tools read these:

```bash
export OPENAI_BASE_URL=http://localhost:8787/v1
export OPENAI_API_KEY=qwg_your_key
```

> Client configuration formats change frequently. If yours is not listed, use
> the **OpenAI-compatible** provider option with the base URL and key above.

---

## 12. Streaming

Set `"stream": true`. The gateway returns OpenAI-compatible SSE and never
buffers the whole answer.

```bash
curl -N http://localhost:8787/v1/chat/completions \
  -H "Authorization: Bearer qwg_your_key" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen","messages":[{"role":"user","content":"Count to three"}],"stream":true}'
```

```
data: {"id":"chatcmpl_x","object":"chat.completion.chunk",…,"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}

data: {"id":"chatcmpl_x",…,"choices":[{"index":0,"delta":{"content":"One"}}]}

data: {"id":"chatcmpl_x",…,"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

Add `"stream_options": {"include_usage": true}` for a final usage-only chunk.

Handled correctly: partial chunks, multi-byte characters split across TCP
frames, upstream disconnects, malformed events, tool-call chunks, reasoning
chunks, the final chunk and `[DONE]`. If the upstream fails *after* streaming
has begun, the gateway emits a terminal error frame followed by `[DONE]` rather
than truncating the connection.

---

## 13. Tool calling

Send standard OpenAI `tools`. Legacy `functions` is accepted too.

```python
tools = [{
    "type": "function",
    "function": {
        "name": "powershell",
        "description": "Run a PowerShell command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}]

response = client.chat.completions.create(
    model="qwen",
    messages=[{"role": "user", "content": "show my current directory"}],
    tools=tools,
)

for call in response.choices[0].message.tool_calls:
    print(call.id, call.function.name, call.function.arguments)
    # call_8s0i… powershell {"command": "Get-Location"}
```

Two upstream paths are supported:

* **Native** — the upstream emits OpenAI-shaped `tool_calls` deltas, which are
  reassembled across chunks.
* **Text-embedded** — the upstream has no native tool API, so the gateway
  injects a tool instruction and parses `<tool_call>{…}</tool_call>` blocks back
  out of the stream. Partial markers are never leaked as assistant text.

Tool semantics are never invented: a block only becomes a tool call if it parses
into a JSON object naming a tool the client actually declared. Otherwise the raw
event is preserved internally and a classification warning is logged, rather
than corrupting the response.

Multi-turn tool loops work — send the `tool_calls` back plus `role: "tool"`
results and continue as usual.

---

## 14. Reasoning

Reasoning is always parsed into its own internal channel, so it never
contaminates `message.content`. It is **not** returned to clients unless you opt
in:

```env
EXPOSE_REASONING=true
```

Then responses carry a separate field:

```json
{
  "message": {
    "role": "assistant",
    "content": "The answer is 4.",
    "reasoning_content": "2 + 2 = 4."
  }
}
```

Reasoning is never fabricated — the field appears only when the upstream
actually emitted it.

---

## 15. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `503 no_available_credential` | No enabled, non-expired, non-cooling credential. Check **Tokens**; use **Clear cooldown** or add another credential. |
| `401 invalid_api_key` | Missing/wrong/disabled/revoked/expired gateway key. Send `Authorization: Bearer qwg_…`. |
| `502 upstream_unauthorized` | Qwen rejected the credential — it likely expired. Rotate it (**Tokens → Edit**) or add a refresh token. |
| `429 rate_limit_exceeded` | Upstream throttled you; the credential is cooling down. Add more credentials. |
| `502 upstream_malformed_response` | The upstream returned something unparsable (often a WAF/login page). Check **Logs**; the credential may need re-authentication. |
| Admin UI shows "Admin access is not configured" | `ADMIN_PASSWORD` is unset. Set it and restart. |
| `Stored credential could not be decrypted` | `GATEWAY_SECRET_KEY` changed. Restore the old key or re-add credentials. |
| UI is a JSON placeholder | The frontend is not built: `npm --prefix frontend install && npm --prefix frontend run build`. |
| Streaming arrives all at once | A buffering proxy. Set `proxy_buffering off` in nginx. |
| `RuntimeError: GATEWAY_SECRET_KEY must be set` | `APP_ENV=production` without a key. Run `python -m app.cli generate-key`. |
| SQLite `database is locked` | Concurrent writers on a network filesystem. Move the DB to local disk or use PostgreSQL. |

Useful checks:

```bash
curl localhost:8787/health          # liveness
curl localhost:8787/api/health      # readiness + token pool
curl localhost:8787/api/stats       # aggregate stats
./.venv/bin/python -m app.cli health
```

Interactive API docs: <http://localhost:8787/docs>.

---

## 16. Security notes

* **Encryption at rest.** Credentials are encrypted with Fernet
  (AES-128-CBC + HMAC-SHA256) using a PBKDF2-SHA256 key (240k iterations)
  derived from `GATEWAY_SECRET_KEY`. Plaintext never touches the database.
* **API keys.** Only a keyed HMAC-SHA256 digest is stored. The plaintext is
  displayed once at creation.
* **Admin passwords** are compared in constant time and never stored or logged.
* **Redaction.** Every log line, request record and error message passes through
  a redactor that masks bearer tokens, cookies, JWTs and `qwg_` keys.
* **No secret ever appears** in API responses, frontend state, `localStorage`,
  logs, exception traces or request history — enforced by tests.
* **Errors are safe.** Upstream bodies and stack traces stay in the logs; clients
  receive a stable normalized error object.
* **Admin isolation.** The public API, admin API and admin frontend are separate
  surfaces. An unauthenticated caller cannot read, add or delete credentials.
* **Hardening.** Request-size limits, upstream timeouts, per-IP admin rate
  limiting, configurable CORS, HttpOnly + SameSite session cookies (Secure in
  production), and a non-root container user.

Production checklist:

- [ ] `APP_ENV=production`
- [ ] Strong, backed-up `GATEWAY_SECRET_KEY`
- [ ] Strong `ADMIN_PASSWORD`
- [ ] HTTPS via reverse proxy (`TRUST_FORWARDED_FOR=true`)
- [ ] `CORS_ALLOW_ORIGINS` restricted to real origins
- [ ] Admin UI not exposed to the open internet, or IP-restricted
- [ ] PostgreSQL or a persistent volume
- [ ] `LOG_JSON=true`

---

## 17. Development and testing

```bash
git clone https://github.com/your-org/qwen-gateway.git
cd qwen-gateway
./scripts/setup.sh
./scripts/dev.sh          # API :8787 with reload + Vite UI :5173
```

| Script | Purpose |
|---|---|
| `scripts/setup.sh` / `.bat` | venv, dependencies, `.env`, UI build |
| `scripts/start.sh` / `.bat` | Run the gateway |
| `scripts/dev.sh` / `.bat` | Reloading backend + Vite dev server |
| `scripts/test.sh` / `.bat` | Test suite |
| `scripts/lint.sh` | `ruff check` + frontend type-check |
| `scripts/format.sh` | `ruff format` + autofix |
| `scripts/build.sh` | Production UI build |

### Mock provider

**No Qwen account is required to develop or test.** Set:

```env
DEFAULT_PROVIDER=mock
DEFAULT_MODEL=mock-qwen
```

The mock emits *the same raw event shapes the real parser consumes*, so it
exercises production code rather than bypassing it. Scenarios are selected via
the credential secret (`mock:<scenario>`) or a model suffix (`mock-qwen#429`):

`echo` (default) · `normal` · `reasoning` · `metadata` · `tool_call` ·
`multi_tool_call` · `native_tool_call` · `malformed` · `empty` · `401` · `403` ·
`429` · `500` · `502` · `timeout` · `network` · `disconnect`

### Tests

```bash
./scripts/test.sh                        # all 192 tests
./scripts/test.sh tests/test_api.py -v
./scripts/test.sh --cov=app --cov-report=term-missing
```

| File | Covers |
|---|---|
| `test_api.py` | Health, models, completions, streaming, auth, validation, logging |
| `test_normalization.py` | UI-markup separation, metadata, reasoning, malformed events |
| `test_tool_calls.py` | Tool blocks, streamed/native/multiple tool calls, OpenAI output |
| `test_scheduler.py` | Round robin, LRU, cooldown, failover, 100-way concurrency |
| `test_errors.py` | 401/403/408/429/5xx, timeouts, disconnects, safe error shape |
| `test_security.py` | Encryption, hashing, redaction, no-leak and admin-protection tests |
| `test_admin.py` | Credential/key/model/settings lifecycles, UTC timestamps |
| `test_qwen_adapter.py` | Both Qwen dialects and the SSE decoder, via a mock transport |
| `test_mock_provider.py` | Every mock scenario and the provider interface |

### Adding a provider

1. Create `app/providers/<name>/client.py` implementing `Provider`.
2. Register it in `app/providers/registry.py`.
3. Keep all protocol quirks inside that package — emit `NormalizedEvent` only.

---

## 18. Credential policy

This project is a **compatibility layer for credentials the operator already
holds**. It deliberately contains no mechanism to obtain credentials without
authorization:

* no browser cookie extraction,
* no credential theft or session hijacking,
* no access-control or CAPTCHA bypass,
* no automated account creation or credential sharing.

Credentials are supplied by the operator, stored encrypted, and used only to
make requests on that operator's behalf. Using this gateway must comply with
Qwen's/Alibaba Cloud's terms of service.

---

## License

MIT. See `pyproject.toml`.
