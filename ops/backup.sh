#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_DIR="$REPO_ROOT/backups"
DOCKER=(docker)
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
COMMAND="${1:-create}"
LOCK_EXIT=75
MIN_FREE_BYTES=$((12 * 1024 * 1024 * 1024))

if [[ "${BACKUP_TEST_MODE:-0}" == "1" ]]; then
  BACKUP_DIR="${BACKUP_TEST_DIR:?BACKUP_TEST_DIR is required}"
  TIMESTAMP="${BACKUP_TEST_TIMESTAMP:?BACKUP_TEST_TIMESTAMP is required}"
  DOCKER=("${BACKUP_TEST_DOCKER:?BACKUP_TEST_DOCKER is required}")
  MIN_FREE_BYTES=0
fi

require_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing dependency: $1" >&2; exit 1; }; }
for command_name in flock gzip tar find sha256sum stat mv python3; do require_cmd "$command_name"; done
"${DOCKER[@]}" compose version >/dev/null

mkdir -p "$BACKUP_DIR"
[[ ! -L "$BACKUP_DIR" ]] || { echo "ERROR: backup directory must not be a symlink" >&2; exit 1; }
[[ "$(stat -c %U "$BACKUP_DIR")" == "$(id -un)" ]] || { echo "ERROR: backup directory owner mismatch" >&2; exit 1; }
chmod 0700 "$BACKUP_DIR"

LOCK_FILE="$BACKUP_DIR/.backup.lock"
exec 9>"$LOCK_FILE"
chmod 0600 "$LOCK_FILE"
if ! flock -n 9; then
  echo "ERROR: another platform backup is active" >&2
  exit "$LOCK_EXIT"
fi

verify_pair() {
  local stamp="$1"
  local manifest="$BACKUP_DIR/backup_${stamp}.manifest.json"
  [[ -f "$manifest" && ! -L "$manifest" ]] || { echo "ERROR: manifest not found" >&2; return 1; }
  gzip -t "$BACKUP_DIR/postgres_${stamp}.sql.gz"
  tar -tzf "$BACKUP_DIR/minio_${stamp}.tar.gz" >/dev/null
  python3 "$SCRIPT_DIR/backup_manifest.py" verify --manifest "$manifest" --timestamp "$stamp" \
    --expected-environment "$expected_environment" --expected-database "$expected_database" \
    --expected-platform-uuid "$expected_platform_uuid" --expected-repository-commit "$expected_repository_commit"
}

if [[ "$COMMAND" == "verify" ]]; then
  [[ $# -eq 2 && "$2" =~ ^[0-9]{8}_[0-9]{6}$ ]] || { echo "Usage: ops/backup.sh verify YYYYmmdd_HHMMSS" >&2; exit 2; }
  verify_postgres_user="${POSTGRES_USER:-}"
  verify_postgres_db="${POSTGRES_DB:-}"
  if [[ -z "$verify_postgres_user" || -z "$verify_postgres_db" ]]; then
    verify_postgres_user="$(${DOCKER[@]} compose config --environment 2>/dev/null | sed -n 's/^POSTGRES_USER=//p' | tail -n1)"
    verify_postgres_db="$(${DOCKER[@]} compose config --environment 2>/dev/null | sed -n 's/^POSTGRES_DB=//p' | tail -n1)"
  fi
  verify_postgres_container="$(${DOCKER[@]} compose ps -q postgres)"
  verify_identity="$(${DOCKER[@]} exec -i "$verify_postgres_container" psql -U "$verify_postgres_user" -d "$verify_postgres_db" -Atqc "SELECT environment||'|'||database_name||'|'||database_identity_id::text FROM ops_control.environment_identity WHERE identity_key='primary' AND database_role='platform'")"
  IFS='|' read -r expected_environment expected_database expected_platform_uuid <<<"$verify_identity"
  expected_repository_commit="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  verify_pair "$2"
  exit
elif [[ "$COMMAND" != "create" || $# -gt 1 ]]; then
  echo "Usage: ops/backup.sh [create|verify YYYYmmdd_HHMMSS]" >&2
  exit 2
fi

available_bytes="$(df -PB1 "$BACKUP_DIR" | awk 'NR==2 {print $4}')"
(( available_bytes >= MIN_FREE_BYTES )) || { echo "ERROR: insufficient free space for coordinated backup" >&2; exit 1; }

postgres_partial="$BACKUP_DIR/postgres_${TIMESTAMP}.sql.gz.partial"
postgres_final="$BACKUP_DIR/postgres_${TIMESTAMP}.sql.gz"
minio_partial="$BACKUP_DIR/minio_${TIMESTAMP}.tar.gz.partial"
minio_final="$BACKUP_DIR/minio_${TIMESTAMP}.tar.gz"
manifest_partial="$BACKUP_DIR/backup_${TIMESTAMP}.manifest.json.partial"
manifest_final="$BACKUP_DIR/backup_${TIMESTAMP}.manifest.json"
minio_stage=""
published_postgres=0
published_minio=0
published_manifest=0

for path in "$postgres_partial" "$postgres_final" "$minio_partial" "$minio_final" "$manifest_partial" "$manifest_final"; do
  [[ ! -e "$path" && ! -L "$path" ]] || { echo "ERROR: backup path already exists: $(basename "$path")" >&2; exit 1; }
done

on_failure() {
  local status="${1:-$?}"
  trap - ERR INT TERM EXIT
  if (( published_manifest )) && [[ -f "$manifest_final" ]]; then mv -n "$manifest_final" "$manifest_final.failed" || true; fi
  if (( published_postgres )) && [[ -f "$postgres_final" ]]; then mv -n "$postgres_final" "$postgres_final.failed" || true; fi
  if (( published_minio )) && [[ -f "$minio_final" ]]; then mv -n "$minio_final" "$minio_final.failed" || true; fi
  [[ -n "$minio_stage" && -d "$minio_stage" ]] && rm -rf "$minio_stage"
  echo "ERROR: backup failed; partial/failed files are not recoverable backups" >&2
  exit "$status"
}
trap 'on_failure $?' ERR
trap 'on_failure 130' INT
trap 'on_failure 143' TERM

postgres_user="${POSTGRES_USER:-}"
postgres_db="${POSTGRES_DB:-}"
if [[ -z "$postgres_user" || -z "$postgres_db" ]]; then
  postgres_user="$("${DOCKER[@]}" compose config --environment 2>/dev/null | sed -n 's/^POSTGRES_USER=//p' | tail -n1)"
  postgres_db="$("${DOCKER[@]}" compose config --environment 2>/dev/null | sed -n 's/^POSTGRES_DB=//p' | tail -n1)"
fi
[[ -n "$postgres_user" && -n "$postgres_db" ]] || { echo "ERROR: platform database identity is unavailable" >&2; exit 1; }

postgres_container="$("${DOCKER[@]}" compose ps -q postgres)"
minio_container="$("${DOCKER[@]}" compose ps -q minio)"
[[ -n "$postgres_container" && -n "$minio_container" ]] || { echo "ERROR: PostgreSQL and MinIO containers must be running" >&2; exit 1; }
identity="$(${DOCKER[@]} exec -i "$postgres_container" psql -U "$postgres_user" -d "$postgres_db" -Atqc "SELECT environment||'|'||database_name||'|'||database_identity_id::text FROM ops_control.environment_identity WHERE identity_key='primary' AND database_role='platform'")"
IFS='|' read -r environment database_name platform_uuid <<<"$identity"
[[ -n "$environment" && "$database_name" == "$postgres_db" && -n "$platform_uuid" ]] || { echo "ERROR: platform environment identity mismatch" >&2; exit 1; }
repository_commit="$(git -C "$REPO_ROOT" rev-parse HEAD)"
expected_environment="$environment"
expected_database="$database_name"
expected_platform_uuid="$platform_uuid"
expected_repository_commit="$repository_commit"

echo "INFO: creating PostgreSQL partial"
"${DOCKER[@]}" compose exec -T postgres pg_dump -U "$postgres_user" -d "$postgres_db" | gzip >"$postgres_partial"
chmod 0600 "$postgres_partial"
[[ -s "$postgres_partial" ]]
gzip -t "$postgres_partial"
logical_bytes="$(gzip -dc "$postgres_partial" | wc -c)"
(( logical_bytes > 0 ))
gzip -dc "$postgres_partial" | sed -n '1p' | grep -q '^--'

minio_stage="$(mktemp -d "$BACKUP_DIR/.minio_${TIMESTAMP}.XXXXXX")"
chmod 0700 "$minio_stage"
echo "INFO: staging immutable MinIO copy"
"${DOCKER[@]}" cp "${minio_container}:/data/." "$minio_stage"
minio_source_file_count="$(find "$minio_stage" -type f -printf . | wc -c)"
(( minio_source_file_count > 0 ))
echo "INFO: creating MinIO partial"
tar -C "$minio_stage" -czf "$minio_partial" .
chmod 0600 "$minio_partial"
[[ -s "$minio_partial" ]]
tar -tzf "$minio_partial" >/dev/null
minio_entry_count="$(tar -tzf "$minio_partial" | wc -l)"
(( minio_entry_count >= minio_source_file_count ))
rm -rf "$minio_stage"
minio_stage=""

python3 "$SCRIPT_DIR/backup_manifest.py" create \
  --output "$manifest_partial" --timestamp "$TIMESTAMP" --environment "$environment" \
  --database "$database_name" --platform-uuid "$platform_uuid" --repository-commit "$repository_commit" \
  --postgres "$postgres_partial" --minio "$minio_partial" \
  --minio-entry-count "$minio_entry_count" --minio-source-file-count "$minio_source_file_count"
python3 -m json.tool "$manifest_partial" >/dev/null
chmod 0600 "$manifest_partial"

mv "$postgres_partial" "$postgres_final"; published_postgres=1
mv "$minio_partial" "$minio_final"; published_minio=1
mv "$manifest_partial" "$manifest_final"; published_manifest=1
chmod 0600 "$postgres_final" "$minio_final" "$manifest_final"
verify_pair "$TIMESTAMP"
trap - ERR INT TERM

# Retention is enforced by ops/backup_retention.py (backup-retention.timer), not
# here: creation and deletion stay separate so a retention bug can never damage
# the set this run just published. Creation remains additive by design.
if [[ -n "${BACKUP_RETENTION_DAYS:-}" ]]; then
  echo "INFO: BACKUP_RETENTION_DAYS=${BACKUP_RETENTION_DAYS} is enforced by ops/backup_retention.py (backup-retention.timer)"
fi
printf 'BACKUP_COMPLETE timestamp=%s postgres_bytes=%s minio_bytes=%s minio_entries=%s\n' \
  "$TIMESTAMP" "$(stat -c %s "$postgres_final")" "$(stat -c %s "$minio_final")" "$minio_entry_count"
