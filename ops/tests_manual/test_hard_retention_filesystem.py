#!/usr/bin/env python3
"""Filesystem hard retention: the cutoff, and the guards around `unlink()`.

Run:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_hard_retention_filesystem.py

Temporary directories only. Nothing here reads or writes a real platform path.

WHAT THIS PROVES

  * files older than the 13-calendar-month cutoff are removed and younger ones
    are not, measured at the boundary rather than in the middle;
  * a dry run mutates nothing and reports the same candidates an execute run
    would delete;
  * the sweep cannot be pointed outside its approved roots — not by an absolute
    path, not by `..`, not by a symlink, and not by an environment variable;
  * a symlinked directory is never descended and a symlinked FILE is never
    unlinked, so a link planted inside a governed root cannot be used to reach
    something outside it;
  * `/`, `/home`, `/tmp` and the account home are refused outright.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import hard_retention as hr  # noqa: E402
from ops import retention_registry as rr  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
CUTOFF = rr.hard_retention_cutoff(NOW)

PASSED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}" if detail else label)


def write(path: Path, *, at: datetime) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    stamp = at.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def sweep(root: Path, *, dry_run: bool):
    return hr.sweep_filesystem(
        hr.FilesystemSweep(policy_id="filesystem.workflow_b_stage2_cleaned", root=root),
        cutoff=CUTOFF, dry_run=dry_run,
    )


def approved(tmp: Path):
    """Make `tmp` an approved root for the duration of one call."""
    original = hr.approved_filesystem_prefixes
    hr.approved_filesystem_prefixes = lambda: (tmp.resolve(),)
    return original


def test_the_cutoff_decides_and_the_boundary_is_exact() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        original = approved(tmp)
        try:
            old = write(tmp / "old.csv", at=CUTOFF - timedelta(seconds=1))
            edge = write(tmp / "edge.csv", at=CUTOFF)
            young = write(tmp / "young.csv", at=CUTOFF + timedelta(seconds=1))
            nested = write(tmp / "a" / "b" / "deep.csv", at=CUTOFF - timedelta(days=400))

            plan = sweep(tmp, dry_run=True)
            check("a dry run deletes nothing", plan.deleted == 0)
            check("and finds both eligible files", plan.examined == 2, str(plan.examined))
            check("all four files are still present",
                  all(path.exists() for path in (old, edge, young, nested)))
            check("the dry run reports the oldest eligible file",
                  plan.oldest_remaining is not None
                  and plan.oldest_remaining < CUTOFF)

            done = sweep(tmp, dry_run=False)
            check("the execute run removes exactly the eligible files",
                  done.deleted == 2, str(done.deleted))
            check("the file one second older than the cutoff is gone", not old.exists())
            check("a nested eligible file is reached", not nested.exists())
            check("the file exactly AT the cutoff survives — eligibility is strict",
                  edge.exists())
            check("and the younger file survives", young.exists())
            check("nothing eligible remains", done.oldest_remaining is None)

            again = sweep(tmp, dry_run=False)
            check("a second pass is a no-op", again.deleted == 0 and again.examined == 0)
            check("and classifies as success",
                  again.classification == "RETENTION_EXECUTION_SUCCEEDED")
        finally:
            hr.approved_filesystem_prefixes = original
    PASSED.append("the_cutoff_decides_and_the_boundary_is_exact")


def test_a_symlink_is_never_followed_and_never_unlinked() -> None:
    with tempfile.TemporaryDirectory() as raw_inside, \
            tempfile.TemporaryDirectory() as raw_outside:
        inside, outside = Path(raw_inside), Path(raw_outside)
        original = approved(inside)
        try:
            victim = write(outside / "precious.csv", at=CUTOFF - timedelta(days=500))
            victim_dir = outside / "tree"
            write(victim_dir / "also_precious.csv", at=CUTOFF - timedelta(days=500))

            # A symlinked FILE planted inside the governed root, and a symlinked
            # DIRECTORY pointing at a whole tree outside it. Both are ways to
            # make a recursive deleter reach somewhere it was never approved for.
            (inside / "link_to_file.csv").symlink_to(victim)
            (inside / "link_to_tree").symlink_to(victim_dir, target_is_directory=True)
            doomed = write(inside / "real_old.csv", at=CUTOFF - timedelta(days=500))

            done = sweep(inside, dry_run=False)
            check("the real eligible file inside the root is removed", not doomed.exists())
            check("the symlinked file itself is not unlinked",
                  (inside / "link_to_file.csv").is_symlink())
            check("its target is untouched", victim.exists())
            check("the symlinked directory is not descended",
                  (victim_dir / "also_precious.csv").exists())
            check("and only the one real file was counted",
                  done.deleted == 1, str(done.deleted))
        finally:
            hr.approved_filesystem_prefixes = original
    PASSED.append("a_symlink_is_never_followed_and_never_unlinked")


def test_the_sweep_refuses_every_root_it_was_not_approved_for() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        original = approved(tmp)
        try:
            for label, candidate in (
                ("an unrelated absolute path", Path("/var/log")),
                ("a traversal out of the root", tmp / ".." / ".."),
                ("a relative path", Path("relative/path")),
            ):
                outcome = hr.sweep_filesystem(
                    hr.FilesystemSweep(
                        policy_id="filesystem.workflow_b_stage2_cleaned", root=candidate,
                    ),
                    cutoff=CUTOFF, dry_run=False,
                )
                check(f"{label} is refused",
                      outcome.classification == "RETENTION_FAILED"
                      and outcome.defect_code == "UNSAFE_ROOT",
                      f"{outcome.classification}/{outcome.defect_code}")
                check(f"{label} deletes nothing", outcome.deleted == 0)
        finally:
            hr.approved_filesystem_prefixes = original

    # The structural floor holds regardless of what is approved.
    for destructive in ("/", "/home", "/tmp", "/etc", "/usr", "/var"):
        try:
            hr._structurally_safe(Path(destructive))
        except hr.UnsafeRoot:
            continue
        raise AssertionError(f"{destructive} was accepted as a retention root")
    try:
        hr._structurally_safe(Path.home())
    except hr.UnsafeRoot:
        pass
    else:
        raise AssertionError("the account home was accepted as a retention root")
    PASSED.append("the_sweep_refuses_every_root_it_was_not_approved_for")


def test_configured_roots_are_accepted_only_after_the_floor() -> None:
    """`REPORTS_DATA_DIR` is operator configuration and genuinely differs between
    the repository default and the production host, so it must be honoured —
    but it must not be able to widen the walk to a system directory."""
    previous = os.environ.get("REPORTS_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as raw:
            os.environ["REPORTS_DATA_DIR"] = raw
            prefixes = hr.approved_filesystem_prefixes()
            check("a legitimate configured root is approved",
                  Path(raw).resolve() in prefixes, str(prefixes))
            check("and the stage-2 scratch root is always approved too",
                  hr._STAGE2_ROOT.resolve() in prefixes, str(prefixes))

        os.environ["REPORTS_DATA_DIR"] = "/"
        check("a configured root of / is dropped, not approved",
              Path("/") not in hr.approved_filesystem_prefixes())
        os.environ["REPORTS_DATA_DIR"] = "/home"
        check("and neither is /home",
              Path("/home") not in hr.approved_filesystem_prefixes())
    finally:
        if previous is None:
            os.environ.pop("REPORTS_DATA_DIR", None)
        else:
            os.environ["REPORTS_DATA_DIR"] = previous
    PASSED.append("configured_roots_are_accepted_only_after_the_floor")


def test_an_absent_root_is_reported_not_failed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        original = approved(tmp)
        try:
            outcome = sweep(tmp / "never-created", dry_run=False)
            check("an absent root is not a failure",
                  outcome.failed == 0, str(outcome.as_dict()))
            check("and it says why", outcome.defect_code == "RELATION_ABSENT")
        finally:
            hr.approved_filesystem_prefixes = original
    PASSED.append("an_absent_root_is_reported_not_failed")


def test_the_walk_is_bounded() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        original = approved(tmp)
        try:
            for index in range(20):
                write(tmp / f"f{index}.csv", at=CUTOFF - timedelta(days=500))
            outcome = hr.sweep_filesystem(
                hr.FilesystemSweep(
                    policy_id="filesystem.workflow_b_stage2_cleaned", root=tmp,
                ),
                cutoff=CUTOFF, dry_run=False, max_files=5,
            )
            check("the walk stops at the limit", outcome.deleted == 5, str(outcome.deleted))
            remaining = len(list(tmp.iterdir()))
            check("and the rest are left for the next call", remaining == 15, str(remaining))
        finally:
            hr.approved_filesystem_prefixes = original
    PASSED.append("the_walk_is_bounded")


def main() -> int:
    test_the_cutoff_decides_and_the_boundary_is_exact()
    test_a_symlink_is_never_followed_and_never_unlinked()
    test_the_sweep_refuses_every_root_it_was_not_approved_for()
    test_configured_roots_are_accepted_only_after_the_floor()
    test_an_absent_root_is_reported_not_failed()
    test_the_walk_is_bounded()
    for name in PASSED:
        print(f"PASS {name}")
    print(f"\n{len(PASSED)} checks passed — filesystem hard retention")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
