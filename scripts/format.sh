#!/usr/bin/env bash
# Auto-format and auto-fix.
set -euo pipefail
cd "$(dirname "$0")/.."
./.venv/bin/ruff format app tests
./.venv/bin/ruff check --fix app tests
