#!/usr/bin/env python3
"""Driver Eco Dashboard V1 — opt-in mailing rollout guard and host transport.

Run:
    python3 ops/tests_manual/test_eco_dashboard_optin_rollout_and_transport.py

WHAT THIS SUITE IS ABOUT

Two things had to become true before a controlled dashboard mailing rollout was
even possible, and this file proves both.

1. THE HOST TRANSPORT COULD NOT PUBLISH AT ALL. `HttpSecureDeliveryTransport`
   shipped `urllib`'s default `Python-urllib/3.x` User-Agent, and the deployed
   Cloudflare workers.dev edge answers that with an empty HTTP 403 BEFORE the
   Worker executes. The measured comparison — urllib UA -> 403, explicit product
   UA -> Worker, curl UA -> Worker, absent UA -> Worker — means normal host
   publishing was broken as shipped, and that the failure arrived as a 403 that
   classified as an anonymous `PROTOCOL_ERROR`. Both are fixed: an explicit,
   stable product User-Agent on every request, and a distinct definite outcome
   for 403.

2. DASHBOARD MAILING IS OPT-IN, AND OPT-IN IS ONLY HALF OF IT. The four existing
   Eco mailing commands take `--with-dashboard`. Without it they are legacy-only
   and touch nothing dashboard-shaped. With it, they additionally require
   explicit client-level rollout permission, which is currently enabled for NO
   production client.

NOT DONE ANYWHERE IN THIS FILE: a real e-mail, an SMTP connection, a network
request of any kind, a Cloudflare resource, a wrangler invocation, a deployment,
a schedule, or any database at all. Every check is deterministic and offline.
The production rollout declaration is READ, never written.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving_dashboard import dashboard_rollout as roll  # noqa: E402
from jobs.ecodriving_dashboard import eco_mailing_integration as emi  # noqa: E402
from jobs.ecodriving_dashboard import secure_delivery_client as sdc  # noqa: E402

PASSED: list[str] = []

BASE_URL = "https://eco.example.invalid/dashboard"
ENDPOINT = "https://publisher.example.invalid"
#: A machine credential this repository invented for these assertions. It is not
#: a secret and names no real system; its only job is to be searched for.
TOKEN = "SYNTHETIC-PUBLISHER-CREDENTIAL-7c41ea9b"
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

#: The four existing mailing variants, with the pipeline family each one's
#: client belongs to. `sources.PIPELINE_FAMILY_BY_CLIENT_CODE` declares only
#: these two clients, so these are the four real person/driver weekly/monthly
#: paths the enabled-path proof has to cover.
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


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}" if detail else name)


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


class _NoDashboardEnv:
    """Run a block with every dashboard environment variable removed.

    The point of several checks below is "the default path does not depend on
    configuration", and a suite that inherited a configured `.env` would prove
    the opposite of what it claims.
    """

    KEYS = (emi.ENV_DASHBOARD_BASE_URL, emi.ENV_PUBLISHER_ENDPOINT,
            emi.ENV_PUBLISHER_TOKEN, roll.ENV_ROLLOUT_FILE)

    def __enter__(self):
        self._saved = {key: os.environ.pop(key, None) for key in self.KEYS}
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


# ==============================================================================
# 1 — THE HOST TRANSPORT
# ==============================================================================


class _CapturedRequest:
    def __init__(self, request):
        self.full_url = request.full_url
        # `urllib` normalises header names to `Capitalize`d form, so this is the
        # shape the outbound request actually carries.
        self.headers = dict(request.headers)
        self.data = request.data

    def header(self, name: str) -> Optional[str]:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


class _FakeResponse:
    """The `urlopen` context manager shape, and nothing more."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _drive_transport(status: int, body: bytes) -> list[_CapturedRequest]:
    """Run every publisher route through the REAL transport, offline.

    `urlopen` is replaced, so nothing leaves the process; the transport, the
    client, the header assembly and the response mapping are all the production
    code.
    """
    import urllib.request

    captured: list[_CapturedRequest] = []
    original = urllib.request.urlopen

    def fake_urlopen(request, timeout=None):
        captured.append(_CapturedRequest(request))
        return _FakeResponse(status, body)

    urllib.request.urlopen = fake_urlopen
    try:
        client = sdc.SecureDeliveryClient(
            sdc.HttpSecureDeliveryTransport(ENDPOINT), publisher_token=TOKEN)
        for call in (
            lambda: client.publish(operation_id="op-1", subject_ref="subject-1",
                                   payload_digest="d" * 64, body=b'{"snapshot":1}',
                                   period_type="weekly"),
            lambda: client.recover(operation_id="op-1", period_type="weekly"),
            lambda: client.record_delivery(operation_id="op-1",
                                           phase=sdc.DELIVERY_PHASE_INTENT,
                                           capability_id="c" * 32),
        ):
            try:
                call()
            except (sdc.SecureDeliveryError, sdc.TransportOutcomeUnknown):
                pass
    finally:
        urllib.request.urlopen = original
    return captured


def test_every_publisher_request_carries_the_explicit_product_user_agent() -> None:
    body = json.dumps({"status": sdc.PUBLISHED, "next_action": sdc.PERSIST_BEARER,
                       "capability": "Z" * 43}).encode("utf-8")
    captured = _drive_transport(201, body)
    check("all three publisher routes were exercised", len(captured) == 3,
          str(len(captured)))
    for request in captured:
        agent = request.header("User-Agent")
        check("the request carries an explicit User-Agent", agent is not None,
              request.full_url)
        check("it is THE product User-Agent", agent == sdc.PUBLISHER_USER_AGENT,
              f"{agent!r}")

    check("the User-Agent is identical on every request",
          len({r.header("User-Agent") for r in captured}) == 1)
    check("urllib's default is gone",
          not any("Python-urllib" in (r.header("User-Agent") or "")
                  for r in captured))
    PASSED.append("every_publisher_request_carries_the_explicit_product_user_agent")


def test_the_user_agent_carries_no_secret_and_no_identity() -> None:
    """It must identify the product, and nothing about who is running it."""
    import socket

    agent = sdc.PUBLISHER_USER_AGENT
    forbidden = {
        "the machine credential": TOKEN,
        "the host name": socket.gethostname(),
        "the FQDN": socket.getfqdn(),
        "the OS user": os.environ.get("USER") or "logplatform",
        "a client code": "BRAVO00016",
        "a second client code": "ALPHA00001",
        "a client id": CLIENT_ID,
        "the publisher endpoint": ENDPOINT,
    }
    for label, value in forbidden.items():
        if not value:
            continue
        check(f"the User-Agent does not contain {label}",
              value.lower() not in agent.lower(), agent)
    check("it is a single ASCII product token", agent.isascii() and " " not in agent)
    check("it names this application and a version",
          agent.startswith("log-platform-") and agent.count("/") == 1, agent)
    check("it is a module constant, so it cannot vary per request",
          isinstance(agent, str))

    # And the value that actually goes on the wire is that constant, for a
    # transport built against a different endpoint and a different credential.
    import urllib.request
    seen: list[str] = []
    original = urllib.request.urlopen

    def fake_urlopen(request, timeout=None):
        seen.append(dict(request.headers).get("User-agent", ""))
        return _FakeResponse(201, b'{"status":"PUBLISHED","next_action":"NONE"}')

    urllib.request.urlopen = fake_urlopen
    try:
        for endpoint, token in ((ENDPOINT, TOKEN),
                                ("https://other.example.invalid", TOKEN + "-2")):
            client = sdc.SecureDeliveryClient(
                sdc.HttpSecureDeliveryTransport(endpoint), publisher_token=token)
            client.publish(operation_id="op", subject_ref="s",
                           payload_digest="d" * 64, body=b"{}", period_type="weekly")
    finally:
        urllib.request.urlopen = original
    check("the wire value does not vary with endpoint or credential",
          set(seen) == {sdc.PUBLISHER_USER_AGENT}, str(seen))
    PASSED.append("the_user_agent_carries_no_secret_and_no_identity")


def test_a_403_is_a_definite_distinct_outcome_and_never_an_ambiguity() -> None:
    """THE measured Cloudflare defect, and the safety rule around it.

    An empty 403 from the edge used to arrive as `PROTOCOL_ERROR`, which is the
    same classification a publisher protocol defect gets. It now has its own
    code, and — the part that matters for safety — it is still a DEFINITE
    failure. `TransportOutcomeUnknown` means "the request may have committed and
    the answer was lost", and a received HTTP response is not that.
    """
    import urllib.error
    import urllib.request

    original = urllib.request.urlopen

    def forbidden_urlopen(request, timeout=None):
        # Exactly what the Cloudflare edge returned: a 403 with an empty body.
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {},
                                     _EmptyBody())

    urllib.request.urlopen = forbidden_urlopen
    try:
        client = sdc.SecureDeliveryClient(
            sdc.HttpSecureDeliveryTransport(ENDPOINT), publisher_token=TOKEN)
        for label, call in (
            ("publish", lambda: client.publish(operation_id="op", subject_ref="s",
                                               payload_digest="d" * 64, body=b"{}", period_type="weekly")),
            ("recover", lambda: client.recover(operation_id="op", period_type="weekly")),
            ("delivery", lambda: client.record_delivery(
                operation_id="op", phase=sdc.DELIVERY_PHASE_DELIVERED,
                capability_id="c" * 32)),
        ):
            try:
                call()
            except sdc.TransportOutcomeUnknown:
                raise AssertionError(
                    f"{label}: a 403 was classified as an AMBIGUOUS outcome")
            except sdc.SecureDeliveryError as error:
                check(f"{label}: 403 has its own definite code",
                      error.code == sdc.PUBLISHER_EDGE_FORBIDDEN, error.code)
                check(f"{label}: the observed status is preserved",
                      error.status == 403, str(error.status))
                check(f"{label}: it is no longer an anonymous protocol error",
                      "PROTOCOL_ERROR" not in str(error), str(error))
                check(f"{label}: the refusal names no credential",
                      TOKEN not in str(error) and TOKEN not in repr(error))
            else:
                raise AssertionError(f"{label}: a 403 was accepted as a result")
    finally:
        urllib.request.urlopen = original

    check("the edge outcome is a distinct member of the taxonomy",
          sdc.PUBLISHER_EDGE_FORBIDDEN not in {
              sdc.CONFLICT, sdc.UNKNOWN_OPERATION, sdc.OBJECT_UNREADABLE,
              sdc.OBJECT_INTEGRITY_FAILURE, sdc.NOT_RECOVERABLE, "PROTOCOL_ERROR"})
    check("the publisher application declares no 403 of its own",
          "403" not in read("delivery/driver_eco_dashboard/worker/lib/http.js")
          + read("delivery/driver_eco_dashboard/worker/index.js"))
    PASSED.append("a_403_is_a_definite_distinct_outcome_and_never_an_ambiguity")


class _EmptyBody:
    """The empty response body a Cloudflare edge 403 actually carries."""

    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        return None


def test_every_other_response_mapping_is_unchanged() -> None:
    """The 403 branch must not have moved anything else."""
    client = sdc.SecureDeliveryClient(_ScriptedTransport(), publisher_token=TOKEN)
    transport = client._transport  # noqa: SLF001 - the scripted seam under test

    transport.answer = (201, {"status": sdc.PUBLISHED,
                              "next_action": sdc.PERSIST_BEARER,
                              "capability": "Z" * 43, "capability_id": "c" * 32})
    outcome = client.publish(operation_id="op", subject_ref="s",
                             payload_digest="d" * 64, body=b"{}", period_type="weekly")
    check("201 PUBLISHED still maps to a result", outcome.status == sdc.PUBLISHED)

    transport.answer = (409, {"status": sdc.CONFLICT, "next_action": sdc.NONE,
                              "reason": "SUPERSEDED"})
    outcome = client.recover(operation_id="op", period_type="weekly")
    check("409 is still a definite non-raising conflict",
          outcome.status == sdc.CONFLICT and outcome.reason == "SUPERSEDED")

    transport.answer = (503, {"error": sdc.OBJECT_UNREADABLE})
    outcome = client.publish(operation_id="op", subject_ref="s",
                             payload_digest="d" * 64, body=b"{}", period_type="weekly")
    check("503 OBJECT_UNREADABLE still maps to retry-after-storage",
          outcome.status == sdc.OBJECT_UNREADABLE
          and outcome.next_action == sdc.RETRY_AFTER_STORAGE_RECOVERS)

    transport.answer = (404, {"error": "NOT_FOUND"})
    try:
        client.recover(operation_id="op", period_type="weekly")
    except sdc.SecureDeliveryError as error:
        check("404 is still a definite refusal", error.status == 404
              and error.code == "NOT_FOUND")
    else:
        raise AssertionError("404 must remain a definite refusal")

    for status in (500, 502, 503, 504):
        transport.answer = (status, {})
        try:
            client.publish(operation_id="op", subject_ref="s",
                           payload_digest="d" * 64, body=b"{}", period_type="weekly")
        except sdc.TransportOutcomeUnknown:
            continue
        except sdc.SecureDeliveryError as error:
            raise AssertionError(
                f"HTTP {status} must stay AMBIGUOUS, got {error.code}")
        raise AssertionError(f"HTTP {status} must stay ambiguous")

    transport.answer = (400, {"error": "BAD_REQUEST"})
    try:
        client.publish(operation_id="op", subject_ref="s",
                       payload_digest="d" * 64, body=b"{}", period_type="weekly")
    except sdc.SecureDeliveryError as error:
        check("an unmapped 4xx is still a definite refusal", error.status == 400)
        check("and it is NOT the edge classification",
              error.code != sdc.PUBLISHER_EDGE_FORBIDDEN, error.code)
    else:
        raise AssertionError("400 must remain a definite refusal")

    transport.raises = RuntimeError("connection reset")
    try:
        client.publish(operation_id="op", subject_ref="s",
                       payload_digest="d" * 64, body=b"{}", period_type="weekly")
    except sdc.TransportOutcomeUnknown:
        pass
    else:
        raise AssertionError("a transport-level failure must remain ambiguous")
    PASSED.append("every_other_response_mapping_is_unchanged")


class _ScriptedTransport:
    answer: tuple = (201, {})
    raises: Optional[BaseException] = None

    def post(self, path: str, headers: Mapping[str, str],
             body: Optional[bytes]):
        if self.raises is not None:
            raise self.raises
        return self.answer


def test_the_transport_still_retries_nothing_on_its_own() -> None:
    """Retry policy belongs to the host state machine; the UA change kept it there."""
    source = read("jobs/ecodriving_dashboard/secure_delivery_client.py")
    body = source[source.index("class HttpSecureDeliveryTransport"):]
    for forbidden in ("for attempt", "while True", "time.sleep", "retries", "backoff"):
        check("the transport performs no retry of its own",
              forbidden not in body, forbidden)
    publisher = read("jobs/ecodriving_dashboard/publisher.py")
    check("an ambiguous outcome still means 'ask the publisher, do not resend'",
          "except sdc.TransportOutcomeUnknown:" in publisher)
    check("a definite refusal still routes to an operator",
          "except sdc.SecureDeliveryError as error:" in publisher
          and "phase=\"PUBLICATION\"" in publisher)
    PASSED.append("the_transport_still_retries_nothing_on_its_own")


# ==============================================================================
# 2 — THE OPT-IN
# ==============================================================================


def test_without_the_flag_the_integration_is_off_however_configured() -> None:
    """The backward-compatibility contract, stated as configuration independence.

    A fully configured deployment — base URL, publisher endpoint, credential and
    an enabled rollout — must still produce the legacy path when the invocation
    did not ask for a dashboard.
    """
    with _NoDashboardEnv():
        os.environ[emi.ENV_DASHBOARD_BASE_URL] = BASE_URL
        os.environ[emi.ENV_PUBLISHER_ENDPOINT] = ENDPOINT
        os.environ[emi.ENV_PUBLISHER_TOKEN] = TOKEN
        for render_only in (False, True):
            for params in ({}, {"client_id": CLIENT_ID},
                           {"execution_mode": "normal_send"},
                           {"with_dashboard": False},
                           {"dashboard_link": False},
                           {"dashboard_base_url": BASE_URL,
                            "publisher_endpoint": ENDPOINT,
                            "publisher_token": TOKEN}):
                settings = emi.DashboardLinkSettings.from_params(
                    params, render_only=render_only)
                check("no opt-in means the legacy path", not settings.enabled,
                      f"{params} render_only={render_only}")
                check("and nothing was captured from the environment",
                      not settings.dashboard_base_url
                      and not settings.publisher_endpoint
                      and not settings.publisher_token)

        # Broken configuration cannot affect a default run either: it is not
        # read, so it cannot fail.
        os.environ[emi.ENV_PUBLISHER_ENDPOINT] = "http://localhost.attacker.invalid"
        settings = emi.DashboardLinkSettings.from_params({}, render_only=False)
        check("an unusable publisher endpoint does not disturb a default run",
              not settings.enabled)
        del os.environ[emi.ENV_DASHBOARD_BASE_URL]
        settings = emi.DashboardLinkSettings.from_params({}, render_only=False)
        check("nor does an absent one", not settings.enabled)
    PASSED.append("without_the_flag_the_integration_is_off_however_configured")


def test_a_disabled_service_publishes_nothing_and_renders_as_before() -> None:
    disabled = emi.EcoDashboardLinkService(
        settings=emi.DashboardLinkSettings(enabled=False),
        client_id=CLIENT_ID, client_code="ALPHA00001", schema="public",
        period_type="weekly", period_start_date=WEEK_START,
        period_end_date=WEEK_END_EXCLUSIVE, send_scope="normal",
        mailer=emi.MAILER_ECO_WEEKLY, read_conn=None)
    check("a disabled service resolves no period identity",
          disabled.current_identity is None)
    outcome = disabled.link_for(identity_key="DRIVER-A", recipient_email="a@b.invalid")
    check("and produces no link", outcome.status == emi.LinkStatus.DISABLED
          and not outcome.has_link and not outcome.blocks_send)

    template = "<html><body><table>{eco_dashboard_section_html}{eco_dashboard_link_html}</table></body></html>"
    rendered: list[dict] = []

    def render(context: dict) -> str:
        rendered.append(dict(context))
        return template.format(**context)

    for service in (None, disabled):
        body, link_outcome = emi.render_with_dashboard_link(
            service, identity_key="DRIVER-A", recipient_email="a@b.invalid",
            context={}, template_html=template, render=render)
        check("the message renders", body is not None)
        check("no dashboard URL is in the rendered content",
              "http" not in body and "eco.example.invalid" not in body, body)
        check("the placeholder resolved to nothing at all",
              body == "<html><body><table></table></body></html>", body)
        check("the outcome is DISABLED",
              link_outcome.status == emi.LinkStatus.DISABLED)
    check("the placeholder key is always supplied so rendering cannot break",
          all(emi.DASHBOARD_LINK_PLACEHOLDER in ctx for ctx in rendered))
    check("no counter recorded any dashboard work",
          disabled.summary()["dashboard_linked_count"] == 0
          and disabled.summary()["dashboard_ledger_connections_opened"] == 0)
    PASSED.append("a_disabled_service_publishes_nothing_and_renders_as_before")


def test_the_flag_makes_the_integration_mandatory_not_best_effort() -> None:
    with _NoDashboardEnv():
        for params in ({"with_dashboard": True}, {"dashboard_link": True}):
            try:
                emi.DashboardLinkSettings.from_params(params, render_only=False)
            except emi.DashboardIntegrationConfigurationError as error:
                check("an unconfigured opt-in stops the run, it does not degrade",
                      emi.ENV_PUBLISHER_ENDPOINT in str(error), str(error))
            else:
                raise AssertionError("an unconfigured opt-in must refuse")

        enabled = emi.DashboardLinkSettings.from_params(
            {"with_dashboard": True, "dashboard_base_url": BASE_URL,
             "publisher_endpoint": ENDPOINT, "publisher_token": TOKEN},
            render_only=False)
        check("the flag plus configuration enables the integration", enabled.enabled)
        check("an explicit false still wins",
              not emi.DashboardLinkSettings.from_params(
                  {"with_dashboard": False, "dashboard_base_url": BASE_URL,
                   "publisher_endpoint": ENDPOINT, "publisher_token": TOKEN},
                  render_only=False).enabled)
        try:
            emi.DashboardLinkSettings.from_params(
                {"with_dashboard": True, "dashboard_base_url": BASE_URL,
                 "publisher_endpoint": "http://localhost.attacker.invalid",
                 "publisher_token": TOKEN}, render_only=False)
        except emi.DashboardIntegrationConfigurationError:
            pass
        else:
            raise AssertionError("a loopback-lookalike endpoint must be refused")
    PASSED.append("the_flag_makes_the_integration_mandatory_not_best_effort")


# ==============================================================================
# 3 — THE CLIENT ROLLOUT GATE
# ==============================================================================


def test_the_production_declaration_enables_only_bravo00016() -> None:
    rollout = roll.load_rollout(roll.DEFAULT_ROLLOUT_PATH)
    check("ALPHA00001 dashboard mailing is DISABLED",
          not rollout.is_enabled("ALPHA00001"))
    check("BRAVO00016 dashboard mailing is ENABLED by owner decision",
          rollout.is_enabled("BRAVO00016"))
    check("and it is the ONLY client enabled today",
          rollout.enabled_clients == frozenset({"BRAVO00016"}),
          str(rollout.enabled_clients))
    check("both clients are DECLARED, so either decision is a one-line edit",
          {"ALPHA00001", "BRAVO00016"} <= rollout.declared_clients)
    for unknown in ("FOXTROT00001", "DELTA00001", "ECHO00001", "NEWCLIENT01",
                    "", "   ", None, "*", "ALL"):
        check("an undeclared or blank client defaults to DISABLED",
              not rollout.is_enabled(unknown), repr(unknown))
    check("the declaration is the repository file under review",
          rollout.source.endswith("ops/eco_dashboard_mailing_rollout.json"))
    PASSED.append("the_production_declaration_enables_only_bravo00016")


def test_a_wildcard_or_malformed_declaration_is_refused_never_widened() -> None:
    def refuse(document: Any, why: str, expected: str = roll.MALFORMED) -> None:
        try:
            roll.parse_rollout(document, source="synthetic")
        except roll.DashboardRolloutDeclarationError as error:
            check(f"{why} is refused with the right code",
                  error.code == expected, error.code)
        else:
            raise AssertionError(f"{why} was accepted")

    base = {"contract": roll.ROLLOUT_CONTRACT}
    for wildcard in ("*", "ALL", "all", "any", "default", "%"):
        refuse({**base, "clients": [{"client_code": wildcard, "enabled": True}]},
               f"a {wildcard!r} wildcard")
    refuse({**base, "clients": [{"client_code": "X1", "enabled": True},
                                {"client_code": "x1", "enabled": False}]},
           "a duplicated client")
    refuse({**base, "clients": [{"client_code": "X1", "enabled": 1}]},
           "a non-boolean enabled")
    refuse({**base, "clients": [{"client_code": "X1", "enabled": "true"}]},
           "a stringly-typed enabled")
    refuse({**base, "clients": [{"client_code": "X1", "enabeld": True}]},
           "a misspelled key")
    refuse({**base, "clients": [{"client_code": "X1"}]}, "an incomplete entry")
    refuse({**base, "clients": [{"client_code": "", "enabled": True}]},
           "an empty client code")
    refuse({**base, "clients": {}}, "a non-list clients value")
    refuse({**base}, "an absent clients list")
    refuse({"contract": "something-else/1", "clients": []}, "a wrong contract")
    refuse({**base, "clients": [], "enabled": True}, "an unknown top-level key")
    refuse([], "a non-object declaration")

    with tempfile.TemporaryDirectory() as directory:
        broken = Path(directory) / "broken.json"
        broken.write_text('{"contract": "eco-dash', encoding="utf-8")
        try:
            roll.load_rollout(broken)
        except roll.DashboardRolloutDeclarationError as error:
            check("readable-but-invalid JSON is MALFORMED, not a filesystem fault",
                  error.code == roll.MALFORMED, error.code)
        else:
            raise AssertionError("truncated JSON was accepted")

        missing = Path(directory) / "absent.json"
        try:
            roll.load_rollout(missing)
        except roll.DashboardRolloutDeclarationError as error:
            check("an absent declaration is UNREADABLE and still a refusal",
                  error.code == roll.UNREADABLE, error.code)
        else:
            raise AssertionError("an absent declaration was accepted")
    PASSED.append("a_wildcard_or_malformed_declaration_is_refused_never_widened")


def _enabled_rollout(*client_codes: str) -> roll.DashboardMailingRollout:
    """A synthetic rollout override, in memory, that enables named clients."""
    return roll.parse_rollout(
        {"contract": roll.ROLLOUT_CONTRACT,
         "clients": [{"client_code": code, "enabled": True} for code in client_codes]},
        source="synthetic-test-rollout")


def test_the_flag_alone_is_never_sufficient() -> None:
    """The two-condition invariant, and the fact that it stops before effects."""
    import smtplib
    import urllib.request

    settings = emi.DashboardLinkSettings(
        enabled=True, dashboard_base_url=BASE_URL, publisher_endpoint=ENDPOINT,
        publisher_token=TOKEN)

    original_urlopen = urllib.request.urlopen
    original_smtp = smtplib.SMTP
    original_smtp_ssl = smtplib.SMTP_SSL
    attempted: list[str] = []

    def refusing_urlopen(*args, **kwargs):  # pragma: no cover - must not run
        attempted.append("http")
        raise AssertionError("a publisher request was attempted")

    class RefusingSMTP:  # pragma: no cover - must not run
        def __init__(self, *args, **kwargs):
            attempted.append("smtp")
            raise AssertionError("an SMTP connection was attempted")

    urllib.request.urlopen = refusing_urlopen
    smtplib.SMTP = RefusingSMTP
    smtplib.SMTP_SSL = RefusingSMTP
    try:
        for client_code in ("ALPHA00001", "FOXTROT00001",
                            "BRAND-NEW-CLIENT", "", None):
            try:
                emi.authorize_dashboard_mailing(settings, client_code=client_code)
            except roll.DashboardMailingNotEnabled as error:
                check("the refusal names the rollout, not a credential",
                      error.code == roll.NOT_ENABLED
                      and TOKEN not in str(error) and ENDPOINT not in str(error),
                      str(error))
                check("and it tells the operator what to do instead",
                      "--with-dashboard" in str(error), str(error))
            else:
                raise AssertionError(
                    f"{client_code!r} was authorised for dashboard mailing")

        # The enabled half against the PRODUCTION declaration: the flag plus a
        # rollout that names the client is the only combination that passes, and
        # authorisation still reaches no publisher request and no SMTP socket.
        check("the one enabled client is authorised by the production declaration",
              emi.authorize_dashboard_mailing(
                  settings, client_code="BRAVO00016") == "BRAVO00016")

        # A run that never asked reads no declaration at all — proved by
        # pointing the loader at a path that would refuse if it were consulted.
        with _NoDashboardEnv():
            os.environ[roll.ENV_ROLLOUT_FILE] = "/nonexistent/rollout.json"
            check("a legacy run needs no declaration",
                  emi.authorize_dashboard_mailing(
                      emi.DashboardLinkSettings(enabled=False),
                      client_code="ALPHA00001") is None)

        # And the enabled half of the invariant, via a synthetic override.
        for client_code in ("ALPHA00001", "BRAVO00016"):
            granted = emi.authorize_dashboard_mailing(
                settings, client_code=client_code,
                rollout=_enabled_rollout(client_code))
            check("an enabled client is authorised", granted == client_code)
            try:
                emi.authorize_dashboard_mailing(
                    settings, client_code="SOMEONE-ELSE",
                    rollout=_enabled_rollout(client_code))
            except roll.DashboardMailingNotEnabled:
                pass
            else:
                raise AssertionError("one enabled client enabled another")
    finally:
        urllib.request.urlopen = original_urlopen
        smtplib.SMTP = original_smtp
        smtplib.SMTP_SSL = original_smtp_ssl

    check("no publisher request and no SMTP connection was attempted", not attempted)
    PASSED.append("the_flag_alone_is_never_sufficient")


def test_the_gate_runs_before_anything_that_could_have_an_effect() -> None:
    """Ordering, read out of the four jobs themselves."""
    import ast

    for path in ECO_JOBS:
        source = read(path)
        check("the job calls the rollout gate",
              "authorize_dashboard_mailing(" in source, path)
        gate = source.index("    authorize_dashboard_mailing(")
        check("the gate runs before the dashboard service is constructed",
              gate < source.index("EcoDashboardLinkService("), path)
        check("the gate runs before any candidate is processed",
              gate < source.index("for row in candidates:"), path)
        for effect, label in (("reserve_send(", "a send-log reservation"),
                              ("smtp", "anything SMTP-shaped")):
            index = source.lower().find(effect.lower(), gate)
            check(f"the gate runs before {label}", index == -1 or gate < index, path)

        tree = ast.parse(source)
        run = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "run")
        calls = [ast.unparse(node.func) for node in ast.walk(run)
                 if isinstance(node, ast.Call)]
        check("the gate is called exactly once per run",
              calls.count("authorize_dashboard_mailing") == 1, path)
        check("the opt-in is resolved exactly once per run",
              calls.count("DashboardLinkSettings.from_params") == 1, path)
    PASSED.append("the_gate_runs_before_anything_that_could_have_an_effect")


def test_each_mailing_variant_selects_the_integration_when_both_conditions_hold() -> None:
    """Requirement G: the future enabled path, for all four command variants.

    No Cloudflare publication and no SMTP send: the service is CONSTRUCTED, which
    is the point at which the existing dashboard integration path is selected,
    and the rendered message is proved to carry the link.
    """
    with _NoDashboardEnv():
        for label, client_code, period_type, mailer, start, end in MAILING_VARIANTS:
            settings = emi.DashboardLinkSettings.from_params(
                {"with_dashboard": True, "dashboard_base_url": BASE_URL,
                 "publisher_endpoint": ENDPOINT, "publisher_token": TOKEN},
                render_only=False)
            check(f"{label}: the flag plus configuration enables it", settings.enabled)

            granted = emi.authorize_dashboard_mailing(
                settings, client_code=client_code,
                rollout=_enabled_rollout(client_code))
            check(f"{label}: the synthetic rollout authorises it",
                  granted == client_code)

            service = emi.EcoDashboardLinkService(
                settings=settings, client_id=CLIENT_ID, client_code=client_code,
                schema="public", period_type=period_type, period_start_date=start,
                period_end_date=end, send_scope="normal", mailer=mailer,
                read_conn=None)
            check(f"{label}: the integration path is selected", service.enabled)
            check(f"{label}: it bound this variant's mailer identity",
                  service.mailer == mailer)
            check(f"{label}: it verified the job's own period",
                  service.current_identity is not None
                  and service.current_identity.period_start_date == start)
            check(f"{label}: it resolved this client's pipeline family",
                  service.family is not None)
            check(f"{label}: no ledger connection was opened by construction",
                  service.summary()["dashboard_ledger_connections_opened"] == 0)
            service.close()

            # The existing template path receives the link.
            template = "<html><body><table>{eco_dashboard_section_html}{eco_dashboard_link_html}</table></body></html>"
            capability_url = BASE_URL + "#k=" + "Q" * 43
            body, outcome = emi.render_with_dashboard_link(
                _ScriptedLinkService(capability_url),
                identity_key="SUBJECT-1", recipient_email="a@b.invalid",
                context={}, template_html=template,
                render=lambda ctx: template.format(**ctx))
            check(f"{label}: the message is sent and carries the link",
                  body is not None and capability_url in body)
            check(f"{label}: and the link status is LINKED",
                  outcome.status == emi.LinkStatus.LINKED)
    PASSED.append("each_mailing_variant_selects_the_integration_when_both_conditions_hold")


class _ScriptedLinkService:
    """An `EcoDashboardLinkService` stand-in with one scripted answer."""

    enabled = True

    def __init__(self, capability_url: str):
        self._url = capability_url

    def link_for(self, *, identity_key: str, recipient_email: str):
        return emi.DashboardLinkOutcome(
            emi.LinkStatus.LINKED, capability_url=self._url,
            snapshot_status="OK", delivery_state="EXTERNAL_MAILER_HANDOFF",
            operation_id="op-1")

    def template_placement_invalid(self, detail: str):  # pragma: no cover
        raise AssertionError(detail)

    def template_link_missing(self, outcome):  # pragma: no cover
        raise AssertionError("the rendered template lost the link")


def test_the_eco_mailer_remains_the_smtp_authority() -> None:
    import ast

    def imported_modules(path: str) -> set:
        """What the module actually IMPORTS. Prose saying "never touches X" must
        not be able to satisfy — or defeat — a check about X."""
        names = set()
        for node in ast.walk(ast.parse(read(path))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
        return names

    integration = imported_modules("jobs/ecodriving_dashboard/eco_mailing_integration.py")
    for forbidden in ("smtplib", "jobs.ecodriving_dashboard.email_provider"):
        check("the integration owns no SMTP lifecycle",
              not any(name == forbidden or name.startswith(forbidden + ".")
                      for name in integration), forbidden)
    rollout_imports = imported_modules("jobs/ecodriving_dashboard/dashboard_rollout.py")
    for forbidden in ("smtplib", "urllib", "psycopg", "psycopg2", "requests",
                      "socket", "http"):
        check("the rollout gate contacts nothing at all",
              not any(name == forbidden or name.startswith(forbidden + ".")
                      for name in rollout_imports), forbidden)
    for path in ECO_JOBS:
        source = read(path)
        check("the Eco job still owns the send", "smtp" in source.lower(), path)
        check("the Eco send log is still the send authority",
              "send_log" in source, path)
    check("ambiguous SMTP remains operator-action-required",
          "AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION" in read(
              "jobs/ecodriving/email_idempotency.py"))
    PASSED.append("the_eco_mailer_remains_the_smtp_authority")


# ==============================================================================
# 4 — THE COMMAND SURFACE AND THE SCHEDULERS
# ==============================================================================


def test_the_four_mailing_commands_accept_the_flag_and_nothing_else_does() -> None:
    import importlib

    runner = importlib.import_module("ops.runner")
    check("the flag is spelled exactly once, in one place",
          runner.WITH_DASHBOARD_FLAG == "--with-dashboard")
    check("it covers exactly the four existing mailing commands",
          set(runner.ECO_MAILING_JOB_MODULES) == {
              "jobs.ecodriving.job_eco_driving_weekly_email_notifications",
              "jobs.ecodriving.job_eco_driving_monthly_email_notifications",
              "jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications",
              "jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications",
          })
    check("the runner's parameter name matches the job's",
          runner.WITH_DASHBOARD_PARAM == emi.PARAM_WITH_DASHBOARD)

    for module in runner.ECO_MAILING_JOB_MODULES:
        params = runner._apply_options(module, {"client_id": CLIENT_ID},
                                       [runner.WITH_DASHBOARD_FLAG])
        check("the flag sets the opt-in parameter",
              params[emi.PARAM_WITH_DASHBOARD] is True, module)
        check("and changes nothing else", params["client_id"] == CLIENT_ID)
        untouched = runner._apply_options(module, {"client_id": CLIENT_ID}, [])
        check("its absence leaves the parameters exactly as given",
              untouched == {"client_id": CLIENT_ID}, module)
        check("so a default invocation carries no dashboard parameter at all",
              emi.PARAM_WITH_DASHBOARD not in untouched)

    for module in ("jobs.ecodriving.job_eco_driving_aggregate",
                   "jobs.api.telematics.dispatcher", "jobs.reports.demo"):
        try:
            runner._apply_options(module, {}, [runner.WITH_DASHBOARD_FLAG])
        except runner.RunnerUsageError:
            pass
        else:
            raise AssertionError(f"{module} accepted a mailing-only option")

    try:
        runner._apply_options(runner.ECO_MAILING_JOB_MODULES[0], {},
                              ["--with-dashboards"])
    except runner.RunnerUsageError:
        pass
    else:
        raise AssertionError("a mistyped option was silently ignored")

    positional, options = runner._split_options(
        ["jobs.x", '{"a":1}', "--with-dashboard"])
    check("options are separated from the positional contract",
          positional == ["jobs.x", '{"a":1}']
          and options == ["--with-dashboard"])
    positional, options = runner._split_options(
        ["jobs.x", "--with-dashboard", '{"a":1}'])
    check("and the flag may appear on either side of the params",
          positional == ["jobs.x", '{"a":1}'] and options == ["--with-dashboard"])
    PASSED.append("the_four_mailing_commands_accept_the_flag_and_nothing_else_does")


def test_only_a_declared_scheduled_mailing_fire_can_carry_the_flag() -> None:
    """Requirement: THE production execution contract, and nothing wider.

    This replaces the earlier "no scheduled invocation may ever carry the flag".
    That statement was correct while no client was authorized for scheduled
    dashboard mailing; the owner decision for BRAVO00016 weekly changed it, and
    a weekly production mail whose dashboard depended on a human remembering to
    type an option is not an execution contract. What the invariant becomes is
    NARROWER, not weaker: a scheduled fire may carry the option only when a
    reviewed declaration names that exact (client, dataset) pair AND the rollout
    declaration independently enables that client — and the dispatcher still
    spells no option itself.
    """
    import ast

    dispatcher = read("jobs/api/telematics/dispatcher.py")
    launcher = next(node for node in ast.walk(ast.parse(dispatcher))
                    if isinstance(node, ast.FunctionDef) and node.name == "_launch_job")
    command = next(node.value for node in ast.walk(launcher)
                   if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "cmd" for t in node.targets))
    elements = [ast.unparse(element) for element in command.elts]
    check("the scheduled command is <python> <runner> <module> <params>, plus "
          "only the options the contract resolved",
          elements == ["_resolve_python_executable()", "'ops/runner.py'",
                       "job_module", "json.dumps(job_params)",
                       "*runner_options"], str(elements))
    check("no option is spelled as a literal in the command",
          not any(element.strip("'\"").startswith("--") for element in elements),
          str(elements))
    check("the dispatcher still names no option anywhere",
          "with-dashboard" not in dispatcher and "with_dashboard" not in dispatcher)
    check("and it refuses any option outside the declared allowlist",
          "ALLOWED_SCHEDULED_RUNNER_OPTIONS" in dispatcher
          and "scheduled fire may not carry option(s)" in dispatcher)

    check("the two scheduled Eco mailing datasets still carry no dataset-mode "
          "parameters of their own",
          '"eco_person_driving_weekly_email_notifications": {},' in dispatcher
          and '"eco_person_driving_monthly_email_notifications": {},' in dispatcher)

    scanned = 0
    for directory, patterns in (("ops/systemd", ("*.service", "*.timer", "*.sh")),
                                ("ops/systemd/proposed", ("*", )),
                                ("scripts", ("*.py", "*.sh"))):
        base = REPO_ROOT / directory
        if not base.exists():
            continue
        for pattern in patterns:
            for path in base.glob(pattern):
                if not path.is_file():
                    continue
                scanned += 1
                text = path.read_text(encoding="utf-8", errors="replace")
                check("no unit, timer or shell script opts in",
                      "with-dashboard" not in text and "with_dashboard" not in text,
                      str(path))
    check("something was actually scanned", scanned > 0)

    for path in ECO_JOBS:
        source = read(path)
        check("no job enables the dashboard for itself",
              "with_dashboard=True" not in source
              and "dashboard_link=True" not in source, path)
    PASSED.append("only_a_declared_scheduled_mailing_fire_can_carry_the_flag")


def test_the_rollout_declaration_is_the_only_thing_that_has_to_change() -> None:
    """Enabling BRAVO00016 later must not touch mailing business logic."""
    declaration = json.loads(read("ops/eco_dashboard_mailing_rollout.json"))
    entries = {entry["client_code"]: entry for entry in declaration["clients"]}
    check("BRAVO00016 was enabled by a value change and nothing else",
          "BRAVO00016" in entries and entries["BRAVO00016"]["enabled"] is True)
    check("ALPHA00001 likewise, so its disablement is reversible by decision",
          "ALPHA00001" in entries and entries["ALPHA00001"]["enabled"] is False)

    for path in ECO_JOBS + ("jobs/ecodriving_dashboard/eco_mailing_integration.py",):
        source = read(path)
        for client_code in ("BRAVO00016", "ALPHA00001"):
            index = source.find(client_code)
            check("no mailing logic decides the rollout by client code",
                  index == -1 or "rollout" not in source[max(0, index - 400):index + 400].lower(),
                  f"{path}:{client_code}")

    # Flipping the declared value is sufficient, proved against a copy.
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "rollout.json"
        declaration["clients"] = [
            {**entry, "enabled": entry["client_code"] == "BRAVO00016"}
            for entry in declaration["clients"]]
        target.write_text(json.dumps(declaration), encoding="utf-8")
        flipped = roll.load_rollout(target)
        check("one value change enables BRAVO00016", flipped.is_enabled("BRAVO00016"))
        check("and enables nobody else",
              flipped.enabled_clients == frozenset({"BRAVO00016"}))
    check("the production declaration was not touched",
          roll.load_rollout(roll.DEFAULT_ROLLOUT_PATH).enabled_clients
          == frozenset({"BRAVO00016"}))
    PASSED.append("the_rollout_declaration_is_the_only_thing_that_has_to_change")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"PASSED {len(PASSED)} groups, {len(tests)} tests")
    for name in PASSED:
        print(f"  ok  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
