#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="/opt/log-platform"
readonly PYTHON_BIN="$REPO_ROOT/.venv/bin/python"

cd "$REPO_ROOT"
exec "$PYTHON_BIN" -m api.platform_prune "$@"
