#!/usr/bin/env python3
"""One release must be activatable on BOTH sides of the first-seen CONTRACT.

THE DEADLOCK THIS CLOSES.
    `db/schema_requirements.json` used to declare exactly one acceptable state
    for the client first-seen pairing constraint: the EXPAND-era
    `ck_client_trips_first_seen_instant_needs_request`. The CONTRACT closure
    drops that constraint and installs `ck_client_trips_first_seen_pairing`
    instead, so the moment the closure runs, every existing M-LAG release stops
    passing its own schema preflight — including the release that is currently
    running and the one rollback would return to. A hypothetical release
    declaring only the strict constraint cannot be activated *before* the
    closure, because the constraint does not exist yet.

    Neither ordering works, so the transition is undeployable unless one release
    can legitimately declare both states. `constraint_alternatives` is that
    declaration, and this suite is its evidence.

THE OTHER HALF — THE HOLE THE BRIDGE WOULD OTHERWISE OPEN.
    A release declares its own prerequisites, so an old release simply does not
    carry a requirement it predates. That is correct for a prerequisite and
    exactly wrong for a NARROWING: after the closure, the strict constraint
    rejects the M4-era writer's request-only inserts, yet the M4 release
    declares nothing about it and would activate cleanly and fail on its first
    production INSERT.

    `SCHEMA_STATE_GUARDS` inverts the direction — the DATABASE asserts the
    narrowing and the release must DECLARE the matching capability. The tests
    below establish that property, not a list of historical release names: any
    release tree that does not declare `client_trips_first_seen_pair_contract`
    is refused post-closure, whether it carries an M4-era requirements file or
    no requirements file at all.

DESTRUCTIVE. Creates and drops its own schemas and client databases.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops.release_schema_preflight import (  # noqa: E402
    CAPABILITY_FIRST_SEEN_PAIR_CONTRACT,
    AffectedClient,
    SchemaPreflightError,
    parse_requirements,
    verify_schema_prerequisites,
)
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

ENV = "TELEMATICS_MLAG_BRIDGE_TEST_DSN"

PLATFORM_MIGRATIONS = (
    "008_workflow_a_control_plane.sql", "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql",
    "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
    "061_workflow_a_provider_request_log.sql",
    "062_workflow_a_multi_cadence_schedule_identity.sql",
    "063_workflow_a_trip_delivery_lag_daily.sql",
)
CLIENT_MIGRATION_M4 = "047_client_trips_first_seen_request_id.sql"
CLIENT_MIGRATION_MLAG = "048_client_trips_first_seen_response_received_at.sql"
PLATFORM_MIGRATION_MLAG = "063_workflow_a_trip_delivery_lag_daily.sql"
MODE = "data_invariants_v1"

EXPAND_CONSTRAINT = "ck_client_trips_first_seen_instant_needs_request"
STRICT_CONSTRAINT = "ck_client_trips_first_seen_pairing"

#: Two enabled clients, so "one client in the wrong state" is a partial fleet
#: rather than the whole of it — the gate must refuse on the strength of one.
CLIENTS = (
    ("bd7662a5-eeb4-4614-8720-d477abfcb227", "AAA00001", "mlagbridge_a"),
    ("b454f82c-5857-4bab-8342-b7258e5cf7de", "BBB00001", "mlagbridge_b"),
)

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


def _expect_refusal(label: str, expected_code: str, fn) -> None:
    try:
        fn()
    except SchemaPreflightError as exc:
        _check(f"{label} -> {expected_code}", exc.code == expected_code,
               f"actual: {exc.code}: {exc.detail}")
        return
    _check(f"{label} -> {expected_code}", False, "the gate PASSED")


def _expect_pass(label: str, fn):
    try:
        report = fn()
    except SchemaPreflightError as exc:
        _check(label, False, f"refused: {exc.code}: {exc.detail}")
        return None
    _check(label, True)
    return report


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _connect(dbname: str):
    import psycopg
    base = os.environ[ENV].rsplit("/", 1)[0]
    return psycopg.connect(f"{base}/{dbname}")


def _admin():
    import psycopg
    conn = psycopg.connect(os.environ[ENV])
    conn.autocommit = True
    return conn


def _platform_db_name() -> str:
    return os.environ[ENV].rsplit("/", 1)[1]


def _host() -> str:
    return os.environ[ENV].split("@")[1].split(":")[0]


def _port() -> int:
    return int(os.environ[ENV].split("@")[1].split(":")[1].split("/")[0])


#: The one-directional EXPAND constraint, as migration 048 installs it.
EXPAND_DDL = (
    f"ALTER TABLE public.client_trips ADD CONSTRAINT {EXPAND_CONSTRAINT} "
    "CHECK (first_seen_response_received_at_utc IS NULL "
    "       OR first_seen_request_id IS NOT NULL) NOT VALID"
)
#: The strict CONTRACT constraint, as `close_telematics_first_seen_pair_contract`
#: installs it.
STRICT_DDL = (
    f"ALTER TABLE public.client_trips ADD CONSTRAINT {STRICT_CONSTRAINT} "
    "CHECK ((first_seen_request_id IS NULL) "
    "       = (first_seen_response_received_at_utc IS NULL)) NOT VALID"
)

#: Every schema state a client business database can be put into. The names are
#: the states the requirements file declares plus the ones it must refuse.
STATE_EXPAND = "EXPAND"
STATE_CONTRACT = "CONTRACT"
STATE_CONTRACT_NOT_VALID = "CONTRACT_NOT_VALID"
STATE_NO_CONSTRAINT = "NO_CONSTRAINT"
STATE_EXPAND_WRONG_DEFINITION = "EXPAND_WRONG_DEFINITION"
STATE_CONTRACT_WRONG_DEFINITION = "CONTRACT_WRONG_DEFINITION"
STATE_BOTH = "BOTH"
#: Independent review's exact reproduction: a valid EXPAND constraint coexisting
#: with a MALFORMED constraint carrying the strict CONTRACT name. The first
#: implementation accepted the EXPAND alternative and stopped looking, so the
#: gate passed — while this CHECK rejects the pair-atomic writer at runtime.
STATE_EXPAND_PLUS_MALFORMED_STRICT = "EXPAND_PLUS_MALFORMED_STRICT"
#: The mirror: a valid validated CONTRACT with a malformed EXPAND-named
#: constraint still on the table.
STATE_CONTRACT_PLUS_MALFORMED_EXPAND = "CONTRACT_PLUS_MALFORMED_EXPAND"
#: A constraint the requirements file never mentions. Exclusivity is scoped to
#: the participating names, so this must change nothing.
STATE_EXPAND_PLUS_UNRELATED = "EXPAND_PLUS_UNRELATED"
STATE_CONTRACT_PLUS_UNRELATED = "CONTRACT_PLUS_UNRELATED"

UNRELATED_CONSTRAINT = "ck_client_trips_unrelated_probe"
UNRELATED_DDL = (
    f"ALTER TABLE public.client_trips ADD CONSTRAINT {UNRELATED_CONSTRAINT} "
    "CHECK (provider_trip_id >= 0)"
)
#: A constraint carrying the STRICT name and the INVERTED expression. It admits
#: exactly the rows strict pairing forbids and forbids exactly the rows the
#: pair-atomic M-LAG writer produces, so a gate that waves it through has
#: authorized an activation the database will reject on its first write.
STRICT_INVERTED_DDL = (
    f"ALTER TABLE public.client_trips ADD CONSTRAINT {STRICT_CONSTRAINT} "
    "CHECK ((first_seen_request_id IS NULL) "
    "       <> (first_seen_response_received_at_utc IS NULL)) NOT VALID"
)
EXPAND_INVERTED_DDL = (
    f"ALTER TABLE public.client_trips ADD CONSTRAINT {EXPAND_CONSTRAINT} "
    "CHECK (first_seen_request_id IS NULL "
    "       OR first_seen_response_received_at_utc IS NOT NULL) NOT VALID"
)


def _apply_state(conn, state: str) -> None:
    conn.execute(f"ALTER TABLE public.client_trips "
                 f"DROP CONSTRAINT IF EXISTS {EXPAND_CONSTRAINT}")
    conn.execute(f"ALTER TABLE public.client_trips "
                 f"DROP CONSTRAINT IF EXISTS {STRICT_CONSTRAINT}")
    conn.execute(f"ALTER TABLE public.client_trips "
                 f"DROP CONSTRAINT IF EXISTS {UNRELATED_CONSTRAINT}")
    if state == STATE_EXPAND:
        conn.execute(EXPAND_DDL)
    elif state == STATE_CONTRACT:
        conn.execute(STRICT_DDL)
        conn.execute("ALTER TABLE public.client_trips "
                     f"VALIDATE CONSTRAINT {STRICT_CONSTRAINT}")
    elif state == STATE_CONTRACT_NOT_VALID:
        conn.execute(STRICT_DDL)
    elif state == STATE_BOTH:
        conn.execute(EXPAND_DDL)
        conn.execute(STRICT_DDL)
        conn.execute("ALTER TABLE public.client_trips "
                     f"VALIDATE CONSTRAINT {STRICT_CONSTRAINT}")
    elif state == STATE_EXPAND_WRONG_DEFINITION:
        conn.execute(
            f"ALTER TABLE public.client_trips ADD CONSTRAINT {EXPAND_CONSTRAINT} "
            "CHECK (first_seen_request_id IS NULL "
            "       OR first_seen_response_received_at_utc IS NOT NULL) NOT VALID"
        )
    elif state == STATE_CONTRACT_WRONG_DEFINITION:
        conn.execute(
            f"ALTER TABLE public.client_trips ADD CONSTRAINT {STRICT_CONSTRAINT} "
            "CHECK (first_seen_request_id IS NOT NULL "
            "       OR first_seen_response_received_at_utc IS NULL)"
        )
    elif state == STATE_EXPAND_PLUS_MALFORMED_STRICT:
        conn.execute(EXPAND_DDL)
        conn.execute(STRICT_INVERTED_DDL)
    elif state == STATE_CONTRACT_PLUS_MALFORMED_EXPAND:
        conn.execute(STRICT_DDL)
        conn.execute("ALTER TABLE public.client_trips "
                     f"VALIDATE CONSTRAINT {STRICT_CONSTRAINT}")
        conn.execute(EXPAND_INVERTED_DDL)
    elif state == STATE_EXPAND_PLUS_UNRELATED:
        conn.execute(EXPAND_DDL)
        conn.execute(UNRELATED_DDL)
    elif state == STATE_CONTRACT_PLUS_UNRELATED:
        conn.execute(STRICT_DDL)
        conn.execute("ALTER TABLE public.client_trips "
                     f"VALIDATE CONSTRAINT {STRICT_CONSTRAINT}")
        conn.execute(UNRELATED_DDL)
    elif state == STATE_NO_CONSTRAINT:
        pass
    else:  # pragma: no cover - a typo in a scenario must not pass silently
        raise AssertionError(f"unknown client state {state!r}")


def build_fleet(*, state: str = STATE_EXPAND, drop_m4_column: bool = False) -> None:
    """The control plane plus every client business database in one schema state."""
    platform = _connect(_platform_db_name())
    platform.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    platform.execute(
        "CREATE TABLE IF NOT EXISTS public.schema_migrations "
        "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    platform.execute("DELETE FROM public.schema_migrations")
    for name in PLATFORM_MIGRATIONS:
        platform.execute((ROOT / "db/migrations" / name).read_text())
        platform.execute(
            "INSERT INTO public.schema_migrations(filename) VALUES (%s) "
            "ON CONFLICT DO NOTHING", (name,),
        )
    for client_id, code, dbname in CLIENTS:
        platform.execute(
            """INSERT INTO workflow_a_control.client_account
              (client_id,client_code,client_name,provider_type,provider_base_url,
               provider_basic_auth_username,provider_basic_auth_password_secret_ref,
               client_db_host,client_db_port,client_db_name,client_db_user,
               client_db_password_secret_ref,client_db_schema,
               speed_trigger_filter_text,enabled,trips_pagination_mode)
              VALUES (%s,%s,%s,'telematics','https://example.invalid','u','REF',
                      %s,%s,%s,'u','REF','public','speeding',true,%s)""",
            (client_id, code, code, _host(), _port(), dbname, MODE),
        )
        platform.execute(
            """INSERT INTO workflow_a_control.client_dataset_schedule
              (schedule_id,client_id,client_code,dataset_name,enabled,frequency,
               run_time,timezone,lookback_days,overwrite_existing)
              VALUES (%s,%s,%s,'trips_sync',true,'daily','02:00','UTC',3,true)""",
            (str(uuid.uuid4()), client_id, code),
        )
    platform.commit()
    platform.close()

    admin = _admin()
    for _client_id, _code, dbname in CLIENTS:
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{dbname}"')
    admin.close()

    for _client_id, _code, dbname in CLIENTS:
        conn = _connect(dbname)
        conn.execute(
            """CREATE TABLE public.client_trips (
                 client_id UUID NOT NULL,
                 provider_trip_id BIGINT NOT NULL,
                 synced_at TIMESTAMPTZ NULL,
                 PRIMARY KEY (client_id, provider_trip_id))"""
        )
        conn.execute(
            "CREATE TABLE public.schema_migrations "
            "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        # 047 and 048's column work, applied as the migrations write it. The
        # constraint 048 installs is then replaced by whichever state this
        # scenario needs, which is exactly what the closure tool does in
        # production.
        conn.execute((ROOT / "db/client_business" / CLIENT_MIGRATION_M4).read_text())
        conn.execute((ROOT / "db/client_business" / CLIENT_MIGRATION_MLAG).read_text())
        _apply_state(conn, state)
        if drop_m4_column:
            # An UNRELATED prerequisite: 047's column. The ledger keeps claiming
            # it, so only the physical check can catch this.
            conn.execute(f"ALTER TABLE public.client_trips "
                         f"DROP CONSTRAINT IF EXISTS {EXPAND_CONSTRAINT}")
            conn.execute(f"ALTER TABLE public.client_trips "
                         f"DROP CONSTRAINT IF EXISTS {STRICT_CONSTRAINT}")
            conn.execute("ALTER TABLE public.client_trips "
                         "DROP COLUMN first_seen_request_id")
        for name in (CLIENT_MIGRATION_M4, CLIENT_MIGRATION_MLAG):
            conn.execute(
                "INSERT INTO public.schema_migrations(filename) VALUES (%s)", (name,)
            )
        conn.commit()
        conn.close()


def set_state(state: str) -> None:
    """Move every client business database to `state`, changing nothing else."""
    for _client_id, _code, dbname in CLIENTS:
        conn = _connect(dbname)
        _apply_state(conn, state)
        conn.commit()
        conn.close()


def _platform_factory():
    return _connect(_platform_db_name())


def _client_factory(client: AffectedClient):
    return _connect(client.db_name)


def _run(release_tree: Path):
    return verify_schema_prerequisites(
        release_tree=release_tree, release_id="test-release",
        platform_conn_factory=_platform_factory,
        client_conn_factory=_client_factory,
    )


# ---------------------------------------------------------------------------
# Release trees
# ---------------------------------------------------------------------------

def write_release(root: Path, document: Optional[Dict]) -> Path:
    """A release tree carrying `document` as its requirements file, or none at all."""
    tree = root
    if document is None:
        tree.mkdir(parents=True, exist_ok=True)
        return tree
    (tree / "db").mkdir(parents=True, exist_ok=True)
    (tree / "db" / "schema_requirements.json").write_text(
        json.dumps(document, indent=2), encoding="utf-8"
    )
    return tree


def _bridge_document() -> Dict:
    """The release under test: the working tree's own requirements file.

    Narrowed to the client migrations this fixture actually applies. The suite
    materializes exactly `CLIENT_MIGRATION_M4` and `CLIENT_MIGRATION_MLAG` in
    each client business database, so a `client_business` requirement for any
    other migration would refuse every activation here for a reason that has
    nothing to do with the transition under test. The pairing relation this
    suite is about is untouched.
    """
    document = json.loads(
        (ROOT / "db/schema_requirements.json").read_text(encoding="utf-8")
    )
    applied = {CLIENT_MIGRATION_M4, CLIENT_MIGRATION_MLAG}
    document["requirements"] = [
        requirement for requirement in document["requirements"]
        if requirement.get("scope") != "client_business"
        or requirement.get("migration") in applied
    ]
    return document


def _m4_era_document() -> Dict:
    """An M4-era release, reconstructed by its defining PROPERTY.

    Not by directory name: what makes such a release unsafe after the CONTRACT
    closes is that its writer sets `first_seen_request_id` alone and that its
    requirements file declares no first-seen pair capability. Any release tree
    with that property must be refused, so the test states the property.
    """
    document = _bridge_document()
    document.pop("capabilities", None)
    document["requirements"] = [
        r for r in document["requirements"]
        if r["migration"] in ("061_workflow_a_provider_request_log.sql",
                              CLIENT_MIGRATION_M4)
    ]
    return document


# ---------------------------------------------------------------------------
# 1 / 2 / 9 / 8 — the bridge passes on BOTH sides of the closure
# ---------------------------------------------------------------------------

def test_bridge_passes_before_closure(trees: Path) -> None:
    print("\n## test_bridge_passes_before_closure")
    build_fleet(state=STATE_EXPAND)
    bridge = write_release(trees / "bridge", _bridge_document())
    report = _expect_pass(
        "1/9: EXPAND state — the bridge release passes schema preflight",
        lambda: _run(bridge),
    )
    if report is None:
        return
    _check("1/9: and every enabled client was actually examined",
           report.affected_client_count == len(CLIENTS),
           f"affected={report.affected_client_count}")
    _check("1/9: no state guard is asserted before the closure",
           report.schema_state_guards == [], str(report.schema_state_guards))
    _check("1/9: the release declares the contract capability regardless",
           CAPABILITY_FIRST_SEEN_PAIR_CONTRACT in report.declared_capabilities,
           str(report.declared_capabilities))


def test_bridge_passes_after_closure(trees: Path) -> None:
    print("\n## test_bridge_passes_after_closure")
    build_fleet(state=STATE_CONTRACT)
    bridge = write_release(trees / "bridge", _bridge_document())
    report = _expect_pass(
        "2/8: strict validated CONTRACT — the SAME release still passes",
        lambda: _run(bridge),
    )
    if report is None:
        return
    _check("2/8: the closure state is recorded as an asserted guard",
           [g["capability"] for g in report.schema_state_guards]
           == [CAPABILITY_FIRST_SEEN_PAIR_CONTRACT] * len(CLIENTS),
           str(report.schema_state_guards))
    _check("2/8: and the release satisfied it by declaration",
           all(g["declared_by_release"] for g in report.schema_state_guards))


def test_no_activation_deadlock_across_the_transition(trees: Path) -> None:
    print("\n## test_no_activation_deadlock_across_the_transition")
    bridge = write_release(trees / "bridge", _bridge_document())
    build_fleet(state=STATE_EXPAND)
    before = _expect_pass("8/9: one release, pre-closure", lambda: _run(bridge))
    set_state(STATE_CONTRACT)
    after = _expect_pass("8/9: the identical release, post-closure",
                         lambda: _run(bridge))
    _check("8/9: there is therefore no ordering in which activation deadlocks",
           before is not None and after is not None)

    # Rollback to the previous release of the same bridge lineage. A predecessor
    # built from this lineage carries the same declaration, so the one-step
    # rollback envelope survives the closure.
    previous = write_release(trees / "bridge-previous", _bridge_document())
    _expect_pass("9: rollback to a bridge-lineage predecessor still passes",
                 lambda: _run(previous))


# ---------------------------------------------------------------------------
# 3 / 4 / 5 / 6 — everything else fails closed
# ---------------------------------------------------------------------------

def test_incomplete_contract_state_fails(trees: Path) -> None:
    print("\n## test_incomplete_contract_state_fails")
    build_fleet(state=STATE_CONTRACT_NOT_VALID)
    bridge = write_release(trees / "bridge", _bridge_document())
    _expect_refusal(
        "3: strict constraint present but NOT VALID, EXPAND absent",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", lambda: _run(bridge),
    )


def test_neither_constraint_fails(trees: Path) -> None:
    print("\n## test_neither_constraint_fails")
    build_fleet(state=STATE_NO_CONSTRAINT)
    bridge = write_release(trees / "bridge", _bridge_document())
    _expect_refusal(
        "4: neither EXPAND nor strict pairing present",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", lambda: _run(bridge),
    )


def test_same_name_wrong_definition_fails(trees: Path) -> None:
    print("\n## test_same_name_wrong_definition_fails")
    bridge = write_release(trees / "bridge", _bridge_document())
    build_fleet(state=STATE_EXPAND_WRONG_DEFINITION)
    _expect_refusal(
        "5: the EXPAND name carrying a different expression",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", lambda: _run(bridge),
    )
    build_fleet(state=STATE_CONTRACT_WRONG_DEFINITION)
    _expect_refusal(
        "6: the strict name carrying a different expression",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", lambda: _run(bridge),
    )


def test_bridge_does_not_weaken_unrelated_requirements(trees: Path) -> None:
    print("\n## test_bridge_does_not_weaken_unrelated_requirements")
    bridge = write_release(trees / "bridge", _bridge_document())

    # A platform prerequisite, missing from the ledger, while the client
    # databases are in a perfectly valid CONTRACT state.
    build_fleet(state=STATE_CONTRACT)
    platform = _connect(_platform_db_name())
    platform.execute("DELETE FROM public.schema_migrations WHERE filename=%s",
                     (PLATFORM_MIGRATION_MLAG,))
    platform.commit()
    platform.close()
    _expect_refusal(
        "7: an unrelated platform migration missing still refuses",
        "RELEASE_SCHEMA_PLATFORM_MIGRATION_MISSING", lambda: _run(bridge),
    )

    # A client prerequisite from an EARLIER milestone, physically gone while the
    # ledger still claims it. The alternative-state machinery must not touch it.
    build_fleet(state=STATE_EXPAND, drop_m4_column=True)
    _expect_refusal(
        "7: an unrelated client column missing still refuses",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", lambda: _run(bridge),
    )


def test_alternative_states_are_mutually_exclusive(trees: Path) -> None:
    """BLOCKER 1. Exactly one declared state may hold — never merely one of them.

    The first implementation accepted the relation as soon as an alternative
    matched, and stopped looking. Independent review reproduced the consequence
    directly (`UNEXPECTED_COMBINATION_PREFLIGHT_PASSED=True`): a database
    carrying a valid EXPAND constraint AND a malformed constraint under the
    strict CONTRACT name passed the gate, because EXPAND was checked first. That
    malformed CHECK is live DDL that rejects the pair-atomic writer, so the gate
    had authorized an activation against a schema no alternative describes.
    """
    print("\n## test_alternative_states_are_mutually_exclusive")
    bridge = write_release(trees / "bridge", _bridge_document())

    # 1 — exact EXPAND only.
    build_fleet(state=STATE_EXPAND)
    _expect_pass("1: exact EXPAND alone passes", lambda: _run(bridge))

    # 2 — exact validated CONTRACT only.
    build_fleet(state=STATE_CONTRACT)
    _expect_pass("2: exact validated CONTRACT alone passes", lambda: _run(bridge))

    # 3 — THE REVIEW'S REPRODUCTION. Valid EXPAND + malformed strict.
    build_fleet(state=STATE_EXPAND_PLUS_MALFORMED_STRICT)
    _expect_refusal(
        "3: valid EXPAND + MALFORMED competing strict constraint",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", lambda: _run(bridge),
    )
    # And the reason that state is dangerous, stated as evidence rather than as
    # a claim: the malformed CHECK really does reject the pair-atomic writer.
    conn = _connect(CLIENTS[0][2])
    rejection = ""
    try:
        conn.execute(
            "INSERT INTO public.client_trips "
            "(client_id, provider_trip_id, first_seen_request_id, "
            " first_seen_response_received_at_utc) "
            "VALUES (%s, 1, %s, now())",
            (CLIENTS[0][0], CLIENTS[0][0]),
        )
    except Exception as exc:
        rejection = f"{type(exc).__name__}: {exc}"
    finally:
        conn.rollback()
        conn.close()
    _check("3: and that malformed strict CHECK really does reject a "
           "pair-atomic INSERT, which is why passing it was unsafe",
           STRICT_CONSTRAINT in rejection, rejection or "the INSERT succeeded")

    # 4 / 6 — valid EXPAND + valid validated strict. One physical state; it must
    # refuse whichever alternative is read as the intended one.
    build_fleet(state=STATE_BOTH)
    _expect_refusal(
        "4/6: valid EXPAND + valid validated CONTRACT (both present)",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", lambda: _run(bridge),
    )

    # 5 — valid validated CONTRACT + malformed competing EXPAND.
    build_fleet(state=STATE_CONTRACT_PLUS_MALFORMED_EXPAND)
    _expect_refusal(
        "5: valid validated CONTRACT + MALFORMED competing EXPAND constraint",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", lambda: _run(bridge),
    )

    # 7 — strict present but NOT VALID, EXPAND absent. An interrupted closure is
    # not a closed one.
    build_fleet(state=STATE_CONTRACT_NOT_VALID)
    _expect_refusal(
        "7: strict NOT VALID alone does not pass as a closed CONTRACT",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", lambda: _run(bridge),
    )

    # 8 — neither.
    build_fleet(state=STATE_NO_CONSTRAINT)
    _expect_refusal(
        "8: neither alternative present",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", lambda: _run(bridge),
    )

    # 9 — an UNRELATED constraint must not invalidate an otherwise valid state.
    # Exclusivity is scoped to the names the alternatives themselves declare.
    build_fleet(state=STATE_EXPAND_PLUS_UNRELATED)
    _expect_pass("9: an unrelated constraint does not invalidate valid EXPAND",
                 lambda: _run(bridge))
    build_fleet(state=STATE_CONTRACT_PLUS_UNRELATED)
    _expect_pass("9: nor a valid validated CONTRACT", lambda: _run(bridge))

    # 10 — an unrelated MISSING prerequisite still refuses, in a valid state.
    build_fleet(state=STATE_EXPAND, drop_m4_column=True)
    _expect_refusal(
        "10: an unrelated missing prerequisite still refuses",
        "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING", lambda: _run(bridge),
    )


def test_exclusivity_is_reported_as_a_competing_state(trees: Path) -> None:
    """The refusal must SAY it was a competing state, not merely 'not present'.

    A defect string that only listed absent constraints would send an operator
    to add the missing one — the opposite of the correct action, which is to
    remove the constraint that does not belong.
    """
    print("\n## test_exclusivity_is_reported_as_a_competing_state")
    from ops.release_schema_preflight import _alternative_state_defects  # noqa: E402
    document = _bridge_document()
    relation = None
    for requirement in parse_requirements(document):
        for candidate in requirement.relations:
            if candidate.constraint_alternatives:
                relation = candidate
    if relation is None:
        _check("the bridge document declares an alternatives relation", False)
        return

    expand_def = ("CHECK (((first_seen_response_received_at_utc IS NULL) "
                  "OR (first_seen_request_id IS NOT NULL))) NOT VALID")
    present = {
        EXPAND_CONSTRAINT: (expand_def, False),
        STRICT_CONSTRAINT: ("CHECK ((nonsense IS NULL))", False),
    }
    defects = _alternative_state_defects(
        present, relation.constraint_alternatives, "public.client_trips",
    )
    joined = ";".join(defects)
    _check("a competing constraint is named as such in the refusal",
           "competing_state_constraint_present" in joined, joined)
    _check("and the refusal names the constraint that must be removed",
           STRICT_CONSTRAINT in joined, joined)

    # The valid single states produce no defect at all.
    _check("exact EXPAND alone yields no defect",
           _alternative_state_defects(
               {EXPAND_CONSTRAINT: (expand_def, False)},
               relation.constraint_alternatives, "public.client_trips") == [])
    strict_def = ("CHECK (((first_seen_request_id IS NULL) "
                  "= (first_seen_response_received_at_utc IS NULL)))")
    _check("exact validated CONTRACT alone yields no defect",
           _alternative_state_defects(
               {STRICT_CONSTRAINT: (strict_def, True)},
               relation.constraint_alternatives, "public.client_trips") == [])
    _check("strict NOT VALID alone does yield a defect",
           _alternative_state_defects(
               {STRICT_CONSTRAINT: (strict_def, False)},
               relation.constraint_alternatives, "public.client_trips") != [])
    _check("an unrelated constraint alongside exact EXPAND yields no defect",
           _alternative_state_defects(
               {EXPAND_CONSTRAINT: (expand_def, False),
                "ck_something_else": ("CHECK ((x > 0))", True)},
               relation.constraint_alternatives, "public.client_trips") == [])


# ---------------------------------------------------------------------------
# The legacy-writer safety property
# ---------------------------------------------------------------------------

def test_legacy_release_is_rejected_after_closure(trees: Path) -> None:
    print("\n## test_legacy_release_is_rejected_after_closure")
    build_fleet(state=STATE_CONTRACT)

    m4 = write_release(trees / "m4", _m4_era_document())
    _expect_refusal(
        "an M4-era release (no declared capability) after closure",
        "RELEASE_SCHEMA_STATE_CAPABILITY_MISSING", lambda: _run(m4),
    )

    # The harder case: a release predating the requirements mechanism entirely.
    # It declares nothing at all, which used to mean it skipped the fleet — the
    # exact path a capability check must not be reachable only through.
    ancient = write_release(trees / "ancient", None)
    _expect_refusal(
        "a release with no requirements file at all, after closure",
        "RELEASE_SCHEMA_STATE_CAPABILITY_MISSING", lambda: _run(ancient),
    )

    # And the interrupted closure: the strict constraint is enforcing new writes
    # from the moment it is added, so an old writer is already unsafe there.
    set_state(STATE_CONTRACT_NOT_VALID)
    _expect_refusal(
        "an M4-era release against an UNVALIDATED strict constraint",
        "RELEASE_SCHEMA_STATE_CAPABILITY_MISSING", lambda: _run(m4),
    )


def test_legacy_release_is_still_valid_before_closure(trees: Path) -> None:
    print("\n## test_legacy_release_is_still_valid_before_closure")
    # The guard must be driven by the database state, not by a blanket ban. Per
    # docs/21 §11 the M4 writer is compatible with EXPAND, and rollback to it
    # stays valid until the contract closes.
    build_fleet(state=STATE_EXPAND)
    m4 = write_release(trees / "m4", _m4_era_document())
    _expect_pass("an M4-era release is NOT blocked while the fleet is in EXPAND",
                 lambda: _run(m4))
    ancient = write_release(trees / "ancient", None)
    report = _expect_pass(
        "and neither is a release predating the requirements mechanism",
        lambda: _run(ancient),
    )
    if report is not None:
        _check("which is still recorded as predating the mechanism",
               report.note in ("release_predates_schema_requirements",
                               "no_enabled_client_accounts"),
               report.note)


def test_the_guard_is_evaluated_for_every_client(trees: Path) -> None:
    print("\n## test_the_guard_is_evaluated_for_every_client")
    # One client closed, one still in EXPAND — the state a partially completed
    # per-client closure produces. A legacy release must be refused on the
    # strength of the single closed client.
    build_fleet(state=STATE_EXPAND)
    conn = _connect(CLIENTS[1][2])
    _apply_state(conn, STATE_CONTRACT)
    conn.commit()
    conn.close()

    m4 = write_release(trees / "m4", _m4_era_document())
    _expect_refusal(
        "one closed client out of two still refuses a legacy release",
        "RELEASE_SCHEMA_STATE_CAPABILITY_MISSING", lambda: _run(m4),
    )
    bridge = write_release(trees / "bridge", _bridge_document())
    _expect_pass("while the bridge release spans the mixed fleet",
                 lambda: _run(bridge))


# ---------------------------------------------------------------------------
# The requirements document itself
# ---------------------------------------------------------------------------

def test_alternatives_are_parsed_strictly() -> None:
    print("\n## test_alternatives_are_parsed_strictly")

    def document(relation: Dict) -> Dict:
        return {
            "version": "log-platform-schema-requirements/1",
            "requirements": [{
                "migration": "x.sql", "scope": "client_business",
                "relations": [dict({"schema": "public", "table": "t"}, **relation)],
            }],
        }

    good = {"constraint_alternatives": [
        {"state": "A", "constraints": [{"name": "c1", "definition": "CHECK (x)"}]},
        {"state": "B", "constraints": [{"name": "c2", "definition": "CHECK (y)"}]},
    ]}
    parsed = parse_requirements(document(good))
    _check("a well-formed alternatives block parses",
           len(parsed[0].relations[0].constraint_alternatives) == 2)
    _check("a relation asserting only alternatives is not 'asserts nothing'",
           parsed[0].relations[0].columns == ())

    def refuses(label: str, relation: Dict) -> None:
        try:
            parse_requirements(document(relation))
        except SchemaPreflightError as exc:
            _check(label, exc.code == "RELEASE_SCHEMA_REQUIREMENTS_MALFORMED",
                   exc.code)
            return
        _check(label, False, "parsed without refusing")

    refuses("a single-member one-of is refused as a disguised requirement",
            {"constraint_alternatives": [
                {"state": "A", "constraints": [{"name": "c1"}]}]})
    refuses("an alternative with no state label is refused",
            {"constraint_alternatives": [
                {"constraints": [{"name": "c1"}]},
                {"state": "B", "constraints": [{"name": "c2"}]}]})
    refuses("an alternative asserting no constraint is refused",
            {"constraint_alternatives": [
                {"state": "A", "constraints": []},
                {"state": "B", "constraints": [{"name": "c2"}]}]})
    refuses("a duplicated state label is refused",
            {"constraint_alternatives": [
                {"state": "A", "constraints": [{"name": "c1"}]},
                {"state": "A", "constraints": [{"name": "c2"}]}]})
    refuses("a non-array alternatives block is refused",
            {"constraint_alternatives": {"state": "A"}})


def test_the_repository_declares_the_capability() -> None:
    print("\n## test_the_repository_declares_the_capability")
    document = _bridge_document()
    _check("db/schema_requirements.json declares the contract capability",
           CAPABILITY_FIRST_SEEN_PAIR_CONTRACT in document.get("capabilities", []),
           str(document.get("capabilities")))
    states = [
        group["state"]
        for requirement in document["requirements"]
        for relation in requirement["relations"]
        for group in relation.get("constraint_alternatives", [])
    ]
    _check("and declares exactly the two first-seen pairing states",
           states == ["EXPAND", "CONTRACT"], str(states))


def main() -> int:
    dsn = os.environ.get(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable PostgreSQL 16 DSN")
        return 0
    require_loopback_dsn_or_exit(dsn, label=ENV)
    if "logdb" in dsn.lower():
        raise RuntimeError("refusing a production-like DSN")

    with tempfile.TemporaryDirectory(prefix="mlag-bridge-") as tmp:
        trees = Path(tmp)
        test_bridge_passes_before_closure(trees)
        test_bridge_passes_after_closure(trees)
        test_no_activation_deadlock_across_the_transition(trees)
        test_incomplete_contract_state_fails(trees)
        test_neither_constraint_fails(trees)
        test_same_name_wrong_definition_fails(trees)
        test_bridge_does_not_weaken_unrelated_requirements(trees)
        test_alternative_states_are_mutually_exclusive(trees)
        test_exclusivity_is_reported_as_a_competing_state(trees)
        test_legacy_release_is_rejected_after_closure(trees)
        test_legacy_release_is_still_valid_before_closure(trees)
        test_the_guard_is_evaluated_for_every_client(trees)
        test_alternatives_are_parsed_strictly()
        test_the_repository_declares_the_capability()

    admin = _admin()
    for _client_id, _code, dbname in CLIENTS:
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    admin.close()

    print()
    if _failures:
        print(f"FAILED — {len(_failures)} assertion(s)")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
