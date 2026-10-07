#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAKE="$TMP/docker"
cat >"$FAKE" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$1 $2" == "compose version" ]]; then exit 0; fi
if [[ "$1 $2 $3" == "compose config --environment" ]]; then
  printf 'POSTGRES_USER=tester\nPOSTGRES_DB=logdb\n'; exit 0
fi
if [[ "$1 $2 $3 ${4:-}" == "compose ps -q postgres" ]]; then echo pg-container; exit 0; fi
if [[ "$1 $2 $3 ${4:-}" == "compose ps -q minio" ]]; then echo minio-container; exit 0; fi
if [[ "$1" == "exec" ]]; then
  printf 'local_dev|logdb|11e594f4-8195-4c10-8301-5d0bf0447a22\n'; exit 0
fi
if [[ "$1 $2 $3 ${4:-}" == "compose exec -T postgres" ]]; then
  printf '%s\n' '-- PostgreSQL database dump' 'CREATE TABLE test(id integer);'
  [[ "${FAKE_PG_FAIL:-0}" != "1" ]] || exit 9
  exit 0
fi
if [[ "$1" == "cp" ]]; then
  [[ "${FAKE_MINIO_FAIL:-0}" != "1" ]] || exit 8
  destination="$3"
  mkdir -p "$destination/bucket"
  printf 'one' >"$destination/bucket/object-one"
  printf 'two' >"$destination/bucket/object-two"
  exit 0
fi
exit 64
FAKE
chmod 0755 "$FAKE"

run_backup() {
  BACKUP_TEST_MODE=1 BACKUP_TEST_DIR="$1" BACKUP_TEST_TIMESTAMP="$2" \
    BACKUP_TEST_DOCKER="$FAKE" "$ROOT/ops/backup.sh" create
}

success="$TMP/success"; mkdir "$success"
run_backup "$success" 20260713_010101 >/dev/null
[[ -f "$success/postgres_20260713_010101.sql.gz" ]]
[[ -f "$success/minio_20260713_010101.tar.gz" ]]
[[ -f "$success/backup_20260713_010101.manifest.json" ]]
[[ ! -e "$success/postgres_20260713_010101.sql.gz.partial" ]]
[[ ! -e "$success/minio_20260713_010101.tar.gz.partial" ]]
[[ ! -e "$success/backup_20260713_010101.manifest.json.partial" ]]
[[ "$(stat -c %a "$success")" == "700" ]]
for file in "$success"/*.gz "$success"/*.json; do [[ "$(stat -c %a "$file")" == "600" ]]; done
BACKUP_TEST_MODE=1 BACKUP_TEST_DIR="$success" BACKUP_TEST_TIMESTAMP=unused \
  BACKUP_TEST_DOCKER="$FAKE" "$ROOT/ops/backup.sh" verify 20260713_010101 >/dev/null
if run_backup "$success" 20260713_010101 >/dev/null 2>&1; then echo duplicate accepted >&2; exit 1; fi

pgfail="$TMP/pgfail"; mkdir "$pgfail"
if FAKE_PG_FAIL=1 run_backup "$pgfail" 20260713_010102 >/dev/null 2>&1; then echo pg failure masked >&2; exit 1; fi
[[ -f "$pgfail/postgres_20260713_010102.sql.gz.partial" ]]
[[ ! -e "$pgfail/postgres_20260713_010102.sql.gz" ]]
[[ ! -e "$pgfail/backup_20260713_010102.manifest.json" ]]

miniofail="$TMP/miniofail"; mkdir "$miniofail"
if FAKE_MINIO_FAIL=1 run_backup "$miniofail" 20260713_010103 >/dev/null 2>&1; then echo MinIO failure masked >&2; exit 1; fi
[[ -f "$miniofail/postgres_20260713_010103.sql.gz.partial" ]]
[[ ! -e "$miniofail/postgres_20260713_010103.sql.gz" ]]
[[ ! -e "$miniofail/minio_20260713_010103.tar.gz" ]]
[[ ! -e "$miniofail/backup_20260713_010103.manifest.json" ]]

locked="$TMP/locked"; mkdir "$locked"; chmod 700 "$locked"
(
  exec 8>"$locked/.backup.lock"
  flock 8
  sleep 3
) &
locker=$!
sleep 0.2
set +e
run_backup "$locked" 20260713_010104 >/dev/null 2>&1
status=$?
set -e
wait "$locker"
[[ "$status" == "75" ]]
[[ "$(find "$locked" -maxdepth 1 -type f ! -name '.backup.lock' | wc -l)" == "0" ]]

echo "OK - backup workflow partial, manifest, failure, duplicate, and lock tests passed"
