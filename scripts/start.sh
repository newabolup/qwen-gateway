#!/usr/bin/env bash
# Start the gateway (production-style, single process).
set -euo pipefail
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -m app
