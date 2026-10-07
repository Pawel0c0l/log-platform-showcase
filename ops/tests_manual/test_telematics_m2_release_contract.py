#!/usr/bin/env python3
"""M2 release-preparation contract — apply path, rollback, window gate, evidence.

Specification of record:
  docs/20_telematics_ingestion_permanent_repair_plan.md §19.8 (apply), §19.9
    (rollback), §19.10 (first-run gate), §3.7 / §17 (late-arrival evidence)
  db/migrations/060_workflow_a_daily_trips_lookback_l3.sql
  ops/sql/m2_rollback_alpha00001_daily_trips_lookback_to_1.sql

Sections 1-5 are pure: stdlib only, no network, no database, no production
access. Section 6 EXECUTES the real rollback artifact against a disposable
PostgreSQL instance and is skipped unless one is named (see below). This suite
guards the *release-preparation* contract, not the L=3 behaviour — that is
`test_telematics_daily_lookback_l3.py`, which is unchanged in intent.

Why each of these is a test rather than a paragraph: every one of them encodes a
claim that was wrong once already, in a document review, and would have been
wrong again silently.

  1. No rollback may live where a migration runner will auto-apply it.
  2. `window_start_ts` on a claimed fire is the EFFECTIVE start, not the nominal
     one. A first-run gate that checks it against F-72h fails a correct run and
     would trigger a false rollback.
  3. ALPHA's production event enrichment is DISABLED, so L=3 issues zero
     `/vehicles/events` requests. The 7 -> 19 chunk growth is conditional
     analysis for a configuration ALPHA does not run.
  4. Committed documentation must not claim direct evidence the retained audit
     does not contain.
  5. Every path a committed document points at must be a tracked file.
  5b. The migration runner's skip/apply control flow is pinned, not merely its
     SQL fragments, and its error state is pinned as ACTIVE rather than merely
     declared: branch 9's whole argument rests on a Python port of that loop,
     and a fragment scan survives deleting the skip `continue`, inverting the
     ledger condition, and inserting `set +e` after the preamble.
  6. The rollback does what its comments say — proven by running it, not by
     reading it. Reading the SQL is how the previous defects got through.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 \
        ops/tests_manual/test_telematics_m2_release_contract.py

and, for section 6, with a disposable PostgreSQL 16 instance:

    M2_ROLLBACK_TEST_DSN=postgresql://user:pass@127.0.0.1:55432/disposable \
    PYTHONDONTWRITEBYTECODE=1 python3 \
        ops/tests_manual/test_telematics_m2_release_contract.py

Section 6 is destructive: it drops and rebuilds `workflow_a_control` and
`public.schema_migrations` in the target database. It refuses to connect to
anything it cannot prove disposable, and never to production.
"""
from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.coverage_windows import derive_effective_window  # noqa: E402
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)

UTC = timezone.utc
SECONDS_PER_DAY = 86_400

L_DAILY_APPROVED = 3
ALPHA_DELAY_S = 10_800
ALPHA_OVERLAP_S = 3_600
ALPHA_MAX_RECOVERY_S = 2_678_400

MIGRATIONS_DIR = REPO_ROOT / "db/migrations"
FORWARD_MIGRATION = MIGRATIONS_DIR / "060_workflow_a_daily_trips_lookback_l3.sql"
ROLLBACK_SQL = REPO_ROOT / "ops/sql/m2_rollback_alpha00001_daily_trips_lookback_to_1.sql"
MIGRATE_RUNNER = REPO_ROOT / "ops/db_migrate.sh"
PLAN_DOC = REPO_ROOT / "docs/20_telematics_ingestion_permanent_repair_plan.md"
AUDIT_DOC = REPO_ROOT / "docs/19_telematics_trips_late_arrival_audit.md"
AUDIT_CSV = REPO_ROOT / "artifacts/telematics_late_arrival_audit.csv"

FAILURES: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL  {name}" + (f"  --  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 1. The rollback cannot be auto-applied.
# ---------------------------------------------------------------------------

runner_src = MIGRATE_RUNNER.read_text(encoding="utf-8")

check(
    "the migration runner auto-discovers every .sql in db/migrations",
    "find \"${MIGRATIONS_DIR}\"" in runner_src and "-name '*.sql'" in runner_src,
    "discovery changed; re-derive where a rollback may safely live",
)
check(
    "the rollback artifact exists",
    ROLLBACK_SQL.is_file(),
)
check(
    "the rollback artifact is NOT inside db/migrations",
    MIGRATIONS_DIR not in ROLLBACK_SQL.parents,
    f"{ROLLBACK_SQL} would be auto-applied",
)
check(
    "the rollback artifact is NOT inside db/client_business either",
    (REPO_ROOT / "db/client_business") not in ROLLBACK_SQL.parents,
)

# The concrete failure this prevents: the M2 rollback shipped as a sequential
# forward migration, where `ops/db_migrate.sh` would auto-apply it and silently
# revert a healthy L = 3 deployment.
#
# This was originally written as "no 061 exists / 060 is the ceiling", which was
# a proxy for that, and only for as long as nobody added a legitimate forward
# migration. M4 added one (`061_workflow_a_provider_request_log.sql`), so the
# proxy is now restated as the property it always meant: whatever the ceiling
# is, nothing in `db/migrations` may be this rollback, or reverse it.
migration_names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
rollback_marker = "M2 EMERGENCY ROLLBACK"
migrations_containing_rollback = sorted(
    path.name for path in MIGRATIONS_DIR.glob("*.sql")
    if rollback_marker in path.read_text(encoding="utf-8")
)
check(
    "no file in db/migrations is the M2 rollback",
    not migrations_containing_rollback,
    f"found: {migrations_containing_rollback}",
)
check(
    "the M2 forward migration is still present and is still 060",
    FORWARD_MIGRATION.name in migration_names,
    f"names: {migration_names[-3:]}",
)
# A migration numbered above 060 is allowed — later milestones need them — but
# it must be a genuine forward change, never a reversal of the approved horizon.
for name in (n for n in migration_names if n > FORWARD_MIGRATION.name):
    body = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
    statements = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("--")
    )
    check(
        f"{name} does not revert the approved DAILY lookback",
        "lookback_days" not in statements,
        "a migration after 060 that writes lookback_days must be reviewed as a "
        "horizon change, not shipped as an ordinary forward migration",
    )

rollback_src = ROLLBACK_SQL.read_text(encoding="utf-8")
rollback_statements = "\n".join(
    line for line in rollback_src.splitlines() if not line.lstrip().startswith("--")
)

check(
    "the rollback runs in an explicit transaction",
    "BEGIN;" in rollback_statements and "COMMIT;" in rollback_statements,
)
check(
    "the rollback refuses unless migration 060 is ledgered",
    "is not recorded in public.schema_migrations" in rollback_src,
)
check(
    "the rollback NEVER writes the migration ledger",
    not re.search(
        r"(INSERT INTO|DELETE FROM|UPDATE)\s+public\.schema_migrations",
        rollback_statements,
    ),
    "the rollback mutates schema_migrations; the next migrate run would undo it",
)
check(
    "the rollback issues exactly one UPDATE, against client_dataset_schedule",
    rollback_statements.count("UPDATE ") == 1
    and "UPDATE workflow_a_control.client_dataset_schedule" in rollback_statements,
)
for verb in ("INSERT", "DELETE", "DROP", "TRUNCATE", "ALTER TABLE", "CREATE TABLE"):
    check(f"the rollback issues no {verb}", verb not in rollback_statements)
for guard in (
    "M2 rollback refused: no client_dataset_schedule row",
    "refusing to guess which one to revert",
    "not daily",
    "M2 rollback write anomaly",
    "M2 rollback verification failed",
    "M2 rollback already applied",
):
    check(f"the rollback guards: {guard[:52]}", guard in rollback_src)

rollback_set_clause = "\n".join(
    re.findall(r"\n\s*SET (.*?)\n\s*WHERE", rollback_statements, flags=re.S)
)
for forbidden in (
    "timezone", "run_time", "frequency", "enabled", "overwrite_existing",
    "event_enrichment_mode",
):
    check(
        f"the rollback does not write `{forbidden}`",
        forbidden not in rollback_set_clause,
    )

# ---------------------------------------------------------------------------
# 2. Nominal vs effective — the first-run gate must not fail a correct run.
#
# The dispatcher claims a compatibility fire with the EFFECTIVE window when the
# coverage gate allows it, and records the nominal bounds separately as
# evidence. So `client_schedule_run_history.window_start_ts` is E_start, and a
# gate asserting "window_start_ts == F - 3 days" would reject a correct L=3 run.
# ---------------------------------------------------------------------------

dispatcher_src = (REPO_ROOT / "jobs/api/telematics/dispatcher.py").read_text(encoding="utf-8")

check(
    "the dispatcher claims the EFFECTIVE window when the coverage gate allows",
    "claim_start = effective.effective_window_start_ts" in dispatcher_src,
)
check(
    "the dispatcher records the nominal bounds separately, as evidence",
    '"nominal_window_start_ts": window_start' in dispatcher_src,
)

F = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)
# Steady state: the watermark sits at the previous fire's effective end.
W_steady = F - timedelta(days=1) - timedelta(seconds=ALPHA_DELAY_S)
window = derive_effective_window(
    scheduled_fire_ts=F,
    lookback_days=L_DAILY_APPROVED,
    stabilization_delay_seconds=ALPHA_DELAY_S,
    overlap_seconds=ALPHA_OVERLAP_S,
    max_recovery_span_seconds=ALPHA_MAX_RECOVERY_S,
    coverage_start_ts=F - timedelta(days=50),
    covered_through_ts=W_steady,
)

check(
    "steady-state nominal start is F - 72 h",
    window.nominal_window_start_ts == F - timedelta(hours=72),
    f"got {window.nominal_window_start_ts}",
)
check(
    "steady-state EFFECTIVE start is F - 76 h, not F - 72 h",
    window.effective_window_start_ts == F - timedelta(hours=76),
    f"got {window.effective_window_start_ts}",
)
check(
    "steady-state effective end is F - 3 h",
    window.effective_window_end_ts == F - timedelta(hours=3),
)
check(
    "steady-state effective span is 73 h",
    window.effective_window_end_ts - window.effective_window_start_ts
    == timedelta(hours=73),
)
check(
    "nominal and effective start genuinely differ, so the gate must name which it checks",
    window.nominal_window_start_ts != window.effective_window_start_ts,
)

# F - 76 h is a steady-state value, NOT an invariant: a watermark left behind by
# a gap legitimately drags E_start earlier via `min(base, W - O)`. A gate that
# hard-codes F-76h would false-alarm on a legitimate catch-up run.
window_behind_w = derive_effective_window(
    scheduled_fire_ts=F,
    lookback_days=L_DAILY_APPROVED,
    stabilization_delay_seconds=ALPHA_DELAY_S,
    overlap_seconds=ALPHA_OVERLAP_S,
    max_recovery_span_seconds=ALPHA_MAX_RECOVERY_S,
    coverage_start_ts=F - timedelta(days=50),
    covered_through_ts=F - timedelta(days=10),
)
check(
    "a watermark left behind by a gap legitimately moves E_start earlier than F - 76 h",
    window_behind_w.effective_window_start_ts < F - timedelta(hours=76),
    f"got {window_behind_w.effective_window_start_ts}",
)
check(
    "so F - 76 h is a steady-state expectation, never a hard invariant",
    window_behind_w.nominal_window_start_ts == window.nominal_window_start_ts,
)

plan_src = PLAN_DOC.read_text(encoding="utf-8")
check(
    "the plan's first-run gate distinguishes nominal from effective explicitly",
    "nominal_window_start_ts" in plan_src and "F − 76 h" in plan_src,
)
check(
    "the plan no longer gates on 'window_start_ts exactly 3 days before the fire'",
    "window_start_ts` is exactly 3 days before the fire" not in plan_src,
)

# ---------------------------------------------------------------------------
# 3. Event enrichment — ALPHA's actual production configuration.
# ---------------------------------------------------------------------------

sync_src = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text(
    encoding="utf-8"
)

check(
    "disabled enrichment skips the /vehicles/events fetch entirely",
    "Vehicle event enrichment disabled by event_enrichment_mode; skipping /vehicles/events fetch"
    in sync_src,
)
check(
    "the events fetch block is gated on both the metrics source and the mode",
    "if not api_owns_trip_metrics or event_enrichment_disabled:" in sync_src,
)
check(
    "the plan states ALPHA's actual production events behaviour as 0 requests",
    "0 → 0" in plan_src or "0 requests" in plan_src,
)
check(
    "the plan labels the 7 -> 19 growth as conditional, not current",
    "CONDITIONAL" in plan_src and "7 → 19" in plan_src,
)
check(
    "the plan no longer asserts ALPHA runs enrichment enabled",
    "ALPHA runs\n  `event_enrichment_mode = 'enabled'`" not in plan_src
    and "ALPHA runs `event_enrichment_mode = 'enabled'`" not in plan_src,
)
check(
    "the fail-loud enrichment contract is still asserted, not weakened",
    "DB upsert skipped due to incomplete event enrichment" in sync_src
    and "_raise_incomplete_event_enrichment" in sync_src,
)

# ---------------------------------------------------------------------------
# 4. Late-arrival evidence — direct vs inferential, and reference integrity.
# ---------------------------------------------------------------------------

check("the audit document is present", AUDIT_DOC.is_file())
check("the audit's machine-readable companion is present", AUDIT_CSV.is_file())

if AUDIT_CSV.is_file():
    with AUDIT_CSV.open(encoding="utf-8") as handle:
        audit_rows = list(csv.DictReader(handle))
    lower_bounds = [
        float(r["publication_lag_lower_bound_hours"])
        for r in audit_rows
        if r.get("publication_lag_lower_bound_hours")
    ]
    inferential = [
        float(m.group(1))
        for r in audit_rows
        for m in [re.search(r"([0-9]+\.?[0-9]*)\s*d", r.get("notes") or "")]
        if m
    ]

    check("the audit cohort is 1 031 records", len(audit_rows) == 1031, f"{len(audit_rows)}")
    check(
        "the maximum DIRECTLY PROVEN absence is 8.61 d",
        abs(max(lower_bounds) / 24 - 8.61) < 0.01,
        f"{max(lower_bounds) / 24:.2f} d",
    )
    check(
        "ZERO records are directly proven absent beyond 14 days",
        sum(1 for v in lower_bounds if v >= 14 * 24) == 0,
    )
    check(
        "ZERO records are directly proven absent beyond 11 days",
        sum(1 for v in lower_bounds if v >= 11 * 24) == 0,
    )
    check(
        "only 4 records are directly proven absent at 8 days or more",
        sum(1 for v in lower_bounds if v >= 8 * 24) == 4,
    )
    check(
        "the inferential provider-id tail does reach beyond 14 days (109 records)",
        sum(1 for v in inferential if v >= 14) == 109,
        f"{sum(1 for v in inferential if v >= 14)}",
    )


def withdrawn_only(text: str, phrase: str) -> bool:
    """True when `phrase` appears only inside an explicit withdrawal.

    The withdrawn figure is deliberately quoted in the correction log and in §17
    so the record shows what was wrong. A naive "phrase not in text" assertion
    cannot distinguish a claim from its retraction, so require every occurrence
    to sit near a withdrawal marker.
    """
    index = text.find(phrase)
    while index != -1:
        neighbourhood = text[max(0, index - 400):index + 400]
        if "withdraw" not in neighbourhood.lower():
            return False
        index = text.find(phrase, index + 1)
    return True


for claim in ("16 records provably", "16 records were provably", "16 records still absent"):
    check(
        f"the plan asserts '{claim}…' nowhere except as a withdrawal",
        withdrawn_only(plan_src, claim),
        "the withdrawn figure is being stated as fact somewhere",
    )
check(
    "the plan labels the long tail INFERENTIAL",
    "INFERENTIAL" in plan_src,
)
check(
    "the plan states the directly proven bound of 8.61 d",
    "8.61" in plan_src,
)
check(
    "the plan reports the two distributions separately",
    "DIRECTLY PROVEN" in plan_src and "provider_trip_id` allocation ordering" in plan_src,
)

request_time_doc = (
    REPO_ROOT / "docs/18_telematics_trips_request_time_contract.md"
).read_text(encoding="utf-8")
check(
    "docs/18 asserts direct proof beyond 14 days nowhere except as a withdrawal",
    withdrawn_only(request_time_doc, "provably still unpublished at 14 days"),
)
check(
    "docs/18's correction note carries the directly proven bound",
    "8.61" in request_time_doc,
)
check(
    "docs/18's note separates directly proven from inferential",
    "Directly proven" in request_time_doc and "Inferential" in request_time_doc,
)

# ---------------------------------------------------------------------------
# 5. Reference integrity from a clean-clone perspective.
#
# Every path a committed document points at must be a TRACKED file. An untracked
# file resolves fine on this machine and is a dangling reference everywhere else.
# ---------------------------------------------------------------------------

tracked = set(
    subprocess.run(
        ["git", "ls-files"], cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=False,
    ).stdout.splitlines()
)

def _committed_text(path: str) -> str:
    """The document as it exists in HEAD, or '' if it is not committed yet.

    Read from HEAD rather than the worktree because that is what this section
    is actually about: a reference is dangling for someone else only once the
    document carrying it has been committed. An uncommitted document that
    points at uncommitted files is internally consistent — the two are
    committed together — and flagging it would make this check fire on every
    in-progress change while proving nothing about a clean clone.

    The guard itself is unweakened: the moment a document is committed, every
    path it names must be a tracked file.
    """
    completed = subprocess.run(
        ["git", "show", f"HEAD:{path}"], cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=False,
    )
    return completed.stdout if completed.returncode == 0 else ""


referenced_docs = {
    name: _committed_text(name)
    for name in (
        "docs/20_telematics_ingestion_permanent_repair_plan.md",
        "docs/18_telematics_trips_request_time_contract.md",
        "docs/19_telematics_trips_late_arrival_audit.md",
    )
}

REFERENCE_RE = re.compile(
    r"`((?:docs|ops|db|jobs|scripts|artifacts)/[A-Za-z0-9_./-]+\.(?:md|py|sql|csv))`"
)
dangling: List[str] = []
for source, text in referenced_docs.items():
    for referenced in sorted(set(REFERENCE_RE.findall(text))):
        if referenced not in tracked and not (REPO_ROOT / referenced).is_file():
            dangling.append(f"{source} -> {referenced} (missing)")
        elif referenced not in tracked:
            dangling.append(f"{source} -> {referenced} (untracked)")

# The worktree copy still may not point at a file that does not exist at all.
# That is always wrong, committed or not, and it is the half of the check that
# does not depend on tracking state.
worktree_missing: List[str] = []
for source, text in (
    ("docs/20_telematics_ingestion_permanent_repair_plan.md", plan_src),
    ("docs/18_telematics_trips_request_time_contract.md", request_time_doc),
    (
        "docs/19_telematics_trips_late_arrival_audit.md",
        AUDIT_DOC.read_text(encoding="utf-8") if AUDIT_DOC.is_file() else "",
    ),
):
    for referenced in sorted(set(REFERENCE_RE.findall(text))):
        if not (REPO_ROOT / referenced).is_file():
            worktree_missing.append(f"{source} -> {referenced} (missing)")

check(
    "no M2 document in the worktree points at a file that does not exist",
    not worktree_missing,
    "; ".join(worktree_missing),
)

check(
    "no committed M2 document points at an untracked or missing file",
    not dangling,
    "; ".join(dangling),
)

# ---------------------------------------------------------------------------
# 5b. The migration runner's skip/apply CONTROL FLOW.
#
# Branch 9 below proves "L = 1 survives the next migrate run" using a Python
# port of `ops/db_migrate.sh`'s loop, because the real runner is welded to the
# production compose stack and must never be pointed at a test database. That
# proof is only worth what the port is worth, and asserting that a few SQL
# fragments still appear in the shell does NOT establish it: the fragments
# survive both mutations that would break the property —
#
#   * deleting the `continue`, so a ledgered migration is re-applied anyway;
#   * inverting `== "1"`, so ledgered and unledgered swap paths.
#
# So the runner's control flow is pinned structurally, and the pin is then run
# against those mutations to prove it is load-bearing rather than decorative.
#
# `set -euo pipefail` is part of this contract, not housekeeping: without `-e` a
# failed `psql` would fall through to the ledger INSERT and record a migration
# that never applied. And its PRESENCE is not the contract — errexit being
# ACTIVE at the apply is. `set +e` inserted after the preamble leaves the
# fragment, the loop text and every structural invariant above untouched while
# destroying exactly that guarantee, so the whole active command sequence from
# the shebang through the loop is pinned as well.
# ---------------------------------------------------------------------------

# Whole-file properties the loop depends on but does not contain.
MIGRATION_RUNNER_SEMANTICS = (
    ("creates the ledger when absent",
     "CREATE TABLE IF NOT EXISTS public.schema_migrations("),
    ("discovers every .sql in db/migrations, sorted",
     "find \"${MIGRATIONS_DIR}\" -maxdepth 1 -type f -name '*.sql' | sort"),
    ("aborts the whole run on the first failing command",
     "set -euo pipefail"),
)

# `set -euo pipefail` PRESENT is not the contract. `errexit` ACTIVE when the
# migration `psql` runs is the contract: a `set +e` inserted anywhere between the
# preamble and the ledger INSERT leaves that fragment — and the loop, which is
# textually unchanged — intact, while a failed migration would then fall through
# to the INSERT and be recorded as applied. Hence the active-prefix pin below.
ERREXIT_ENABLE_RE = re.compile(r"^\s*set\s+(?:-[a-zA-Z]*e[a-zA-Z]*|-o\s+errexit)\b")
ERREXIT_DISABLE_RE = re.compile(r"^\s*set\s+(?:\+[a-zA-Z]*e[a-zA-Z]*|\+o\s+errexit)\b")
ERROR_SUPPRESSION_RE = re.compile(r"\|\||&&\s*$|^\s*(?:if|while|until|!)\s")

MIGRATION_LOOP_HEADER = 'for migration_path in "${migration_files[@]}"; do'

LEDGER_QUERY_FRAGMENT = "SELECT 1 FROM public.schema_migrations WHERE filename ="
LEDGER_INSERT_FRAGMENT = "INSERT INTO public.schema_migrations(filename)"
APPLY_REDIRECT_FRAGMENT = '< "${migration_path}"'
LEDGER_HIT_CONDITION = '"${already_applied}" == "1"'

# The loop exactly as it must read. Whitespace-normalized only (trailing spaces
# stripped); nothing semantic is normalized away. A cosmetic edit to the runner
# fails this deliberately — re-deriving the port is the point of the gate.
EXPECTED_MIGRATION_LOOP = r'''for migration_path in "${migration_files[@]}"; do
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
done'''


EXPECTED_ACTIVE_PREFIX = r'''#!/usr/bin/env bash
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
done'''


def active_script_prefix(source: str) -> str:
    """Every COMMAND from the shebang through the migration loop's `done`.

    Blank lines and whole-line comments are dropped — neither can change what
    the shell does, and dropping them keeps a comment edit from failing the pin
    for no reason. Everything else is kept verbatim and in order, so any command
    INSERTED into the region the migration apply depends on — `set +e` being the
    one that motivated this — changes the text and fails the comparison. That is
    the fail-closed property: the pin does not enumerate forbidden statements,
    it refuses anything that is not the reviewed sequence.
    """
    lines = source.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == MIGRATION_LOOP_HEADER),
        None,
    )
    if start is None:
        return ""
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip() == "done"), None
    )
    if end is None:
        return ""
    kept: List[str] = []
    for index, line in enumerate(lines[:end + 1]):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and not (index == 0 and stripped.startswith("#!")):
            continue
        kept.append(line.rstrip())
    return "\n".join(kept)


def errexit_violations(prefix: str) -> List[str]:
    """Violations of "errexit is active when the migration is applied", by code.

    Independent of the exact pin, and deliberately so: the realistic way the pin
    gets defeated is a reviewer regenerating the expected prefix from an already
    modified file. This layer reads the error-state statements themselves, so a
    regenerated pin still fails.
    """
    lines = prefix.splitlines()
    if not lines:
        return ["NO_ACTIVE_PREFIX"]

    violations: List[str] = []
    # `[[ ]]`, `mapfile` and `set -o pipefail` are bash, not POSIX sh.
    if not lines[0].startswith("#!") or "bash" not in lines[0]:
        violations.append("RUNNER_NOT_INVOKED_AS_BASH")

    enables = [i for i, line in enumerate(lines) if ERREXIT_ENABLE_RE.match(line)]
    disables = [i for i, line in enumerate(lines) if ERREXIT_DISABLE_RE.match(line)]
    applies = [i for i, line in enumerate(lines) if APPLY_REDIRECT_FRAGMENT in line]
    inserts = [i for i, line in enumerate(lines) if LEDGER_INSERT_FRAGMENT in line]

    if not enables:
        violations.append("ERREXIT_NEVER_ENABLED")
    if len(applies) != 1:
        violations.append("NO_SINGLE_MIGRATION_APPLY")
    if len(inserts) != 1:
        violations.append("NO_SINGLE_LEDGER_INSERT")
    if violations and ("ERREXIT_NEVER_ENABLED" in violations
                       or "NO_SINGLE_MIGRATION_APPLY" in violations
                       or "NO_SINGLE_LEDGER_INSERT" in violations):
        return violations

    enable, apply_at, insert_at = enables[0], applies[0], inserts[0]
    if enable != 1:
        # Anything executed before errexit is armed runs unguarded.
        violations.append("ERREXIT_NOT_ENABLED_AS_FIRST_COMMAND")
    # The window that matters runs from arming errexit to the ledger write. A
    # disable anywhere inside it — before the loop, or between apply and INSERT —
    # breaks "a failed migration cannot be ledgered". A later re-enable does not
    # repair it, so the whole window is scanned rather than just the apply line.
    if any(enable < index <= insert_at for index in disables):
        violations.append("ERREXIT_DISABLED_BEFORE_LEDGER_WRITE")
    # errexit does not fire for a command whose status is consumed by `||`, `&&`,
    # `!` or an `if`/`while` condition.
    if ERROR_SUPPRESSION_RE.search(lines[apply_at]):
        violations.append("APPLY_ERROR_SUPPRESSED")
    return violations


def extract_migration_loop(source: str) -> str:
    """The runner's per-migration loop, header line through its `done`."""
    lines = source.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == MIGRATION_LOOP_HEADER),
        None,
    )
    if start is None:
        return ""
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip() == "done"), None
    )
    if end is None:
        return ""
    return "\n".join(line.rstrip() for line in lines[start:end + 1])


def migration_loop_violations(block: str) -> List[str]:
    """Structural violations of the skip/apply contract, by code.

    Positional, not textual: it locates the ledger query, the branch it feeds,
    the branch body, the migration application and the ledger write, then
    asserts how they are ordered and nested. That is what makes it survive
    renames and catch reorderings, which a fragment scan does neither of.
    """
    lines = [line.rstrip() for line in block.splitlines()]

    def find(predicate) -> List[int]:
        return [i for i, line in enumerate(lines) if predicate(line)]

    violations: List[str] = []
    queries = find(
        lambda line: LEDGER_QUERY_FRAGMENT in line and "already_applied=" in line
    )
    branches = find(
        lambda line: line.strip().startswith("if [[") and "already_applied" in line
    )
    applies = find(lambda line: APPLY_REDIRECT_FRAGMENT in line)
    inserts = find(lambda line: LEDGER_INSERT_FRAGMENT in line)

    if len(queries) != 1:
        violations.append("NO_SINGLE_LEDGER_QUERY")
    if len(branches) != 1:
        violations.append("NO_SINGLE_LEDGER_BRANCH")
    if len(applies) != 1:
        violations.append("NO_SINGLE_MIGRATION_APPLY")
    if len(inserts) != 1:
        violations.append("NO_SINGLE_LEDGER_INSERT")
    if violations:
        return violations

    query, branch, apply_at, insert_at = queries[0], branches[0], applies[0], inserts[0]
    ends = [i for i in find(lambda line: line.strip() == "fi") if i > branch]
    if not ends:
        violations.append("NO_BRANCH_END")
        return violations
    end = ends[0]

    if query > branch:
        violations.append("LEDGER_QUERY_AFTER_BRANCH")
    if LEDGER_HIT_CONDITION not in lines[branch]:
        # Catches the inverted condition: `!= "1"` no longer selects the
        # ledgered case, so the branch bodies have swapped meaning.
        violations.append("LEDGER_CONDITION_NOT_EQUALITY_ON_HIT")

    body = lines[branch + 1:end]
    if not any(line.strip() == "continue" for line in body):
        violations.append("SKIP_BRANCH_DOES_NOT_CONTINUE")
    if not any('echo "SKIP' in line for line in body):
        violations.append("SKIP_BRANCH_NOT_ANNOUNCED")
    if branch < apply_at < end:
        violations.append("MIGRATION_APPLIED_ON_LEDGERED_PATH")
    if branch < insert_at < end:
        violations.append("LEDGER_WRITTEN_ON_LEDGERED_PATH")
    if apply_at < end:
        violations.append("APPLY_NOT_ON_UNLEDGERED_PATH")
    if insert_at < end:
        violations.append("LEDGER_INSERT_NOT_ON_UNLEDGERED_PATH")
    if insert_at < apply_at:
        violations.append("LEDGER_WRITTEN_BEFORE_MIGRATION_APPLIED")
    return violations


for _label, _fragment in MIGRATION_RUNNER_SEMANTICS:
    check(
        f"the migration runner still: {_label}",
        _fragment in runner_src,
        "ops/db_migrate.sh changed; re-derive the port before trusting branch 9",
    )

active_prefix = active_script_prefix(runner_src)
check(
    "the runner's active execution prefix is extractable",
    bool(active_prefix),
    "no `for migration_path ... done` loop found in ops/db_migrate.sh",
)
check(
    "every command from the shebang through the migration loop is the reviewed one",
    active_prefix == EXPECTED_ACTIVE_PREFIX,
    "ops/db_migrate.sh gained, lost or changed a command in the region the "
    "migration apply depends on; re-review it, then re-derive this pin",
)
check(
    "errexit is armed first and still active when the migration is applied",
    errexit_violations(active_prefix) == [],
    f"violations: {errexit_violations(active_prefix)}",
)

# The errexit mutants, derived from the real source. Each keeps `set -euo
# pipefail` in the file and the migration loop textually intact — which is
# exactly why the previous fragment assertion passed them.
_errexit_mutants = (
    (
        "mutant 6: `set +e` inserted after the preamble, before the loop",
        runner_src.replace(
            "set -euo pipefail\n", "set -euo pipefail\nset +e\n", 1
        ),
        "ERREXIT_DISABLED_BEFORE_LEDGER_WRITE",
    ),
    (
        "mutant 7: `set +o errexit` inserted immediately before the loop",
        runner_src.replace(
            MIGRATION_LOOP_HEADER, "set +o errexit\n" + MIGRATION_LOOP_HEADER, 1
        ),
        "ERREXIT_DISABLED_BEFORE_LEDGER_WRITE",
    ),
    (
        "mutant 8: `set +e` inserted inside the loop, just before the apply",
        runner_src.replace(
            '  echo "APPLY ${filename}"\n',
            '  echo "APPLY ${filename}"\n  set +e\n',
            1,
        ),
        "ERREXIT_DISABLED_BEFORE_LEDGER_WRITE",
    ),
    (
        "mutant 9: the original `-e` dropped from the preamble",
        runner_src.replace("set -euo pipefail", "set -uo pipefail", 1),
        "ERREXIT_NEVER_ENABLED",
    ),
    (
        "mutant 10: the migration apply's failure suppressed with `|| true`",
        runner_src.replace(
            '< "${migration_path}"\n', '< "${migration_path}" || true\n', 1
        ),
        "APPLY_ERROR_SUPPRESSED",
    ),
)

for _mutant_name, _mutant_src, _expected_code in _errexit_mutants:
    _mutant_prefix = active_script_prefix(_mutant_src)
    check(
        f"the active-prefix pin rejects {_mutant_name}",
        _mutant_src != runner_src
        and bool(_mutant_prefix)
        and _mutant_prefix != EXPECTED_ACTIVE_PREFIX
        and _expected_code in errexit_violations(_mutant_prefix),
        f"violations: {errexit_violations(_mutant_prefix)}",
    )

# The Codex reproduction, stated as the property rather than as a mutant: the
# transformation that used to escape every assertion in this suite.
_codex_bypass = runner_src.replace("set -euo pipefail\n", "set -euo pipefail\nset +e\n", 1)
check(
    "the `set +e` bypass keeps everything the OLD proof checked — and is still rejected",
    all(fragment in _codex_bypass for _label, fragment in MIGRATION_RUNNER_SEMANTICS)
    and extract_migration_loop(_codex_bypass) == EXPECTED_MIGRATION_LOOP
    and migration_loop_violations(extract_migration_loop(_codex_bypass)) == []
    and active_script_prefix(_codex_bypass) != EXPECTED_ACTIVE_PREFIX
    and errexit_violations(active_script_prefix(_codex_bypass)) != [],
    "the bypass is either no longer green on the old assertions, or no longer "
    "caught by the new one",
)

migration_loop = extract_migration_loop(runner_src)
check(
    "the runner's per-migration loop is where it is expected to be",
    bool(migration_loop),
    "no `for migration_path ... done` loop found in ops/db_migrate.sh",
)
check(
    "the runner's loop is byte-for-byte the reviewed one",
    migration_loop == EXPECTED_MIGRATION_LOOP,
    "ops/db_migrate.sh's loop changed; re-review it, then re-derive both this "
    "pin and the Python port used by branch 9",
)
check(
    "the runner's loop satisfies the skip/apply contract structurally",
    migration_loop_violations(migration_loop) == [],
    f"violations: {migration_loop_violations(migration_loop)}",
)

# The pin, proven load-bearing. Each mutant is the REAL loop with one semantic
# change — the same changes that leave a fragment scan green.
_loop_lines = migration_loop.splitlines()
_continue_at = next(
    i for i, line in enumerate(_loop_lines) if line.strip() == "continue"
)
_apply_at = next(
    i for i, line in enumerate(_loop_lines) if APPLY_REDIRECT_FRAGMENT in line
)
_insert_at = next(
    i for i, line in enumerate(_loop_lines) if LEDGER_INSERT_FRAGMENT in line
)

_no_continue = "\n".join(
    line for i, line in enumerate(_loop_lines) if i != _continue_at
)
_inverted = migration_loop.replace(LEDGER_HIT_CONDITION, '"${already_applied}" != "1"')
_swapped_lines = list(_loop_lines)
_swapped_lines[_apply_at], _swapped_lines[_insert_at] = (
    _swapped_lines[_insert_at], _swapped_lines[_apply_at],
)
_ledger_first = "\n".join(_swapped_lines)
_apply_in_skip_lines = [
    line for i, line in enumerate(_loop_lines) if i != _apply_at
]
_apply_in_skip_lines.insert(
    _continue_at, _loop_lines[_apply_at]
)
_apply_in_skip = "\n".join(_apply_in_skip_lines)

for _mutant_name, _mutant, _expected_code in (
    ("mutant 1: the skip branch's `continue` removed",
     _no_continue, "SKIP_BRANCH_DOES_NOT_CONTINUE"),
    ("mutant 2: the ledger condition inverted to `!= \"1\"`",
     _inverted, "LEDGER_CONDITION_NOT_EQUALITY_ON_HIT"),
    ("mutant 3: the ledger row written before the migration is applied",
     _ledger_first, "LEDGER_WRITTEN_BEFORE_MIGRATION_APPLIED"),
    ("mutant 4: the migration applied on the ledgered path",
     _apply_in_skip, "MIGRATION_APPLIED_ON_LEDGERED_PATH"),
):
    check(
        f"the control-flow pin rejects {_mutant_name}",
        _mutant != migration_loop
        and _expected_code in migration_loop_violations(_mutant)
        and _mutant != EXPECTED_MIGRATION_LOOP,
        f"violations: {migration_loop_violations(_mutant)}",
    )

_no_errexit = runner_src.replace("set -euo pipefail", "set -uo pipefail", 1)
check(
    "the pin rejects mutant 5: `set -e` dropped, so a failed migration would "
    "fall through to the ledger INSERT and be recorded as applied",
    _no_errexit != runner_src
    and any(
        fragment not in _no_errexit for _label, fragment in MIGRATION_RUNNER_SEMANTICS
    ),
)

# ---------------------------------------------------------------------------
# 6. The rollback contract, EXECUTED.
#
# Everything above reads the artifact. Reading is how the last two defects got
# through review, so this section runs the real file, byte for byte, against a
# disposable PostgreSQL database and asserts what it actually did to the rows.
#
# The load-bearing property is not the UPDATE. It is that the rollback leaves
# migration 060 LEDGERED, so the next ordinary `ops/db_migrate.sh` run skips it
# and L = 1 survives. Branch 9 proves that by running the migration runner's own
# loop afterwards; branch 10 proves the assertion is not vacuous by deleting the
# ledger row and watching L snap back to 3.
# ---------------------------------------------------------------------------

ROLLBACK_DSN_ENV = "M2_ROLLBACK_TEST_DSN"

# Enough of the control plane for the rollback's two tables to exist, and no
# more. Pinned rather than "everything in db/migrations" so an unrelated future
# migration cannot change what this suite bootstraps.
BOOTSTRAP_MIGRATIONS = (
    "008_workflow_a_control_plane.sql",
    "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    # 017 denormalizes client_code into the retention table too, so 013 is a
    # prerequisite of the bootstrap even though the rollback never reads it.
    "013_workflow_a_client_table_retention.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
)

TARGET_CLIENT_CODE = "ALPHA00001"
TARGET_DATASET = "trips_sync"
TEST_CLIENT_ID = "1379f9e1-8392-4406-8557-a1c5348d6a69"
TEST_SCHEDULE_ID = "4fe63066-22d5-43a1-8850-98c6f08146eb"
TEST_DUPLICATE_SCHEDULE_ID = "a19cb368-317c-4cf0-8332-b5a35f496691"

# `ops/db_migrate.sh` is bound to the production compose stack (`docker compose
# ps -q postgres`), so it cannot be pointed at the disposable database. Its loop
# is ported below and pinned in section 5b: if the shell changes, the port is no
# longer faithful and that fails rather than silently testing a fiction.


def _production_fingerprint() -> dict:
    """The production Postgres identity, from `.env`. Values are never printed."""
    values: dict = {}
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return {
        "host": values.get("LOG_PLATFORM_EXPECTED_POSTGRES_HOST", "127.0.0.1"),
        "port": values.get("LOG_PLATFORM_EXPECTED_POSTGRES_PORT", "5432"),
        # A missing .env must not mean "no production database exists".
        "dbname": values.get("LOG_PLATFORM_EXPECTED_POSTGRES_DB")
        or values.get("POSTGRES_DB")
        or "logdb",
        "identity": values.get("LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID", ""),
    }


def _refuse_target(code: str, detail: str) -> None:
    print(
        f"REFUSED: {ROLLBACK_DSN_ENV} is not a proven disposable database "
        f"({code}). {detail}"
    )
    sys.exit(2)


def require_disposable_postgres(dsn: str):
    """Prove the target is disposable, or exit. Never touches production.

    Four independent refusals, in increasing order of authority:

      1. loopback-only DSN — the repository-wide guard, applied before any
         connection is opened;
      2. the connection's own host/port must not be the production endpoint;
      3. the connected database must not carry the production name;
      4. the connected database must contain neither the production platform
         identity marker nor any pre-existing Workflow A control-plane rows.

    (2) and (3) are evaluated from libpq's resolved connection parameters, not
    from the DSN string, so a creative spelling cannot slip past them. (4) is
    the decisive one: production `logdb` holds client_account rows and would be
    refused here even if every earlier check had been defeated.
    """
    import psycopg

    require_loopback_dsn_or_exit(dsn, label=ROLLBACK_DSN_ENV)
    expected = _production_fingerprint()

    conn = psycopg.connect(dsn, autocommit=True)
    try:
        conn.execute("SET default_transaction_read_only = on")
        info = conn.info
        host = str(info.host or "")
        port = str(info.port or "")
        dbname = str(info.dbname or "")

        if host == expected["host"] and port == expected["port"]:
            _refuse_target(
                "PRODUCTION_ENDPOINT",
                "the connection resolves to the production PostgreSQL endpoint "
                "declared in .env. Run a separate disposable instance on another "
                "port.",
            )
        if dbname == expected["dbname"]:
            _refuse_target(
                "PRODUCTION_DATABASE_NAME",
                "the connected database carries the production database name. "
                "Create a differently named disposable database.",
            )

        marker = conn.execute(
            "SELECT to_regclass('ops_control.environment_identity') IS NOT NULL"
        ).fetchone()[0]
        if marker:
            rows = conn.execute(
                "SELECT count(*) FROM ops_control.environment_identity"
                " WHERE environment = 'production'"
                "   AND database_identity_id::text = %s",
                (expected["identity"],),
            ).fetchone()[0]
            if rows:
                _refuse_target(
                    "PRODUCTION_IDENTITY_MARKER",
                    "the database attests the production platform identity.",
                )

        control_plane = conn.execute(
            "SELECT to_regclass('workflow_a_control.client_account') IS NOT NULL"
        ).fetchone()[0]
        if control_plane:
            populated = conn.execute(
                "SELECT count(*) FROM workflow_a_control.client_account"
            ).fetchone()[0]
            if populated:
                _refuse_target(
                    "POPULATED_CONTROL_PLANE",
                    "the database already holds Workflow A client rows this suite "
                    "did not create. This suite drops and rebuilds the control "
                    "plane, so it will only do that to an empty database.",
                )
    finally:
        conn.close()

    return {"host": host, "port": port, "dbname": dbname}


def bootstrap_rollback_db(conn) -> None:
    """A control plane containing exactly the two tables the rollback names."""
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.schema_migrations")
        cur.execute(
            "CREATE TABLE public.schema_migrations ("
            " filename TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        for name in BOOTSTRAP_MIGRATIONS:
            cur.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO public.schema_migrations(filename) VALUES (%s)", (name,)
            )


def seed(
    conn,
    *,
    lookback: int = 3,
    frequency: str = "daily",
    ledgered: bool = True,
    rows: int = 1,
) -> None:
    """One deterministic pre-state. `rows` 0 = missing target, 2 = duplicates."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM workflow_a_control.client_dataset_schedule")
        cur.execute("DELETE FROM workflow_a_control.client_account")
        # The duplicate branch models a control plane whose uniqueness guarantee
        # is missing — the only state in which two rows can match at all, and
        # exactly the state the rollback's `matched > 1` refusal exists for.
        cur.execute(
            "ALTER TABLE workflow_a_control.client_dataset_schedule"
            " DROP CONSTRAINT IF EXISTS uq_client_dataset_schedule"
        )
        # M5 added a partial unique index enforcing one BASE schedule
        # per (client, dataset). Modelling an ambiguous schedule now
        # means defeating both structures, not just the constraint.
        cur.execute(
            "DROP INDEX IF EXISTS"
            " workflow_a_control.uq_client_dataset_schedule_base"
        )
        if rows < 2:
            cur.execute(
                "ALTER TABLE workflow_a_control.client_dataset_schedule"
                " ADD CONSTRAINT uq_client_dataset_schedule"
                " UNIQUE (client_id, dataset_name)"
            )
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_account
              (client_id, client_code, client_name, provider_type,
               provider_base_url, provider_basic_auth_username,
               provider_basic_auth_password_secret_ref, client_db_host,
               client_db_port, client_db_name, client_db_user,
               client_db_password_secret_ref, client_db_schema,
               speed_trigger_filter_text, enabled)
            VALUES (%s,%s,'Alpha','telematics','https://provider.invalid','u','REF',
                    '127.0.0.1',5432,'db','u','REF','public','speeding',true)
            """,
            (TEST_CLIENT_ID, TARGET_CLIENT_CODE),
        )
        for index, schedule_id in enumerate(
            (TEST_SCHEDULE_ID, TEST_DUPLICATE_SCHEDULE_ID)[:rows]
        ):
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_dataset_schedule
                  (schedule_id, client_id, client_code, dataset_name, enabled,
                   frequency, day_of_week, day_of_month, run_time, timezone,
                   lookback_days, overwrite_existing, event_enrichment_mode)
                VALUES (%s,%s,%s,%s,true,%s,%s,%s,'02:00:00','UTC',%s,true,'disabled')
                """,
                (
                    schedule_id, TEST_CLIENT_ID, TARGET_CLIENT_CODE, TARGET_DATASET,
                    frequency,
                    0 if frequency == "weekly" else None,
                    1 if frequency == "monthly" else None,
                    lookback + index,
                ),
            )
        cur.execute(
            "DELETE FROM public.schema_migrations WHERE filename = %s",
            (FORWARD_MIGRATION.name,),
        )
        if ledgered:
            cur.execute(
                "INSERT INTO public.schema_migrations(filename) VALUES (%s)",
                (FORWARD_MIGRATION.name,),
            )


def ledger_state(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT filename, applied_at FROM public.schema_migrations"
            " ORDER BY filename"
        )
        return [tuple(row) for row in cur.fetchall()]


def schedule_state(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schedule_id, dataset_name, frequency, lookback_days,"
            "       timezone, run_time, enabled, overwrite_existing,"
            "       event_enrichment_mode, updated_at"
            "  FROM workflow_a_control.client_dataset_schedule"
            " ORDER BY schedule_id"
        )
        return [tuple(row) for row in cur.fetchall()]


def lookback_of(conn, schedule_id: str = TEST_SCHEDULE_ID):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT lookback_days FROM workflow_a_control.client_dataset_schedule"
            " WHERE schedule_id = %s",
            (schedule_id,),
        )
        row = cur.fetchone()
        return None if row is None else row[0]


def execute_sql(conn, sql: str):
    """Run a whole SQL script the way `psql -v ON_ERROR_STOP=1 <file` would."""
    import psycopg

    notices: List[str] = []
    handler = lambda diagnostic: notices.append(diagnostic.message_primary or "")  # noqa: E731
    conn.add_notice_handler(handler)
    error = None
    try:
        conn.execute(sql)
    except psycopg.Error as exc:
        error = " ".join(str(exc).split())
        # The script's own BEGIN is still open and aborted; close it.
        conn.execute("ROLLBACK")
    finally:
        conn.remove_notice_handler(handler)
    return error, notices


def migration_runner_pass(conn, files) -> tuple:
    """`ops/db_migrate.sh`'s loop, ported onto the disposable connection.

    Statement for statement, in the order section 5b pins: ensure the ledger
    exists, then per file in sorted order — query the ledger, and on a hit skip
    the file and `continue` without executing it; otherwise apply the SQL and
    only then insert the ledger row. Raising out of the apply (rather than
    catching) is this port's `set -e`: the ledger INSERT below is unreachable
    for a migration that failed, which branch 11 proves.
    """
    applied: List[str] = []
    skipped: List[str] = []
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations("
            " filename TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    for name in sorted(files):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM public.schema_migrations WHERE filename = %s LIMIT 1",
                (name,),
            )
            if cur.fetchone() is not None:
                skipped.append(name)
                continue
        conn.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.schema_migrations(filename) VALUES (%s)", (name,)
            )
        applied.append(name)
    return applied, skipped


def run_rollback_matrix(dsn: str) -> None:
    import psycopg

    target = require_disposable_postgres(dsn)
    print(f"      (disposable target: db={target['dbname']} port={target['port']})")

    runner_files = list(BOOTSTRAP_MIGRATIONS) + [FORWARD_MIGRATION.name]
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        bootstrap_rollback_db(conn)

        # -- 1. valid rollback ------------------------------------------------
        seed(conn, lookback=3, ledgered=True)
        ledger_before = ledger_state(conn)
        before = schedule_state(conn)
        error, notices = execute_sql(conn, rollback_src)
        after = schedule_state(conn)
        check("branch 1 valid rollback: the transaction succeeds", error is None, str(error))
        check(
            "branch 1 valid rollback: the target row reads 1 afterwards",
            lookback_of(conn) == 1,
            f"got {lookback_of(conn)}",
        )
        check(
            "branch 1 valid rollback: exactly one row changed, and only its lookback",
            len(before) == len(after) == 1
            and before[0][3] == 3 and after[0][3] == 1
            and before[0][:3] == after[0][:3]
            and before[0][4:9] == after[0][4:9],
            f"{before} -> {after}",
        )
        check(
            "branch 1 valid rollback: the migration ledger is untouched",
            ledger_state(conn) == ledger_before,
        )
        check(
            "branch 1 valid rollback: 060 is still ledgered",
            any(row[0] == FORWARD_MIGRATION.name for row in ledger_state(conn)),
        )
        check(
            "branch 1 valid rollback: it says so, on the record",
            any("M2 rollback applied" in note for note in notices),
            f"notices: {notices}",
        )

        # -- 9. the load-bearing property: the next migrate run must SKIP 060 --
        applied, skipped = migration_runner_pass(conn, runner_files)
        check(
            "branch 9 runner: 060 is SKIPPED because it is still ledgered",
            FORWARD_MIGRATION.name in skipped and FORWARD_MIGRATION.name not in applied,
            f"applied={applied} skipped={skipped}",
        )
        check(
            "branch 9 runner: the migration runner applied nothing at all",
            applied == [],
            f"applied={applied}",
        )
        check(
            "branch 9 runner: L stays 1 after an ordinary migration run",
            lookback_of(conn) == 1,
            f"got {lookback_of(conn)}",
        )

        # -- 10. and the same run WOULD undo the rollback without the ledger --
        # Not a contract, a sensitivity proof: branch 9 asserts something that
        # is false the moment the ledger row goes away.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM public.schema_migrations WHERE filename = %s",
                (FORWARD_MIGRATION.name,),
            )
        applied, _skipped = migration_runner_pass(conn, runner_files)
        check(
            "branch 10 sensitivity: without the ledger row the runner RE-APPLIES 060",
            applied == [FORWARD_MIGRATION.name],
            f"applied={applied}",
        )
        check(
            "branch 10 sensitivity: and L is silently raised back to 3",
            lookback_of(conn) == 3,
            f"got {lookback_of(conn)}",
        )

        # -- 11. the port's apply-before-ledger ordering, behaviourally ---------
        # The pinned shell applies the SQL and only then writes the ledger, under
        # `set -euo pipefail`, so a migration that fails is never recorded. That
        # ordering is unobservable while everything succeeds, so it is observed
        # here through a real failure: 060 refuses an unexpected current value.
        seed(conn, lookback=5, ledgered=False)
        ledger_before = ledger_state(conn)
        failure = None
        try:
            migration_runner_pass(conn, runner_files)
        except psycopg.Error as exc:
            failure = " ".join(str(exc).split())
            if conn.info.transaction_status:
                conn.execute("ROLLBACK")
        check(
            "branch 11 apply-before-ledger: a failing migration aborts the runner pass",
            failure is not None and "M2 precondition failed" in failure,
            str(failure),
        )
        check(
            "branch 11 apply-before-ledger: the failed migration is NOT ledgered",
            ledger_state(conn) == ledger_before
            and not any(
                row[0] == FORWARD_MIGRATION.name for row in ledger_state(conn)
            ),
            f"{ledger_before} -> {ledger_state(conn)}",
        )
        check(
            "branch 11 apply-before-ledger: and it changed nothing",
            lookback_of(conn) == 5,
            f"got {lookback_of(conn)}",
        )

        # -- 3. already rolled back -------------------------------------------
        seed(conn, lookback=1, ledgered=True)
        ledger_before = ledger_state(conn)
        before = schedule_state(conn)
        error, notices = execute_sql(conn, rollback_src)
        check("branch 3 already rolled back: no error", error is None, str(error))
        check(
            "branch 3 already rolled back: it is a NOTICE-level no-op, not a refusal",
            any("M2 rollback already applied" in note for note in notices),
            f"notices: {notices}",
        )
        check(
            "branch 3 already rolled back: nothing was written",
            schedule_state(conn) == before and ledger_state(conn) == ledger_before,
        )

        # -- 2. migration 060 not ledgered -------------------------------------
        seed(conn, lookback=3, ledgered=False)
        ledger_before = ledger_state(conn)
        before = schedule_state(conn)
        error, _notices = execute_sql(conn, rollback_src)
        check(
            "branch 2 unledgered 060: the rollback REFUSES",
            error is not None and "is not recorded in public.schema_migrations" in error,
            str(error),
        )
        check(
            "branch 2 unledgered 060: no schedule mutation",
            schedule_state(conn) == before and lookback_of(conn) == 3,
        )
        check(
            "branch 2 unledgered 060: no ledger mutation either — it does not 'repair' the ledger",
            ledger_state(conn) == ledger_before,
        )

        # -- 4. unexpected current value ---------------------------------------
        seed(conn, lookback=5, ledgered=True)
        ledger_before = ledger_state(conn)
        before = schedule_state(conn)
        error, _notices = execute_sql(conn, rollback_src)
        check(
            "branch 4 unexpected L=5: the rollback REFUSES rather than 'repairing'",
            error is not None and "expected 3 (deployed) or 1 (already rolled back)" in error,
            str(error),
        )
        check(
            "branch 4 unexpected L=5: no mutation",
            schedule_state(conn) == before and ledger_state(conn) == ledger_before,
        )

        # -- 5. non-daily target -----------------------------------------------
        seed(conn, lookback=3, frequency="weekly", ledgered=True)
        ledger_before = ledger_state(conn)
        before = schedule_state(conn)
        error, _notices = execute_sql(conn, rollback_src)
        check(
            "branch 5 weekly target: the rollback REFUSES",
            error is not None and "not daily" in error,
            str(error),
        )
        check(
            "branch 5 weekly target: no mutation",
            schedule_state(conn) == before and ledger_state(conn) == ledger_before,
        )

        # -- 6. missing target row ---------------------------------------------
        seed(conn, rows=0, ledgered=True)
        ledger_before = ledger_state(conn)
        error, _notices = execute_sql(conn, rollback_src)
        check(
            "branch 6 missing target: the rollback REFUSES",
            error is not None
            and "no client_dataset_schedule row for ALPHA00001/trips_sync" in error,
            str(error),
        )
        check(
            "branch 6 missing target: no rows were invented, no ledger written",
            schedule_state(conn) == [] and ledger_state(conn) == ledger_before,
        )

        # -- 7. duplicate target rows -------------------------------------------
        seed(conn, lookback=3, rows=2, ledgered=True)
        ledger_before = ledger_state(conn)
        before = schedule_state(conn)
        error, _notices = execute_sql(conn, rollback_src)
        check(
            "branch 7 duplicate targets: the rollback REFUSES to guess",
            error is not None and "refusing to guess which one to revert" in error,
            str(error),
        )
        check(
            "branch 7 duplicate targets: neither row was touched",
            schedule_state(conn) == before and ledger_state(conn) == ledger_before,
        )

        # -- 8. ledger immutability, across every branch --------------------------
        # Every branch above compared the ledger before and after its own run.
        # This is the same property stated once, over the whole matrix: whatever
        # the pre-state and whichever way the rollback ends, the ledger rows —
        # filenames AND their `applied_at` — come out byte-identical.
        for pre_lookback, frequency, rows_count, ledgered in (
            (3, "daily", 1, True),      # succeeds
            (1, "daily", 1, True),      # NOTICE no-op
            (3, "daily", 1, False),     # refuses: unledgered
            (5, "daily", 1, True),      # refuses: unexpected value
            (3, "weekly", 1, True),     # refuses: not daily
            (3, "daily", 0, True),      # refuses: missing target
            (3, "daily", 2, True),      # refuses: duplicate targets
        ):
            seed(
                conn, lookback=pre_lookback, frequency=frequency,
                rows=rows_count, ledgered=ledgered,
            )
            before_ledger = ledger_state(conn)
            execute_sql(conn, rollback_src)
            check(
                f"branch 8 ledger immutability: unchanged after L={pre_lookback} "
                f"{frequency} rows={rows_count} ledgered={ledgered}",
                ledger_state(conn) == before_ledger,
                f"{before_ledger} -> {ledger_state(conn)}",
            )

        # -- mutation sensitivity: the assertions above are not vacuous ----------
        # Each mutant is the real artifact with one guard removed. If a mutant
        # behaved like the original, the corresponding branch would be proving
        # nothing.
        no_ledger_guard = rollback_src.replace("IF ledgered = 0 THEN", "IF false THEN")
        check("mutant A differs from the artifact", no_ledger_guard != rollback_src)
        seed(conn, lookback=3, ledgered=False)
        error, _notices = execute_sql(conn, no_ledger_guard)
        check(
            "mutant A (precondition ledger guard removed) STILL refuses — the ledger "
            "requirement is enforced twice, before and after the write",
            error is not None and "is no longer ledgered" in error
            and lookback_of(conn) == 3,
            f"error={error} lookback={lookback_of(conn)}",
        )

        # Both ledger guards removed. This is what branch 2 actually rules out:
        # an unledgered 060 rolled back and committed, which branch 10 then shows
        # the next migration run would silently undo.
        neither_ledger_guard = no_ledger_guard.replace(
            "IF final_ledgered = 0 THEN", "IF false THEN"
        )
        check(
            "mutant A2 differs from mutant A",
            neither_ledger_guard != no_ledger_guard,
            "the post-write ledger check moved; re-derive this mutant",
        )
        seed(conn, lookback=3, ledgered=False)
        error, _notices = execute_sql(conn, neither_ledger_guard)
        check(
            "mutant A2 (both ledger guards removed) rolls back an UNLEDGERED 060 — "
            "so branch 2 is load-bearing",
            error is None and lookback_of(conn) == 1,
            f"error={error} lookback={lookback_of(conn)}",
        )

        no_value_guard = rollback_src.replace(
            "IF current_lookback IS DISTINCT FROM deployed_lookback THEN",
            "IF false THEN",
        )
        check("mutant B differs from the artifact", no_value_guard != rollback_src)
        seed(conn, lookback=5, ledgered=True)
        error, _notices = execute_sql(conn, no_value_guard)
        check(
            "mutant B (value guard removed) still fails closed on the UPDATE predicate — "
            "the L=3 predicate is real defence in depth, not decoration",
            error is not None and "M2 rollback write anomaly" in error
            and lookback_of(conn) == 5,
            f"error={error} lookback={lookback_of(conn)}",
        )

        no_predicate = no_value_guard.replace(
            "   WHERE schedule_id = target_schedule_id\n"
            "     AND lookback_days = deployed_lookback;",
            "   WHERE schedule_id = target_schedule_id;",
        )
        check(
            "mutant C differs from mutant B",
            no_predicate != no_value_guard,
            "the UPDATE predicate text moved; re-derive this mutant",
        )
        seed(conn, lookback=5, ledgered=True)
        error, _notices = execute_sql(conn, no_predicate)
        check(
            "mutant C (value guard AND L=3 predicate removed) silently rewrites L=5 to 1 — "
            "so branch 4 is load-bearing",
            error is None and lookback_of(conn) == 1,
            f"error={error} lookback={lookback_of(conn)}",
        )

        # The ledger row deleted inside the rollback's own transaction, before it
        # verifies. This is the mutation the whole design exists to prevent: it
        # would leave a rollback the next migrate run quietly undoes.
        ledger_deleting = rollback_src.replace(
            "  SELECT lookback_days INTO final_lookback",
            "  DELETE FROM public.schema_migrations WHERE filename = forward_migration;\n"
            "  SELECT lookback_days INTO final_lookback",
        )
        check(
            "mutant D differs from the artifact",
            ledger_deleting != rollback_src,
            "the read-back block moved; re-derive this mutant",
        )
        seed(conn, lookback=3, ledgered=True)
        error, _notices = execute_sql(conn, ledger_deleting)
        check(
            "mutant D (ledger deleted by the rollback) is caught by the artifact's own "
            "verification step",
            error is not None and "is no longer ledgered" in error,
            f"error={error}",
        )
        check(
            "mutant D: and because it aborted, nothing was written",
            lookback_of(conn) == 3
            and any(row[0] == FORWARD_MIGRATION.name for row in ledger_state(conn)),
            f"lookback={lookback_of(conn)}",
        )
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
                cur.execute("DROP TABLE IF EXISTS public.schema_migrations")
        finally:
            conn.close()


rollback_dsn = os.getenv(ROLLBACK_DSN_ENV)
if not rollback_dsn:
    print()
    print(
        f"SKIP  section 6 (behavioural rollback matrix): set {ROLLBACK_DSN_ENV} to a "
        "disposable PostgreSQL 16 DSN to execute the real rollback SQL"
    )
else:
    print()
    run_rollback_matrix(rollback_dsn)

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print("ALL M2 RELEASE-CONTRACT CHECKS PASSED")
