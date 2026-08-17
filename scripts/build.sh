#!/usr/bin/env bash
# Build the admin UI for production (served by the backend at /).
set -euo pipefail
cd "$(dirname "$0")/.."
npm --prefix frontend install --no-audit --no-fund
npm --prefix frontend run build
echo "Built to frontend/dist — the backend serves it automatically."
