#!/usr/bin/env python3
"""Focused tests for the manual Report 207 recovery workflow."""

from __future__ import annotations

from argparse import Namespace
from datetime import date
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import recover_report_207_speed_violations as recovery


def _scope() -> recovery.RecoveryScope:
    return recovery.RecoveryScope(
        client_code="BRAVO00016",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        raw_file_ids=("112b1b92-0000-0000-0000-000000000000",),
        source_artifact_ids=("22222222-0000-0000-0000-000000000000",),
    )


def _check(name: str, condition: bool, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"FAIL: {name}: {detail!r}")
    print(f"PASS: {name}")


def test_dry_run_guard_does_not_require_write_tokens() -> None:
    recovery.validate_execute_guards(
        scope=_scope(),
        execute=False,
        require_env_name=None,
        backup_confirmation_token=None,
        counter_recalc_confirmation_token=None,
        cleaned_csvs=[],
    )
    _check("dry-run does not require execute confirmation tokens", True)


def test_execute_refuses_without_explicit_guards() -> None:
    try:
        recovery.validate_execute_guards(
            scope=_scope(),
            execute=True,
            require_env_name=None,
            backup_confirmation_token=None,
            counter_recalc_confirmation_token=None,
            cleaned_csvs=[],
        )
    except recovery.RecoverySafetyError as exc:
        _check("execute refuses without environment guard", "--require-env-name" in str(exc), str(exc))
        return
    raise AssertionError("execute without guards did not fail")


def test_client_trips_source_lineage_ambiguity_stops_execute() -> None:
    scope = _scope()
    try:
        recovery.validate_execute_guards(
            scope=scope,
            execute=True,
            require_env_name="production",
            backup_confirmation_token=scope.backup_confirmation_token,
            counter_recalc_confirmation_token=None,
            cleaned_csvs=[],
        )
    except recovery.RecoverySafetyError as exc:
        _check(
            "execute detects client_trips raw/source lineage ambiguity",
            "client_trips does not store raw_file_id/source_artifact_id lineage" in str(exc),
            str(exc),
        )
        return
    raise AssertionError("execute without counter recalculation acknowledgement did not fail")


def test_cleanup_scope_is_limited_to_target_report_source() -> None:
    step = recovery.build_report_delete_sql(_scope())
    sql = " ".join(step.sql.split())
    _check("report cleanup deletes only report_207", 'DELETE FROM "telematics_reports"."report_207" AS r' in sql, sql)
    _check("report cleanup scopes raw_file_id", 'r."_raw_file_id" = ANY' in sql, sql)
    _check("report cleanup scopes source_artifact_id", 'r."_source_artifact_id" = ANY' in sql, sql)
    _check("report cleanup scopes date range", "AT TIME ZONE" in sql and "::date" in sql, sql)


def test_client_trips_base_rows_are_not_deleted() -> None:
    clear_step = recovery.build_clear_client_trips_sql(_scope())
    repop_step = recovery.build_repopulate_client_trips_sql(_scope())
    combined = f"{clear_step.sql}\n{repop_step.sql}"
    _check("client_trips recovery does not delete base trip rows", "DELETE FROM \"public\".\"client_trips\"" not in combined, combined)
    _check("client_trips clear uses UPDATE", 'UPDATE "public"."client_trips" AS t' in clear_step.sql, clear_step.sql)
    _check("client_trips clear only touches speeding buckets", "speeding_140_160_count" in clear_step.sql and "speeding_170_plus_count" in clear_step.sql, clear_step.sql)


def test_repopulate_is_assignment_based_and_idempotent() -> None:
    step = recovery.build_repopulate_client_trips_sql(_scope())
    sql = " ".join(step.sql.split())
    _check("repopulate sets counters from aggregate counts", 'SET "speeding_140_160_count" = tc.count_140_160' in sql, sql)
    _check("repopulate does not increment existing counters", '+ tc.count_140_160' not in sql and '+ i.inc_140_160' not in sql, sql)
    _check("repopulate marks matched report rows migrated", 'SET "migrated_to_client_db" = TRUE' in sql, sql)
    _check("repopulate preserves error reporting", "NO_MATCHING_TRIP" in sql and "AMBIGUOUS_TRIP_MATCH" in sql, sql)


def test_unrelated_clients_and_months_are_out_of_scope() -> None:
    clear_step = recovery.build_clear_client_trips_sql(_scope())
    _check("client_trips clear includes target client_code param", clear_step.params[0] == "BRAVO00016", clear_step.params)
    _check("client_trips clear includes requested date bounds", date(2026, 6, 30) in clear_step.params and date(2026, 6, 1) in clear_step.params, clear_step.params)
    report_step = recovery.build_report_counts_sql(_scope())
    _check("report checks include requested source scope params", list(_scope().raw_file_ids) in report_step.params and list(_scope().source_artifact_ids) in report_step.params, report_step.params)


def test_cleaned_csv_spec_requires_explicit_raw_file_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "cleaned.csv"
        csv_path.write_text("Data i czas;Nr rejestracyjny;Prędkość;Ograniczenie prędkości drogowej;Lokalizacja\n", encoding="utf-8")
        parsed = recovery._parse_local_cleaned_csv_specs([f"112b1b92={csv_path}"])
        _check("local cleaned CSV preserves explicit raw_file_id", parsed[0].raw_file_id == "112b1b92", parsed)
    try:
        recovery._parse_local_cleaned_csv_specs(["/tmp/no-raw-file.csv"])
    except recovery.RecoverySafetyError as exc:
        _check("local cleaned CSV without raw_file_id is rejected", "RAW_FILE_ID" in str(exc), str(exc))
        return
    raise AssertionError("cleaned CSV without raw_file_id did not fail")


def test_build_scope_normalizes_client_and_requires_valid_dates() -> None:
    args = Namespace(
        client_code="bravo00016",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
        raw_file_id=["a", "a", "b"],
        source_artifact_id=[],
    )
    scope = recovery.build_scope(args)
    _check("client_code is normalized", scope.client_code == "BRAVO00016", scope)
    _check("raw_file_ids are deduplicated", scope.raw_file_ids == ("a", "b"), scope)


def main() -> None:
    test_dry_run_guard_does_not_require_write_tokens()
    test_execute_refuses_without_explicit_guards()
    test_client_trips_source_lineage_ambiguity_stops_execute()
    test_cleanup_scope_is_limited_to_target_report_source()
    test_client_trips_base_rows_are_not_deleted()
    test_repopulate_is_assignment_based_and_idempotent()
    test_unrelated_clients_and_months_are_out_of_scope()
    test_cleaned_csv_spec_requires_explicit_raw_file_id()
    test_build_scope_normalizes_client_and_requires_valid_dates()


if __name__ == "__main__":
    main()
