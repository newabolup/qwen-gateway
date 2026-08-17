#!/usr/bin/env bash
# One-time setup: virtualenv, dependencies, frontend build, .env scaffold.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"

echo "==> Creating virtual environment (.venv)"
[ -d .venv ] || "$PYTHON" -m venv .venv

echo "==> Installing Python dependencies"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements-dev.txt

if [ ! -f .env ]; then
  echo "==> Creating .env from .env.example"
  cp .env.example .env
  KEY="$(./.venv/bin/python -m app.cli generate-key)"
  PASS="$(./.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(18))')"
  # Portable in-place edit (GNU and BSD sed).
  sed -i.bak "s|^GATEWAY_SECRET_KEY=.*|GATEWAY_SECRET_KEY=${KEY}|" .env
  sed -i.bak "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${PASS}|" .env
  rm -f .env.bak
  chmod 600 .env
  echo "    Generated GATEWAY_SECRET_KEY and ADMIN_PASSWORD in .env"
  echo "    Admin password: ${PASS}"
else
  echo "==> .env already exists (left untouched)"
fi

if command -v npm >/dev/null 2>&1; then
  echo "==> Building the admin UI"
  npm --prefix frontend install --no-audit --no-fund --silent
  npm --prefix frontend run build --silent
else
  echo "==> npm not found; skipping UI build (the API still works)"
fi

mkdir -p data
echo
echo "Setup complete. Start the gateway with: ./scripts/start.sh"
