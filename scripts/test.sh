#!/usr/bin/env bash
# Run the test suite (no Qwen credentials required).
set -euo pipefail
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -m pytest "$@"
