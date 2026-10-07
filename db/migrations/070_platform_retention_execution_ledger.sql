-- 070_platform_retention_execution_ledger.sql
--
-- Observability for the global 13-calendar-month hard-retention sweep
-- (`ops/hard_retention.py`, governed by `ops/retention_registry.py`).
--
-- WHY A TABLE AT ALL. A retention sweep that leaves no durable trace can only
-- be audited by reading journald, which is itself not retained by policy on
-- this host. An operator asked "did the ceiling actually run against
-- alpha_main last week, and what is the oldest row still in report_207?" needs
-- an answer that survives a log rotation.
--
-- WHY CURRENT-STATE AND NOT A HISTORY LOG. A history table of every sweep of
-- every policy of every client would itself accumulate without bound, and would
-- then need its own retention policy — a governance layer that cannot govern
-- itself is not one. This table therefore holds exactly one row per
-- (policy_id, scope): the LAST outcome. Its row count is bounded by the number
-- of registered policies times the number of client databases, it is rewritten
-- in place, and it is registered under
-- `platform_db.current_state_singletons` in the retention registry for the same
-- reason `ops_control.scheduler_heartbeat` is.
--
-- WHY IT RECORDS `oldest_remaining_ts`. "Deleted 0 rows" has two very different
-- meanings — nothing was eligible, or the sweep never reached this store. The
-- oldest surviving eligible timestamp distinguishes them, and it is the single
-- number that proves compliance: if it is never older than the cutoff, the
-- ceiling holds.
--
-- ADDITIVE AND SAFE ON A POPULATED DATABASE. One new table in the existing
-- `ops_control` schema, no column touched anywhere else, no data moved, and no
-- deletion performed. Applying it changes no runtime behaviour on its own:
-- until `ops/hard_retention.py` is scheduled, the table simply stays empty.

BEGIN;

CREATE TABLE IF NOT EXISTS ops_control.retention_execution (
    -- The registry identifier, e.g. `platform_db.public.portal_audit_events`.
    -- Deliberately NOT a foreign key to anything: the registry is repository
    -- code, and a database constraint pointing at it would make an ordinary
    -- code change require a migration.
    policy_id            text        NOT NULL,
    -- Which instance of the store this row is about. `platform` for the
    -- platform database, the client code for a client business database, the
    -- absolute path for a filesystem root. One policy can legitimately have one
    -- row per client.
    scope                text        NOT NULL,
    executed_at          timestamptz NOT NULL,
    -- The cutoff the sweep actually used, so a disputed deletion can be
    -- reconstructed exactly rather than recomputed from a later clock.
    cutoff_ts            timestamptz NOT NULL,
    dry_run              boolean     NOT NULL,
    classification       text        NOT NULL,
    examined_count       bigint      NOT NULL DEFAULT 0,
    deleted_count        bigint      NOT NULL DEFAULT 0,
    -- Rows the sweep found eligible by age but deliberately did not remove: a
    -- RUNNING schedule fire, an unpublished export object still awaiting
    -- cleanup, a raw file another row still names. A non-zero value here is
    -- normal; a GROWING one is the signal that something needs an operator.
    skipped_count        bigint      NOT NULL DEFAULT 0,
    failed_count         bigint      NOT NULL DEFAULT 0,
    -- Oldest eligible record still present after the sweep. NULL means nothing
    -- eligible remains, which is the compliant steady state.
    oldest_remaining_ts  timestamptz NULL,
    -- Safe, aggregate-only detail. Never a row payload, never an object key,
    -- never a capability value.
    detail               jsonb       NOT NULL DEFAULT '{}'::jsonb,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_retention_execution PRIMARY KEY (policy_id, scope),
    CONSTRAINT ck_retention_execution_policy_id
        CHECK (policy_id ~ '^[a-z][a-z0-9_.]{2,119}$'),
    CONSTRAINT ck_retention_execution_scope
        CHECK (length(scope) BETWEEN 1 AND 200),
    CONSTRAINT ck_retention_execution_counts
        CHECK (examined_count >= 0 AND deleted_count >= 0
               AND skipped_count >= 0 AND failed_count >= 0),
    CONSTRAINT ck_retention_execution_detail
        CHECK (jsonb_typeof(detail) = 'object'),
    CONSTRAINT ck_retention_execution_classification
        CHECK (classification IN (
            'RETENTION_DRY_RUN_SUCCEEDED',
            'RETENTION_EXECUTION_SUCCEEDED',
            'RETENTION_PARTIAL_FAILURE',
            'RETENTION_SKIPPED_BLOCKED',
            'RETENTION_FAILED'
        ))
);

COMMENT ON TABLE ops_control.retention_execution IS
    'Last outcome per (retention policy, scope) for the global hard-retention sweep. Current state, not history: one row per pair, rewritten in place.';
COMMENT ON COLUMN ops_control.retention_execution.oldest_remaining_ts IS
    'Oldest still-eligible record after the sweep. NULL is the compliant steady state; a value older than cutoff_ts means the ceiling is not being met.';

-- The operator question this index answers is "what is not compliant?", which
-- is a scan of the small subset where something eligible survived.
CREATE INDEX IF NOT EXISTS idx_retention_execution_noncompliant
    ON ops_control.retention_execution (policy_id, scope)
    WHERE oldest_remaining_ts IS NOT NULL;

COMMIT;
