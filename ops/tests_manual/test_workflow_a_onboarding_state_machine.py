#!/usr/bin/env python3
"""Onboarding must leave a client in exactly one state: not production-ready.

Proves the enforcement added to `scripts/onboard_workflow_a_client.py`:

  * a new client is created `strict_meta` with a **disabled** `trips_sync`
    schedule, and onboarding has no code path that enables it;
  * onboarding does not declare success from the fact that rows exist — it
    verifies the exact newly created state, on the transaction that created it
    and before the commit, and refuses an ambiguous or unexpected state;
  * that transaction is genuinely all-or-nothing: a failed verification leaves
    zero newly created rows and every pre-existing row byte-identical;
  * onboarding is a *fresh-client creation tool* — an existing, partially
    created, progressed or ambiguous target is refused before any mutation and
    before any provider request, and is never reported as
    `CREATED_DISABLED_STRICT`;
  * its output states plainly that the account is not production-ready, names
    the state machine, and prints the audit as the required next step instead of
    the old direct sync command (which, against a disabled schedule, would skip
    its work and still exit 0);
  * it emits an onboarding state reference the later tools can bind to.

Set `TELEMATICS_ONBOARDING_STATE_TEST_DSN` to a disposable PostgreSQL 16 DSN.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.api.telematics import schedule_mutation_surfaces as sms  # noqa: E402
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from ops.tests_manual.test_telematics_cold_start_audit import (  # noqa: E402
    install_network_guard,
)

ENV = "TELEMATICS_ONBOARDING_STATE_TEST_DSN"

MIGRATIONS = (
    "008_workflow_a_control_plane.sql",
    "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql",
    "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    "032_workflow_a_eco_driving_registry.sql",
    "040_workflow_a_trip_metrics_population_source.sql",
    "046_workflow_a_eco_person_registry.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
    # Needed by the zero-state check: coverage and recovery are two of the four
    # kinds of progress that must refuse a fresh onboarding.
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
    "062_workflow_a_multi_cadence_schedule_identity.sql",
)

CID = "0f01d9e8-f7ba-4ec0-8f64-279ef6b7fe87"
CODE = "ONBD00001"
SID = "4208d57f-9837-42cc-89b5-1cf1833bf352"


def _dict_row():
    from psycopg.rows import dict_row
    return dict_row


_ONBOARD_MODULE_NAME = "onboard_workflow_a_client_under_test"


def _onboard():
    """Import the onboarding module by path, without executing its CLI.

    Registered in `sys.modules` before execution because the module defines
    dataclasses, and `dataclasses` resolves annotations through
    `sys.modules[cls.__module__]`.
    """
    import importlib.util

    if _ONBOARD_MODULE_NAME in sys.modules:
        return sys.modules[_ONBOARD_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(
        _ONBOARD_MODULE_NAME,
        ROOT / "scripts" / "onboard_workflow_a_client.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ONBOARD_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_ONBOARD_MODULE_NAME, None)
        raise
    return module


# ---------------------------------------------------------------------------
# Source-level guarantees (no database needed)
# ---------------------------------------------------------------------------

def test_onboarding_cannot_enable_trips_sync_in_source() -> None:
    """No statement in the script can produce an enabled schedule."""
    text = (ROOT / "scripts" / "onboard_workflow_a_client.py").read_text(
        encoding="utf-8"
    )
    flat = " ".join(text.split())

    inserts = re.findall(
        r"INSERT INTO workflow_a_control\.client_dataset_schedule.*?\)",
        flat, re.IGNORECASE,
    )
    assert inserts, "the schedule INSERT was not found"
    for statement in inserts:
        assert "TRUE," not in statement.split("VALUES", 1)[-1][:40], statement

    # It never issues an UPDATE against the schedule table at all.
    assert not re.search(
        r"UPDATE workflow_a_control\.client_dataset_schedule", flat,
        re.IGNORECASE,
    ), "onboarding must not update a schedule row"

    # The seeded value is a named constant, and it is False.
    module = _onboard()
    assert module.SEEDED_SCHEDULE_ENABLED is False
    assert module.ONBOARDED_TRIPS_PAGINATION_MODE == "strict_meta"
    assert module.ONBOARDING_TERMINAL_STATE == "CREATED_DISABLED_STRICT"
    assert module.ONBOARDING_STATE_MACHINE[0] == "CREATED_DISABLED_STRICT"
    assert module.ONBOARDING_STATE_MACHINE[-1] == "PRODUCTION_READY"
    assert len(module.ONBOARDING_STATE_MACHINE) == 9


def test_onboarding_no_longer_prints_a_direct_sync_command() -> None:
    """The old "first manual run command" is gone, not merely reworded.

    Printing `ops/runner.py jobs.api.telematics.sync_trips_and_speeding` as the
    next step is what made a skipped run look like a first successful run.
    """
    text = (ROOT / "scripts" / "onboard_workflow_a_client.py").read_text(
        encoding="utf-8"
    )
    assert "def print_run_command" not in text
    assert "First manual run command" not in text
    # The sync module may still be named in prose warning against running it,
    # but never inside a runner invocation the operator could copy.
    assert "runner.py \\\n      jobs.api.telematics.sync_trips_and_speeding" \
        not in text


def test_creation_guard_refuses_an_enabled_trips_sync() -> None:
    try:
        sms.assert_creation_permitted(
            surface=sms.SURFACE_ONBOARDING,
            dataset_name="trips_sync", enabled=True,
        )
    except sms.ScheduleMutationRefused as exc:
        assert exc.code == "SCHEDULE_CREATION_REFUSED_ENABLED", exc.code
    else:
        raise AssertionError("an enabled trips_sync creation was permitted")


# ---------------------------------------------------------------------------
# Committed-state verification
# ---------------------------------------------------------------------------

def bootstrap(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
        for name in MIGRATIONS:
            cur.execute((ROOT / "db/migrations" / name).read_text(encoding="utf-8"))
        # The platform-run probe reads `public.runs`; the API creates it at
        # startup rather than through a migration, so it is created here in the
        # same shape (`api/main.py`).
        cur.execute("DROP TABLE IF EXISTS public.runs CASCADE")
        cur.execute(
            """
            CREATE TABLE public.runs (
              run_id UUID PRIMARY KEY,
              started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              ended_at TIMESTAMPTZ,
              status TEXT NOT NULL,
              trigger TEXT NOT NULL DEFAULT 'MANUAL',
              source TEXT NOT NULL DEFAULT 'test',
              actor TEXT,
              params JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
    conn.commit()


def seed(
    conn, *, mode="strict_meta", schedule_enabled=False, schedules=1,
    duplicate_code=False,
) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM workflow_a_control.client_dataset_schedule")
        cur.execute("DELETE FROM workflow_a_control.client_account")
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
        cur.execute(
            "ALTER TABLE workflow_a_control.client_account"
            " DROP CONSTRAINT IF EXISTS uq_client_account_client_code"
        )
        # The duplicate-code case deliberately creates the state the unique
        # index normally prevents, to prove the verifier does not rely on it.
        cur.execute("DROP INDEX IF EXISTS idx_client_account_client_code")
        cur.execute(
            "DROP INDEX IF EXISTS workflow_a_control.idx_client_account_client_code"
        )
        for index, client_id in enumerate(
            [CID] + ([str(CID).replace("0b0a4d01", "0b0a4d09")]
                     if duplicate_code else [])
        ):
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_account
                  (client_id, client_code, client_name, provider_type,
                   provider_base_url, provider_basic_auth_username,
                   provider_basic_auth_password_secret_ref, client_db_host,
                   client_db_port, client_db_name, client_db_user,
                   client_db_password_secret_ref, client_db_schema,
                   speed_trigger_filter_text, enabled, trips_pagination_mode)
                VALUES (%s,%s,%s,'telematics','https://example.invalid','u','REF',
                        '127.0.0.1',5432,'db','u','REF','public','speeding',
                        true,%s)
                """,
                (client_id, CODE, f"Onboarded {index}", mode),
            )
        for index in range(schedules):
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_dataset_schedule
                  (schedule_id, client_id, client_code, dataset_name, enabled,
                   frequency, run_time, timezone, lookback_days,
                   overwrite_existing)
                VALUES (%s,%s,%s,'trips_sync',%s,'daily','02:00','UTC',1,true)
                """,
                (SID if index == 0 else SID.replace("0b0a4d02", "0b0a4d08"),
                 CID, CODE, schedule_enabled),
            )
    conn.commit()


def _with_platform_env(dsn: str, fn):
    parts = dict(piece.split("=", 1) for piece in dsn.split() if "=" in piece)
    original = dict(os.environ)
    try:
        os.environ.update({
            "POSTGRES_HOST": parts.get("host", "127.0.0.1"),
            "POSTGRES_PORT": parts.get("port", "5432"),
            "POSTGRES_DB": parts["dbname"],
            "POSTGRES_USER": parts["user"],
            "POSTGRES_PASSWORD": parts.get("password", ""),
        })
        return fn()
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_a_new_client_is_strict_and_disabled(conn, dsn) -> None:
    module = _onboard()
    seed(conn)
    state = _with_platform_env(
        dsn, lambda: module.verify_onboarding_state(CID, CODE)
    )
    assert state["state"] == "CREATED_DISABLED_STRICT", state
    assert state["trips_pagination_mode"] == "strict_meta", state
    assert state["trips_sync_schedule_enabled"] is False, state
    assert state["production_ready"] is False, state
    assert state["reporting_ready"] is False, state
    assert state["schedule_id"] == SID, state
    assert state["client_id"] == CID and state["client_code"] == CODE, state


def test_verification_refuses_an_enabled_schedule(conn, dsn) -> None:
    module = _onboard()
    seed(conn, schedule_enabled=True)
    try:
        _with_platform_env(
            dsn, lambda: module.verify_onboarding_state(CID, CODE)
        )
    except module.OnboardError as exc:
        assert "enabled" in str(exc), exc
    else:
        raise AssertionError("an enabled trips_sync schedule was accepted")


def test_verification_refuses_a_compatibility_mode_client(conn, dsn) -> None:
    """The mode flip is a later state, not something onboarding may produce."""
    module = _onboard()
    seed(conn, mode="data_invariants_v1")
    try:
        _with_platform_env(
            dsn, lambda: module.verify_onboarding_state(CID, CODE)
        )
    except module.OnboardError as exc:
        assert "strict_meta" in str(exc), exc
    else:
        raise AssertionError("a data_invariants_v1 new client was accepted")


def test_verification_refuses_ambiguous_onboarding_state(conn, dsn) -> None:
    module = _onboard()

    seed(conn, schedules=2)
    try:
        _with_platform_env(
            dsn, lambda: module.verify_onboarding_state(CID, CODE)
        )
    except module.OnboardError as exc:
        assert "2 schedule rows" in str(exc), exc
    else:
        raise AssertionError("two competing schedules were accepted")

    seed(conn, duplicate_code=True)
    try:
        _with_platform_env(
            dsn, lambda: module.verify_onboarding_state(CID, CODE)
        )
    except module.OnboardError as exc:
        assert "ambiguous identity" in str(exc), exc
    else:
        raise AssertionError("a duplicated client_code was accepted")


def test_output_says_not_production_ready(conn, dsn) -> None:
    module = _onboard()
    seed(conn)
    state = _with_platform_env(
        dsn, lambda: module.verify_onboarding_state(CID, CODE)
    )
    cfg = module.OnboardConfig.__new__(module.OnboardConfig)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        module.print_next_state_machine_step(state, cfg)
    text = buffer.getvalue()

    assert "NOT production-ready" in text, text
    assert "NOT reporting-ready" in text, text
    # It names the required next state-machine step, and the read-only tool.
    assert "ZERO_STATE_VERIFIED" in text, text
    assert "audit_telematics_cold_start.py" in text, text
    # It warns against the exact thing that produced the ECHO00001 confusion.
    assert "still exits 0" in text, text
    # Every state is listed, and only the reached one is marked.
    for name in module.ONBOARDING_STATE_MACHINE:
        assert name in text, name
    assert text.count("[✓]") == 1, text
    # A coverage row alone means nothing.
    assert "does NOT mean the client is reporting-ready" in text, text

    # The state reference is machine-readable and complete.
    reference = json.loads(text.rsplit("\n  ", 1)[-1].strip())
    assert reference["state"] == "CREATED_DISABLED_STRICT"
    assert reference["schedule_id"] == SID
    assert reference["production_ready"] is False
    assert reference["onboarding_state_version"] == "telematics-onboarding-state/1"


# ---------------------------------------------------------------------------
# Fresh-creation atomicity and zero-state refusals
# ---------------------------------------------------------------------------
#
# Two defects are pinned here. First, the control-plane rows were written by
# three separate autocommit connections and verified only *after* they were
# durable, so a verification failure left a client, ten schedule rows and
# fourteen retention rows behind. Second, an existing client was treated as
# "skip the insert and carry on", so an already-progressed client could be
# driven through the rest of the script and reported as CREATED_DISABLED_STRICT.

NEW_NAME = "Freshly Onboarded"
NEW_CODE = "FRSH00001"
NEW_DB = "frsh00001_business"

#: A pre-existing, entirely unrelated client. Nothing any onboarding path does
#: may change a single byte of it.
OTHER_ID = "0bef2333-76e4-4fcf-803e-b4e8a5052324"
OTHER_CODE = "OTHR00001"


def _fresh_config(module, *, name=NEW_NAME, code=NEW_CODE, db_name=NEW_DB):
    """An `OnboardConfig` for a client that does not exist."""
    return module.OnboardConfig(
        client_name=name,
        client_key=name.upper().replace(" ", "_"),
        client_code=code,
        enabled=True,
        provider_type="telematics_fleet",
        provider_base_url="https://provider.invalid",
        client_db_host="127.0.0.1",
        client_db_port=5432,
        client_db_name=db_name,
        client_db_schema="public",
        speed_trigger_filter_text="speeding",
        api_username_env="FRESH_API_USERNAME",
        api_key_env="FRESH_API_KEY",
        db_username_env="FRESH_DB_USERNAME",
        db_key_env="FRESH_DB_KEY",
        api_username="provider-user",
        api_key="unused-in-these-tests",
        db_username="frsh_user",
        db_key="unused-in-these-tests",
    )


def _reset_control_plane(conn) -> None:
    """Empty every control-plane table, then seed one unrelated client."""
    with conn.cursor() as cur:
        for table in (
            "workflow_a_control.client_dataset_recovery_run",
            "workflow_a_control.client_dataset_coverage",
            "workflow_a_control.client_schedule_run_history",
            "workflow_a_control.client_table_retention",
            "workflow_a_control.client_dataset_schedule",
            "workflow_a_control.client_account",
        ):
            cur.execute(f"DELETE FROM {table}")
        cur.execute("DELETE FROM public.runs")
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_account
              (client_id, client_code, client_name, provider_type,
               provider_base_url, provider_basic_auth_username,
               provider_basic_auth_password_secret_ref, client_db_host,
               client_db_port, client_db_name, client_db_user,
               client_db_password_secret_ref, client_db_schema,
               speed_trigger_filter_text, enabled, trips_pagination_mode)
            VALUES (%s,%s,'Unrelated Existing','telematics',
                    'https://other.invalid','ou','OREF','127.0.0.1',5432,
                    'othr00001_business','ou','OREF','public','speeding',
                    true,'data_invariants_v1')
            """,
            (OTHER_ID, OTHER_CODE),
        )
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_schedule
              (client_id, client_code, dataset_name, enabled, frequency,
               run_time, timezone, lookback_days, overwrite_existing)
            VALUES (%s,%s,'trips_sync',true,'daily','02:00','UTC',1,true)
            """,
            (OTHER_ID, OTHER_CODE),
        )
    conn.commit()


def _control_plane_snapshot(conn) -> dict:
    """Everything that must not change, as comparable values."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_jsonb(a.*) AS row FROM workflow_a_control.client_account a"
            " ORDER BY a.client_id"
        )
        accounts = [r["row"] for r in cur.fetchall()]
        cur.execute(
            "SELECT to_jsonb(s.*) AS row"
            "  FROM workflow_a_control.client_dataset_schedule s"
            " ORDER BY s.client_id, s.dataset_name"
        )
        schedules = [r["row"] for r in cur.fetchall()]
        cur.execute(
            "SELECT to_jsonb(t.*) AS row"
            "  FROM workflow_a_control.client_table_retention t"
            " ORDER BY t.client_id, t.table_name"
        )
        retention = [r["row"] for r in cur.fetchall()]
    return {"accounts": accounts, "schedules": schedules, "retention": retention}


def _counts_for(conn, *, client_code: str) -> dict:
    with conn.cursor() as cur:
        counts = {}
        for label, table in (
            ("accounts", "workflow_a_control.client_account"),
            ("schedules", "workflow_a_control.client_dataset_schedule"),
            ("retention", "workflow_a_control.client_table_retention"),
        ):
            cur.execute(
                f"SELECT count(*) AS n FROM {table} WHERE client_code = %s",
                (client_code,),
            )
            counts[label] = int(cur.fetchone()["n"])
    return counts


def _refusal(module, cfg):
    """Run the zero-state check, returning the refusal it raised."""
    try:
        module.check_platform_control_plane(cfg)
    except module.OnboardingRefusal as exc:
        return exc
    except module.OnboardError as exc:
        raise AssertionError(f"expected a refusal, got OnboardError: {exc}")
    raise AssertionError("the zero-state check accepted a non-zero state")


def test_clean_zero_state_onboarding_succeeds(conn, dsn) -> None:
    module = _onboard()
    _reset_control_plane(conn)
    cfg = _fresh_config(module)

    state = _with_platform_env(dsn, lambda: (
        module.check_platform_control_plane(cfg),
        module.create_control_plane_state(cfg, module.OnboardState()),
    )[1])

    assert state["state"] == "CREATED_DISABLED_STRICT", state
    assert state["client_code"] == NEW_CODE, state
    assert state["trips_sync_schedule_enabled"] is False, state
    assert state["production_ready"] is False, state

    counts = _counts_for(conn, client_code=NEW_CODE)
    assert counts["accounts"] == 1, counts
    assert counts["schedules"] == len(module.DEFAULT_DATASETS), counts
    assert counts["retention"] == len(module.DEFAULT_TABLES), counts
    # Every seeded schedule is disabled, without exception.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n"
            "  FROM workflow_a_control.client_dataset_schedule"
            " WHERE client_code = %s AND enabled",
            (NEW_CODE,),
        )
        assert int(cur.fetchone()["n"]) == 0


def test_verification_happens_before_commit(conn, dsn) -> None:
    """The verification must run on the creating transaction, not after it.

    Proven behaviorally: the verification is replaced by a probe that counts the
    rows it can see. It sees the newly created rows — so it runs after the
    inserts — while a second, independent connection sees none of them, which is
    only possible before the commit.
    """
    import psycopg

    module = _onboard()
    _reset_control_plane(conn)
    cfg = _fresh_config(module)
    observed = {}

    original = module.verify_onboarding_state_on_cursor

    def probing_verify(cur, client_id, client_code):
        cur.execute(
            "SELECT count(*) AS n"
            "  FROM workflow_a_control.client_dataset_schedule"
            " WHERE client_id = %s",
            (client_id,),
        )
        observed["visible_in_transaction"] = int(cur.fetchone()["n"])
        with psycopg.connect(dsn, row_factory=_dict_row(), autocommit=True) as other:
            result = other.execute(
                "SELECT count(*) AS n FROM workflow_a_control.client_account"
                " WHERE client_code = %s", (client_code,),
            )
            observed["visible_elsewhere"] = int(result.fetchone()["n"])
        return original(cur, client_id, client_code)

    module.verify_onboarding_state_on_cursor = probing_verify
    try:
        _with_platform_env(dsn, lambda: (
            module.check_platform_control_plane(cfg),
            module.create_control_plane_state(cfg, module.OnboardState()),
        )[1])
    finally:
        module.verify_onboarding_state_on_cursor = original

    assert observed["visible_in_transaction"] == len(module.DEFAULT_DATASETS), (
        observed
    )
    assert observed["visible_elsewhere"] == 0, (
        "the rows were already committed when the verification ran", observed,
    )


def test_a_failed_verification_rolls_back_every_created_row(conn, dsn) -> None:
    """Zero newly created rows, and every pre-existing row byte-identical."""
    module = _onboard()
    _reset_control_plane(conn)
    cfg = _fresh_config(module)
    before = _control_plane_snapshot(conn)

    original = module.verify_onboarding_state_on_cursor

    def failing_verify(cur, client_id, client_code):
        raise module.OnboardError("forced verification failure")

    state = module.OnboardState()
    module.verify_onboarding_state_on_cursor = failing_verify
    try:
        _with_platform_env(dsn, lambda: (
            module.check_platform_control_plane(cfg),
            module.create_control_plane_state(cfg, state),
        )[1])
    except module.OnboardError as exc:
        assert "forced verification failure" in str(exc), exc
    else:
        raise AssertionError("a failed verification still committed")
    finally:
        module.verify_onboarding_state_on_cursor = original

    counts = _counts_for(conn, client_code=NEW_CODE)
    assert counts == {"accounts": 0, "schedules": 0, "retention": 0}, counts
    # The compensating-cleanup flag no longer names a row, because none exists.
    assert state.inserted_client_id is None, state
    assert state.control_plane_committed is False, state
    # And nothing that existed before was touched.
    assert _control_plane_snapshot(conn) == before


def test_a_failure_partway_through_seeding_leaves_nothing(conn, dsn) -> None:
    """A failure between the inserts is as atomic as one at the end."""
    module = _onboard()
    _reset_control_plane(conn)
    cfg = _fresh_config(module)
    before = _control_plane_snapshot(conn)

    original = module.seed_dataset_schedule_and_retention

    def half_seed(client_id, client_code, *, apply, cur=None):
        original(client_id, client_code, apply=apply, cur=cur)
        raise module.OnboardError("forced failure after seeding")

    module.seed_dataset_schedule_and_retention = half_seed
    try:
        _with_platform_env(dsn, lambda: module.create_control_plane_state(
            cfg, module.OnboardState(),
        ))
    except module.OnboardError:
        pass
    else:
        raise AssertionError("a failure during seeding still committed")
    finally:
        module.seed_dataset_schedule_and_retention = original

    assert _counts_for(conn, client_code=NEW_CODE) == {
        "accounts": 0, "schedules": 0, "retention": 0,
    }
    assert _control_plane_snapshot(conn) == before


def test_an_existing_target_client_refuses_before_mutation(conn, dsn) -> None:
    module = _onboard()
    _reset_control_plane(conn)
    cfg = _fresh_config(module)
    _with_platform_env(dsn, lambda: module.create_control_plane_state(
        cfg, module.OnboardState(),
    ))
    before = _control_plane_snapshot(conn)

    refusal = _with_platform_env(dsn, lambda: _refusal(module, cfg))
    assert refusal.code == module.ONBOARDING_REFUSED_CLIENT_EXISTS, refusal.code
    # Rerunning onboarding for a successful client performs zero writes.
    assert _control_plane_snapshot(conn) == before

    # The same target reached by client_code alone, and by client_db_name alone.
    for variant in (
        _fresh_config(module, name="Different Name"),
        _fresh_config(module, name="Different Name", code="OTHRCODE1"),
    ):
        refusal = _with_platform_env(dsn, lambda v=variant: _refusal(module, v))
        assert refusal.code in module.ONBOARDING_REFUSAL_CODES, refusal.code
        assert _control_plane_snapshot(conn) == before


def test_partially_created_target_state_refuses_before_mutation(conn, dsn) -> None:
    """Residue from an interrupted attempt is not a zero state.

    In an intact platform database the `017` consistency trigger and the
    `client_id` foreign keys normally prevent a control-plane row from carrying
    a `client_code` with no `client_account` behind it. This deliberately
    creates that state anyway — by dropping the FK and the trigger, the same way
    the duplicate-code case above suspends the unique index — because the check
    must not *depend* on those constraints. The state is reachable in reality
    through exactly the situation this refusal exists for: a manual repair
    performed with constraints suspended, or a restore of a partial dump.
    """
    module = _onboard()
    orphan_probes = (
        ("client_dataset_schedule", "trg_client_dataset_schedule_client_code",
         "INSERT INTO workflow_a_control.client_dataset_schedule"
         " (client_id, client_code, dataset_name, enabled, frequency,"
         "  run_time, timezone, lookback_days, overwrite_existing)"
         " VALUES (%s,%s,'trips_sync',false,'daily','02:00','UTC',1,true)"),
        ("client_table_retention", "trg_client_table_retention_client_code",
         "INSERT INTO workflow_a_control.client_table_retention"
         " (client_id, client_code, table_name, enabled, retention_days)"
         " VALUES (%s,%s,'client_trips',false,365)"),
    )
    for table, trigger, statement in orphan_probes:
        bootstrap(conn)
        _reset_control_plane(conn)
        cfg = _fresh_config(module)
        orphan_client = "abade8ec-f17c-470f-8c90-436a032a9f0a"
        qualified = f"workflow_a_control.{table}"
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {qualified} DISABLE TRIGGER {trigger}")
            for constraint in (
                f"{table}_client_id_fkey",
                f"fk_{table}_client_id_code",
            ):
                cur.execute(
                    f"ALTER TABLE {qualified} DROP CONSTRAINT IF EXISTS"
                    f" {constraint}"
                )
            cur.execute(statement, (orphan_client, NEW_CODE))
        conn.commit()
        before = _control_plane_snapshot(conn)

        refusal = _with_platform_env(dsn, lambda: _refusal(module, cfg))
        assert refusal.code == module.ONBOARDING_REFUSED_PARTIAL_STATE, (
            table, refusal.code,
        )
        assert refusal.evidence, (table, refusal.evidence)
        assert _control_plane_snapshot(conn) == before

    # Restore the schema the remaining tests rely on.
    bootstrap(conn)


def test_a_progressed_target_refuses_before_mutation(conn, dsn) -> None:
    """Coverage, a recovery row, schedule history and a business run each refuse."""
    module = _onboard()

    def seed_progress(kind: str) -> None:
        _reset_control_plane(conn)
        cfg = _fresh_config(module)
        _with_platform_env(dsn, lambda: module.create_control_plane_state(
            cfg, module.OnboardState(),
        ))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT client_id::text AS client_id"
                "  FROM workflow_a_control.client_account"
                " WHERE client_code = %s", (NEW_CODE,),
            )
            client_id = cur.fetchone()["client_id"]
            cur.execute(
                "SELECT schedule_id::text AS schedule_id"
                "  FROM workflow_a_control.client_dataset_schedule"
                " WHERE client_id = %s AND dataset_name = 'trips_sync'",
                (client_id,),
            )
            schedule_id = cur.fetchone()["schedule_id"]
            if kind == "coverage":
                cur.execute(
                    "INSERT INTO workflow_a_control.client_dataset_coverage"
                    " (schedule_id, client_id, client_code, dataset_name)"
                    " VALUES (%s,%s,%s,'trips_sync')",
                    (schedule_id, client_id, NEW_CODE),
                )
            elif kind == "recovery":
                cur.execute(
                    """
                    INSERT INTO workflow_a_control.client_dataset_recovery_run
                      (client_id, client_code, schedule_id, dataset_name,
                       window_start_ts, window_end_ts,
                       expected_old_covered_through_ts, status, reason,
                       approval_ref, repository_head, pagination_mode,
                       stabilization_delay_seconds, overlap_seconds,
                       max_recovery_span_seconds, initial_coverage_snapshot,
                       initial_coverage_fingerprint)
                    VALUES (%s,%s,%s,'trips_sync',
                            '2026-07-01T00:00:00Z','2026-07-02T00:00:00Z',
                            '2026-07-01T00:00:00Z','PLANNED','progress probe',
                            'TELEMATICS-COLD-START-FRSH00001-2026-07-W01',%s,
                            'data_invariants_v1',
                            600,60,86400,'{}'::jsonb,%s)
                    """,
                    (client_id, NEW_CODE, schedule_id, "b" * 40, "a" * 64),
                )
            elif kind == "history":
                cur.execute(
                    """
                    INSERT INTO workflow_a_control.client_schedule_run_history
                      (schedule_id, client_id, client_code, dataset_name,
                       scheduled_fire_ts, window_start_ts, window_end_ts,
                       status)
                    VALUES (%s,%s,%s,'trips_sync',
                            '2026-07-01T02:00:00Z','2026-06-30T00:00:00Z',
                            '2026-07-01T00:00:00Z','SUCCESS')
                    """,
                    (schedule_id, client_id, NEW_CODE),
                )
            elif kind == "business_run":
                cur.execute(
                    "INSERT INTO public.runs (run_id, status, trigger, source,"
                    " params) VALUES (gen_random_uuid(),'SUCCESS','MANUAL',"
                    "'jobs.api.telematics.sync_trips_and_speeding',"
                    " jsonb_build_object('client_id', %s::text))",
                    (client_id,),
                )
            else:
                raise AssertionError(kind)
        conn.commit()

    for kind in ("coverage", "recovery", "history", "business_run"):
        seed_progress(kind)
        cfg = _fresh_config(module)
        before = _control_plane_snapshot(conn)
        refusal = _with_platform_env(dsn, lambda: _refusal(module, cfg))
        assert refusal.code == module.ONBOARDING_REFUSED_PROGRESS_STATE, (
            kind, refusal.code,
        )
        assert kind.replace("business_run", "platform_business_runs") in (
            " ".join(refusal.evidence)
        ) or refusal.evidence, (kind, refusal.evidence)
        assert _control_plane_snapshot(conn) == before


def test_ambiguous_duplicate_client_state_refuses(conn, dsn) -> None:
    """Two rows the target identity could mean is never resolved by guessing."""
    module = _onboard()
    _reset_control_plane(conn)
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE workflow_a_control.client_account"
            " DROP CONSTRAINT IF EXISTS uq_client_account_client_code"
        )
        cur.execute("DROP INDEX IF EXISTS idx_client_account_client_code")
        cur.execute(
            "DROP INDEX IF EXISTS"
            " workflow_a_control.idx_client_account_client_code"
        )
        for index, client_id in enumerate((
            "33bda17b-19df-4133-86ba-116c0ea10631",
            "d4a40456-a756-4c7f-836b-940edb74ff84",
        )):
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_account
                  (client_id, client_code, client_name, provider_type,
                   provider_base_url, provider_basic_auth_username,
                   provider_basic_auth_password_secret_ref, client_db_host,
                   client_db_port, client_db_name, client_db_user,
                   client_db_password_secret_ref, client_db_schema,
                   speed_trigger_filter_text, enabled, trips_pagination_mode)
                VALUES (%s,%s,%s,'telematics','https://x.invalid','u','REF',
                        '127.0.0.1',5432,%s,'u','REF','public','speeding',
                        true,'strict_meta')
                """,
                (client_id, NEW_CODE, f"{NEW_NAME} {index}",
                 f"{NEW_DB}_{index}"),
            )
    conn.commit()
    before = _control_plane_snapshot(conn)

    cfg = _fresh_config(module)
    refusal = _with_platform_env(dsn, lambda: _refusal(module, cfg))
    assert refusal.code == module.ONBOARDING_REFUSED_AMBIGUOUS_STATE, refusal.code
    assert len(refusal.evidence.get("client_ids", [])) == 2, refusal.evidence
    assert _control_plane_snapshot(conn) == before
    # Restore the constraint the other tests rely on.
    bootstrap(conn)


def test_refusal_output_never_claims_the_first_state(conn, dsn) -> None:
    """The output points at diagnosis, never at CREATED_DISABLED_STRICT."""
    module = _onboard()
    _reset_control_plane(conn)
    cfg = _fresh_config(module)
    _with_platform_env(dsn, lambda: module.create_control_plane_state(
        cfg, module.OnboardState(),
    ))
    refusal = _with_platform_env(dsn, lambda: _refusal(module, cfg))

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        module.print_refusal_guidance(refusal, cfg)
    text = buffer.getvalue()

    assert module.ONBOARDING_REFUSED_CLIENT_EXISTS in text, text
    # It never claims the client was created, or reached the first state.
    assert "CREATED_DISABLED_STRICT" not in text, text
    assert "onboarding_state" not in text, text
    assert "Nothing was created" in text, text
    # It points at read-only diagnosis, and at the separate procedures.
    assert "audit_telematics_cold_start.py" in text, text
    assert "docs/07_operations.md" in text, text
    assert "decommissioning" in text.lower(), text
    # And explicitly not at a direct business-job run.
    assert "runner.py" not in text, text
    assert "still\n  exits 0" in text or "exits 0" in text, text


def test_no_onboarding_path_performs_provider_work(conn, dsn) -> None:
    """A refused target issues zero provider requests.

    The zero-state check runs before the provider auth preflight, so the
    ineligible target is decided without any outbound call. Proven by driving
    the real preflight ordering with a provider check that fails the test if it
    is ever reached.
    """
    module = _onboard()
    _reset_control_plane(conn)
    cfg = _fresh_config(module)
    _with_platform_env(dsn, lambda: module.create_control_plane_state(
        cfg, module.OnboardState(),
    ))

    calls = []
    original = module.provider_auth_preflight
    module.provider_auth_preflight = lambda c: calls.append(c)
    args = SimpleNamespace(
        skip_provider_auth_check=False, skip_ddl=True, skip_db_create=True,
    )
    try:
        _with_platform_env(
            dsn, lambda: module.preflight_all(cfg, args=args),
        )
    except module.OnboardingRefusal as exc:
        assert exc.code == module.ONBOARDING_REFUSED_CLIENT_EXISTS, exc.code
    else:
        raise AssertionError("the preflight accepted an existing client")
    finally:
        module.provider_auth_preflight = original

    assert calls == [], "a provider request was made for a refused target"

    # And the onboarding module launches no subprocess on any path at all: the
    # business job is never invoked, in any form, by any branch.
    source = (ROOT / "scripts" / "onboard_workflow_a_client.py").read_text(
        encoding="utf-8"
    )
    assert "import subprocess" not in source, (
        "onboarding must not import subprocess"
    )
    assert "subprocess.run" not in source and "subprocess.Popen" not in source
    assert "os.system" not in source and "os.exec" not in source
    # `ops/runner.py` may be named only in prose warning against running the
    # sync job, never as a command the operator could copy and run.
    assert "ops/runner.py" not in source, source[:0]


# ---------------------------------------------------------------------------

def test_on_disposable_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn:
        bootstrap(conn)
        test_a_new_client_is_strict_and_disabled(conn, dsn)
        test_verification_refuses_an_enabled_schedule(conn, dsn)
        test_verification_refuses_a_compatibility_mode_client(conn, dsn)
        test_verification_refuses_ambiguous_onboarding_state(conn, dsn)
        test_output_says_not_production_ready(conn, dsn)
        conn.rollback()

        # --- fresh-creation atomicity and zero-state refusals ---
        # These commit deliberately: the point is what survives a rollback, so
        # they cannot run inside one enclosing transaction.
        bootstrap(conn)
        test_clean_zero_state_onboarding_succeeds(conn, dsn)
        test_verification_happens_before_commit(conn, dsn)
        test_a_failed_verification_rolls_back_every_created_row(conn, dsn)
        test_a_failure_partway_through_seeding_leaves_nothing(conn, dsn)
        test_an_existing_target_client_refuses_before_mutation(conn, dsn)
        test_partially_created_target_state_refuses_before_mutation(conn, dsn)
        test_a_progressed_target_refuses_before_mutation(conn, dsn)
        test_ambiguous_duplicate_client_state_refuses(conn, dsn)
        test_refusal_output_never_claims_the_first_state(conn, dsn)
        test_no_onboarding_path_performs_provider_work(conn, dsn)


def main() -> None:
    install_network_guard()
    test_onboarding_cannot_enable_trips_sync_in_source()
    test_onboarding_no_longer_prints_a_direct_sync_command()
    test_creation_guard_refuses_an_enabled_trips_sync()
    dsn = os.getenv(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable PostgreSQL 16 DSN")
        return
    # Destructive: drops schemas and applies migrations. Prove the target
    # is loopback-only before opening a connection.
    require_loopback_dsn_or_exit(dsn, label=ENV)
    test_on_disposable_postgres(dsn)
    print("OK - Workflow A onboarding state-machine checks passed")


if __name__ == "__main__":
    main()
