#!/usr/bin/env python3
"""Focused tests for the hardening campaign runner.

Pure: no database, no child process. Every suite launch and every database
connection is injected, so what is asserted is the runner's own decision logic —
which is the part that must not be able to report a green campaign for a run
that skipped its work.

Set no environment variable. This suite never connects to anything.
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.tests_manual import (  # noqa: E402
    run_telematics_onboarding_hardening_campaign as campaign,
)

FAILURES: list = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
    print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


#: One synthetic suite for driving the runner's decision logic. It names a real
#: file — the existence check is part of what is under test and must not be
#: bypassed — but the launch itself is always injected, so that file is never
#: actually executed by these tests.
ONE_SUITE = campaign.Suite(
    module="test_postgres_dsn_safety",
    markers=("OK - example passed", "PASS: disposable PostgreSQL example"),
    dsn_envs=("EXAMPLE_TEST_DSN",),
    destructive=True,
)

GOOD_STDOUT = "PASS: disposable PostgreSQL example\nOK - example passed\n"
LOOPBACK_ENV = {"EXAMPLE_TEST_DSN": "host=127.0.0.1 port=5432 dbname=x"}


def _classify(**kwargs):
    base = dict(returncode=0, stdout=GOOD_STDOUT, stderr="")
    base.update(kwargs)
    return campaign.classify_result(ONE_SUITE, **base)


# ---------------------------------------------------------------------------
# The required suite set
# ---------------------------------------------------------------------------

print("\n-- required suite set --")

_check(
    "the campaign enumerates exactly 27 suites",
    len(campaign.REQUIRED_SUITES) == campaign.REQUIRED_SUITE_COUNT == 27,
    f"{len(campaign.REQUIRED_SUITES)}",
)
_check(
    "every enumerated suite file exists",
    all((ROOT / s.path).exists() for s in campaign.REQUIRED_SUITES),
    str([s.module for s in campaign.REQUIRED_SUITES
         if not (ROOT / s.path).exists()]),
)
_check(
    "every enumerated suite declares at least one terminal marker",
    all(s.markers for s in campaign.REQUIRED_SUITES),
)
_check(
    "no suite is listed twice",
    len({s.module for s in campaign.REQUIRED_SUITES})
    == len(campaign.REQUIRED_SUITES),
)
_check(
    "verify_suite_set accepts the declared set",
    campaign.verify_suite_set() is None,
)

try:
    campaign.verify_suite_set(campaign.REQUIRED_SUITES[:-1])
except campaign.CampaignError as exc:
    _check(
        "a shortened suite set is refused",
        exc.code == "CAMPAIGN_SUITE_SET_CHANGED", exc.code,
    )
else:
    _check("a shortened suite set is refused", False, "accepted")

try:
    campaign.verify_suite_set(
        tuple(campaign.REQUIRED_SUITES[:-1])
        + (campaign.Suite(module="does_not_exist", markers=("x",)),)
    )
except campaign.CampaignError as exc:
    _check(
        "a suite whose file is absent is refused",
        exc.code == "CAMPAIGN_SUITE_MISSING", exc.code,
    )
else:
    _check("a missing suite file is refused", False, "accepted")


# ---------------------------------------------------------------------------
# Missing DSNs fail before the campaign runs
# ---------------------------------------------------------------------------

print("\n-- required DSNs --")

try:
    campaign.require_all_dsns({}, [ONE_SUITE])
except campaign.CampaignError as exc:
    _check(
        "a missing required DSN fails before campaign execution",
        exc.code == "CAMPAIGN_MISSING_DSN" and "EXAMPLE_TEST_DSN" in str(exc),
        str(exc),
    )
else:
    _check("a missing required DSN fails", False, "accepted")

try:
    campaign.require_all_dsns({"EXAMPLE_TEST_DSN": "   "}, [ONE_SUITE])
except campaign.CampaignError as exc:
    _check(
        "a blank DSN counts as missing",
        exc.code == "CAMPAIGN_MISSING_DSN", exc.code,
    )
else:
    _check("a blank DSN counts as missing", False, "accepted")

_check(
    "a present DSN is accepted",
    campaign.require_all_dsns(LOOPBACK_ENV, [ONE_SUITE])
    == {"EXAMPLE_TEST_DSN": "host=127.0.0.1 port=5432 dbname=x"},
)
_check(
    "the real campaign declares every DSN its suites read",
    set(campaign.required_dsn_envs()) == {
        name for suite in campaign.REQUIRED_SUITES for name in suite.dsn_envs
    },
)


# ---------------------------------------------------------------------------
# A destructive non-loopback DSN fails before any child launch
# ---------------------------------------------------------------------------

print("\n-- destructive DSN safety --")

launched: list = []


def _recording_runner(suite, env, root):
    launched.append(suite.module)
    return SimpleNamespace(returncode=0, stdout=GOOD_STDOUT, stderr="")


try:
    campaign.validate_destructive_dsns(
        {"EXAMPLE_TEST_DSN": "host=203.0.113.10 port=5432 dbname=x"},
        [ONE_SUITE],
    )
except campaign.CampaignError as exc:
    _check(
        "a destructive non-loopback DSN is refused",
        exc.code == "CAMPAIGN_UNSAFE_DSN", exc.code,
    )
else:
    _check("a destructive non-loopback DSN is refused", False, "accepted")

# And the refusal happens before any suite is launched.
try:
    campaign.run_campaign(
        {"EXAMPLE_TEST_DSN": "host=203.0.113.10 port=5432 dbname=x"},
        suites=(ONE_SUITE,), expected_count=1,
        runner=_recording_runner, skip_cleanup=True,
    )
except campaign.CampaignError as exc:
    _check(
        "no child process is launched when a destructive DSN is unsafe",
        launched == [] and exc.code in (
            "CAMPAIGN_UNSAFE_DSN", "CAMPAIGN_MISSING_DSN",
        ),
        f"launched={launched} code={exc.code}",
    )
else:
    _check("an unsafe destructive DSN aborts the campaign", False, "ran")

_check(
    "a loopback destructive DSN is accepted",
    bool(campaign.validate_destructive_dsns(LOOPBACK_ENV, [ONE_SUITE])),
)


# ---------------------------------------------------------------------------
# Per-suite classification
# ---------------------------------------------------------------------------

print("\n-- suite classification --")

_check(
    "a clean suite passes",
    _classify().passed,
)

skipped = _classify(
    stdout=f"{campaign.SKIP_MARKER} set EXAMPLE_TEST_DSN to a disposable DSN\n"
)
_check(
    "suite output containing a skip marker fails even with exit code 0",
    not skipped.passed
    and any("declined a required section" in f for f in skipped.failures),
    str(skipped.failures),
)

skipped_on_stderr = _classify(
    stdout=GOOD_STDOUT, stderr=f"{campaign.SKIP_MARKER} no DSN\n"
)
_check(
    "a skip marker on stderr fails too",
    not skipped_on_stderr.passed, str(skipped_on_stderr.failures),
)

missing_db_marker = _classify(stdout="OK - example passed\n")
_check(
    "a missing DB completion marker fails",
    not missing_db_marker.passed
    and any("PASS: disposable PostgreSQL example" in f
            for f in missing_db_marker.failures),
    str(missing_db_marker.failures),
)

nonzero = _classify(returncode=1)
_check(
    "a non-zero suite exit fails",
    not nonzero.passed and any("exit code 1" in f for f in nonzero.failures),
    str(nonzero.failures),
)

unlaunchable = _classify(returncode=None, stdout="", stderr="OSError: boom")
_check(
    "a suite that could not be launched fails",
    not unlaunchable.passed, str(unlaunchable.failures),
)

# The two conditions are independent: a suite may exit 0 and still fail.
_check(
    "exit code 0 alone is never sufficient",
    not _classify(stdout="", returncode=0).passed,
)


# ---------------------------------------------------------------------------
# End-to-end with injected suites
# ---------------------------------------------------------------------------

print("\n-- campaign outcome --")


def _campaign_with(outputs, *, cleanup_failures=None):
    """Run the campaign over one synthetic suite with a scripted child result."""
    calls = []

    def runner(suite, env, root):
        calls.append(suite.module)
        return SimpleNamespace(**outputs)

    def connect(dsn, autocommit=False):
        class _Conn:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def execute(self_inner, statement):
                if cleanup_failures:
                    raise RuntimeError("cleanup exploded")
                return None

        return _Conn()

    # The nested run prints its own summary, which would reproduce the skip
    # marker in *this* suite's stdout — and this suite is itself one of the 27
    # the campaign scans. Its output is captured and discarded.
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        outcome = campaign.run_campaign(
            dict(LOOPBACK_ENV), suites=(ONE_SUITE,), expected_count=1,
            runner=runner, connect=connect,
        )
    return outcome, calls


(exit_code, results), calls = _campaign_with(
    {"returncode": 0, "stdout": GOOD_STDOUT, "stderr": ""}
)
_check(
    "a successful suite produces a green campaign",
    exit_code == 0 and calls == ["test_postgres_dsn_safety"],
    f"exit={exit_code} calls={calls}",
)
summary = campaign.format_summary(results)
_check(
    "a successful suite produces a deterministic summary",
    "PASS" in summary and "Suites: 1/1 passed" in summary
    and summary == campaign.format_summary(results),
    summary,
)

(exit_code, results), _ = _campaign_with(
    {"returncode": 0, "stdout": f"{campaign.SKIP_MARKER} no DSN\n", "stderr": ""}
)
_check(
    "a skipped suite produces a non-zero campaign result",
    exit_code != 0, f"exit={exit_code}",
)
_check(
    "the summary names the declined section",
    "declined a required section" in campaign.format_summary(results),
)

(exit_code, _), _ = _campaign_with(
    {"returncode": 3, "stdout": GOOD_STDOUT, "stderr": ""}
)
_check(
    "a non-zero suite exit produces a non-zero campaign result",
    exit_code != 0, f"exit={exit_code}",
)

(exit_code, _), _ = _campaign_with(
    {"returncode": 0, "stdout": "OK - example passed\n", "stderr": ""}
)
_check(
    "a missing DB marker produces a non-zero campaign result",
    exit_code != 0, f"exit={exit_code}",
)

(exit_code, _), _ = _campaign_with(
    {"returncode": 0, "stdout": GOOD_STDOUT, "stderr": ""},
    cleanup_failures=True,
)
_check(
    "a cleanup failure produces a non-zero campaign result",
    exit_code != 0, f"exit={exit_code}",
)


# ---------------------------------------------------------------------------
# Cleanup safety
# ---------------------------------------------------------------------------

print("\n-- cleanup safety --")

attempted: list = []


def _tracking_connect(dsn, autocommit=False):
    attempted.append(dsn)

    class _Conn:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *exc):
            return False

        def execute(self_inner, statement):
            return None

    return _Conn()


failures = campaign.cleanup_disposable_resources(
    {"EXAMPLE_TEST_DSN": "host=203.0.113.10 dbname=x"},
    suites=[ONE_SUITE], connect=_tracking_connect,
)
_check(
    "cleanup refuses a non-loopback DSN without connecting",
    attempted == [] and failures and "refused before cleanup" in failures[0],
    f"attempted={attempted} failures={failures}",
)

attempted.clear()
failures = campaign.cleanup_disposable_resources(
    LOOPBACK_ENV, suites=[ONE_SUITE], connect=_tracking_connect,
)
_check(
    "cleanup connects once to a loopback DSN and reports no failure",
    failures == [] and len(attempted) == 1,
    f"attempted={attempted} failures={failures}",
)


# ---------------------------------------------------------------------------
# The guard probe and py_compile
# ---------------------------------------------------------------------------

print("\n-- guard probe and compilation --")

_check(
    "the non-loopback guard probe passes",
    campaign.run_non_loopback_guard_probe() is None,
)
_check(
    "py_compile accepts every declared target",
    campaign.run_py_compile() is None,
)
_check(
    "the campaign contains no production credential or hostname",
    not any(
        token in (ROOT / "ops/tests_manual"
                  / "run_telematics_onboarding_hardening_campaign.py").read_text(
            encoding="utf-8"
        )
        for token in ("password=", "logdb", "BRAVO00016", "ALPHA00001",
                      "DELTA00001", "FOXTROT00001", "ECHO00001")
    ),
)


# ---------------------------------------------------------------------------

print("")
if FAILURES:
    print(f"FAIL — {len(FAILURES)} check(s) failed:")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("OK - Telematics onboarding hardening campaign runner checks passed")
