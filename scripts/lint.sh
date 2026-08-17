#!/usr/bin/env bash
# Lint Python and type-check the frontend.
set -euo pipefail
cd "$(dirname "$0")/.."
./.venv/bin/ruff check app tests
[ -d frontend/node_modules ] && npm --prefix frontend run lint
