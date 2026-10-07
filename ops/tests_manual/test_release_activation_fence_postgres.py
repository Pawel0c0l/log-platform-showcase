#!/usr/bin/env python3
"""The release pointer moves inside the fleet fence, or it does not move.

WHAT THIS PINS, AND WHY IT IS A SEPARATE SUITE.
    `test_release_schema_preflight_postgres.py` proves the fence itself holds
    the authoritative fleet still. This one drives the **real**
    `release_boundary.activate_release` against a temporary release tree and
    proves the ordering it is embedded in:

        verify bytes -> verify schema prerequisites -> [FENCE: re-check fleet ->
        swap pointer] -> release fence

    The failure this closes was ordering, not locking strength: the previous
    implementation validated, released its lock, re-checked a fingerprint on a
    fresh connection and *then* swapped. Independent review committed a mutation
    in that gap. So the assertions here are about *when* things happen relative
    to the pointer, which cannot be established by inspecting call order in the
    source — a real writer has to try, and be observed failing to get through.

NOTHING PRODUCTION IS TOUCHED. Every release root is a temporary directory, the
platform database is the disposable instance, and no real `current`/`previous`
pointer is read or written.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops import release_boundary as rb  # noqa: E402
from ops.release_schema_preflight import (  # noqa: E402
    PreflightReport,
    SchemaPreflightError,
    activation_fence,
    enumerate_affected_clients,
    fleet_fingerprint,
)
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

ENV = "TELEMATICS_M4_FENCE_TEST_DSN"

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


def _connect():
    import psycopg
    return psycopg.connect(os.environ[ENV])


def _client_connect(client):
    """Connect to a client business database of the fixture fleet.

    The fence re-reads every client's narrowing schema state under the
    transition lock before it yields, so activation now genuinely requires
    client connectivity. That is deliberate — an unreachable client is a
    refusal, never a skip — so the fixture materializes real (empty) client
    databases rather than pretending connectivity is free.
    """
    import psycopg
    base = os.environ[ENV].rsplit("/", 1)[0]
    return psycopg.connect(f"{base}/{client.db_name}")


def _admin():
    import psycopg
    conn = psycopg.connect(os.environ[ENV])
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------------------
# Fixtures: a minimal fleet, and a real release tree
# ---------------------------------------------------------------------------

def build_fleet(*, clients: int = 2) -> None:
    conn = _connect()
    conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    conn.execute("CREATE SCHEMA workflow_a_control")
    conn.execute(
        """CREATE TABLE workflow_a_control.client_account (
             client_id UUID PRIMARY KEY,
             client_code TEXT,
             client_name TEXT,
             enabled BOOLEAN NOT NULL,
             client_db_host TEXT NOT NULL,
             client_db_port INT NOT NULL,
             client_db_name TEXT NOT NULL)"""
    )
    for index in range(clients):
        conn.execute(
            "INSERT INTO workflow_a_control.client_account "
            "(client_id, client_code, client_name, enabled, client_db_host, "
            " client_db_port, client_db_name) VALUES (%s,%s,%s,true,%s,%s,%s)",
            (str(uuid.uuid4()), f"CL{index:06d}", f"CL{index:06d}",
             _host(), _port(), f"fence_db_{index}"),
        )
    conn.commit()
    conn.close()

    admin = _admin()
    for index in range(clients):
        admin.execute(f'DROP DATABASE IF EXISTS "fence_db_{index}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "fence_db_{index}"')
    admin.close()


def _host() -> str:
    return os.environ[ENV].split("@")[1].split(":")[0]


def _port() -> int:
    return int(os.environ[ENV].split("@")[1].split(":")[1].split("/")[0])


def current_fingerprint() -> str:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            affected, enabled = enumerate_affected_clients(cur)
        conn.rollback()
    finally:
        conn.close()
    return fleet_fingerprint(affected, enabled)


def build_release_root(tmp: Path) -> tuple:
    """A real release layout with one prepared release, built by the real code."""
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('release')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "release"], cwd=repo, check=True)

    release_root = tmp / "releases_root"
    release_root.mkdir()
    prepared = rb.prepare_release(
        release_root=release_root, source_repo=repo, committish="HEAD",
    )
    return release_root, repo, prepared["release_id"]


def _preflight_stub(fingerprint):
    """Stands in for the expensive validation pass, returning a chosen fleet."""
    def _run(*, release_tree, release_id):
        report = PreflightReport(release_id=release_id, note="test")
        report.fleet_fingerprint = fingerprint
        return report
    return _run


# ---------------------------------------------------------------------------
# R2 / R4 — the pointer swap happens under the fence, against raw SQL
# ---------------------------------------------------------------------------

def test_R2_R4_pointer_swap_is_fenced_against_raw_sql(tmp: Path) -> None:
    print("\n## test_R2_R4_pointer_swap_is_fenced_against_raw_sql")
    build_fleet()
    release_root, repo, release_id = build_release_root(tmp / "r2")
    fingerprint = current_fingerprint()

    events: List[tuple] = []
    started = threading.Event()
    finished = threading.Event()

    def _raw_sql_writer():
        # No advisory lock, no knowledge of this module: a bare connection doing
        # the one mutation that changes fleet membership.
        raw = _connect()
        try:
            with raw.cursor() as cur:
                started.set()
                cur.execute(
                    "UPDATE workflow_a_control.client_account SET enabled=false"
                )
            raw.commit()
            events.append(("raw_sql_committed", time.monotonic()))
        except Exception as exc:  # pragma: no cover
            events.append((f"raw_failed:{type(exc).__name__}", time.monotonic()))
        finally:
            raw.close()
            finished.set()

    writer = threading.Thread(target=_raw_sql_writer, daemon=True)

    # Wrap the real pointer swap so the test can observe exactly when it lands,
    # and start the raw writer while the fence is provably held.
    original_swap = rb._swap_pointer
    swapped_current = threading.Event()

    def _observed_swap(pointer, target):
        original_swap(pointer, target)
        if pointer.name == "current":
            events.append(("pointer_swapped", time.monotonic()))
            swapped_current.set()

    rb._swap_pointer = _observed_swap
    original_fence = rb._fleet_fence

    def _instrumented_fence(fp, declared_capabilities=frozenset()):
        ctx = original_fence(fp, declared_capabilities)

        class _Wrapper:
            def __enter__(self):
                value = ctx.__enter__()
                # The lock is now held. Launch the writer and give it a real
                # chance to commit before the pointer moves.
                writer.start()
                started.wait(timeout=5)
                blocked = not finished.wait(timeout=2.5)
                events.append(("observed_blocked" if blocked else "observed_free",
                               time.monotonic()))
                return value

            def __exit__(self, *exc):
                return ctx.__exit__(*exc)

        return _Wrapper()

    rb._fleet_fence = _instrumented_fence
    try:
        result = rb.activate_release(
            release_root=release_root, release_id=release_id, source_repo=repo,
            schema_preflight=_preflight_stub(fingerprint),
        )
    finally:
        rb._swap_pointer = original_swap
        rb._fleet_fence = original_fence

    _check("R2: activation reports the pointer moved", result["changed"] is True)
    _check("R2: raw SQL was blocked while the fence was held",
           ("observed_blocked", ) in [(n,) for n, _ in events],
           f"events={[n for n, _ in events]}")

    completed = finished.wait(timeout=10)
    _check("R4: the raw write completes once the fence releases", completed)
    writer.join(timeout=5)

    order = [name for name, _ts in events]
    _check("R2: the pointer swap strictly precedes the raw-SQL commit",
           "raw_sql_committed" in order
           and order.index("pointer_swapped") < order.index("raw_sql_committed"),
           f"order={order}")
    _check("R2: the release really is current on disk",
           (release_root / "current").resolve().name == release_id)


# ---------------------------------------------------------------------------
# R5 — a failing pointer swap
# ---------------------------------------------------------------------------

def test_R5_pointer_failure_releases_the_fence_and_claims_nothing(tmp: Path) -> None:
    print("\n## test_R5_pointer_failure_releases_the_fence_and_claims_nothing")
    build_fleet()
    release_root, repo, release_id = build_release_root(tmp / "r5")
    fingerprint = current_fingerprint()

    original_swap = rb._swap_pointer

    def _failing_swap(pointer, target):
        raise OSError("forced pointer failure")

    rb._swap_pointer = _failing_swap
    try:
        rb.activate_release(
            release_root=release_root, release_id=release_id, source_repo=repo,
            schema_preflight=_preflight_stub(fingerprint),
        )
        _check("R5: a failing pointer swap raises", False)
    except OSError:
        _check("R5: a failing pointer swap raises", True)
    finally:
        rb._swap_pointer = original_swap

    _check("R5: no release became current", not (release_root / "current").exists())
    log = release_root / "meta" / "activations.jsonl"
    _check("R5: nothing claims the activation happened",
           not log.exists() or not log.read_text(encoding="utf-8").strip())

    # The fence transaction must be gone: a fresh writer proceeds immediately.
    writer_done = threading.Event()

    def _writer():
        conn = _connect()
        try:
            conn.execute(
                "UPDATE workflow_a_control.client_account SET enabled=false"
            )
            conn.commit()
        finally:
            conn.close()
            writer_done.set()

    thread = threading.Thread(target=_writer, daemon=True)
    thread.start()
    _check("R5: the fence lock was released despite the failure",
           writer_done.wait(timeout=5))
    thread.join(timeout=5)

    # And the fence itself wrote nothing.
    conn = _connect()
    remaining = conn.execute(
        "SELECT count(*) FROM workflow_a_control.client_account"
    ).fetchone()[0]
    conn.rollback()
    conn.close()
    _check("R5: the fence left no database mutation of its own", remaining == 2)


# ---------------------------------------------------------------------------
# R6 — prerequisite failure happens before any pointer mutation
# ---------------------------------------------------------------------------

def test_R6_preflight_failure_precedes_any_pointer_change(tmp: Path) -> None:
    print("\n## test_R6_preflight_failure_precedes_any_pointer_change")
    build_fleet()
    release_root, repo, release_id = build_release_root(tmp / "r6")

    def _refusing_preflight(*, release_tree, release_id):
        raise SchemaPreflightError(
            "RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING", "forced refusal",
        )

    swaps: List[str] = []
    original_swap = rb._swap_pointer
    rb._swap_pointer = lambda pointer, target: swaps.append(pointer.name)
    try:
        rb.activate_release(
            release_root=release_root, release_id=release_id, source_repo=repo,
            schema_preflight=_refusing_preflight,
        )
        _check("R6: a refused prerequisite aborts activation", False)
    except SchemaPreflightError:
        _check("R6: a refused prerequisite aborts activation", True)
    finally:
        rb._swap_pointer = original_swap

    _check("R6: no pointer was touched at all", swaps == [], f"swaps={swaps}")
    _check("R6: nothing became current", not (release_root / "current").exists())


# ---------------------------------------------------------------------------
# R3 — a fleet mutation between validation and the fence
# ---------------------------------------------------------------------------

def test_R3_fleet_change_between_validation_and_fence_refuses(tmp: Path) -> None:
    print("\n## test_R3_fleet_change_between_validation_and_fence_refuses")
    build_fleet()
    release_root, repo, release_id = build_release_root(tmp / "r3")
    stale = current_fingerprint()

    # Commit a real fleet change after validation captured `stale`.
    conn = _connect()
    conn.execute(
        "INSERT INTO workflow_a_control.client_account "
        "(client_id, client_code, client_name, enabled, client_db_host, "
        " client_db_port, client_db_name) VALUES (%s,'LATE','LATE',true,%s,%s,%s)",
        (str(uuid.uuid4()), _host(), _port(), "fence_db_late"),
    )
    conn.commit()
    conn.close()

    swaps: List[str] = []
    original_swap = rb._swap_pointer
    rb._swap_pointer = lambda pointer, target: swaps.append(pointer.name)
    try:
        rb.activate_release(
            release_root=release_root, release_id=release_id, source_repo=repo,
            schema_preflight=_preflight_stub(stale),
        )
        _check("R3: a changed fleet refuses activation", False)
    except SchemaPreflightError as exc:
        _check("R3: a changed fleet refuses activation",
               exc.code == "RELEASE_SCHEMA_FLEET_CHANGED_DURING_ACTIVATION",
               f"code={exc.code}")
    finally:
        rb._swap_pointer = original_swap
    _check("R3: and no pointer moved", swaps == [], f"swaps={swaps}")


# ---------------------------------------------------------------------------
# R7 — the dry run leaves nothing behind
# ---------------------------------------------------------------------------

def test_R7_dry_run_leaves_no_locks_or_transactions(tmp: Path) -> None:
    print("\n## test_R7_dry_run_leaves_no_locks_or_transactions")
    build_fleet()
    fingerprint = current_fingerprint()

    before = _open_transaction_count()
    # The fence is the only thing that takes a lock, and a dry run never enters
    # it: `manage_release activate` without --execute calls the preflight only.
    for _ in range(3):
        current_fingerprint()
    after = _open_transaction_count()
    _check("R7: read-only preflight paths leave no open transaction",
           after <= before, f"before={before} after={after}")

    # And a writer is never blocked by them.
    done = threading.Event()

    def _writer():
        conn = _connect()
        try:
            conn.execute(
                "UPDATE workflow_a_control.client_account SET enabled=true"
            )
            conn.commit()
        finally:
            conn.close()
            done.set()

    thread = threading.Thread(target=_writer, daemon=True)
    thread.start()
    _check("R7: nothing is blocked after read-only paths run",
           done.wait(timeout=5))
    thread.join(timeout=5)

    # A completed fence must also leave no lock behind.
    with activation_fence(expected_fingerprint=fingerprint,
                          platform_conn_factory=_connect,
                          client_conn_factory=_client_connect):
        pass
    done2 = threading.Event()

    def _writer2():
        conn = _connect()
        try:
            conn.execute(
                "UPDATE workflow_a_control.client_account SET enabled=true"
            )
            conn.commit()
        finally:
            conn.close()
            done2.set()

    thread2 = threading.Thread(target=_writer2, daemon=True)
    thread2.start()
    _check("R7: an exited fence holds no lock", done2.wait(timeout=5))
    thread2.join(timeout=5)


def _open_transaction_count() -> int:
    conn = _connect()
    try:
        count = conn.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE state IN ('idle in transaction', "
            "'idle in transaction (aborted)')"
        ).fetchone()[0]
        conn.rollback()
    finally:
        conn.close()
    return int(count)


def main() -> int:
    dsn = os.getenv(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable PostgreSQL 16 DSN")
        return 0
    require_loopback_dsn_or_exit(dsn, label=ENV)
    if "logdb" in dsn.lower():
        raise RuntimeError("refusing a production-like DSN")

    # Point the PRODUCTION connection factory at the disposable instance, so
    # these tests drive `default_platform_conn` — the code activation really
    # uses — rather than an injected stand-in. The loopback guard above already
    # proved the DSN is local, and `logdb` is refused outright.
    from urllib.parse import urlsplit
    parts = urlsplit(dsn)
    os.environ["POSTGRES_HOST"] = parts.hostname or "127.0.0.1"
    os.environ["POSTGRES_PORT"] = str(parts.port or 5432)
    os.environ["POSTGRES_DB"] = (parts.path or "/").lstrip("/")
    os.environ["POSTGRES_USER"] = parts.username or ""
    os.environ["POSTGRES_PASSWORD"] = parts.password or ""

    with tempfile.TemporaryDirectory(prefix="m4-fence-") as tmpdir:
        tmp = Path(tmpdir)
        test_R2_R4_pointer_swap_is_fenced_against_raw_sql(tmp)
        test_R5_pointer_failure_releases_the_fence_and_claims_nothing(tmp)
        test_R6_preflight_failure_precedes_any_pointer_change(tmp)
        test_R3_fleet_change_between_validation_and_fence_refuses(tmp)
        test_R7_dry_run_leaves_no_locks_or_transactions(tmp)

    admin = _admin()
    for index in range(8):
        admin.execute(f'DROP DATABASE IF EXISTS "fence_db_{index}" WITH (FORCE)')
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
