#!/usr/bin/env bash
set -euo pipefail

URL="${LOG_API_URL:-http://127.0.0.1:8000}"

if ! health="$(curl -fsS "$URL/health")"; then
  echo "ERROR: API health check failed: $URL/health" >&2
  exit 1
fi

echo "HEALTH: $health"
echo "OK: smoke test passed"
