import { useEffect } from "react";
import { api } from "../lib/api";
import { Card, Empty, StatCard, StatusBadge, formatDate, useAsync } from "../components/ui";

export default function Dashboard() {
  const { data, loading, error, reload } = useAsync(() => api.overview(), []);

  useEffect(() => {
    const timer = setInterval(() => void reload(), 10_000);
    return () => clearInterval(timer);
  }, [reload]);

  if (loading && !data) return <Empty message="Loading dashboard…" />;
  if (error) return <Empty message={error} />;
  if (!data) return null;

  const successRate =
    data.requests_today > 0
      ? Math.round((data.successful_requests / data.requests_today) * 100)
      : 100;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div className="grid cols-4">
        <StatCard
          label="Gateway"
          value={<StatusBadge status={data.tokens.healthy > 0 ? "healthy" : "degraded"} />}
          sub={`provider: ${data.providers.join(", ")}`}
        />
        <StatCard label="Requests today" value={data.requests_today} sub={`${data.requests_total} all time`} />
        <StatCard label="Success rate" value={`${successRate}%`} sub={`${data.failed_requests} failed today`} />
        <StatCard
          label="Avg latency"
          value={`${Math.round(data.average_latency_ms)} ms`}
          sub={`${data.active_streams} active stream(s)`}
        />
      </div>

      <div className="grid cols-4">
        <StatCard label="Tokens total" value={data.tokens.total} />
        <StatCard label="Healthy tokens" value={data.tokens.healthy} sub="ready to serve" />
        <StatCard label="Cooling down" value={data.tokens.cooldown} sub="rate limited / backing off" />
        <StatCard
          label="Unavailable"
          value={data.tokens.disabled + data.tokens.expired}
          sub={`${data.tokens.disabled} disabled · ${data.tokens.expired} expired`}
        />
      </div>

      <div className="grid cols-2">
        <Card title="Scheduler">
          <dl className="kv">
            <dt>Strategy</dt>
            <dd className="mono">{data.scheduler.strategy}</dd>
            <dt>In-flight per token</dt>
            <dd className="mono">
              {Object.keys(data.scheduler.in_flight).length === 0
                ? "idle"
                : Object.entries(data.scheduler.in_flight)
                    .map(([id, n]) => `#${id}: ${n}`)
                    .join("  ")}
            </dd>
            <dt>API keys</dt>
            <dd>
              {data.api_keys.active} active / {data.api_keys.total} total
            </dd>
          </dl>
        </Card>

        <Card title="Recent errors">
          {data.recent_errors.length === 0 ? (
            <p className="dim" style={{ margin: 0 }}>
              No errors recorded.
            </p>
          ) : (
            <div style={{ display: "grid", gap: 9 }}>
              {data.recent_errors.slice(0, 6).map((err) => (
                <div key={err.request_id} style={{ display: "grid", gap: 2 }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <span className="badge err">{err.category ?? "error"}</span>
                    <span className="dim mono" style={{ fontSize: 11 }}>
                      {formatDate(err.created_at)}
                    </span>
                    <span className="dim" style={{ fontSize: 12 }}>
                      {err.model}
                    </span>
                  </div>
                  <div style={{ fontSize: 12.5 }}>{err.message}</div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
