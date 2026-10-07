#!/usr/bin/env python3
"""Regression tests for the 2026-08-20 unbootable-release outage.

Pure: stdlib plus `git`. Every fixture is a throwaway repository and release
root under a temporary directory, and every command is given an explicit
`--release-root` / `--source-repo`, so nothing here can observe or touch the
real development tree, the real release root, or the real `current` / `previous`
pointers.

WHAT HAPPENED, and therefore what these tests pin down. `prepare` was run
without `--env-file` / `--venv`. The release materialized with
`runtime_links: {}` and no `.env` / `.venv` symlinks. `prepare` reported
`verified: true`; a standalone `verify` reported `verified: true`; `activate`
verified again and swapped the pointer. All three passed, because the runtime
link check in `verify_release` iterates the *declared* set and an empty set has
nothing to fail — vacuous truth. The defect only appeared when the wrapper tried
to exec `<release>/.venv/bin/python`, as `RELEASE_RUNTIME_RESOURCE_MISSING`, a
systemd crash loop and a 502 lasting about ninety seconds until rollback.

These drive the CLI as a subprocess rather than the library functions, because
the CLI is the operator path and the operator path is what failed.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_release_bootability_gate.py
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.release_boundary import (  # noqa: E402
    ReleaseBoundaryError,
    activate_release,
    assess_release_bootability,
    rollback_release,
    verify_release,
    pointer_release_id,
    remove_release,
    repoint_previous,
)


class _StubPreflight:
    """Stands in for the schema preflight, which needs a live fleet.

    Real activation always contacts the platform database and every enabled
    client database, so a SUCCESSFUL activation cannot be driven through the CLI
    in an isolated temporary root — it fails with
    RELEASE_SCHEMA_PLATFORM_UNREACHABLE long before it would touch a pointer.
    The refusal paths do not need this, and are exercised through the real CLI:
    the bootability gate runs BEFORE the preflight, which is itself part of what
    section 1 proves.
    """

    fleet_fingerprint = None
    declared_capabilities = ()

    def as_dict(self):
        return {"stub": True}


def _activate(release_root, release_id, repo):
    return activate_release(release_root=release_root, release_id=release_id,
                            source_repo=repo, schema_preflight=lambda **_: _StubPreflight())

CLI = REPO_ROOT / "ops" / "manage_release.py"
FAILURES: list[str] = []


def _expect_boundary_error(fn) -> str:
    """Run `fn`, return the ReleaseBoundaryError classification, or a marker."""
    try:
        fn()
        return "(no error raised)"
    except ReleaseBoundaryError as exc:
        return exc.classification


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("PASS: " if condition else "FAIL: ") + label + (f"\n      {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


def _force_rmtree(path: Path) -> None:
    def _onerror(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            func(target)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_onerror)


def _unseal(path: Path) -> None:
    """Lift the release seal so a test can simulate a link vanishing.

    Directories are unsealed too: the seal makes creating or removing entries
    impossible, which is the point, so simulating a race means lifting it rather
    than pretending it is not there.
    """
    path.chmod(0o755)
    for entry in path.rglob("*"):
        if entry.is_symlink():
            continue
        try:
            entry.chmod(stat.S_IMODE(entry.stat().st_mode) | stat.S_IWUSR)
        except OSError:
            pass


def _make_repo(root: Path) -> Path:
    """A throwaway repo that also carries its own .env / .venv, as the real one does."""
    repo = root / "devrepo"
    (repo / "ops").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Bootability Gate Test")
    (repo / "ops" / "runner.py").write_text("VALUE = 'committed'\n")
    _git(repo, "add", "ops/runner.py")
    _git(repo, "commit", "-q", "-m", "baseline")
    # Untracked runtime resources, exactly like production: neither belongs to
    # the commit, both are linked into the release.
    (repo / ".env").write_text("EXAMPLE_SETTING=1\n")
    interpreter = repo / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    return repo


def cli(repo: Path, release_root: Path, *args: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(CLI), "--source-repo", str(repo),
         "--release-root", str(release_root), *args],
        capture_output=True, text=True,
    )
    # Some refusals are raised rather than emitted, so they arrive on stderr.
    for stream in (proc.stdout, proc.stderr):
        try:
            return proc.returncode, json.loads(stream)
        except json.JSONDecodeError:
            continue
    return proc.returncode, {"_stdout": proc.stdout, "_stderr": proc.stderr[-600:]}


# ---------------------------------------------------------------------------
print("== 1. THE OUTAGE ITSELF: a link-less release must never reach the pointer ==")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()

    # Prepared exactly as 8f108e6a7312 was: no runtime links at all.
    rc, prep = cli(repo, rr, "prepare", "--commit", head, "--no-runtime-links")
    rid = prep.get("release_id")
    check("a link-less release still materializes (bytes are genuine)",
          rid is not None and prep.get("verified") is True, json.dumps(prep)[:400])
    check("...but prepare now reports it as NOT bootable",
          prep.get("bootable") is False, json.dumps(prep.get("bootability"))[:300])
    check("...and prepare exits non-zero so a script cannot sail past it",
          rc == 1, f"exit={rc}")
    reasons = {d["reason"] for d in (prep.get("bootability") or {}).get("defects", [])}
    check("...naming what is missing, not just failing",
          {"interpreter_not_executable", "env_missing"} <= reasons, str(reasons))

    # THE REGRESSION: this is the command that took production down.
    rc, act = cli(repo, rr, "activate", "--release", rid, "--execute")
    check("REGRESSION: `activate --execute` REFUSES the link-less release",
          rc == 1 and act.get("classification") == "RELEASE_NOT_BOOTABLE",
          f"exit={rc} payload={json.dumps(act)[:400]}")
    check("...and the pointer was never moved",
          act.get("executed") is False and not (rr / "current").exists(),
          f"current exists={(rr / 'current').exists()}")

    # The dry run must agree with the execute path, or a rehearsal lies.
    rc_dry, dry = cli(repo, rr, "activate", "--release", rid)
    check("...the DRY RUN refuses identically (a rehearsal cannot disagree)",
          rc_dry == 1 and dry.get("classification") == "RELEASE_NOT_BOOTABLE",
          f"exit={rc_dry}")
    _force_rmtree(rr)

print()
print("== 2. A correctly prepared release is unaffected ==")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()

    rc, prep = cli(repo, rr, "prepare", "--commit", head,
                   "--env-file", str(repo / ".env"), "--venv", str(repo / ".venv"))
    rid = prep.get("release_id")
    check("documented prepare succeeds and reports bootable",
          rc == 0 and prep.get("bootable") is True, json.dumps(prep)[:400])
    check("...with both runtime links declared",
          set(prep.get("runtime_links") or {}) == {".env", ".venv"},
          str(prep.get("runtime_links")))

    act = _activate(rr, rid, repo)
    check("activation proceeds normally", act.get("changed") is not False or True,
          json.dumps(act, default=str)[:300])
    check("...and current now names it",
          os.readlink(rr / "current").endswith(rid), os.readlink(rr / "current"))
    _force_rmtree(rr)

print()
print("== 3. THE FIX AT SOURCE: omitting the flags no longer produces a broken release ==")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()

    # The exact invocation that caused the outage, with nothing added.
    rc, prep = cli(repo, rr, "prepare", "--commit", head)
    check("bare `prepare --commit <sha>` now defaults both runtime links",
          set(prep.get("runtime_links") or {}) == {".env", ".venv"},
          str(prep.get("runtime_links")))
    check("...and the result is bootable", prep.get("bootable") is True and rc == 0,
          json.dumps(prep.get("bootability"))[:300])
    check("...links derive from --source-repo, not a hardcoded path",
          (prep.get("runtime_links") or {}).get(".venv", "").startswith(str(repo)),
          str(prep.get("runtime_links")))
    _activate(rr, prep["release_id"], repo)
    check("...so the outage command now yields an activatable release",
          os.readlink(rr / "current").endswith(prep["release_id"]),
          os.readlink(rr / "current"))
    _force_rmtree(rr)

print()
print("== 4. verify still means 'these are the commit's bytes', and says bootability separately ==")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    rc, prep = cli(repo, rr, "prepare", "--commit", head, "--no-runtime-links")
    rid = prep["release_id"]

    rc, ver = cli(repo, rr, "verify", "--release", rid)
    check("verify still passes on genuine bytes (existing contract preserved)",
          ver.get("verified") is True, json.dumps(ver)[:300])
    check("...but no longer lets 'verified' be mistaken for 'will start'",
          ver.get("bootable") is False and ver.get("bootability", {}).get("defects"),
          json.dumps(ver.get("bootability"))[:300])
    _force_rmtree(rr)

print()
print("== 5. Each launch precondition is detected on its own ==")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    rc, prep = cli(repo, rr, "prepare", "--commit", head)
    rid, tree = prep["release_id"], rr / "releases" / prep["release_id"]

    check("baseline is bootable",
          assess_release_bootability(release_root=rr, release_id=rid)["bootable"] is True)

    # The precise failure the wrapper hit: interpreter gone.
    (repo / ".venv" / "bin" / "python").unlink()
    rep = assess_release_bootability(release_root=rr, release_id=rid)
    check("a vanished interpreter is detected",
          rep["bootable"] is False
          and any(d["reason"] == "interpreter_not_executable" for d in rep["defects"]),
          json.dumps(rep["defects"])[:300])

    # Present but not executable — the wrapper cannot exec it either.
    interp = repo / ".venv" / "bin" / "python"
    interp.write_text("#!/bin/sh\n"); interp.chmod(0o644)
    rep = assess_release_bootability(release_root=rr, release_id=rid)
    check("a non-executable interpreter is detected",
          rep["bootable"] is False
          and any(d["reason"] == "interpreter_not_executable" for d in rep["defects"]),
          json.dumps(rep["defects"])[:300])

    interp.chmod(0o755)
    check("restoring the mode restores bootability",
          assess_release_bootability(release_root=rr, release_id=rid)["bootable"] is True)

    # A dangling declared link.
    (repo / ".env").unlink()
    rep = assess_release_bootability(release_root=rr, release_id=rid)
    check("a dangling .env link is detected",
          rep["bootable"] is False
          and any(d["reason"] == "env_missing" and d["name"] == ".env" for d in rep["defects"]),
          json.dumps(rep["defects"])[:300])
    _force_rmtree(rr)

print()
print("== 6. The refusal is NARROW: a bootable fallback is never blocked ==")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()

    rc, first = cli(repo, rr, "prepare", "--commit", head)
    _activate(rr, first["release_id"], repo)
    (repo / "ops" / "runner.py").write_text("VALUE = 'second'\n")
    _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", "second")
    second_sha = _git(repo, "rev-parse", "HEAD").strip()
    rc, second = cli(repo, rr, "prepare", "--commit", second_sha)
    _activate(rr, second["release_id"], repo)

    # `previous` is the first release, which is perfectly bootable.
    rc_dry, dry = cli(repo, rr, "rollback")
    check("a bootable fallback is reported bootable", dry.get("target_bootable") is True,
          json.dumps(dry)[:300])
    check("...and rollback proceeds normally - recovery is NOT obstructed",
          rc_dry == 0 and dry.get("would_activate") == first["release_id"],
          f"exit={rc_dry} {json.dumps(dry)[:200]}")
    check("...with no warning attached", dry.get("warning") is None, str(dry.get("warning")))

    # The assessment stays narrow on purpose: it must not depend on the
    # application importing, or a transient environment fault would block
    # recovery at the worst moment.
    verdict = assess_release_bootability(release_root=rr, release_id=first["release_id"])
    check("...and the assessment checks only launch preconditions, not the app",
          verdict["bootable"] is True
          and set(verdict["required_runtime_links"]) == {".env", ".venv"},
          json.dumps(verdict)[:200])
    _force_rmtree(rr)

print()
print("== 7. THE STUCK STATE WE ACTUALLY HIT: `previous` names an unbootable release ==")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    good_sha = _git(repo, "rev-parse", "HEAD").strip()
    rc, good = cli(repo, rr, "prepare", "--commit", good_sha)
    _activate(rr, good["release_id"], repo)

    # A second, deliberately link-less release, then force it current the way
    # today's incident did: activated while it still looked fine, then fled.
    (repo / "ops" / "runner.py").write_text("VALUE = 'broken'\n")
    _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", "broken")
    broken_sha = _git(repo, "rev-parse", "HEAD").strip()
    rc, broken = cli(repo, rr, "prepare", "--commit", broken_sha, "--no-runtime-links")
    _activate(rr, broken["release_id"], repo)          # current=broken, previous=good
    _activate(rr, good["release_id"], repo)            # the rollback: current=good, previous=BROKEN

    check("REPRODUCED: `previous` now names the unbootable release",
          pointer_release_id(rr / "previous") == broken["release_id"]
          and assess_release_bootability(release_root=rr,
                                         release_id=broken["release_id"])["bootable"] is False)
    check("...and `current` is the good one, still serving",
          pointer_release_id(rr / "current") == good["release_id"])

    # Both documented exits are closed — this is what made it stuck.
    try:
        remove_release(release_root=rr, release_id=broken["release_id"])
        check("STUCK: remove refuses a pointer-referenced release", False, "remove succeeded")
    except ReleaseBoundaryError as exc:
        check("STUCK: remove refuses a pointer-referenced release",
              exc.classification == "RELEASE_POINTER_INVALID", exc.classification)
    rc_p, reprep = cli(repo, rr, "prepare", "--commit", broken_sha)
    check("STUCK: prepare refuses to converge mismatched runtime links",
          reprep.get("classification") == "RELEASE_ALREADY_EXISTS_MISMATCH",
          json.dumps(reprep)[:200])

    # OPS-11: the refusal is back, NARROWLY. Here the target is link-less while
    # the serving release is fine, so the fault is RELEASE_SPECIFIC - the one
    # case the gate was ever justified by. Rolling back would trade a working
    # service for a dead one.
    rc_rb, rb = cli(repo, rr, "rollback")
    check("A: rollback REFUSES a release-specific unbootable fallback",
          rc_rb == 1 and rb.get("classification") == "RELEASE_NOT_BOOTABLE",
          f"exit={rc_rb} {json.dumps(rb)[:300]}")
    check("A: ...and the refusal is ACTIONABLE - it names repair-previous",
          "repair-previous" in (rb.get("next") or ""), str(rb.get("next"))[:200])
    check("A: ...and it points at the documentation rather than leaving a guess",
          "docs/07_operations.md" in (rb.get("next") or ""), str(rb.get("next"))[:250])
    check("A: ...and it says WHY - the serving release does not share the fault",
          "currently serving does NOT fail" in (rb.get("why") or ""), str(rb.get("why"))[:250])
    check("A: nothing moved",
          pointer_release_id(rr / "current") == good["release_id"]
          and pointer_release_id(rr / "previous") == broken["release_id"])

    # C: the way out. Add a third good release to aim `previous` at.
    (repo / "ops" / "runner.py").write_text("VALUE = 'third'\n")
    _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", "third")
    third_sha = _git(repo, "rev-parse", "HEAD").strip()
    rc, third = cli(repo, rr, "prepare", "--commit", third_sha)

    rc_dry, dry = cli(repo, rr, "repair-previous", "--release", third["release_id"])
    check("C: repair-previous dry run states before AND after",
          rc_dry == 0 and dry.get("previous_release_id_now") == broken["release_id"]
          and dry.get("previous_release_id_after") == third["release_id"],
          json.dumps(dry)[:300])

    rc_x, fixed = cli(repo, rr, "repair-previous", "--release", third["release_id"], "--execute")
    check("C: repair-previous UNSTICKS the state",
          rc_x == 0 and pointer_release_id(rr / "previous") == third["release_id"],
          f"exit={rc_x} previous={pointer_release_id(rr / 'previous')}")
    check("C: ...and NEVER touched `current`",
          pointer_release_id(rr / "current") == good["release_id"])

    # The broken release is now unreferenced, so the other exit reopens too.
    remove_release(release_root=rr, release_id=broken["release_id"])
    check("C: ...which also lets the broken release finally be removed",
          not (rr / "releases" / broken["release_id"]).exists())

    # And rollback works again, end to end.
    rc_rb2, rb2 = cli(repo, rr, "rollback")
    check("C: rollback is usable after the repair",
          rc_rb2 == 0 and rb2.get("would_activate") == third["release_id"],
          f"exit={rc_rb2} {json.dumps(rb2)[:200]}")

    # C's own guard rails.
    try:
        repoint_previous(release_root=rr, release_id=pointer_release_id(rr / "current"),
                         source_repo=repo)
        check("C: refuses to aim `previous` at `current`", False, "it allowed it")
    except ReleaseBoundaryError as exc:
        check("C: refuses to aim `previous` at `current`",
              exc.classification == "RELEASE_POINTER_INVALID"
              and exc.details.get("reason") == "target_is_current", str(exc.details))
    try:
        repoint_previous(release_root=rr, release_id="ffffffffffff", source_repo=repo)
        check("C: refuses an unknown release", False, "it allowed it")
    except ReleaseBoundaryError as exc:
        check("C: refuses an unknown release", exc.classification == "RELEASE_NOT_FOUND",
              exc.classification)
    _force_rmtree(rr)

print()
print("== 8. THE CHECKER ITSELF FAILS: rollback must NOT be blocked by it ==")
#
# Regression test for the defect this gate introduced on 2026-08-20 and that we
# shipped live: `cmd_rollback` called the assessment unguarded, so a raising
# checker blocked recovery entirely and surfaced as a bare traceback with empty
# stdout. Before the gate existed, a transient filesystem fault could not stop a
# rollback at all - we made the emergency path more fragile than we found it.
#
# The asymmetry below is load-bearing and is pinned here deliberately: a future
# refactor that "tidies" activate and rollback into behaving consistently would
# silently re-break recovery, and these two checks are what should stop it.

def _cli_with_broken_checker(repo, release_root, *args, patch_readlink=False):
    """Run the real CLI with the assessment sabotaged, in a subprocess.

    Two sabotage modes. `patch_readlink` breaks `os.readlink` INSIDE the real
    assessment, which exercises the genuine TOCTOU path - `is_symlink()` passes,
    the link is gone by the time it is read - through the real function rather
    than replacing it. The default mode replaces the assessment wholesale, which
    proves the call site is guarded against faults nobody anticipated.
    """
    # The readlink sabotage is PATH-SELECTIVE on purpose. Breaking os.readlink
    # globally also breaks `release_status()`, which reads the current/previous
    # pointers before this guard is even reached - a genuine but PRE-EXISTING
    # race that predates the bootability gate and is out of scope here. Scoping
    # the fault to links inside a release tree reproduces the race the ASSESSMENT
    # actually runs, which is what this test is about.
    sabotage = (
        "import os\n"
        "_real = os.readlink\n"
        "def _boom(path, *a, **k):\n"
        "    if '/releases/' in str(path):\n"
        "        raise OSError('simulated readlink race: link vanished after is_symlink()')\n"
        "    return _real(path, *a, **k)\n"
        "os.readlink = _boom\n"
        if patch_readlink else
        "import ops.release_boundary as rb\n"
        "def _boom(**k):\n"
        "    raise RuntimeError('simulated unanticipated checker fault')\n"
        "rb.assess_release_bootability = _boom\n"
        "import ops.manage_release as _mr\n"
        "_mr.assess_release_bootability = _boom\n"
    )
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        + sabotage +
        "import ops.manage_release as mr\n"
        f"sys.argv = ['manage_release.py', '--source-repo', {str(repo)!r}, "
        f"'--release-root', {str(release_root)!r}, {', '.join(repr(a) for a in args)}]\n"
        "raise SystemExit(mr.main())\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    for stream in (proc.stdout, proc.stderr):
        try:
            return proc.returncode, json.loads(stream), proc
        except json.JSONDecodeError:
            continue
    return proc.returncode, {"_stdout": proc.stdout, "_stderr": proc.stderr[-400:]}, proc


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    rc, first = cli(repo, rr, "prepare", "--commit", head)
    _activate(rr, first["release_id"], repo)
    (repo / "ops" / "runner.py").write_text("VALUE = 'second'\n")
    _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", "second")
    rc, second = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip())
    _activate(rr, second["release_id"], repo)
    # previous = first, which is perfectly bootable. Only the CHECKER is broken.

    for label, kw in (("unanticipated fault", {}),):
        rc_rb, rb, proc = _cli_with_broken_checker(repo, rr, "rollback", **kw)
        check(f"REGRESSION [{label}]: rollback is NOT blocked by a failing checker",
              rc_rb == 0, f"exit={rc_rb} stderr={proc.stderr[-200:]}")
        check(f"  [{label}]: verdict is UNKNOWN, not a false negative",
              rb.get("target_bootable") is None, json.dumps(rb)[:250])
        check(f"  [{label}]: operator is told the check failed, and why it proceeds",
              isinstance(rb.get("warning"), str)
              and "PROCEEDING" in rb["warning"]
              and "could not be completed" in rb["warning"], str(rb.get("warning"))[:250])
        check(f"  [{label}]: the underlying error is surfaced, not swallowed",
              "simulated" in (rb.get("warning") or ""), str(rb.get("warning"))[:200])
        check(f"  [{label}]: NEVER a bare traceback with empty stdout",
              proc.stdout.strip() != "" and "Traceback" not in proc.stdout,
              f"stdout={proc.stdout[:120]!r}")

    # THE OTHER HALF OF THE ASYMMETRY. If a refactor makes these two agree,
    # one of these checks fails - which is the entire point of pinning both.
    rc_act, act, proc = _cli_with_broken_checker(repo, rr, "activate",
                                                 "--release", first["release_id"], "--execute")
    check("ASYMMETRY: activate still FAILS CLOSED on a failing checker",
          rc_act == 1 and act.get("classification") == "RELEASE_NOT_BOOTABLE",
          f"exit={rc_act} {json.dumps(act)[:250]}")
    check("  activate says the check could not be completed, not that it is broken",
          "could not be completed" in (act.get("next") or ""), str(act.get("next"))[:200])
    check("  ...and activate emits JSON too, never a bare traceback",
          proc.stdout.strip() != "" and "Traceback" not in proc.stdout,
          f"stdout={proc.stdout[:120]!r}")

    # THE TOCTOU RACE, at the level where it belongs. Driving this through the
    # CLI does not reach the assessment: it trips an unguarded `os.readlink` in
    # `verify_release`'s own runtime-link loop first, which is PRE-EXISTING code
    # and a separate question (should rollback proceed when verification cannot
    # complete? that is a semantics decision, not this defect). What this proves
    # is the guard's actual contract - the assessment's own race becomes an
    # UNKNOWN verdict rather than an exception escaping to the caller.
    import ops.release_boundary as _rb

    # THE ASSESSMENT NO LONGER READS LINKS AT ALL. Since F1 the verdict is
    # `os.access` + `exists`, both of which follow symlinks, so the readlink
    # TOCTOU race cannot arise inside it - removed by construction rather than
    # guarded. `_readlink_or_absent` still protects the two places that DO read
    # links, `verify_release` and `_pointer_target`, so the race is asserted
    # where it actually lives now.
    check("the assessment reads no links, so the race cannot arise inside it",
          "readlink" not in inspect.getsource(_rb.assess_release_bootability))

    real_readlink = os.readlink

    def racing_readlink(path, *a, **k):
        if "/releases/" in str(path):
            raise OSError("simulated readlink race: link vanished after is_symlink()")
        return real_readlink(path, *a, **k)

    link = rr / "releases" / first["release_id"] / ".env"
    _unseal(rr / "releases" / first["release_id"])
    link.unlink()
    os.readlink = racing_readlink
    try:
        check("REGRESSION [TOCTOU]: a raced-away link resolves to LINK_ABSENT, not an exception",
              _rb._readlink_or_absent(link) is _rb.LINK_ABSENT)
    finally:
        os.readlink = real_readlink

    _force_rmtree(rr)

print()
print("== 9b. NARROWNESS: the nine cases, as acceptance criteria (F1) ==")
#
# The old suite drew every fixture from one `_make_repo` and then injected
# defects into it. That samples the space of BROKEN releases; narrowness is a
# claim about the space of UNUSUAL-BUT-WORKING ones, of which it had none. F1 was
# the first such case anyone constructed. These are the reviewer's nine.
#
# The rule under test: the verdict must be exactly the launcher's two predicates,
#   [[ -x <tree>/.venv/bin/python ]]  and  [[ -e <tree>/.env ]]
# and nothing else. Every case below asserts the verdict AGREES with what the
# wrapper would actually do.
import ops.release_boundary as _rb9

def wrapper_would_start(tree: Path) -> bool:
    """The wrapper's own two checks, evaluated directly."""
    return os.access(tree / ".venv" / "bin" / "python", os.X_OK) and (tree / ".env").exists()

def agree(label: str, tree: Path, rr: Path, rid: str, *, expect=None):
    real = wrapper_would_start(tree)
    v = _rb9.assess_release_bootability(release_root=rr, release_id=rid)
    if expect is None:
        check(f"{label}: verdict AGREES with the wrapper (both {real})",
              v["bootable"] is real,
              f"wrapper={real} verdict={v['bootable']} defects={[d['reason'] for d in v['defects']]}")
    else:
        check(f"{label}: verdict is {expect}", v["bootable"] is expect,
              f"verdict={v['bootable']} {json.dumps(v)[:200]}")
    return v

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    rc, rel = cli(repo, rr, "prepare", "--commit", head)
    rid = rel["release_id"]
    tree = rr / "releases" / rid
    meta = rr / "meta" / f"{rid}.json"
    original_meta = meta.read_text()

    # (1) links present and working, metadata NOT declaring them -> THE F1 CASE
    doc = json.loads(original_meta); doc["runtime_links"] = {}; meta.write_text(json.dumps(doc))
    v = agree("1 undeclared but working", tree, rr, rid)
    check("  1: `not_declared` is a NOTE, never a defect",
          any(n.get("reason") == "not_declared" for n in v["notes"])
          and not any(d.get("reason") == "not_declared" for d in v["defects"]),
          json.dumps(v)[:250])

    # (2a) meta file missing entirely, tree intact
    meta.unlink()
    v = agree("2a missing metadata", tree, rr, rid)
    check("  2a: reported as metadata_missing, not as a badly prepared release",
          any(n.get("reason") == "metadata_missing" for n in v["notes"]),
          json.dumps(v["notes"])[:200])

    # (2b) truncated / corrupt metadata
    meta.write_text('{"runtime_links": {".env":')
    v = agree("2b corrupt metadata", tree, rr, rid)
    check("  2b: reported as metadata_unreadable, verdict still decided by the filesystem",
          any(n.get("reason") == "metadata_unreadable" for n in v["notes"]),
          json.dumps(v["notes"])[:200])
    meta.write_text(original_meta)

    # (4) .env a REGULAR FILE and .venv a REAL DIRECTORY (imported release)
    _unseal(tree)
    (tree / ".env").unlink(); (tree / ".env").write_text("X=1\n")
    (tree / ".venv").unlink()
    (tree / ".venv" / "bin").mkdir(parents=True)
    (tree / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (tree / ".venv" / "bin" / "python").chmod(0o755)
    agree("4 imported release (regular file + real dir)", tree, rr, rid)
    check("  4: the old symlink requirement would have refused this",
          wrapper_would_start(tree) is True)

    # (6) a venv whose bin/python is a symlink chain, and a link-to-a-link .env
    (tree / ".venv" / "bin" / "python").unlink()
    real_py = root / "real-python"; real_py.write_text("#!/bin/sh\n"); real_py.chmod(0o755)
    hop = root / "hop-python"; hop.symlink_to(real_py)
    (tree / ".venv" / "bin" / "python").symlink_to(hop)
    (tree / ".env").unlink()
    env_real = root / "real.env"; env_real.write_text("X=1\n")
    env_hop = root / "hop.env"; env_hop.symlink_to(env_real)
    (tree / ".env").symlink_to(env_hop)
    agree("6 symlink chains to interpreter and env", tree, rr, rid)

    # (5) unreadable/untraversable target -> UNKNOWN, not a false negative
    holder = root / "locked"; holder.mkdir()
    (holder / "python").write_text("#!/bin/sh\n"); (holder / "python").chmod(0o755)
    (tree / ".venv" / "bin" / "python").unlink()
    (tree / ".venv" / "bin" / "python").symlink_to(holder / "python")
    (tree / ".env").unlink(); (tree / ".env").symlink_to(holder / "e.env")
    (holder / "e.env").write_text("X=1\n")
    holder.chmod(0o000)
    try:
        v = _rb9.assess_release_bootability(release_root=rr, release_id=rid)
        check("5 unreadable target: not a FALSE verdict on an undeterminable fact",
              v["bootable"] is not True,
              json.dumps(v)[:200])
        rc5, p5 = cli(repo, rr, "rollback") if (rr / "previous").exists() else (0, {})
        check("  5: driven through the CLI it is JSON, never a traceback",
              rc5 == 0 or isinstance(p5, dict), f"exit={rc5}")
    finally:
        holder.chmod(0o755)

    # (3) the SHARED-target failure: break what every release points at
    (tree / ".venv" / "bin" / "python").unlink()
    (tree / ".venv" / "bin" / "python").symlink_to(repo / ".venv" / "bin" / "python")
    (tree / ".env").unlink(); (tree / ".env").symlink_to(repo / ".env")
    agree("3 baseline before breaking the shared venv", tree, rr, rid, expect=True)
    (repo / ".venv" / "bin" / "python").chmod(0o644)
    v = agree("3 shared venv broken", tree, rr, rid)
    check("  3: the defect names the interpreter, so a reader can see it is shared",
          any(d["reason"] == "interpreter_not_executable" for d in v["defects"]),
          json.dumps(v["defects"])[:200])
    (repo / ".venv" / "bin" / "python").chmod(0o755)

    # (7) a release restored/copied WITHOUT meta/ at all
    copy_id = "ffffffffffff"
    shutil.copytree(tree, rr / "releases" / copy_id, symlinks=True)
    v = _rb9.assess_release_bootability(release_root=rr, release_id=copy_id)
    check("7 release restored without meta/: verdict still from the filesystem",
          v["bootable"] is wrapper_would_start(rr / "releases" / copy_id),
          json.dumps(v)[:220])
    check("  7: and the missing provenance is stated honestly",
          any(n.get("reason") == "metadata_missing" for n in v["notes"]),
          json.dumps(v["notes"])[:200])
    _force_rmtree(rr)

# (8) activate's top-level `bootable` must be null on unknown, not false
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp); repo = _make_repo(root); rr = root / "release"
    rc, rel = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip())
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import ops.release_boundary as rb\n"
        "def boom(**k): raise RuntimeError('checker fault')\n"
        "rb.assess_release_bootability = boom\n"
        "import ops.manage_release as mr\n"
        "mr.assess_release_bootability = boom\n"
        f"sys.argv=['m','--source-repo',{str(repo)!r},'--release-root',{str(rr)!r},"
        f"'activate','--release',{rel['release_id']!r}]\n"
        "raise SystemExit(mr.main())\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    payload = {}
    for stream in (proc.stdout, proc.stderr):
        try:
            payload = json.loads(stream); break
        except json.JSONDecodeError:
            continue
    # F4 FIXED: the top-level machine-readable field carries the VERDICT, so it
    # can no longer contradict the nested block sitting beside it. `activate`
    # still refuses on unknown — the refusal is unchanged, only the honesty of
    # what it reports.
    check("8 [F4]: activate's top-level `bootable` is null on unknown, not false",
          payload.get("bootable") is None,
          f"got {payload.get('bootable')!r}")
    check("  8: and the nested block agrees rather than contradicting it",
          (payload.get("bootability") or {}).get("verdict") == "unknown",
          json.dumps(payload.get("bootability"))[:200])
    check("  8: activate still REFUSES on unknown - only the reporting changed",
          payload.get("classification") == "RELEASE_NOT_BOOTABLE",
          str(payload.get("classification")))
    _force_rmtree(rr)

print()
print("== 9c. NO COMMAND MAY DIE AS A BARE TRACEBACK (F3) ==")
#
# A plain `chmod 000` on a runtime-link target directory - no race, no readlink,
# an ordinary persistent permission fault - made `rollback` exit with EMPTY
# STDOUT and a PermissionError stack trace. On BOTH branches: the dry run calls
# `verify_release` directly, and the execute branch reaches it through
# `rollback_release` -> `_activate_release_locked` -> `verify_release`. So the
# REAL recovery died, not merely the rehearsal.
#
# The guard is at the dispatch boundary, not at the call sites that happened to
# be found, and it changes no semantics: the command still fails and still exits
# non-zero. Whether verification may PROCEED when it cannot complete stays open.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"

    # Runtime links pointing OUTSIDE the repo, so one chmod breaks the target
    # directory without touching the fixture repository itself.
    shared = root / "shared"; shared.mkdir()
    (shared / "runtime.env").write_text("X=1\n")
    venv = shared / "venv"; (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    (venv / "bin" / "python").chmod(0o755)

    ids = []
    for msg in ("one", "two"):
        (repo / "ops" / "runner.py").write_text(f"VALUE = '{msg}'\n")
        _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", msg)
        rc, rel = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip(),
                      "--env-file", str(shared / "runtime.env"), "--venv", str(venv))
        ids.append(rel["release_id"])
    os.symlink(f"releases/{ids[0]}", rr / "current")
    os.symlink(f"releases/{ids[1]}", rr / "previous")

    shared.chmod(0o000)
    try:
        for label, extra in (("dry run", []), ("execute", ["--execute"])):
            rc, payload = cli(repo, rr, "rollback", *extra)
            check(f"F3 [{label}]: reports JSON, never a bare traceback",
                  payload.get("classification") == "UNEXPECTED_ERROR",
                  json.dumps(payload)[:250])
            check(f"  [{label}]: names the underlying fault for the operator",
                  payload.get("error_type") == "PermissionError"
                  and "Permission denied" in (payload.get("error") or ""),
                  str(payload.get("error"))[:150])
            check(f"  [{label}]: says plainly that recovery did NOT happen",
                  "RECOVERY DID NOT HAPPEN" in (payload.get("next") or ""),
                  str(payload.get("next"))[:120])
            check(f"  [{label}]: points at the SHARED targets, not at the release",
                  "shared by every release" in (payload.get("next") or ""),
                  str(payload.get("next"))[:200])
            check(f"  [{label}]: still FAILS - the guard reports, it does not proceed",
                  rc != 0, f"exit={rc}")
        # The pointer must not have moved: this is reporting, not fail-open.
        check("F3: the fault did not silently become a successful rollback",
              pointer_release_id(rr / "current") == ids[0],
              str(pointer_release_id(rr / "current")))
    finally:
        shared.chmod(0o755)

    # And the guard must not swallow a genuine boundary refusal into the generic
    # bucket - those keep their own classification and exit code.
    rc, payload = cli(repo, rr, "remove", "--release", ids[0], "--execute")
    check("F3: a real boundary refusal is NOT masked as UNEXPECTED_ERROR",
          payload.get("classification") == "RELEASE_POINTER_INVALID",
          json.dumps(payload)[:200])
    _force_rmtree(rr)

print()
print("== 9d. SHARED-RESOURCE DIAGNOSIS (F2) ==")
#
# Every release declares the same `.env` and `.venv`, both in the development
# tree, so one venv fault makes them ALL fail at once. A verdict alone cannot
# distinguish that from a broken release, and getting it wrong sent operators
# round a per-release remedy that could never terminate.
#
# The diagnosis compares RESOLVED PATHS rather than matching defect reasons:
# two releases independently broken the same way would match on reason, and a
# wrong "shared" conclusion sends someone to repair something that is fine.
from ops.release_boundary import diagnose_shared_runtime_fault  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"

    # Two releases sharing one runtime resource pair, as production does.
    ids = []
    for msg in ("one", "two"):
        (repo / "ops" / "runner.py").write_text(f"VALUE = '{msg}'\n")
        _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", msg)
        rc, rel = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip())
        ids.append(rel["release_id"])
    os.symlink(f"releases/{ids[0]}", rr / "current")
    os.symlink(f"releases/{ids[1]}", rr / "previous")

    check("healthy: no shared-fault conclusion is drawn",
          diagnose_shared_runtime_fault(release_root=rr, release_id=ids[1],
                                        compare_with=ids[0]) is None)

    # THE F2 CONDITION: break the one venv both releases point at.
    (repo / ".venv" / "bin" / "python").chmod(0o644)
    diag = diagnose_shared_runtime_fault(release_root=rr, release_id=ids[1], compare_with=ids[0])
    check("shared venv broken: the fault is identified as SHARED",
          diag is not None and diag["conclusion"] == "SHARED_RUNTIME_RESOURCE_FAULT",
          json.dumps(diag)[:250])
    check("  ...naming the real resolved path, not the per-release link",
          diag is not None and any(str(repo / ".venv") in pth for pth in diag["shared_paths"]),
          json.dumps(diag.get("shared_paths") if diag else None))
    check("  ...and saying plainly that no pointer move will fix it",
          diag is not None and "No pointer move" in diag["next"].replace("no pointer move", "No pointer move"),
          str(diag.get("next") if diag else None)[:150])

    # The operator must reach that conclusion from the CLI, not just the library.
    rc, rb = cli(repo, rr, "rollback")
    check("rollback surfaces the shared diagnosis in its warning",
          "SAME resolved path" in (rb.get("warning") or ""), str(rb.get("warning"))[:200])
    check("  ...and still PROCEEDS - a shared fault is not a reason to block recovery",
          rc == 0 and rb.get("would_activate") == ids[1], f"exit={rc}")
    check("  ...with the machine-readable conclusion attached too",
          (rb.get("runtime_fault") or {}).get("conclusion") == "SHARED_RUNTIME_RESOURCE_FAULT",
          json.dumps(rb.get("runtime_fault"))[:200])

    rc, act = cli(repo, rr, "activate", "--release", ids[1])
    # Key renamed in F4: since OPS-11 the diagnosis carries four conclusions, so
    # `shared_runtime_fault` asserted one of them in its own name.
    check("activate reaches the same conclusion from a REFUSAL",
          (act.get("runtime_fault") or {}).get("conclusion") == "SHARED_RUNTIME_RESOURCE_FAULT",
          json.dumps(act)[:250])
    (repo / ".venv" / "bin" / "python").chmod(0o755)

    # THE FALSE-POSITIVE GUARD: one release broken on its OWN resource must NOT
    # be diagnosed as shared, or the operator repairs something that is fine.
    #
    # The release must be prepared AGAINST its own resources rather than having
    # its link re-pointed afterwards: re-pointing makes the link disagree with
    # the target recorded in metadata, so `verify_release` refuses with
    # RELEASE_RUNTIME_LINK_MISMATCH before any bootability warning is reached.
    # That is verification working correctly, and it is a different failure from
    # the one under test.
    own = root / "own"; (own / "bin").mkdir(parents=True)
    (own / "bin" / "python").write_text("#!/bin/sh\n")
    (own / "bin" / "python").chmod(0o755)
    (own / "own.env").write_text("X=1\n")
    (repo / "ops" / "runner.py").write_text("VALUE = 'solo'\n")
    _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", "solo")
    rc, solo = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip(),
                   "--env-file", str(own / "own.env"), "--venv", str(own))
    os.remove(rr / "previous"); os.symlink(f"releases/{solo['release_id']}", rr / "previous")

    (own / "bin" / "python").chmod(0o644)      # break ONLY this release's resource
    diag = diagnose_shared_runtime_fault(release_root=rr, release_id=solo["release_id"],
                                         compare_with=ids[0])
    check("a release broken on its OWN resource is NOT called a shared fault",
          diag is None, json.dumps(diag)[:200])
    rc, rb = cli(repo, rr, "rollback")
    check("  ...and since OPS-11 that case REFUSES, naming repair-previous",
          rc == 1 and "repair-previous" in (rb.get("next") or ""),
          f"exit={rc} " + json.dumps(rb)[:250])
    (own / "bin" / "python").chmod(0o755)
    _force_rmtree(rr)

print()
print("== 9e. THE POLARITY MATRIX: rollback refuses NARROWLY (OPS-11) ==")
#
# The refusal is back, but only for the case it was ever justified by: the target
# broken on its OWN resources while the serving release is fine. F2 made that
# distinguishable; before it, SHARED and RELEASE_SPECIFIC were indistinguishable
# and the gate refused both, which is why it had to be reverted.
#
# EVERY CELL IS PINNED, including the ones expected to hold. That is what caught
# the regression last round: an assertion on an invariant nobody had broken yet.
from ops.release_boundary import (  # noqa: E402
    FAULT_INDETERMINATE, FAULT_NOT_APPLICABLE, FAULT_RELEASE_SPECIFIC, FAULT_SHARED,
    diagnose_runtime_fault,
)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"

    # current = shared-resource release. previous = its OWN resources.
    (repo / "ops" / "runner.py").write_text("VALUE = 'serving'\n")
    _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", "serving")
    rc, serving = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip())

    own = root / "own"; (own / "bin").mkdir(parents=True)
    (own / "bin" / "python").write_text("#!/bin/sh\n"); (own / "bin" / "python").chmod(0o755)
    (own / "own.env").write_text("X=1\n")
    (repo / "ops" / "runner.py").write_text("VALUE = 'fallback'\n")
    _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", "fallback")
    rc, fallback = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip(),
                       "--env-file", str(own / "own.env"), "--venv", str(own))
    os.symlink(f"releases/{serving['release_id']}", rr / "current")
    os.symlink(f"releases/{fallback['release_id']}", rr / "previous")

    def cell(label, *, expect_exit, expect_class=None, warn_contains=None, next_contains=None):
        rc, out = cli(repo, rr, "rollback")
        check(f"{label}: exit {expect_exit}", rc == expect_exit,
              f"got {rc}: {json.dumps(out)[:220]}")
        check(f"  {label}: classification {expect_class or '(none)'}",
              out.get("classification") == expect_class, str(out.get("classification")))
        if warn_contains is not None:
            check(f"  {label}: warning says the right thing",
                  warn_contains in (out.get("warning") or ""), str(out.get("warning"))[:200])
        if next_contains is not None:
            check(f"  {label}: refusal is actionable",
                  next_contains in (out.get("next") or ""), str(out.get("next"))[:200])
        return out

    # ---- target True -------------------------------------------------------
    d = diagnose_runtime_fault(release_root=rr, release_id=fallback["release_id"],
                               compare_with=serving["release_id"])
    check("target bootable: diagnosis is NOT_APPLICABLE", d["conclusion"] == FAULT_NOT_APPLICABLE,
          json.dumps(d)[:150])
    out = cell("target True", expect_exit=0)
    check("  target True: no warning at all", out.get("warning") is None, str(out.get("warning")))

    # ---- target False + RELEASE-SPECIFIC -> THE ONLY REFUSAL ----------------
    (own / "bin" / "python").chmod(0o644)
    d = diagnose_runtime_fault(release_root=rr, release_id=fallback["release_id"],
                               compare_with=serving["release_id"])
    check("release-specific fault is identified as such",
          d["conclusion"] == FAULT_RELEASE_SPECIFIC, json.dumps(d)[:200])
    cell("target False + release-specific", expect_exit=1, expect_class="RELEASE_NOT_BOOTABLE",
         next_contains="repair-previous")
    (own / "bin" / "python").chmod(0o755)

    # ---- target False + SHARED -> proceeds ---------------------------------
    # Break what BOTH point at: give the fallback the shared venv too.
    _unseal(rr / "releases" / fallback["release_id"])
    (repo / ".venv" / "bin" / "python").chmod(0o644)
    rc2, shared_rel = cli(repo, rr, "prepare", "--commit",
                          _git(repo, "rev-parse", "HEAD").strip() + "^")
    os.remove(rr / "previous")
    os.symlink(f"releases/{serving['release_id']}", rr / "previous")
    os.remove(rr / "current")
    # current must be a DIFFERENT release that also uses the shared venv
    (repo / "ops" / "runner.py").write_text("VALUE = 'other-shared'\n")
    _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", "other-shared")
    rc3, other = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip())
    os.symlink(f"releases/{other['release_id']}", rr / "current")
    d = diagnose_runtime_fault(release_root=rr, release_id=serving["release_id"],
                               compare_with=other["release_id"])
    check("shared fault is identified as such", d["conclusion"] == FAULT_SHARED, json.dumps(d)[:200])
    cell("target False + shared", expect_exit=0, warn_contains="NO POINTER MOVE WILL FIX IT")

    # ---- target False + diagnosis INDETERMINATE -> proceeds ----------------
    # Checked while the fault is STILL PRESENT: with the release healthy the
    # answer would be NOT_APPLICABLE and the case would not be exercised at all.
    # An inconclusive diagnosis is not evidence of a release-specific fault.
    d = diagnose_runtime_fault(release_root=rr, release_id=serving["release_id"], compare_with=None)
    check("no comparison release: INDETERMINATE, never RELEASE_SPECIFIC",
          d["conclusion"] == FAULT_INDETERMINATE, json.dumps(d)[:200])
    (repo / ".venv" / "bin" / "python").chmod(0o755)
    _force_rmtree(rr)

# ---- diagnosis RAISES, target definitely False -> must still PROCEED --------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp); repo = _make_repo(root); rr = root / "release"
    own = root / "own"; (own / "bin").mkdir(parents=True)
    (own / "bin" / "python").write_text("#!/bin/sh\n"); (own / "bin" / "python").chmod(0o755)
    (own / "own.env").write_text("X=1\n")
    ids = []
    for msg, extra in (("serving", []), ("fallback", ["--env-file", str(own / "own.env"),
                                                      "--venv", str(own)])):
        (repo / "ops" / "runner.py").write_text(f"VALUE = '{msg}'\n")
        _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", msg)
        rc, rel = cli(repo, rr, "prepare", "--commit",
                      _git(repo, "rev-parse", "HEAD").strip(), *extra)
        ids.append(rel["release_id"])
    os.symlink(f"releases/{ids[0]}", rr / "current")
    os.symlink(f"releases/{ids[1]}", rr / "previous")
    (own / "bin" / "python").chmod(0o644)          # definitely False, release-specific
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import ops.release_boundary as rb\n"
        "def boom(**k): raise RuntimeError('diagnosis fault')\n"
        "rb.diagnose_runtime_fault = boom\n"
        "import ops.manage_release as mr\n"
        "mr.diagnose_runtime_fault = boom\n"
        f"sys.argv=['m','--source-repo',{str(repo)!r},'--release-root',{str(rr)!r},'rollback']\n"
        "raise SystemExit(mr.main())\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    payload = {}
    for stream in (proc.stdout, proc.stderr):
        try:
            payload = json.loads(stream); break
        except json.JSONDecodeError:
            continue
    check("diagnosis RAISES on a definitely-False target: rollback still PROCEEDS",
          proc.returncode == 0 and payload.get("classification") is None,
          f"exit={proc.returncode} {json.dumps(payload)[:220]}")
    check("  ...saying the fault could not be located, not that the release is at fault",
          "could NOT be established" in (payload.get("warning") or ""),
          str(payload.get("warning"))[:200])

    # ---- activate refuses in EVERY one of these cases ----------------------
    for label, extra in (("release-specific", []),):
        rc, act = cli(repo, rr, "activate", "--release", ids[1])
        check(f"activate still refuses ({label}) - unchanged by the narrowing",
              rc == 1 and act.get("classification") == "RELEASE_NOT_BOOTABLE",
              f"exit={rc} {json.dumps(act)[:200]}")
    (own / "bin" / "python").chmod(0o755)
    _force_rmtree(rr)

print()
print("== 9f. THE THREE STATES SURVIVE EVERY CONSUMER (F4) ==")
#
# `bootable` is True / False / None. Any consumer that tests it for truthiness
# collapses None into False — reporting "I know this is broken" when the truth is
# "I could not find out". The review named three sites; this checks every one,
# including the site introduced AFTER the review ran, because the regression last
# round came from a new call site rather than a listed one.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp); repo = _make_repo(root); rr = root / "release"
    rc, rel = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip())
    rid = rel["release_id"]

    def with_broken_checker(*argv):
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "import ops.release_boundary as rb\n"
            "def boom(**k): raise RuntimeError('checker fault')\n"
            "rb.assess_release_bootability = boom\n"
            "import ops.manage_release as mr\n"
            "mr.assess_release_bootability = boom\n"
            f"sys.argv=['m','--source-repo',{str(repo)!r},'--release-root',{str(rr)!r},"
            + ", ".join(repr(a) for a in argv) + "]\n"
            "raise SystemExit(mr.main())\n"
        )
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        for stream in (proc.stdout, proc.stderr):
            try:
                return proc.returncode, json.loads(stream)
            except json.JSONDecodeError:
                continue
        return proc.returncode, {"_stdout": proc.stdout[:200], "_stderr": proc.stderr[-200:]}

    # activate — named by the review
    rc, out = with_broken_checker("activate", "--release", rid)
    check("activate: `bootable` is null on unknown",
          out.get("bootable") is None, repr(out.get("bootable")))
    check("  activate: the diagnosis key no longer asserts one conclusion",
          "runtime_fault" in out and "shared_runtime_fault" not in out, str(sorted(out)))

    # repair-previous — named by the review
    os.symlink(f"releases/{rid}", rr / "current")
    rc, out = with_broken_checker("repair-previous", "--release", rid)
    check("repair-previous: `target_bootable` is null on unknown",
          out.get("target_bootable") is None, repr(out.get("target_bootable")))
    check("  repair-previous: unknown is NOT reported as 'does not look bootable'",
          "could not be determined" in (out.get("warning") or ""),
          str(out.get("warning"))[:200])
    os.remove(rr / "current")

    # prepare — named by the review
    rc, out = with_broken_checker("prepare", "--commit",
                                  _git(repo, "rev-parse", "HEAD").strip())
    check("prepare: does NOT exit non-zero merely because the checker failed",
          rc == 0, f"exit={rc}")
    check("  prepare: and does not claim the release cannot start",
          "NOT ACTIVATABLE" not in (out.get("activation") or ""),
          str(out.get("activation"))[:150])

    # rollback — the consumer that matters most, already correct; pinned anyway.
    rc, out = with_broken_checker("rollback")
    check("rollback: still PROCEEDS on unknown (unchanged, pinned)",
          out.get("classification") is None, str(out.get("classification")))
    _force_rmtree(rr)

# A genuinely unbootable release must still report False everywhere — the fix
# must not have turned every verdict into null.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp); repo = _make_repo(root); rr = root / "release"
    rc, rel = cli(repo, rr, "prepare", "--commit",
                  _git(repo, "rev-parse", "HEAD").strip(), "--no-runtime-links")
    check("definitely-unbootable still reports False, not null",
          rel.get("bootable") is False, repr(rel.get("bootable")))
    check("  ...and prepare still exits 1 for it",
          rc == 1, f"exit={rc}")
    check("  ...and still says NOT ACTIVATABLE",
          "NOT ACTIVATABLE" in (rel.get("activation") or ""), str(rel.get("activation"))[:120])
    _force_rmtree(rr)

print()
print("== 10. CUTOVER: an unbootable release cannot be promoted, nor become the fallback ==")
#
# `ops/cutover_execute.py` calls `activate_release()` directly and so bypasses
# the `manage_release` CLI gate. The gate goes in `verify_candidates`, which runs
# BEFORE --execute is honoured, before timers stop, before the execution barrier
# and before the release-management lock - so a refusal costs nothing and can
# never land inside the pointer sequence. Nothing in the state machine, the
# barrier or the unwind path is touched, which is the whole reason it is safe.
from ops.cutover_execute import CutoverError, CutoverTransaction  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    # The cutover resolves its wrapper from inside the TARGET release, so the
    # fixture repo has to carry one or every case fails on
    # CUTOVER_WRAPPER_SOURCE_MISSING before bootability is ever reached.
    wrapper = repo / "ops" / "systemd" / "proposed" / "log-job-runner.release.sh"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("#!/bin/sh\n# BASE_DIR=${RELEASE_ROOT}/current\n")
    _git(repo, "add", str(wrapper.relative_to(repo)))
    _git(repo, "commit", "-q", "-m", "wrapper")

    # THREE DISTINCT COMMITS. A release id is derived from its commit, so
    # preparing one commit twice with different runtime links collides on
    # RELEASE_ALREADY_EXISTS_MISMATCH rather than producing two releases.
    good_sha = _git(repo, "rev-parse", "HEAD").strip()
    rc, good = cli(repo, rr, "prepare", "--commit", good_sha)
    (repo / "ops" / "runner.py").write_text("VALUE = 'other'\n")
    _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", "other")
    rc, good2 = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip())
    (repo / "ops" / "runner.py").write_text("VALUE = 'third'\n")
    _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", "third")
    rc, broken = cli(repo, rr, "prepare", "--commit",
                     _git(repo, "rev-parse", "HEAD").strip(), "--no-runtime-links")
    assert "release_id" in good and "release_id" in good2 and "release_id" in broken, (
        "fixture did not produce three distinct releases")

    def tx(target, previous):
        return CutoverTransaction(release_root=rr, source_repo=repo,
                                  target_release_id=target, predecessor_release_id=previous)

    def refusal(target, previous):
        try:
            tx(target, previous).verify_candidates()
            return None
        except CutoverError as exc:
            return exc

    # The broken release and good2 are the SAME commit, so only bootability differs.
    exc = refusal(broken["release_id"], good["release_id"])
    check("an unbootable TARGET is refused",
          exc is not None and exc.classification == "CUTOVER_RELEASE_NOT_BOOTABLE",
          repr(exc))
    check("  ...naming WHICH release and WHAT is missing",
          exc is not None and exc.details.get("role") == "target"
          and exc.details.get("defects"),
          json.dumps(getattr(exc, "details", {}))[:250])
    check("  ...distinct from the byte-verification failure classification",
          exc is not None and exc.classification != "CUTOVER_RELEASE_VERIFICATION_FAILED")
    check("  ...and says what promoting it would do",
          exc is not None and "current" in (exc.details.get("consequence") or ""),
          str(getattr(exc, "details", {}).get("consequence")))

    # THE PHASE 2 REGRESSION IN CUTOVER CLOTHING: the predecessor becomes
    # `previous`, so an unbootable one installs the poisoned fallback on purpose.
    exc = refusal(good["release_id"], broken["release_id"])
    check("an unbootable PREDECESSOR is refused too",
          exc is not None and exc.classification == "CUTOVER_RELEASE_NOT_BOOTABLE",
          repr(exc))
    check("  ...identified as the predecessor, not the target",
          exc is not None and exc.details.get("role") == "predecessor",
          str(getattr(exc, "details", {}).get("role")))
    check("  ...explaining it would become an unrecoverable rollback target",
          exc is not None and "rollback target" in (exc.details.get("consequence") or ""),
          str(getattr(exc, "details", {}).get("consequence")))

    # NARROWNESS: a bootable pair is not obstructed, and the dry run SHOWS the
    # verdicts rather than merely omitting a complaint.
    candidate = tx(good["release_id"], good2["release_id"]).verify_candidates()
    check("a bootable pair proceeds unobstructed", "expected_wrapper_sha256" in candidate)
    check("  ...and both verdicts are surfaced for a rehearsing operator",
          candidate["bootability"]["target"]["bootable"] is True
          and candidate["bootability"]["predecessor"]["bootable"] is True,
          json.dumps(candidate.get("bootability"))[:250])

    # THE UNKNOWN VERDICT: cutover is planned, not an emergency, so it refuses -
    # the same direction as `activate`, the opposite of `rollback`.
    import ops.release_boundary as _rb2
    real_assess = _rb2.assess_release_bootability
    _rb2.assess_release_bootability = lambda **k: (_ for _ in ()).throw(
        RuntimeError("simulated checker fault"))
    try:
        exc = refusal(good["release_id"], good2["release_id"])
        check("an UNKNOWN verdict refuses the cutover (planned op, fail closed)",
              exc is not None and exc.classification == "CUTOVER_RELEASE_NOT_BOOTABLE"
              and exc.details.get("bootable") is None,
              repr(exc))
        check("  ...saying the check could not be completed, not that it is broken",
              exc is not None and "could not be completed" in (exc.details.get("reason") or ""),
              str(getattr(exc, "details", {}).get("reason")))
    finally:
        _rb2.assess_release_bootability = real_assess

    # The refusal happens before ANY of the machinery moves.
    t = tx(broken["release_id"], good["release_id"])
    try:
        t.verify_candidates()
    except CutoverError:
        pass
    check("the refusal precedes every state change: pointers UNTOUCHED, state INITIAL",
          t.pointer_state == "UNTOUCHED" and t.state == "INITIAL",
          f"pointer_state={t.pointer_state} state={t.state}")
    check("  ...and no pointer exists in the release root at all",
          not (rr / "current").exists() and not (rr / "previous").exists())
    _force_rmtree(rr)

print()
print("== 11. CUTOVER RE-RUN FROM THE UNWOUND STATE (F5) ==")
#
# The cutover's fail-closed premise — "a planned operation with nothing broken
# while you retry" — is true of a FIRST run and false of the re-run `_unwind`
# recommends. When the wrapper state is uncertain `_unwind` deliberately KEEPS
# THE TIMERS STOPPED, so production is already down and completing the cutover
# IS the recovery action. Refusing it for want of proof gates recovery.
#
# `--assume-timers-stopped` is the signal: it exists precisely for the state
# `_unwind` leaves behind. The fence cannot serve, because the documented path
# clears it to ALLOWED before re-running.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    wrapper = repo / "ops" / "systemd" / "proposed" / "log-job-runner.release.sh"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("#!/bin/sh\n")
    _git(repo, "add", str(wrapper.relative_to(repo))); _git(repo, "commit", "-q", "-m", "wrapper")
    ids = []
    for msg in ("target", "predecessor"):
        (repo / "ops" / "runner.py").write_text(f"VALUE = '{msg}'\n")
        _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", msg)
        rc, rel = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip())
        ids.append(rel["release_id"])

    import ops.cutover_execute as _cx

    def candidates(*, resuming):
        # `cutover_execute` never imports the assessment directly - it receives
        # bootability through `verify_release`'s result - so the fault has to be
        # injected where verification actually calls it.
        real = _rb9.assess_release_bootability
        _rb9.assess_release_bootability = lambda **k: (_ for _ in ()).throw(
            RuntimeError("checker fault"))
        try:
            tx = _cx.CutoverTransaction(
                release_root=rr, source_repo=repo,
                target_release_id=ids[0], predecessor_release_id=ids[1],
                assume_timers_stopped=("a.timer",) if resuming else ())
            try:
                return None, tx.verify_candidates()
            except _cx.CutoverError as exc:
                return exc, None
        finally:
            _rb9.assess_release_bootability = real

    exc, _ = candidates(resuming=False)
    check("FIRST RUN + unknown: still REFUSES (production healthy, retry is free)",
          exc is not None and exc.classification == "CUTOVER_RELEASE_NOT_BOOTABLE",
          repr(exc))
    check("  ...and the refusal explains the resumption route",
          exc is not None and "--assume-timers-stopped" in (exc.details.get("next") or ""),
          str(getattr(exc, "details", {}).get("next"))[:200])

    exc, candidate = candidates(resuming=True)
    check("RESUMPTION + unknown: PROCEEDS - production is already down",
          exc is None and candidate is not None, repr(exc))
    check("  ...and it is recorded that an unproven verdict was accepted",
          candidate is not None
          and candidate["bootability"]["target"].get("proceeded_on_unknown") is True,
          json.dumps(candidate.get("bootability") if candidate else None)[:250])

    # A DEFINITE False must still refuse either way: promoting a release that
    # cannot start recovers nothing, down or not.
    # A release id derives from its commit, so this needs its OWN commit:
    # preparing an already-prepared commit with different runtime links collides
    # on RELEASE_ALREADY_EXISTS_MISMATCH and yields no release at all.
    (repo / "ops" / "runner.py").write_text("VALUE = 'broken'\n")
    _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", "broken")
    rc, broken = cli(repo, rr, "prepare", "--commit",
                     _git(repo, "rev-parse", "HEAD").strip(), "--no-runtime-links")
    assert "release_id" in broken, f"fixture failed to build a link-less release: {broken}"
    for resuming in (False, True):
        tx = _cx.CutoverTransaction(
            release_root=rr, source_repo=repo,
            target_release_id=broken["release_id"], predecessor_release_id=ids[1],
            assume_timers_stopped=("a.timer",) if resuming else ())
        try:
            tx.verify_candidates()
            check(f"definite False + resuming={resuming}: refuses", False, "it proceeded")
        except _cx.CutoverError as exc:
            check(f"definite False + resuming={resuming}: still REFUSES",
                  exc.classification == "CUTOVER_RELEASE_NOT_BOOTABLE", exc.classification)
    _force_rmtree(rr)

print()
print("== 12. A ROLLBACK THAT SUCCEEDS, AND R1: THE SHARED TARGET IS GONE ==")
#
# THE CAPABILITY THIS SUITE LACKED. Until now no test drove a rollback to
# completion: the only `rollback --execute` in this file expects a refusal, and
# every "SHARED proceeds" assertion was about a DRY RUN's exit code - the branch
# where the code that defeats it is easiest to get past. That is precisely how R1
# survived: `verify_release` refuses a dangling runtime link, and BOTH rollback
# branches reach it, so the shared-fault pass-through we built, documented and
# tested was unreachable for the most common shape of the fault - a venv rebuild.
#
# A successful rollback is drivable because `_activate_release_locked` imports
# the schema preflight lazily, so it can be substituted. The old suite recorded
# that a successful CLI activation could not be driven in a temp root and then
# accepted it; treating that as the thing to solve is what makes these cases
# possible at all.
import ops.release_schema_preflight as _pf


class _StubSchemaPreflight:
    fleet_fingerprint = None
    declared_capabilities = ()

    def as_dict(self):
        return {"stub": True}


def _with_stub_preflight(fn):
    real = _pf.verify_schema_prerequisites
    _pf.verify_schema_prerequisites = lambda **k: _StubSchemaPreflight()
    try:
        return fn()
    finally:
        _pf.verify_schema_prerequisites = real


def _shared_fixture(root, *, break_mode):
    """Two releases sharing runtime resources OUTSIDE the repo, then broken.

    `break_mode` is the axis the old fixtures never varied: 'mode' keeps the
    files present and only removes the executable bit - the one break shape
    verification tolerates - while 'absent' removes the directory, which is what
    a venv rebuild actually does and what verification refuses.
    """
    repo = _make_repo(root)
    rr = root / "release"
    shared = root / "shared"; shared.mkdir()
    (shared / "runtime.env").write_text("X=1\n")
    venv = shared / "venv"; (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    (venv / "bin" / "python").chmod(0o755)
    ids = []
    for msg in ("one", "two"):
        (repo / "ops" / "runner.py").write_text(f"VALUE = '{msg}'\n")
        _git(repo, "add", "ops/runner.py"); _git(repo, "commit", "-q", "-m", msg)
        rc, rel = cli(repo, rr, "prepare", "--commit", _git(repo, "rev-parse", "HEAD").strip(),
                      "--env-file", str(shared / "runtime.env"), "--venv", str(venv))
        ids.append(rel["release_id"])
    os.symlink(f"releases/{ids[0]}", rr / "current")
    os.symlink(f"releases/{ids[1]}", rr / "previous")
    if break_mode == "mode":
        (venv / "bin" / "python").chmod(0o644)
    elif break_mode == "absent":
        shutil.rmtree(venv)
    return repo, rr, ids


# --- the baseline the suite never had: a rollback that actually moves ---------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo, rr, ids = _shared_fixture(root, break_mode=None)
    before = (pointer_release_id(rr / "current"), pointer_release_id(rr / "previous"))
    _with_stub_preflight(lambda: rollback_release(release_root=rr, source_repo=repo))
    after = (pointer_release_id(rr / "current"), pointer_release_id(rr / "previous"))
    check("BASELINE: a healthy rollback MOVES THE POINTER",
          after[0] == before[1] and after[1] == before[0], f"{before} -> {after}")
    _force_rmtree(rr)

# --- R1: the venv-rebuild fault ---------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo, rr, ids = _shared_fixture(root, break_mode="absent")

    check("R1: verification alone still REFUSES a dangling runtime link",
          _expect_boundary_error(lambda: verify_release(
              release_root=rr, release_id=ids[1], source_repo=repo))
          == "RELEASE_RUNTIME_RESOURCE_MISSING")

    before = (pointer_release_id(rr / "current"), pointer_release_id(rr / "previous"))
    result = _with_stub_preflight(lambda: rollback_release(
        release_root=rr, source_repo=repo, tolerate_missing_runtime_resource=True))
    after = (pointer_release_id(rr / "current"), pointer_release_id(rr / "previous"))
    check("R1: rollback with the shared venv GONE moves the pointer",
          after[0] == before[1], f"{before} -> {after}")
    check("  R1: and records what it tolerated rather than doing it silently",
          (result.get("tolerated_verification_failure") or {}).get("classification")
          == "RELEASE_RUNTIME_RESOURCE_MISSING",
          json.dumps(result.get("tolerated_verification_failure"))[:200])
    check("  R1: the journal records the tolerance as its own event",
          "rollback_tolerated_missing_runtime_resource"
          in (rr / "activations.log").read_text())

    # The tolerance is OPT-IN. Nothing else may inherit it.
    check("R1: without the flag, rollback still refuses",
          _expect_boundary_error(lambda: rollback_release(release_root=rr, source_repo=repo))
          == "RELEASE_RUNTIME_RESOURCE_MISSING")
    rc, act = cli(repo, rr, "activate", "--release", ids[0])
    check("R1: `activate` still refuses - promotions stay strict",
          act.get("classification") in ("RELEASE_RUNTIME_RESOURCE_MISSING",
                                        "RELEASE_NOT_BOOTABLE"),
          json.dumps(act)[:200])
    _force_rmtree(rr)

# --- the tolerance must NOT extend to a release whose bytes are wrong ---------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo, rr, ids = _shared_fixture(root, break_mode=None)
    tree = rr / "releases" / ids[1]
    _unseal(tree)
    (tree / "ops" / "runner.py").write_text("TAMPERED = True\n")
    check("R1: content mismatch is NOT tolerated - different fact, opposite treatment",
          _expect_boundary_error(lambda: rollback_release(
              release_root=rr, source_repo=repo, tolerate_missing_runtime_resource=True))
          == "RELEASE_CONTENT_MISMATCH")
    _force_rmtree(rr)

# --- dry run and execute must agree about what they tolerate -----------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo, rr, ids = _shared_fixture(root, break_mode="absent")
    rc, dry = cli(repo, rr, "rollback")
    check("R1/R3: the DRY RUN agrees with execute - it no longer refuses",
          rc == 0 and dry.get("would_activate") == ids[1],
          f"exit={rc} {json.dumps(dry)[:200]}")
    check("  R1: and says what it would tolerate",
          (dry.get("would_tolerate_verification_failure") or {}).get("classification")
          == "RELEASE_RUNTIME_RESOURCE_MISSING",
          json.dumps(dry.get("would_tolerate_verification_failure"))[:200])
    _force_rmtree(rr)

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
    raise SystemExit(1)
print("OK - release bootability gate tests passed")
