import { useCallback, useEffect, useState } from "react";
import { api } from "./lib/api";
import { ToastProvider, useToast } from "./components/ui";
import Dashboard from "./pages/Dashboard";
import Tokens from "./pages/Tokens";
import ApiKeys from "./pages/ApiKeys";
import Models from "./pages/Models";
import Requests from "./pages/Requests";
import Logs from "./pages/Logs";
import Settings from "./pages/Settings";

type Page = "dashboard" | "tokens" | "keys" | "models" | "requests" | "logs" | "settings";

const NAV: Array<{ id: Page; label: string; icon: string }> = [
  { id: "dashboard", label: "Dashboard", icon: "◆" },
  { id: "tokens", label: "Tokens", icon: "⬢" },
  { id: "keys", label: "API Keys", icon: "⚿" },
  { id: "models", label: "Models", icon: "⚙" },
  { id: "requests", label: "Requests", icon: "≡" },
  { id: "logs", label: "Logs", icon: "▤" },
  { id: "settings", label: "Settings", icon: "✧" },
];

function useTheme() {
  const [dark, setDark] = useState(() => localStorage.getItem("qwg-theme") !== "light");
  useEffect(() => {
    document.documentElement.classList.toggle("light", !dark);
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("qwg-theme", dark ? "dark" : "light");
  }, [dark]);
  return { dark, toggle: () => setDark((d) => !d) };
}

function Login({ onSuccess, configured }: { onSuccess: () => void; configured: boolean }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username, password);
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="brand" style={{ padding: "0 0 16px" }}>
          <div className="brand-mark">Q</div>
          <div>
            <div className="brand-text">Qwen Token Gateway</div>
            <div className="brand-sub">Admin console</div>
          </div>
        </div>
        {!configured ? (
          <p style={{ color: "var(--warn)" }}>
            Admin access is not configured. Set <span className="mono">ADMIN_USERNAME</span> and{" "}
            <span className="mono">ADMIN_PASSWORD</span> in the environment, then restart the gateway.
          </p>
        ) : (
          <form onSubmit={submit}>
            <div className="field">
              <label>Username</label>
              <input type="text" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
            </div>
            <div className="field">
              <label>Password</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </div>
            {error && (
              <div className="badge err" style={{ marginBottom: 12 }}>
                {error}
              </div>
            )}
            <button className="btn primary" type="submit" disabled={busy} style={{ width: "100%", justifyContent: "center" }}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}

function Shell() {
  const toast = useToast();
  const theme = useTheme();
  const [page, setPage] = useState<Page>("dashboard");
  const [auth, setAuth] = useState<{ authenticated: boolean; username: string | null; configured: boolean } | null>(
    null,
  );

  const checkSession = useCallback(async () => {
    try {
      const info = await api.session();
      setAuth({ authenticated: info.authenticated, username: info.username, configured: info.admin_configured });
    } catch {
      setAuth({ authenticated: false, username: null, configured: true });
    }
  }, []);

  useEffect(() => {
    void checkSession();
  }, [checkSession]);

  if (!auth) return <div className="login-wrap">Loading…</div>;
  if (!auth.authenticated) return <Login configured={auth.configured} onSuccess={() => void checkSession()} />;

  const title = NAV.find((n) => n.id === page)?.label ?? "Dashboard";

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">Q</div>
          <div>
            <div className="brand-text">Qwen Gateway</div>
            <div className="brand-sub">OpenAI-compatible</div>
          </div>
        </div>
        {NAV.map((item) => (
          <button
            key={item.id}
            className={`nav-item ${page === item.id ? "active" : ""}`}
            onClick={() => setPage(item.id)}
          >
            <span aria-hidden>{item.icon}</span>
            {item.label}
          </button>
        ))}
        <div className="nav-spacer" />
        <a className="nav-item" href="/docs" target="_blank" rel="noreferrer">
          <span aria-hidden>◈</span> API docs
        </a>
      </aside>

      <main className="main">
        <header className="topbar">
          <h1>{title}</h1>
          <div className="topbar-actions">
            <span className="dim" style={{ fontSize: 12.5 }}>
              {auth.username}
            </span>
            <button className="btn sm" onClick={theme.toggle} title="Toggle theme">
              {theme.dark ? "☾" : "☀"}
            </button>
            <button
              className="btn sm"
              onClick={async () => {
                await api.logout();
                toast("info", "Signed out");
                void checkSession();
              }}
            >
              Sign out
            </button>
          </div>
        </header>

        <div className="content">
          {page === "dashboard" && <Dashboard />}
          {page === "tokens" && <Tokens />}
          {page === "keys" && <ApiKeys />}
          {page === "models" && <Models />}
          {page === "requests" && <Requests />}
          {page === "logs" && <Logs />}
          {page === "settings" && <Settings />}
        </div>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  );
}
