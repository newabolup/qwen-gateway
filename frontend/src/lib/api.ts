/** Typed admin API client. All requests are same-origin and cookie-authenticated. */

export interface Credential {
  id: number;
  name: string;
  provider: string;
  auth_mode: string;
  secret_hint: string;
  status: string;
  status_reason: string | null;
  enabled: boolean;
  base_url: string | null;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  last_success_at: string | null;
  last_error_at: string | null;
  cooldown_until: string | null;
  request_count: number;
  success_count: number;
  failure_count: number;
  consecutive_failures: number;
  in_flight: number;
}

export interface ApiKey {
  id: number;
  name: string;
  description: string | null;
  key_preview: string;
  enabled: boolean;
  revoked: boolean;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  request_count: number;
  success_count: number;
  failure_count: number;
}

export interface ApiKeyCreated extends ApiKey {
  api_key: string;
}

export interface ModelEntry {
  id: number;
  provider: string;
  model_id: string;
  display_name: string | null;
  aliases: string[];
  enabled: boolean;
  context_window: number | null;
  supports_tools: boolean;
  supports_reasoning: boolean;
  discovered_at: string;
}

export interface RequestEntry {
  id: number;
  request_id: string;
  created_at: string;
  api_key_name: string | null;
  credential_id: number | null;
  credential_name: string | null;
  provider: string;
  model: string;
  upstream_model: string | null;
  endpoint: string;
  streaming: boolean;
  status: string;
  status_code: number | null;
  error_category: string | null;
  error_message: string | null;
  latency_ms: number | null;
  first_token_ms: number | null;
  attempts: number;
  total_tokens: number | null;
}

export interface Overview {
  requests_today: number;
  requests_total: number;
  successful_requests: number;
  failed_requests: number;
  average_latency_ms: number;
  active_streams: number;
  tokens: { total: number; healthy: number; cooldown: number; disabled: number; expired: number };
  api_keys: { total: number; active: number };
  recent_errors: Array<{
    request_id: string;
    created_at: string | null;
    model: string;
    category: string | null;
    message: string | null;
    credential_id: number | null;
  }>;
  scheduler: { strategy: string; in_flight: Record<string, number> };
  providers: string[];
}

export interface LogEntry {
  ts: string;
  level: string;
  event: string;
  request_id: string | null;
  extra: Record<string, unknown>;
}

export interface GatewaySettings {
  app_env: string;
  default_provider: string;
  scheduler_strategy: string;
  expose_reasoning: boolean;
  default_model: string;
  model_aliases: Record<string, string>;
  qwen_mode: string;
  request_log_retention_days: number;
  store_request_bodies: boolean;
  max_failover_attempts: number;
  default_cooldown_seconds: number;
  rate_limit_cooldown_seconds: number;
  mock_provider_enabled: boolean;
  secret_key_configured: boolean;
  admin_configured: boolean;
}

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: init.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const err = (payload as { error?: { message?: string; code?: string } } | null)?.error;
    throw new ApiError(err?.message ?? `Request failed (${response.status})`, response.status, err?.code);
  }
  return payload as T;
}

const body = (data: unknown): RequestInit => ({ body: JSON.stringify(data) });

export const api = {
  // session
  session: () =>
    request<{ authenticated: boolean; username: string | null; admin_configured: boolean }>(
      "/api/admin/session",
    ),
  login: (username: string, password: string) =>
    request<{ ok: boolean; username: string }>("/api/admin/login", {
      method: "POST",
      ...body({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>("/api/admin/logout", { method: "POST" }),

  // dashboard
  overview: () => request<Overview>("/api/admin/overview"),
  health: () => request<Record<string, unknown>>("/api/health"),

  // credentials
  credentials: () => request<Credential[]>("/api/admin/credentials"),
  createCredential: (data: {
    name: string;
    secret: string;
    refresh_secret?: string;
    auth_mode: string;
    base_url?: string;
  }) => request<Credential>("/api/admin/credentials", { method: "POST", ...body(data) }),
  updateCredential: (id: number, data: Record<string, unknown>) =>
    request<Credential>(`/api/admin/credentials/${id}`, { method: "PATCH", ...body(data) }),
  deleteCredential: (id: number) =>
    request<{ ok: boolean }>(`/api/admin/credentials/${id}`, { method: "DELETE" }),
  testCredential: (id: number) =>
    request<{ id: number; healthy: boolean; detail: string | null; latency_ms: number | null; models_discovered: number | null }>(
      `/api/admin/credentials/${id}/test`,
      { method: "POST" },
    ),

  // api keys
  apiKeys: () => request<ApiKey[]>("/api/admin/api-keys"),
  createApiKey: (data: { name: string; description?: string; expires_in_days?: number }) =>
    request<ApiKeyCreated>("/api/admin/api-keys", { method: "POST", ...body(data) }),
  updateApiKey: (id: number, data: Record<string, unknown>) =>
    request<ApiKey>(`/api/admin/api-keys/${id}`, { method: "PATCH", ...body(data) }),
  revokeApiKey: (id: number) =>
    request<ApiKey>(`/api/admin/api-keys/${id}/revoke`, { method: "POST" }),
  deleteApiKey: (id: number) =>
    request<{ ok: boolean }>(`/api/admin/api-keys/${id}`, { method: "DELETE" }),

  // models
  models: () => request<ModelEntry[]>("/api/admin/models"),
  upsertModel: (data: Record<string, unknown>) =>
    request<ModelEntry>("/api/admin/models", { method: "POST", ...body(data) }),
  discoverModels: () => request<ModelEntry[]>("/api/admin/models/discover", { method: "POST" }),
  deleteModel: (id: number) =>
    request<{ ok: boolean }>(`/api/admin/models/${id}`, { method: "DELETE" }),

  // requests & logs
  requests: (params: { page?: number; page_size?: number; status?: string; search?: string }) => {
    const query = new URLSearchParams();
    if (params.page) query.set("page", String(params.page));
    if (params.page_size) query.set("page_size", String(params.page_size));
    if (params.status) query.set("status", params.status);
    if (params.search) query.set("search", params.search);
    return request<{ items: RequestEntry[]; total: number; page: number; page_size: number }>(
      `/api/admin/requests?${query.toString()}`,
    );
  },
  purgeRequests: () => request<{ ok: boolean; detail: string }>("/api/admin/requests/purge", { method: "POST" }),
  logs: (level?: string) =>
    request<LogEntry[]>(`/api/admin/logs?limit=300${level ? `&level=${level}` : ""}`),

  // settings
  settings: () => request<GatewaySettings>("/api/admin/settings"),
  updateSettings: (data: Record<string, unknown>) =>
    request<GatewaySettings>("/api/admin/settings", { method: "PATCH", ...body(data) }),
};
