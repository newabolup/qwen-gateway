import { useState } from "react";
import { api } from "../lib/api";
import type { ModelEntry } from "../lib/api";
import { ConfirmDialog, Empty, Modal, StatusBadge, useAsync, useToast } from "../components/ui";

export default function Models() {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync(() => api.models(), []);
  const [editing, setEditing] = useState<ModelEntry | "new" | null>(null);
  const [deleting, setDeleting] = useState<ModelEntry | null>(null);
  const [discovering, setDiscovering] = useState(false);

  const discover = async () => {
    setDiscovering(true);
    try {
      const models = await api.discoverModels();
      toast("ok", `Discovered ${models.length} model(s)`);
      await reload();
    } catch (e) {
      toast("err", e instanceof Error ? e.message : "Discovery failed");
    } finally {
      setDiscovering(false);
    }
  };

  return (
    <div>
      <div className="toolbar">
        <span className="dim">Aliases let clients request e.g. "qwen" and reach a real upstream model.</span>
        <div className="spacer" />
        <button className="btn" disabled={discovering} onClick={() => void discover()}>
          {discovering ? "Discovering…" : "Discover upstream"}
        </button>
        <button className="btn primary" onClick={() => setEditing("new")}>
          Add model
        </button>
      </div>

      {error && <Empty message={error} />}
      {loading && !data && <Empty message="Loading models…" />}
      {data && data.length === 0 && <Empty message="No models configured." />}

      {data && data.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Model ID</th>
                <th>Display name</th>
                <th>Provider</th>
                <th>Aliases</th>
                <th>Context</th>
                <th>Capabilities</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.map((m) => (
                <tr key={m.id}>
                  <td className="mono">{m.model_id}</td>
                  <td>{m.display_name ?? "—"}</td>
                  <td>
                    <span className="badge muted">{m.provider}</span>
                  </td>
                  <td className="mono dim">{m.aliases.length ? m.aliases.join(", ") : "—"}</td>
                  <td className="dim mono">{m.context_window ? m.context_window.toLocaleString() : "—"}</td>
                  <td>
                    <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
                      {m.supports_tools && <span className="badge info">tools</span>}
                      {m.supports_reasoning && <span className="badge info">reasoning</span>}
                    </div>
                  </td>
                  <td>
                    <StatusBadge status={m.enabled ? "healthy" : "disabled"} />
                  </td>
                  <td className="nowrap">
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                      <button className="btn sm" onClick={() => setEditing(m)}>
                        Edit
                      </button>
                      <button className="btn sm danger" onClick={() => setDeleting(m)}>
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

      {editing && (
        <ModelModal
          entry={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            toast("ok", "Model saved");
            await reload();
          }}
        />
      )}

      {deleting && (
        <ConfirmDialog
          title="Delete model"
          message={`Remove "${deleting.model_id}" from the catalogue? Clients requesting it will fall back to the default model.`}
          confirmLabel="Delete"
          onCancel={() => setDeleting(null)}
          onConfirm={async () => {
            const target = deleting;
            setDeleting(null);
            try {
              await api.deleteModel(target.id);
              toast("ok", "Model deleted");
              await reload();
            } catch (e) {
              toast("err", e instanceof Error ? e.message : "Delete failed");
            }
          }}
        />
      )}
    </div>
  );
}

function ModelModal({
  entry,
  onClose,
  onSaved,
}: {
  entry: ModelEntry | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [modelId, setModelId] = useState(entry?.model_id ?? "");
  const [displayName, setDisplayName] = useState(entry?.display_name ?? "");
  const [aliases, setAliases] = useState((entry?.aliases ?? []).join(", "));
  const [enabled, setEnabled] = useState(entry?.enabled ?? true);
  const [tools, setTools] = useState(entry?.supports_tools ?? true);
  const [reasoning, setReasoning] = useState(entry?.supports_reasoning ?? false);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!modelId.trim()) {
      toast("err", "Model ID is required");
      return;
    }
    setBusy(true);
    try {
      await api.upsertModel({
        model_id: modelId.trim(),
        display_name: displayName.trim() || undefined,
        aliases: aliases
          .split(",")
          .map((a) => a.trim())
          .filter(Boolean),
        enabled,
        provider: entry?.provider ?? undefined,
        supports_tools: tools,
        supports_reasoning: reasoning,
      });
      onSaved();
    } catch (e) {
      toast("err", e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={entry ? `Edit ${entry.model_id}` : "Add model"}
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
        <label>Model ID (upstream)</label>
        <input type="text" value={modelId} onChange={(e) => setModelId(e.target.value)} placeholder="qwen3-max" disabled={!!entry} />
      </div>
      <div className="field">
        <label>Display name</label>
        <input type="text" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
      </div>
      <div className="field">
        <label>Aliases (comma separated)</label>
        <input type="text" value={aliases} onChange={(e) => setAliases(e.target.value)} placeholder="qwen, qwen-default" />
      </div>
      <div className="field" style={{ display: "flex", gap: 18, flexWrap: "wrap" }}>
        <label className="switch">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <span>Enabled</span>
        </label>
        <label className="switch">
          <input type="checkbox" checked={tools} onChange={(e) => setTools(e.target.checked)} />
          <span>Tools</span>
        </label>
        <label className="switch">
          <input type="checkbox" checked={reasoning} onChange={(e) => setReasoning(e.target.checked)} />
          <span>Reasoning</span>
        </label>
      </div>
    </Modal>
  );
}
