#!/usr/bin/env python3
"""Driver Eco Dashboard — the delivery ledger's CONNECTION LIFECYCLE.

Run:
    python3 ops/tests_manual/test_eco_dashboard_ledger_connection_lifecycle.py

THE FINDING THIS FILE EXISTS FOR

A production `test_send` for BRAVO00016 (`with_dashboard=true`, limit 1) reached
a healthy `OK` snapshot and then failed EVERY candidate with
`DASHBOARD_INTEGRATION_ERROR` while reporting
`dashboard_ledger_connections_opened: 0` — that is, it failed before the ledger
connection was ever counted as open, so no publication and no SMTP attempt
happened at all.

The cause was a two-step connection lifecycle:

    conn = _client_business_pg_conn(cfg)   # ← configures the business timezone
    conn.autocommit = True                 # ← always fails, by construction

`_client_business_pg_conn` sets the session timezone with a real statement
(`SELECT set_config('TimeZone', …, false)`), and a statement on a psycopg
session that is NOT in autocommit opens a transaction. psycopg then refuses the
mode change outright:

    psycopg.ProgrammingError: can't change 'autocommit' now:
    connection in transaction status INTRANS

So the second line could never succeed once the first had run. The fix is not a
retry, a suppression or a stray `commit()`: the transaction mode is now chosen
when the connection is CONSTRUCTED, before any statement exists to open a
transaction with.

WHAT IS PROVED HERE

  * the ledger connection is in autocommit when `_publisher_services()` hands it
    over, and it got there at construction — never by assignment afterwards;
  * the business timezone is still configured on that connection;
  * no INTRANS → `autocommit = True` transition is attempted anywhere;
  * a REALISTIC psycopg lifecycle (one that enforces psycopg's actual rule)
    initialises successfully — the same fake refuses the old code;
  * `dashboard_ledger_connections_opened` increments exactly once, after the
    connection is established, and one connection serves many candidates;
  * the DEFAULT stays transactional, so the Eco jobs' own connections and the
    mailing send-log accounting are untouched;
  * a failure to open the ledger connection still produces no publisher effect
    and no SMTP effect.

NOT DONE ANYWHERE IN THIS FILE: a database, a network socket, an SMTP
connection, a real capability, a publisher call, or any mutation of anything.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.timezone_utils import get_business_timezone_name  # noqa: E402
from jobs.ecodriving_dashboard import eco_mailing_integration as emi  # noqa: E402
from jobs.ecodriving_dashboard import job_eco_dashboard_snapshot as snap  # noqa: E402

PASSED: list[str] = []

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
CLIENT_CODE = "BRAVO00016"
BASE_URL = "https://eco.example.invalid/dashboard"
ENDPOINT = "https://publisher.example.invalid"
TOKEN = "SYNTHETIC-PUBLISHER-CREDENTIAL-3b7d4e"
WEEK_START = date(2026, 8, 1)
WEEK_END_EXCLUSIVE = date(2026, 8, 17)   # the W3 cumulative boundary from the run


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}" if detail else name)


# ==============================================================================
# A psycopg stand-in that enforces psycopg's ACTUAL rule
# ==============================================================================

class FakeProgrammingError(Exception):
    """Stands in for `psycopg.ProgrammingError`."""


class FakeCursor:
    def __init__(self, conn: "FakePgConn"):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.statements.append((sql, params))
        # THE RULE THIS FAKE EXISTS TO ENFORCE: a statement on a non-autocommit
        # session opens a transaction and leaves it open. On an autocommit
        # session it does not.
        if not self._conn.autocommit:
            self._conn.transaction_status = "INTRANS"
        return self

    def fetchone(self):
        return None


class FakePgConn:
    """Models psycopg's transaction-status/autocommit interlock, nothing else."""

    def __init__(self, *, autocommit: bool = False):
        self._autocommit = bool(autocommit)
        self.transaction_status = "IDLE"
        self.statements: list = []
        #: every assignment attempt, with the status it was attempted in
        self.autocommit_assignments: list = []
        self.closed = False
        self.commits = 0

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value) -> None:
        self.autocommit_assignments.append((bool(value), self.transaction_status))
        if self.transaction_status == "INTRANS":
            raise FakeProgrammingError(
                "can't change 'autocommit' now: connection in transaction status INTRANS")
        self._autocommit = bool(value)

    def cursor(self, *args, **kwargs):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.transaction_status = "IDLE"

    def close(self):
        self.closed = True


class LedgerConnFactory:
    """Builds ledger connections the way the REAL helper builds them.

    Deliberately routed through the real `set_pg_session_timezone`, so the
    timezone statement is the real statement in the real order.
    """

    def __init__(self):
        self.calls: list = []
        self.conns: list = []

    def __call__(self, cfg, *, autocommit: bool = False):
        from api.timezone_utils import set_pg_session_timezone

        self.calls.append({"cfg": cfg, "autocommit": autocommit})
        conn = FakePgConn(autocommit=autocommit)
        self.conns.append(conn)
        return set_pg_session_timezone(conn)


class _NullCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return None


class _NullReadConn:
    """The Eco job's own lent connection. Never the ledger connection."""

    autocommit = False

    def cursor(self, *args, **kwargs):
        return _NullCursor()


def _service(factory, *, run_id: str = "run-ledger-lifecycle"):
    emi._client_business_pg_conn = factory  # type: ignore[assignment]
    return emi.EcoDashboardLinkService(
        settings=emi.DashboardLinkSettings(
            enabled=True, dashboard_base_url=BASE_URL,
            publisher_endpoint=ENDPOINT, publisher_token=TOKEN),
        client_id=CLIENT_ID, client_code=CLIENT_CODE, schema="public",
        period_type="weekly", period_start_date=WEEK_START,
        period_end_date=WEEK_END_EXCLUSIVE, send_scope="test",
        mailer=emi.MAILER_ECO_PERSON_WEEKLY, read_conn=_NullReadConn(),
        cfg=SimpleNamespace(client_db_name="synthetic"), run_id=run_id)


# ==============================================================================
# The tests
# ==============================================================================

def test_the_ledger_connection_is_born_in_autocommit() -> None:
    original = emi._client_business_pg_conn
    factory = LedgerConnFactory()
    try:
        service = _service(factory)
        services = service._publisher_services()
        conn = factory.conns[0]

        check("the helper was asked for autocommit AT CONSTRUCTION",
              factory.calls == [{"cfg": service.cfg, "autocommit": True}],
              str(factory.calls))
        check("the connection handed to the ledger is in autocommit",
              conn.autocommit is True)
        check("and it is the connection the ledger actually holds",
              services.ledger._conn is conn)
        check("no autocommit assignment was made after construction",
              conn.autocommit_assignments == [], str(conn.autocommit_assignments))
        check("so no INTRANS -> autocommit transition was ever attempted",
              not any(status == "INTRANS"
                      for _value, status in conn.autocommit_assignments))
        check("and the session was never left inside a transaction",
              conn.transaction_status == "IDLE", conn.transaction_status)
        check("no commit was issued merely to make the mode changeable",
              conn.commits == 0, str(conn.commits))
    finally:
        emi._client_business_pg_conn = original  # type: ignore[assignment]
    PASSED.append("the_ledger_connection_is_born_in_autocommit")


def test_the_business_timezone_is_still_configured_on_it() -> None:
    original = emi._client_business_pg_conn
    factory = LedgerConnFactory()
    try:
        service = _service(factory)
        service._publisher_services()
        conn = factory.conns[0]
        check("the timezone statement ran on the ledger connection",
              conn.statements == [("SELECT set_config('TimeZone', %s, false)",
                                   (get_business_timezone_name(),))],
              str(conn.statements))
        check("it is a SESSION setting, not a transaction-local one",
              ", false)" in conn.statements[0][0])
    finally:
        emi._client_business_pg_conn = original  # type: ignore[assignment]
    PASSED.append("the_business_timezone_is_still_configured_on_it")


def test_the_old_two_step_lifecycle_is_refused_by_the_same_fake() -> None:
    """The fake is only evidence if it would have CAUGHT the production bug."""
    from api.timezone_utils import set_pg_session_timezone

    conn = set_pg_session_timezone(FakePgConn())          # the old construction
    check("a transactional session is INTRANS after the timezone statement",
          conn.transaction_status == "INTRANS")
    raised = None
    try:
        conn.autocommit = True                            # the old second step
    except FakeProgrammingError as exc:
        raised = exc
    check("and psycopg's rule refuses the mode change afterwards",
          raised is not None and "INTRANS" in str(raised), str(raised))
    PASSED.append("the_old_two_step_lifecycle_is_refused_by_the_same_fake")


def test_the_counter_increments_once_after_the_connection_is_established() -> None:
    original = emi._client_business_pg_conn
    factory = LedgerConnFactory()
    try:
        service = _service(factory)
        check("nothing is counted before the first candidate needs it",
              service.summary()["dashboard_ledger_connections_opened"] == 0)
        service._publisher_services()
        check("exactly one open is counted",
              service.summary()["dashboard_ledger_connections_opened"] == 1)

        # --- and one connection serves many candidate link requests ----------
        for _ in range(7):
            service._publisher_services()
        check("no second connection was opened", len(factory.calls) == 1,
              str(len(factory.calls)))
        check("and the counter did not move",
              service.summary()["dashboard_ledger_connections_opened"] == 1)

        conn = factory.conns[0]
        service.close()
        check("the run closes the connection it owns", conn.closed is True)
    finally:
        emi._client_business_pg_conn = original  # type: ignore[assignment]
    PASSED.append("the_counter_increments_once_after_the_connection_is_established")


def test_a_failure_to_open_the_ledger_counts_nothing_and_publishes_nothing() -> None:
    """The production symptom, asserted as a contract rather than an accident."""
    original = emi._client_business_pg_conn
    attempts: list = []

    def refusing(cfg, *, autocommit: bool = False):
        attempts.append(autocommit)
        raise FakeProgrammingError("connection refused")

    try:
        emi._client_business_pg_conn = refusing  # type: ignore[assignment]
        service = _service(refusing)
        raised = None
        try:
            service._publisher_services()
        except FakeProgrammingError as exc:
            raised = exc
        check("the failure is not swallowed at this layer", raised is not None)
        check("nothing was counted as opened",
              service.summary()["dashboard_ledger_connections_opened"] == 0)
        check("no publisher services object exists to call anything with",
              service._services is None and service._ledger_conn is None)
        check("nothing was linked, so no message can carry a dashboard",
              service.summary()["dashboard_linked_count"] == 0)
        check("and the attempt was still made in the correct mode",
              attempts == [True], str(attempts))
    finally:
        emi._client_business_pg_conn = original  # type: ignore[assignment]
    PASSED.append("a_failure_to_open_the_ledger_counts_nothing_and_publishes_nothing")


# ==============================================================================
# The helper itself: mode at construction, default unchanged
# ==============================================================================

class _FakePsycopgModule:
    """Captures exactly what `psycopg.connect` was asked for."""

    ProgrammingError = FakeProgrammingError

    def __init__(self):
        self.connect_calls: list = []
        self.conns: list = []

    def connect(self, dsn, *args, **kwargs):
        self.connect_calls.append({"dsn": dsn, "kwargs": dict(kwargs)})
        conn = FakePgConn(autocommit=bool(kwargs.get("autocommit", False)))
        self.conns.append(conn)
        return conn


def _with_fake_psycopg(fn):
    fake = _FakePsycopgModule()
    original_module = sys.modules.get("psycopg")
    original_resolve = snap.resolve_secret
    sys.modules["psycopg"] = fake  # type: ignore[assignment]
    snap.resolve_secret = lambda _ref: "synthetic-password"  # type: ignore[assignment]
    try:
        return fn(fake)
    finally:
        if original_module is None:
            sys.modules.pop("psycopg", None)
        else:
            sys.modules["psycopg"] = original_module
        snap.resolve_secret = original_resolve  # type: ignore[assignment]


_CFG = SimpleNamespace(
    client_db_host="db.invalid", client_db_port=5432, client_db_name="synthetic",
    client_db_user="synthetic", client_db_password_secret_ref="env:SYNTHETIC")


def test_the_helper_sets_the_mode_at_connect_time_not_afterwards() -> None:
    def body(fake):
        conn = snap._client_business_pg_conn(_CFG, autocommit=True)
        check("autocommit was passed to psycopg.connect itself",
              fake.connect_calls[0]["kwargs"].get("autocommit") is True,
              str(fake.connect_calls))
        check("the connection is already in autocommit before any statement",
              conn.autocommit is True)
        check("the mode was never assigned after connect",
              conn.autocommit_assignments == [], str(conn.autocommit_assignments))
        check("the timezone statement still ran",
              len(conn.statements) == 1 and "set_config('TimeZone'" in conn.statements[0][0])
        check("and it left no open transaction behind",
              conn.transaction_status == "IDLE", conn.transaction_status)

    _with_fake_psycopg(body)
    PASSED.append("the_helper_sets_the_mode_at_connect_time_not_afterwards")


def test_the_default_client_business_connection_is_still_transactional() -> None:
    """The Eco jobs, the snapshot reads and the send-log accounting own real
    transactions. The new argument must not have changed any of them."""
    def body(fake):
        conn = snap._client_business_pg_conn(_CFG)
        check("psycopg was asked for a transactional session",
              fake.connect_calls[0]["kwargs"].get("autocommit") is False,
              str(fake.connect_calls))
        check("the session is not in autocommit", conn.autocommit is False)
        check("and the timezone statement opened a transaction, as before",
              conn.transaction_status == "INTRANS")

    _with_fake_psycopg(body)

    # The argument is keyword-only, so no positional caller can flip the mode of
    # an existing transactional job by accident.
    import inspect

    params = inspect.signature(snap._client_business_pg_conn).parameters
    check("autocommit is keyword-only",
          params["autocommit"].kind is inspect.Parameter.KEYWORD_ONLY)
    check("and it defaults to the transactional behaviour",
          params["autocommit"].default is False)
    PASSED.append("the_default_client_business_connection_is_still_transactional")


def test_no_dashboard_call_site_flips_autocommit_after_connecting() -> None:
    """A source scan, because the failure mode is a two-line ORDERING."""
    import re

    for path in ("jobs/ecodriving_dashboard/eco_mailing_integration.py",
                 "jobs/ecodriving_dashboard/job_eco_dashboard_publish.py",
                 "jobs/ecodriving_dashboard/job_eco_dashboard_snapshot.py"):
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        check(f"{path} assigns .autocommit nowhere",
              re.search(r"^\s*\S*\.autocommit\s*=", code, re.MULTILINE) is None, path)
        if "_client_business_pg_conn(" in code and "DeliveryLedger(" in code:
            check(f"{path} opens its ledger connection in autocommit",
                  "autocommit=True" in code, path)
    PASSED.append("no_dashboard_call_site_flips_autocommit_after_connecting")


def main() -> int:
    tests = [
        test_the_ledger_connection_is_born_in_autocommit,
        test_the_business_timezone_is_still_configured_on_it,
        test_the_old_two_step_lifecycle_is_refused_by_the_same_fake,
        test_the_counter_increments_once_after_the_connection_is_established,
        test_a_failure_to_open_the_ledger_counts_nothing_and_publishes_nothing,
        test_the_helper_sets_the_mode_at_connect_time_not_afterwards,
        test_the_default_client_business_connection_is_still_transactional,
        test_no_dashboard_call_site_flips_autocommit_after_connecting,
    ]
    print("### Eco Dashboard — delivery-ledger connection lifecycle")
    for test in tests:
        test()
        print(f"PASS  {test.__name__}")
    print("\n" + "=" * 78)
    print(f"{len(PASSED)} checks passed — no database, no network, no SMTP, no capability")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
