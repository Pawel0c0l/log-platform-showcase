"""Fail-closed Git repository state inspection for privileged operations."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


CLASSIFICATION = "REPOSITORY_WORKTREE_NOT_CLEAN"
MAX_REPORTED_PATHS = 20


class RepositoryStateError(RuntimeError):
    def __init__(self, details: dict[str, object]) -> None:
        self.classification = CLASSIFICATION
        self.details = details
        super().__init__(CLASSIFICATION)


@dataclass(frozen=True)
class RepositoryState:
    root: Path
    head: str
    branch: str
    counts: dict[str, int]
    paths: tuple[dict[str, str], ...]
    operation_state: tuple[str, ...]

    def public_dict(self) -> dict[str, object]:
        return {
            "repository_root": str(self.root),
            "head": self.head,
            "branch": self.branch,
            "counts": dict(self.counts),
            "paths": [dict(row) for row in self.paths],
            "operation_state": list(self.operation_state) or ["none"],
        }


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run_git(runner: Runner, probe: Path, *args: str) -> str:
    result = runner(
        ["git", "-C", str(probe), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RepositoryStateError({
            "repository_root": str(probe),
            "head": None,
            "branch": None,
            "counts": {},
            "paths": [],
            "operation_state": ["git_command_failed"],
        })
    return result.stdout


def _operation_states(runner: Runner, root: Path) -> tuple[str, ...]:
    markers = {
        "merge": ("MERGE_HEAD", "file"),
        "rebase": ("rebase-merge", "directory"),
        "rebase_apply": ("rebase-apply", "directory"),
        "cherry_pick": ("CHERRY_PICK_HEAD", "file"),
        "revert": ("REVERT_HEAD", "file"),
    }
    active: list[str] = []
    for name, (marker, kind) in markers.items():
        relative = _run_git(runner, root, "rev-parse", "--git-path", marker).strip()
        path = Path(relative)
        if not path.is_absolute():
            path = root / path
        present = path.is_dir() if kind == "directory" else path.is_file()
        if present:
            active.append("rebase" if name == "rebase_apply" else name)
    return tuple(sorted(set(active)))


def _parse_status(raw: str) -> tuple[str, str, dict[str, int], tuple[dict[str, str], ...]]:
    head = ""
    branch = ""
    counts = {
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicts": 0,
        "deleted": 0,
        "submodules": 0,
    }
    found: list[tuple[str, str]] = []
    records = raw.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith("# branch.oid "):
            head = record.removeprefix("# branch.oid ")
            continue
        if record.startswith("# branch.head "):
            branch = record.removeprefix("# branch.head ")
            continue
        if record.startswith("? "):
            path = record[2:]
            counts["untracked"] += 1
            found.append(("untracked", path))
            continue
        if record.startswith("u "):
            path = record.split(" ", 10)[-1]
            counts["conflicts"] += 1
            found.append(("conflict", path))
            continue
        if record.startswith(("1 ", "2 ")):
            fields = record.split(" ", 8 if record.startswith("1 ") else 9)
            xy = fields[1]
            submodule = fields[2]
            path = fields[-1]
            if record.startswith("2 ") and index < len(records):
                index += 1  # Original rename/copy path is the following NUL record.
            if xy[0] != ".":
                counts["staged"] += 1
                found.append(("staged", path))
            if xy[1] != ".":
                counts["unstaged"] += 1
                found.append(("unstaged", path))
            if "D" in xy:
                counts["deleted"] += 1
            if submodule != "N...":
                counts["submodules"] += 1
                found.append(("submodule", path))
            continue
    bounded = tuple(
        {"category": category, "path": path}
        for category, path in sorted(set(found))[:MAX_REPORTED_PATHS]
    )
    return head, branch, counts, bounded


def require_clean_repository(
    expected_root: Path,
    *,
    path: Path | None = None,
    runner: Runner = subprocess.run,
) -> RepositoryState:
    """Return bounded repository identity or fail if any mutable Git state exists.

    Ignored files are intentionally excluded. Untracked files and dirty recursive
    submodules are included by porcelain v2 with ``--ignore-submodules=none``.
    """
    expected_input = Path(expected_root).absolute()
    probe_input = Path(path or expected_root).absolute()
    if expected_input.is_symlink():
        raise RepositoryStateError({
            "repository_root": str(expected_input), "head": None, "branch": None,
            "counts": {}, "paths": [], "operation_state": ["unexpected_repository_path"],
        })
    expected = expected_input.resolve()
    probe = probe_input.resolve()
    try:
        probe.relative_to(expected)
    except ValueError as exc:
        raise RepositoryStateError({
            "repository_root": str(probe), "head": None, "branch": None,
            "counts": {}, "paths": [], "operation_state": ["outside_expected_repository"],
        }) from exc
    actual = Path(_run_git(runner, probe, "rev-parse", "--show-toplevel").strip()).resolve()
    if actual != expected:
        raise RepositoryStateError({
            "repository_root": str(actual), "head": None, "branch": None,
            "counts": {}, "paths": [], "operation_state": ["unexpected_repository_root"],
        })
    raw = _run_git(
        runner,
        actual,
        "status",
        "--porcelain=v2",
        "-z",
        "--branch",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    head, branch, counts, paths = _parse_status(raw)
    operations = _operation_states(runner, actual)
    if not head or head == "(initial)" or not branch:
        operations = tuple(sorted(set(operations + ("repository_identity_unresolved",))))
    state = RepositoryState(actual, head, branch, counts, paths, operations)
    if any(counts.values()) or operations:
        raise RepositoryStateError(state.public_dict())
    return state
