-- 059_operational_watchdog_state.sql
--
-- P0-3 / P0-4 operational safety foundation.
--
-- Two additive tables in the existing `ops_control` schema. Neither changes any
-- scheduling, coverage or idempotency semantics; both are pure observability
-- state that the independent watchdogs read and write.
--
--   ops_control.scheduler_heartbeat
--     The Workflow A dispatcher deliberately writes NO `public.runs` row for a
--     successful no-work tick (`DEFERRED_RUN_CREATION`). That is correct, but it
--     means `public.runs` cannot prove the dispatcher is alive: a dead timer and
--     an idle timer look identical. The dispatcher therefore stamps one row per
--     component on every tick, before it takes the advisory lock, so the
--     heartbeat proves "the process started and reached the database" even when
--     the tick legitimately does nothing.
--
--   ops_control.watchdog_observation
--     Last observed verdict per watchdog subject. Used only to make repeated
--     scans idempotent and to detect recovery transitions; alert cooldown and
--     deduplication remain owned by `suspected_bug_incidents`.
--
-- Both tables are single-row-per-key upserts and are safe to truncate: doing so
-- costs one extra incident per subject, never a missed one.

BEGIN;

CREATE TABLE IF NOT EXISTS ops_control.scheduler_heartbeat (
    component            text PRIMARY KEY,
    last_beat_at         timestamptz NOT NULL,
    last_beat_detail     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    beat_count           bigint      NOT NULL DEFAULT 1,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_scheduler_heartbeat_component
        CHECK (component ~ '^[a-z][a-z0-9_.]{2,79}$'),
    CONSTRAINT ck_scheduler_heartbeat_beat_count
        CHECK (beat_count >= 0)
);

COMMENT ON TABLE ops_control.scheduler_heartbeat IS
    'Liveness proof for schedulers whose idle ticks intentionally persist no run row.';
COMMENT ON COLUMN ops_control.scheduler_heartbeat.last_beat_at IS
    'Wall-clock instant the component last reached the platform database.';

-- `open_incident_fingerprints` is what makes recovery *exact*. Incident identity
-- is a fingerprint, and `suspected_bug_incidents` has no subject column, so
-- resolving by (component, incident_code) closed every filesystem's incident
-- whenever any one of them recovered. Each subject now carries the fingerprints
-- it currently has open, so recovery resolves those and nothing else — and a
-- warning superseded by a critical on the same mountpoint is closed explicitly
-- rather than left open alongside it.
--
-- `subject_key` is deliberately stable per *subject*, never per scheduled fire.
-- Keying it per fire made a continuing Workflow B outage open a fresh incident
-- twice a day, none of which could ever resolve, because the later successful
-- fire had a different key.
CREATE TABLE IF NOT EXISTS ops_control.watchdog_observation (
    subject_key          text PRIMARY KEY,
    watchdog_name        text        NOT NULL,
    verdict              text        NOT NULL,
    detail               jsonb       NOT NULL DEFAULT '{}'::jsonb,
    open_incident_fingerprints jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- Eligibility epoch. `first_observed_at` is when the watchdog first saw the
    -- subject at all, which is NOT when it became eligible: a schedule observed
    -- while its client was disabled would, on re-enable, retroactively "expect"
    -- fires that happened during the disabled period. `client_account` carries no
    -- timestamps, so the transition is only knowable by remembering the previous
    -- eligibility here. `eligible_since` advances only on a false->true edge and
    -- is cleared when the subject becomes ineligible, so repeated scans never
    -- push it forward and every disabled interval starts a fresh epoch.
    eligible             boolean,
    eligible_since       timestamptz,
    first_observed_at    timestamptz NOT NULL DEFAULT now(),
    last_observed_at     timestamptz NOT NULL DEFAULT now(),
    last_alerted_at      timestamptz,
    observation_count    bigint      NOT NULL DEFAULT 1,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_watchdog_observation_verdict
        CHECK (verdict IN (
            'OK',
            'EXPECTED_FAILED',
            'MISSING',
            'STALE',
            'DISABLED',
            'NOT_YET_EXPECTED',
            'IN_WINDOW',
            'HEARTBEAT_LOST',
            'THRESHOLD_WARNING',
            'THRESHOLD_CRITICAL'
        )),
    CONSTRAINT ck_watchdog_observation_fingerprints
        CHECK (jsonb_typeof(open_incident_fingerprints) = 'array'),
    CONSTRAINT ck_watchdog_observation_count
        CHECK (observation_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_watchdog_observation_open
    ON ops_control.watchdog_observation (watchdog_name, verdict)
    WHERE verdict NOT IN ('OK', 'DISABLED', 'IN_WINDOW');

COMMENT ON TABLE ops_control.watchdog_observation IS
    'Latest verdict per watchdog subject; makes repeated scans idempotent and recovery detectable.';

COMMIT;
