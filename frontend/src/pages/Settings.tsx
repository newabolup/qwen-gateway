import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Card, Empty, useAsync, useToast } from "../components/ui";

export default function Settings() {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync(() => api.settings(), []);
  const [strategy, setStrategy] = useState("round_robin");
  const [exposeReasoning, setExposeReasoning] = useState(false);
  const [defaultModel, setDefaultModel] = useState("");
  const [retention, setRetention] = useState(14);
  const [storeBodies, setStoreBodies] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!data) return;
    setStrategy(data.scheduler_strategy);
    setExposeReasoning(data.expose_reasoning);
    setDefaultModel(data.default_model);
    setRetention(data.request_log_retention_days);
    setStoreBodies(data.store_request_bodies);
  }, [data]);

  if (loading && !data) return <Empty message="Loading settings…" />;
  if (error) return <Empty message={error} />;
  if (!data) return null;

  const save = async () => {
    setBusy(true);
    try {
      await api.updateSettings({
        scheduler_strategy: strategy,
        expose_reasoning: exposeReasoning,
        default_model: defaultModel,
        request_log_retention_days: retention,
        store_request_bodies: storeBodies,
      });
      toast("ok", "Settings saved");
      await reload();
    } catch (e) {
      toast("err", e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid cols-2">
      <Card title="Runtime settings">
        <div className="field">
          <label>Scheduler strategy</label>
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            <option value="round_robin">Round robin</option>
            <option value="least_recently_used">Least recently used</option>
          </select>
        </div>

        <div className="field">
          <label>Default model</label>
          <input type="text" value={defaultModel} onChange={(e) => setDefaultModel(e.target.value)} />
          <div className="field-hint">Used when a client requests an unknown model.</div>
        </div>

        <div className="field">
          <label>Request log retention (days)</label>
          <input
            type="number"
            min={0}
            value={retention}
            onChange={(e) => setRetention(Number(e.target.value))}
          />
          <div className="field-hint">0 disables automatic purging.</div>
        </div>

        <div className="field">
          <label className="switch">
            <input
              type="checkbox"
              checked={exposeReasoning}
              onChange={(e) => setExposeReasoning(e.target.checked)}
            />
            <span>Expose reasoning to clients</span>
          </label>
          <div className="field-hint">
            When off, upstream reasoning is parsed and separated internally but never returned.
          </div>
        </div>

        <div className="field">
          <label className="switch">
            <input type="checkbox" checked={storeBodies} onChange={(e) => setStoreBodies(e.target.checked)} />
            <span>Store redacted request previews</span>
          </label>
          <div className="field-hint">Useful for debugging; adds prompt data to the database.</div>
        </div>

        <button className="btn primary" onClick={() => void save()} disabled={busy}>
          {busy ? "Saving…" : "Save settings"}
        </button>
      </Card>

      <div style={{ display: "grid", gap: 14, alignContent: "start" }}>
        <Card title="Environment (read-only)">
          <dl className="kv">
            <dt>Environment</dt>
            <dd className="mono">{data.app_env}</dd>
            <dt>Provider</dt>
            <dd className="mono">{data.default_provider}</dd>
            <dt>Qwen mode</dt>
            <dd className="mono">{data.qwen_mode}</dd>
            <dt>Failover attempts</dt>
            <dd className="mono">{data.max_failover_attempts}</dd>
            <dt>Default cooldown</dt>
            <dd className="mono">{data.default_cooldown_seconds}s</dd>
            <dt>Rate-limit cooldown</dt>
            <dd className="mono">{data.rate_limit_cooldown_seconds}s</dd>
            <dt>Encryption key</dt>
            <dd>
              {data.secret_key_configured ? (
                <span className="badge ok">configured</span>
              ) : (
                <span className="badge warn">ephemeral (dev only)</span>
              )}
            </dd>
            <dt>Mock provider</dt>
            <dd>
              <span className={`badge ${data.mock_provider_enabled ? "info" : "muted"}`}>
                {data.mock_provider_enabled ? "enabled" : "disabled"}
              </span>
            </dd>
          </dl>
        </Card>

        <Card title="Model aliases (from environment)">
          {Object.keys(data.model_aliases).length === 0 ? (
            <p className="dim" style={{ margin: 0 }}>No aliases configured.</p>
          ) : (
            <dl className="kv">
              {Object.entries(data.model_aliases).map(([alias, target]) => (
                <div key={alias} style={{ display: "contents" }}>
                  <dt className="mono">{alias}</dt>
                  <dd className="mono">→ {target}</dd>
                </div>
              ))}
            </dl>
          )}
        </Card>

        <Card title="Connect a client">
          <div className="code-block">{`base_url: ${window.location.origin}/v1
api_key:  qwg_…  (create one in API Keys)
model:    ${data.default_model}`}</div>
          <div className="field-hint" style={{ marginTop: 8 }}>
            Full API reference: <a href="/docs" target="_blank" rel="noreferrer">/docs</a>
          </div>
        </Card>
      </div>
    </div>
  );
}
