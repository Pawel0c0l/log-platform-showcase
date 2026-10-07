"""The publication boundary: how a generated report becomes a Report Explorer instance.

This module is the ONLY authorized way to write the three `S15` relations. A
schema and a UI with no authoritative publication path would be a half-feature,
so the contract is stated once, here, and every future report generator calls it
instead of writing SQL of its own.

THE CONTRACT.

    definition  = register_definition(...)      # once per report TYPE
    attempt     = begin_generation(client, type_key, period, display_name)
    ...run the report...
    publish(attempt, files=[...], row_count=..., source=...)
      -- or --
    fail(attempt, code, message)

Everything the boundary needs is EXPLICIT: the client, the definition, the
reporting period, the lifecycle outcome and the produced files. Nothing is
derived from a filename, a folder name, an artifact timestamp or a display
label (`docs/39` §1, §5).

IDEMPOTENCY IS STRUCTURAL, NOT DISCIPLINARY.
    A logical period has exactly ONE identity:
    `(client_code, definition_id, period_kind, period_start)`. `period_key` is
    its NAME, derived from those dates and bound to them by migration `069`, not
    a second key a caller may choose. `begin_generation` upserts on that one
    identity, so two concurrent first generations of the same period converge on
    one row instead of one of them escaping as a uniqueness error, a retry
    reuses the same `instance_id` and the same detail URL, and no read-then-write
    race can fork a period. Regeneration REPLACES the member set inside one
    transaction, so no report is ever visible with a half-published file list
    (`docs/40` §4.2, `I-8`).

FENCING IS TERMINAL, NOT MERELY CURRENT.
    Each attempt takes a claim token, and a claim authorizes exactly ONE
    completion. `publish` and `fail` consume the claim they act on: the token
    moves from `claim_token` (the ACTIVE attempt) to `completed_claim_token`
    plus `completed_claim_outcome` (how it closed). Three answers follow, and
    they are the whole contract:

      active claim      -> the mutation happens, once
      completed claim   -> a replay: the same outcome returns, nothing mutates;
                           a DIFFERENT outcome is refused, so a late `fail()`
                           can never turn a published report into a failed one
      neither           -> stale: a newer attempt owns the instance

    Two concurrent callbacks holding one claim therefore cannot both replace the
    member set — the row is taken `FOR UPDATE`, the first consumes the claim,
    and the second wakes up holding a completed one.

AUTHORITATIVE METADATA.
    A member's content type and byte size are read from the `artifacts` row, not
    from what the caller declared about it. An independent review published
    `file_format='PDF'` with `content_type='text/html'` and `is_previewable=true`
    and the preview embedded it; a caller's description of bytes it does not own
    is not evidence about those bytes.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .contract import (
    VALID_FORMATS,
    VALID_ROLES,
    assert_publication_satisfies,
    contract_from_stored,
    normalize_contract,
)
from .delivery import INLINE_PREVIEW_CONTENT_TYPES, normalize_content_type
from .errors import (
    AttemptAlreadyCompletedError,
    EmptyPublicationError,
    ReportDefinitionNotFoundError,
    ReportProvenanceError,
    ReportPublicationError,
    ReportSchemaUnavailableError,
    StaleAttemptError,
)
from .models import CADENCE_CLASSES
from .periods import PERIOD_KINDS, ReportingPeriod

# `contract.py` owns the role and format vocabulary, because it is the module
# that has to decide whether a declaration and a file set agree. Re-exported
# here so the publication boundary's own surface is unchanged for callers.
VALID_METRIC_KINDS = ("pages", "sheets", "rows")

# The content types each format may actually carry, mirroring migration `069`'s
# `portal_generated_report_files_format_content_type_check`. The badge and the
# MIME describe ONE file, so they are not independently settable.
CONTENT_TYPES_BY_FORMAT = {
    "PDF": ("application/pdf",),
    "XLSX": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ),
    "CSV": ("text/csv", "application/csv"),
    "JSON": ("application/json", "text/json"),
    "TXT": ("text/plain",),
    "ZIP": ("application/zip", "application/x-zip-compressed"),
}


@dataclass(frozen=True)
class PublishedFile:
    """One file an attempt produced, ready to become a member.

    `artifact_id` points at an already-written `artifacts` row: bytes and
    delivery stay in the existing infrastructure, and this record carries the
    product meaning (`docs/40` §8).
    """

    artifact_id: str
    display_filename: str
    file_format: str
    content_type: str
    size_bytes: int
    semantic_role: str
    is_main_file: bool = False
    is_previewable: bool = False
    content_metric_kind: str | None = None
    content_metric_value: int | None = None
    expires_at: datetime | None = None
    # `None` means "the caller did not care"; the publication order is used.
    # An explicit `0` is a real position and is honoured — display order is
    # presentation, but silently rewriting a declared one would reorder the
    # approved files panel behind the generator's back.
    display_order: int | None = None


@dataclass(frozen=True)
class SourceSnapshot:
    """The resolved `RP-18` binding, captured at generation time."""

    dataset_id: str | None
    dataset_slug: str
    dataset_name: str
    date_column: str


@dataclass(frozen=True)
class GenerationAttempt:
    """The handle a running generation holds over one instance."""

    instance_id: str
    definition_id: str
    client_code: str
    period_key: str
    claim_token: str
    attempt_count: int


@dataclass(frozen=True)
class DefinitionSpec:
    """The declarative description of one report TYPE."""

    type_key: str
    display_name: str
    cadence_class: str
    period_kind: str
    generation_definition_ref: str
    description: str = ""
    cadence_detail: str = ""
    period_timezone: str = "Europe/Warsaw"
    source_dataset_slug: str | None = None
    source_date_column: str | None = None
    file_contract: tuple[dict, ...] = field(default=())
    retention_months: int | None = None
    is_active: bool = True


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReportPublicationError(message)


class ReportPublicationService:
    """Writes generated-report definitions, instances and members.

    `connection_factory` is the platform-database connection callable, injected
    so this module stays free of `api.main` and can be driven directly by a job,
    a test or a future scheduler.
    """

    def __init__(self, connection_factory) -> None:
        self._connect = connection_factory

    # -- definitions --------------------------------------------------------

    def register_definition(self, spec: DefinitionSpec) -> str:
        """Create or update one report type, keyed by its canonical `type_key`.

        `type_key` is the identity and is never rewritten: an update changes the
        display metadata, the cadence detail, the source binding and enablement,
        and it deliberately cannot change `period_kind`, because instances
        reference `(definition_id, period_kind)` and a type that changed
        vocabulary would invalidate its own period ordering (`I-4`).
        """
        _require(spec.cadence_class in CADENCE_CLASSES, f"unknown cadence class {spec.cadence_class!r}")
        _require(spec.period_kind in PERIOD_KINDS, f"unknown period kind {spec.period_kind!r}")
        _require(bool(spec.type_key), "a report definition needs a type key")
        _require(bool(spec.display_name), "a report definition needs a display name")
        _require(
            (spec.source_dataset_slug is None) == (spec.source_date_column is None),
            "a source binding needs both a dataset slug and a date column, or neither",
        )
        # `docs/40` §1.1 makes the file contract the definition's responsibility.
        # Validating it HERE is what lets `publish` prove a file set against it:
        # an unvalidated declaration could not be enforced without guessing what
        # its author meant.
        declared = normalize_contract(spec.file_contract)
        contract = json.dumps([dict(entry) for entry in declared], ensure_ascii=False)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO portal_generated_report_definitions (
                      type_key, display_name, description, cadence_class, cadence_detail,
                      period_kind, period_timezone, source_dataset_slug, source_date_column,
                      generation_definition_ref, file_contract_json, retention_months, is_active
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    ON CONFLICT (type_key) DO UPDATE SET
                      display_name = EXCLUDED.display_name,
                      description = EXCLUDED.description,
                      cadence_class = EXCLUDED.cadence_class,
                      cadence_detail = EXCLUDED.cadence_detail,
                      period_timezone = EXCLUDED.period_timezone,
                      source_dataset_slug = EXCLUDED.source_dataset_slug,
                      source_date_column = EXCLUDED.source_date_column,
                      generation_definition_ref = EXCLUDED.generation_definition_ref,
                      file_contract_json = EXCLUDED.file_contract_json,
                      retention_months = EXCLUDED.retention_months,
                      is_active = EXCLUDED.is_active,
                      updated_at = now()
                    RETURNING definition_id, period_kind
                    """,
                    (
                        spec.type_key, spec.display_name, spec.description, spec.cadence_class,
                        spec.cadence_detail, spec.period_kind, spec.period_timezone,
                        spec.source_dataset_slug, spec.source_date_column,
                        spec.generation_definition_ref, contract, spec.retention_months,
                        spec.is_active,
                    ),
                )
                row = cur.fetchone() or {}
        stored_kind = str(row.get("period_kind") or "")
        if stored_kind != spec.period_kind:
            raise ReportPublicationError(
                f"report type {spec.type_key!r} already produces {stored_kind!r} periods; "
                f"a type cannot change its period vocabulary"
            )
        return str(row.get("definition_id"))

    def get_definition(self, type_key: str) -> dict | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM portal_generated_report_definitions WHERE type_key = %s",
                    (type_key,),
                )
                return cur.fetchone()

    # -- lifecycle ----------------------------------------------------------

    def begin_generation(
        self,
        *,
        client_code: str,
        type_key: str,
        period: ReportingPeriod,
        display_name: str,
        now: datetime | None = None,
    ) -> GenerationAttempt:
        """Claim `(client, type, period)` for a new attempt.

        Creates the instance the first time and re-claims it on every retry or
        regeneration. An instance that is already published STAYS published
        while the new attempt runs: `last_published_at` is untouched, so the
        library keeps rendering `Gotowy` with working actions instead of hiding
        files that exist (`docs/40` §3.2).

        CONCURRENCY. The conflict target is the ONE logical identity —
        `(client_code, definition_id, period_kind, period_start)`. An
        independent review reproduced two concurrent first generations of one
        period where the loser raised PostgreSQL `23505`, because the upsert
        named the period-KEY index while the period-START index was the one that
        actually collided. With `069` those are no longer two identities: the
        key is derived from the dates, and this statement names the identity the
        database orders periods by. The second caller therefore blocks on the
        index, wakes up, and takes the `DO UPDATE` branch on the SAME row.
        """
        _require(bool(client_code), "a report instance needs a client")
        _require(bool(display_name), "a report instance needs a display name")
        definition = self.get_definition(type_key)
        if not definition:
            raise ReportDefinitionNotFoundError(f"no report definition for type key {type_key!r}")
        if period.kind != str(definition.get("period_kind")):
            raise ReportPublicationError(
                f"report type {type_key!r} produces {definition.get('period_kind')!r} periods, "
                f"not {period.kind!r}"
            )
        token = str(uuid.uuid4())
        started = now
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO portal_generated_report_instances (
                      definition_id, client_code, period_kind, period_key,
                      period_start, period_end, display_name,
                      generation_state, generation_started_at, generation_finished_at,
                      library_timestamp, attempt_count, claim_token
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s,
                            'running', COALESCE(%s, now()), NULL,
                            COALESCE(%s, now()), 1, %s)
                    ON CONFLICT ON CONSTRAINT portal_generated_report_instances_period_start_uniq
                    DO UPDATE SET
                      display_name = EXCLUDED.display_name,
                      generation_state = 'running',
                      generation_started_at = EXCLUDED.generation_started_at,
                      generation_finished_at = NULL,
                      -- A published instance keeps ordering by the generation
                      -- that produced its visible files; only an instance that
                      -- has never published moves to the new attempt's start.
                      library_timestamp = CASE
                        WHEN portal_generated_report_instances.last_published_at IS NULL
                          THEN EXCLUDED.generation_started_at
                        ELSE portal_generated_report_instances.library_timestamp
                      END,
                      attempt_count = portal_generated_report_instances.attempt_count + 1,
                      claim_token = EXCLUDED.claim_token,
                      -- A NEW attempt owns the instance, so the previous
                      -- attempt's terminal record stops being a replay target
                      -- and its late callbacks become plainly stale.
                      completed_claim_token = NULL,
                      completed_claim_outcome = NULL,
                      safe_error_code = NULL,
                      safe_error_message = NULL,
                      updated_at = now()
                    RETURNING instance_id, definition_id, attempt_count
                    """,
                    (
                        definition["definition_id"], client_code, period.kind, period.key,
                        period.start, period.end, display_name,
                        started, started, token,
                    ),
                )
                row = cur.fetchone() or {}
        return GenerationAttempt(
            instance_id=str(row["instance_id"]),
            definition_id=str(row["definition_id"]),
            client_code=client_code,
            period_key=period.key,
            claim_token=token,
            attempt_count=int(row.get("attempt_count") or 1),
        )

    # -- attempt fencing ----------------------------------------------------

    _ACTIVE = "active"
    _REPLAY = "replay"

    def _hold(self, cur, attempt: GenerationAttempt, *, intent: str) -> tuple[str, dict]:
        """Lock the instance and decide what this claim is still allowed to do.

        Returns `("active", row)` when the claim may perform its mutation, or
        `("replay", row)` when this exact attempt already completed with this
        exact outcome and the correct answer is to change nothing. Every other
        case raises: a stale claim and a completed claim asking for a DIFFERENT
        outcome are both refusals, not fallbacks.

        `FOR UPDATE` is what makes two concurrent callbacks holding one claim
        serialise: the second one blocks here and re-reads the row the first one
        already closed.
        """
        cur.execute(
            """
            SELECT instance_id, client_code, definition_id,
                   period_kind, period_start, period_end,
                   claim_token, completed_claim_token, completed_claim_outcome,
                   last_published_at
              FROM portal_generated_report_instances
             WHERE instance_id = %s
               FOR UPDATE
            """,
            (attempt.instance_id,),
        )
        held = cur.fetchone()
        if not held:
            raise StaleAttemptError("the report instance no longer exists")
        active = str(held.get("claim_token") or "")
        completed = str(held.get("completed_claim_token") or "")
        outcome = str(held.get("completed_claim_outcome") or "")
        if active and active == attempt.claim_token:
            return self._ACTIVE, held
        if completed and completed == attempt.claim_token:
            if outcome == intent:
                # An exact replay of a completed callback — a client retry, a
                # duplicated delivery. It is answered, and it mutates nothing:
                # re-running the mutation to look idempotent would replace a
                # member set that is already correct.
                return self._REPLAY, held
            raise AttemptAlreadyCompletedError(
                f"this generation attempt already completed as {outcome!r} and "
                f"cannot now complete as {intent!r}",
                outcome=outcome,
            )
        raise StaleAttemptError(
            "this generation attempt no longer holds the report instance; "
            "a newer attempt has taken it"
        )

    def publish(
        self,
        attempt: GenerationAttempt,
        *,
        files: list[PublishedFile] | tuple[PublishedFile, ...],
        row_count: int | None = None,
        source: SourceSnapshot | None = None,
        period_from: date | None = None,
        period_to_exclusive: date | None = None,
        now: datetime | None = None,
    ) -> str:
        """Publish this attempt's complete member set, atomically.

        One transaction supersedes the previous member set, inserts the new one,
        stamps the publication and records the provenance. From a reader's point
        of view the instance transitions to published exactly once, with all its
        files — there is no window in which it is `Gotowy` with a partial list
        (`docs/40` §4.2).

        The claim is CONSUMED here. A second `publish` with the same claim — a
        retry, a duplicated callback, or a genuinely concurrent second call — is
        recognised as a replay and returns without mutating anything.
        """
        members = list(files or ())
        if not members:
            # `I-7` at the boundary, so the generator gets a named error rather
            # than a constraint violation at COMMIT.
            raise EmptyPublicationError(
                "a successful report publication must publish at least one file; "
                "report the attempt as failed instead"
            )
        self._validate_members(members)

        with self._connect() as conn:
            with conn.cursor() as cur:
                state, held = self._hold(cur, attempt, intent="succeeded")
                if state == self._REPLAY:
                    return attempt.instance_id

                definition = self._locked_definition(cur, str(held["definition_id"]))
                # AUTHORITATIVE METADATA. What the stored object actually is
                # replaces what the caller said it was, before anything about it
                # is persisted or rendered.
                bound = self._bind_members_to_artifacts(
                    cur, members, str(held.get("client_code") or "")
                )
                # The definition's declared outputs, proven against the set that
                # is about to become this report's files.
                assert_publication_satisfies(
                    contract_from_stored(definition.get("file_contract_json")), bound
                )
                dataset_id, provenance = self._resolve_provenance(
                    cur,
                    definition=definition,
                    instance=held,
                    source=source,
                    period_from=period_from,
                    period_to_exclusive=period_to_exclusive,
                )

                # Regeneration replaces; it never leaves two live member sets
                # (`docs/40` §4.4). The old artifacts are the caller's to clean
                # up — deleting the member never deletes bytes, and deleting the
                # bytes never deletes the member.
                cur.execute(
                    "DELETE FROM portal_generated_report_files WHERE instance_id = %s",
                    (attempt.instance_id,),
                )
                for order, member in enumerate(bound):
                    cur.execute(
                        """
                        INSERT INTO portal_generated_report_files (
                          instance_id, artifact_id, display_filename, file_format, content_type,
                          size_bytes, is_previewable, semantic_role, is_main_file,
                          content_metric_kind, content_metric_value, expires_at, display_order
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            attempt.instance_id, member.artifact_id, member.display_filename,
                            member.file_format, member.content_type, int(member.size_bytes),
                            bool(member.is_previewable), member.semantic_role,
                            bool(member.is_main_file), member.content_metric_kind,
                            member.content_metric_value, member.expires_at,
                            order if member.display_order is None else int(member.display_order),
                        ),
                    )
                cur.execute(
                    """
                    UPDATE portal_generated_report_instances
                       SET generation_state = 'succeeded',
                           generation_finished_at = COALESCE(%s, now()),
                           last_published_at = COALESCE(%s, now()),
                           library_timestamp = COALESCE(%s, now()),
                           row_count = %s,
                           source_dataset_id = %s,
                           source_provenance_json = %s::jsonb,
                           claim_token = NULL,
                           completed_claim_token = %s,
                           completed_claim_outcome = 'succeeded',
                           safe_error_code = NULL,
                           safe_error_message = NULL,
                           updated_at = now()
                     WHERE instance_id = %s
                       AND claim_token = %s
                    """,
                    (
                        now, now, now, row_count,
                        dataset_id,
                        json.dumps(provenance, ensure_ascii=False),
                        attempt.claim_token,
                        attempt.instance_id, attempt.claim_token,
                    ),
                )
                if cur.rowcount != 1:
                    raise StaleAttemptError("the report instance was claimed by a newer attempt")
        return attempt.instance_id

    def fail(
        self,
        attempt: GenerationAttempt,
        *,
        safe_error_code: str,
        safe_error_message: str = "",
        now: datetime | None = None,
    ) -> None:
        """Record that this attempt terminated without publishing.

        A never-published instance becomes `Błąd generowania` with no files and
        the `Zgłoś problem` action (`RP-9`). An instance that HAS published stays
        `Gotowy`: the failure is operational state, and contradicting the files
        on screen would remove actions that work (`docs/40` §3.2).

        A claim that already PUBLISHED cannot fail. The review's late-`fail()`
        case — a slow error path arriving after a successful publication and
        rewriting the report as failed — is refused here, before any statement
        touches the instance.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                state, _held = self._hold(cur, attempt, intent="failed")
                if state == self._REPLAY:
                    return
                cur.execute(
                    """
                    UPDATE portal_generated_report_instances
                       SET generation_state = 'failed',
                           generation_finished_at = COALESCE(%s, now()),
                           library_timestamp = CASE
                             WHEN last_published_at IS NULL THEN COALESCE(%s, now())
                             ELSE library_timestamp
                           END,
                           claim_token = NULL,
                           completed_claim_token = %s,
                           completed_claim_outcome = 'failed',
                           safe_error_code = %s,
                           safe_error_message = %s,
                           updated_at = now()
                     WHERE instance_id = %s
                       AND claim_token = %s
                    """,
                    (
                        now, now, attempt.claim_token,
                        (safe_error_code or "")[:80], (safe_error_message or "")[:500],
                        attempt.instance_id, attempt.claim_token,
                    ),
                )
                if cur.rowcount != 1:
                    raise StaleAttemptError("the report instance was claimed by a newer attempt")

    # -- retention ----------------------------------------------------------

    def expire_due_members(self, *, now: datetime | None = None) -> int:
        """Mark members whose retention window has elapsed as unavailable.

        Availability and generation state are separate layers (`docs/40` §3.3):
        this never touches `generation_state`, never deletes a member and never
        deletes an instance. A report whose files expired remains a historical
        instance rendering `Pliki wygasły` — the record outlives the bytes.
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE portal_generated_report_files
                       SET is_available = FALSE, updated_at = now()
                     WHERE is_available
                       AND expires_at IS NOT NULL
                       AND expires_at <= COALESCE(%s, now())
                    """,
                    (now,),
                )
                return int(cur.rowcount or 0)

    # -- validation ---------------------------------------------------------

    def _validate_members(self, members: list[PublishedFile]) -> None:
        mains = [m for m in members if m.is_main_file]
        if len(mains) != 1:
            # `I-6`. Exactly one, declared explicitly — never the first one, the
            # PDF one, or the one whose name looks right.
            raise ReportPublicationError(
                f"a published report needs exactly one explicit main file, got {len(mains)}"
            )
        seen_names: set[str] = set()
        seen_artifacts: set[str] = set()
        for member in members:
            _require(bool(member.artifact_id), "a published file needs a stored object")
            _require(bool(member.display_filename), "a published file needs a display filename")
            _require(member.file_format in VALID_FORMATS, f"unsupported file format {member.file_format!r}")
            _require(member.semantic_role in VALID_ROLES, f"unknown file role {member.semantic_role!r}")
            _require(int(member.size_bytes) >= 0, "a published file cannot have a negative size")
            _require(
                (member.content_metric_kind is None) == (member.content_metric_value is None),
                "a content metric needs both a kind and a value",
            )
            _require(
                member.content_metric_kind is None or member.content_metric_kind in VALID_METRIC_KINDS,
                f"unknown content metric {member.content_metric_kind!r}",
            )
            name = member.display_filename.casefold()
            if name in seen_names:
                raise ReportPublicationError(
                    f"two published files share the display filename {member.display_filename!r}"
                )
            seen_names.add(name)
            if str(member.artifact_id) in seen_artifacts:
                raise ReportPublicationError("the same stored object was published twice")
            seen_artifacts.add(str(member.artifact_id))

    def _locked_definition(self, cur, definition_id: str) -> dict:
        cur.execute(
            "SELECT * FROM portal_generated_report_definitions WHERE definition_id = %s",
            (definition_id,),
        )
        row = cur.fetchone()
        if not row:  # pragma: no cover - the FK makes this unreachable
            raise ReportDefinitionNotFoundError("the report definition no longer exists")
        return row

    def _bind_members_to_artifacts(
        self, cur, members: list[PublishedFile], client_code: str
    ) -> list[PublishedFile]:
        """Replace declared byte metadata with the stored object's own.

        Two rules, and the second is the one the review's HTML-labelled-as-PDF
        member broke:

        1. **No instance may publish another client's bytes.** The schema cannot
           express a cross-table equality, so it is enforced here — the only
           place that can create a membership — and again on every delivery.
        2. **The artifact describes itself.** `content_type` and `size_bytes` are
           taken from the `artifacts` row, never from the caller. The declared
           `file_format` must then be consistent with that authoritative content
           type, and `is_previewable` survives only for content this platform
           actually embeds. A caller can still turn previewability OFF; it can
           no longer turn it on for bytes that are not a PDF.
        """
        ids = [str(m.artifact_id) for m in members]
        cur.execute(
            """
            SELECT artifact_id, client_code, content_type, size_bytes
              FROM artifacts
             WHERE artifact_id = ANY(%s::uuid[])
            """,
            (ids,),
        )
        found = {str(row["artifact_id"]): row for row in cur.fetchall()}

        bound: list[PublishedFile] = []
        for member in members:
            artifact_id = str(member.artifact_id)
            row = found.get(artifact_id)
            if row is None:
                raise ReportPublicationError(f"stored object {artifact_id} does not exist")
            if str(row.get("client_code") or "") != client_code:
                raise ReportPublicationError(
                    "a report instance cannot publish a stored object belonging to another client"
                )
            actual_type = normalize_content_type(row.get("content_type"))
            if not actual_type:
                raise ReportPublicationError(
                    f"stored object {artifact_id} declares no content type and cannot be published"
                )
            allowed = CONTENT_TYPES_BY_FORMAT.get(member.file_format, ())
            if actual_type not in allowed:
                raise ReportPublicationError(
                    f"the stored object is {actual_type!r}, which is not a "
                    f"{member.file_format} file"
                )
            actual_size = int(row.get("size_bytes") or 0)
            if actual_size < 0:  # pragma: no cover - defensive
                actual_size = 0
            bound.append(
                PublishedFile(
                    artifact_id=artifact_id,
                    display_filename=member.display_filename,
                    file_format=member.file_format,
                    # AUTHORITATIVE, both of them.
                    content_type=actual_type,
                    size_bytes=actual_size,
                    semantic_role=member.semantic_role,
                    is_main_file=member.is_main_file,
                    is_previewable=bool(member.is_previewable)
                    and actual_type in INLINE_PREVIEW_CONTENT_TYPES,
                    content_metric_kind=member.content_metric_kind,
                    content_metric_value=member.content_metric_value,
                    expires_at=member.expires_at,
                    display_order=member.display_order,
                )
            )
        return bound

    def _resolve_provenance(
        self, cur, *, definition: dict, instance: dict, source: SourceSnapshot | None,
        period_from: date | None, period_to_exclusive: date | None,
    ) -> tuple[str | None, dict]:
        """`RP-18` provenance, validated against the catalogue and this period.

        The review published another client's dataset id, a slug inconsistent
        with the definition, an arbitrary date column and an unrelated source
        range, and every one of them was stored. Live navigation still
        reauthorized correctly, so nothing escalated — but the persisted
        historical record was false, and a provenance snapshot exists precisely
        to be true years later (`docs/40` §12).

        Every field is now either resolved from a server-side authority or
        checked against one:

        * the dataset must be the definition's declared `slug` **resolved for
          this instance's client** and CURRENTLY ACTIVE in the catalogue — so a
          foreign-client dataset id, a mismatched slug and a dataset already
          withdrawn from the Database Explorer catalogue are all refused, and the
          dataset NAME is read from the catalogue rather than accepted from the
          caller. `is_active IS TRUE` is the same predicate
          `_get_portal_database_dataset_for_user` enforces at click time, so a
          NEW publication can never name as its authoritative source a dataset
          the catalogue no longer offers;
        * the date column must be the definition's declared column **and** exist
          in the dataset's approved column catalogue, so it is never arbitrary
          text;
        * the applied range must be this instance's own reporting period. It is
          DERIVED by default and only checked when supplied, so a snapshot can
          never describe a different interval than the report it belongs to.

        The activity rule binds PUBLICATION, not history. A report published
        while its dataset was active stays a valid historical report after the
        dataset is deactivated: nothing here rewrites, invalidates or deletes an
        instance that already exists. Whether that report's source-data ACTION is
        offered later is a separate, click-time question, answered by the current
        catalogue and the current Database Explorer grant (`service._source_link`).
        """
        declared_slug = str(definition.get("source_dataset_slug") or "")
        declared_column = str(definition.get("source_date_column") or "")
        period_start = instance.get("period_start")
        period_end = instance.get("period_end")

        if source is None:
            return None, {}
        if not declared_slug or not declared_column:
            raise ReportProvenanceError(
                "this report type declares no source-data binding, so it cannot "
                "record source provenance"
            )
        if str(source.dataset_slug or "") != declared_slug:
            raise ReportProvenanceError(
                f"this report type reads dataset {declared_slug!r}, "
                f"not {source.dataset_slug!r}"
            )
        if str(source.date_column or "") != declared_column:
            raise ReportProvenanceError(
                f"this report type filters on {declared_column!r}, "
                f"not {source.date_column!r}"
            )
        if not source.dataset_id:
            raise ReportProvenanceError("source provenance needs the resolved dataset")

        cur.execute(
            """
            SELECT d.dataset_id, d.dataset_name
              FROM portal_database_datasets d
             WHERE d.dataset_id = %s
               AND d.client_code = %s
               AND d.slug = %s
               AND d.is_active IS TRUE
             LIMIT 1
            """,
            (str(source.dataset_id), str(instance.get("client_code") or ""), declared_slug),
        )
        dataset = cur.fetchone()
        if not dataset:
            # One indistinguishable refusal for "another client's dataset",
            # "a dataset with a different slug", "no such dataset" and "a dataset
            # that is no longer active": the publication boundary is not a probe
            # for what exists, and it fails closed for every one of them.
            raise ReportProvenanceError(
                "the source dataset does not resolve to this client's active "
                f"{declared_slug!r} dataset"
            )

        cur.execute(
            """
            SELECT 1 FROM portal_database_dataset_columns
             WHERE dataset_id = %s AND column_name = %s
             LIMIT 1
            """,
            (str(dataset["dataset_id"]), declared_column),
        )
        if not cur.fetchone():
            raise ReportProvenanceError(
                f"{declared_column!r} is not a column of the source dataset's "
                f"approved catalogue"
            )

        expected_from = period_start
        expected_to = period_end + timedelta(days=1) if period_end else None
        applied_from = period_from if period_from is not None else expected_from
        applied_to = period_to_exclusive if period_to_exclusive is not None else expected_to
        if applied_from != expected_from or applied_to != expected_to:
            raise ReportProvenanceError(
                f"the source range {applied_from}..{applied_to} is not this report's "
                f"reporting period {expected_from}..{expected_to}"
            )

        return str(dataset["dataset_id"]), {
            "dataset_slug": declared_slug,
            # The catalogue's name, not the caller's claim about it.
            "dataset_name": str(dataset.get("dataset_name") or ""),
            "date_column": declared_column,
            "applied_from": applied_from.isoformat() if applied_from else None,
            "applied_to_exclusive": applied_to.isoformat() if applied_to else None,
        }


def generated_report_schema_available(connection_factory) -> bool:
    """Whether migration 068 has been applied to this database.

    `S15` declares 068 in `db/schema_requirements.json`, so a release cannot
    activate over a database missing it. This probe exists for the paths that
    gate does not cover — a development database, a restored dump — and its only
    job is to let the module say plainly that the library is unavailable instead
    of rendering an empty one.
    """
    required = {
        "portal_generated_report_definitions",
        "portal_generated_report_instances",
        "portal_generated_report_files",
    }
    try:
        with connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name FROM information_schema.tables
                     WHERE table_schema = 'public' AND table_name = ANY(%s)
                    """,
                    (sorted(required),),
                )
                present = {str(row["table_name"]) for row in cur.fetchall()}
    except Exception:  # noqa: BLE001 - an unreadable catalogue is an absent schema
        return False
    return required.issubset(present)


__all__ = [
    "DefinitionSpec",
    "GenerationAttempt",
    "PublishedFile",
    "ReportPublicationService",
    "ReportSchemaUnavailableError",
    "SourceSnapshot",
    "generated_report_schema_available",
]
