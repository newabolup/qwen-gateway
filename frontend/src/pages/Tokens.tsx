import { useState } from "react";
import { api } from "../lib/api";
import type { Credential } from "../lib/api";
import {
  ConfirmDialog,
  Empty,
  Modal,
  StatusBadge,
  Toggle,
  formatDate,
  relativeTime,
  useAsync,
  useToast,
} from "../components/ui";

export default function Tokens() {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync(() => api.credentials(), []);
  const [showAdd, setShowAdd] = useState(false);
  const [renaming, setRenaming] = useState<Credential | null>(null);
  const [deleting, setDeleting] = useState<Credential | null>(null);
  const [testing, setTesting] = useState<number | null>(null);

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn();
      toast("ok", ok);
      await reload();
    } catch (e) {
      toast("err", e instanceof Error ? e.message : "Action failed");
    }
  };

  const runTest = async (credential: Credential) => {
    setTesting(credential.id);
    try {
      const result = await api.testCredential(credential.id);
      toast(
        result.healthy ? "ok" : "err",
        result.healthy
          ? `${credential.name}: healthy${result.latency_ms ? ` (${Math.round(result.latency_ms)} ms)` : ""}`
          : `${credential.name}: ${result.detail ?? "unhealthy"}`,
      );
      await reload();
    } catch (e) {
      toast("err", e instanceof Error ? e.message : "Test failed");
    } finally {
      setTesting(null);
    }
  };

  return (
    <div>
      <div className="toolbar">
        <span className="dim">{data?.length ?? 0} credential(s) in the pool</span>
        <div className="spacer" />
        <button className="btn" onClick={() => void reload()}>
          Refresh
        </button>
        <button className="btn primary" onClick={() => setShowAdd(true)}>
          Add token
        </button>
      </div>

      {error && <Empty message={error} />}
      {loading && !data && <Empty message="Loading tokens…" />}

      {data && data.length === 0 && (
        <Empty message="No credentials yet. Add a Qwen token you are authorized to use." />
      )}

      {data && data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Secret</th>
                <th>Mode</th>
                <th>Status</th>
                <th>Requests</th>
                <th>Last used</th>
                <th>Cooldown</th>
                <th>Enabled</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.map((c) => (
                <tr key={c.id}>
                  <td className="mono dim">{c.id}</td>
                  <td>
                    <div>{c.name}</div>
                    {c.status_reason && (
                      <div className="dim" style={{ fontSize: 11.5 }}>
                        {c.status_reason}
                      </div>
                    )}
                  </td>
                  <td className="mono dim">{c.secret_hint}</td>
                  <td>
                    <span className="badge muted">{c.auth_mode}</span>
                  </td>
                  <td>
                    <StatusBadge status={c.enabled ? c.status : "disabled"} />
                  </td>
                  <td className="nowrap">
                    <span className="mono">{c.request_count}</span>
                    {c.failure_count > 0 && (
                      <span className="dim" style={{ fontSize: 11.5 }}>
                        {" "}
                        / {c.failure_count} err
                      </span>
                    )}
                    {c.in_flight > 0 && <span className="badge info" style={{ marginLeft: 6 }}>{c.in_flight} live</span>}
                  </td>
                  <td className="dim nowrap">{relativeTime(c.last_used_at)}</td>
                  <td className="dim nowrap">
                    {c.cooldown_until && new Date(c.cooldown_until) > new Date() ? (
                      <span className="badge warn">until {formatDate(c.cooldown_until)}</span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    <Toggle
                      checked={c.enabled}
                      onChange={(value) =>
                        void act(() => api.updateCredential(c.id, { enabled: value }), `${c.name} ${value ? "enabled" : "disabled"}`)
                      }
                    />
                  </td>
                  <td className="nowrap">
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                      <button className="btn sm" disabled={testing === c.id} onClick={() => void runTest(c)}>
                        {testing === c.id ? "Testing…" : "Test"}
                      </button>
                      <button className="btn sm" onClick={() => setRenaming(c)}>
                        Edit
                      </button>
                      {c.cooldown_until && (
                        <button
                          className="btn sm"
                          onClick={() =>
                            void act(() => api.updateCredential(c.id, { clear_cooldown: true }), "Cooldown cleared")
                          }
                        >
                          Clear cooldown
                        </button>
                      )}
                      <button className="btn sm danger" onClick={() => setDeleting(c)}>
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showAdd && (
        <AddTokenModal
          onClose={() => setShowAdd(false)}
          onCreated={async () => {
            setShowAdd(false);
            toast("ok", "Credential stored (encrypted at rest)");
            await reload();
          }}
        />
      )}

      {renaming && (
        <EditTokenModal
          credential={renaming}
          onClose={() => setRenaming(null)}
          onSaved={async () => {
            setRenaming(null);
            toast("ok", "Credential updated");
            await reload();
          }}
        />
      )}

      {deleting && (
        <ConfirmDialog
          title="Delete credential"
          message={`Delete "${deleting.name}"? In-flight requests using it may fail. This cannot be undone.`}
          confirmLabel="Delete"
          onCancel={() => setDeleting(null)}
          onConfirm={() => {
            const target = deleting;
            setDeleting(null);
            void act(() => api.deleteCredential(target.id), "Credential deleted");
          }}
        />
      )}
    </div>
  );
}

function AddTokenModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [secret, setSecret] = useState("");
  const [refresh, setRefresh] = useState("");
  const [mode, setMode] = useState("auto");
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!name.trim() || secret.trim().length < 8) {
      toast("err", "Provide a name and a valid token");
      return;
    }
    setBusy(true);
    try {
      await api.createCredential({
        name: name.trim(),
        secret: secret.trim(),
        refresh_secret: refresh.trim() || undefined,
        auth_mode: mode,
        base_url: baseUrl.trim() || undefined,
      });
      onCreated();
    } catch (e) {
      toast("err", e instanceof Error ? e.message : "Could not save credential");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="Add Qwen credential"
      description="Only add credentials you own or are authorized to use. The value is encrypted at rest and never shown again."
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn primary" onClick={() => void submit()} disabled={busy}>
            {busy ? "Saving…" : "Save credential"}
          </button>
        </>
      }
    >
      <div className="field">
        <label>Name</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="Account 1" />
      </div>
      <div className="field">
        <label>Token / session secret</label>
        <textarea value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="Paste your own Qwen token" />
        <div className="field-hint">
          Portal mode: an OAuth access token from your own Qwen login. Web mode: your chat.qwen.ai session token.
        </div>
      </div>
      <div className="field">
        <label>Auth mode</label>
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="auto">Auto-detect</option>
          <option value="portal">Portal (Bearer token)</option>
          <option value="web">Web (session cookie)</option>
        </select>
      </div>
      <div className="field">
        <label>Refresh token (optional)</label>
        <input type="password" value={refresh} onChange={(e) => setRefresh(e.target.value)} placeholder="Enables automatic refresh" />
      </div>
      <div className="field">
        <label>Base URL override (optional)</label>
        <input type="text" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://portal.qwen.ai/v1" />
      </div>
    </Modal>
  );
}

function EditTokenModal({
  credential,
  onClose,
  onSaved,
}: {
  credential: Credential;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(credential.name);
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await api.updateCredential(credential.id, {
        name: name.trim() || undefined,
        secret: secret.trim() || undefined,
      });
      onSaved();
    } catch (e) {
      toast("err", e instanceof Error ? e.message : "Update failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`Edit "${credential.name}"`}
      description="Leave the secret blank to keep the current one. Existing secrets are never displayed."
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn primary" onClick={() => void submit()} disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      <div className="field">
        <label>Name</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="field">
        <label>Rotate secret (optional)</label>
        <textarea value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="Paste a new token to rotate" />
      </div>
      <div className="field">
        <label>Current secret</label>
        <div className="mono dim">{credential.secret_hint}</div>
      </div>
    </Modal>
  );
}
