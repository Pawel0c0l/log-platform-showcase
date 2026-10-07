#!/usr/bin/env bash
# Bring up an isolated demo stack: Postgres, MinIO, API and portal, with two
# synthetic clients onboarded through the repository's own onboarding script.
#
# Requirements: Docker with the Compose plugin and Python 3. Host ports used:
# 8010 (API and portal), 5433 (Postgres), 9010/9011 (MinIO).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

export COMPOSE_PROJECT_NAME=logplatform-demo
ENV_FILE="demo/.env.demo"
COMPOSE=(docker compose --env-file "${ENV_FILE}" -f docker-compose.yml -f demo/docker-compose.demo.yml)
ADMIN_PASSWORD="${PORTAL_ADMIN_PASSWORD:-demo-admin}"

psql_demo() {
  "${COMPOSE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" "$@"
}

echo "== environment"
python3 demo/make_env.py
set -a; # shellcheck disable=SC1090
source "${ENV_FILE}"; set +a
mkdir -p "${REPORTS_DATA_DIR}"

echo "== containers"
"${COMPOSE[@]}" up -d --build

echo "== waiting for postgres"
for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T postgres pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "== waiting for the API (it creates the base tables on startup)"
for _ in $(seq 1 45); do
  if curl -fsS -m 3 http://127.0.0.1:8010/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS -m 3 http://127.0.0.1:8010/health >/dev/null || {
  echo "API did not become healthy; last log lines:" >&2
  "${COMPOSE[@]}" logs --tail 30 api >&2
  exit 1
}

# ops/db_migrate.sh finds the Postgres container through the Compose project
# name exported above and applies db/migrations/*.sql exactly once each. Two
# migrations are gated on state that only exists on a provisioned platform, so
# on a fresh database the pass stops twice, the missing state is created the
# way production creates it, and the pass resumes.
echo "== platform migrations, pass 1 (stops at the environment identity guard)"
ops/db_migrate.sh || true

echo "== environment identity (local_dev)"
# The platform marker row that ops/provision_local_environment_identity.py
# writes; that script also insists on a client-business identity, which the
# demo provisions through onboarding instead.
psql_demo <<SQL
INSERT INTO ops_control.environment_identity
  (identity_key, environment, database_identity_id, database_role, database_name, client_code, provisioned_by, notes)
VALUES
  ('primary', 'local_dev', '${LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID}'::uuid, 'platform', '${POSTGRES_DB}', NULL, 'demo/up.sh', 'demo stack')
ON CONFLICT (identity_key) DO NOTHING;
SQL

echo "== platform migrations, pass 2 (stops at migration 060)"
ops/db_migrate.sh || true

# 060 rewrites the schedule of one production client and asserts on its state.
# A fresh database has no such client and nothing to rewrite, so it is recorded
# as applied. The onboarding script below targets the schema as it stands after
# every migration, which is why clients cannot be created before this point.
echo "== migration 060 (production data migration, no-op on a fresh database)"
psql_demo -c "INSERT INTO public.schema_migrations(filename) VALUES ('060_workflow_a_daily_trips_lookback_l3.sql') ON CONFLICT DO NOTHING;"

echo "== platform migrations, pass 3 (stops at the client assertion that ends 072)"
ops/db_migrate.sh || true

echo "== synthetic clients (scripts/onboard_workflow_a_client.py)"
# Secrets are per-run throwaways; the provider is never contacted.
for key in alpha bravo; do
  upper="$(echo "${key}" | tr '[:lower:]' '[:upper:]')"
  "${COMPOSE[@]}" run --rm -T \
    -e "${upper}_API_USERNAME=demo" \
    -e "${upper}_API_KEY=demo-not-a-credential" \
    -e "${upper}_DB_USERNAME=${key}_user" \
    -e "${upper}_DB_KEY=${key}-$(python3 -c 'import secrets; print(secrets.token_hex(8))')" \
    tools scripts/onboard_workflow_a_client.py \
      --config "demo/clients/${key}.yaml" --apply --skip-provider-auth-check \
    | grep -E 'onboarding_state|client_code|ERROR|Traceback' || true
done

# 072 changed a constraint (already applied above) and then asserts that both
# production clients carry the 32-day recovery span. The demo clients were
# created a moment ago without one, so the value the migration enforces is set
# here and the migration is recorded as applied once the assertion holds.
echo "== migration 072 data half for the demo clients"
psql_demo <<'SQL'
UPDATE workflow_a_control.client_account
   SET trips_max_recovery_span_seconds = 2768400
 WHERE client_code IN ('ALPHA00001', 'BRAVO00016')
   AND (trips_max_recovery_span_seconds IS NULL OR trips_max_recovery_span_seconds < 2768400);
DO $$
DECLARE adopted INTEGER;
BEGIN
  SELECT count(*) INTO adopted FROM workflow_a_control.client_account
   WHERE client_code IN ('ALPHA00001', 'BRAVO00016') AND trips_max_recovery_span_seconds = 2768400;
  IF adopted <> 2 THEN
    RAISE EXCEPTION 'demo: expected both synthetic clients onboarded, found %', adopted;
  END IF;
END $$;
INSERT INTO public.schema_migrations(filename) VALUES ('072_workflow_a_trips_monthly_recovery_span.sql') ON CONFLICT DO NOTHING;
SQL

echo "== platform migrations, pass 4 (must report nothing left to apply)"
ops/db_migrate.sh

echo "== portal administrator"
"${COMPOSE[@]}" run --rm -T -e "PORTAL_ADMIN_PASSWORD=${ADMIN_PASSWORD}" \
  tools scripts/bootstrap_portal_admin.py --username admin --display-name "Demo Admin"

echo
echo "API health:    http://127.0.0.1:8010/health"
echo "Portal login:  http://127.0.0.1:8010/login   (admin / ${ADMIN_PASSWORD})"
echo "MinIO console: http://127.0.0.1:9011         (credentials in demo/.env.demo)"
echo "Stop and remove everything: ./demo/down.sh"
