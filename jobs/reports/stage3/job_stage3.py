from __future__ import annotations

import csv
import json
import math
import os
import re
import tempfile
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from api.timezone_utils import set_pg_session_timezone
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.common.environment_identity import EnvironmentIdentityError
from jobs.reports.stage3.batch_contract import (
    Stage3BatchError,
    Stage3BatchResult,
    Stage3ItemResult,
    Stage3Outcome,
)
from jobs.reports.stage3.schema_readiness import (
    MissingSchemaObject,
    RuntimeSchemaMutationDisabledError,
    Stage3SchemaReadinessError,
)
# P0-G. The *same* resolver the orchestrator runs after the load, imported here
# so the pre-write gate and the post-load requirement cannot disagree. Two
# independent notions of "is this client configured" is how the gap opened.
# Import direction is safe: the selector module depends only on
# jobs.trip_metrics_population_source, so there is no cycle back into Stage 3.
from jobs.reports.workflow_b.trip_metrics_selector import (
    WorkflowBSelectorResolutionError,
    load_report_policy_selector_state,
    resolve_workflow_b_trip_metrics_population_source,
)
# P0-E. Recovery classification for durable states a crashed or failed prior
# attempt can leave behind. Kept in its own module because the decision is a
# pure function of (status, age, destination evidence) and must stay testable
# without a database.
from jobs.reports.stage3.recovery import (
    DEFAULT_STAGE3_STALE_GRACE_MINUTES,
    STATUS_OK,
    DestinationEvidence,
    Stage3LoadStrategy,
    Stage3RecoveryClass,
    Stage3RecoveryDecision,
    classify_stage3_recovery,
    format_stage3_error_evidence,
    probe_destination_evidence,
    stage3_file_advisory_lock_key,
)


SOURCE = "jobs.reports.stage3.job_stage3"
DESTINATION_SCHEMA = "telematics_reports"
ALPHA_GPS_REPORT_TYPE = "Alpha_GPS_Baza_LOG"
ALPHA_GPS_TARGET_SCHEMA = "telematics_reports"
ALPHA_GPS_TARGET_TABLE = "Alpha_GPS_Baza_LOG"
ALPHA_GPS_REQUIRED_COLUMNS = (
    "ID",
    "Nr rejestracyjny",
    "Data przydziału",
    "Nazwa Pliku csv",
)
WORKFLOW_NAME = "workflow_b"
STAGE1_FETCH_STAGE = "stage_1_fetch"
STAGE2_CLEAN_STAGE = "stage_2_clean"
STAGE3_STAGE = "stage_3_load"
TECHNICAL_COLUMNS = (
    "_loaded_at",
    "_raw_file_id",
    "_source_artifact_id",
    "_source_filename",
    "_stage3_run_id",
)
TECHNICAL_INSERT_COLUMNS = (
    "_raw_file_id",
    "_source_artifact_id",
    "_source_filename",
    "_stage3_run_id",
)
SAFE_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ClientDbConfig:
    client_code: str
    client_id: str | None
    client_db_host: str
    client_db_port: int
    client_db_name: str
    client_db_user: str
    client_db_password_secret_ref: str
    client_db_sslmode: str
    client_db_environment: str | None = None
    client_db_identity_id: str | None = None


@dataclass(frozen=True)
class Candidate:
    raw_file_id: str
    client_code: str
    report_type: str
    source_filename: str | None
    source_sha256: str | None = None
    # P0-E. Durable state the row already carried when discovery selected it. A
    # first attempt leaves these empty; a crashed or failed prior attempt does
    # not, and `_process_candidate` must consult the destination before it may
    # replay anything. Carried on the candidate rather than re-queried later so
    # the decision is made from the same snapshot discovery selected on.
    stage3_status: str | None = None
    stage3_started_at: Any = None
    stage3_destination_schema: str | None = None
    stage3_destination_table: str | None = None
    # The cleaned artifact this attempt would load. It is what makes the
    # recovery decision attempt-scoped rather than raw-file-scoped: a Stage 2
    # re-clean mints a new artifact id, so rows from a previous generation can
    # never be mistaken for this attempt's work.
    stage2_cleaned_artifact_id: str | None = None
    # Carries the durable retryability marker written by `_mark_stage3_error`.
    stage3_error: str | None = None


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    filename: str | None
    original_filename: str | None
    display_filename: str | None


@dataclass
class LoadResult:
    raw_file_id: str
    client_code: str
    report_type: str
    destination_schema: str
    destination_table: str
    data_overwrite: bool
    input_rows: int
    inserted_rows: int
    updated_rows: int
    skipped_rows: int
    status: str
    source_artifact_id: str | None = None
    source_filename: str | None = None
    destination_database: str | None = None
    error: str | None = None
    rejected_rows: list[dict[str, str]] | None = None
    raw_artifact_id: str | None = None
    normalized_artifact_id: str | None = None
    cleaned_artifact_id: str | None = None
    # P0-E. True only when this result finalized an interrupted attempt from
    # durable destination evidence instead of performing a load. Carried on the
    # result rather than inferred from row counts, because a genuine load can
    # also legitimately insert zero rows.
    recovered_reconciled: bool = False


def process_stage3_batch(client, run_id: str, params: dict) -> Stage3BatchResult:
    params = params or {}
    raw_file_id = _optional_str(params.get("raw_file_id"))
    limit = _optional_int(params.get("limit"))
    force_reprocess = bool(params.get("force_reprocess", False))
    dry_run = _bool_param(params.get("dry_run", False))
    persist_dry_run_result = _bool_param(params.get("persist_dry_run_result", False))
    auto_grant_permissions = _bool_param(params.get("auto_grant_permissions", False))

    client.log(
        "INFO",
        "SCRIPT",
        SOURCE,
        "Workflow B Stage 3 load started",
        run_id=run_id,
        context={
            "raw_file_id": raw_file_id,
            "limit": limit,
            "force_reprocess": force_reprocess,
            "dry_run": dry_run,
            "persist_dry_run_result": persist_dry_run_result,
            "auto_grant_permissions": auto_grant_permissions,
        },
    )

    batch = Stage3BatchResult()

    if auto_grant_permissions:
        exc = RuntimeSchemaMutationDisabledError("auto_grant_permissions")
        batch.items.append(
            _stage3_failure_item(None, exc, dry_run=dry_run, force_reprocess=force_reprocess)
        )
        raise Stage3BatchError(batch) from exc

    try:
        platform_context = _platform_pg_conn()
    except Exception as exc:
        batch.items.append(_stage3_failure_item(None, exc, dry_run=dry_run, force_reprocess=force_reprocess))
        raise Stage3BatchError(batch) from exc

    with platform_context as platform_conn:
        try:
            candidates = _select_stage3_candidates(
                platform_conn,
                raw_file_id=raw_file_id,
                limit=limit,
                force_reprocess=force_reprocess,
            )
        except Exception as exc:
            batch.items.append(_stage3_failure_item(None, exc, dry_run=dry_run, force_reprocess=force_reprocess))
            raise Stage3BatchError(batch) from exc
        batch.discovered_candidate_count = len(candidates)
        batch.eligible_count = len(candidates)

        if not candidates:
            diagnostics = _stage3_no_pending_diagnostics(
                platform_conn,
                raw_file_id=raw_file_id,
            )
            batch.items.extend(
                _stage3_items_from_diagnostics(
                    diagnostics,
                    dry_run=dry_run,
                    force_reprocess=force_reprocess,
                )
            )
            client.log(
                "INFO",
                "SCRIPT",
                SOURCE,
                "No Workflow B Stage 3 pending reports found",
                run_id=run_id,
                context={"raw_file_id": raw_file_id, **diagnostics},
            )
            return batch

        for candidate in candidates:
            try:
                if dry_run:
                    legacy_result = _dry_run_candidate(
                        platform_conn,
                        client,
                        run_id,
                        candidate,
                        persist_result=persist_dry_run_result,
                        auto_grant_permissions=auto_grant_permissions,
                    )
                else:
                    legacy_result = _process_candidate(
                        platform_conn,
                        client,
                        run_id,
                        candidate,
                        auto_grant_permissions=auto_grant_permissions,
                    )
                item = _stage3_item_from_legacy(
                    candidate,
                    legacy_result,
                    dry_run=dry_run,
                    force_reprocess=force_reprocess,
                )
                batch.items.append(item)
                client.log(
                    "INFO",
                    "SCRIPT",
                    SOURCE,
                    "Workflow B Stage 3 raw file dry-run validated" if dry_run else "Workflow B Stage 3 raw file processed",
                    run_id=run_id,
                    context=item.to_dict(),
                )
                if dry_run:
                    print(json.dumps(legacy_result, sort_keys=True), flush=True)
            except Exception as exc:
                _rollback_quietly(platform_conn)
                batch.items.append(
                    _stage3_failure_item(
                        candidate,
                        exc,
                        dry_run=dry_run,
                        force_reprocess=force_reprocess,
                    )
                )
                client.log(
                    "ERROR",
                    "SCRIPT",
                    SOURCE,
                    f"Workflow B Stage 3 raw file failed: {type(exc).__name__}: {exc}",
                    run_id=run_id,
                    context={
                        "raw_file_id": candidate.raw_file_id,
                        "client_code": candidate.client_code,
                        "report_type": candidate.report_type,
                    },
                    error=traceback.format_exc(),
                )

    client.log(
        "INFO",
        "SCRIPT",
        SOURCE,
        "Workflow B Stage 3 load finished",
        run_id=run_id,
        context={
            **{key: value for key, value in batch.to_dict().items() if key not in {"items", "successful_load_identities"}},
            "dry_run": dry_run,
            "auto_grant_permissions": auto_grant_permissions,
        },
    )

    if batch.has_failures:
        raise Stage3BatchError(batch)
    return batch


def run(client, run_id: str, params: dict) -> Stage3BatchResult:
    """Backward-compatible runner entrypoint returning the reusable typed result."""
    return process_stage3_batch(client, run_id, params)


def _stage3_item_from_legacy(
    candidate: Candidate,
    result: LoadResult | dict[str, Any],
    *,
    dry_run: bool,
    force_reprocess: bool,
) -> Stage3ItemResult:
    if isinstance(result, dict):
        status = str(result.get("dry_run_status") or "ERROR")
        outcome = (
            Stage3Outcome.DRY_RUN_SUCCEEDED
            if status in {"OK", "WARNING"}
            else Stage3Outcome.REJECTED_VALIDATION
        )
        return Stage3ItemResult(
            raw_file_id=candidate.raw_file_id,
            client_code=candidate.client_code,
            report_type=candidate.report_type,
            outcome=outcome,
            destination_schema=result.get("destination_schema"),
            destination_table=result.get("destination_table"),
            persisted_status=None,
            error_category="dry_run_validation" if status == "ERROR" else None,
            operator_action_required=status == "ERROR",
            dry_run=True,
            force_reprocess=force_reprocess,
            inserted_rows=int(result.get("would_insert_rows") or 0),
            updated_rows=int(result.get("would_update_rows") or 0),
            skipped_rows=int(result.get("would_skip_rows") or 0),
            rejected_rows=int(result.get("would_reject_rows") or 0),
            source_cleaned_artifact_id=result.get("source_artifact_id"),
        )
    if result.status == "SKIPPED_NO_RECORD_ID":
        outcome = Stage3Outcome.SKIPPED_INELIGIBLE
    elif getattr(result, "recovered_reconciled", False):
        outcome = Stage3Outcome.RECOVERED_RECONCILED
    else:
        outcome = Stage3Outcome.LOADED
    return Stage3ItemResult(
        raw_file_id=result.raw_file_id,
        client_code=result.client_code,
        report_type=result.report_type,
        outcome=outcome,
        destination_schema=result.destination_schema,
        destination_table=result.destination_table,
        persisted_status=result.status,
        dry_run=False,
        force_reprocess=force_reprocess,
        inserted_rows=result.inserted_rows,
        updated_rows=result.updated_rows,
        skipped_rows=result.skipped_rows,
        rejected_rows=len(result.rejected_rows or []),
        source_cleaned_artifact_id=result.cleaned_artifact_id or result.source_artifact_id,
    )


def _stage3_items_from_diagnostics(
    diagnostics: dict[str, Any],
    *,
    dry_run: bool,
    force_reprocess: bool,
) -> list[Stage3ItemResult]:
    items: list[Stage3ItemResult] = []
    for row in diagnostics.get("latest_stage2_ok_rows") or []:
        reasons = set(row.get("exclusion_reasons") or [])
        if "stage3_status_ok" in reasons:
            outcome = Stage3Outcome.SKIPPED_COMPLETED
        elif reasons & {"missing_client_code", "missing_stage2_report_type", "missing_stage2_cleaned_artifact"}:
            outcome = Stage3Outcome.SKIPPED_ROUTING
        else:
            outcome = Stage3Outcome.SKIPPED_INELIGIBLE
        items.append(Stage3ItemResult(
            raw_file_id=str(row.get("raw_file_id") or "unknown"),
            client_code=row.get("client_code"),
            report_type=row.get("report_type"),
            outcome=outcome,
            persisted_status=row.get("stage3_status"),
            error_category=sorted(reasons)[0] if reasons else None,
            dry_run=dry_run,
            force_reprocess=force_reprocess,
        ))
    return items


def classify_stage3_exception(
    exc: Exception, *, has_candidate: bool
) -> tuple[Stage3Outcome, str, bool, bool]:
    """One place that decides what a Stage 3 exception *means*.

    P0-E. Extracted because two consumers must never disagree: the in-memory
    `Stage3ItemResult` this cycle reports, and the durable retryability marker
    `_mark_stage3_error` writes into `stage3_error`. If they could drift, a
    failure reported as non-retryable could still be retried forever after a
    restart, which is exactly the distinction the durable marker exists to keep.

    Returns (outcome, category, retryable, operator_action_required).
    """
    text = str(exc).lower()
    if isinstance(exc, Stage3SchemaReadinessError):
        return Stage3Outcome.FAILED_SCHEMA_NOT_READY, "schema_not_ready", False, True
    if isinstance(exc, RuntimeSchemaMutationDisabledError):
        return (
            Stage3Outcome.FAILED_NON_RETRYABLE_CONFIGURATION,
            "runtime_schema_mutation_disabled",
            False,
            True,
        )
    if isinstance(exc, EnvironmentIdentityError):
        return Stage3Outcome.FAILED_ENVIRONMENT_IDENTITY, "environment_identity", False, True
    if isinstance(exc, PermissionError) or "permission denied" in text or "insufficient privilege" in text:
        return Stage3Outcome.FAILED_PERMISSION, "database_permission", False, True
    if isinstance(exc, Stage3WriterValidationError):
        # Deterministic refusal. The next scheduled fire sees the same immutable
        # cleaned artifact, the same destination shape and the same
        # configuration, so autonomous replay cannot succeed and would only
        # red-cycle Workflow B at 06:00 and 20:00 indefinitely. The signal comes
        # from the exception, never from its message text.
        return (
            Stage3Outcome.FAILED_WRITER_VALIDATION,
            f"writer_validation_{exc.signal}",
            False,
            True,
        )
    if isinstance(exc, (ValueError, KeyError, TypeError)) or "unsafe sql identifier" in text:
        return (
            Stage3Outcome.FAILED_NON_RETRYABLE_CONFIGURATION,
            "configuration_or_validation",
            False,
            True,
        )
    if not has_candidate:
        return (
            Stage3Outcome.FAILED_RETRYABLE_INFRASTRUCTURE,
            "platform_selection_or_connection",
            True,
            False,
        )
    # What is left is genuinely plausibly transient: psycopg OperationalError and
    # connection loss, and the requests-level failures raised while downloading
    # the cleaned artifact. Every deterministic refusal has been claimed above by
    # Stage3WriterValidationError, so this bucket no longer silently absorbs one.
    return Stage3Outcome.FAILED_DATABASE_LOAD, "candidate_load", True, False


def _stage3_failure_item(
    candidate: Candidate | None,
    exc: Exception,
    *,
    dry_run: bool,
    force_reprocess: bool,
) -> Stage3ItemResult:
    text = str(exc).lower()
    if isinstance(exc, Stage3RecoveryNotOwnedError):
        # P0-E. Shapes with opposite meanings share this exception, so they must
        # not share an outcome. AWAIT_LIVE_OWNER and AWAIT_GRACE are plain skips
        # — another process legitimately owns the file, or it is too young to
        # touch, and this cycle correctly stood aside; nothing is wrong and
        # nothing is owed to an operator. Anything else needs a human, and is
        # surfaced with operator_action_required so it appears in every cycle's
        # review items until resolved.
        if exc.recovery_class in {
            Stage3RecoveryClass.AWAIT_GRACE,
            Stage3RecoveryClass.AWAIT_LIVE_OWNER,
        }:
            return Stage3ItemResult(
                raw_file_id=exc.raw_file_id,
                client_code=exc.client_code,
                report_type=exc.report_type,
                outcome=Stage3Outcome.SKIPPED_RECOVERY_DEFERRED,
                error_category=f"recovery_{exc.recovery_class.value.lower()}",
                retryable=True,
                operator_action_required=False,
                error_detail=exc.decision.reason,
                dry_run=dry_run,
                force_reprocess=force_reprocess,
            )
        return Stage3ItemResult(
            raw_file_id=exc.raw_file_id,
            client_code=exc.client_code,
            report_type=exc.report_type,
            outcome=Stage3Outcome.BLOCKED_RECOVERY_OPERATOR,
            error_category=f"recovery_{exc.recovery_class.value.lower()}",
            retryable=False,
            operator_action_required=True,
            error_detail=exc.decision.reason,
            dry_run=dry_run,
            force_reprocess=force_reprocess,
        )
    if isinstance(exc, Stage3LoadPolicyNotConfiguredError):
        # BLOCKED_OPERATOR_ACTION, not FAILED_*: nothing technical went wrong and
        # nothing will change until a human configures the policy. It still
        # counts in `has_failures`, so the run does not report success — but the
        # row keeps a NULL stage3_status, so the very next cycle retries it for
        # free once the configuration exists.
        return Stage3ItemResult(
            raw_file_id=candidate.raw_file_id if candidate else "batch",
            client_code=exc.client_code,
            report_type=exc.report_type,
            outcome=Stage3Outcome.BLOCKED_OPERATOR_ACTION,
            error_category=exc.category,
            retryable=False,
            operator_action_required=True,
            error_detail=str(exc),
            dry_run=dry_run,
            force_reprocess=force_reprocess,
        )
    outcome, category, retryable, operator_action = classify_stage3_exception(
        exc, has_candidate=candidate is not None
    )
    error_detail = (
        str(exc)
        if isinstance(exc, (Stage3SchemaReadinessError, RuntimeSchemaMutationDisabledError))
        else None
    )
    return Stage3ItemResult(
        raw_file_id=candidate.raw_file_id if candidate else "batch",
        client_code=candidate.client_code if candidate else None,
        report_type=candidate.report_type if candidate else None,
        outcome=outcome,
        error_category=category,
        retryable=retryable,
        operator_action_required=operator_action,
        error_detail=error_detail,
        dry_run=dry_run,
        force_reprocess=force_reprocess,
    )


def _dry_run_candidate(
    platform_conn,
    client,
    run_id: str,
    candidate: Candidate,
    *,
    persist_result: bool = False,
    auto_grant_permissions: bool = False,
) -> dict[str, Any]:
    if auto_grant_permissions:
        raise RuntimeSchemaMutationDisabledError("auto_grant_permissions")
    try:
        destination_schema, destination_table = _destination_for_report_type(candidate.report_type)
        with platform_conn.cursor() as cur:
            artifact = _find_stage2_cleaned_artifact(
                cur,
                raw_file_id=candidate.raw_file_id,
                report_type=candidate.report_type,
            )
            data_overwrite, policy_found = _load_data_overwrite_policy(
                cur,
                client_code=candidate.client_code,
                report_type=candidate.report_type,
            )
            db_config = _load_client_account_by_code(cur, candidate.client_code)
            lineage = _stage1_artifact_lineage(cur, raw_file_id=candidate.raw_file_id)

        artifact_filename = (
            artifact.display_filename
            or artifact.original_filename
            or artifact.filename
            or f"{artifact.artifact_id}.csv"
        )
        with tempfile.TemporaryDirectory(prefix="workflow-b-stage3-dry-run-") as tmpdir:
            input_path = Path(tmpdir) / _safe_download_filename(artifact_filename)
            client.download_artifact(artifact.artifact_id, str(input_path))
            _require_nonempty_artifact_file(input_path, artifact.artifact_id)
            df = _read_cleaned_csv(input_path)
            with _client_business_pg_conn(db_config) as destination_conn:
                with destination_conn.cursor() as cur:
                    if _is_alpha_gps_report(candidate.report_type):
                        plan = _build_alpha_gps_replace_all_plan(
                            cur,
                            df=df,
                            destination_schema=destination_schema,
                            destination_table=destination_table,
                        )
                    else:
                        plan = _build_load_plan(
                            cur,
                            df=df,
                            data_overwrite=data_overwrite,
                            destination_schema=destination_schema,
                            destination_table=destination_table,
                        )

        summary = _dry_run_summary(
            candidate=candidate,
            artifact=artifact,
            source_filename=artifact_filename,
            data_overwrite=True if _is_alpha_gps_report(candidate.report_type) else data_overwrite,
            policy_found=policy_found,
            destination_schema=destination_schema,
            destination_table=destination_table,
            destination_database=db_config.client_db_name,
            plan=plan,
            would_auto_grant_permissions=auto_grant_permissions,
        )
        summary["raw_artifact_id"] = lineage.get("raw_artifact_id")
        summary["normalized_artifact_id"] = lineage.get("normalized_artifact_id")
        if persist_result:
            _upload_stage3_dry_run_artifact(client, run_id, artifact, summary)
        return summary
    except (EnvironmentIdentityError, PermissionError):
        _rollback_quietly(platform_conn)
        raise
    except Exception as exc:
        _rollback_quietly(platform_conn)
        return _dry_run_error_summary(
            candidate,
            exc,
            would_auto_grant_permissions=auto_grant_permissions,
        )


class Stage3LoadPolicyNotConfiguredError(RuntimeError):
    """A required per-client Stage 3 policy is missing, invalid or incompatible.

    Raised strictly before the first irreversible client/business-data write for
    the file, which is the whole point: the same condition used to surface only
    after Stage 3 had already committed rows, from the orchestrator's
    postprocessor discovery, leaving customer data changed by a failed run.

    Not a ValueError. `_stage3_failure_item` classifies bare ValueError as
    FAILED_NON_RETRYABLE_CONFIGURATION, and this needs its own branch so the
    operator sees *which* configuration is missing rather than a generic
    validation failure.
    """

    def __init__(self, category: str, message: str, *, client_code: str, report_type: str):
        self.category = category
        self.client_code = client_code
        self.report_type = report_type
        super().__init__(f"{message} (client_code={client_code}, report_type={report_type})")


class Stage3WriterValidationError(RuntimeError):
    """A Stage 3 writer or validator deterministically refused its input.

    P0-E / second review. The durable `stage3_error` marker decides whether a
    later cycle may replay a failed attempt, and the question that marker has to
    answer is precise:

        "Can repeating this exact attempt against unchanged durable input and
        configuration plausibly succeed?"

    For every raise site converted to this type the answer is no. The cleaned
    artifact is immutable, the destination shape is what it is, and the client
    account row either exists or does not — so the next 06:00 fire, and the one
    after it, reproduce the identical failure. Classifying these as retryable
    (which the generic `RuntimeError` fallback did) turns one bad input into a
    red cycle twice a day forever, with no path to resolution.

    Deliberately a *subclass* of RuntimeError, for two reasons: existing
    handlers such as `_build_load_plan`'s `except RuntimeError` keep working
    unchanged, and callers that only care "this attempt failed" are unaffected.

    `signal` is a stable machine identifier, never a human message. The
    classifier keys on the exception type and this field, so retryability is
    never decided by matching text like "invalid date" — a message someone could
    reword without realising they had changed production recovery behaviour.
    """

    def __init__(self, message: str, *, signal: str):
        self.signal = signal
        super().__init__(message)


class Stage3RecoveryNotOwnedError(RuntimeError):
    """P0-E. This cycle discovered the row but must not act on it.

    Two distinct situations, both of which must leave the durable row exactly as
    found:

      * AWAIT_GRACE — another process legitimately holds it. Touching it would
        race a live load.
      * TERMINAL_OPERATOR — the state is terminal by design (a deliberate data
        refusal), or unclassifiable, or its destination evidence could not be
        read. Replaying any of those is either useless or unsafe.

    It is an error type rather than a silent skip because the file must remain
    *visible*: it is reported every cycle with an explicit next owner, which is
    the property whose absence made these states permanent orphans.
    """

    def __init__(self, decision, *, raw_file_id: str, client_code: str, report_type: str):
        self.decision = decision
        self.recovery_class = decision.recovery_class
        self.raw_file_id = raw_file_id
        self.client_code = client_code
        self.report_type = report_type
        super().__init__(
            f"Stage 3 recovery not owned by this cycle "
            f"({decision.recovery_class.value}: {decision.reason}; "
            f"raw_file_id={raw_file_id}, client_code={client_code}, report_type={report_type})"
        )


def _require_downstream_policy_configured(platform_conn, candidate: Candidate) -> None:
    """Fail closed unless this file's post-load handling is fully configured.

    Runs exactly the resolution `_discover_postprocessor_plans` performs after a
    successful load, on inputs that are entirely static — a client code and a
    report type. Nothing here depends on the load having happened, which is why
    it can be asked first.

    Both refusal shapes are covered:
      * the resolver raising (missing policy row, malformed or unknown selector);
      * the resolver returning a resolution whose `error_category` is set, i.e.
        a selector that is valid in itself but incompatible with this report
        type. That produces `unsupported_configured_postprocessor` downstream,
        which blocks just as hard.

    A resolution with no error category — including `disabled` and the
    Workflow A source, which create no plan at all — passes untouched, so the
    configured clients keep their existing behaviour exactly.
    """
    with platform_conn.cursor() as cur:
        state = load_report_policy_selector_state(
            cur, client_code=candidate.client_code, report_type=candidate.report_type
        )
    _rollback_quietly(platform_conn)
    try:
        resolution = resolve_workflow_b_trip_metrics_population_source(
            report_type=candidate.report_type,
            client_default=state.client_default,
            report_override=state.report_override,
            load_policy_found=state.load_policy_found,
        )
    except WorkflowBSelectorResolutionError as exc:
        raise Stage3LoadPolicyNotConfiguredError(
            exc.category,
            str(exc),
            client_code=candidate.client_code,
            report_type=candidate.report_type,
        ) from exc
    if resolution.error_category:
        raise Stage3LoadPolicyNotConfiguredError(
            resolution.error_category,
            "Workflow B report load policy selector is incompatible with this report type",
            client_code=candidate.client_code,
            report_type=candidate.report_type,
        )


def _stage3_load_strategy(report_type: str) -> Stage3LoadStrategy | None:
    """Which writer will handle this report type.

    P0-E. Mirrors the branch `_load_dataframe_to_destination` actually takes, so
    the recovery probe reads the same provenance columns the loader wrote. An
    unrecognized report type returns None and is classified fail-closed rather
    than probed with guessed column names.
    """
    if _is_alpha_gps_report(report_type):
        return Stage3LoadStrategy.ALPHA_GPS_REPLACE_ALL
    try:
        _destination_for_report_type(report_type)
    except Exception:
        return None
    return Stage3LoadStrategy.TELEMATICS_TECHNICAL


def _try_stage3_file_lock(conn, raw_file_id: str) -> bool:
    """Claim exclusive ownership of one raw file for this session.

    P0-E. This, not a clock, is what proves no other process is working on the
    file. A PostgreSQL session-level advisory lock dies with its session, so a
    crashed loader releases its claim immediately while a legitimate one — which
    may run for hours under `TimeoutStartSec=6h`, or unbounded when Stage 3 is
    invoked standalone — keeps it for exactly as long as it is alive.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (stage3_file_advisory_lock_key(raw_file_id),))
        row = cur.fetchone()
    _rollback_quietly(conn)
    return bool(row.get("pg_try_advisory_lock") if isinstance(row, dict) else row[0])


def _release_stage3_file_lock(conn, raw_file_id: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(%s)", (stage3_file_advisory_lock_key(raw_file_id),)
            )
        _rollback_quietly(conn)
    except Exception:
        # The session is going away anyway, which releases the lock server-side.
        _rollback_quietly(conn)


def _probe_stage3_destination(
    platform_conn, candidate: Candidate
) -> tuple[DestinationEvidence, str | None, str | None]:
    """Read-only, attempt-scoped destination inspection for one candidate.

    Returns (evidence, destination_schema, destination_table). The probe uses a
    separate short-lived client connection: the decision of whether a load may
    happen must not be taken from inside the transaction that would perform it.
    """
    # `_mark_stage3_started` stamps the destination before any load, so a
    # crashed row names its own target. Fall back to the static route only when
    # it does not (an ERROR stamped before the destination was resolved).
    destination_schema = candidate.stage3_destination_schema
    destination_table = candidate.stage3_destination_table
    if not destination_schema or not destination_table:
        try:
            destination_schema, destination_table = _destination_for_report_type(
                candidate.report_type
            )
        except Exception:
            destination_schema, destination_table = None, None

    strategy = _stage3_load_strategy(candidate.report_type)
    try:
        with platform_conn.cursor() as cur:
            db_config = _load_client_account_by_code(cur, candidate.client_code)
        _rollback_quietly(platform_conn)
        with _client_business_pg_conn(db_config) as destination_conn:
            with destination_conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
            evidence = probe_destination_evidence(
                destination_conn,
                strategy=strategy,
                destination_schema=destination_schema or "",
                destination_table=destination_table or "",
                raw_file_id=candidate.raw_file_id,
                cleaned_artifact_id=candidate.stage2_cleaned_artifact_id,
            )
            destination_conn.rollback()
    except Exception as exc:
        # Unreadable, never "empty". The classifier refuses on unreadable.
        evidence = DestinationEvidence(
            None, None, strategy, f"{type(exc).__name__}: {exc}"
        )
    return evidence, destination_schema, destination_table


def _resolve_stage3_recovery(
    platform_conn,
    client,
    run_id: str,
    candidate: Candidate,
) -> LoadResult | None:
    """P0-E. Decide what a previously-attempted file's durable state now means.

    Returns None when the file must be processed by the ordinary path — either
    it was never attempted, or its interrupted attempt provably did not commit
    *this* generation and its writer is idempotent, so a full replay is correct.
    Returns a LoadResult when this attempt's load is durably present and must be
    reconciled instead of repeated. Raises `Stage3RecoveryNotOwnedError` when
    this cycle must leave the row untouched.

    The caller already holds this file's ownership lock, which is why liveness
    is not re-examined here.
    """
    status = str(candidate.stage3_status or "").strip()
    if not status or status.upper() == STATUS_OK:
        # Ordinary first attempt, or an 'OK' row Stage 2 superseded. No prior
        # attempt to reason about, and no reason to open a client connection.
        return None

    evidence, destination_schema, destination_table = _probe_stage3_destination(
        platform_conn, candidate
    )
    decision = classify_stage3_recovery(
        stage3_status=candidate.stage3_status,
        stage3_started_at=candidate.stage3_started_at,
        stage3_error=candidate.stage3_error,
        now=datetime.now(timezone.utc),
        evidence=evidence,
        file_lock_acquired=True,
    )
    context = {
        "raw_file_id": candidate.raw_file_id,
        "client_code": candidate.client_code,
        "report_type": candidate.report_type,
        "observed_stage3_status": candidate.stage3_status,
        "cleaned_artifact_id": candidate.stage2_cleaned_artifact_id,
        "destination_schema": destination_schema,
        "destination_table": destination_table,
        **decision.to_dict(),
    }
    client.log(
        "INFO",
        "SCRIPT",
        SOURCE,
        f"Workflow B Stage 3 recovery classified: {decision.recovery_class.value}",
        run_id=run_id,
        context=context,
    )

    if decision.recovery_class == Stage3RecoveryClass.SAFE_REPLAY:
        return None

    if decision.recovery_class == Stage3RecoveryClass.RECONCILE_COMMITTED:
        # This attempt's rows are committed in the client business database.
        # Re-running the load would re-download the artifact and re-issue writes
        # the destination already holds; what the crash actually skipped is only
        # the platform-side terminal status. Write exactly that, from the
        # destination's own attempt-scoped count.
        committed_rows = int((decision.evidence.rows_for_current_attempt or 0) if decision.evidence else 0)
        result = LoadResult(
            raw_file_id=candidate.raw_file_id,
            client_code=candidate.client_code,
            report_type=candidate.report_type,
            destination_schema=destination_schema or "",
            destination_table=destination_table or "",
            data_overwrite=False,
            input_rows=committed_rows,
            inserted_rows=0,
            updated_rows=0,
            skipped_rows=committed_rows,
            status="OK",
            source_filename=candidate.source_filename,
            source_artifact_id=candidate.stage2_cleaned_artifact_id,
            cleaned_artifact_id=candidate.stage2_cleaned_artifact_id,
            error=None,
            recovered_reconciled=True,
        )
        with platform_conn.cursor() as cur:
            _mark_stage3_finished(cur, result)
        platform_conn.commit()
        client.log(
            "INFO",
            "SCRIPT",
            SOURCE,
            "Workflow B Stage 3 reconciled an interrupted committed load",
            run_id=run_id,
            context=context,
        )
        return result

    raise Stage3RecoveryNotOwnedError(
        decision,
        raw_file_id=candidate.raw_file_id,
        client_code=candidate.client_code,
        report_type=candidate.report_type,
    )



def _process_candidate(
    platform_conn,
    client,
    run_id: str,
    candidate: Candidate,
    *,
    auto_grant_permissions: bool = False,
) -> LoadResult:
    if auto_grant_permissions:
        raise RuntimeSchemaMutationDisabledError("auto_grant_permissions")
    destination_schema: str | None = None
    destination_table: str | None = None
    data_overwrite = False
    artifact: ArtifactRef | None = None

    # P0-G. Before anything is marked, downloaded or written: prove that the
    # per-client configuration this file's Stage 3 handling *will* require
    # actually exists.
    #
    # Deliberately outside the try/except below, and deliberately ahead of
    # `_mark_stage3_started`. Both matter:
    #
    #   * outside the try — the handler calls `_mark_stage3_error`, which sets
    #     stage3_status='ERROR'. Since P0-E, ERROR is no longer a permanent
    #     orphan, but it is still strictly worse than leaving the row alone: an
    #     ERROR row costs a destination probe on every later cycle and reports
    #     as a recovery item rather than as the plain configuration block it is.
    #     A NULL row simply retries for free the moment the policy exists.
    #
    #   * ahead of `_mark_stage3_started` — that call clears stage3_finished_at
    #     and stamps 'RUNNING'. A file blocked on configuration never entered
    #     Stage 3 and must not be left looking like it did.
    #
    # This validates configuration only. It deliberately does not run the
    # postprocessor: that has its own transaction and external effects and
    # correctly belongs after the load commit. What moves earlier is the static
    # question the postprocessor step used to ask too late.
    _require_downstream_policy_configured(platform_conn, candidate)

    # P0-E. Claim exclusive ownership of this file before touching it. This is
    # the liveness authority the previous design tried to get from a wall-clock
    # threshold and could not: `log-workflow-b.service` allows a 6-hour run,
    # `log-job@.service` 4 hours, and a standalone Stage 3 invocation is
    # unbounded and takes no orchestration lock, so no timeout can prove that
    # nobody else is still working. A session-level advisory lock can: it dies
    # with a crashed process and survives for the whole life of a healthy one.
    #
    # Taken for every candidate, not only recovery ones, so two concurrent
    # Stage 3 invocations cannot both load the same file.
    if not _try_stage3_file_lock(platform_conn, candidate.raw_file_id):
        raise Stage3RecoveryNotOwnedError(
            Stage3RecoveryDecision(
                Stage3RecoveryClass.AWAIT_LIVE_OWNER,
                "another process holds this file's Stage 3 ownership lock",
            ),
            raw_file_id=candidate.raw_file_id,
            client_code=candidate.client_code,
            report_type=candidate.report_type,
        )

    try:
        # Only now — after P0-G has passed, so a policy-blocked candidate still
        # never opens a client business connection, and after ownership is
        # claimed — may the durable state of a previous attempt be resolved
        # against the destination.
        recovered = _resolve_stage3_recovery(platform_conn, client, run_id, candidate)
        if recovered is not None:
            return recovered
        return _load_candidate(
            platform_conn, client, run_id, candidate,
            destination_schema=destination_schema,
            destination_table=destination_table,
            data_overwrite=data_overwrite,
            artifact=artifact,
        )
    finally:
        _release_stage3_file_lock(platform_conn, candidate.raw_file_id)


def _load_candidate(
    platform_conn,
    client,
    run_id: str,
    candidate: Candidate,
    *,
    destination_schema: str | None,
    destination_table: str | None,
    data_overwrite: bool,
    artifact: "ArtifactRef | None",
) -> LoadResult:
    """The ordinary Stage 3 load. Split out of `_process_candidate` so the
    ownership lock has a single, obvious `finally`."""
    try:
        destination_schema, destination_table = _destination_for_report_type(candidate.report_type)
        with platform_conn.cursor() as cur:
            _mark_stage3_started(
                cur,
                candidate.raw_file_id,
                destination_schema=destination_schema,
                destination_table=destination_table,
            )
        platform_conn.commit()

        with platform_conn.cursor() as cur:
            artifact = _find_stage2_cleaned_artifact(
                cur,
                raw_file_id=candidate.raw_file_id,
                report_type=candidate.report_type,
            )
            data_overwrite, policy_found = _load_data_overwrite_policy(
                cur,
                client_code=candidate.client_code,
                report_type=candidate.report_type,
            )
            db_config = _load_client_account_by_code(cur, candidate.client_code)
            lineage = _stage1_artifact_lineage(cur, raw_file_id=candidate.raw_file_id)
        if not policy_found:
            client.log(
                "INFO",
                "SCRIPT",
                SOURCE,
                "Workflow B Stage 3 using default data_overwrite=false policy",
                run_id=run_id,
                context={
                    "raw_file_id": candidate.raw_file_id,
                    "client_code": candidate.client_code,
                    "report_type": candidate.report_type,
                },
            )

        artifact_filename = (
            artifact.display_filename
            or artifact.original_filename
            or artifact.filename
            or f"{artifact.artifact_id}.csv"
        )
        with tempfile.TemporaryDirectory(prefix="workflow-b-stage3-") as tmpdir:
            input_path = Path(tmpdir) / _safe_download_filename(artifact_filename)
            client.download_artifact(artifact.artifact_id, str(input_path))
            _require_nonempty_artifact_file(input_path, artifact.artifact_id)
            df = _read_cleaned_csv(input_path)

            with _client_business_pg_conn(db_config) as destination_conn:
                result = _load_dataframe_to_destination(
                    destination_conn,
                    raw_file_id=candidate.raw_file_id,
                    run_id=run_id,
                    client_code=candidate.client_code,
                    report_type=candidate.report_type,
                    source_artifact_id=artifact.artifact_id,
                    source_filename=artifact_filename,
                    data_overwrite=data_overwrite,
                    df=df,
                    source_sha256=candidate.source_sha256,
                    raw_artifact_id=lineage.get("raw_artifact_id"),
                    normalized_artifact_id=lineage.get("normalized_artifact_id"),
                    cleaned_artifact_id=artifact.artifact_id,
                )
                result.destination_database = db_config.client_db_name

        try:
            _upload_stage3_artifacts(client, run_id, artifact, result)
        except Exception:
            client.log(
                "WARNING",
                "SCRIPT",
                SOURCE,
                "Workflow B Stage 3 artifact upload failed after destination load",
                run_id=run_id,
                context=_result_context(result),
                error=traceback.format_exc(),
            )
        with platform_conn.cursor() as cur:
            _mark_stage3_finished(cur, result)
        platform_conn.commit()
        return result
    except Exception as exc:
        _rollback_quietly(platform_conn)
        # P0-E. The same classifier the batch item uses, so the durable marker
        # and the reported outcome cannot disagree about retryability.
        _, error_category, retryable, _operator = classify_stage3_exception(
            exc, has_candidate=True
        )
        try:
            with platform_conn.cursor() as cur:
                _mark_stage3_error(
                    cur,
                    raw_file_id=candidate.raw_file_id,
                    error_message=str(exc),
                    destination_schema=destination_schema or "",
                    destination_table=destination_table or "",
                    data_overwrite=data_overwrite,
                    error_category=error_category,
                    retryable=retryable,
                )
            platform_conn.commit()
        except Exception:
            _rollback_quietly(platform_conn)
        raise


def _platform_pg_conn():
    import psycopg
    from psycopg.rows import dict_row

    dsn = (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn, row_factory=dict_row))


def _client_business_pg_conn(config: ClientDbConfig):
    import psycopg
    from psycopg.rows import dict_row

    password = resolve_secret(config.client_db_password_secret_ref)
    return set_pg_session_timezone(psycopg.connect(
        host=config.client_db_host,
        port=config.client_db_port,
        dbname=config.client_db_name,
        user=config.client_db_user,
        password=password,
        sslmode=config.client_db_sslmode,
        row_factory=dict_row,
    ))


def _rollback_quietly(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _select_stage3_candidates(
    platform_conn,
    *,
    raw_file_id: str | None = None,
    limit: int | None = None,
    force_reprocess: bool = False,
    stale_grace_minutes: int = DEFAULT_STAGE3_STALE_GRACE_MINUTES,
) -> list[Candidate]:
    where = [
        "rf.stage2_status = 'OK'",
        "rf.client_code IS NOT NULL",
        "btrim(rf.client_code) <> ''",
        "rf.stage2_report_type IS NOT NULL",
        "btrim(rf.stage2_report_type) <> ''",
    ]
    params: list[Any] = []

    if raw_file_id:
        where.append("rf.id = %s")
        params.append(raw_file_id)
        if not force_reprocess:
            where.append(_stage3_batch_status_eligible_sql("rf"))
            params.append(stale_grace_minutes)
    else:
        where.append(_stage3_batch_status_eligible_sql("rf"))
        params.append(stale_grace_minutes)
        where.append(_stage2_cleaned_artifact_exists_sql("rf"))
        params.extend([WORKFLOW_NAME, STAGE2_CLEAN_STAGE])

    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT %s"
        params.append(limit)

    query = f"""
        SELECT
            rf.id::text AS raw_file_id,
            btrim(rf.client_code) AS client_code,
            btrim(rf.stage2_report_type) AS report_type,
            COALESCE(rf.original_filename, rf.normalized_csv_path, rf.raw_path) AS source_filename,
            rf.sha256 AS source_sha256,
            rf.stage3_status,
            rf.stage3_started_at,
            rf.stage3_destination_schema,
            rf.stage3_destination_table,
            rf.stage2_cleaned_artifact_id::text AS stage2_cleaned_artifact_id,
            rf.stage3_error
        FROM ingest.raw_file rf
        WHERE {' AND '.join(where)}
        ORDER BY rf.stage2_updated_at ASC NULLS LAST, rf.id ASC
        {limit_sql}
    """

    with platform_conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [
        Candidate(
            raw_file_id=str(row["raw_file_id"]),
            client_code=str(row["client_code"]).strip(),
            report_type=str(row["report_type"]).strip(),
            source_filename=row.get("source_filename"),
            source_sha256=row.get("source_sha256"),
            stage3_status=row.get("stage3_status"),
            stage3_started_at=row.get("stage3_started_at"),
            stage3_destination_schema=row.get("stage3_destination_schema"),
            stage3_destination_table=row.get("stage3_destination_table"),
            stage2_cleaned_artifact_id=row.get("stage2_cleaned_artifact_id"),
            stage3_error=row.get("stage3_error"),
        )
        for row in rows
    ]


def _stage3_batch_status_eligible_sql(raw_file_alias: str) -> str:
    """States autonomous discovery owns.

    P0-E. The first three branches are the original contract: never attempted,
    or a completed load that Stage 2 has since superseded. The last two are the
    fix. 'RUNNING' past its grace and 'ERROR' were previously terminal *by
    omission* — nothing selected them again, so a process killed just after
    `_mark_stage3_started` committed removed the file from the autonomous
    system permanently.

    Admitting them here is discovery only, never a decision to load. Every
    admitted row goes through `classify_stage3_recovery`, which consults the
    destination before anything is replayed; see `jobs/reports/stage3/recovery.py`.

    The grace is a bound parameter rather than a literal so the file-level and
    run-level staleness contracts stay on one number.
    """
    return f"""(
            {raw_file_alias}.stage3_status IS NULL
            OR btrim({raw_file_alias}.stage3_status) = ''
            OR (
                btrim({raw_file_alias}.stage3_status) = 'OK'
                AND {raw_file_alias}.stage2_updated_at IS NOT NULL
                AND {raw_file_alias}.stage3_finished_at IS NOT NULL
                AND {raw_file_alias}.stage2_updated_at > {raw_file_alias}.stage3_finished_at
            )
            OR (
                btrim({raw_file_alias}.stage3_status) = 'RUNNING'
                AND (
                    {raw_file_alias}.stage3_started_at IS NULL
                    OR {raw_file_alias}.stage3_started_at
                       < now() - make_interval(mins => %s)
                )
            )
            OR btrim({raw_file_alias}.stage3_status) = 'ERROR'
        )"""


def _stage2_cleaned_artifact_exists_sql(raw_file_alias: str) -> str:
    return f"""EXISTS (
            SELECT 1
            FROM artifacts a
            WHERE a.raw_file_id = {raw_file_alias}.id
              AND a.workflow_name = %s
              AND a.stage_name = %s
              AND a.artifact_role = 'cleaned'
              AND a.report_type IN (
                  btrim({raw_file_alias}.stage2_report_type),
                  {_artifact_report_type_sql_expr(f'{raw_file_alias}.stage2_report_type')}
              )
        )"""


def _artifact_report_type_sql_expr(report_type_sql: str) -> str:
    return (
        "btrim(regexp_replace("
        f"regexp_replace(lower(btrim(coalesce({report_type_sql}, ''))), "
        "'[^a-z0-9_.-]+', '_', 'g'), "
        "'_+', '_', 'g'), '._-')"
    )


def _stage3_no_pending_diagnostics(platform_conn, *, raw_file_id: str | None = None) -> dict[str, Any]:
    with platform_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COALESCE(NULLIF(btrim(stage2_report_type), ''), '<EMPTY>') AS report_type,
                COALESCE(NULLIF(btrim(client_code), ''), '<EMPTY>') AS client_code,
                COALESCE(NULLIF(btrim(stage3_status), ''), '<NULL>') AS stage3_status,
                COUNT(*)::int AS count,
                MAX(stage2_updated_at) AS latest_stage2_updated_at,
                MAX(stage3_finished_at) AS latest_stage3_finished_at
            FROM ingest.raw_file
            WHERE stage2_status = 'OK'
            GROUP BY
                COALESCE(NULLIF(btrim(stage2_report_type), ''), '<EMPTY>'),
                COALESCE(NULLIF(btrim(client_code), ''), '<EMPTY>'),
                COALESCE(NULLIF(btrim(stage3_status), ''), '<NULL>')
            ORDER BY report_type, client_code, stage3_status
            """
        )
        counts = [
            {
                "report_type": row["report_type"],
                "client_code": row["client_code"],
                "stage3_status": row["stage3_status"],
                "count": int(row["count"]),
                "latest_stage2_updated_at": _iso_or_none(row.get("latest_stage2_updated_at")),
                "latest_stage3_finished_at": _iso_or_none(row.get("latest_stage3_finished_at")),
            }
            for row in cur.fetchall()
        ]

        latest_where = ["rf.stage2_status = 'OK'"]
        latest_params: list[Any] = [WORKFLOW_NAME, STAGE2_CLEAN_STAGE]
        if raw_file_id:
            latest_where.append("rf.id = %s")
            latest_params.append(raw_file_id)
        cur.execute(
            f"""
            SELECT
                rf.id::text AS raw_file_id,
                rf.original_filename,
                rf.status AS raw_file_status,
                rf.duplicate_of_id::text AS duplicate_of_id,
                rf.stage2_report_type,
                rf.client_code,
                rf.stage2_updated_at,
                rf.stage3_status,
                rf.stage3_started_at,
                rf.stage3_finished_at,
                rf.stage3_error,
                {_stage2_cleaned_artifact_exists_sql("rf")} AS has_cleaned_artifact
            FROM ingest.raw_file rf
            WHERE {' AND '.join(latest_where)}
            ORDER BY rf.stage2_updated_at DESC NULLS LAST, rf.id DESC
            LIMIT 10
            """,
            latest_params,
        )
        latest_rows = [_stage3_diagnostic_row(row) for row in cur.fetchall()]

    reason_counts: dict[str, int] = {}
    for row in latest_rows:
        for reason in row["exclusion_reasons"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    return {
        "stage2_ok_counts_by_report_type_client_stage3_status": counts,
        "latest_stage2_ok_rows": latest_rows,
        "exclusion_reason_counts": reason_counts,
    }


def _stage3_diagnostic_row(row: dict[str, Any]) -> dict[str, Any]:
    stage3_status = row.get("stage3_status")
    stage2_updated_at = row.get("stage2_updated_at")
    stage3_finished_at = row.get("stage3_finished_at")
    reasons = _stage3_candidate_exclusion_reasons(
        client_code=row.get("client_code"),
        report_type=row.get("stage2_report_type"),
        stage3_status=stage3_status,
        stage2_updated_at=stage2_updated_at,
        stage3_finished_at=stage3_finished_at,
        has_cleaned_artifact=bool(row.get("has_cleaned_artifact")),
    )
    return {
        "raw_file_id": row.get("raw_file_id"),
        "original_filename": row.get("original_filename"),
        "raw_file_status": row.get("raw_file_status"),
        "duplicate_of_id": row.get("duplicate_of_id"),
        "report_type": row.get("stage2_report_type"),
        "client_code": row.get("client_code"),
        "stage2_updated_at": _iso_or_none(stage2_updated_at),
        "stage3_status": stage3_status,
        "stage3_started_at": _iso_or_none(row.get("stage3_started_at")),
        "stage3_finished_at": _iso_or_none(stage3_finished_at),
        "stage3_error": row.get("stage3_error"),
        "has_cleaned_artifact": bool(row.get("has_cleaned_artifact")),
        "exclusion_reasons": reasons,
    }


def _stage3_candidate_exclusion_reasons(
    *,
    client_code: Any,
    report_type: Any,
    stage3_status: Any,
    stage2_updated_at: Any,
    stage3_finished_at: Any,
    has_cleaned_artifact: bool,
) -> list[str]:
    reasons: list[str] = []
    if not str(client_code or "").strip():
        reasons.append("missing_client_code")
    if not str(report_type or "").strip():
        reasons.append("missing_stage2_report_type")
    if not has_cleaned_artifact:
        reasons.append("missing_stage2_cleaned_artifact")
    status = str(stage3_status or "").strip()
    if status == "OK":
        if not _stage2_output_is_newer_than_stage3(stage2_updated_at, stage3_finished_at):
            reasons.append("stage3_status_ok")
    elif status == "ERROR":
        reasons.append("stage3_status_error")
    elif status == "RUNNING":
        reasons.append("stage3_status_running")
    elif status == "SKIPPED_NO_RECORD_ID":
        reasons.append("stage3_status_skipped_no_record_id")
    elif status:
        reasons.append("stage3_status_not_eligible")
    return reasons


def _stage2_output_is_newer_than_stage3(stage2_updated_at: Any, stage3_finished_at: Any) -> bool:
    return (
        stage2_updated_at is not None
        and stage3_finished_at is not None
        and stage2_updated_at > stage3_finished_at
    )


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _find_stage2_cleaned_artifact(cur, *, raw_file_id: str, report_type: str) -> ArtifactRef:
    report_type_candidates = _artifact_report_type_candidates(report_type)
    cur.execute(
        """
        SELECT
            artifact_id::text AS artifact_id,
            filename,
            original_filename,
            display_filename
        FROM artifacts
        WHERE raw_file_id = %s
          AND workflow_name = %s
          AND stage_name = %s
          AND artifact_role = 'cleaned'
          AND report_type = ANY(%s::text[])
        ORDER BY created_at DESC, artifact_id DESC
        LIMIT 1
        """,
        (raw_file_id, WORKFLOW_NAME, STAGE2_CLEAN_STAGE, report_type_candidates),
    )
    row = cur.fetchone()
    if not row:
        raise Stage3WriterValidationError(
            _missing_stage2_cleaned_artifact_message(cur, raw_file_id=raw_file_id, report_type=report_type),
            signal="missing_stage2_cleaned_artifact",
        )
    return ArtifactRef(
        artifact_id=str(row["artifact_id"]),
        filename=row.get("filename"),
        original_filename=row.get("original_filename"),
        display_filename=row.get("display_filename"),
    )


def _artifact_report_type_candidates(report_type: str) -> list[str]:
    candidates: list[str] = []
    for value in (report_type, _sanitize_artifact_component_like_api(report_type)):
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _sanitize_artifact_component_like_api(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "_", str(value or "").strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("._-")
    return normalized or "unknown"


def _stage2_cleaned_artifact_lookup_filters(*, raw_file_id: str, report_type: str) -> dict[str, Any]:
    return {
        "raw_file_id": raw_file_id,
        "workflow_name": WORKFLOW_NAME,
        "stage_name": STAGE2_CLEAN_STAGE,
        "artifact_role": "cleaned",
        "report_type_any": _artifact_report_type_candidates(report_type),
    }


def _artifact_group_summary_for_raw_file(cur, *, raw_file_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            COALESCE(stage_name, '<NULL>') AS stage_name,
            COALESCE(artifact_role, '<NULL>') AS artifact_role,
            COALESCE(kind, '<NULL>') AS artifact_kind,
            COALESCE(report_type, '<NULL>') AS report_type,
            COALESCE(client_code, '<NULL>') AS client_code,
            COUNT(*)::int AS count,
            MAX(created_at) AS latest_created_at
        FROM artifacts
        WHERE raw_file_id = %s
        GROUP BY stage_name, artifact_role, kind, report_type, client_code
        ORDER BY latest_created_at DESC NULLS LAST, stage_name, artifact_role, kind, report_type, client_code
        """,
        (raw_file_id,),
    )
    return [
        {
            "stage_name": row["stage_name"],
            "artifact_role": row["artifact_role"],
            "artifact_kind": row["artifact_kind"],
            "report_type": row["report_type"],
            "client_code": row["client_code"],
            "count": int(row["count"]),
            "latest_created_at": row["latest_created_at"].isoformat()
            if hasattr(row.get("latest_created_at"), "isoformat")
            else row.get("latest_created_at"),
        }
        for row in cur.fetchall()
    ]


def _missing_stage2_cleaned_artifact_message(cur, *, raw_file_id: str, report_type: str) -> str:
    filters = _stage2_cleaned_artifact_lookup_filters(raw_file_id=raw_file_id, report_type=report_type)
    summary = _artifact_group_summary_for_raw_file(cur, raw_file_id=raw_file_id)
    hint = "no artifact rows exist for this raw_file_id"
    if summary:
        expected_report_types = set(filters["report_type_any"])
        same_stage = [row for row in summary if row["stage_name"] == STAGE2_CLEAN_STAGE]
        same_stage_cleaned = [row for row in same_stage if row["artifact_role"] == "cleaned"]
        same_report = [row for row in same_stage_cleaned if row["report_type"] in expected_report_types]
        if not same_stage:
            hint = "artifact rows exist for raw_file_id, but none have stage_name=stage_2_clean"
        elif not same_stage_cleaned:
            hint = "Stage 2 artifact rows exist, but none have artifact_role=cleaned"
        elif not same_report:
            observed = sorted({row["report_type"] for row in same_stage_cleaned})
            hint = "Stage 2 cleaned artifact rows exist, but report_type does not match lookup candidates: " + ", ".join(observed)
        else:
            hint = "Stage 2 appears to have a cleaned artifact, but another lookup condition did not match"
    return (
        "Stage 2 cleaned artifact not found "
        f"for raw_file_id={raw_file_id}, report_type={report_type}; "
        f"artifact_lookup_filters={json.dumps(filters, sort_keys=True)}; "
        f"artifact_rows_for_raw_file={json.dumps(summary, sort_keys=True)}; "
        f"hint={hint}"
    )


def _stage1_artifact_lineage(cur, *, raw_file_id: str) -> dict[str, str | None]:
    lineage: dict[str, str | None] = {
        "raw_artifact_id": None,
        "normalized_artifact_id": None,
    }
    cur.execute(
        """
        SELECT artifact_role, artifact_id::text AS artifact_id
        FROM artifacts
        WHERE raw_file_id = %s
          AND workflow_name = %s
          AND stage_name = %s
          AND artifact_role IN ('raw', 'normalized')
        ORDER BY created_at DESC, artifact_id DESC
        """,
        (raw_file_id, WORKFLOW_NAME, STAGE1_FETCH_STAGE),
    )
    for row in cur.fetchall():
        role = str(row["artifact_role"])
        key = f"{role}_artifact_id"
        if key in lineage and not lineage[key]:
            lineage[key] = str(row["artifact_id"])
    return lineage


def _load_data_overwrite_policy(cur, *, client_code: str, report_type: str) -> tuple[bool, bool]:
    if _is_alpha_gps_report(report_type):
        return True, True
    cur.execute(
        """
        SELECT data_overwrite
        FROM workflow_b_control.report_type_client_load_policy
        WHERE client_code = %s
          AND report_type = %s
        """,
        (client_code, report_type),
    )
    row = cur.fetchone()
    if not row:
        return False, False
    return bool(row["data_overwrite"]), True


def _is_alpha_gps_report(report_type: str) -> bool:
    return (report_type or "").strip() == ALPHA_GPS_REPORT_TYPE


def _destination_for_report_type(report_type: str) -> tuple[str, str]:
    if _is_alpha_gps_report(report_type):
        return ALPHA_GPS_TARGET_SCHEMA, ALPHA_GPS_TARGET_TABLE
    return DESTINATION_SCHEMA, _safe_table_name(report_type)


def _load_client_account_by_code(cur, client_code: str) -> ClientDbConfig:
    cur.execute(
        """
        SELECT
            client_id,
            client_code,
            client_db_host,
            client_db_port,
            client_db_name,
            client_db_user,
            client_db_password_secret_ref,
            client_db_environment,
            client_db_identity_id
        FROM workflow_a_control.client_account
        WHERE enabled IS TRUE
          AND client_code = %s
        """,
        (client_code,),
    )
    row = cur.fetchone()
    if not row:
        raise Stage3WriterValidationError(
            f"No enabled client database account found for client_code={client_code}",
            signal="client_account_not_configured",
        )
    required = [
        "client_db_host",
        "client_db_name",
        "client_db_user",
        "client_db_password_secret_ref",
    ]
    missing = [name for name in required if not row.get(name)]
    if missing:
        raise Stage3WriterValidationError(
            f"Client database account for client_code={client_code} is missing: {', '.join(missing)}",
            signal="client_account_incomplete",
        )
    return ClientDbConfig(
        client_code=str(row["client_code"]),
        client_id=str(row["client_id"]) if row.get("client_id") is not None else None,
        client_db_host=str(row["client_db_host"]),
        client_db_port=int(row.get("client_db_port") or 5432),
        client_db_name=str(row["client_db_name"]),
        client_db_user=str(row["client_db_user"]),
        client_db_password_secret_ref=str(row["client_db_password_secret_ref"]),
        client_db_sslmode="prefer",
        client_db_environment=(
            str(row["client_db_environment"])
            if row.get("client_db_environment") is not None
            else None
        ),
        client_db_identity_id=(
            str(row["client_db_identity_id"])
            if row.get("client_db_identity_id") is not None
            else None
        ),
    )


def _read_cleaned_csv(path: Path):
    import pandas as pd

    df = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.columns = [str(col) for col in df.columns]
    if "record_id" not in df.columns:
        df["record_id"] = ""
    return df


def _load_dataframe_to_destination(
    destination_conn,
    *,
    raw_file_id: str,
    run_id: str,
    client_code: str,
    report_type: str,
    source_artifact_id: str,
    source_filename: str | None,
    data_overwrite: bool,
    df,
    source_sha256: str | None = None,
    raw_artifact_id: str | None = None,
    normalized_artifact_id: str | None = None,
    cleaned_artifact_id: str | None = None,
) -> LoadResult:
    destination_schema, destination_table = _destination_for_report_type(report_type)
    if _is_alpha_gps_report(report_type):
        return _load_alpha_gps_replace_all(
            destination_conn,
            raw_file_id=raw_file_id,
            run_id=run_id,
            client_code=client_code,
            report_type=report_type,
            source_artifact_id=source_artifact_id,
            source_filename=source_filename,
            df=df,
            source_sha256=source_sha256,
            raw_artifact_id=raw_artifact_id,
            normalized_artifact_id=normalized_artifact_id,
            cleaned_artifact_id=cleaned_artifact_id,
            destination_schema=destination_schema,
            destination_table=destination_table,
        )

    _ensure_record_id_column(df)

    try:
        with destination_conn.cursor() as cur:
            plan = _build_load_plan(
                cur,
                df=df,
                data_overwrite=data_overwrite,
                destination_schema=DESTINATION_SCHEMA,
                destination_table=destination_table,
            )
            if plan["errors"]:
                # Accumulated deterministic plan refusals: invalid columns,
                # and the duplicate existing record_id refusal that blocks the
                # unique index. The same cleaned artifact reproduces both.
                raise Stage3WriterValidationError(
                    "; ".join(plan["errors"]), signal="load_plan_rejected"
                )

            report_columns = plan["report_columns"]
            input_rows = plan["input_rows"]
            _require_destination_schema_ready(
                plan,
                schema_name=DESTINATION_SCHEMA,
                table_name=destination_table,
            )
            if not plan["usable_record_id"] and not data_overwrite:
                destination_conn.rollback()
                return LoadResult(
                    raw_file_id=raw_file_id,
                    client_code=client_code,
                    report_type=report_type,
                    destination_schema=DESTINATION_SCHEMA,
                    destination_table=destination_table,
                    data_overwrite=data_overwrite,
                    input_rows=input_rows,
                    inserted_rows=0,
                    updated_rows=0,
                    skipped_rows=input_rows,
                    status="SKIPPED_NO_RECORD_ID",
                    source_artifact_id=source_artifact_id,
                    source_filename=source_filename,
                    error=(
                        "record_id is missing or empty and data_overwrite=false, "
                        "so Stage 3 did not load rows to avoid duplicates."
                    ),
                )

            if plan["usable_record_id"]:
                if data_overwrite:
                    inserted_rows, updated_rows = _upsert_record_id_rows(
                        cur,
                        DESTINATION_SCHEMA,
                        destination_table,
                        report_columns,
                        plan["loadable_rows"],
                        plan["existing_record_ids"],
                        raw_file_id=raw_file_id,
                        run_id=run_id,
                        source_artifact_id=source_artifact_id,
                        source_filename=source_filename,
                    )
                    skipped_rows = plan["would_skip_rows"]
                else:
                    inserted_rows = _insert_rows(
                        cur,
                        DESTINATION_SCHEMA,
                        destination_table,
                        report_columns,
                        plan["rows_to_insert"],
                        raw_file_id=raw_file_id,
                        run_id=run_id,
                        source_artifact_id=source_artifact_id,
                        source_filename=source_filename,
                        conflict_action="nothing",
                    )
                    updated_rows = 0
                    skipped_rows = plan["would_skip_rows"]
            else:
                _replace_table_rows(
                    cur,
                    DESTINATION_SCHEMA,
                    destination_table,
                    report_columns,
                    plan["all_rows"],
                    raw_file_id=raw_file_id,
                    run_id=run_id,
                    source_artifact_id=source_artifact_id,
                    source_filename=source_filename,
                )
                inserted_rows = input_rows
                updated_rows = 0
                skipped_rows = 0

        destination_conn.commit()
    except Exception:
        destination_conn.rollback()
        raise
    return LoadResult(
        raw_file_id=raw_file_id,
        client_code=client_code,
        report_type=report_type,
        destination_schema=DESTINATION_SCHEMA,
        destination_table=destination_table,
        data_overwrite=data_overwrite,
        input_rows=input_rows,
        inserted_rows=inserted_rows,
        updated_rows=updated_rows,
        skipped_rows=skipped_rows,
        status="OK",
        source_artifact_id=source_artifact_id,
        source_filename=source_filename,
        rejected_rows=plan["rejected_rows"],
    )


def _build_load_plan(
    cur,
    *,
    df,
    data_overwrite: bool,
    destination_schema: str,
    destination_table: str,
) -> dict[str, Any]:
    original_has_record_id = "record_id" in list(df.columns)
    _ensure_record_id_column(df)
    report_columns = _report_columns(df)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        _validate_report_columns(report_columns)
    except RuntimeError as exc:
        errors.append(str(exc))

    input_rows = _row_count(df)
    usable_record_id = _has_usable_record_id(df)
    inspection = _inspect_destination(cur, destination_schema, destination_table)
    columns_to_create: list[str] = []
    columns_to_add: list[str] = []

    if inspection["destination_table_exists"]:
        existing_columns = inspection["existing_columns"]
        for column in report_columns:
            current_type = existing_columns.get(column)
            if current_type is None:
                columns_to_add.append(column)
            elif not _is_text_type(current_type):
                errors.append(
                    f"Destination column {destination_schema}.{destination_table}.{column} "
                    f"has incompatible type {current_type}"
                )
        for column in TECHNICAL_COLUMNS:
            if column not in existing_columns:
                columns_to_add.append(column)
    else:
        columns_to_create = report_columns + list(TECHNICAL_COLUMNS)

    prepared = _prepared_record_id_rows(df, report_columns)
    non_empty_record_ids = [row["record_id"] for row in prepared["loadable_rows"]]
    empty_record_id_rows = len(
        [row for row in (prepared["rejected_rows"] or []) if row.get("_stage3_skip_reason") == "empty_record_id"]
    )
    duplicate_record_id_rows = len(
        [
            row
            for row in (prepared["rejected_rows"] or [])
            if row.get("_stage3_skip_reason") == "duplicate_record_id_in_input"
        ]
    )
    duplicate_existing_record_ids: list[dict[str, Any]] = []
    existing_record_ids: set[str] = set()
    if inspection["destination_table_exists"] and "record_id" in inspection["existing_columns"]:
        duplicate_existing_record_ids = _find_existing_duplicate_record_ids(
            cur,
            destination_schema,
            destination_table,
        )
        if duplicate_existing_record_ids:
            sample = ", ".join(
                f"{row['record_id']} ({row['count']})" for row in duplicate_existing_record_ids
            )
            errors.append(
                "Cannot create record_id unique index because destination table already "
                f"contains duplicate non-empty record_id values: {sample}"
            )
        existing_record_ids = _fetch_existing_record_ids(
            cur,
            destination_schema,
            destination_table,
            non_empty_record_ids,
        )

    rows_to_insert = [
        row for row in prepared["loadable_rows"] if row["record_id"] not in existing_record_ids
    ]
    existing_skipped_rows = [
        _rejected_row(row["row"], "record_id_already_loaded")
        for row in prepared["loadable_rows"]
        if row["record_id"] in existing_record_ids
    ]

    if not usable_record_id and not data_overwrite:
        warnings.append(
            "record_id is missing or empty and data_overwrite=false, so Stage 3 would not load rows."
        )
        would_insert_rows = 0
        would_update_rows = 0
        would_skip_rows = input_rows
        would_reject_rows = 0
        would_replace_table = False
    elif not usable_record_id and data_overwrite:
        would_insert_rows = input_rows
        would_update_rows = 0
        would_skip_rows = 0
        would_reject_rows = 0
        would_replace_table = True
    elif data_overwrite:
        unique_ids = {row["record_id"] for row in prepared["loadable_rows"]}
        would_insert_rows = len(unique_ids - existing_record_ids)
        would_update_rows = len(unique_ids & existing_record_ids)
        would_reject_rows = empty_record_id_rows + duplicate_record_id_rows
        would_skip_rows = would_reject_rows
        would_replace_table = False
    else:
        would_insert_rows = len(rows_to_insert)
        would_update_rows = 0
        would_reject_rows = empty_record_id_rows + duplicate_record_id_rows
        would_skip_rows = len(existing_skipped_rows) + would_reject_rows
        would_replace_table = False

    return {
        **inspection,
        "report_columns": report_columns,
        "input_rows": input_rows,
        "original_has_record_id": original_has_record_id,
        "usable_record_id": usable_record_id,
        "non_empty_record_id_rows": len(non_empty_record_ids),
        "empty_record_id_rows": empty_record_id_rows,
        "duplicate_record_id_rows_in_input": duplicate_record_id_rows,
        "columns_to_create": columns_to_create,
        "columns_to_add": columns_to_add,
        "duplicate_existing_record_ids": duplicate_existing_record_ids,
        "duplicate_existing_record_ids_detected": bool(duplicate_existing_record_ids),
        "existing_record_ids": existing_record_ids,
        "loadable_rows": prepared["loadable_rows"],
        "rows_to_insert": rows_to_insert,
        "rejected_rows": prepared["rejected_rows"] + existing_skipped_rows,
        "all_rows": _all_rows(df, report_columns),
        "would_insert_rows": would_insert_rows,
        "would_update_rows": would_update_rows,
        "would_skip_rows": would_skip_rows,
        "would_reject_rows": would_reject_rows,
        "would_replace_table": would_replace_table,
        "warnings": warnings,
        "errors": errors,
    }


def _build_alpha_gps_replace_all_plan(
    cur,
    *,
    df,
    destination_schema: str,
    destination_table: str,
) -> dict[str, Any]:
    inspection = _inspect_destination(cur, destination_schema, destination_table)
    rows, errors = _alpha_gps_rows(df)
    if not inspection["destination_table_exists"]:
        errors.append(
            f"Destination table {destination_schema}.\"{destination_table}\" is missing; "
            "apply db/client_business/023_alpha_gps_baza_log_workflow_b.sql first"
        )
    return {
        **inspection,
        "report_columns": list(ALPHA_GPS_REQUIRED_COLUMNS),
        "input_rows": _row_count(df),
        "original_has_record_id": "record_id" in list(df.columns),
        "usable_record_id": False,
        "non_empty_record_id_rows": 0,
        "empty_record_id_rows": 0,
        "duplicate_record_id_rows_in_input": 0,
        "columns_to_create": [] if inspection["destination_table_exists"] else list(ALPHA_GPS_REQUIRED_COLUMNS),
        "columns_to_add": [],
        "duplicate_existing_record_ids": [],
        "duplicate_existing_record_ids_detected": False,
        "existing_record_ids": set(),
        "loadable_rows": [],
        "rows_to_insert": [],
        "rejected_rows": [],
        "all_rows": rows,
        "would_insert_rows": len(rows) if not errors else 0,
        "would_update_rows": 0,
        "would_skip_rows": 0,
        "would_reject_rows": 0,
        "would_replace_table": not errors,
        "warnings": [],
        "errors": errors,
    }


def _inspect_destination(cur, schema_name: str, table_name: str) -> dict[str, Any]:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = %s) AS exists",
        (schema_name,),
    )
    schema_row = cur.fetchone()
    schema_exists = bool(schema_row and schema_row["exists"])

    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name = %s
        ) AS exists
        """,
        (schema_name, table_name),
    )
    table_row = cur.fetchone()
    table_exists = bool(table_row and table_row["exists"])
    existing_columns = _existing_columns(cur, schema_name, table_name) if table_exists else {}
    unique_index_exists = _record_id_unique_index_exists(cur, schema_name, table_name) if table_exists else False
    return {
        "destination_schema_exists": schema_exists,
        "destination_table_exists": table_exists,
        "existing_columns": existing_columns,
        "unique_index_exists": unique_index_exists,
    }


def _record_id_unique_index_exists(cur, schema_name: str, table_name: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = %s
              AND tablename = %s
              AND indexname = %s
              AND indexdef ILIKE 'CREATE UNIQUE INDEX%%'
              AND indexdef ILIKE '%%record_id%%WHERE%%NULLIF%%BTRIM%%record_id%%IS NOT NULL%%'
        ) AS exists
        """,
        (schema_name, table_name, _safe_index_name(f"{table_name}__record_id_uidx")),
    )
    row = cur.fetchone()
    return bool(row and row["exists"])


def _dry_run_summary(
    *,
    candidate: Candidate,
    artifact: ArtifactRef,
    source_filename: str | None,
    data_overwrite: bool,
    policy_found: bool,
    destination_schema: str,
    destination_table: str,
    destination_database: str | None,
    plan: dict[str, Any],
    would_auto_grant_permissions: bool = False,
) -> dict[str, Any]:
    readiness_errors = _schema_readiness_messages(
        plan,
        schema_name=destination_schema,
        table_name=destination_table,
    )
    errors = list(plan["errors"]) + readiness_errors
    status = "ERROR" if errors else ("WARNING" if plan["warnings"] else "OK")
    return {
        "raw_file_id": candidate.raw_file_id,
        "client_code": candidate.client_code,
        "report_type": candidate.report_type,
        "source_artifact_id": artifact.artifact_id,
        "source_filename": source_filename,
        "destination_database": destination_database,
        "input_rows": plan["input_rows"],
        "has_record_id": plan["original_has_record_id"],
        "usable_record_id": plan["usable_record_id"],
        "non_empty_record_id_rows": plan["non_empty_record_id_rows"],
        "empty_record_id_rows": plan["empty_record_id_rows"],
        "duplicate_record_id_rows_in_input": plan["duplicate_record_id_rows_in_input"],
        "data_overwrite": data_overwrite,
        "data_overwrite_policy_found": policy_found,
        "would_auto_grant_permissions": would_auto_grant_permissions,
        "destination_schema": destination_schema,
        "destination_table": destination_table,
        "destination_schema_exists": plan["destination_schema_exists"],
        "destination_table_exists": plan["destination_table_exists"],
        "columns_to_create": plan["columns_to_create"],
        "columns_to_add": plan["columns_to_add"],
        "unique_index_exists": plan["unique_index_exists"],
        "duplicate_existing_record_ids_detected": plan["duplicate_existing_record_ids_detected"],
        "duplicate_existing_record_ids": plan["duplicate_existing_record_ids"],
        "would_insert_rows": plan["would_insert_rows"],
        "would_update_rows": plan["would_update_rows"],
        "would_skip_rows": plan["would_skip_rows"],
        "would_reject_rows": plan["would_reject_rows"],
        "would_replace_table": plan["would_replace_table"],
        "warnings": plan["warnings"],
        "errors": errors,
        "dry_run_status": status,
    }


def _dry_run_error_summary(
    candidate: Candidate,
    exc: Exception,
    *,
    would_auto_grant_permissions: bool = False,
) -> dict[str, Any]:
    destination_schema = DESTINATION_SCHEMA
    destination_table = None
    try:
        destination_schema, destination_table = _destination_for_report_type(candidate.report_type)
    except Exception:
        pass
    return {
        "raw_file_id": candidate.raw_file_id,
        "client_code": candidate.client_code,
        "report_type": candidate.report_type,
        "source_artifact_id": None,
        "source_filename": candidate.source_filename,
        "destination_database": None,
        "input_rows": 0,
        "has_record_id": False,
        "usable_record_id": False,
        "non_empty_record_id_rows": 0,
        "empty_record_id_rows": 0,
        "duplicate_record_id_rows_in_input": 0,
        "data_overwrite": None,
        "data_overwrite_policy_found": False,
        "would_auto_grant_permissions": would_auto_grant_permissions,
        "destination_schema": destination_schema,
        "destination_table": destination_table,
        "destination_schema_exists": False,
        "destination_table_exists": False,
        "columns_to_create": [],
        "columns_to_add": [],
        "unique_index_exists": False,
        "duplicate_existing_record_ids_detected": False,
        "duplicate_existing_record_ids": [],
        "would_insert_rows": 0,
        "would_update_rows": 0,
        "would_skip_rows": 0,
        "would_reject_rows": 0,
        "would_replace_table": False,
        "warnings": [],
        "errors": [str(exc)],
        "dry_run_status": "ERROR",
    }


def _upload_stage3_dry_run_artifact(client, run_id: str, source_artifact: ArtifactRef, summary: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-b-stage3-dry-run-artifacts-") as tmpdir:
        result_path = Path(tmpdir) / "stage3_dry_run_result.json"
        result_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        client.upload_artifact(
            str(result_path),
            kind="JSON",
            run_id=run_id,
            raw_file_id=summary["raw_file_id"],
            workflow_name=WORKFLOW_NAME,
            stage_name=STAGE3_STAGE,
            artifact_role="dry_run_result",
            report_type=summary["report_type"],
            client_code=summary["client_code"],
            original_filename=source_artifact.original_filename,
            display_filename=result_path.name,
            metadata=summary,
        )


def _missing_destination_schema_objects(
    plan: dict[str, Any],
    *,
    schema_name: str,
    table_name: str,
) -> list[MissingSchemaObject]:
    if not plan["destination_table_exists"]:
        return [MissingSchemaObject(schema_name, table_name, "table", table_name)]
    missing = [
        MissingSchemaObject(schema_name, table_name, "column", column)
        for column in plan["columns_to_add"]
    ]
    if plan["usable_record_id"] and not plan["unique_index_exists"]:
        missing.append(
            MissingSchemaObject(
                schema_name,
                table_name,
                "unique index",
                _safe_index_name(f"{table_name}__record_id_uidx"),
            )
        )
    return missing


def _require_destination_schema_ready(
    plan: dict[str, Any],
    *,
    schema_name: str,
    table_name: str,
) -> None:
    missing = _missing_destination_schema_objects(
        plan,
        schema_name=schema_name,
        table_name=table_name,
    )
    if missing:
        raise Stage3SchemaReadinessError(missing)


def _schema_readiness_messages(
    plan: dict[str, Any],
    *,
    schema_name: str,
    table_name: str,
) -> list[str]:
    missing = _missing_destination_schema_objects(
        plan,
        schema_name=schema_name,
        table_name=table_name,
    )
    return [str(Stage3SchemaReadinessError(missing))] if missing else []


def _load_alpha_gps_replace_all(
    destination_conn,
    *,
    raw_file_id: str,
    run_id: str,
    client_code: str,
    report_type: str,
    source_artifact_id: str,
    source_filename: str | None,
    df,
    source_sha256: str | None,
    raw_artifact_id: str | None,
    normalized_artifact_id: str | None,
    cleaned_artifact_id: str | None,
    destination_schema: str,
    destination_table: str,
) -> LoadResult:
    rows, errors = _alpha_gps_rows(df)
    if errors:
        # Missing required columns, per-row date parse failures, and
        # `empty_result` all arrive here. Every one is a property of the
        # workbook that was already parsed, not of the moment it was tried.
        raise Stage3WriterValidationError(
            "; ".join(errors), signal="alpha_gps_rows_rejected"
        )

    try:
        with destination_conn.cursor() as cur:
            inspection = _inspect_destination(cur, destination_schema, destination_table)
            if not inspection["destination_table_exists"]:
                raise Stage3SchemaReadinessError(
                    [MissingSchemaObject(
                        destination_schema,
                        destination_table,
                        "table",
                        destination_table,
                    )],
                    required_migration="db/client_business/023_alpha_gps_baza_log_workflow_b.sql",
                )
            _replace_alpha_gps_rows(
                cur,
                destination_schema,
                destination_table,
                rows,
                workflow_run_id=run_id,
                raw_file_id=raw_file_id,
                source_artifact_id=raw_artifact_id or source_artifact_id,
                normalized_artifact_id=normalized_artifact_id,
                cleaned_artifact_id=cleaned_artifact_id or source_artifact_id,
                source_sha256=source_sha256,
            )
        destination_conn.commit()
    except Exception:
        destination_conn.rollback()
        raise

    return LoadResult(
        raw_file_id=raw_file_id,
        client_code=client_code,
        report_type=report_type,
        destination_schema=destination_schema,
        destination_table=destination_table,
        data_overwrite=True,
        input_rows=_row_count(df),
        inserted_rows=len(rows),
        updated_rows=0,
        skipped_rows=0,
        status="OK",
        source_artifact_id=source_artifact_id,
        source_filename=source_filename,
        rejected_rows=[],
        raw_artifact_id=raw_artifact_id,
        normalized_artifact_id=normalized_artifact_id,
        cleaned_artifact_id=cleaned_artifact_id or source_artifact_id,
    )


def _alpha_gps_rows(df) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    columns = list(df.columns)
    missing = [column for column in ALPHA_GPS_REQUIRED_COLUMNS if column not in columns]
    if missing:
        errors.append("missing required columns for Alpha_GPS_Baza_LOG: " + ", ".join(missing))
        return [], errors

    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(_iter_rows(df), start=1):
        raw = {column: _cell_text(_row_get(row, column)).strip() for column in columns}
        selected = {column: raw.get(column, "") for column in ALPHA_GPS_REQUIRED_COLUMNS}
        if all(value == "" for value in selected.values()):
            continue
        try:
            assignment_date = _parse_alpha_gps_date(selected["Data przydziału"])
        except ValueError as exc:
            errors.append(f"row {idx}: {exc}")
            continue
        rows.append(
            {
                "source_id": selected["ID"],
                "registration": _normalize_registration(selected["Nr rejestracyjny"]),
                "assignment_date": assignment_date,
                "csv_filename": selected["Nazwa Pliku csv"],
                "source_row_number": idx,
                "raw_row_json": raw,
            }
        )

    if not rows and not errors:
        errors.append("empty_result")
    return rows, errors


def _replace_alpha_gps_rows(
    cur,
    schema_name: str,
    table_name: str,
    rows: list[dict[str, Any]],
    *,
    workflow_run_id: str,
    raw_file_id: str,
    source_artifact_id: str | None,
    normalized_artifact_id: str | None,
    cleaned_artifact_id: str | None,
    source_sha256: str | None,
) -> None:
    sql = _pg_sql()
    cur.execute(
        sql.SQL("DELETE FROM {}.{}").format(
            sql.Identifier(schema_name),
            sql.Identifier(table_name),
        )
    )
    if not rows:
        return
    from psycopg.types.json import Jsonb

    insert_sql = sql.SQL(
        "INSERT INTO {}.{} ("
        " source_id, registration, assignment_date, csv_filename,"
        " workflow_run_id, raw_file_id, source_artifact_id, normalized_artifact_id,"
        " cleaned_artifact_id, source_sha256, source_row_number, raw_row_json"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    ).format(sql.Identifier(schema_name), sql.Identifier(table_name))
    cur.executemany(
        insert_sql,
        [
            (
                row["source_id"],
                row["registration"],
                row["assignment_date"],
                row["csv_filename"],
                workflow_run_id,
                raw_file_id,
                source_artifact_id,
                normalized_artifact_id,
                cleaned_artifact_id,
                source_sha256,
                row["source_row_number"],
                Jsonb(row["raw_row_json"]),
            )
            for row in rows
        ],
    )


def _existing_columns(cur, schema_name: str, table_name: str) -> dict[str, str]:
    cur.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        """,
        (schema_name, table_name),
    )
    return {str(row["column_name"]): str(row["data_type"]) for row in cur.fetchall()}


def _find_existing_duplicate_record_ids(
    cur,
    schema_name: str,
    table_name: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    sql = _pg_sql()
    cur.execute(
        sql.SQL(
            "SELECT record_id, COUNT(*)::int AS count "
            "FROM {}.{} "
            "WHERE NULLIF(btrim(record_id), '') IS NOT NULL "
            "GROUP BY record_id "
            "HAVING COUNT(*) > 1 "
            "ORDER BY record_id "
            "LIMIT %s"
        ).format(sql.Identifier(schema_name), sql.Identifier(table_name)),
        (limit,),
    )
    return [{"record_id": str(row["record_id"]), "count": int(row["count"])} for row in cur.fetchall()]


def _fetch_existing_record_ids(
    cur,
    schema_name: str,
    table_name: str,
    record_ids: list[str],
) -> set[str]:
    if not record_ids:
        return set()
    sql = _pg_sql()
    found: set[str] = set()
    for batch in _chunks(record_ids, 5000):
        cur.execute(
            sql.SQL("SELECT record_id FROM {}.{} WHERE record_id = ANY(%s)").format(
                sql.Identifier(schema_name),
                sql.Identifier(table_name),
            ),
            (batch,),
        )
        found.update(str(row["record_id"]) for row in cur.fetchall())
    return found


def _insert_rows(
    cur,
    schema_name: str,
    table_name: str,
    report_columns: list[str],
    rows: list[dict[str, Any]],
    *,
    raw_file_id: str,
    run_id: str,
    source_artifact_id: str,
    source_filename: str | None,
    conflict_action: str | None = None,
) -> int:
    if not rows:
        return 0
    sql = _pg_sql()
    insert_columns = report_columns + list(TECHNICAL_INSERT_COLUMNS)
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in insert_columns)
    query = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier(schema_name),
        sql.Identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(column) for column in insert_columns),
        placeholders,
    )
    if conflict_action == "nothing":
        query = query + sql.SQL(
            " ON CONFLICT (record_id) WHERE NULLIF(btrim(record_id), '') IS NOT NULL DO NOTHING"
        )

    values = [
        _row_values(row["row"], report_columns)
        + [raw_file_id, source_artifact_id, source_filename, run_id]
        for row in rows
    ]
    cur.executemany(query, values)
    return len(rows)


def _upsert_record_id_rows(
    cur,
    schema_name: str,
    table_name: str,
    report_columns: list[str],
    rows: list[dict[str, Any]],
    existing_record_ids: set[str],
    *,
    raw_file_id: str,
    run_id: str,
    source_artifact_id: str,
    source_filename: str | None,
) -> tuple[int, int]:
    if not rows:
        return 0, 0
    sql = _pg_sql()
    insert_columns = report_columns + list(TECHNICAL_INSERT_COLUMNS)
    update_columns = [column for column in report_columns if column != "record_id"] + list(
        TECHNICAL_INSERT_COLUMNS
    )
    update_assignments = [
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
        for column in update_columns
    ]
    update_assignments.append(sql.SQL("_loaded_at = now()"))
    query = sql.SQL(
        "INSERT INTO {}.{} ({}) VALUES ({}) "
        "ON CONFLICT (record_id) WHERE NULLIF(btrim(record_id), '') IS NOT NULL "
        "DO UPDATE SET {}"
    ).format(
        sql.Identifier(schema_name),
        sql.Identifier(table_name),
        sql.SQL(", ").join(sql.Identifier(column) for column in insert_columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in insert_columns),
        sql.SQL(", ").join(update_assignments),
    )
    values = [
        _row_values(row["row"], report_columns)
        + [raw_file_id, source_artifact_id, source_filename, run_id]
        for row in rows
    ]
    cur.executemany(query, values)
    row_ids = {row["record_id"] for row in rows}
    updated = len(row_ids & existing_record_ids)
    inserted = len(row_ids - existing_record_ids)
    return inserted, updated


def _replace_table_rows(
    cur,
    schema_name: str,
    table_name: str,
    report_columns: list[str],
    rows: list[dict[str, Any]],
    *,
    raw_file_id: str,
    run_id: str,
    source_artifact_id: str,
    source_filename: str | None,
) -> None:
    sql = _pg_sql()
    cur.execute(
        sql.SQL("DELETE FROM {}.{}").format(
            sql.Identifier(schema_name),
            sql.Identifier(table_name),
        )
    )
    _insert_rows(
        cur,
        schema_name,
        table_name,
        report_columns,
        [{"row": row} for row in rows],
        raw_file_id=raw_file_id,
        run_id=run_id,
        source_artifact_id=source_artifact_id,
        source_filename=source_filename,
    )


def _mark_stage3_started(
    cur,
    raw_file_id: str,
    *,
    destination_schema: str,
    destination_table: str,
) -> None:
    cur.execute(
        """
        UPDATE ingest.raw_file
        SET stage3_status = 'RUNNING',
            stage3_started_at = now(),
            stage3_finished_at = NULL,
            stage3_error = NULL,
            stage3_inserted_rows = NULL,
            stage3_updated_rows = NULL,
            stage3_skipped_rows = NULL,
            stage3_destination_schema = %s,
            stage3_destination_table = %s,
            stage3_data_overwrite = NULL
        WHERE id = %s
        """,
        (destination_schema, destination_table, raw_file_id),
    )


def _mark_stage3_finished(cur, result: LoadResult) -> None:
    cur.execute(
        """
        UPDATE ingest.raw_file
        SET stage3_status = %s,
            stage3_finished_at = now(),
            stage3_error = %s,
            stage3_inserted_rows = %s,
            stage3_updated_rows = %s,
            stage3_skipped_rows = %s,
            stage3_destination_schema = %s,
            stage3_destination_table = %s,
            stage3_data_overwrite = %s
        WHERE id = %s
        """,
        (
            result.status,
            result.error,
            result.inserted_rows,
            result.updated_rows,
            result.skipped_rows,
            result.destination_schema,
            result.destination_table,
            result.data_overwrite,
            result.raw_file_id,
        ),
    )


def _mark_stage3_error(
    cur,
    *,
    raw_file_id: str,
    error_message: str,
    destination_schema: str,
    destination_table: str,
    data_overwrite: bool,
    error_category: str = "unclassified",
    retryable: bool = False,
) -> None:
    """Stamp ERROR, carrying the retryability that decides its next owner.

    P0-E. `stage3_error` is prefixed with a parseable marker
    (`format_stage3_error_evidence`) so the classification computed here
    survives the process that computed it. Without it every ERROR row looks
    alike after a restart, and a deterministic failure would be retried at
    06:00 and 20:00 indefinitely. The default is the fail-closed pair
    (`unclassified`, non-retryable) so a caller that forgets cannot
    accidentally create an infinitely retried row.
    """
    marked = (
        format_stage3_error_evidence(error_category, retryable=retryable)
        + str(error_message)
    )
    cur.execute(
        """
        UPDATE ingest.raw_file
        SET stage3_status = 'ERROR',
            stage3_finished_at = now(),
            stage3_error = %s,
            stage3_destination_schema = %s,
            stage3_destination_table = %s,
            stage3_data_overwrite = %s
        WHERE id = %s
        """,
        (marked[:4000], destination_schema, destination_table, data_overwrite, raw_file_id),
    )


def _artifact_filename_component(value: str | None, *, length: int = 8) -> str:
    text = str(value or "").strip()
    if not text:
        return "na"
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text[:length]).strip("._-")
    return text or "na"


def _stage3_artifact_filename(prefix: str, result: LoadResult, ext: str) -> str:
    raw_short = _artifact_filename_component(result.raw_file_id)
    source_short = _artifact_filename_component(result.source_artifact_id)
    return f"{prefix}__{raw_short}__{source_short}.{ext}"


def _upload_stage3_artifacts(client, run_id: str, source_artifact: ArtifactRef, result: LoadResult) -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-b-stage3-artifacts-") as tmpdir:
        tmp = Path(tmpdir)
        result_filename = _stage3_artifact_filename("stage3_load_result", result, "json")
        result_path = tmp / result_filename
        result_path.write_text(json.dumps(_result_context(result), indent=2, sort_keys=True), encoding="utf-8")
        client.upload_artifact(
            str(result_path),
            kind="JSON",
            run_id=run_id,
            raw_file_id=result.raw_file_id,
            workflow_name=WORKFLOW_NAME,
            stage_name=STAGE3_STAGE,
            artifact_role="load_result",
            report_type=result.report_type,
            client_code=result.client_code,
            original_filename=source_artifact.original_filename,
            display_filename=result_filename,
            metadata=_result_context(result),
        )

        if result.rejected_rows:
            rejected_filename = _stage3_artifact_filename("stage3_rejected_rows", result, "csv")
            rejected_path = tmp / rejected_filename
            with rejected_path.open("w", newline="", encoding="utf-8") as f:
                fieldnames = sorted({key for row in result.rejected_rows for key in row.keys()})
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(result.rejected_rows)
            client.upload_artifact(
                str(rejected_path),
                kind="REPORT",
                run_id=run_id,
                raw_file_id=result.raw_file_id,
                workflow_name=WORKFLOW_NAME,
                stage_name=STAGE3_STAGE,
                artifact_role="rejected_rows",
                report_type=result.report_type,
                client_code=result.client_code,
                original_filename=source_artifact.original_filename,
                display_filename=rejected_filename,
                metadata={
                    "raw_file_id": result.raw_file_id,
                    "skipped_rows": result.skipped_rows,
                    "source_artifact_id": source_artifact.artifact_id,
                },
            )


def _result_context(result: LoadResult) -> dict[str, Any]:
    return {
        "raw_file_id": result.raw_file_id,
        "client_code": result.client_code,
        "report_type": result.report_type,
        "destination_schema": result.destination_schema,
        "destination_table": result.destination_table,
        "destination_database": result.destination_database,
        "source_artifact_id": result.source_artifact_id,
        "source_filename": result.source_filename,
        "data_overwrite": result.data_overwrite,
        "input_rows": result.input_rows,
        "inserted_rows": result.inserted_rows,
        "updated_rows": result.updated_rows,
        "skipped_rows": result.skipped_rows,
        "status": result.status,
        "error": result.error,
        "raw_artifact_id": result.raw_artifact_id,
        "normalized_artifact_id": result.normalized_artifact_id,
        "cleaned_artifact_id": result.cleaned_artifact_id,
    }


def _report_columns(df) -> list[str]:
    return [str(column) for column in df.columns]


def _ensure_record_id_column(df) -> None:
    if "record_id" in df.columns:
        return
    try:
        df["record_id"] = ""
    except Exception as exc:
        raise Stage3WriterValidationError(
            "Cleaned report is missing record_id and cannot be amended",
            signal="cleaned_report_missing_record_id",
        ) from exc


def _row_count(df) -> int:
    try:
        return int(len(df.index))
    except AttributeError:
        return int(len(df))


def _has_usable_record_id(df) -> bool:
    if "record_id" not in df.columns:
        return False
    return any(_normalize_record_id(_row_get(row, "record_id")) for row in _iter_rows(df))


def _prepared_record_id_rows(df, report_columns: list[str]) -> dict[str, list[dict[str, Any]]]:
    seen: set[str] = set()
    loadable_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, str]] = []
    for row in _iter_rows(df):
        record_id = _normalize_record_id(_row_get(row, "record_id"))
        if not record_id:
            rejected_rows.append(_rejected_row(row, "empty_record_id"))
            continue
        if record_id in seen:
            rejected_rows.append(_rejected_row(row, "duplicate_record_id_in_input"))
            continue
        seen.add(record_id)
        loadable_rows.append({"record_id": record_id, "row": _row_dict(row, report_columns)})
    return {"loadable_rows": loadable_rows, "rejected_rows": rejected_rows}


def _all_rows(df, report_columns: list[str]) -> list[dict[str, Any]]:
    return [_row_dict(row, report_columns) for row in _iter_rows(df)]


def _iter_rows(df) -> Iterable[Any]:
    if hasattr(df, "iterrows"):
        for _, row in df.iterrows():
            yield row
        return
    yield from df


def _row_get(row, column: str) -> Any:
    if hasattr(row, "get"):
        return row.get(column)
    return row[column]


def _row_dict(row, report_columns: list[str]) -> dict[str, Any]:
    return {column: _cell_text(_row_get(row, column)) for column in report_columns}


def _row_values(row: dict[str, Any], report_columns: list[str]) -> list[str]:
    return [_cell_text(row.get(column)) for column in report_columns]


def _rejected_row(row, reason: str) -> dict[str, str]:
    if hasattr(row, "to_dict"):
        data = {str(key): _cell_text(value) for key, value in row.to_dict().items()}
    elif isinstance(row, dict):
        data = {str(key): _cell_text(value) for key, value in row.items()}
    else:
        data = {"row": _cell_text(row)}
    data["_stage3_skip_reason"] = reason
    return data


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value)
    if text.lower() == "nan":
        return ""
    return text


def _parse_alpha_gps_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _cell_text(value).strip()
    if not text:
        raise ValueError("Data przydziału is required")
    if re.fullmatch(r"\d+(\.0+)?", text):
        serial = int(float(text))
        if serial > 0:
            return (datetime(1899, 12, 30) + timedelta(days=serial)).date()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise ValueError(f"unparseable Data przydziału: {text!r}") from exc


def _normalize_registration(value: Any) -> str:
    return " ".join(_cell_text(value).strip().split()).upper()


def _normalize_record_id(value: Any) -> str:
    return _cell_text(value).strip()


def _validate_report_columns(columns: list[str]) -> None:
    seen: set[str] = set()
    for column in columns:
        if not column or not column.strip():
            raise Stage3WriterValidationError(
                "Cleaned report contains an empty column name",
                signal="cleaned_report_columns_invalid",
            )
        if "\x00" in column:
            raise Stage3WriterValidationError(
                f"Cleaned report column contains a NUL byte: {column!r}",
                signal="cleaned_report_columns_invalid",
            )
        if column in TECHNICAL_COLUMNS:
            raise Stage3WriterValidationError(
                f"Cleaned report column conflicts with Stage 3 metadata column: {column}",
                signal="cleaned_report_columns_invalid",
            )
        if column in seen:
            raise Stage3WriterValidationError(
                f"Cleaned report contains duplicate column name: {column}",
                signal="cleaned_report_columns_invalid",
            )
        seen.add(column)


def _safe_table_name(report_type: str) -> str:
    value = (report_type or "").strip()
    if not SAFE_TABLE_RE.match(value):
        raise Stage3WriterValidationError(
            f"Unsafe report_type for destination table name: {report_type!r}",
            signal="unsafe_destination_identifier",
        )
    return value


def _safe_index_name(index_name: str) -> str:
    value = index_name.strip()
    if not SAFE_TABLE_RE.match(value):
        raise Stage3WriterValidationError(
            f"Unsafe generated index name: {index_name!r}",
            signal="unsafe_destination_identifier",
        )
    return value


def _safe_download_filename(filename: str) -> str:
    name = Path(filename).name
    if not name:
        return "stage2_cleaned_report.csv"
    return name.replace("\x00", "")


def _require_nonempty_artifact_file(path: Path, artifact_id: str) -> None:
    if not path.exists():
        raise Stage3WriterValidationError(
            f"Downloaded artifact file is missing: artifact_id={artifact_id}",
            signal="downloaded_artifact_unusable",
        )
    if path.stat().st_size <= 0:
        raise Stage3WriterValidationError(
            f"Downloaded artifact file is empty: artifact_id={artifact_id}",
            signal="downloaded_artifact_unusable",
        )


def _is_text_type(data_type: str) -> bool:
    return data_type in {"text", "character varying", "character"}


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _pg_sql():
    from psycopg import sql

    return sql


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("limit must be a positive integer")
    return parsed


def _bool_param(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"", "0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean parameter value: {value!r}")
