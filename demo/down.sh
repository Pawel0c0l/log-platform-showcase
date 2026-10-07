#!/usr/bin/env bash
# Stop the demo stack and remove its containers, volumes (including the tools
# virtualenv volume) and image. demo/.env.demo is kept so a later up.sh reuses
# the same secrets; delete it by hand for a clean slate.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

export COMPOSE_PROJECT_NAME=logplatform-demo
ENV_FILE="demo/.env.demo"
[[ -f "${ENV_FILE}" ]] || ENV_FILE=".env.example"
docker compose --env-file "${ENV_FILE}" --profile tools \
  -f docker-compose.yml -f demo/docker-compose.demo.yml down -v --rmi local --remove-orphans
echo "demo stack removed"
