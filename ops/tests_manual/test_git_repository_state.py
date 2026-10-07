#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops import git_repository_state as git_state
from ops import promote_environment_identity as promote
from ops import provision_runtime_environment_identity as provision


def run(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check,
        capture_output=True, text=True,
    )


def make_repo() -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
    owner = tempfile.TemporaryDirectory()
    root = Path(owner.name) / "repo"
    root.mkdir()
    run(root, "init", "-b", "main")
    run(root, "config", "user.name", "Guard Test")
    run(root, "config", "user.email", "guard@example.invalid")
    (root / "tracked.txt").write_text("reviewed\n", encoding="utf-8")
    run(root, "add", "tracked.txt")
    run(root, "commit", "-m", "initial")
    return owner, root, run(root, "rev-parse", "HEAD").stdout.strip()


def rejected(root: Path) -> git_state.RepositoryStateError:
    try:
        git_state.require_clean_repository(root)
    except git_state.RepositoryStateError as exc:
        assert exc.classification == git_state.CLASSIFICATION
        assert len(exc.details.get("paths", [])) <= git_state.MAX_REPORTED_PATHS
        return exc
    raise AssertionError("dirty repository accepted")


def test_clean_and_root_resolution() -> None:
    owner, root, head = make_repo()
    try:
        state = git_state.require_clean_repository(root)
        assert state.head == head and state.branch == "main"
        subdir = root / "nested"
        subdir.mkdir()
        assert git_state.require_clean_repository(root, path=subdir).root == root.resolve()
        outside = Path(owner.name)
        try:
            git_state.require_clean_repository(root, path=outside)
        except git_state.RepositoryStateError as exc:
            assert exc.details["operation_state"] == ["outside_expected_repository"]
        else:
            raise AssertionError("outside invocation accepted")
        link = Path(owner.name) / "repo-link"
        link.symlink_to(root, target_is_directory=True)
        try:
            git_state.require_clean_repository(link)
        except git_state.RepositoryStateError as exc:
            assert exc.details["operation_state"] == ["unexpected_repository_path"]
        else:
            raise AssertionError("symlinked expected root accepted")
    finally:
        owner.cleanup()


def test_tracked_staged_untracked_deleted_and_bounded() -> None:
    for kind in ("modified", "staged", "untracked", "deleted"):
        owner, root, _ = make_repo()
        try:
            if kind == "modified":
                (root / "tracked.txt").write_text("SECRET_VALUE\n", encoding="utf-8")
            elif kind == "staged":
                (root / "tracked.txt").write_text("staged\n", encoding="utf-8")
                run(root, "add", "tracked.txt")
            elif kind == "untracked":
                (root / "untracked.txt").write_text("SECRET_VALUE\n", encoding="utf-8")
            else:
                (root / "tracked.txt").unlink()
            exc = rejected(root)
            rendered = json.dumps(exc.details, sort_keys=True)
            assert "SECRET_VALUE" not in rendered
            category = "unstaged" if kind in {"modified", "deleted"} else kind
            assert exc.details["counts"][category] >= 1
        finally:
            owner.cleanup()
    owner, root, _ = make_repo()
    try:
        for number in range(git_state.MAX_REPORTED_PATHS + 7):
            (root / f"untracked-{number:02d}").write_text("secret contents", encoding="utf-8")
        exc = rejected(root)
        assert exc.details["counts"]["untracked"] == git_state.MAX_REPORTED_PATHS + 7
        assert len(exc.details["paths"]) == git_state.MAX_REPORTED_PATHS
    finally:
        owner.cleanup()


def test_conflict_and_operation_markers() -> None:
    owner, root, _ = make_repo()
    try:
        run(root, "switch", "-c", "other")
        (root / "tracked.txt").write_text("other\n", encoding="utf-8")
        run(root, "commit", "-am", "other")
        run(root, "switch", "main")
        (root / "tracked.txt").write_text("main\n", encoding="utf-8")
        run(root, "commit", "-am", "main")
        result = run(root, "merge", "other", check=False)
        assert result.returncode != 0
        exc = rejected(root)
        assert exc.details["counts"]["conflicts"] == 1
        assert "merge" in exc.details["operation_state"]
    finally:
        owner.cleanup()
    for marker, state, directory in (
        ("rebase-merge", "rebase", True),
        ("CHERRY_PICK_HEAD", "cherry_pick", False),
        ("REVERT_HEAD", "revert", False),
    ):
        owner, root, head = make_repo()
        try:
            git_dir = Path(run(root, "rev-parse", "--git-dir").stdout.strip())
            if not git_dir.is_absolute():
                git_dir = root / git_dir
            path = git_dir / marker
            if directory:
                path.mkdir()
            else:
                path.write_text(head + "\n", encoding="ascii")
            assert state in rejected(root).details["operation_state"]
        finally:
            owner.cleanup()


def test_provisioning_blocks_before_inspection_or_checkpoint() -> None:
    owner, root, head = make_repo()
    try:
        (root / "dirty.txt").write_text("do not inspect", encoding="utf-8")
        argv = [
            "--expected-host", "host", "--expected-repository-head", head,
            "--expected-current-environment", "local_dev",
            "--backup-reference", str(root / "missing-checkpoint.json"),
        ]
        for execute in (False, True):
            called = []
            args = argv + (["--execute", "--attestation", "never"] if execute else [])
            with patch.object(provision, "REPO_ROOT", root), \
                 patch.object(provision, "_validate_backup_reference", side_effect=lambda *_: called.append("checkpoint")), \
                 patch.object(provision, "inspect_root_sources", side_effect=lambda *_: called.append("inspector")):
                output = io.StringIO()
                try:
                    with contextlib.redirect_stdout(output):
                        provision.main(args)
                except provision.ProvisioningError as exc:
                    assert exc.code == git_state.CLASSIFICATION
                else:
                    raise AssertionError("dirty provisioning accepted")
                assert called == [] and output.getvalue() == ""
    finally:
        owner.cleanup()


def test_promotion_modes_block_before_scope_database_or_journal() -> None:
    owner, root, _ = make_repo()
    try:
        (root / "dirty.txt").write_text("dirty", encoding="utf-8")
        args = SimpleNamespace()
        for function in (promote._dry_run, promote._execute, promote._readiness, promote._rollback):
            with patch.object(promote, "REPO_ROOT", root), \
                 patch.object(promote, "_validate_scope", side_effect=AssertionError("scope called")), \
                 patch.object(promote, "_require_arguments", side_effect=AssertionError("arguments called")):
                try:
                    function(args)
                except promote.promotion.PromotionError as exc:
                    assert exc.code == git_state.CLASSIFICATION
                else:
                    raise AssertionError(f"dirty mode accepted: {function.__name__}")
    finally:
        owner.cleanup()


def test_rollback_incomplete_arguments_and_preparation_errors_are_structured() -> None:
    clean_state = SimpleNamespace()
    with patch.object(promote, "_repository_state", return_value=clean_state):
        try:
            promote._rollback(SimpleNamespace())
        except promote.promotion.PromotionError as exc:
            assert exc.code == "REQUIRED_ARGUMENT_MISSING"
            assert exc.details["writes_performed"] is False
            assert exc.details["reconciliation_required"] is False
        else:
            raise AssertionError("incomplete rollback namespace was accepted")

    secret = "synthetic-preparation-secret"
    args = SimpleNamespace(
        promotion_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
        runtime_environment_file=Path("/tmp/synthetic-runtime"),
        backup_reference=Path("/tmp/synthetic-checkpoint"),
        recovery_root=Path("/tmp/synthetic-recovery"),
        preserved_recovery_backup=Path("/tmp/synthetic-backup"),
        recovery_evidence=Path("/tmp/synthetic-evidence"),
    )
    with patch.object(promote, "_repository_state", return_value=clean_state), \
            patch.object(
                promote, "_runtime",
                side_effect=ValueError(f"runtime preparation password={secret}"),
            ):
        try:
            promote._rollback(args)
        except promote.promotion.PromotionError as exc:
            assert exc.code == "RECOVERY_V2_EXECUTION_INTERRUPTED"
            assert exc.details["original_exception_class"] == "ValueError"
            assert exc.details["writes_performed"] is False
            assert exc.details["reconciliation_required"] is False
            assert secret not in json.dumps(exc.details)
        else:
            raise AssertionError("raw rollback preparation error was accepted")


def main() -> None:
    test_clean_and_root_resolution()
    test_tracked_staged_untracked_deleted_and_bounded()
    test_conflict_and_operation_markers()
    test_provisioning_blocks_before_inspection_or_checkpoint()
    test_promotion_modes_block_before_scope_database_or_journal()
    test_rollback_incomplete_arguments_and_preparation_errors_are_structured()
    print("git repository state tests: OK")


if __name__ == "__main__":
    main()
