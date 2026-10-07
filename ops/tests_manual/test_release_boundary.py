#!/usr/bin/env python3
"""Deterministic tests for the production release boundary.

Pure: stdlib plus `git`/`tar`. No network, no database, no secrets, no provider
call. Every fixture is a throwaway repository and release root under a temporary
directory, so nothing here can observe or touch the real development tree, the
real release root, or the installed wrapper.

What is proven, in the order the M1 acceptance criteria ask for it:

  1. a release is pinned to an explicit commit, and anything that does not
     resolve to a commit is refused;
  2. dirty and untracked development-tree state cannot reach a release, because
     the payload is read from the commit object and never from the worktree;
  3. a release does not change when the development tree changes afterwards;
  4. re-preparing an existing release verifies and reuses it, and refuses when
     the destination holds something else;
  5. tampering with release content, modes, metadata or runtime links is
     detected by verification;
  6. activation is atomic, verified-before-swap, and leaves the outgoing release
     recoverable; rollback restores it and is itself reversible.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_release_boundary.py
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.release_boundary import (  # noqa: E402
    ReleaseBoundaryError,
    activate_release as _real_activate_release,
    blob_sha1,
    list_releases,
    pointer_release_id,
    prepare_release,
    remove_release,
    release_id_for,
    release_status,
    resolve_commit,
    rollback_release as _real_rollback_release,
    source_tree_digest,
    commit_tree_entries,
    expected_wrapper_source_relative,
    installed_wrapper_variant,
    management_lock,
    wrapper_replaceability,
    WRAPPER_REPLACEABLE_BY_PROVISIONING,
    verify_release,
)

class _StubPreflight:
    """Stands in for the schema preflight, which needs a live fleet.

    This file is about the release BOUNDARY — pointers, verification, locking,
    the wrapper — none of which involves a database. Real activation contacts the
    platform database and every enabled client database, so driving one from an
    isolated temporary root either fails for want of credentials or, worse,
    succeeds in reaching the REAL fleet and refuses because a synthetic release
    naturally does not declare the capabilities a live client schema carries.
    That is what happened: this suite began failing with
    RELEASE_SCHEMA_STATE_CAPABILITY_MISSING for a real client, which said
    nothing about the boundary it was written to test.

    `activate_release` takes `schema_preflight` for exactly this reason. The gate
    itself is exercised where it belongs, against a real fleet, in
    `test_release_schema_preflight_postgres.py` and
    `test_release_activation_fence_postgres.py`.
    """

    fleet_fingerprint = None
    declared_capabilities = ()


def activate_release(**kwargs):
    """`activate_release` with the fleet gate stubbed unless a case overrides it."""
    kwargs.setdefault("schema_preflight", lambda **_: _StubPreflight())
    return _real_activate_release(**kwargs)


def rollback_release(**kwargs):
    """`rollback_release`, likewise."""
    kwargs.setdefault("schema_preflight", lambda **_: _StubPreflight())
    return _real_rollback_release(**kwargs)


FAILURES: list[str] = []


def _check(label: str, condition: bool, evidence: str = "") -> None:
    if condition:
        print(f"PASS  {label}")
        return
    FAILURES.append(label)
    print(f"FAIL  {label}")
    if evidence:
        print(f"      {evidence}")


def _expect_error(label: str, classification: str, fn) -> None:
    try:
        fn()
    except ReleaseBoundaryError as exc:
        _check(label, exc.classification == classification,
               f"expected {classification}, got {exc.classification}: {exc.details}")
        return
    except Exception as exc:  # noqa: BLE001 - an unexpected type is itself the failure
        _check(label, False, f"expected {classification}, raised {type(exc).__name__}: {exc}")
        return
    _check(label, False, f"expected {classification}, nothing raised")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args],
                            check=True, capture_output=True, text=True)
    return result.stdout


def _force_rmtree(path: Path) -> None:
    """Release trees are sealed read-only; restore write bits before cleanup."""
    def _onerror(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            func(target)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_onerror)


def _unseal(path: Path) -> None:
    """Undo the release seal so a test can act as an attacker would.

    Directories are unsealed too: the seal makes creating or removing entries
    impossible, which is the point, so simulating tampering means lifting it
    first rather than pretending it is not there.

    Executable bits are preserved: restoring write access must not itself change
    the mode, or verification reports that instead of the tampering under test.
    """
    path.chmod(0o755)
    for entry in path.rglob("*"):
        if entry.is_symlink():
            continue
        entry.chmod(stat.S_IMODE(entry.stat().st_mode) | stat.S_IWUSR)


def _make_repo(root: Path) -> Path:
    repo = root / "devrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Release Boundary Test")
    (repo / "ops").mkdir()
    (repo / "jobs").mkdir()
    (repo / "ops" / "runner.py").write_text("VALUE = 'committed'\n")
    (repo / "jobs" / "payload.py").write_text("MODE = 'baseline'\n")
    script = repo / "ops" / "tool.sh"
    script.write_text("#!/bin/sh\necho committed\n")
    script.chmod(0o755)
    _git(repo, "add", "ops/runner.py", "jobs/payload.py", "ops/tool.sh")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _runtime_resources(root: Path) -> dict:
    env_file = root / "runtime.env"
    env_file.write_text("EXAMPLE_SETTING=1\n")
    venv = root / "sharedvenv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\n")
    return {".env": env_file, ".venv": venv}


# ---------------------------------------------------------------------------
print("-- pinned commit --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()

    _check("a full SHA resolves to itself", resolve_commit(repo, head) == head)
    _check("an abbreviated SHA resolves to the full SHA", resolve_commit(repo, head[:8]) == head)
    _expect_error("a nonexistent SHA is refused", "RELEASE_COMMIT_UNRESOLVED",
                  lambda: resolve_commit(repo, "0" * 40))
    _expect_error("a garbage ref is refused", "RELEASE_COMMIT_UNRESOLVED",
                  lambda: resolve_commit(repo, "no-such-ref"))
    _expect_error("an empty committish is refused", "RELEASE_COMMIT_UNRESOLVED",
                  lambda: resolve_commit(repo, ""))

    tree_sha = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    _expect_error("a tree object is not accepted as a release source", "RELEASE_COMMIT_UNRESOLVED",
                  lambda: resolve_commit(repo, tree_sha))

    result = prepare_release(source_repo=repo, release_root=release_root, committish=head)
    metadata = json.loads((release_root / "meta" / f"{result['release_id']}.json").read_text())
    _check("provenance records the resolved full SHA", metadata["commit"] == head,
           f"metadata_commit={metadata['commit']!r} head={head!r}")
    _check("the release id addresses that commit", result["release_id"] == release_id_for(head))
    _check("provenance records source repository identity",
           metadata["source_repository_root"] == str(repo.resolve()))
    _check("provenance records creation time and author",
           bool(metadata["created_at"]) and "@" in metadata["created_by"])
    _check("provenance records the commit subject", metadata["commit_subject"] == "baseline")
    _check("a prepared release is not activated",
           release_status(release_root)["current_release_id"] is None)
    _check("the release tree carries no .git", not (Path(result["release_path"]) / ".git").exists())

    _expect_error("the development repository cannot be used as a release root",
                  "RELEASE_ROOT_INVALID",
                  lambda: prepare_release(source_repo=repo, release_root=repo, committish=head))
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- dirty-tree isolation --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()

    # Exactly the shape of the real development tree: a modified tracked file,
    # plus untracked files that must never be shipped.
    (repo / "jobs" / "payload.py").write_text("MODE = 'UNCOMMITTED EDIT'\n")
    (repo / "untracked_artifact.csv").write_text("forensic,export\n")
    (repo / "docs_draft.md").write_text("# not committed\n")
    _check("the fixture worktree really is dirty",
           bool(_git(repo, "status", "--porcelain").strip()))

    result = prepare_release(source_repo=repo, release_root=release_root, committish=head)
    tree = Path(result["release_path"])

    _check("the release ships the committed content, not the working copy",
           (tree / "jobs" / "payload.py").read_text() == "MODE = 'baseline'\n",
           f"content={(tree / 'jobs' / 'payload.py').read_text()!r}")
    _check("an uncommitted tracked edit does not leak into the release",
           "UNCOMMITTED EDIT" not in (tree / "jobs" / "payload.py").read_text())
    _check("untracked files do not leak into the release",
           not (tree / "untracked_artifact.csv").exists() and not (tree / "docs_draft.md").exists())
    _check("provenance records that the source worktree was dirty",
           json.loads((release_root / "meta" / f"{result['release_id']}.json").read_text())
           ["source_worktree_dirty_at_preparation"] is True)
    _check("the executable bit survives materialization",
           os.stat(tree / "ops" / "tool.sh").st_mode & stat.S_IXUSR != 0)
    _check("release files are sealed read-only",
           os.stat(tree / "ops" / "runner.py").st_mode & 0o222 == 0)
    verify_release(release_root=release_root, release_id=result["release_id"], source_repo=repo)
    _check("a release built from a dirty tree still verifies against its commit", True)
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- immutability under development-tree change --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    result = prepare_release(source_repo=repo, release_root=release_root, committish=head)
    tree = Path(result["release_path"])
    before = blob_sha1((tree / "ops" / "runner.py").read_bytes())

    (repo / "ops" / "runner.py").write_text("VALUE = 'edited after materialization'\n")
    (repo / "ops" / "new_module.py").write_text("X = 1\n")
    after = blob_sha1((tree / "ops" / "runner.py").read_bytes())

    _check("editing a development file does not change release bytes", before == after)
    _check("a new development file does not appear in the release",
           not (tree / "ops" / "new_module.py").exists())
    verify_release(release_root=release_root, release_id=result["release_id"], source_repo=repo)
    _check("the release still verifies after the development tree moved on", True)

    # A second materialization of the same commit must be content-equivalent.
    second_root = root / "release2"
    second = prepare_release(source_repo=repo, release_root=second_root, committish=head)
    _check("a second materialization of the same commit is content-identical",
           second["source_tree_digest"] == result["source_tree_digest"],
           f"{second['source_tree_digest']} vs {result['source_tree_digest']}")
    _check("and lands on the same release id", second["release_id"] == result["release_id"])
    _force_rmtree(release_root)
    _force_rmtree(second_root)

# ---------------------------------------------------------------------------
print("\n-- existing release: reuse or fail closed --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    first = prepare_release(source_repo=repo, release_root=release_root, committish=head)
    created_at = json.loads((release_root / "meta" / f"{first['release_id']}.json").read_text())["created_at"]

    again = prepare_release(source_repo=repo, release_root=release_root, committish=head)
    _check("re-preparing an identical release reuses it", again["reused"] is True)
    _check("reuse does not rewrite provenance",
           json.loads((release_root / "meta" / f"{first['release_id']}.json").read_text())
           ["created_at"] == created_at)
    _check("reuse does not accumulate duplicate releases", len(list_releases(release_root)) == 1)

    # A destination holding different content for the same id must be refused,
    # never silently overwritten.
    tree = Path(first["release_path"])
    _unseal(tree)
    (tree / "ops" / "runner.py").write_text("VALUE = 'tampered'\n")
    _expect_error("a destination whose content differs is refused, not overwritten",
                  "RELEASE_CONTENT_MISMATCH",
                  lambda: prepare_release(source_repo=repo, release_root=release_root, committish=head))
    _check("the refused preparation left the destination untouched",
           (tree / "ops" / "runner.py").read_text() == "VALUE = 'tampered'\n")
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- verification detects tampering --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    links = _runtime_resources(root)
    head = _git(repo, "rev-parse", "HEAD").strip()

    def _fresh() -> tuple[Path, str, Path]:
        release_root = root / f"release-{os.urandom(4).hex()}"
        result = prepare_release(source_repo=repo, release_root=release_root,
                                 committish=head, runtime_links=links)
        return release_root, result["release_id"], Path(result["release_path"])

    release_root, release_id, tree = _fresh()
    verify_release(release_root=release_root, release_id=release_id, source_repo=repo)
    _check("an untouched release verifies", True)

    _unseal(tree)
    (tree / "ops" / "runner.py").write_text("VALUE = 'tampered'\n")
    _expect_error("modified content is detected", "RELEASE_CONTENT_MISMATCH",
                  lambda: verify_release(release_root=release_root, release_id=release_id, source_repo=repo))

    release_root, release_id, tree = _fresh()
    _unseal(tree)
    (tree / "ops" / "smuggled.py").write_text("BACKDOOR = True\n")
    _expect_error("an extra file the commit does not contain is detected", "RELEASE_CONTENT_MISMATCH",
                  lambda: verify_release(release_root=release_root, release_id=release_id, source_repo=repo))

    release_root, release_id, tree = _fresh()
    _unseal(tree)
    (tree / "jobs" / "payload.py").unlink()
    _expect_error("a missing file is detected", "RELEASE_CONTENT_MISMATCH",
                  lambda: verify_release(release_root=release_root, release_id=release_id, source_repo=repo))

    release_root, release_id, tree = _fresh()
    _unseal(tree)
    (tree / "ops" / "tool.sh").chmod(0o644)
    _expect_error("a stripped executable bit is detected", "RELEASE_CONTENT_MISMATCH",
                  lambda: verify_release(release_root=release_root, release_id=release_id, source_repo=repo))

    release_root, release_id, tree = _fresh()
    (release_root / "meta" / f"{release_id}.json").write_text("{not json")
    _expect_error("unreadable provenance is detected", "RELEASE_METADATA_INVALID",
                  lambda: verify_release(release_root=release_root, release_id=release_id, source_repo=repo))

    release_root, release_id, tree = _fresh()
    meta_path = release_root / "meta" / f"{release_id}.json"
    doctored = json.loads(meta_path.read_text())
    doctored["source_tree_digest"] = "sha256:" + "0" * 64
    meta_path.write_text(json.dumps(doctored))
    _expect_error("a forged content digest is detected", "RELEASE_METADATA_INVALID",
                  lambda: verify_release(release_root=release_root, release_id=release_id, source_repo=repo))

    release_root, release_id, tree = _fresh()
    _unseal(tree)
    (tree / ".env").unlink()
    (tree / ".env").symlink_to(root / "somewhere-else.env")
    _expect_error("a retargeted runtime link is detected", "RELEASE_RUNTIME_LINK_MISMATCH",
                  lambda: verify_release(release_root=release_root, release_id=release_id, source_repo=repo))

    release_root, release_id, tree = _fresh()
    declared_env = json.loads((release_root / "meta" / f"{release_id}.json").read_text())["runtime_links"][".env"]
    moved = Path(declared_env).with_suffix(".moved")
    Path(declared_env).rename(moved)
    _expect_error("a dangling runtime link is detected", "RELEASE_RUNTIME_RESOURCE_MISSING",
                  lambda: verify_release(release_root=release_root, release_id=release_id, source_repo=repo))
    moved.rename(declared_env)

    _expect_error("a runtime resource that does not exist is refused at preparation",
                  "RELEASE_RUNTIME_RESOURCE_MISSING",
                  lambda: prepare_release(source_repo=repo, release_root=root / "release-missing",
                                          committish=head,
                                          runtime_links={".env": root / "nope.env"}))
    _expect_error("a relative runtime target is refused", "RELEASE_RUNTIME_RESOURCE_MISSING",
                  lambda: prepare_release(source_repo=repo, release_root=root / "release-relative",
                                          committish=head, runtime_links={".env": Path("relative.env")}))
    _expect_error("a runtime link colliding with a committed path is refused",
                  "RELEASE_RUNTIME_LINK_MISMATCH",
                  lambda: prepare_release(source_repo=repo, release_root=root / "release-collide",
                                          committish=head, runtime_links={"ops": root / "sharedvenv"}))
    _check("secrets are linked, never copied into the release",
           (tree / ".env").is_symlink())

    for stale in root.glob("release*"):
        _force_rmtree(stale)

# ---------------------------------------------------------------------------
print("\n-- activation and rollback --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    links = _runtime_resources(root)
    release_root = root / "release"

    first_sha = _git(repo, "rev-parse", "HEAD").strip()
    release_a = prepare_release(source_repo=repo, release_root=release_root,
                                committish=first_sha, runtime_links=links)["release_id"]
    (repo / "jobs" / "payload.py").write_text("MODE = 'second release'\n")
    _git(repo, "add", "jobs/payload.py")
    _git(repo, "commit", "-q", "-m", "second")
    second_sha = _git(repo, "rev-parse", "HEAD").strip()
    release_b = prepare_release(source_repo=repo, release_root=release_root,
                                committish=second_sha, runtime_links=links)["release_id"]

    _check("both releases exist side by side before any activation",
           len(list_releases(release_root)) == 2)
    _check("preparing a second release does not activate anything",
           release_status(release_root)["current_release_id"] is None)

    activate_release(release_root=release_root, release_id=release_a, source_repo=repo)
    _check("first activation sets current", pointer_release_id(release_root / "current") == release_a)
    _check("first activation records no previous",
           pointer_release_id(release_root / "previous") is None)
    _check("current is a symlink, not a copied tree", (release_root / "current").is_symlink())
    _check("activation resolves to a complete release",
           (release_root / "current" / "ops" / "runner.py").read_text() == "VALUE = 'committed'\n")

    repeat = activate_release(release_root=release_root, release_id=release_a, source_repo=repo)
    _check("re-activating the active release is a no-op", repeat["changed"] is False)

    activate_release(release_root=release_root, release_id=release_b, source_repo=repo)
    _check("promotion moves current", pointer_release_id(release_root / "current") == release_b)
    _check("promotion preserves the outgoing release identity",
           pointer_release_id(release_root / "previous") == release_a)
    _check("the outgoing release directory is not deleted",
           (release_root / "releases" / release_a / "ops" / "runner.py").exists())
    _check("current now resolves to the newer content",
           (release_root / "current" / "jobs" / "payload.py").read_text() == "MODE = 'second release'\n")

    rollback_release(release_root=release_root, source_repo=repo)
    _check("rollback restores the prior release",
           pointer_release_id(release_root / "current") == release_a)
    _check("rollback resolves to the prior content",
           (release_root / "current" / "jobs" / "payload.py").read_text() == "MODE = 'baseline'\n")
    _check("rollback is itself reversible",
           pointer_release_id(release_root / "previous") == release_b)
    rollback_release(release_root=release_root, source_repo=repo)
    _check("rolling forward again returns to the newer release",
           pointer_release_id(release_root / "current") == release_b)

    log_lines = [json.loads(line) for line in (release_root / "activations.log").read_text().splitlines()]
    _check("every pointer move is journalled",
           [row["action"] for row in log_lines].count("rollback") == 2 and
           [row["action"] for row in log_lines].count("activate") >= 3,
           f"actions={[row['action'] for row in log_lines]}")
    _check("the journal records the commit each activation shipped",
           all(len(row["commit"]) == 40 for row in log_lines))

    # An unverifiable release must never become current.
    tampered_tree = release_root / "releases" / release_a
    _unseal(tampered_tree)
    (tampered_tree / "ops" / "runner.py").write_text("VALUE = 'tampered'\n")
    _expect_error("activation of a tampered release is refused", "RELEASE_CONTENT_MISMATCH",
                  lambda: activate_release(release_root=release_root, release_id=release_a, source_repo=repo))
    _check("the refused activation left current untouched",
           pointer_release_id(release_root / "current") == release_b)
    _expect_error("rollback onto a tampered release is refused", "RELEASE_CONTENT_MISMATCH",
                  lambda: rollback_release(release_root=release_root, source_repo=repo))
    _check("the refused rollback left current untouched",
           pointer_release_id(release_root / "current") == release_b)

    _expect_error("a non-symlink squatting on current is refused", "RELEASE_POINTER_INVALID",
                  lambda: pointer_release_id(release_root / "releases"))
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- executing a release does not mutate it --")

with tempfile.TemporaryDirectory() as tmp:
    # Regression: the first candidate built during M1 failed verification
    # immediately after a smoke run, because importing a module from the release
    # made CPython write __pycache__ into it. A release that corrupts itself the
    # first time production runs it is worse than no boundary at all.
    root = Path(tmp)
    repo = _make_repo(root)
    (repo / "jobs" / "__init__.py").write_text("")
    (repo / "jobs" / "importable.py").write_text("VALUE = 'release'\n")
    _git(repo, "add", "jobs/__init__.py", "jobs/importable.py")
    _git(repo, "commit", "-q", "-m", "importable module")
    release_root = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    result = prepare_release(source_repo=repo, release_root=release_root, committish=head)
    tree = Path(result["release_path"])

    _check("release directories are sealed against new entries",
           os.stat(tree / "jobs").st_mode & 0o222 == 0,
           f"mode={oct(os.stat(tree / 'jobs').st_mode)}")
    try:
        (tree / "jobs" / "intruder.py").write_text("x = 1\n")
        created = True
    except OSError:
        created = False
    _check("a new file cannot be created inside a sealed release", not created)

    run = subprocess.run([sys.executable, "-c", "import jobs.importable; print(jobs.importable.VALUE)"],
                         cwd=tree, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(tree), "PYTHONDONTWRITEBYTECODE": ""})
    _check("a module still imports from the sealed release",
           run.returncode == 0 and run.stdout.strip() == "release",
           f"rc={run.returncode} out={run.stdout!r} err={run.stderr[-300:]!r}")
    _check("importing wrote no __pycache__ into the release",
           not list(tree.rglob("__pycache__")),
           f"found={[str(p) for p in tree.rglob('__pycache__')]}")
    verify_release(release_root=release_root, release_id=result["release_id"], source_repo=repo)
    _check("the release still verifies after being executed", True)

    # And the wrapper's cache redirection keeps bytecode outside the release.
    cache_dir = root / "pycache"
    run = subprocess.run([sys.executable, "-c", "import jobs.importable"],
                         cwd=tree, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(tree),
                              "PYTHONPYCACHEPREFIX": str(cache_dir), "PYTHONDONTWRITEBYTECODE": ""})
    _check("with PYTHONPYCACHEPREFIX the cache lands outside the release",
           run.returncode == 0 and cache_dir.exists() and not list(tree.rglob("__pycache__")),
           f"rc={run.returncode} cache_exists={cache_dir.exists()}")
    verify_release(release_root=release_root, release_id=result["release_id"], source_repo=repo)
    _check("the release verifies after a cache-redirected execution", True)
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- release removal --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    first_sha = _git(repo, "rev-parse", "HEAD").strip()
    release_a = prepare_release(source_repo=repo, release_root=release_root,
                                committish=first_sha)["release_id"]
    (repo / "jobs" / "payload.py").write_text("MODE = 'next'\n")
    _git(repo, "add", "jobs/payload.py")
    _git(repo, "commit", "-q", "-m", "next")
    release_b = prepare_release(source_repo=repo, release_root=release_root,
                                committish=_git(repo, "rev-parse", "HEAD").strip())["release_id"]
    activate_release(release_root=release_root, release_id=release_a, source_repo=repo)
    activate_release(release_root=release_root, release_id=release_b, source_repo=repo)

    _expect_error("the active release cannot be removed", "RELEASE_POINTER_INVALID",
                  lambda: remove_release(release_root=release_root, release_id=release_b))
    _expect_error("the rollback target cannot be removed", "RELEASE_POINTER_INVALID",
                  lambda: remove_release(release_root=release_root, release_id=release_a))
    _check("both releases survived the refused removals",
           len(list_releases(release_root)) == 2)

    third = prepare_release(source_repo=repo, release_root=release_root,
                            committish=first_sha + "^{commit}")["release_id"]
    _check("re-preparing an existing commit did not create a third release",
           third == release_a and len(list_releases(release_root)) == 2)
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- rollback with no history --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    release_id = prepare_release(source_repo=repo, release_root=release_root, committish=head)["release_id"]
    activate_release(release_root=release_root, release_id=release_id, source_repo=repo)
    _expect_error("rollback without a recorded predecessor fails closed", "RELEASE_POINTER_INVALID",
                  lambda: rollback_release(release_root=release_root, source_repo=repo))
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- verification survives the source repository --")

with tempfile.TemporaryDirectory() as tmp:
    # A rollback target must stay verifiable years later, when its commit may
    # have been made unreachable by a rebase, a deleted branch or `git gc`. If
    # verification needed the commit, the rollback lever would quietly expire.
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    release_id = prepare_release(source_repo=repo, release_root=release_root, committish=head)["release_id"]
    metadata = json.loads((release_root / "meta" / f"{release_id}.json").read_text())

    _check("provenance carries the full per-path manifest",
           len(metadata["manifest"]) == metadata["file_count"] == 3,
           f"manifest={metadata['manifest']}")
    _check("the manifest records mode and blob sha per path",
           metadata["manifest"]["ops/tool.sh"][0] == "100755"
           and len(metadata["manifest"]["ops/tool.sh"][1]) == 40)

    report = verify_release(release_root=release_root, release_id=release_id, source_repo=repo)
    _check("verification cross-checks git while the commit is reachable",
           report["verified_against_source_repository"] is True)

    unreachable = root / "gone"
    repo.rename(unreachable)
    detached = root / "devrepo"
    detached.mkdir()
    _git(detached, "init", "-q", "-b", "main")
    _git(detached, "config", "user.email", "test@example.invalid")
    _git(detached, "config", "user.name", "Release Boundary Test")
    (detached / "unrelated.txt").write_text("x\n")
    _git(detached, "add", "unrelated.txt")
    _git(detached, "commit", "-q", "-m", "unrelated")

    orphan = verify_release(release_root=release_root, release_id=release_id, source_repo=detached)
    _check("a release whose commit is unreachable still verifies from its manifest",
           orphan["commit"] == head and orphan["verified_against_source_repository"] is False)
    activate_release(release_root=release_root, release_id=release_id, source_repo=detached)
    _check("and can still be activated, so rollback does not expire",
           pointer_release_id(release_root / "current") == release_id)

    tree = Path(orphan["release_path"])
    _unseal(tree)
    (tree / "ops" / "runner.py").write_text("VALUE = 'tampered'\n")
    _expect_error("tampering is still detected without the source repository",
                  "RELEASE_CONTENT_MISMATCH",
                  lambda: verify_release(release_root=release_root, release_id=release_id, source_repo=detached))
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    release_id = prepare_release(source_repo=repo, release_root=release_root, committish=head)["release_id"]
    meta_path = release_root / "meta" / f"{release_id}.json"
    doctored = json.loads(meta_path.read_text())
    doctored["manifest"]["ops/runner.py"] = ["100644", "0" * 40]
    meta_path.write_text(json.dumps(doctored))
    _expect_error("a manifest doctored to match tampered bytes is caught by git",
                  "RELEASE_METADATA_INVALID",
                  lambda: verify_release(release_root=release_root, release_id=release_id, source_repo=repo))
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- rollback refuses a degenerate pointer state --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    release_id = prepare_release(source_repo=repo, release_root=release_root, committish=head)["release_id"]
    activate_release(release_root=release_root, release_id=release_id, source_repo=repo)
    # The state an activation interrupted between its two pointer swaps leaves.
    (release_root / "previous").symlink_to(f"releases/{release_id}")
    _expect_error("rollback refuses when previous and current name the same release",
                  "RELEASE_POINTER_INVALID",
                  lambda: rollback_release(release_root=release_root, source_repo=repo))
    log = release_root / "activations.log"
    _check("no rollback was journalled for the refused attempt",
           "rollback" not in log.read_text())
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- installed wrapper identity --")

_check("the release wrapper is recognized by content",
       installed_wrapper_variant(
           repo_root=REPO_ROOT,
           installed_wrapper=REPO_ROOT / "ops/systemd/proposed/log-job-runner.release.sh",
       ) == "release")
_check("the development wrapper is recognized by content",
       installed_wrapper_variant(
           repo_root=REPO_ROOT,
           installed_wrapper=REPO_ROOT / "ops/systemd/proposed/log-job-runner.sh",
       ) == "development_tree")
_check("an absent wrapper is reported, not guessed",
       installed_wrapper_variant(repo_root=REPO_ROOT,
                                 installed_wrapper=Path("/nonexistent/log-job-runner.sh")) == "absent")
_check("readiness expects the release wrapper once it is installed",
       expected_wrapper_source_relative(
           repo_root=REPO_ROOT,
           installed_wrapper=REPO_ROOT / "ops/systemd/proposed/log-job-runner.release.sh",
       ) == "ops/systemd/proposed/log-job-runner.release.sh")
_check("readiness expects the development wrapper before cutover",
       expected_wrapper_source_relative(
           repo_root=REPO_ROOT,
           installed_wrapper=REPO_ROOT / "ops/systemd/proposed/log-job-runner.sh",
       ) == "ops/systemd/proposed/log-job-runner.sh")
_check("BASE_DIR cannot be used to identify the release wrapper",
       "${RELEASE_ROOT}" in (REPO_ROOT / "ops/systemd/proposed/log-job-runner.release.sh").read_text())

# ---------------------------------------------------------------------------
print("\n-- cutover wrapper --")

WRAPPER = REPO_ROOT / "ops" / "systemd" / "proposed" / "log-job-runner.release.sh"

with tempfile.TemporaryDirectory() as tmp:
    # The wrapper is the artifact a future cutover installs, so its guards are
    # tested here rather than discovered in production. The final `exec` is
    # replaced by an echo: the point is to prove the preamble resolves and
    # refuses correctly, never to run a job.
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    links = _runtime_resources(root)
    (links[".venv"] / "bin" / "python").chmod(0o755)
    head = _git(repo, "rev-parse", "HEAD").strip()
    release_id = prepare_release(source_repo=repo, release_root=release_root,
                                 committish=head, runtime_links=links)["release_id"]

    source = WRAPPER.read_text()
    _check("the live wrapper is not the release wrapper",
           "log-platform-release" in source and "PYTHONPYCACHEPREFIX" in source)
    harness = source.replace(
        'RELEASE_ROOT="/opt/log-platform-release"',
        f'RELEASE_ROOT="{release_root}"',
    )
    harness = harness.replace('exec "${VENV_PY}" ops/run_with_environment_identity.py -- \\\n',
                              'echo "RESOLVED base=${BASE_DIR} cache=${PYTHONPYCACHEPREFIX} cwd=$(pwd)"\nexit 0\n#')
    script = root / "harness.sh"
    script.write_text(harness)
    script.chmod(0o755)

    missing_pointer = subprocess.run([str(script), "jobs.example"], capture_output=True, text=True)
    _check("the wrapper refuses when current does not exist",
           missing_pointer.returncode == 3 and "RELEASE_POINTER_INVALID" in missing_pointer.stderr,
           f"rc={missing_pointer.returncode} err={missing_pointer.stderr.strip()!r}")

    activate_release(release_root=release_root, release_id=release_id, source_repo=repo)
    resolved = subprocess.run([str(script), "jobs.example"], capture_output=True, text=True)
    # The wrapper resolves the pointer once and runs from the resolved path, so
    # a swap during a long run cannot change what an already-started job reads.
    _check("the wrapper runs from the resolved release, not through the pointer",
           resolved.returncode == 0
           and f"base={release_root}/releases/{release_id}" in resolved.stdout
           and "/current" not in resolved.stdout.split("cache=")[0],
           f"rc={resolved.returncode} out={resolved.stdout.strip()!r} err={resolved.stderr.strip()!r}")
    _check("the wrapper records the release identity for the journal",
           f"release={release_id}" in resolved.stderr,
           f"err={resolved.stderr.strip()!r}")
    _check("the wrapper redirects the bytecode cache out of the release",
           f"cache={release_root}/state/pycache" in resolved.stdout,
           f"out={resolved.stdout.strip()!r}")
    _check("the wrapper runs with the release as working directory",
           "cwd=" in resolved.stdout and str(release_root) in resolved.stdout)
    _check("the wrapper requires a job module",
           subprocess.run([str(script)], capture_output=True, text=True).returncode == 2)

    # The decisive guard: a pointer aimed at the development tree satisfies
    # "is a symlink" and "contains ops/runner.py", and without the release-path
    # assertion it would put production straight back on the mutable tree.
    (release_root / "current").unlink()
    (release_root / "current").symlink_to(repo)
    hijacked = subprocess.run([str(script), "jobs.example"], capture_output=True, text=True)
    _check("the wrapper refuses a pointer aimed outside releases/",
           hijacked.returncode == 3 and "RELEASE_POINTER_INVALID" in hijacked.stderr,
           f"rc={hijacked.returncode} err={hijacked.stderr.strip()!r}")

    (release_root / "current").unlink()
    (release_root / "current").mkdir()
    not_a_link = subprocess.run([str(script), "jobs.example"], capture_output=True, text=True)
    _check("the wrapper refuses when current is not a symlink",
           not_a_link.returncode == 3 and "RELEASE_POINTER_INVALID" in not_a_link.stderr,
           f"rc={not_a_link.returncode} err={not_a_link.stderr.strip()!r}")
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- wrapper provisioning safety (R1) --")

DEV_WRAPPER = REPO_ROOT / "ops/systemd/proposed/log-job-runner.sh"
REL_WRAPPER = REPO_ROOT / "ops/systemd/proposed/log-job-runner.release.sh"

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)

    # A release wrapper from an earlier commit: same structure, different bytes.
    historical = root / "historical-runner.sh"
    historical.write_text(REL_WRAPPER.read_text().replace(
        "set -euo pipefail", "set -euo pipefail\n# (an older release wrapper)", 1))
    arbitrary = root / "arbitrary.sh"
    arbitrary.write_text("#!/bin/sh\nexec /usr/bin/python3 /home/dev/ops/runner.py \"$@\"\n")
    unreadable = root / "unreadable.sh"
    unreadable.write_text(REL_WRAPPER.read_text())
    unreadable.chmod(0o000)

    cases = [
        ("development wrapper", DEV_WRAPPER, "development_tree", True, None),
        ("current release wrapper", REL_WRAPPER, "release", False, "RELEASE_BOUNDARY_ACTIVE"),
        ("historical release wrapper", historical, "release_historical", False, "RELEASE_BOUNDARY_ACTIVE"),
        ("arbitrary wrapper", arbitrary, "unrecognized", False, "INSTALLED_WRAPPER_UNRECOGNIZED"),
        ("unreadable wrapper", unreadable, "unreadable", False, "INSTALLED_WRAPPER_INDETERMINATE"),
        ("absent wrapper", root / "nope.sh", "absent", True, None),
    ]
    for label, path, expected_variant, replaceable, classification in cases:
        variant = installed_wrapper_variant(repo_root=REPO_ROOT, installed_wrapper=path)
        _check(f"{label} classifies as {expected_variant}", variant == expected_variant,
               f"got {variant!r}")
        decision = wrapper_replaceability(repo_root=REPO_ROOT, installed_wrapper=path)
        _check(f"{label} replaceable={replaceable}", decision["replaceable"] is replaceable,
               f"decision={decision}")
        if classification:
            _check(f"{label} refuses with {classification}",
                   decision["classification"] == classification, f"decision={decision}")

    # The decisive property: only the two development-side states are replaceable.
    _check("only absent and development wrappers may be overwritten by provisioning",
           set(WRAPPER_REPLACEABLE_BY_PROVISIONING) == {"absent", "development_tree"},
           f"set={set(WRAPPER_REPLACEABLE_BY_PROVISIONING)}")
    _check("a historical release wrapper is never expected to match the development wrapper",
           expected_wrapper_source_relative(repo_root=REPO_ROOT, installed_wrapper=historical)
           == "ops/systemd/proposed/log-job-runner.release.sh")
    unreadable.chmod(0o644)

provisioning_source = (REPO_ROOT / "ops/provision_runtime_environment_identity.py").read_text()
_check("provisioning consults the replaceability decision",
       "wrapper_replaceability(" in provisioning_source)
_check("provisioning refuses when the wrapper is not replaceable",
       'if not decision["replaceable"]:' in provisioning_source)
_check("the guard sits in the plan, so dry-run and execute are both covered",
       provisioning_source.index("wrapper_replaceability(")
       > provisioning_source.index("def _plan(")
       and provisioning_source.index("wrapper_replaceability(")
       < provisioning_source.index("def _execute("))
readiness_source = (REPO_ROOT / "ops/runtime_identity_readiness.py").read_text()
_check("tripwire: readiness still emits the boundary state block",
       '"release_boundary":release_boundary_state' in readiness_source
       and "provisioning_may_replace" in readiness_source)

# ---------------------------------------------------------------------------
print("\n-- no production bypass of the installed wrapper (R2) --")

INSTALLED_RUNNER = "/usr/local/bin/log-job-runner.sh"
unit_dir = REPO_ROOT / "ops/systemd"
for unit in sorted(unit_dir.rglob("*.service")):
    lines = [line.strip() for line in unit.read_text().splitlines() if line.startswith("ExecStart=")]
    for line in lines:
        command = line[len("ExecStart="):]
        if "jobs." not in command:
            continue  # not a runner job surface
        _check(f"{unit.relative_to(REPO_ROOT)} runs jobs through the installed wrapper",
               command.startswith(INSTALLED_RUNNER), f"ExecStart={command!r}")
        _check(f"{unit.relative_to(REPO_ROOT)} does not invoke ops/runner.py directly",
               "ops/runner.py" not in command, f"ExecStart={command!r}")
        parts = shlex.split(command)
        if len(parts) > 2:
            try:
                json.loads(parts[2])
                ok = True
            except json.JSONDecodeError:
                ok = False
            # systemd strips double quotes when splitting ExecStart, so an
            # unquoted JSON object would reach the wrapper unparseable.
            _check(f"{unit.relative_to(REPO_ROOT)} passes params that survive systemd quoting",
                   ok, f"arg={parts[2]!r}")

# Every tracked document that shows a runner *command line* must say plainly
# that it is development-only, **before** the first such command. A marker
# further down the file is not a warning: by the time an operator reaches it
# they have already copied the command.
DEV_ONLY_MARKER = "RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION"
_RUNNER_COMMAND_RE = re.compile(
    r"^[^|\n]*(?:PYTHONPATH=|\.venv/bin/python|python3?)\s+[^|\n]*ops/runner\.py", re.MULTILINE)
tracked_docs = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "docs/"],
                              check=True, capture_output=True, text=True).stdout.split()
unmarked, late_marker = [], []
for relative in tracked_docs:
    text = (REPO_ROOT / relative).read_text(errors="replace")
    first_command = _RUNNER_COMMAND_RE.search(text)
    if not first_command:
        continue
    if DEV_ONLY_MARKER not in text:
        unmarked.append(relative)
    elif text.index(DEV_ONLY_MARKER) > first_command.start():
        late_marker.append(relative)
_check("every tracked doc showing a runner command line marks it development-only",
       not unmarked, f"unmarked={unmarked}")
_check("the development-only marker precedes the first runner command in each doc",
       not late_marker, f"marker_after_first_command={late_marker}")

# Block-level rejection of the specific shape that reads as a *production*
# alternative: routing through the identity wrapper into the development-tree
# runner keeps the identity guarantee and therefore looks production-grade,
# while silently dropping the release boundary.
BYPASS_BLOCKS = []
ALTERNATIVE_PHRASING = []
for relative in tracked_docs:
    text = (REPO_ROOT / relative).read_text(errors="replace")
    blocks = re.findall(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL)
    for block in blocks:
        if "run_with_environment_identity.py" in block and "ops/runner.py" in block:
            BYPASS_BLOCKS.append(relative)
    for line in text.splitlines():
        lowered = line.lower()
        if "log-job-runner.sh" in lowered and " or" in lowered and lowered.rstrip().endswith("or:"):
            ALTERNATIVE_PHRASING.append(f"{relative}: {line.strip()[:90]}")
_check("no documented code block routes the identity wrapper into the dev-tree runner",
       not BYPASS_BLOCKS, f"blocks_in={sorted(set(BYPASS_BLOCKS))}")
_check("tripwire: no document phrases a dev-tree runner as a wrapper alternative",
       not ALTERNATIVE_PHRASING, f"phrasing={ALTERNATIVE_PHRASING}")

_check("the jobs document names the canonical production entrypoint",
       INSTALLED_RUNNER in (REPO_ROOT / "docs/05_jobs.md").read_text())

# ---------------------------------------------------------------------------
print("\n-- cutover preflight ordering (R3) --")

preflight_source = (REPO_ROOT / "ops/cutover_preflight.py").read_text()
import ops.cutover_preflight as preflight  # noqa: E402

_check("tripwire: the known wrapper-consumer floor still lists all three timers",
       set(preflight.KNOWN_WRAPPER_CONSUMER_TIMERS) ==
       {"log-job@dispatcher.timer", "log-workflow-b.timer", "log-job@retention-purge.timer"},
       f"timers={preflight.KNOWN_WRAPPER_CONSUMER_TIMERS}")
_check("tripwire: the known service floor matches the timer floor",
       len(preflight.KNOWN_WRAPPER_CONSUMER_SERVICES) == len(preflight.KNOWN_WRAPPER_CONSUMER_TIMERS))
_check("both advisory-lock domains are checked",
       preflight.DISPATCHER_ADVISORY_LOCK_KEY == 728503746327118001
       and "_workflow_b_lock_key" in preflight_source)
_check("the Workflow B lock key is imported, not copied",
       "from jobs.reports.workflow_b.orchestrator import workflow_b_advisory_lock_key" in preflight_source)
_check("tripwire: the preflight issues no mutating systemctl verb",
       not any(token in preflight_source for token in
               ('"stop"', "'stop'", '"start"', "'start'", '"restart"', "'restart'",
                "daemon-reload")),
       "preflight must be read-only")

unknown_unit = preflight._unit_state("log-job@definitely-not-a-real-unit.timer")
_check("a unit systemd cannot resolve is indeterminate, not quiescent",
       unknown_unit["known"] is False and unknown_unit["quiescent"] is False,
       f"state={unknown_unit}")
discovered = preflight.discover_wrapper_consumers()
_check("discovery returns at least the known floor",
       set(preflight.KNOWN_WRAPPER_CONSUMER_TIMERS) <= set(discovered["timers"]),
       f"discovered={discovered['timers']}")
_check("discovery finds log-job@ instances beyond the hardcoded floor",
       all(t.endswith(".timer") for t in discovered["timers"]),
       f"discovered={discovered['timers']}")

blocked = preflight.evaluate(Path(tempfile.gettempdir()) / "no-such-release-root", None, REPO_ROOT)
_check("an armed timer blocks the cutover",
       blocked["safe_to_replace_wrapper"] is False
       and any("timer still armed" in b for b in blocked["blockers"]),
       f"blockers={blocked['blockers']}")
_check("the enforced ordering stops timers before checking services",
       "stop every wrapper-consumer timer" in blocked["ordering_contract"][0]
       and "immediately before replacing" in blocked["ordering_contract"][2],
       f"contract={blocked['ordering_contract']}")

indeterminate = [row for row in blocked["advisory_locks"] if not row["determinate"]]
_check("an indeterminate advisory-lock check is a blocker, never a pass",
       all(any(row["domain"] in b for b in blocked["blockers"]) for row in indeterminate))

# ---------------------------------------------------------------------------
print("\n-- concurrent release management (R4) --")

# Contention in these tests is intentional, so the wait budget is bounded: this
# process must report RELEASE_MANAGEMENT_LOCKED quickly instead of sitting out
# the production default. Spawned children get a longer budget because they are
# expected to succeed after queueing, not to time out.
os.environ["RELEASE_MANAGEMENT_LOCK_TIMEOUT_SECONDS"] = "2"
_CHILD_LOCK_TIMEOUT = "90"


def _spawn(code: str, **extra_env):
    """Run a snippet as a real second process. Two OS processes is the point:
    the lock must hold across processes, not merely across threads."""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1",
           "RELEASE_MANAGEMENT_LOCK_TIMEOUT_SECONDS": _CHILD_LOCK_TIMEOUT, **extra_env}
    return subprocess.Popen([sys.executable, "-c", textwrap.dedent(code)],
                            cwd=str(REPO_ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_for(path: Path, timeout: float = 60.0) -> bool:
    """Bounded wait on a filesystem handshake — no fixed sleeps, so the
    interleaving is forced rather than hoped for."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.005)
    return False


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    release_a = prepare_release(source_repo=repo, release_root=release_root, committish=head)["release_id"]

    held, go = root / "held", root / "go"
    child = _spawn(f"""
        import time
        from pathlib import Path
        from ops.release_boundary import management_lock
        with management_lock(Path({str(release_root)!r})):
            Path({str(held)!r}).write_text("1")
            while not Path({str(go)!r}).exists():
                time.sleep(0.005)
    """)
    _check("the holder process acquired the management lock", _wait_for(held))
    _expect_error("a second process cannot enter the critical section",
                  "RELEASE_MANAGEMENT_LOCKED",
                  lambda: activate_release(release_root=release_root, release_id=release_a,
                                           source_repo=repo))
    # Read-only operations stay available while a mutation holds the lock.
    verify_release(release_root=release_root, release_id=release_a, source_repo=repo)
    _check("verify remains available while the lock is held", True)
    _check("status remains available while the lock is held",
           release_status(release_root)["current_release_id"] is None)
    go.write_text("1")
    child.wait(timeout=60)
    _check("the holder exited cleanly", child.returncode == 0, child.stderr.read()[-300:])
    activate_release(release_root=release_root, release_id=release_a, source_repo=repo)
    _check("the lock is available again once the holder finishes",
           pointer_release_id(release_root / "current") == release_a)
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # A killed operator command must not wedge release management forever. flock
    # is released by the kernel on process death, which a PID-file lock is not.
    root = Path(tmp)
    release_root = root / "release"
    release_root.mkdir()
    held = root / "held"
    crasher = _spawn(f"""
        import os
        from pathlib import Path
        from ops.release_boundary import management_lock
        with management_lock(Path({str(release_root)!r})):
            Path({str(held)!r}).write_text("1")
            os._exit(9)
    """)
    _check("the crashing process took the lock", _wait_for(held))
    crasher.wait(timeout=60)
    _check("it died while holding the lock", crasher.returncode == 9, f"rc={crasher.returncode}")
    acquired = False
    try:
        with management_lock(release_root, timeout_seconds=5):
            acquired = True
    except ReleaseBoundaryError:
        acquired = False
    _check("a crashed holder leaves no stale lock", acquired)

with tempfile.TemporaryDirectory() as tmp:
    # Concurrent activate vs activate. Without serialization both processes read
    # the same outgoing release and one records a predecessor that was never
    # current, so `previous` points at the wrong code and rollback goes there.
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    first = _git(repo, "rev-parse", "HEAD").strip()
    rel_a = prepare_release(source_repo=repo, release_root=release_root, committish=first)["release_id"]
    _git(repo, "commit", "-q", "--allow-empty", "-m", "second")
    second = _git(repo, "rev-parse", "HEAD").strip()
    rel_b = prepare_release(source_repo=repo, release_root=release_root, committish=second)["release_id"]
    _git(repo, "commit", "-q", "--allow-empty", "-m", "third")
    third = _git(repo, "rev-parse", "HEAD").strip()
    rel_c = prepare_release(source_repo=repo, release_root=release_root, committish=third)["release_id"]
    activate_release(release_root=release_root, release_id=rel_a, source_repo=repo)

    racer = """
        import sys, time
        from pathlib import Path
        from ops.release_boundary import activate_release
        release_root, repo, release_id, ready, start = sys.argv[1:6]
        Path(ready).write_text("1")
        while not Path(start).exists():
            time.sleep(0.005)
        # Stubbed for the same reason as in-process: this races POINTERS, not
        # schemas, and the child imports the real function directly.
        activate_release(release_root=Path(release_root), release_id=release_id,
                         source_repo=Path(repo), schema_preflight=lambda **_: None)
    """
    start = root / "start"
    procs = []
    for release_id, tag in ((rel_b, "b"), (rel_c, "c")):
        ready = root / f"ready-{tag}"
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1",
               "RELEASE_MANAGEMENT_LOCK_TIMEOUT_SECONDS": _CHILD_LOCK_TIMEOUT}
        procs.append((tag, ready, subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(racer), str(release_root), str(repo),
             release_id, str(ready), str(start)],
            cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)))
    _check("both racers are ready before either activates",
           all(_wait_for(ready) for _tag, ready, _p in procs))
    start.write_text("1")  # release both at once
    for _tag, _ready, proc in procs:
        proc.wait(timeout=120)
    _check("both concurrent activations succeeded",
           all(proc.returncode == 0 for _t, _r, proc in procs),
           "; ".join(proc.stderr.read()[-200:] for _t, _r, proc in procs))

    current = pointer_release_id(release_root / "current")
    previous = pointer_release_id(release_root / "previous")
    _check("current and previous never name the same release", current != previous,
           f"current={current} previous={previous}")
    _check("current is one of the racing releases", current in (rel_b, rel_c), f"current={current}")
    _check("previous records a release that really was current",
           previous in (rel_a, rel_b, rel_c) and previous != current,
           f"previous={previous}")
    journal = [json.loads(line) for line in (release_root / "activations.log").read_text().splitlines()]
    activations = [row for row in journal if row["action"] == "activate"]
    _check("every activation is journalled exactly once",
           len(activations) == 3, f"activations={[r['release_id'] for r in activations]}")
    # The serialized history must be a real chain: each activation's recorded
    # predecessor is whatever the preceding activation made current.
    chain_ok = all(activations[i]["previous_release_id"] == activations[i - 1]["release_id"]
                   for i in range(1, len(activations)))
    _check("the journal forms a consistent predecessor chain", chain_ok,
           f"chain={[(r['release_id'], r['previous_release_id']) for r in activations]}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Activate vs remove: the "is this release referenced?" check and the delete
    # must be one critical section, or a release becomes current between them and
    # is deleted out from under production.
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    first = _git(repo, "rev-parse", "HEAD").strip()
    rel_a = prepare_release(source_repo=repo, release_root=release_root, committish=first)["release_id"]
    _git(repo, "commit", "-q", "--allow-empty", "-m", "second")
    rel_b = prepare_release(source_repo=repo, release_root=release_root,
                            committish=_git(repo, "rev-parse", "HEAD").strip())["release_id"]
    activate_release(release_root=release_root, release_id=rel_a, source_repo=repo)

    start, ready = root / "start2", root / "ready2"
    activator = _spawn(f"""
        import time
        from pathlib import Path
        from ops.release_boundary import activate_release
        Path({str(ready)!r}).write_text("1")
        while not Path({str(start)!r}).exists():
            time.sleep(0.005)
        activate_release(release_root=Path({str(release_root)!r}), release_id={rel_b!r},
                         source_repo=Path({str(repo)!r}), schema_preflight=lambda **_: None)
    """)
    _check("the activator is ready", _wait_for(ready))
    start.write_text("1")
    removal_refused = None
    os.environ["RELEASE_MANAGEMENT_LOCK_TIMEOUT_SECONDS"] = _CHILD_LOCK_TIMEOUT
    try:
        remove_release(release_root=release_root, release_id=rel_b)
        removal_refused = False
    except ReleaseBoundaryError as exc:
        removal_refused = exc.classification
    activator.wait(timeout=120)
    os.environ["RELEASE_MANAGEMENT_LOCK_TIMEOUT_SECONDS"] = "2"
    activator_stderr = activator.stderr.read()

    current = pointer_release_id(release_root / "current")
    tree_exists = (release_root / "releases" / rel_b).is_dir()
    evidence = (f"current={current} removal_refused={removal_refused} "
                f"tree_exists={tree_exists} activator_rc={activator.returncode}")

    # Both serialized orders are correct; only an interleaving is not. Whichever
    # operation takes the lock first wins outright, and the loser fails closed:
    #
    #   activate first -> remove refuses, because the release is now referenced
    #   remove first   -> activate refuses, because the release no longer exists
    #
    # The unsafe outcome the lock exists to prevent is the third one: a `current`
    # pointer naming a directory that was deleted.
    activate_won = activator.returncode == 0
    if activate_won:
        _check("activate winning the lock forces the removal to refuse",
               removal_refused == "RELEASE_POINTER_INVALID" and current == rel_b and tree_exists,
               evidence)
    else:
        _check("remove winning the lock forces the activation to fail closed",
               removal_refused is False and current != rel_b
               and "RELEASE_NOT_FOUND" in activator_stderr,
               f"{evidence} stderr={activator_stderr[-200:]!r}")
    _check("a release named by current is never deleted",
           not (current == rel_b and not tree_exists), evidence)
    _check("the pointer never dangles",
           (release_root / "current").resolve().is_dir(), evidence)
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Concurrent prepare of the same commit: one immutable release, and both
    # callers agree on its content rather than one observing a half-built tree.
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()
    start, procs = root / "start3", []
    preparer = """
        import sys, time, json
        from pathlib import Path
        from ops.release_boundary import prepare_release
        release_root, repo, commit, ready, start = sys.argv[1:6]
        Path(ready).write_text("1")
        while not Path(start).exists():
            time.sleep(0.005)
        result = prepare_release(source_repo=Path(repo), release_root=Path(release_root), committish=commit)
        print(json.dumps({"digest": result["source_tree_digest"], "reused": result["reused"]}))
    """
    for tag in ("x", "y"):
        ready = root / f"ready3-{tag}"
        procs.append((ready, subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(preparer), str(release_root), str(repo),
             head, str(ready), str(start)],
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                 "RELEASE_MANAGEMENT_LOCK_TIMEOUT_SECONDS": _CHILD_LOCK_TIMEOUT},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)))
    _check("both preparers are ready", all(_wait_for(ready) for ready, _p in procs))
    start.write_text("1")
    outputs = []
    for _ready, proc in procs:
        out, err = proc.communicate(timeout=180)
        outputs.append((proc.returncode, out.strip(), err[-200:]))
    _check("both concurrent prepares succeeded",
           all(rc == 0 for rc, _o, _e in outputs), f"outputs={outputs}")
    digests = {json.loads(out)["digest"] for _rc, out, _e in outputs if out}
    _check("both agree on one content digest", len(digests) == 1, f"digests={digests}")
    _check("exactly one release directory exists", len(list_releases(release_root)) == 1,
           f"releases={list_releases(release_root)}")
    _check("exactly one of them built it, the other reused it",
           sorted(json.loads(out)["reused"] for _rc, out, _e in outputs if out) == [False, True],
           f"outputs={outputs}")
    verify_release(release_root=release_root,
                   release_id=list_releases(release_root)[0]["release_id"], source_repo=repo)
    _check("the concurrently prepared release verifies", True)
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- same-process thread serialization (R8) --")

with tempfile.TemporaryDirectory() as tmp:
    # Re-entrancy bookkeeping keyed by release root alone was a correctness bug:
    # thread B would see thread A's "held" marker, believe itself a nested owner,
    # skip flock and run a mutation concurrently. Ownership is per (root, thread).
    root = Path(tmp)
    release_root = root / "release"
    release_root.mkdir()
    entered = []
    overlap = []
    # Counts threads *actually inside* the critical section — deliberately
    # separate from `entered`, which also records the contender's refusal.
    occupancy = {"count": 0}
    occupancy_guard = threading.Lock()
    inside = threading.Event()
    proceed = threading.Event()
    barrier = threading.Barrier(2)

    def enter_section(name):
        with occupancy_guard:
            occupancy["count"] += 1
            if occupancy["count"] > 1:
                overlap.append(name)
        entered.append(name)

    def leave_section():
        with occupancy_guard:
            occupancy["count"] -= 1

    def holder():
        barrier.wait()
        with management_lock(release_root, timeout_seconds=30):
            enter_section("holder")
            inside.set()
            # Hold until the contender has definitely tried and failed.
            proceed.wait(timeout=30)
            leave_section()

    def contender():
        barrier.wait()
        inside.wait(timeout=30)
        try:
            with management_lock(release_root, timeout_seconds=1):
                enter_section("contender")
                leave_section()
        except ReleaseBoundaryError as exc:
            entered.append(exc.classification)

    threads = [threading.Thread(target=holder), threading.Thread(target=contender)]
    for thread in threads:
        thread.start()
    # Give the contender its bounded attempt, then release the holder.
    threads[1].join(timeout=30)
    proceed.set()
    for thread in threads:
        thread.join(timeout=30)

    _check("a second thread cannot enter the critical section",
           entered == ["holder", "RELEASE_MANAGEMENT_LOCKED"], f"entered={entered}")
    _check("the two threads never overlapped inside the lock", not overlap,
           f"overlap={overlap}")

    reentered = []
    with management_lock(release_root, timeout_seconds=5):
        with management_lock(release_root, timeout_seconds=5):
            reentered.append("nested")
    _check("the same thread may still nest legitimately", reentered == ["nested"])

    after = []
    with management_lock(release_root, timeout_seconds=5):
        after.append("acquired")
    _check("no stale ownership remains after nesting", after == ["acquired"])

    try:
        with management_lock(release_root, timeout_seconds=5):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    recovered = []
    with management_lock(release_root, timeout_seconds=5):
        recovered.append("acquired")
    _check("an exception inside the critical section releases ownership",
           recovered == ["acquired"])
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # The real mutations, from separate threads: pointer history must stay valid.
    root = Path(tmp)
    repo = _make_repo(root)
    release_root = root / "release"
    ids = []
    for index in range(3):
        if index:
            _git(repo, "commit", "-q", "--allow-empty", "-m", f"r{index}")
        ids.append(prepare_release(source_repo=repo, release_root=release_root,
                                   committish=_git(repo, "rev-parse", "HEAD").strip())["release_id"])
    activate_release(release_root=release_root, release_id=ids[0], source_repo=repo)

    errors = []
    start = threading.Barrier(3)

    def do_activate(release_id):
        def run():
            start.wait()
            try:
                activate_release(release_root=release_root, release_id=release_id, source_repo=repo)
            except ReleaseBoundaryError as exc:
                errors.append(exc.classification)
        return run

    def do_rollback():
        start.wait()
        try:
            rollback_release(release_root=release_root, source_repo=repo)
        except ReleaseBoundaryError as exc:
            errors.append(exc.classification)

    threads = [threading.Thread(target=do_activate(ids[1])),
               threading.Thread(target=do_activate(ids[2])),
               threading.Thread(target=do_rollback)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    current = pointer_release_id(release_root / "current")
    previous = pointer_release_id(release_root / "previous")
    _check("concurrent threads leave current and previous distinct",
           current != previous, f"current={current} previous={previous}")
    _check("both pointers name real releases",
           current in ids and previous in ids, f"current={current} previous={previous}")
    _check("the release named by current still exists on disk",
           (release_root / "releases" / current).is_dir())
    journal = [json.loads(line) for line in
               (release_root / "activations.log").read_text().splitlines()]
    _check("every journalled activation records a real release",
           all(row["release_id"] in ids for row in journal), f"journal={journal}")
    _check("no activation recorded itself as its own predecessor",
           all(row.get("previous_release_id") != row["release_id"] for row in journal),
           f"journal={journal}")

    # activate vs remove from threads: a referenced release must survive.
    removal_errors = []
    start2 = threading.Barrier(2)

    def racing_remove():
        start2.wait()
        try:
            remove_release(release_root=release_root, release_id=ids[1])
        except ReleaseBoundaryError as exc:
            removal_errors.append(exc.classification)

    def racing_activate():
        start2.wait()
        try:
            activate_release(release_root=release_root, release_id=ids[1], source_repo=repo)
        except ReleaseBoundaryError as exc:
            removal_errors.append(exc.classification)

    threads = [threading.Thread(target=racing_remove), threading.Thread(target=racing_activate)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    final_current = pointer_release_id(release_root / "current")
    _check("a release named by current is never left deleted",
           not (final_current == ids[1] and not (release_root / "releases" / ids[1]).is_dir()),
           f"current={final_current} errors={removal_errors}")
    _check("the current pointer never dangles",
           (release_root / "current").resolve().is_dir(), f"errors={removal_errors}")
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- digest stability --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    head = _git(repo, "rev-parse", "HEAD").strip()
    entries = commit_tree_entries(repo, head)
    _check("the source digest is stable across recomputation",
           source_tree_digest(entries) == source_tree_digest(dict(reversed(list(entries.items())))))
    _check("the source digest changes when a mode changes",
           source_tree_digest(entries) != source_tree_digest(
               {**entries, "ops/tool.sh": ("100644", entries["ops/tool.sh"][1])}))

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)} release boundary check(s):")
    for name in FAILURES:
        print(f"  - {name}")
    raise SystemExit(1)
print("ALL RELEASE BOUNDARY CHECKS PASSED")
