#!/usr/bin/env bash
# Development mode: auto-reloading backend on :8787 and Vite UI on :5173.
set -euo pipefail
cd "$(dirname "$0")/.."

cleanup() { jobs -p | xargs -r kill 2>/dev/null || true; }
trap cleanup EXIT INT TERM

./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8787}" --reload &

if [ -d frontend/node_modules ]; then
  npm --prefix frontend run dev &
  echo "Admin UI (dev): http://localhost:5173"
else
  echo "frontend/node_modules missing; run: npm --prefix frontend install"
fi

echo "API: http://localhost:${PORT:-8787}  |  Docs: http://localhost:${PORT:-8787}/docs"
wait
