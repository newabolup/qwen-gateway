import { useState } from "react";
import { api } from "../lib/api";
import { Empty, StatusBadge, formatDate, useAsync, useToast } from "../components/ui";

export default function Requests() {
  const toast = useToast();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");

  const { data, loading, error, reload } = useAsync(
    () => api.requests({ page, page_size: 50, status: status || undefined, search: query || undefined }),
    [page, status, query],
  );

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div>
      <div className="toolbar">
        <input
          className="search"
          type="text"
          placeholder="Search request id, model, error…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setQuery(search);
              setPage(1);
            }
          }}
        />
        <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} style={{ width: 150 }}>
          <option value="">All statuses</option>
          <option value="success">Success</option>
          <option value="error">Error</option>
        </select>
        <button className="btn" onClick={() => { setQuery(search); setPage(1); }}>
          Search
        </button>
        <div className="spacer" />
        <button className="btn" onClick={() => void reload()}>
          Refresh
        </button>
        <button
          className="btn"
          onClick={async () => {
            try {
              const result = await api.purgeRequests();
              toast("ok", result.detail);
              await reload();
            } catch (e) {
              toast("err", e instanceof Error ? e.message : "Purge failed");
            }
          }}
        >
          Purge old
        </button>
      </div>

      {error && <Empty message={error} />}
      {loading && !data && <Empty message="Loading requests…" />}
      {data && data.items.length === 0 && <Empty message="No requests recorded yet." />}

      {data && data.items.length > 0 && (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Request ID</th>
                  <th>Model</th>
                  <th>API key</th>
                  <th>Token</th>
                  <th>Mode</th>
                  <th>Status</th>
                  <th>Latency</th>
                  <th>Tokens</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((r) => (
                  <tr key={r.id}>
                    <td className="dim nowrap">{formatDate(r.created_at)}</td>
                    <td className="mono dim" title={r.request_id}>
                      {r.request_id.slice(0, 18)}…
                    </td>
                    <td>
                      <div className="mono">{r.model}</div>
                      {r.upstream_model && r.upstream_model !== r.model && (
                        <div className="dim" style={{ fontSize: 11 }}>
                          → {r.upstream_model}
                        </div>
                      )}
                    </td>
                    <td className="dim">{r.api_key_name ?? "—"}</td>
                    <td className="dim">
                      {r.credential_name ? `${r.credential_name} (#${r.credential_id})` : "—"}
                      {r.attempts > 1 && <span className="badge warn" style={{ marginLeft: 6 }}>{r.attempts} tries</span>}
                    </td>
                    <td>
                      <span className="badge muted">{r.streaming ? "stream" : "sync"}</span>
                    </td>
                    <td>
                      <StatusBadge status={r.status} />
                      {r.error_category && (
                        <div className="dim" style={{ fontSize: 11 }} title={r.error_message ?? ""}>
                          {r.error_category}
                        </div>
                      )}
                    </td>
                    <td className="mono dim nowrap">
                      {r.latency_ms ? `${Math.round(r.latency_ms)} ms` : "—"}
                      {r.first_token_ms && (
                        <div style={{ fontSize: 11 }}>ttfb {Math.round(r.first_token_ms)} ms</div>
                      )}
                    </td>
                    <td className="mono dim">{r.total_tokens ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="pagination">
            <span className="dim">
              {data.total} result(s) · page {data.page} of {totalPages}
            </span>
            <button className="btn sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              Previous
            </button>
            <button className="btn sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
              Next
            </button>
          </div>
        </>
      )}
    </div>
  );
}
