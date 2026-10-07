#!/usr/bin/env python3
"""Read-only quiescence check for the release-boundary cutover.

Answers one question: *is it safe to replace `/usr/local/bin/log-job-runner.sh`
right now?* It never stops, starts, installs or writes anything — the operator
performs the systemd actions, this only refuses to bless an unsafe moment.

The ordering it enforces is the point. Checking that a service is inactive and
*then* stopping its timer is a race: a tick can start in between, and stopping a
timer never terminates a run that has already begun. So the contract is

    stop the timers first  ->  then check services and advisory locks

and the check is meant to be run **twice**: once after the timers are stopped,
and again immediately before the wrapper is replaced, because a long-running job
can finish (or, if a timer was missed, `Persistent=true` can start one) between
the two.

Exit codes:

    0  safe to proceed at this instant
    1  not safe — a timer is still armed, a job is running, or a lock is held
    2  the check itself could not be completed (never read as "safe")

    ops/cutover_preflight.py --release <release_id>
    ops/cutover_preflight.py --release <release_id> --stage pre-replacement
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Re-exec under the project interpreter when the system one lacks our
# dependencies. The advisory-lock check needs `psycopg`, which lives only in
# .venv, and this script is invoked by shebang during a cutover. Without this,
# every run would report both lock domains indeterminate — a permanent blocker
# that fires *after* the operator has already stopped all ingestion timers.
def _reexec_under_project_interpreter() -> None:
    import importlib.util
    import os

    if os.environ.get("_CUTOVER_PREFLIGHT_REEXEC") == "1":
        return
    if importlib.util.find_spec("psycopg") is not None:
        return
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    # Compare the *unresolved* paths. A venv's `bin/python` is a symlink to the
    # same real binary as the system interpreter — the venv is selected by the
    # path it is invoked through, not by the inode — so resolving here would
    # make this look like a no-op re-exec and silently skip it. Loop safety
    # comes from the environment marker above, not from this comparison.
    if not venv_python.is_file() or str(venv_python) == sys.executable:
        return
    os.execve(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
              {**os.environ, "_CUTOVER_PREFLIGHT_REEXEC": "1"})


if __name__ == "__main__":
    # Only when run as a program. `cutover_execute` imports this module while a
    # cutover is in flight; re-execing on import would replace that process —
    # with the wrong argv — after the ingestion timers are already stopped.
    _reexec_under_project_interpreter()

from ops.cutover_fence import read_fence  # noqa: E402
from ops.release_boundary import (  # noqa: E402
    INSTALLED_WRAPPER_PATH,
    wrapper_has_bootstrap_capabilities,
    WRAPPER_DEVELOPMENT,
    WRAPPER_DEVELOPMENT_HISTORICAL,
    WRAPPER_RELEASE,
    ReleaseBoundaryError,
    installed_wrapper_variant,
    pointer_release_id,
    verify_release,
)

DEFAULT_RELEASE_ROOT = Path("/opt/log-platform-release")


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None

# Every systemd timer that can launch work through the job wrapper. Derived from
# the installed topology, not from the repository: the dispatcher and Workflow B
# call the wrapper directly, and retention reaches it through
# /usr/local/bin/log-retention-purge.sh (an override.conf redirect).
KNOWN_WRAPPER_CONSUMER_TIMERS = (
    "log-job@dispatcher.timer",
    "log-workflow-b.timer",
    "log-job@retention-purge.timer",
)
KNOWN_WRAPPER_CONSUMER_SERVICES = (
    "log-job@dispatcher.service",
    "log-workflow-b.service",
    "log-job@retention-purge.service",
)

# Both advisory-lock domains that guard wrapper-launched work. The dispatcher key
# is a literal in dispatcher.py; the Workflow B key is derived, so it is imported
# rather than copied — a duplicated constant here would silently stop matching.
DISPATCHER_ADVISORY_LOCK_KEY = 728503746327118001


def _workflow_b_lock_key() -> int | None:
    try:
        from jobs.reports.workflow_b.orchestrator import workflow_b_advisory_lock_key

        return int(workflow_b_advisory_lock_key())
    except Exception:  # noqa: BLE001 - reported as indeterminate, never as "free"
        return None


def _systemctl(*args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(["systemctl", *args], check=False,
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return 125, str(exc)
    return result.returncode, (result.stdout or result.stderr).strip()


def _unit_state(unit: str) -> dict:
    """Load state and activity for one unit.

    `systemctl is-active` prints `inactive` for a unit that does not exist and
    only signals it through the exit code, so a renamed or mistyped unit would
    otherwise read as quiescent. An unknown unit is reported as indeterminate,
    which is a blocker — this check must never conclude "nothing is running"
    from a name it could not resolve.
    """
    rc, raw = _systemctl("show", unit, "--property=LoadState,ActiveState,Unit")
    values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    load_state = values.get("LoadState", "unknown")
    active_state = values.get("ActiveState", "unknown")
    known = rc == 0 and load_state == "loaded"
    return {"unit": unit, "load_state": load_state, "active_state": active_state,
            "known": known,
            "quiescent": known and active_state in ("inactive", "failed")}


def discover_wrapper_consumers() -> dict:
    """Every unit that can launch work through the job wrapper, from the host.

    A hardcoded list is a snapshot: `log-job@.service` is a template, so any
    instance — including ones enabled later in the project — runs the same
    wrapper. The known set below is a floor, not the answer.

    Discovery failure is *not* an empty result. If `systemctl` cannot enumerate
    units, the honest statement is "the consumer set is unknown", and the caller
    must treat that as a blocker. Silently falling back to the hardcoded floor
    would let a real consumer keep firing during a wrapper replacement while the
    preflight reported quiescence.
    """
    timers = set(KNOWN_WRAPPER_CONSUMER_TIMERS)
    services = set(KNOWN_WRAPPER_CONSUMER_SERVICES)
    failures = []

    rc, raw = _systemctl("list-unit-files", "--no-legend", "--no-pager")
    if rc != 0:
        failures.append({"command": "systemctl list-unit-files", "returncode": rc,
                         "output": raw[:200]})
    else:
        for line in raw.splitlines():
            name = line.split()[0] if line.split() else ""
            if name.startswith("log-job@") and name.endswith(".timer") and "@." not in name:
                timers.add(name)
                services.add(name[: -len(".timer")] + ".service")

    rc, raw = _systemctl("list-units", "--all", "--no-legend", "--no-pager", "log-job@*.service")
    if rc != 0:
        failures.append({"command": "systemctl list-units log-job@*.service", "returncode": rc,
                         "output": raw[:200]})
    else:
        for line in raw.splitlines():
            name = line.replace("*", " ").split()[0] if line.strip() else ""
            if name.startswith("log-job@") and name.endswith(".service"):
                services.add(name)

    return {"timers": sorted(timers), "services": sorted(services),
            "complete": not failures, "discovery_failures": failures}


def timer_states(units: list[str]) -> list[dict]:
    rows = []
    for unit in units:
        state = _unit_state(unit)
        _rc, enabled = _systemctl("is-enabled", unit)
        state["enabled"] = enabled
        rows.append(state)
    return rows


def service_states(units: list[str]) -> list[dict]:
    return [_unit_state(unit) for unit in units]


def advisory_lock_states() -> list[dict]:
    """Report holders of both wrapper-relevant advisory-lock domains.

    Read-only: a single SELECT against pg_locks. An unreachable database is
    reported as indeterminate, never as "no locks held" — a preflight that
    degrades to optimism is worse than no preflight.
    """
    keys = {"dispatcher": DISPATCHER_ADVISORY_LOCK_KEY, "workflow_b": _workflow_b_lock_key()}
    rows = []
    try:
        import psycopg
        from dotenv import load_dotenv
        import os

        load_dotenv(REPO_ROOT / ".env", override=False)
        dsn = os.environ.get("PLATFORM_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not dsn:
            host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
            port = os.environ.get("POSTGRES_PORT", "5432")
            name = os.environ.get("POSTGRES_DB", "postgres")
            user = os.environ.get("POSTGRES_USER", "postgres")
            password = os.environ.get("POSTGRES_PASSWORD", "")
            dsn = f"postgresql://{user}:{password}@{host}:{port}/{name}"
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                for domain, key in keys.items():
                    if key is None:
                        rows.append({"domain": domain, "key": None, "held": None,
                                     "determinate": False, "reason": "lock key unresolved"})
                        continue
                    cur.execute(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE locktype = 'advisory' AND ((classid::bigint << 32) | objid::bigint) = %s",
                        (key,),
                    )
                    held = int(cur.fetchone()[0])
                    rows.append({"domain": domain, "key": key, "held": held > 0,
                                 "holders": held, "determinate": True})
    except Exception as exc:  # noqa: BLE001 - indeterminate is a refusal, not a pass
        for domain, key in keys.items():
            rows.append({"domain": domain, "key": key, "held": None,
                         "determinate": False, "reason": f"{type(exc).__name__}: {exc}"[:200]})
    return rows


def evaluate(release_root: Path, release_id: str | None, source_repo: Path,
             *, require_current_target: bool = True) -> dict:
    """Judge this instant, optionally against a named target release.

    `release_id` and `require_current_target` are two independent questions that
    used to be one. Passing a release id asks "is this candidate real and does it
    verify?"; `require_current_target` asks the separate question "is it already
    the current release?". Collapsing them meant the only way to stop demanding
    the second — which a *pre*-cutover gate must not, because the pointer
    sequence has not run yet — was to pass no release id at all, which also
    silently stopped verifying the candidate. A nonexistent target then read as
    safe. Callers that gate before the pointer sequence pass the target with
    `require_current_target=False`: verify the candidate, do not demand it be
    current yet.
    """
    consumers = discover_wrapper_consumers()
    timers = timer_states(consumers["timers"])
    services = service_states(consumers["services"])
    locks = advisory_lock_states()
    variant = installed_wrapper_variant(repo_root=source_repo, installed_wrapper=INSTALLED_WRAPPER_PATH)

    blockers: list[str] = []
    if not consumers.get("complete", False):
        for failure in consumers.get("discovery_failures", []):
            blockers.append(
                f"wrapper-consumer discovery failed: `{failure['command']}` returned "
                f"{failure['returncode']} — the consumer set is unknown, so quiescence "
                f"cannot be established")
    for row in timers:
        if not row["known"]:
            blockers.append(f"timer {row['unit']} could not be resolved "
                            f"(LoadState={row['load_state']}); its state is unknown, not quiescent")
        elif not row["quiescent"]:
            blockers.append(f"timer still armed: {row['unit']} is {row['active_state']} "
                            f"(stop every wrapper-consumer timer before checking services)")
    for row in services:
        if not row["known"]:
            blockers.append(f"service {row['unit']} could not be resolved "
                            f"(LoadState={row['load_state']}); its state is unknown, not quiescent")
        elif not row["quiescent"]:
            blockers.append(f"job still running: {row['unit']} is {row['active_state']} "
                            f"(stopping a timer does not stop a run already in progress)")
    for row in locks:
        if not row["determinate"]:
            blockers.append(f"advisory lock domain {row['domain']} indeterminate: {row.get('reason')}")
        elif row["held"]:
            blockers.append(f"advisory lock held in {row['domain']} domain ({row['holders']} holder(s))")

    release = {"release_id": release_id, "verified": None}
    if release_id:
        try:
            report = verify_release(release_root=release_root, release_id=release_id,
                                    source_repo=source_repo)
            release.update({"verified": True, "commit": report["commit"]})
        except ReleaseBoundaryError as exc:
            release.update({"verified": False, "classification": exc.classification})
            blockers.append(f"release {release_id} does not verify: {exc.classification}")
        try:
            current = pointer_release_id(release_root / "current")
        except ReleaseBoundaryError as exc:
            current = None
            blockers.append(f"current pointer invalid: {exc.classification}")
        release["current_release_id"] = current
        # Reported either way; a blocker only when the caller is asking about a
        # moment at which the target is supposed to be current already.
        if require_current_target and current != release_id:
            blockers.append(f"current points at {current!r}, not the release being cut over to "
                            f"({release_id!r}); activate it before replacing the wrapper")
        release["current_must_be_target"] = require_current_target

    # The first cutover may begin only from the exact reviewed barrier-capable
    # development wrapper. `development_tree` means byte-identical to the
    # repository copy, which is the only way to know the installed wrapper
    # actually participates in the execution barrier, the fence and the
    # stale-wrapper re-exec. A wrapper from an earlier commit
    # (`development_historical`) predates those contracts — it is exactly what is
    # installed today — so it is refused rather than assumed compatible.
    if variant == WRAPPER_RELEASE:
        notes = "installed wrapper is already the release wrapper; cutover appears complete"
    elif variant == WRAPPER_DEVELOPMENT:
        notes = ("installed wrapper is the exact reviewed barrier-capable development "
                 "bootstrap wrapper; first cutover may proceed")
        # Byte-equality alone is defeatable by checking out an older copy of the
        # repository wrapper, which would match while having neither the barrier
        # nor the fence. Assert the capabilities themselves.
        capability = wrapper_has_bootstrap_capabilities(INSTALLED_WRAPPER_PATH)
        if not capability["capable"]:
            blockers.append(f"installed wrapper matches the repository copy but lacks the "
                            f"pre-cutover contracts it must implement: "
                            f"{', '.join(capability['missing'])}")
    elif variant == WRAPPER_DEVELOPMENT_HISTORICAL:
        notes = "installed wrapper is a development wrapper from an earlier commit"
        blockers.append(
            "installed wrapper is a historical development wrapper: it predates the execution "
            "barrier and the cutover fence, so quiescence cannot be enforced. Install the exact "
            "reviewed bootstrap wrapper (host prerequisite B) before cutting over")
    else:
        notes = f"installed wrapper is {variant}; resolve before replacing it"
        blockers.append(f"installed wrapper variant is {variant}, not the exact reviewed "
                        f"development bootstrap wrapper")

    # The fence must be in a healthy pre-cutover state, or a previous failed
    # attempt is still blocking production and must be recovered first.
    fence = read_fence(release_root)
    if not fence.get("execution_permitted"):
        blockers.append(f"cutover fence is {fence.get('state')}: production execution is "
                        f"blocked by an unresolved earlier attempt; run recovery first")

    return {
        "safe_to_replace_wrapper": not blockers,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "installed_wrapper_sha256": _sha256(INSTALLED_WRAPPER_PATH),
        "blockers": blockers,
        "timers": timers,
        "services": services,
        "consumer_discovery": {"complete": consumers.get("complete", False),
                               "failures": consumers.get("discovery_failures", [])},
        "advisory_locks": locks,
        "installed_wrapper_variant": variant,
        "cutover_fence": fence,
        "installed_wrapper_capabilities": wrapper_has_bootstrap_capabilities(INSTALLED_WRAPPER_PATH),
        "notes": notes,
        "release": release,
        "ordering_contract": [
            "1. stop every wrapper-consumer timer",
            "2. re-run this check: services and advisory locks must be clear",
            "3. re-run this check again immediately before replacing the wrapper",
            "4. replace the wrapper atomically (install to a temp name, then mv -f)",
            "5. start only the timers this procedure stopped",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=None, help="release id the cutover will promote")
    parser.add_argument("--release-root", default=str(DEFAULT_RELEASE_ROOT))
    parser.add_argument("--source-repo", default=str(REPO_ROOT))
    parser.add_argument("--stage", default="post-timer-stop",
                        choices=["post-timer-stop", "pre-replacement"],
                        help="which of the two mandatory checks this invocation is")
    args = parser.parse_args(argv)

    try:
        report = evaluate(Path(args.release_root), args.release, Path(args.source_repo))
    except Exception as exc:  # noqa: BLE001 - an incomplete check is never "safe"
        print(json.dumps({"safe_to_replace_wrapper": False, "stage": args.stage,
                          "error": f"{type(exc).__name__}: {exc}"}, indent=2), file=sys.stderr)
        return 2
    report["stage"] = args.stage
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["safe_to_replace_wrapper"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
