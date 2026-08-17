import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Empty, useAsync } from "../components/ui";

export default function Logs() {
  const [level, setLevel] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const { data, loading, error, reload } = useAsync(() => api.logs(level || undefined), [level]);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = setInterval(() => void reload(), 5000);
    return () => clearInterval(timer);
  }, [autoRefresh, reload]);

  return (
    <div>
      <div className="toolbar">
        <select value={level} onChange={(e) => setLevel(e.target.value)} style={{ width: 160 }}>
          <option value="">All levels</option>
          <option value="DEBUG">Debug</option>
          <option value="INFO">Info</option>
          <option value="WARNING">Warning</option>
          <option value="ERROR">Error</option>
        </select>
        <label className="switch">
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
          <span>Auto-refresh</span>
        </label>
        <div className="spacer" />
        <span className="dim" style={{ fontSize: 12 }}>
          Secrets are redacted before logs are written.
        </span>
        <button className="btn" onClick={() => void reload()}>
          Refresh
        </button>
      </div>

      {error && <Empty message={error} />}
      {loading && !data && <Empty message="Loading logs…" />}
      {data && data.length === 0 && <Empty message="No log entries." />}

      {data && data.length > 0 && (
        <div className="table-wrap" style={{ maxHeight: "70vh", overflowY: "auto" }}>
          {data.map((entry, index) => (
            <div className="log-line" key={`${entry.ts}-${index}`}>
              <span className="log-ts">{entry.ts}</span>
              <span className={`log-level ${entry.level}`}>{entry.level}</span>
              <span>{entry.event}</span>
              {entry.request_id && entry.request_id !== "-" && (
                <span className="log-extra">[{entry.request_id}]</span>
              )}
              {Object.keys(entry.extra).length > 0 && (
                <span className="log-extra">
                  {Object.entries(entry.extra)
                    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
                    .join(" ")}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
