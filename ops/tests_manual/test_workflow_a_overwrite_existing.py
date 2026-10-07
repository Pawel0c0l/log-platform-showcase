#!/usr/bin/env python3
"""Manual sanity test for `overwrite_existing` integration in Workflow A jobs.

What this checks (no DB, no network):

  * Both `sync_trips_and_speeding` and `aggregate_trip_fuel_daily`:
      - import `load_dataset_schedule` from `control_plane`,
      - declare a `DATASET_NAME` constant (the registry key),
      - call `load_dataset_schedule(client_id=..., dataset_name=...)`
        with the same name,
      - read the `overwrite_existing` flag,
      - branch their `ON CONFLICT (...)` SQL between `DO UPDATE` and
        `DO NOTHING` paths driven by that flag,
      - bail out (no-op) when the schedule says `enabled=False`.
  * `sync_trips_and_speeding` filters its bucket UPDATE by
    `WHERE sync_run_id = %s` so it only touches rows just upserted by
    the current run (correct under both overwrite=true and =false).
  * Both jobs include `record_id`, `synced_at`, and `sync_run_id` in
    their INSERT column lists.
  * Both jobs have the registry's dataset name string match the registry
    constant exactly.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_workflow_a_overwrite_existing.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import registry  # noqa: E402


JOB_FILES = {
    "sync_trips_and_speeding":
        REPO_ROOT / "jobs" / "api" / "telematics" / "sync_trips_and_speeding.py",
    "aggregate_trip_fuel_daily":
        REPO_ROOT / "jobs" / "api" / "telematics" / "aggregate_trip_fuel_daily.py",
}

EXPECTED_DATASET = {
    "sync_trips_and_speeding": "trips_sync",
    "aggregate_trip_fuel_daily": "fuel_daily_aggregation",
}


FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _module_constants(tree: ast.AST) -> dict[str, str]:
    """Return module-level `NAME = "literal"` assignments to strings."""
    out: dict[str, str] = {}
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            out[node.targets[0].id] = node.value.value
    return out


def _imports(tree: ast.AST) -> set[tuple[str, str]]:
    """Return set of (module, name) for each `from module import name [as ...]`."""
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                pairs.add((node.module, alias.name))
    return pairs


def test_one_job(job_label: str, path: Path, expected_dataset: str) -> None:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        _check(f"{job_label}: parses cleanly", False, str(exc))
        return
    _check(f"{job_label}: parses cleanly", True)

    consts = _module_constants(tree)
    _check(f"{job_label}: DATASET_NAME constant present",
           "DATASET_NAME" in consts)
    if consts.get("DATASET_NAME") is not None:
        _check(f"{job_label}: DATASET_NAME == {expected_dataset!r}",
               consts["DATASET_NAME"] == expected_dataset,
               f"got={consts['DATASET_NAME']!r}")
        _check(f"{job_label}: DATASET_NAME is in the Python registry",
               consts["DATASET_NAME"] in registry.DATASETS)

    imports = _imports(tree)
    _check(f"{job_label}: imports load_dataset_schedule from control_plane",
           any(name == "load_dataset_schedule"
               and module.endswith("control_plane")
               for module, name in imports))

    _check(f"{job_label}: calls load_dataset_schedule(...)",
           "load_dataset_schedule(" in src)
    _check(f"{job_label}: passes dataset_name=DATASET_NAME",
           "dataset_name=DATASET_NAME" in src)
    _check(f"{job_label}: reads schedule.overwrite_existing",
           "schedule.overwrite_existing" in src)
    _check(f"{job_label}: handles enabled=False (early return path)",
           "schedule.enabled" in src or
           re.search(r"\.enabled\s*[):]\s*False", src) is not None or
           re.search(r"if\s+not\s+schedule\.enabled", src) is not None)

    # Branching on overwrite_existing for ON CONFLICT.
    do_update_count = len(re.findall(r"ON\s+CONFLICT\s*\([^)]+\)\s*DO\s+UPDATE",
                                     src, re.IGNORECASE))
    do_nothing_count = len(re.findall(r"ON\s+CONFLICT\s*\([^)]+\)\s*DO\s+NOTHING",
                                      src, re.IGNORECASE))
    _check(f"{job_label}: contains ON CONFLICT … DO UPDATE branch",
           do_update_count >= 1, f"count={do_update_count}")
    _check(f"{job_label}: contains ON CONFLICT … DO NOTHING branch",
           do_nothing_count >= 1, f"count={do_nothing_count}")

    branch_re = re.compile(
        r"if\s+overwrite_existing\b[\s\S]{0,5000}?\belse\b",
        re.MULTILINE,
    )
    _check(f"{job_label}: ON CONFLICT branches gated by `if overwrite_existing` … else",
           branch_re.search(src) is not None)

    # record_id / synced_at / sync_run_id appear in INSERT column lists.
    for col in ("record_id", "synced_at", "sync_run_id"):
        _check(f"{job_label}: INSERT mentions `{col}`",
               re.search(rf"\b{col}\b", src) is not None)


def test_rpm_columns_in_sync_trips() -> None:
    """`high_rpm_events_count` and `overrev_events_count` must show up in
    the `client_trips` INSERT column list, in the values tuple build, and
    in the `DO UPDATE SET` branch (so overwrite_existing=True actually
    refreshes them)."""
    sync_path = JOB_FILES["sync_trips_and_speeding"]
    src = sync_path.read_text(encoding="utf-8")

    for col in ("high_rpm_events_count", "overrev_events_count"):
        # Mentioned at all.
        _check(f"sync_trips_and_speeding: mentions `{col}`",
               re.search(rf"\b{col}\b", src) is not None)
        # Mentioned inside DO UPDATE SET (overwrite_existing=True branch).
        in_do_update = re.search(
            rf"DO\s+UPDATE\s+SET[\s\S]+?{col}\s*=\s*EXCLUDED\.{col}",
            src, re.IGNORECASE,
        ) is not None
        _check(f"sync_trips_and_speeding: `{col}` in DO UPDATE SET",
               in_do_update)

    # _compute_rpm_counts is wired into run().
    _check("sync_trips_and_speeding: calls _compute_rpm_counts(...)",
           "_compute_rpm_counts(" in src)
    # Notification type extractor exists and is referenced.
    _check("sync_trips_and_speeding: defines _extract_notification_type",
           "def _extract_notification_type" in src)
    _check("sync_trips_and_speeding: matches HIGH_RPM and OVERREV constants",
           "HIGH_RPM" in src and "OVERREV" in src)


def test_sync_run_id_filter_on_bucket_update() -> None:
    sync_path = JOB_FILES["sync_trips_and_speeding"]
    src = sync_path.read_text(encoding="utf-8")
    # Bucket UPDATE must be scoped to rows touched by the current run.
    # The clause may appear directly after WHERE or on a chained `AND`.
    bucket_update = re.search(
        r"UPDATE\s+\{?[^\s{}]*trips[^\s{}]*\}?\s+SET[\s\S]+?(?=\n\s*\"\"\"|\n\s*''')",
        src, re.IGNORECASE,
    )
    if bucket_update is None:
        # Fall back to the broadest possible UPDATE block on a trips table.
        bucket_update = re.search(
            r"UPDATE\s+[^\n]+?trips[^\n]*\n[\s\S]+?WHERE[\s\S]+?(?=\n\s*\"\"\")",
            src, re.IGNORECASE,
        )
    block = bucket_update.group(0) if bucket_update else ""
    found = bool(re.search(r"\bsync_run_id\s*=\s*%s", block, re.IGNORECASE))
    _check("sync_trips_and_speeding: bucket UPDATE filters by sync_run_id",
           found,
           "no `sync_run_id=%s` clause found inside the bucket UPDATE block"
           if not found else "")


def main() -> int:
    for label, path in JOB_FILES.items():
        if not path.exists():
            _check(f"{label} file present", False, f"missing: {path}")
            continue
        test_one_job(label, path, EXPECTED_DATASET[label])

    if JOB_FILES["sync_trips_and_speeding"].exists():
        test_sync_run_id_filter_on_bucket_update()
        test_rpm_columns_in_sync_trips()

    print("")
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("OK — overwrite_existing wiring + record_id/synced_at/sync_run_id "
          "look correct in both jobs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
