import { useState } from "react";
import { api } from "../lib/api";
import type { ApiKey } from "../lib/api";
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

export default function ApiKeys() {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync(() => api.apiKeys(), []);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [deleting, setDeleting] = useState<ApiKey | null>(null);

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn();
      toast("ok", ok);
      await reload();
    } catch (e) {
      toast("err", e instanceof Error ? e.message : "Action failed");
    }
  };

  return (
    <div>
      <div className="toolbar">
        <span className="dim">Clients authenticate with these keys — they never see the Qwen credential.</span>
        <div className="spacer" />
        <button className="btn" onClick={() => void reload()}>
          Refresh
        </button>
        <button className="btn primary" onClick={() => setCreating(true)}>
          Create API key
        </button>
      </div>

      {error && <Empty message={error} />}
      {loading && !data && <Empty message="Loading API keys…" />}
      {data && data.length === 0 && <Empty message="No API keys yet. Create one to let a client connect." />}

      {data && data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Key</th>
                <th>Status</th>
                <th>Requests</th>
                <th>Last used</th>
                <th>Expires</th>
                <th>Enabled</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.map((k) => (
                <tr key={k.id}>
                  <td className="mono dim">{k.id}</td>
                  <td>
                    <div>{k.name}</div>
                    {k.description && (
                      <div className="dim" style={{ fontSize: 11.5 }}>
                        {k.description}
                      </div>
                    )}
                  </td>
                  <td className="mono dim">{k.key_preview}</td>
                  <td>
                    <StatusBadge status={k.revoked ? "invalid" : k.enabled ? "healthy" : "disabled"} />
                  </td>
                  <td className="mono">
                    {k.request_count}
                    {k.failure_count > 0 && <span className="dim"> / {k.failure_count} err</span>}
                  </td>
                  <td className="dim nowrap">{relativeTime(k.last_used_at)}</td>
                  <td className="dim nowrap">{k.expires_at ? formatDate(k.expires_at) : "never"}</td>
                  <td>
                    <Toggle
                      checked={k.enabled && !k.revoked}
                      onChange={(value) =>
                        void act(() => api.updateApiKey(k.id, { enabled: value }), `Key ${value ? "enabled" : "disabled"}`)
                      }
                    />
                  </td>
                  <td className="nowrap">
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                      {!k.revoked && (
                        <button className="btn sm" onClick={() => setRevoking(k)}>
                          Revoke
                        </button>
                      )}
                      <button className="btn sm danger" onClick={() => setDeleting(k)}>
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

      {creating && (
        <CreateKeyModal
          onClose={() => setCreating(false)}
          onCreated={async (plaintext) => {
            setCreating(false);
            setCreated(plaintext);
            await reload();
          }}
        />
      )}

      {created && (
        <Modal
          title="API key created"
          description="Copy it now — this is the only time it will be shown."
          onClose={() => setCreated(null)}
          actions={
            <>
              <button
                className="btn"
                onClick={() => {
                  void navigator.clipboard?.writeText(created);
                  toast("ok", "Copied to clipboard");
                }}
              >
                Copy
              </button>
              <button className="btn primary" onClick={() => setCreated(null)}>
                Done
              </button>
            </>
          }
        >
          <div className="secret-reveal">{created}</div>
          <div className="field-hint">
            Use it as <span className="mono">Authorization: Bearer {created.slice(0, 12)}…</span>
          </div>
        </Modal>
      )}

      {revoking && (
        <ConfirmDialog
          title="Revoke API key"
          message={`Revoke "${revoking.name}"? Any client using it will immediately stop working.`}
          confirmLabel="Revoke"
          onCancel={() => setRevoking(null)}
          onConfirm={() => {
            const target = revoking;
            setRevoking(null);
            void act(() => api.revokeApiKey(target.id), "API key revoked");
          }}
        />
      )}

      {deleting && (
        <ConfirmDialog
          title="Delete API key"
          message={`Permanently delete "${deleting.name}"? Its usage history stays in the request log.`}
          confirmLabel="Delete"
          onCancel={() => setDeleting(null)}
          onConfirm={() => {
            const target = deleting;
            setDeleting(null);
            void act(() => api.deleteApiKey(target.id), "API key deleted");
          }}
        />
      )}
    </div>
  );
}

function CreateKeyModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (plaintext: string) => void;
}) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [expires, setExpires] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!name.trim()) {
      toast("err", "Name is required");
      return;
    }
    setBusy(true);
    try {
      const result = await api.createApiKey({
        name: name.trim(),
        description: description.trim() || undefined,
        expires_in_days: expires ? Number(expires) : undefined,
      });
      onCreated(result.api_key);
    } catch (e) {
      toast("err", e instanceof Error ? e.message : "Could not create key");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="Create API key"
      description="Only a hash is stored; the plaintext is shown once."
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn primary" onClick={() => void submit()} disabled={busy}>
            {busy ? "Creating…" : "Create"}
          </button>
        </>
      }
    >
      <div className="field">
        <label>Name</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="Claude Code" />
      </div>
      <div className="field">
        <label>Description (optional)</label>
        <input type="text" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Laptop CLI" />
      </div>
      <div className="field">
        <label>Expires in days (optional)</label>
        <input type="number" min={1} value={expires} onChange={(e) => setExpires(e.target.value)} placeholder="never" />
      </div>
    </Modal>
  );
}
