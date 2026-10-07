#!/usr/bin/env python3
"""Driver Eco Dashboard V1 — delivery ledger SCHEMA suite (real PostgreSQL 16).

Run:
    ECO_DASHBOARD_SCHEMA_TEST_DSN=postgresql://postgres:pw@127.0.0.1:5433/disposable \\
      python3 ops/tests_manual/test_driver_eco_dashboard_delivery_schema_postgres.py

WHAT THIS PROVES, AND WHY EACH PART IS SEPARATE FROM THE LIFECYCLE SUITE

The lifecycle suite drives the publisher and therefore only ever produces rows
the publisher would produce. This one attacks the SCHEMA directly, as the
runtime role, with rows the application would never write — which is the only
way to establish what the database itself refuses:

1. **Runtime-role privileges.** Migration 049 is applied by the schema owner,
   and the publisher runs as the per-client runtime role. A table only the
   owner can use is a table the job cannot use. Every lifecycle DML statement
   is therefore replayed while connected AS THE RUNTIME ROLE, and the
   privileges deliberately withheld are proved withheld.

2. **The bearer cannot reach a diagnostic column.** Including the case a CHECK
   constraint structurally cannot see: one UPDATE that copies the bearer into
   `failure_detail` while clearing `capability_secret`, so the resulting row no
   longer contains the value to compare against.

3. **Incomplete lifecycle states are not representable.** For every durable
   state the code can persist, the row must carry enough to determine exactly
   one safe next action. Valid fixtures are asserted ACCEPTED — over-constraining
   a legitimate crash-recovery state would be its own defect — and incomplete
   permutations are asserted REJECTED.

4. **Both rollout paths carry 049.** New-client onboarding and the existing-client
   migration runner, checked against the repository files themselves.

DESTRUCTIVE. It creates and drops its own schema objects in the database the
DSN names, so it refuses any DSN that is not loopback. No production database,
no persistent migration, no e-mail, no provider and no Cloudflare resource is
involved anywhere in this file.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving_dashboard.delivery_contract import (  # noqa: E402
    ALL_STATES,
    derive_message_fingerprint,
    derive_provider_backend_id,
)
from ops.release_schema_preflight import (  # noqa: E402
    parse_requirements,
    requirement_defects,
)
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

ENV = "ECO_DASHBOARD_SCHEMA_TEST_DSN"
#: THE CHAIN, NOT ONE FILE. 049 is applied shared history on every existing
#: client, so the reviewed external-mailer ownership contract arrives as the
#: forward migration 050 and the schema under test is what BOTH files install.
MIGRATION_049 = "049_eco_dashboard_delivery_operation.sql"
MIGRATION_050 = "050_eco_dashboard_external_mailer_ownership.sql"
#: Expired-capability retirement. Forward again, for the same reason 050 was:
#: 049 and 050 are applied history everywhere, so the `CAPABILITY_RETIRED`
#: state and its constraints cannot arrive as an edit to either.
MIGRATION_051 = "051_eco_dashboard_capability_retirement.sql"
ECO_DASHBOARD_MIGRATIONS = (MIGRATION_049, MIGRATION_050, MIGRATION_051)
MIGRATIONS = tuple(REPO_ROOT / "db" / "client_business" / name
                   for name in ECO_DASHBOARD_MIGRATIONS)
MIGRATION = MIGRATIONS[0]
#: The requirement the release gate keys on: 051, because that is the migration
#: whose presence makes the CURRENT reviewed contract true. A client carrying
#: only 049 or only 049 + 050 is refused on the ledger check, before any
#: physical inspection.
REQUIREMENT_MIGRATION = MIGRATION_051
TABLE = "public.eco_dashboard_delivery_operation"

#: The role the publisher actually connects as. Created by this suite, dropped
#: by it, and never granted ownership of anything.
RUNTIME_ROLE = "eco_dash_runtime_probe"
RUNTIME_PASSWORD = "disposable-probe"

PASSED: list[str] = []

#: A synthetic 43-character bearer. Never a real capability: no grant is minted
#: with it and it exists only inside this process and the disposable database.
BEARER = "B" * 43
BEARER_DIGEST = "b" * 64
CAPABILITY_ID = "c" * 32
BACKEND_ID = derive_provider_backend_id("fake", "probe-account", "probe-endpoint")
FINGERPRINT = derive_message_fingerprint(
    recipient_email="driver@example.invalid", subject="s", message_id="<m@x.invalid>",
    html_body="<p>h</p>", text_body="t")


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}" if detail else name)


# --- connections ----------------------------------------------------------------


def connect(dsn: str, **kwargs):
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn, row_factory=dict_row, **kwargs)
    conn.autocommit = True
    return conn


def runtime_dsn(dsn: str) -> str:
    """The same database, reached as the RUNTIME role rather than the owner."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(dsn)
    host = parts.hostname or "127.0.0.1"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit((parts.scheme,
                       f"{RUNTIME_ROLE}:{RUNTIME_PASSWORD}@{host}{port}",
                       parts.path, "", ""))


def build_schema(owner) -> None:
    """A minimal client business database, then migration 049 exactly as written.

    `public.client_trips` is created first and granted to the runtime role
    because that is what 049's grant block reads: the repository convention is
    that a client-business migration mirrors "the DML roles of this database"
    from `client_trips` rather than hard-coding a role name it cannot know
    (`db/client_business/033_eco_driving_weekly_email_send_log_grants.sql`).
    """
    with owner.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.client_trips CASCADE")
        cur.execute(
            f"""DO $$
                BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
                    CREATE ROLE {RUNTIME_ROLE} LOGIN PASSWORD '{RUNTIME_PASSWORD}';
                  END IF;
                END $$""")
        cur.execute("CREATE TABLE public.client_trips (id BIGSERIAL PRIMARY KEY)")
        cur.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE public.client_trips "
                    f"TO {RUNTIME_ROLE}")
        cur.execute(f"GRANT CONNECT ON DATABASE {owner.info.dbname} TO {RUNTIME_ROLE}")
        for migration in MIGRATIONS:
            cur.execute(migration.read_text(encoding="utf-8"))


def drop_schema(owner) -> None:
    """Remove ONLY what this suite created."""
    with owner.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS "
                    "public.eco_dashboard_delivery_operation_guard() CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.client_trips CASCADE")
        cur.execute(f"REVOKE ALL ON DATABASE {owner.info.dbname} FROM {RUNTIME_ROLE}")
        # 049 grants USAGE ON SCHEMA public to every mirrored DML role, so the
        # role still depends on that grant until it is revoked.
        cur.execute(f"REVOKE ALL ON SCHEMA public FROM {RUNTIME_ROLE}")
        cur.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {RUNTIME_ROLE}")
        cur.execute(f"DROP ROLE IF EXISTS {RUNTIME_ROLE}")


# --- row fixtures ---------------------------------------------------------------


def _operation_id(seed: str) -> str:
    import hashlib
    import base64

    raw = hashlib.sha256(seed.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def base_row(seed: str) -> dict:
    return {
        "operation_id": _operation_id(seed),
        "client_id": str(uuid.uuid4()),
        "identity_key": f"DRIVER-{seed}",
        "period_type": "weekly",
        "period_start_date": "2026-07-01",
        "period_end_date": "2026-07-08",
        "send_scope": "normal",
        "subject_ref": f"subject-{seed}",
        "payload_digest": "a" * 64,
        "recipient_identity": f"rcpt_{seed}",
        "recipient_email": "driver@example.invalid",
        "state": "PREPARED",
        "bearer_generation": 0,
    }


def with_bearer(row: dict) -> dict:
    row.update({
        "capability_id": CAPABILITY_ID,
        "capability_secret": BEARER,
        "capability_digest": BEARER_DIGEST,
        "capability_expires_at": "2026-12-01T00:00:00+00:00",
        "bearer_generation": 1,
        "bearer_persisted_at": "2026-07-09T00:00:00+00:00",
    })
    return row


def with_binding(row: dict) -> dict:
    row.update({
        "provider_name": "fake",
        "provider_idempotency_key": "eco-dash-" + _operation_id(row["operation_id"])[:40],
        "provider_backend_id": BACKEND_ID,
        "provider_message_fingerprint": FINGERPRINT,
        "provider_bound_capability_id": CAPABILITY_ID,
        "provider_bound_bearer_generation": 1,
    })
    return row


def valid_fixture(state: str, seed: str) -> dict:
    """The row the publisher would actually persist for `state`."""
    row = base_row(seed)
    row["state"] = state
    if state == "PREPARED":
        return row
    if state == "BEARER_RECOVERY_REQUIRED":
        row.update({"capability_id": CAPABILITY_ID, "capability_digest": BEARER_DIGEST,
                    "bearer_generation": 1,
                    "bearer_persisted_at": "2026-07-09T00:00:00+00:00",
                    "bearer_cleared_at": "2026-07-09T00:01:00+00:00",
                    "failure_phase": "PUBLICATION",
                    "failure_code": "BEARER_NOT_PERSISTED"})
        return row
    if state == "CAPABILITY_RETIRED":
        # The grant's validity window passed and the sweep destroyed its raw
        # bearer. Migration 051 makes a retired row holding one unrepresentable
        # (`chk_..._bearer_absent`) and makes it unable to forget WHICH grant it
        # held (`chk_..._retired_audit`), so the audit identity is present and
        # the secret is not. Nothing here claims a delivery: no `finalized_at`,
        # no acceptance, no remote delivery.
        row.update({"capability_id": CAPABILITY_ID, "capability_digest": BEARER_DIGEST,
                    "capability_expires_at": "2026-07-19T00:00:00+00:00",
                    "bearer_generation": 1,
                    "bearer_persisted_at": "2026-07-09T00:00:00+00:00",
                    "bearer_cleared_at": "2026-07-19T00:05:00+00:00",
                    "external_mailer": "eco_driving_weekly_email_notifications",
                    "metadata_json": (
                        '{"capability_retirement": '
                        '{"reason": "CAPABILITY_EXPIRED"}}')})
        return row
    with_bearer(row)
    if state == "CAPABILITY_PERSISTED":
        return row
    if state == "EXTERNAL_MAILER_HANDOFF":
        # The publication-only branch: a capability the EXISTING Eco mailing
        # lifecycle took over. It retains the bearer (a rerun must hand over the
        # same link) and it is provider-unbound permanently.
        #
        # `external_mailer` is what makes that true, and it is set here because
        # the publisher sets it in the INSERT that CREATES the row, not when the
        # handoff is recorded: `chk_..._handoff_owned` refuses a handoff state on
        # a row that never declared an owner, precisely so ownership cannot be a
        # late annotation with an adoptable window in front of it.
        row["external_mailer"] = "eco_driving_weekly_email_notifications"
        row["metadata_json"] = (
            '{"external_mailer_handoff": {"mailer": '
            '"eco_driving_weekly_email_notifications"}}')
        return row
    with_binding(row)
    if state == "DELIVERY_INTENT_RECORDED":
        return row
    row.update({"provider_attempts": 1,
                "provider_submitted_at": "2026-07-09T00:02:00+00:00"})
    if state == "PROVIDER_SUBMISSION_PENDING":
        return row
    if state == "PROVIDER_AMBIGUOUS":
        row.update({"failure_phase": "PROVIDER", "failure_code": "PROVIDER_UNSURE",
                    "operator_action_required": True})
        return row
    if state == "PROVIDER_REJECTED":
        row.update({"failure_phase": "PROVIDER", "failure_code": "RECIPIENT_REFUSED"})
        return row
    row.update({"provider_message_id": "fake-msg-0001",
                "provider_accepted_at": "2026-07-09T00:03:00+00:00"})
    if state == "PROVIDER_ACCEPTED":
        return row
    row["remote_delivered_at"] = "2026-07-09T00:04:00+00:00"
    if state == "REMOTE_DELIVERED":
        return row
    if state == "FINALIZED":
        row.update({"capability_secret": None,
                    "bearer_cleared_at": "2026-07-09T00:05:00+00:00",
                    "finalized_at": "2026-07-09T00:05:00+00:00"})
        return row
    if state == "OPERATOR_REQUIRED":
        row.update({"failure_phase": "PROVIDER", "failure_code": "OPERATOR",
                    "operator_action_required": True})
        return row
    raise AssertionError(f"no fixture for {state}")


def insert_row(conn, row: dict):
    columns = [k for k, v in row.items() if v is not None]
    placeholders = ", ".join(["%s"] * len(columns))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"RETURNING delivery_id",
            tuple(row[c] for c in columns))
        return cur.fetchone()["delivery_id"]


def rejects(conn, action, *, label: str) -> str:
    """Run `action(cursor)` and require the database to refuse it."""
    try:
        with conn.cursor() as cur:
            action(cur)
    except Exception as error:  # noqa: BLE001 - the refusal itself is the assertion
        return str(error)
    raise AssertionError(f"the database ACCEPTED an invalid write: {label}")


# --- 1. runtime-role privileges -------------------------------------------------


def test_the_runtime_role_can_run_the_whole_lifecycle(ctx) -> None:
    """Every lifecycle statement, as the role the job actually connects as."""
    runtime = connect(runtime_dsn(ctx["dsn"]))
    try:
        row = base_row("runtime-lifecycle")
        delivery_id = insert_row(runtime, row)
        check("the runtime role may INSERT the operation", delivery_id is not None)

        with runtime.cursor() as cur:
            cur.execute(f"SELECT state FROM {TABLE} WHERE delivery_id = %s",
                        (str(delivery_id),))
            check("the runtime role may SELECT", cur.fetchone()["state"] == "PREPARED")

            # claim -> capability -> intent -> pending -> accepted -> delivered
            # -> finalize, i.e. every UPDATE shape the ledger issues.
            cur.execute(f"UPDATE {TABLE} SET lease_owner = %s, "
                        f"lease_expires_at = now() + interval '5 min', "
                        f"attempt_count = attempt_count + 1 WHERE delivery_id = %s",
                        ("runtime-owner", str(delivery_id)))
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'CAPABILITY_PERSISTED',
                        capability_id = %s, capability_secret = %s,
                        capability_digest = %s, capability_expires_at = now(),
                        bearer_generation = 1, bearer_persisted_at = now()
                      WHERE delivery_id = %s AND lease_owner = %s
                        AND lease_expires_at > now()""",
                (CAPABILITY_ID, BEARER, BEARER_DIGEST, str(delivery_id), "runtime-owner"))
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'DELIVERY_INTENT_RECORDED',
                        provider_name = 'fake', provider_idempotency_key = %s,
                        provider_backend_id = %s, provider_message_fingerprint = %s,
                        provider_bound_capability_id = %s,
                        provider_bound_bearer_generation = 1
                      WHERE delivery_id = %s""",
                ("eco-dash-runtimeprobe", BACKEND_ID, FINGERPRINT, CAPABILITY_ID,
                 str(delivery_id)))
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'PROVIDER_SUBMISSION_PENDING',
                        provider_attempts = provider_attempts + 1,
                        provider_submitted_at = now() WHERE delivery_id = %s""",
                (str(delivery_id),))
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'PROVIDER_ACCEPTED',
                        provider_message_id = 'fake-msg-0001',
                        provider_accepted_at = now(),
                        metadata_json = jsonb_set(metadata_json, '{{acceptance_source}}',
                                                  %s::jsonb)
                      WHERE delivery_id = %s""",
                (json.dumps("DIRECT"), str(delivery_id)))
            cur.execute(f"UPDATE {TABLE} SET state = 'REMOTE_DELIVERED', "
                        f"remote_delivered_at = now() WHERE delivery_id = %s",
                        (str(delivery_id),))
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'FINALIZED', capability_secret = NULL,
                        bearer_cleared_at = now(), finalized_at = now(),
                        lease_owner = NULL, lease_expires_at = NULL
                      WHERE delivery_id = %s""",
                (str(delivery_id),))
            cur.execute(f"SELECT state, capability_secret FROM {TABLE} "
                        f"WHERE delivery_id = %s", (str(delivery_id),))
            final = cur.fetchone()
        check("the runtime role drove the lifecycle to FINALIZED",
              final["state"] == "FINALIZED", final["state"])
        check("the bearer was cleared by the runtime role",
              final["capability_secret"] is None)
    finally:
        runtime.close()
    PASSED.append("the_runtime_role_can_run_the_whole_lifecycle")


def test_the_runtime_role_holds_no_privilege_it_does_not_need(ctx) -> None:
    """Minimum privileges, asserted as absences rather than assumed."""
    runtime = connect(runtime_dsn(ctx["dsn"]))
    try:
        with runtime.cursor() as cur:
            cur.execute(
                """SELECT privilege_type FROM information_schema.role_table_grants
                    WHERE table_schema = 'public'
                      AND table_name = 'eco_dashboard_delivery_operation'
                      AND grantee = %s""", (RUNTIME_ROLE,))
            granted = {r["privilege_type"] for r in cur.fetchall()}
        check("SELECT, INSERT and UPDATE are granted",
              {"SELECT", "INSERT", "UPDATE"} <= granted, str(sorted(granted)))
        check("nothing beyond them is granted",
              granted <= {"SELECT", "INSERT", "UPDATE"}, str(sorted(granted)))

        row = base_row("runtime-privileges")
        delivery_id = insert_row(runtime, row)
        rejects(runtime,
                lambda cur: cur.execute(f"DELETE FROM {TABLE} WHERE delivery_id = %s",
                                        (str(delivery_id),)),
                label="DELETE as the runtime role")
        rejects(runtime, lambda cur: cur.execute(f"TRUNCATE {TABLE}"),
                label="TRUNCATE as the runtime role")
        rejects(runtime,
                lambda cur: cur.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT "
                                        f"chk_eco_dashboard_delivery_operation_state"),
                label="dropping a constraint as the runtime role")
        rejects(runtime,
                lambda cur: cur.execute(
                    "SELECT public.eco_dashboard_delivery_operation_guard()"),
                label="calling the guard function directly as the runtime role")
    finally:
        runtime.close()
    PASSED.append("the_runtime_role_holds_no_privilege_it_does_not_need")


# --- 2. the bearer may not reach a diagnostic column ----------------------------


def test_the_bearer_cannot_be_copied_into_diagnostics(ctx) -> None:
    """Every generic diagnostic field, including the copy-and-clear case.

    Copy-and-clear is the one a CHECK constraint cannot answer: the statement
    removes the value it would have been compared against. Only the OLD row
    knows, which is why the guard is a trigger.
    """
    runtime = connect(runtime_dsn(ctx["dsn"]))
    try:
        row = with_bearer(base_row("bearer-diagnostics"))
        row["state"] = "CAPABILITY_PERSISTED"
        delivery_id = insert_row(runtime, row)
        rid = str(delivery_id)

        # (a0) THE reproduced case, in the state where clearing the bearer is
        #      LEGAL. Finalisation is exactly when the bearer is destroyed, so
        #      it is the one moment a copy-and-clear would otherwise leave a
        #      row that no NEW-row check could object to.
        legal_clear = with_binding(with_bearer(base_row("bearer-at-cleanup")))
        legal_clear.update({
            "state": "REMOTE_DELIVERED",
            "provider_attempts": 1,
            "provider_submitted_at": "2026-07-09T00:02:00+00:00",
            "provider_message_id": "fake-msg-0003",
            "provider_accepted_at": "2026-07-09T00:03:00+00:00",
            "remote_delivered_at": "2026-07-09T00:04:00+00:00",
        })
        cleanup_id = str(insert_row(runtime, legal_clear))
        error = rejects(
            runtime,
            lambda cur: cur.execute(
                f"""UPDATE {TABLE} SET state = 'FINALIZED',
                        failure_detail = capability_secret,
                        capability_secret = NULL, bearer_cleared_at = now(),
                        finalized_at = now() WHERE delivery_id = %s""", (cleanup_id,)),
            label="copy-and-clear during finalisation")
        check("copy-and-clear at cleanup is refused as a bearer leak",
              "ECO_DASHBOARD_BEARER_IN_DIAGNOSTICS" in error, error[:200])
        with runtime.cursor() as cur:
            cur.execute(f"SELECT state, capability_secret FROM {TABLE} "
                        f"WHERE delivery_id = %s", (cleanup_id,))
            still = cur.fetchone()
        check("the refused finalisation was not partially applied",
              still["state"] == "REMOTE_DELIVERED"
              and still["capability_secret"] == BEARER, still["state"])
        # ...and the SAME finalisation without the leak is accepted.
        with runtime.cursor() as cur:
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'FINALIZED', capability_secret = NULL,
                        bearer_cleared_at = now(), finalized_at = now()
                      WHERE delivery_id = %s""", (cleanup_id,))
            cur.execute(f"SELECT state FROM {TABLE} WHERE delivery_id = %s",
                        (cleanup_id,))
            check("a clean finalisation is still accepted",
                  cur.fetchone()["state"] == "FINALIZED")

        # (a) COPY-AND-CLEAR, per generic column.
        for column in ("failure_detail", "failure_code", "failure_phase"):
            error = rejects(
                runtime,
                lambda cur, c=column: cur.execute(
                    f"UPDATE {TABLE} SET {c} = capability_secret, "
                    f"capability_secret = NULL, bearer_cleared_at = now() "
                    f"WHERE delivery_id = %s", (rid,)),
                label=f"copy-and-clear into {column}")
            check(f"copy-and-clear into {column} is refused as a bearer leak",
                  "ECO_DASHBOARD_BEARER_IN_DIAGNOSTICS" in error, error[:160])
        rejects(runtime,
                lambda cur: cur.execute(
                    f"UPDATE {TABLE} SET metadata_json = "
                    f"jsonb_build_object('note', capability_secret), "
                    f"capability_secret = NULL, bearer_cleared_at = now() "
                    f"WHERE delivery_id = %s", (rid,)),
                label="copy-and-clear into metadata_json")

        # (b) EMBEDDED forms, with the bearer still present.
        for expression, label in (
            (f"'provider said: ' || capability_secret", "prefixed"),
            (f"capability_secret || ' <- that link'", "suffixed"),
        ):
            rejects(runtime,
                    lambda cur, e=expression: cur.execute(
                        f"UPDATE {TABLE} SET failure_detail = {e} WHERE delivery_id = %s",
                        (rid,)),
                    label=f"{label} bearer in failure_detail")
        rejects(runtime,
                lambda cur: cur.execute(
                    f"UPDATE {TABLE} SET metadata_json = jsonb_build_object("
                    f"'outer', jsonb_build_object('inner', capability_secret)) "
                    f"WHERE delivery_id = %s", (rid,)),
                label="nested bearer in metadata_json")

        # (c) The row still holds its bearer, and normal writes still work.
        with runtime.cursor() as cur:
            cur.execute(f"SELECT capability_secret FROM {TABLE} WHERE delivery_id = %s",
                        (rid,))
            check("no refused write was partially applied",
                  cur.fetchone()["capability_secret"] == BEARER)
            cur.execute(f"UPDATE {TABLE} SET failure_detail = %s WHERE delivery_id = %s",
                        ("provider said: RATE_LIMITED", rid))
            cur.execute(f"UPDATE {TABLE} SET metadata_json = %s::jsonb "
                        f"WHERE delivery_id = %s",
                        (json.dumps({"acceptance_source": "DIRECT"}), rid))

        # (d) LEGITIMATE cleanup still succeeds. The invariant must not have
        #     been bought by making finalisation impossible.
        with runtime.cursor() as cur:
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'DELIVERY_INTENT_RECORDED',
                        provider_name = 'fake', provider_idempotency_key = %s,
                        provider_backend_id = %s, provider_message_fingerprint = %s,
                        provider_bound_capability_id = %s,
                        provider_bound_bearer_generation = 1,
                        failure_detail = NULL
                      WHERE delivery_id = %s""",
                ("eco-dash-cleanupprobe", BACKEND_ID, FINGERPRINT, CAPABILITY_ID, rid))
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'PROVIDER_SUBMISSION_PENDING',
                        provider_attempts = 1, provider_submitted_at = now()
                      WHERE delivery_id = %s""", (rid,))
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'PROVIDER_ACCEPTED',
                        provider_message_id = 'fake-msg-0002',
                        provider_accepted_at = now() WHERE delivery_id = %s""", (rid,))
            cur.execute(f"UPDATE {TABLE} SET state = 'REMOTE_DELIVERED', "
                        f"remote_delivered_at = now() WHERE delivery_id = %s", (rid,))
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'FINALIZED', capability_secret = NULL,
                        bearer_cleared_at = now(), finalized_at = now()
                      WHERE delivery_id = %s""", (rid,))
            cur.execute(f"SELECT state, capability_secret, failure_detail, "
                        f"metadata_json FROM {TABLE} WHERE delivery_id = %s", (rid,))
            final = cur.fetchone()
        check("valid bearer cleanup still succeeds", final["state"] == "FINALIZED")
        check("the bearer is gone", final["capability_secret"] is None)
        check("no diagnostic retained it",
              BEARER not in json.dumps(final, default=str))
    finally:
        runtime.close()
    PASSED.append("the_bearer_cannot_be_copied_into_diagnostics")


# --- 3. state completeness ------------------------------------------------------


def test_every_valid_lifecycle_state_is_representable(ctx) -> None:
    """Over-constraining is a defect too. Each real state must still be storable."""
    runtime = connect(runtime_dsn(ctx["dsn"]))
    try:
        for state in ALL_STATES:
            insert_row(runtime, valid_fixture(state, f"valid-{state}"))
        check("every durable state has an accepted fixture", True)
    finally:
        runtime.close()
    PASSED.append("every_valid_lifecycle_state_is_representable")


def test_incomplete_lifecycle_states_are_rejected(ctx) -> None:
    """A row must always contain enough to determine ONE safe next action."""
    runtime = connect(runtime_dsn(ctx["dsn"]))

    def missing(state: str, seed: str, **drop_or_set) -> dict:
        row = valid_fixture(state, seed)
        row.update(drop_or_set)
        return {k: v for k, v in row.items() if v is not None}

    cases = [
        # -- provider intent/submission without the identity it depends on
        ("intent without a backend scope",
         missing("DELIVERY_INTENT_RECORDED", "x1", provider_backend_id=None,
                 provider_message_fingerprint=None,
                 provider_bound_capability_id=None,
                 provider_bound_bearer_generation=None)),
        ("intent without a message fingerprint",
         missing("DELIVERY_INTENT_RECORDED", "x2", provider_message_fingerprint=None)),
        ("submission pending without a backend scope",
         missing("PROVIDER_SUBMISSION_PENDING", "x3", provider_backend_id=None)),
        ("submission pending without a message fingerprint",
         missing("PROVIDER_SUBMISSION_PENDING", "x4",
                 provider_message_fingerprint=None)),
        ("acceptance without a backend scope",
         missing("PROVIDER_ACCEPTED", "x5", provider_backend_id=None)),
        ("acceptance without a provider message id",
         missing("PROVIDER_ACCEPTED", "x6", provider_message_id=None)),
        ("a bound identity without its idempotency key",
         missing("DELIVERY_INTENT_RECORDED", "x7", provider_idempotency_key=None)),
        ("a partially bound identity",
         missing("DELIVERY_INTENT_RECORDED", "x8",
                 provider_bound_capability_id=None)),
        # -- ambiguity without the material its own next action needs
        ("ambiguity without the bearer an operator must reconcile against",
         missing("PROVIDER_AMBIGUOUS", "x9", capability_secret=None,
                 bearer_cleared_at="2026-07-09T00:06:00+00:00")),
        ("ambiguity without provider identity",
         missing("PROVIDER_AMBIGUOUS", "x10", provider_backend_id=None)),
        # -- capability-dependent states without a capability
        ("capability persisted without the bearer",
         missing("CAPABILITY_PERSISTED", "x11", capability_secret=None,
                 bearer_cleared_at="2026-07-09T00:06:00+00:00")),
        ("remote delivered without a delivery instant",
         missing("REMOTE_DELIVERED", "x12", remote_delivered_at=None)),
        ("finalised while still holding the bearer",
         missing("FINALIZED", "x13", capability_secret=BEARER,
                 bearer_cleared_at=None)),
        # -- unpaired lease state
        ("a lease owner with no expiry",
         dict(base_row("x14"), lease_owner="ghost")),
        ("a lease expiry with no owner",
         dict(base_row("x15"), lease_expires_at="2026-07-09T00:00:00+00:00")),
        ("an empty lease owner",
         dict(base_row("x16"), lease_owner="  ",
              lease_expires_at="2026-07-09T00:00:00+00:00")),
        # -- malformed binding material
        ("a backend id that is not a derived scope digest",
         dict(with_binding(with_bearer(base_row("x17"))),
              state="DELIVERY_INTENT_RECORDED", provider_backend_id="smtp")),
        ("a fingerprint that is not a digest",
         dict(with_binding(with_bearer(base_row("x18"))),
              state="DELIVERY_INTENT_RECORDED",
              provider_message_fingerprint="not-a-digest")),
        ("a bound bearer generation of zero",
         dict(with_binding(with_bearer(base_row("x19"))),
              state="DELIVERY_INTENT_RECORDED", provider_bound_bearer_generation=0)),
    ]
    try:
        for label, row in cases:
            rejects(runtime, lambda cur, r=row: insert_row_on(cur, r), label=label)
    finally:
        runtime.close()
    check("every incomplete permutation was rejected", True)
    PASSED.append("incomplete_lifecycle_states_are_rejected")


def insert_row_on(cur, row: dict) -> None:
    columns = [k for k, v in row.items() if v is not None]
    placeholders = ", ".join(["%s"] * len(columns))
    cur.execute(f"INSERT INTO {TABLE} ({', '.join(columns)}) "
                f"VALUES ({placeholders})", tuple(row[c] for c in columns))


#: The provider submission identity, as ONE set. Read from the ledger's own
#: binding statement rather than restated: these are the columns
#: `record_delivery_intent()` writes together and the guard trigger then
#: freezes, so any subset of them is by definition a half-made decision.
PROVIDER_BINDING_COLUMNS = (
    "provider_name",
    "provider_idempotency_key",
    "provider_backend_id",
    "provider_message_fingerprint",
    "provider_bound_capability_id",
    "provider_bound_bearer_generation",
)

#: Every state the lifecycle passes through BEFORE it may decide a binding.
PRE_BINDING_STATES = ("PREPARED", "BEARER_RECOVERY_REQUIRED", "CAPABILITY_PERSISTED")


def test_a_partial_provider_binding_can_never_exist(ctx) -> None:
    """THE poisoned-row finding: a half-binding the lifecycle cannot continue.

    An early row carrying only `provider_idempotency_key` (or only
    `provider_name`) was accepted by the database. The guard trigger then makes
    a non-NULL binding column immutable, so the legitimate atomic bind that
    should follow is refused for the rest of the row's life. The result is a
    durable state with NO valid next action — the one property this table
    exists to make impossible.

    Two independent constraints close it, and both are exercised here:
    `chk_..._binding_coherent` (the set is all-or-none) and
    `chk_..._provider_unbound` (and it is empty until the binding step).
    """
    runtime = connect(runtime_dsn(ctx["dsn"]))
    complete = {c: with_binding(base_row("binding-values"))[c]
                for c in PROVIDER_BINDING_COLUMNS}
    try:
        # (a) EVERY proper non-empty subset, in EVERY pre-binding state.
        seed = 0
        for state in PRE_BINDING_STATES:
            for size in range(1, len(PROVIDER_BINDING_COLUMNS)):
                for combo in itertools.combinations(PROVIDER_BINDING_COLUMNS, size):
                    seed += 1
                    row = valid_fixture(state, f"partial-{seed}")
                    row.update({c: complete[c] for c in combo})
                    rejects(runtime, lambda cur, r=row: insert_row_on(cur, r),
                            label=f"{state} with only {', '.join(combo)}")
        # (b) ...and the COMPLETE binding is refused there too: an early state
        #     that already carries an immutable binding has had the decision
        #     made for it by something that was not the binding step.
        for state in PRE_BINDING_STATES:
            seed += 1
            row = valid_fixture(state, f"early-complete-{seed}")
            row.update({c: complete[c] for c in PROVIDER_BINDING_COLUMNS})
            rejects(runtime, lambda cur, r=row: insert_row_on(cur, r),
                    label=f"{state} carrying a complete early binding")
        check(f"every partial and early binding was rejected ({seed} permutations)", True)

        # (c) A partial binding cannot be reached by UPDATE either.
        rid = str(insert_row(runtime, valid_fixture("CAPABILITY_PERSISTED", "partial-upd")))
        for column in PROVIDER_BINDING_COLUMNS:
            rejects(runtime,
                    lambda cur, c=column: cur.execute(
                        f"UPDATE {TABLE} SET {c} = %s WHERE delivery_id = %s",
                        (complete[c], rid)),
                    label=f"setting {column} alone by UPDATE")

        # (d) THE LEGITIMATE ATOMIC BIND still works, from the same row.
        with runtime.cursor() as cur:
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'DELIVERY_INTENT_RECORDED',
                        provider_name = %s, provider_idempotency_key = %s,
                        provider_backend_id = %s, provider_message_fingerprint = %s,
                        provider_bound_capability_id = %s,
                        provider_bound_bearer_generation = %s
                      WHERE delivery_id = %s""",
                (*(complete[c] for c in PROVIDER_BINDING_COLUMNS), rid))
            cur.execute(f"SELECT state, provider_backend_id FROM {TABLE} "
                        f"WHERE delivery_id = %s", (rid,))
            bound = cur.fetchone()
        check("CAPABILITY_PERSISTED -> complete binding remains a normal transition",
              bound["state"] == "DELIVERY_INTENT_RECORDED"
              and bound["provider_backend_id"] == complete["provider_backend_id"],
              str(bound))

        # (e) ...and every field of it is immutable afterwards.
        for column in PROVIDER_BINDING_COLUMNS:
            error = rejects(
                runtime,
                lambda cur, c=column: cur.execute(
                    f"UPDATE {TABLE} SET {c} = NULL WHERE delivery_id = %s", (rid,)),
                label=f"clearing {column} after the bind")
            check(f"{column} cannot be cleared after binding",
                  "ECO_DASHBOARD_PROVIDER_BINDING_IMMUTABLE" in error
                  or "binding_coherent" in error
                  or "provider_key_present" in error, error[:160])
    finally:
        runtime.close()
    PASSED.append("a_partial_provider_binding_can_never_exist")


def test_a_bound_submission_identity_cannot_drift(ctx) -> None:
    """Once bound, the submission identity is immutable AT THE DATABASE.

    The publisher refuses drift before contacting a provider. This is the
    independent control: even a direct SQL edit cannot repoint an idempotency
    key at another backend or another message.
    """
    runtime = connect(runtime_dsn(ctx["dsn"]))
    try:
        row = valid_fixture("DELIVERY_INTENT_RECORDED", "immutable")
        rid = str(insert_row(runtime, row))
        other_backend = derive_provider_backend_id("fake", "other-account", "elsewhere")
        for column, value in (
            ("provider_backend_id", other_backend),
            ("provider_message_fingerprint", "f" * 64),
            ("provider_idempotency_key", "eco-dash-something-else"),
            ("provider_bound_capability_id", "d" * 32),
            ("provider_bound_bearer_generation", 2),
        ):
            error = rejects(
                runtime,
                lambda cur, c=column, v=value: cur.execute(
                    f"UPDATE {TABLE} SET {c} = %s WHERE delivery_id = %s", (v, rid)),
                label=f"changing {column} after binding")
            check(f"{column} is refused as an immutable binding",
                  "ECO_DASHBOARD_PROVIDER_BINDING_IMMUTABLE" in error, error[:160])

        for column, value in (("operation_id", _operation_id("elsewhere")),
                              ("recipient_email", "someone.else@example.invalid"),
                              ("payload_digest", "e" * 64),
                              ("subject_ref", "another-subject")):
            error = rejects(
                runtime,
                lambda cur, c=column, v=value: cur.execute(
                    f"UPDATE {TABLE} SET {c} = %s WHERE delivery_id = %s", (v, rid)),
                label=f"changing {column}")
            check(f"{column} is refused as an immutable identity",
                  "ECO_DASHBOARD_DELIVERY_IDENTITY_IMMUTABLE" in error, error[:160])

        # ...while a first binding, from NULL, is exactly what the ledger does.
        second = valid_fixture("CAPABILITY_PERSISTED", "first-binding")
        rid2 = str(insert_row(runtime, second))
        with runtime.cursor() as cur:
            cur.execute(
                f"""UPDATE {TABLE} SET state = 'DELIVERY_INTENT_RECORDED',
                        provider_name = 'fake', provider_idempotency_key = %s,
                        provider_backend_id = %s, provider_message_fingerprint = %s,
                        provider_bound_capability_id = %s,
                        provider_bound_bearer_generation = 1
                      WHERE delivery_id = %s""",
                ("eco-dash-firstbinding", BACKEND_ID, FINGERPRINT, CAPABILITY_ID, rid2))
        check("binding from NULL is permitted", True)
    finally:
        runtime.close()
    PASSED.append("a_bound_submission_identity_cannot_drift")


# --- 4. both rollout paths ------------------------------------------------------


def test_both_rollout_paths_carry_the_delivery_schema(ctx) -> None:
    """New-client onboarding AND the existing-client runner, from the files.

    The existing-client runner applies every pending `db/client_business/*.sql`
    by directory scan, so 049 reaches existing clients by being in the
    directory. Onboarding uses an explicit list, which is precisely why a new
    client can silently receive an older schema than the running code expects —
    so the list is asserted, not assumed.
    """
    onboard = (REPO_ROOT / "scripts" / "onboard_workflow_a_client.py").read_text("utf-8")
    check("new-client onboarding applies the whole 049 + 050 chain",
          all(name in onboard for name in ECO_DASHBOARD_MIGRATIONS),
          "onboarding DDL list")
    ddl_section = onboard.split("CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED")[0]
    check("both files are in the onboarding DDL file list",
          all(name in ddl_section for name in ECO_DASHBOARD_MIGRATIONS))
    check("and 050 runs after the 049 it upgrades",
          ddl_section.index(MIGRATION_050) > ddl_section.index(MIGRATION_049))
    check("and 051 runs after the 050 it upgrades",
          ddl_section.index(MIGRATION_051) > ddl_section.index(MIGRATION_050))
    marked = onboard.split("CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED")[1]
    check("both are recorded applied for a newly onboarded database",
          all(name in marked for name in ECO_DASHBOARD_MIGRATIONS))

    # THE ORDERING TRAP. Onboarding runs `apply_client_ddl` BEFORE
    # `apply_grants`, so when 049 executes for a new client the `client_trips`
    # grants it mirrors from do not exist yet and its own grant block is a
    # no-op. The new-client path therefore depends on the table being named in
    # onboarding's own grant list, and a migration list entry alone would leave
    # a new client with a table its runtime role cannot touch.
    grants_section = onboard.split("def apply_grants(")[1].split("def ")[0]
    check("onboarding grants the delivery ledger to the client runtime role",
          '"eco_dashboard_delivery_operation"' in grants_section,
          "apply_grants table list")
    check("it is granted with the DML privileges the publisher needs",
          "GRANT SELECT, INSERT, UPDATE" in grants_section)

    runner = (REPO_ROOT / "scripts" / "apply_client_business_migrations.py").read_text("utf-8")
    check("the existing-client runner scans the migration directory",
          "CLIENT_BUSINESS_DIR" in runner)
    check("both migration files are in that directory",
          all(path.exists() for path in MIGRATIONS))

    requirements = json.loads(
        (REPO_ROOT / "db" / "schema_requirements.json").read_text("utf-8"))
    entry = [r for r in requirements["requirements"]
             if r["migration"] == REQUIREMENT_MIGRATION]
    check("release preflight declares the delivery schema requirement",
          len(entry) == 1, str(len(entry)))
    check("it is declared in the client_business scope",
          entry[0]["scope"] == "client_business", entry[0]["scope"])
    relation = entry[0]["relations"][0]
    check("it names the delivery ledger relation",
          (relation["schema"], relation["table"])
          == ("public", "eco_dashboard_delivery_operation"))
    declared = {c["name"] for c in relation["columns"]}
    check("it names the binding columns the code depends on",
          {"provider_backend_id", "provider_message_fingerprint",
           "provider_idempotency_key"} <= declared, str(sorted(declared)))
    PASSED.append("both_rollout_paths_carry_the_delivery_schema")


def test_the_declared_requirement_matches_the_migration(ctx) -> None:
    """The preflight declaration is compared against a REAL applied schema.

    A requirements file that drifted from the migration would refuse a release
    whose schema is in fact correct, or pass one whose schema is not.

    It is decided by `requirement_defects` — the function release activation
    itself decides with — rather than by a private re-implementation. The
    earlier form compared declared columns by name/type and declared constraints
    by NAME ONLY, so it agreed with a declaration that could not see a same-named
    `CHECK (true)`, a rebound guard trigger or a no-op guard function. The
    adversarial matrix for those lives in
    `ops/tests_manual/test_migration_049_physical_preflight_postgres.py`; what
    is asserted here is the one direction this suite owns: the schema the
    migration really installs satisfies the release declaration in full.
    """
    document = json.loads(
        (REPO_ROOT / "db" / "schema_requirements.json").read_text("utf-8"))
    requirement = [r for r in parse_requirements(document)
                   if r.migration == REQUIREMENT_MIGRATION][0]
    # A TUPLE cursor: the preflight module reads its catalog rows positionally,
    # which is what its production connection factories hand it. This suite
    # connects with `dict_row` for its own readability, and the two must not be
    # confused when borrowing the real decision function.
    from psycopg.rows import tuple_row

    with ctx["owner"].cursor(row_factory=tuple_row) as cur:
        defects = requirement_defects(cur, requirement)
    check("the applied 049 + 050 + 051 chain satisfies the release requirement in full",
          defects == [], "; ".join(sorted(defects)))
    relation = requirement.relations[0]
    check("the requirement names provider_name, whose absence broke "
          "DeliveryLedger.load()",
          any(c.name == "provider_name" for c in relation.columns))
    check("every required CHECK is pinned to a definition, never to a name",
          all(c.definition is not None for c in relation.constraints),
          str([c.name for c in relation.constraints if c.definition is None]))
    check("the guard trigger and its function are part of the requirement",
          bool(relation.triggers) and bool(requirement.functions))
    PASSED.append("the_declared_requirement_matches_the_migration")


# --- runner ---------------------------------------------------------------------


TESTS = [
    test_the_runtime_role_can_run_the_whole_lifecycle,
    test_the_runtime_role_holds_no_privilege_it_does_not_need,
    test_the_bearer_cannot_be_copied_into_diagnostics,
    test_every_valid_lifecycle_state_is_representable,
    test_incomplete_lifecycle_states_are_rejected,
    test_a_partial_provider_binding_can_never_exist,
    test_a_bound_submission_identity_cannot_drift,
    test_both_rollout_paths_carry_the_delivery_schema,
    test_the_declared_requirement_matches_the_migration,
]


def main() -> int:
    dsn = os.getenv(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable loopback PostgreSQL 16 DSN")
        return 0
    require_loopback_dsn_or_exit(dsn, label=ENV)
    if "log_platform" in dsn or "logplatform" in dsn:
        raise RuntimeError("refusing a DSN that looks like the platform database")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        print("SKIP: psycopg is not installed in this interpreter")
        return 0

    owner = connect(dsn)
    build_schema(owner)
    ctx = {"dsn": dsn, "owner": owner}
    try:
        for test in TESTS:
            test(ctx)
            print(f"PASS {test.__name__}")
    finally:
        try:
            drop_schema(owner)
        finally:
            owner.close()
    print(f"\n{len(PASSED)} schema checks passed — runtime-role privileges, bearer "
          f"diagnostics, state completeness and both rollout paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
