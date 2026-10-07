#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MIGRATIONS_DIR="${REPO_ROOT}/db/migrations"

POSTGRES_USER_VALUE="${POSTGRES_USER:-loguser}"
POSTGRES_DB_VALUE="${POSTGRES_DB:-logdb}"

if [[ ! -d "${MIGRATIONS_DIR}" ]]; then
  echo "ERROR: Missing migrations directory: ${MIGRATIONS_DIR}" >&2
  exit 1
fi

cd "${REPO_ROOT}"

container_id="$(docker compose ps -q postgres)"
if [[ -z "${container_id}" ]]; then
  echo "ERROR: Postgres container is not running (docker compose ps -q postgres returned empty)." >&2
  exit 1
fi

psql_exec() {
  docker exec -i "${container_id}" psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER_VALUE}" -d "${POSTGRES_DB_VALUE}" "$@"
}

psql_exec <<'SQL'
CREATE TABLE IF NOT EXISTS public.schema_migrations(
  filename TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
SQL

mapfile -t migration_files < <(find "${MIGRATIONS_DIR}" -maxdepth 1 -type f -name '*.sql' | sort)

if [[ "${#migration_files[@]}" -eq 0 ]]; then
  echo "No migration files found in ${MIGRATIONS_DIR}."
  exit 0
fi

for migration_path in "${migration_files[@]}"; do
  filename="$(basename "${migration_path}")"
  filename_sql="${filename//\'/\'\'}"

  already_applied="$(psql_exec -tA -c "SELECT 1 FROM public.schema_migrations WHERE filename = '${filename_sql}' LIMIT 1;")"

  if [[ "${already_applied}" == "1" ]]; then
    echo "SKIP  ${filename}"
    continue
  fi

  echo "APPLY ${filename}"
  docker exec -i "${container_id}" psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER_VALUE}" -d "${POSTGRES_DB_VALUE}" < "${migration_path}"

  psql_exec -c "INSERT INTO public.schema_migrations(filename) VALUES ('${filename_sql}');"
done

echo "Migrations complete."
