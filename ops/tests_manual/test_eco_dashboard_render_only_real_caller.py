#!/usr/bin/env python3
"""Driver Eco Dashboard V1 — render-only through the REAL caller send scope.

Run:
    python3 ops/tests_manual/test_eco_dashboard_render_only_real_caller.py

THE DEFECT THIS SUITE EXISTS TO KEEP FIXED

All four Eco mailing jobs construct `EcoDashboardLinkService` with
`send_scope=execution.send_scope`. For `execution_mode=render_only` — the
DEFAULT execution mode — that value is `"render_only"`, which names an
EXECUTION scope, not a delivery. Construction used to normalise every scope
through the delivery mapping eagerly, so `--with-dashboard` +
`execution_mode=render_only` raised

    DeliveryContractError: no dashboard delivery scope for execution
    scope 'render_only'

before the candidate loop, for every client and all four mailing families.
The previous render-only coverage constructed the service with
`send_scope="normal"` and therefore never met the real caller value; this
suite exercises exactly that value, read from the real `ExecutionContract`,
so the blind spot cannot re-form.

THE INVARIANT, IN BOTH DIRECTIONS

A render-only rehearsal never holds a delivery scope: `"render_only"` has no
entry in the delivery mapping, `publication_send_scope()` refuses under
render-only, and constructing a render-only service under `normal`/`test`/
`forced` refuses too. A real delivery run is unchanged: `normal`/`forced`/
`test` still map, an unmappable scope still refuses at construction, and the
publication identity is still minted only on the publishing path.

NOT DONE ANYWHERE IN THIS FILE: a real e-mail, an SMTP connection, a network
request, a database, a Cloudflare resource, or any production state. Every
check is deterministic and offline.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving.email_safety import ExecutionContract, ExecutionMode  # noqa: E402
from jobs.ecodriving_dashboard import eco_mailing_integration as emi  # noqa: E402
from jobs.ecodriving_dashboard.delivery_contract import (  # noqa: E402
    DeliveryContractError,
    SEND_SCOPES,
)

PASSED: list[str] = []

BASE_URL = "https://eco.example.invalid/dashboard"
ENDPOINT = "https://publisher.example.invalid"
TOKEN = "SYNTHETIC-PUBLISHER-CREDENTIAL-render-only"
CLIENT_ID = "11111111-1111-1111-1111-111111111111"

WEEK_START = date(2026, 5, 1)
WEEK_END_EXCLUSIVE = date(2026, 5, 18)   # W3 of 2026-05
MONTH_START = date(2026, 4, 1)
MONTH_END_EXCLUSIVE = date(2026, 5, 1)

ECO_JOBS = (
    "jobs/ecodriving/job_eco_driving_weekly_email_notifications.py",
    "jobs/ecodriving/job_eco_driving_monthly_email_notifications.py",
    "jobs/ecodriving_person/job_eco_driving_person_weekly_email_notifications.py",
    "jobs/ecodriving_person/job_eco_driving_person_monthly_email_notifications.py",
)

#: The four mailing families, each with a client the pipeline-family
#: declaration actually knows.
MAILING_VARIANTS = (
    ("driver weekly", "ALPHA00001", "weekly", emi.MAILER_ECO_WEEKLY,
     WEEK_START, WEEK_END_EXCLUSIVE),
    ("driver monthly", "ALPHA00001", "monthly", emi.MAILER_ECO_MONTHLY,
     MONTH_START, MONTH_END_EXCLUSIVE),
    ("person weekly", "BRAVO00016", "weekly", emi.MAILER_ECO_PERSON_WEEKLY,
     WEEK_START, WEEK_END_EXCLUSIVE),
    ("person monthly", "BRAVO00016", "monthly", emi.MAILER_ECO_PERSON_MONTHLY,
     MONTH_START, MONTH_END_EXCLUSIVE),
)

TEMPLATE = ("<html><body><table>"
            "{eco_dashboard_section_html}{eco_dashboard_link_html}"
            "</table></body></html>")


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}" if detail else name)


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _contract(mode: ExecutionMode, *, test_recipient: Optional[str] = None,
              reason: Optional[str] = None) -> ExecutionContract:
    return ExecutionContract(mode=mode, test_recipient_email=test_recipient,
                             force_resend_reason=reason,
                             allow_unclosed_period_for_test=False)


def _render_only_settings() -> emi.DashboardLinkSettings:
    """The REAL render-only settings shape: `from_params` with the opt-in, the
    way all four jobs resolve it (`render_only=dry_run`)."""
    return emi.DashboardLinkSettings.from_params(
        {"with_dashboard": True, "dashboard_base_url": BASE_URL},
        render_only=True)


def _delivery_settings() -> emi.DashboardLinkSettings:
    return emi.DashboardLinkSettings.from_params(
        {"with_dashboard": True, "dashboard_base_url": BASE_URL,
         "publisher_endpoint": ENDPOINT, "publisher_token": TOKEN},
        render_only=False)


def _service(settings: emi.DashboardLinkSettings, send_scope: str,
             *, client_code: str = "ALPHA00001", period_type: str = "weekly",
             start: date = WEEK_START, end: date = WEEK_END_EXCLUSIVE,
             mailer: str = emi.MAILER_ECO_WEEKLY,
             read_conn: Any = None) -> emi.EcoDashboardLinkService:
    return emi.EcoDashboardLinkService(
        settings=settings, client_id=CLIENT_ID, client_code=client_code,
        schema="public", period_type=period_type, period_start_date=start,
        period_end_date=end, send_scope=send_scope, mailer=mailer,
        read_conn=read_conn)


@dataclass
class _SyntheticSnapshot:
    payload: bytes
    payload_digest: str
    current_identity: Any
    previous_identity: Any
    family: Any
    client_code: str
    period_type: str
    _status: str = "OK"

    @property
    def snapshot_status(self) -> str:
        return self._status


class _NullCursor:
    def __init__(self, statements: list):
        self.statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append(" ".join(str(sql).split()))


class _NullConn:
    """Transactional, like the connection the Eco jobs actually lend."""

    autocommit = False

    def __init__(self):
        self.statements: list = []

    def cursor(self, **kwargs):
        return _NullCursor(self.statements)


class _SnapshotStandIn:
    """Swap the snapshot builder for a synthetic one, and forbid every
    remote-shaped effect for the duration."""

    def __enter__(self):
        self._build = emi.build_delivery_snapshot_from_cursor
        self._conn = emi._client_business_pg_conn
        self.ledger_conns: list = []

        def forbidden_conn(cfg, **kwargs):
            self.ledger_conns.append(cfg)
            raise AssertionError("a render-only run must open no ledger connection")

        emi.build_delivery_snapshot_from_cursor = (  # type: ignore[assignment]
            lambda cur, **kwargs: _SyntheticSnapshot(
                payload=b"{}", payload_digest="0" * 64,
                current_identity=kwargs["current_identity"],
                previous_identity=kwargs["previous_identity"],
                family=kwargs["family"], client_code=kwargs["client_code"],
                period_type=kwargs["period_type"]))
        emi._client_business_pg_conn = forbidden_conn  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        emi.build_delivery_snapshot_from_cursor = self._build  # type: ignore[assignment]
        emi._client_business_pg_conn = self._conn  # type: ignore[assignment]
        return False


# ==============================================================================
# 1 — THE REAL CALLER VALUE REACHES THE INTEGRATION AND WORKS
# ==============================================================================


def test_the_real_caller_value_is_the_render_only_execution_scope() -> None:
    """`"render_only"` is not this suite's invention: it is what the execution
    contract actually hands the four jobs, and what they pass on verbatim."""
    contract = _contract(ExecutionMode.RENDER_ONLY)
    check("render-only execution resolves send_scope='render_only'",
          contract.send_scope == "render_only", contract.send_scope)
    check("the integration names the SAME value",
          emi.RENDER_ONLY_SEND_SCOPE == contract.send_scope)
    check("and a render-only execution sends no email at all",
          not contract.sends_email and not contract.is_real_recipient_scope)
    PASSED.append("the_real_caller_value_is_the_render_only_execution_scope")


def test_render_only_constructs_and_links_for_all_four_families() -> None:
    """The defect path itself: real settings shape, real caller scope, all four
    mailing families, through construction, link production and rendering."""
    scope = _contract(ExecutionMode.RENDER_ONLY).send_scope
    for label, client_code, period_type, mailer, start, end in MAILING_VARIANTS:
        with _SnapshotStandIn() as stand_in:
            # This construction raised DeliveryContractError on the unfixed
            # tree — before the candidate loop, for every client.
            service = _service(_render_only_settings(), scope,
                               client_code=client_code, period_type=period_type,
                               start=start, end=end, mailer=mailer,
                               read_conn=_NullConn())
            check(f"{label}: dashboard integration initialises", service.enabled)
            check(f"{label}: the execution scope is kept verbatim",
                  service.send_scope == scope, service.send_scope)
            check(f"{label}: the job's own period was verified",
                  service.current_identity is not None
                  and service.current_identity.period_start_date == start)

            body, outcome = emi.render_with_dashboard_link(
                service, identity_key="DRIVER-A",
                recipient_email="a@example.invalid", context={},
                template_html=TEMPLATE,
                render=lambda ctx: TEMPLATE.format(**ctx))
            check(f"{label}: the outcome is RENDER_ONLY",
                  outcome.status == emi.LinkStatus.RENDER_ONLY)
            check(f"{label}: it does not block the rehearsal",
                  not outcome.blocks_send and body is not None)
            check(f"{label}: the message carries the placeholder link",
                  outcome.capability_url.endswith("#k=" + "0" * 43)
                  and outcome.capability_url in body)
            check(f"{label}: no ledger connection was opened",
                  stand_in.ledger_conns == []
                  and service.summary()["dashboard_ledger_connections_opened"] == 0)
            check(f"{label}: nothing was published or linked for real",
                  service.summary()["dashboard_linked_count"] == 0
                  and service.summary()["dashboard_render_only_count"] == 1)
            service.close()
    PASSED.append("render_only_constructs_and_links_for_all_four_families")


def test_all_four_jobs_reach_the_shared_path_with_the_execution_scope() -> None:
    """Family coverage is real, not asserted: each job passes
    `execution.send_scope` into the ONE shared service construction, so the
    construction proof above covers every production caller."""
    import ast

    for path in ECO_JOBS:
        tree = ast.parse(read(path))
        constructions = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).endswith("EcoDashboardLinkService")]
        check("the job constructs the shared service exactly once",
              len(constructions) == 1, path)
        keywords = {kw.arg: ast.unparse(kw.value) for kw in constructions[0].keywords}
        check("send_scope is the execution contract's scope, verbatim",
              keywords.get("send_scope") == "execution.send_scope", path)
        check("the settings come from the shared opt-in resolver",
              "DashboardLinkSettings.from_params" in read(path), path)
    PASSED.append("all_four_jobs_reach_the_shared_path_with_the_execution_scope")


# ==============================================================================
# 2 — RENDER-ONLY NEVER BECOMES A DELIVERY
# ==============================================================================


def test_render_only_never_derives_or_holds_a_delivery_scope() -> None:
    check("the delivery mapping stays exactly three entries wide",
          set(emi.DELIVERY_SEND_SCOPE_BY_EXECUTION_SCOPE) == {"normal", "forced", "test"})
    check("render_only has no delivery mapping and never will",
          emi.RENDER_ONLY_SEND_SCOPE not in emi.DELIVERY_SEND_SCOPE_BY_EXECUTION_SCOPE)
    check("the ledger's own scope vocabulary excludes it too",
          emi.RENDER_ONLY_SEND_SCOPE not in SEND_SCOPES)

    service = _service(_render_only_settings(), emi.RENDER_ONLY_SEND_SCOPE)
    check("the rehearsal keeps its execution scope verbatim",
          service.send_scope == emi.RENDER_ONLY_SEND_SCOPE)
    try:
        service.publication_send_scope()
    except DeliveryContractError:
        pass
    else:
        raise AssertionError(
            "a render-only run must refuse to produce a delivery send scope")
    PASSED.append("render_only_never_derives_or_holds_a_delivery_scope")


def test_a_rehearsal_cannot_be_constructed_under_a_delivery_scope() -> None:
    """The previous coverage's exact blind spot — render-only settings with
    `send_scope="normal"` — is now unwritable, so a test or future caller
    cannot quietly bind a rehearsal to a real delivery's scope again."""
    for wrong in ("normal", "test", "forced"):
        try:
            _service(_render_only_settings(), wrong)
        except DeliveryContractError as error:
            check("the refusal names the render-only contract",
                  "render-only" in str(error), str(error))
        else:
            raise AssertionError(
                f"render-only construction under {wrong!r} must refuse")
    PASSED.append("a_rehearsal_cannot_be_constructed_under_a_delivery_scope")


# ==============================================================================
# 3 — REAL DELIVERY SEMANTICS ARE UNCHANGED
# ==============================================================================


def test_delivery_scopes_still_map_and_still_fail_closed() -> None:
    expected = {
        ExecutionMode.NORMAL_SEND: "normal",
        ExecutionMode.FORCE_RESEND: "normal",   # forced converges ON PURPOSE
        ExecutionMode.TEST_SEND: "test",
    }
    recipients = {ExecutionMode.TEST_SEND: "qa@example.invalid"}
    reasons = {ExecutionMode.FORCE_RESEND: "operator-authorised resend"}
    for mode, delivery in expected.items():
        contract = _contract(mode, test_recipient=recipients.get(mode),
                             reason=reasons.get(mode))
        service = _service(_delivery_settings(), contract.send_scope)
        check(f"{mode.value} resolves delivery scope {delivery!r}",
              service.send_scope == delivery, service.send_scope)
        check("and the publishing path hands out the same scope",
              service.publication_send_scope() == delivery)

    # A true-delivery run claiming the rehearsal scope is the inverse defect,
    # and it still refuses at construction with the original error.
    try:
        _service(_delivery_settings(), "render_only")
    except DeliveryContractError as error:
        check("a delivery run cannot borrow the rehearsal scope",
              "no dashboard delivery scope" in str(error), str(error))
    else:
        raise AssertionError("a delivery run under 'render_only' must refuse")

    # An undeclared scope never acquires a fallback.
    try:
        _service(_delivery_settings(), "broadcast")
    except DeliveryContractError:
        pass
    else:
        raise AssertionError("an undeclared scope must refuse at construction")
    PASSED.append("delivery_scopes_still_map_and_still_fail_closed")


def test_a_real_delivery_still_publishes_under_the_mapped_identity() -> None:
    """The publishing path still derives its identity from the single
    producer: a forced run publishes as `normal`, exactly as before."""
    captured: list = []

    class _Record:
        operation_id = "OPERATION-0000000001"

    class _Result:
        capability_url = BASE_URL + "#k=" + "Q" * 43
        has_link = True
        state = "EXTERNAL_MAILER_HANDOFF"
        record = _Record()
        conflict_code = None
        invocation = "COMPLETED"
        detail = ""

    original_ensure = emi.pub.ensure_capability
    original_conn = emi._client_business_pg_conn
    original_build = emi.build_delivery_snapshot_from_cursor

    class _AutocommitConn:
        autocommit = True

        def close(self):
            pass

    def fake_ensure(services, *, identity, **kwargs):
        captured.append(identity)
        return _Result()

    try:
        emi.pub.ensure_capability = fake_ensure  # type: ignore[assignment]
        emi._client_business_pg_conn = (  # type: ignore[assignment]
            lambda cfg, **kwargs: _AutocommitConn())
        emi.build_delivery_snapshot_from_cursor = (  # type: ignore[assignment]
            lambda cur, **kwargs: _SyntheticSnapshot(
                payload=b"{}", payload_digest="0" * 64,
                current_identity=kwargs["current_identity"],
                previous_identity=kwargs["previous_identity"],
                family=kwargs["family"], client_code=kwargs["client_code"],
                period_type=kwargs["period_type"]))
        service = _service(_delivery_settings(),
                           _contract(ExecutionMode.FORCE_RESEND,
                                     reason="operator-authorised resend").send_scope,
                           read_conn=_NullConn())
        outcome = service.link_for(identity_key="DRIVER-A",
                                   recipient_email="a@example.invalid")
        check("the forced run produced a real link",
              outcome.status == emi.LinkStatus.LINKED and outcome.has_link)
        check("exactly one publication identity was minted", len(captured) == 1)
        check("and it publishes under the mapped delivery scope 'normal'",
              captured[0].send_scope == "normal", captured[0].send_scope)
        service.close()
    finally:
        emi.pub.ensure_capability = original_ensure  # type: ignore[assignment]
        emi._client_business_pg_conn = original_conn  # type: ignore[assignment]
        emi.build_delivery_snapshot_from_cursor = original_build  # type: ignore[assignment]
    PASSED.append("a_real_delivery_still_publishes_under_the_mapped_identity")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ALL PASS ({len(PASSED)} groups)")
    for name in PASSED:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
