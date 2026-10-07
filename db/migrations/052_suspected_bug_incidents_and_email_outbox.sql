-- Platform-level suspected_bug incident grouping and transactional email outbox.
--
-- suspected_bug is an ERROR *classification* carried in logs.context.classification.
-- It is not a run status and never changes runs.status.
--
-- The durable ERROR log row stays in `logs`; this migration adds the logical
-- incident (deduplicated by fingerprint), the per-detection occurrence history,
-- and the email outbox consumed by ops/suspected_bug_email_worker.py.
--
-- All references to logs/runs are ON DELETE SET NULL so that
-- POST /maintenance/prune keeps working without deleting incident history.

CREATE TABLE IF NOT EXISTS suspected_bug_incidents (
  incident_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  fingerprint TEXT NOT NULL,
  classification TEXT NOT NULL DEFAULT 'suspected_bug',
  incident_code TEXT NOT NULL,
  title TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'error',
  state TEXT NOT NULL DEFAULT 'open',
  environment TEXT,
  component TEXT,
  client_id UUID,
  client_code TEXT,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  occurrence_count BIGINT NOT NULL DEFAULT 0,
  latest_log_id BIGINT REFERENCES logs(id) ON DELETE SET NULL,
  latest_run_id UUID REFERENCES runs(run_id) ON DELETE SET NULL,
  latest_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  material_signature TEXT,
  last_email_enqueued_at TIMESTAMPTZ,
  last_email_sent_at TIMESTAMPTZ,
  resolved_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT suspected_bug_incidents_fingerprint_key UNIQUE (fingerprint),
  CONSTRAINT suspected_bug_incidents_classification_check
    CHECK (classification = 'suspected_bug'),
  CONSTRAINT suspected_bug_incidents_state_check
    CHECK (state IN ('open', 'resolved')),
  CONSTRAINT suspected_bug_incidents_severity_check
    CHECK (severity IN ('warning', 'error', 'critical')),
  CONSTRAINT suspected_bug_incidents_occurrence_count_check
    CHECK (occurrence_count >= 0),
  CONSTRAINT suspected_bug_incidents_payload_check
    CHECK (jsonb_typeof(latest_payload) = 'object'),
  CONSTRAINT suspected_bug_incidents_fingerprint_not_blank
    CHECK (btrim(fingerprint) <> ''),
  CONSTRAINT suspected_bug_incidents_resolved_at_check
    CHECK ((state = 'resolved') = (resolved_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_suspected_bug_incidents_open_last_seen
  ON suspected_bug_incidents (last_seen_at DESC, incident_id)
  WHERE state = 'open';

CREATE INDEX IF NOT EXISTS idx_suspected_bug_incidents_code_last_seen
  ON suspected_bug_incidents (incident_code, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_suspected_bug_incidents_client_last_seen
  ON suspected_bug_incidents (client_code, last_seen_at DESC)
  WHERE client_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_suspected_bug_incidents_first_seen
  ON suspected_bug_incidents (first_seen_at DESC);

COMMENT ON TABLE suspected_bug_incidents IS
  'Logical suspected_bug incidents grouped by deterministic fingerprint. One row per logical cause, not per affected record.';
COMMENT ON COLUMN suspected_bug_incidents.fingerprint IS
  'sha256 over canonical identity JSON (environment, component, incident code, client, report type, database/schema/table, subject, invariant identity). Excludes run/raw-file/artifact ids, timestamps and counts.';
COMMENT ON COLUMN suspected_bug_incidents.material_signature IS
  'Hash of material evidence used to re-alert inside one fingerprint (state, target table, bounded scope bucket).';

CREATE TABLE IF NOT EXISTS suspected_bug_occurrences (
  occurrence_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id UUID NOT NULL REFERENCES suspected_bug_incidents(incident_id) ON DELETE CASCADE,
  occurrence_no BIGINT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  log_id BIGINT REFERENCES logs(id) ON DELETE SET NULL,
  run_id UUID REFERENCES runs(run_id) ON DELETE SET NULL,
  component TEXT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  email_decision TEXT NOT NULL,
  email_decision_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT suspected_bug_occurrences_no_check CHECK (occurrence_no >= 1),
  CONSTRAINT suspected_bug_occurrences_payload_check
    CHECK (jsonb_typeof(payload) = 'object'),
  CONSTRAINT suspected_bug_occurrences_decision_check
    CHECK (email_decision IN ('enqueued', 'suppressed'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_suspected_bug_occurrences_log
  ON suspected_bug_occurrences (log_id)
  WHERE log_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_suspected_bug_occurrences_incident
  ON suspected_bug_occurrences (incident_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_suspected_bug_occurrences_occurred_at
  ON suspected_bug_occurrences (occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_suspected_bug_occurrences_run
  ON suspected_bug_occurrences (run_id)
  WHERE run_id IS NOT NULL;

COMMENT ON TABLE suspected_bug_occurrences IS
  'Every suspected_bug detection, linked to its durable ERROR log row. Occurrence history is preserved even when the alert email is deduplicated.';

CREATE TABLE IF NOT EXISTS suspected_bug_email_outbox (
  outbox_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id UUID NOT NULL REFERENCES suspected_bug_incidents(incident_id) ON DELETE CASCADE,
  occurrence_id UUID REFERENCES suspected_bug_occurrences(occurrence_id) ON DELETE SET NULL,
  notification_key TEXT NOT NULL,
  notification_reason TEXT NOT NULL,
  recipient_config_ref TEXT NOT NULL,
  recipient_fingerprint TEXT NOT NULL,
  recipients JSONB NOT NULL,
  subject TEXT NOT NULL,
  body_text TEXT NOT NULL,
  body_html TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL,
  available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_at TIMESTAMPTZ,
  claim_token UUID,
  lease_expires_at TIMESTAMPTZ,
  sent_at TIMESTAMPTZ,
  last_error TEXT,
  provider_message_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT suspected_bug_email_outbox_notification_key_key UNIQUE (notification_key),
  CONSTRAINT suspected_bug_email_outbox_status_check
    CHECK (status IN ('pending', 'sending', 'sent', 'retry', 'dead_letter', 'suppressed')),
  CONSTRAINT suspected_bug_email_outbox_reason_check
    CHECK (notification_reason IN ('new', 'material_change', 'reminder')),
  CONSTRAINT suspected_bug_email_outbox_attempts_check
    CHECK (attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts),
  CONSTRAINT suspected_bug_email_outbox_recipients_check
    CHECK (jsonb_typeof(recipients) = 'array' AND jsonb_array_length(recipients) >= 1),
  CONSTRAINT suspected_bug_email_outbox_sent_check
    CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
  CONSTRAINT suspected_bug_email_outbox_claim_check
    CHECK ((status = 'sending') = (claim_token IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_suspected_bug_email_outbox_due
  ON suspected_bug_email_outbox (available_at ASC, outbox_id ASC)
  WHERE status IN ('pending', 'retry');

CREATE INDEX IF NOT EXISTS idx_suspected_bug_email_outbox_lease
  ON suspected_bug_email_outbox (lease_expires_at)
  WHERE status = 'sending';

CREATE INDEX IF NOT EXISTS idx_suspected_bug_email_outbox_incident
  ON suspected_bug_email_outbox (incident_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_suspected_bug_email_outbox_status_created
  ON suspected_bug_email_outbox (status, created_at DESC);

COMMENT ON TABLE suspected_bug_email_outbox IS
  'Transactional outbox for suspected_bug alert emails. Enqueued in the same transaction as the log/incident/occurrence; delivered asynchronously by ops/suspected_bug_email_worker.py.';
COMMENT ON COLUMN suspected_bug_email_outbox.notification_key IS
  'Idempotent enqueue key: fingerprint + reason + reason bucket. Concurrent detections collapse to one row.';
COMMENT ON COLUMN suspected_bug_email_outbox.recipient_config_ref IS
  'Name of the configuration entry that produced the recipient list (for example SUSPECTED_BUG_ALERT_TO). Never an SMTP credential.';
