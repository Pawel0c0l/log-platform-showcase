#!/usr/bin/env python3
"""The authoritative campaign for the Telematics onboarding hardening stack.

WHY THIS EXISTS.
    Several suites in this directory print `SKIP:` and return success when their
    DSN is absent. That is correct for standalone use — a developer without a
    disposable PostgreSQL should not see a red failure — but it means a campaign
    that simply runs them all and checks exit codes can report 27/27 green while
    every database-backed section silently did nothing. That is precisely the
    "return code 0 is not evidence" failure this whole stack exists to close, so
    the campaign must not reproduce it.

    This runner is therefore the authority. It requires every DSN explicitly,
    validates every destructive one as loopback-only *before* launching a child,
    and treats a `SKIP:` line, a missing terminal marker or a non-zero exit as a
    campaign failure. The individual suites keep their standalone skip behavior
    unchanged.

WHAT IT IS NOT.
    Not part of CI, not a production runtime dependency, and not a Docker
    dependency. It assumes the disposable PostgreSQL 16 databases already exist
    and are reachable on loopback; how they were created — a container, a local
    cluster — is the operator's choice.

USAGE.
    Export one DSN per required environment variable (see `required_dsn_envs()`
    or run with `--list`), then:

        cd /opt/log-platform
        PYTHONPATH="$PWD" .venv/bin/python \\
          ops/tests_manual/run_telematics_onboarding_hardening_campaign.py

    Exit code 0 means every required suite ran, reached its database sections,
    and passed. Anything else is a failure, including a cleanup failure.
"""
from __future__ import annotations

import argparse
import os
import py_compile
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    URI_SCHEMES,
    UnsafeDsnError,
    require_loopback_dsn,
)

SUITE_DIR = Path("ops/tests_manual")

#: A line anywhere in a required suite's output that means it declined to run a
#: section. Never acceptable in the campaign.
SKIP_MARKER = "SKIP:"


class CampaignError(RuntimeError):
    """A campaign-level refusal, raised before or instead of running suites."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Suite:
    """One required suite and what proves it actually ran.

    `markers` are substrings that must all appear in the suite's stdout. For a
    database-backed suite at least one of them is the marker the suite prints
    *only after* its disposable-PostgreSQL section completed, which is what
    makes "the DB section ran" checkable rather than assumed.
    """

    module: str
    markers: Tuple[str, ...]
    dsn_envs: Tuple[str, ...] = ()
    #: True when the suite creates, drops or writes anything. Its DSNs are
    #: validated as loopback-only before any child process is launched.
    destructive: bool = False
    #: DSN variables this suite parses as a `postgresql://` URI rather than
    #: handing straight to `psycopg.connect`. Both forms are valid libpq input,
    #: but a suite that calls `urlsplit` on a keyword DSN silently reads an
    #: empty host and then fails deep inside a connection attempt, so the form
    #: is declared and checked here instead of being discovered that way.
    uri_form_dsn_envs: Tuple[str, ...] = ()

    @property
    def path(self) -> Path:
        return SUITE_DIR / f"{self.module}.py"

    @property
    def database_backed(self) -> bool:
        return bool(self.dsn_envs)


# ---------------------------------------------------------------------------
# The exact required set
# ---------------------------------------------------------------------------
#
# Twenty-seven suites. Sixteen are database-backed and destructive; eleven are
# pure. The list is explicit and exact — there is no directory glob — so a suite
# that is renamed or deleted fails the campaign instead of quietly leaving it.
#
# Four of the database-backed suites were classified as pure until this runner
# was first executed: they read a DSN variable nobody had exported, printed
# `SKIP:` and exited 0. That is exactly the silent pass this campaign exists to
# make impossible, and finding them is what the skip check is for.

REQUIRED_SUITES: Tuple[Suite, ...] = (
    # --- database-backed, destructive -------------------------------------
    Suite(
        module="test_telematics_recovery_execution_path_postgres",
        markers=("OK - Telematics recovery execution-path checks passed",),
        dsn_envs=(
            "TELEMATICS_EXECUTION_PATH_TEST_DSN",
            "TELEMATICS_EXECUTION_PATH_BUSINESS_DSN",
        ),
        destructive=True,
    ),
    Suite(
        module="test_telematics_schedule_activation_postgres",
        markers=("OK - Telematics schedule activation PostgreSQL checks passed",),
        dsn_envs=("TELEMATICS_SCHEDULE_ACTIVATION_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_workflow_a_onboarding_state_machine",
        markers=("OK - Workflow A onboarding state-machine checks passed",),
        dsn_envs=("TELEMATICS_ONBOARDING_STATE_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_telematics_cold_start_recovery_postgres",
        markers=("OK - Telematics cold-start recovery PostgreSQL checks passed",),
        dsn_envs=("TELEMATICS_COLD_START_RECOVERY_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_telematics_cold_start_bootstrap_postgres",
        markers=("OK - Telematics cold-start bootstrap PostgreSQL checks passed",),
        dsn_envs=(
            "TELEMATICS_COLD_START_TEST_DSN",
            "TELEMATICS_COLD_START_BUSINESS_DSN",
        ),
        destructive=True,
        # The cold-start fixture builds the `client_account` row from the
        # business DSN with `urlsplit`, so that one must be a URI.
        uri_form_dsn_envs=("TELEMATICS_COLD_START_BUSINESS_DSN",),
    ),
    Suite(
        module="test_telematics_trips_recovery_postgres",
        markers=("OK - Telematics manual recovery PostgreSQL checks passed",),
        dsn_envs=("TELEMATICS_C11_RECOVERY_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_telematics_coverage_finalization_postgres",
        markers=("OK - C6 coverage finalization PostgreSQL checks passed",),
        dsn_envs=("TELEMATICS_C6_FINALIZATION_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_telematics_coverage_concurrency_postgres",
        markers=(
            "OK - C6 coverage concurrency/reconciliation PostgreSQL checks "
            "passed",
        ),
        dsn_envs=("TELEMATICS_C6_CONCURRENCY_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_telematics_coverage_state_schema_postgres",
        # Two markers: the second is printed only when the disposable-PostgreSQL
        # section actually ran, so its absence is a silent-skip failure even
        # though the suite would still exit 0.
        markers=(
            "OK - Telematics stabilized coverage state schema checks passed",
            "PASS: disposable PostgreSQL coverage-schema checks",
        ),
        dsn_envs=("TELEMATICS_COVERAGE_STATE_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_telematics_coverage_bootstrap_writer_postgres",
        markers=(
            "OK - Telematics coverage bootstrap writer checks passed",
            "PASS: disposable PostgreSQL coverage-bootstrap writer checks",
        ),
        dsn_envs=("TELEMATICS_BOOTSTRAP_WRITER_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_telematics_coverage_bootstrap_multi_client_postgres",
        markers=(
            "OK - Telematics per-client coverage bootstrap checks passed",
            "PASS: disposable PostgreSQL per-client bootstrap checks",
        ),
        dsn_envs=("TELEMATICS_BOOTSTRAP_MULTI_TEST_DSN",),
        destructive=True,
    ),
    # --- pure -------------------------------------------------------------
    Suite(
        module="test_postgres_dsn_safety",
        markers=("OK - loopback-only PostgreSQL DSN guard checks passed",),
    ),
    Suite(
        module="test_telematics_onboarding_hardening_campaign",
        markers=("OK - Telematics onboarding hardening campaign runner checks "
                 "passed",),
    ),
    Suite(
        module="test_telematics_cold_start_chain",
        markers=("OK - 18 Telematics cold-start chain checks passed",),
    ),
    Suite(
        module="test_telematics_cold_start_audit",
        markers=("OK - Telematics cold-start audit checks passed",),
        # Shares the cold-start pair with the bootstrap suite, and builds the
        # same `client_account` row from the business URI.
        dsn_envs=(
            "TELEMATICS_COLD_START_TEST_DSN",
            "TELEMATICS_COLD_START_BUSINESS_DSN",
        ),
        destructive=True,
        uri_form_dsn_envs=("TELEMATICS_COLD_START_BUSINESS_DSN",),
    ),
    Suite(
        module="test_telematics_coverage_bootstrap_audit",
        markers=(
            "OK - Telematics coverage bootstrap audit checks passed",
            "PASS: disposable PostgreSQL coverage-bootstrap audit checks",
        ),
        dsn_envs=("TELEMATICS_BOOTSTRAP_AUDIT_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_telematics_coverage_bootstrap_gate",
        # This suite prints no separate database marker. Its database section
        # having run is therefore established the other way: the campaign
        # refuses any `SKIP:` line, and the only skip it can emit is the one for
        # a missing DSN.
        markers=("OK — Telematics coverage bootstrap gate behaves as specified.",),
        dsn_envs=("TELEMATICS_COVERAGE_GATE_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_workflow_a_dispatcher",
        markers=("OK — dispatcher schedule + queueing logic looks correct.",),
    ),
    Suite(
        module="test_dispatcher_silent_noop",
        markers=("OK - dispatcher silent no-op persistence regressions passed",),
    ),
    Suite(
        module="test_telematics_trips_recovery_workflow",
        markers=(
            "OK - Telematics manual recovery workflow (pure/static) checks passed",
        ),
    ),
    Suite(
        module="test_telematics_trips_stabilization_windows",
        markers=(
            "OK - Telematics stabilized schedule-window derivation checks passed",
        ),
    ),
    Suite(
        module="test_telematics_trips_stabilization_config",
        markers=(
            "OK - Telematics trips stabilization configuration checks passed",
            "PASS: disposable PostgreSQL migration checks",
        ),
        dsn_envs=("TELEMATICS_STABILIZATION_CONFIG_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_telematics_trips_pagination_mode_config",
        markers=(
            "OK - Telematics trips pagination-mode configuration checks passed",
            "PASS: disposable PostgreSQL migration checks",
        ),
        dsn_envs=("TELEMATICS_PAGINATION_MODE_TEST_DSN",),
        destructive=True,
    ),
    Suite(
        module="test_workflow_a_registry_sync",
        markers=(
            "OK — Python registry and final migration registry seeds are in sync.",
        ),
    ),
    Suite(
        module="test_workflow_a_onboarding_provider_preflight",
        markers=("OK - onboarding provider preflight checks passed.",),
    ),
    Suite(
        module="test_workflow_a_onboarding_no_trip_fuel_columns",
        markers=(
            "OK - new-client onboarding omits deprecated trip-level fuel columns.",
        ),
    ),
    Suite(
        module="test_workflow_a_onboarding_stage3_permissions",
        markers=(
            "PASS: onboarding main flow includes Stage 3 permission bootstrap",
        ),
    ),
)

REQUIRED_SUITE_COUNT = 27

#: Every file `py_compile` must accept before the campaign is considered green.
PY_COMPILE_TARGETS: Tuple[str, ...] = (
    "jobs/api/telematics/execution_outcome.py",
    "jobs/api/telematics/manual_recovery_authority.py",
    "jobs/api/telematics/schedule_mutation_surfaces.py",
    "jobs/api/telematics/sync_trips_and_speeding.py",
    "jobs/api/telematics/dispatcher.py",
    "jobs/api/telematics/control_plane.py",
    "ops/recover_telematics_trips_window.py",
    "ops/activate_telematics_trips_schedule.py",
    "ops/tests_manual/postgres_dsn_safety.py",
    "ops/tests_manual/run_telematics_onboarding_hardening_campaign.py",
    "scripts/onboard_workflow_a_client.py",
)

#: Objects a destructive suite may leave behind in a disposable database.
CLEANUP_STATEMENTS: Tuple[str, ...] = (
    "DROP SCHEMA IF EXISTS workflow_a_control CASCADE",
    "DROP SCHEMA IF EXISTS ops_control CASCADE",
    "DROP TABLE IF EXISTS public.runs CASCADE",
    "DROP TABLE IF EXISTS public.schema_migrations CASCADE",
    "DROP TABLE IF EXISTS public.client_trips CASCADE",
    "DROP TABLE IF EXISTS public.client_trips_legacy_backup_020 CASCADE",
    "DROP TABLE IF EXISTS public.client_trips_legacy_backup_021 CASCADE",
    "DROP TABLE IF EXISTS public.client_speeding_notifications CASCADE",
)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class SuiteResult:
    suite: Suite
    returncode: Optional[int]
    stdout: str
    stderr: str
    failures: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


def classify_result(
    suite: Suite, *, returncode: Optional[int], stdout: str, stderr: str,
) -> SuiteResult:
    """Decide pass/fail for one suite. Pure — no process, no database.

    Three independent failure conditions, checked in a fixed order so the
    summary reads the same way every run:

      1. a non-zero (or absent) exit code;
      2. any `SKIP:` line — the campaign never accepts a declined section, even
         though the suite itself exits 0 for one;
      3. a missing terminal marker, which catches a suite that exited 0 and
         printed no skip but still never reached its database work.
    """
    failures: List[str] = []
    combined = f"{stdout}\n{stderr}"

    if returncode is None:
        failures.append("the suite could not be launched")
    elif returncode != 0:
        failures.append(f"exit code {returncode}")

    if SKIP_MARKER in combined:
        skipped = [
            line.strip() for line in combined.splitlines()
            if SKIP_MARKER in line
        ]
        for line in skipped:
            failures.append(f"declined a required section: {line}")

    for marker in suite.markers:
        if marker not in stdout:
            failures.append(f"missing required marker: {marker!r}")

    return SuiteResult(
        suite=suite, returncode=returncode, stdout=stdout, stderr=stderr,
        failures=failures,
    )


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def required_dsn_envs(
    suites: Sequence[Suite] = REQUIRED_SUITES,
) -> Tuple[str, ...]:
    """Every DSN environment variable the campaign needs, deduplicated."""
    seen: List[str] = []
    for suite in suites:
        for name in suite.dsn_envs:
            if name not in seen:
                seen.append(name)
    return tuple(seen)


def require_all_dsns(
    env: Dict[str, str], suites: Sequence[Suite] = REQUIRED_SUITES,
) -> Dict[str, str]:
    """Every required DSN must be present, before anything is launched.

    A missing DSN is a campaign failure, not a reason to skip: the whole point
    of this runner is that a section which did not run is never counted as one
    that passed.
    """
    missing = [
        name for name in required_dsn_envs(suites)
        if not str(env.get(name) or "").strip()
    ]
    if missing:
        raise CampaignError(
            "CAMPAIGN_MISSING_DSN",
            "the campaign requires every disposable DSN to be set explicitly; "
            f"missing: {', '.join(missing)}",
        )
    return {name: env[name] for name in required_dsn_envs(suites)}


def validate_destructive_dsns(
    env: Dict[str, str], suites: Sequence[Suite] = REQUIRED_SUITES,
) -> Dict[str, object]:
    """Prove every destructive DSN is loopback-only, before any child launch.

    Deliberately performed here, once, rather than relying only on each suite's
    own guard: a suite that lost its guard in a future edit must still not be
    handed a routable DSN by this runner.
    """
    evidence: Dict[str, object] = {}
    for suite in suites:
        if not suite.destructive:
            continue
        for name in suite.dsn_envs:
            if name in evidence:
                continue
            try:
                evidence[name] = require_loopback_dsn(
                    env.get(name), label=name, env=env,
                )
            except UnsafeDsnError as exc:
                raise CampaignError(
                    "CAMPAIGN_UNSAFE_DSN",
                    f"{name} is not loopback-only ({exc.code}); the campaign "
                    "runs destructive suites and refuses before launching any "
                    "child process",
                ) from exc
    return evidence


def validate_dsn_forms(
    env: Dict[str, str], suites: Sequence[Suite] = REQUIRED_SUITES,
) -> None:
    """Each DSN must be in the form its suite actually parses.

    A suite that calls `urlsplit` on a keyword DSN gets an empty host and then
    fails inside a connection attempt with an opaque `OperationalError`. That is
    a real failure, but it looks like an infrastructure problem rather than a
    campaign-configuration one, so the form is checked up front and named.
    """
    for suite in suites:
        for name in suite.uri_form_dsn_envs:
            value = str(env.get(name) or "")
            if not value.lower().startswith(URI_SCHEMES):
                raise CampaignError(
                    "CAMPAIGN_WRONG_DSN_FORM",
                    f"{name} must be a postgresql:// URI, because "
                    f"{suite.module} parses it as one; a keyword/value DSN "
                    "would silently resolve to no host",
                )
        for name in suite.dsn_envs:
            if name in suite.uri_form_dsn_envs:
                continue
            value = str(env.get(name) or "")
            if not (value.lower().startswith(URI_SCHEMES) or "=" in value):
                raise CampaignError(
                    "CAMPAIGN_WRONG_DSN_FORM",
                    f"{name} is neither a postgresql:// URI nor keyword/value "
                    "pairs",
                )


def run_non_loopback_guard_probe() -> None:
    """Prove the guard itself still refuses, in this exact interpreter.

    A campaign that trusted an un-exercised guard would be trusting the same
    kind of untested assumption this stack exists to remove.
    """
    refuse = (
        "postgresql://u:p@203.0.113.10:5432/db",   # public
        "host=192.168.1.50 port=5432 dbname=db",   # RFC1918
        "host=127.0.0.1,203.0.113.10 dbname=db",   # mixed multi-host
        "not a dsn at all",                        # malformed
    )
    for dsn in refuse:
        try:
            require_loopback_dsn(dsn, label="guard probe", env={})
        except UnsafeDsnError:
            continue
        raise CampaignError(
            "CAMPAIGN_GUARD_PROBE_FAILED",
            "the loopback DSN guard accepted a non-loopback target",
        )
    # And it still accepts a genuine loopback DSN, so the probe is not passing
    # merely because the guard refuses everything.
    require_loopback_dsn(
        "host=127.0.0.1 port=5432 dbname=db", label="guard probe", env={},
    )


def run_py_compile(root: Path = REPO_ROOT) -> None:
    for relative in PY_COMPILE_TARGETS:
        path = root / relative
        if not path.exists():
            raise CampaignError(
                "CAMPAIGN_PY_COMPILE_TARGET_MISSING",
                f"{relative} does not exist",
            )
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            raise CampaignError(
                "CAMPAIGN_PY_COMPILE_FAILED", f"{relative}: {exc}",
            ) from exc


def verify_suite_set(suites: Sequence[Suite] = REQUIRED_SUITES,
                     root: Path = REPO_ROOT,
                     expected_count: Optional[int] = None) -> None:
    """The enumeration must be exactly the declared 27, and all must exist.

    `expected_count` defaults to the declared 27 for the real set and is passed
    explicitly by tests that drive a synthetic set; it is never relaxed for the
    real campaign.
    """
    expected = REQUIRED_SUITE_COUNT if expected_count is None else expected_count
    if len(suites) != expected:
        raise CampaignError(
            "CAMPAIGN_SUITE_SET_CHANGED",
            f"the campaign enumerates {len(suites)} suites, expected "
            f"{expected}; changing the required set is a deliberate "
            "decision, not a side effect",
        )
    modules = [suite.module for suite in suites]
    duplicates = sorted({m for m in modules if modules.count(m) > 1})
    if duplicates:
        raise CampaignError(
            "CAMPAIGN_SUITE_SET_CHANGED",
            f"duplicate suite(s) in the required set: {', '.join(duplicates)}",
        )
    missing = [s.module for s in suites if not (root / s.path).exists()]
    if missing:
        raise CampaignError(
            "CAMPAIGN_SUITE_MISSING",
            f"required suite file(s) absent: {', '.join(missing)}",
        )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def default_runner(suite: Suite, env: Dict[str, str], root: Path):
    """Launch one suite as a child process, capturing everything it says."""
    return subprocess.run(
        [sys.executable, str(root / suite.path)],
        cwd=str(root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def run_suites(
    env: Dict[str, str],
    *,
    suites: Sequence[Suite] = REQUIRED_SUITES,
    root: Path = REPO_ROOT,
    runner: Optional[Callable] = None,
) -> List[SuiteResult]:
    launch = default_runner if runner is None else runner
    child_env = dict(env)
    child_env["PYTHONPATH"] = str(root)
    results: List[SuiteResult] = []
    for suite in suites:
        try:
            completed = launch(suite, child_env, root)
        except Exception as exc:  # the child could not be started at all
            results.append(classify_result(
                suite, returncode=None, stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
            ))
            continue
        results.append(classify_result(
            suite,
            returncode=getattr(completed, "returncode", None),
            stdout=getattr(completed, "stdout", "") or "",
            stderr=getattr(completed, "stderr", "") or "",
        ))
    return results


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_disposable_resources(
    env: Dict[str, str],
    *,
    suites: Sequence[Suite] = REQUIRED_SUITES,
    connect: Optional[Callable] = None,
) -> List[str]:
    """Drop what the destructive suites created. Returns the failures.

    Every DSN reaching this function has already been proven loopback-only by
    `validate_destructive_dsns`; it is re-validated here anyway, because a
    cleanup that ran against the wrong database would be the single most
    damaging thing this runner could do.
    """
    failures: List[str] = []
    if connect is None:
        try:
            import psycopg
        except ImportError:  # pragma: no cover - environment without the driver
            return ["psycopg is not importable; cannot clean up"]
        connect = psycopg.connect

    done: List[str] = []
    for suite in suites:
        if not suite.destructive:
            continue
        for name in suite.dsn_envs:
            if name in done:
                continue
            done.append(name)
            dsn = env.get(name)
            try:
                require_loopback_dsn(dsn, label=name, env=env)
            except UnsafeDsnError as exc:
                failures.append(f"{name}: refused before cleanup ({exc.code})")
                continue
            try:
                with connect(dsn, autocommit=True) as conn:
                    for statement in CLEANUP_STATEMENTS:
                        conn.execute(statement)
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
    return failures


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_summary(results: Sequence[SuiteResult]) -> str:
    """A deterministic per-suite summary, in the declared order."""
    width = max((len(r.suite.module) for r in results), default=0)
    lines = ["", "Per-suite results:"]
    for result in results:
        kind = "db  " if result.suite.database_backed else "pure"
        code = "-" if result.returncode is None else str(result.returncode)
        lines.append(
            f"  {result.status:<4} {kind}  exit={code:<3} "
            f"{result.suite.module.ljust(width)}"
        )
        for failure in result.failures:
            lines.append(f"         ! {failure}")
    passed = sum(1 for r in results if r.passed)
    lines.append("")
    lines.append(f"Suites: {passed}/{len(results)} passed")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_campaign(
    env: Optional[Dict[str, str]] = None,
    *,
    suites: Optional[Sequence[Suite]] = None,
    root: Path = REPO_ROOT,
    runner: Optional[Callable] = None,
    connect: Optional[Callable] = None,
    skip_cleanup: bool = False,
    expected_count: Optional[int] = None,
) -> Tuple[int, List[SuiteResult]]:
    """Run the whole campaign, in the order the gates must be applied.

    Everything that can refuse without touching a database refuses first: the
    suite set, the guard probe, compilation, the required DSNs and the
    loopback-only proof. Only then is any child process launched.
    """
    environment = dict(os.environ if env is None else env)
    selected = REQUIRED_SUITES if suites is None else tuple(suites)

    verify_suite_set(selected, root=root, expected_count=expected_count)
    run_non_loopback_guard_probe()
    run_py_compile(root=root)
    require_all_dsns(environment, selected)
    validate_dsn_forms(environment, selected)
    validate_destructive_dsns(environment, selected)

    results = run_suites(environment, suites=selected, root=root, runner=runner)
    print(format_summary(results))

    exit_code = 0 if all(r.passed for r in results) else 1

    if not skip_cleanup:
        failures = cleanup_disposable_resources(
            environment, suites=selected, connect=connect,
        )
        if failures:
            print("\nCleanup FAILED:")
            for failure in failures:
                print(f"  ! {failure}")
            # A cleanup failure is a campaign failure in its own right, even
            # when every suite passed: disposable resources left behind are the
            # next run's silent contamination.
            exit_code = 1
        else:
            print("\nCleanup: disposable resources dropped")

    return exit_code, results


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the authoritative Telematics onboarding hardening campaign. "
            "Fails on any skipped section."
        )
    )
    parser.add_argument(
        "--list", action="store_true",
        help="print the required suites and DSN variables, then exit",
    )
    parser.add_argument(
        "--skip-cleanup", action="store_true",
        help="leave the disposable databases populated for inspection",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.list:
        print(f"Required suites ({len(REQUIRED_SUITES)}):")
        for suite in REQUIRED_SUITES:
            kind = "db  " if suite.database_backed else "pure"
            print(f"  {kind}  {suite.module}")
        print("\nRequired DSN environment variables:")
        for name in required_dsn_envs():
            print(f"  {name}")
        return 0

    print("=" * 72)
    print("  Telematics onboarding hardening campaign")
    print(f"  {len(REQUIRED_SUITES)} required suites; zero skips accepted")
    print("=" * 72)

    try:
        exit_code, _ = run_campaign(skip_cleanup=args.skip_cleanup)
    except CampaignError as exc:
        print(f"\nCAMPAIGN REFUSED — {exc}")
        return 2

    print(
        "\nCampaign result: "
        + ("GREEN" if exit_code == 0 else "FAILED")
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
