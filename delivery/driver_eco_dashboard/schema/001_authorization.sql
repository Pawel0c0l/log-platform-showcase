-- Driver Eco Dashboard V1 — Cloudflare D1 authorization schema.
--
-- This database holds ONLY authorization state. It contains no Eco score, no
-- driver name, no e-mail address and no client identifier: `subject_ref` is an
-- opaque publisher-chosen handle and `snapshot_object_key` is an opaque R2 key.
-- Neither is ever sent to a browser.
--
-- Raw capabilities are never stored. `capability_digest` is SHA-256 of the raw
-- value, or HMAC-SHA-256 under the CAPABILITY_PEPPER secret when one is bound.

CREATE TABLE IF NOT EXISTS eco_capability (
  capability_id        TEXT PRIMARY KEY,
  capability_digest    TEXT NOT NULL UNIQUE,
  subject_ref          TEXT NOT NULL,
  snapshot_object_key  TEXT NOT NULL,
  issued_at            INTEGER NOT NULL,
  expires_at           INTEGER NOT NULL,
  revoked_at           INTEGER NULL,
  rotated_to           TEXT NULL,
  -- Bumping the epoch invalidates every session derived from this grant
  -- without withdrawing the grant itself.
  session_epoch        INTEGER NOT NULL DEFAULT 0,
  CONSTRAINT chk_eco_capability_validity CHECK (expires_at > issued_at)
);

CREATE INDEX IF NOT EXISTS idx_eco_capability_subject ON eco_capability (subject_ref);
CREATE INDEX IF NOT EXISTS idx_eco_capability_expiry  ON eco_capability (expires_at);

CREATE TABLE IF NOT EXISTS eco_session (
  session_digest  TEXT PRIMARY KEY,
  capability_id   TEXT NOT NULL,
  created_at      INTEGER NOT NULL,
  expires_at      INTEGER NOT NULL,
  -- Copied from the grant at issue time; a mismatch means the grant has since
  -- invalidated its sessions.
  epoch           INTEGER NOT NULL,
  FOREIGN KEY (capability_id) REFERENCES eco_capability (capability_id)
);

CREATE INDEX IF NOT EXISTS idx_eco_session_capability ON eco_session (capability_id);
CREATE INDEX IF NOT EXISTS idx_eco_session_expiry     ON eco_session (expires_at);

-- LIFECYCLE OF THE TWO TABLES ABOVE. Expiry is ENFORCED, not erased.
--
-- Capability lifetimes are period-scoped (worker/lib/capability_ttl.js): a
-- weekly grant lives 10 days, a monthly grant 60. Publishing a NEWER period
-- never revokes an older still-valid grant — each one is pinned to its own
-- immutable snapshot object and keeps answering with that period's report
-- until its own expiry, so consecutive reports deliberately overlap.
--
-- eco_capability rows SURVIVE expiry, on purpose. They hold no secret (the raw
-- bearer was returned once to the publisher and never stored; only a digest is
-- here), they already stop authorising anything the moment `expires_at` passes
-- (`classifyCapability`), and they are what lets an expired link answer
-- `410 LINK_EXPIRED` instead of being indistinguishable from a link that never
-- existed. Deleting them at expiry would trade a real operator and driver
-- answer for a row count. The secret-bearing copy of a capability is the HOST's,
-- and the host destroys it once the grant expires
-- (`eco_dashboard_delivery_operation`, migration 051).
--
-- SURVIVING EXPIRY IS NOT SURVIVING FOREVER. The tombstone ends at the global
-- hard-retention ceiling: 13 CALENDAR MONTHS after the grant was ISSUED
-- (`worker/lib/retention_policy.js`, mirroring `ops/retention_registry.py`).
-- `POST /api/publish/maintenance` removes it then, together with its
-- publication ledger row, and never while it is live, referenced by a session,
-- or referenced by an operation. The anchor is `issued_at` and not
-- `expires_at` deliberately: the ceiling limits how long a record may be
-- STORED, and anchoring on expiry would keep a monthly grant for thirteen
-- months plus its own sixty-day TTL.
--
-- eco_session rows are compacted once expired, by the publisher-authenticated
-- `POST /api/publish/maintenance`. They are the only table here that grows with
-- driver traffic rather than with publications, and an expired session
-- authorises nothing.
--
-- Snapshot RETENTION IN R2 IS A SEPARATE CONCERN and is not coupled to bearer
-- expiry: an expired capability never deletes the historical report it pointed
-- at. It is a separate concern with its own horizon, not an absent one — the
-- same 13-calendar-month ceiling applies to the object, measured from its own
-- R2 `uploaded` stamp, and the same maintenance call enforces it. The report
-- outlives the link by months; it does not outlive the platform.

-- Publication operations — the authoritative coordination primitive.
--
-- One row per logical publication of one driver's snapshot. It is not a
-- progress log: it is the lock. Every security-relevant transition below is a
-- compare-and-set on THIS row, and the grant insert that accompanies a
-- transition runs in the same D1 batch (= one transaction) as the transition
-- itself. There is no path that inserts a capability and then, separately,
-- records it here.
--
-- STATE MACHINE
--
--   CREATED ─▶ SNAPSHOT_WRITTEN ─▶ GRANT_MINTED ─▶ DELIVERY_INTENT_RECORDED ─▶ DELIVERED
--
--   CREATED                   the operation exists AND owns exactly one
--                             server-minted R2 object key. The key is chosen
--                             before this row is inserted and never changes,
--                             so concurrent publishers cannot accumulate
--                             objects: whoever loses the insert discards its
--                             candidate key without ever touching R2.
--   SNAPSHOT_WRITTEN          the owned object has been written to R2.
--   GRANT_MINTED              a capability exists AND this row references it.
--                             Both facts commit together or not at all.
--   DELIVERY_INTENT_RECORDED  the host durably queued its own send intent.
--   DELIVERED                 terminal. The bearer may already be in the
--                             driver's mailbox, so recovery is refused from
--                             here by the transaction's own predicate.
--
-- It holds NO raw bearer. A raw capability is returned to the initiating
-- process once and is unrecoverable afterwards by design; `bearer_generation`
-- counts how many bearers have been emitted for this operation (1 after the
-- initial mint, +1 per recovery), so recovery is auditable without ever
-- storing the value.
--
-- It also holds no Eco data, no driver name and no e-mail address.

CREATE TABLE IF NOT EXISTS eco_publication_operation (
  operation_id         TEXT PRIMARY KEY,
  subject_ref          TEXT NOT NULL,
  payload_digest       TEXT NOT NULL,
  -- Owned from creation and immutable. NOT NULL is the schema-level half of
  -- the one-operation/one-object invariant.
  snapshot_object_key  TEXT NOT NULL,
  capability_id        TEXT NULL,
  bearer_generation    INTEGER NOT NULL DEFAULT 0,
  state                TEXT NOT NULL,
  created_at           INTEGER NOT NULL,
  updated_at           INTEGER NOT NULL,
  CONSTRAINT chk_eco_publication_state CHECK (state IN (
    'CREATED', 'SNAPSHOT_WRITTEN', 'GRANT_MINTED',
    'DELIVERY_INTENT_RECORDED', 'DELIVERED'
  )),
  -- A grant-authoritative state cannot exist without this row naming the
  -- grant, and a pre-grant state cannot name one. This is the invariant the
  -- review found violated: it is now impossible to represent, not merely
  -- avoided by the order of two writes.
  CONSTRAINT chk_eco_publication_grant_ledger CHECK (
    (state IN ('CREATED', 'SNAPSHOT_WRITTEN') AND capability_id IS NULL)
    OR
    (state IN ('GRANT_MINTED', 'DELIVERY_INTENT_RECORDED', 'DELIVERED')
     AND capability_id IS NOT NULL)
  ),
  CONSTRAINT chk_eco_publication_generation CHECK (
    (capability_id IS NULL AND bearer_generation = 0)
    OR
    (capability_id IS NOT NULL AND bearer_generation >= 1)
  )
);

-- One object key belongs to at most one operation, and one grant is the
-- current authoritative grant of at most one operation. Both are enforced by
-- the database, not by process-local reasoning.
CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_publication_object_key
  ON eco_publication_operation (snapshot_object_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_publication_capability
  ON eco_publication_operation (capability_id) WHERE capability_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_eco_publication_subject ON eco_publication_operation (subject_ref);
CREATE INDEX IF NOT EXISTS idx_eco_publication_state   ON eco_publication_operation (state);
