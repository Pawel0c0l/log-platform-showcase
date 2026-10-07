#!/usr/bin/env python3
"""Focused synthetic-bare-origin tests for the resume-v2 remote binding."""
from __future__ import annotations

import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from ops import environment_identity_promotion as promotion
from ops import resume_plan_v2 as resume
from ops.tests_manual.test_resume_plan_v2 import fixture_plan


def run(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def expect_remote_drift(fn) -> None:
    try:
        fn()
    except promotion.PromotionError as exc:
        assert exc.code == "RESUME_REMOTE_HEAD_DRIFT", exc
        assert exc.details["writes_performed"] is False
    else:
        raise AssertionError("expected RESUME_REMOTE_HEAD_DRIFT")


def repositories(parent: Path) -> tuple[Path, Path]:
    bare = parent / "origin.git"
    work = parent / "work"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    run(work, "config", "user.name", "Resume Test")
    run(work, "config", "user.email", "resume@example.invalid")
    (work / "tracked.txt").write_text("one\n", encoding="utf-8")
    run(work, "add", "tracked.txt")
    run(work, "commit", "-m", "initial")
    run(work, "remote", "add", "origin", str(bare))
    run(work, "push", "-u", "origin", "main")
    return bare, work


def test_canonical_actual_remote_binding_and_fail_closed_cases() -> None:
    with tempfile.TemporaryDirectory() as directory:
        bare, work = repositories(Path(directory))
        head = run(work, "rev-parse", "HEAD")
        binding = resume.remote_repository_binding(
            work, local_head=head, local_branch="main",
        )
        assert binding["remote_url"] == bare.resolve().as_uri()
        assert binding["remote_identity"] == binding["remote_url"]
        assert binding["remote_ref"] == "refs/heads/main"
        assert binding["local_head"] == binding["remote_head"] == head
        assert binding["local_remote_head_equal"] is True

        # Actual remote movement is observed without fetching or changing
        # refs/remotes/origin/main in the approved worktree.
        other = Path(directory) / "other"
        subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
        run(other, "config", "user.name", "Resume Test")
        run(other, "config", "user.email", "resume@example.invalid")
        run(other, "checkout", "main")
        (other / "tracked.txt").write_text("two\n", encoding="utf-8")
        run(other, "commit", "-am", "remote movement")
        run(other, "push", "origin", "main")
        tracking_before = run(work, "rev-parse", "refs/remotes/origin/main")
        expect_remote_drift(lambda: resume.remote_repository_binding(
            work, local_head=head, local_branch="main",
        ))
        assert run(work, "rev-parse", "refs/remotes/origin/main") == tracking_before

        run(work, "fetch", "origin", "main")
        run(work, "reset", "--hard", "origin/main")
        new_head = run(work, "rev-parse", "HEAD")
        run(work, "remote", "set-url", "--add", "origin", bare.resolve().as_uri())
        expect_remote_drift(lambda: resume.remote_repository_binding(
            work, local_head=new_head, local_branch="main",
        ))
        run(work, "config", "--unset-all", "remote.origin.url")
        run(work, "config", "remote.origin.url", str(bare))

        run(work, "config", "branch.main.merge", "refs/heads/not-main")
        expect_remote_drift(lambda: resume.remote_repository_binding(
            work, local_head=new_head, local_branch="main",
        ))
        run(work, "config", "branch.main.merge", "refs/heads/main")
        expect_remote_drift(lambda: resume.remote_repository_binding(
            work, local_head=new_head, local_branch="feature",
        ))

        run(work, "push", "origin", "--delete", "main")
        expect_remote_drift(lambda: resume.remote_repository_binding(
            work, local_head=new_head, local_branch="main",
        ))
        run(work, "push", "origin", "main")

        run(work, "remote", "set-url", "origin", str(Path(directory) / "missing.git"))
        expect_remote_drift(lambda: resume.remote_repository_binding(
            work, local_head=new_head, local_branch="main",
        ))
        run(work, "remote", "set-url", "origin", str(bare))

        real_run = subprocess.run

        def malformed_ls_remote(command, **kwargs):
            if command[-4:] == ["ls-remote", "--refs", "origin", "refs/heads/main"]:
                return subprocess.CompletedProcess(
                    command, 0, "NOT-A-SHA\trefs/heads/main\n", "",
                )
            return real_run(command, **kwargs)

        with patch.object(subprocess, "run", side_effect=malformed_ls_remote):
            expect_remote_drift(lambda: resume.remote_repository_binding(
                work, local_head=new_head, local_branch="main",
            ))


def test_remote_fields_participate_in_hash_and_schema_is_required() -> None:
    base = {
        "remote_repository_binding": {
            "remote_url": "ssh://git@example.invalid/log-platform.git",
            "remote_identity": "ssh://git@example.invalid/log-platform.git",
            "remote_head": "4" * 40,
            "remote_ref": "refs/heads/main",
        },
        "migration_054_schema_contract": {"contract": "complete"},
    }
    baseline = promotion.plan_hash(base)
    for key, value in (
        ("remote_url", "ssh://git@example.invalid/other.git"),
        ("remote_identity", "ssh://git@example.invalid/other.git"),
        ("remote_head", "9" * 40),
        ("remote_ref", "refs/heads/other"),
    ):
        changed = deepcopy(base)
        changed["remote_repository_binding"][key] = value
        assert promotion.plan_hash(changed) != baseline
        assert resume.drift_classification(base, changed) == "RESUME_REMOTE_HEAD_DRIFT"

    old = {
        "contract": resume.CONTRACT,
        "contract_version": 2,
        "attestation_contract": resume.ATTESTATION_CONTRACT,
    }
    expect_code = None
    try:
        resume.validate_plan(old)
    except promotion.PromotionError as exc:
        expect_code = exc.code
    assert expect_code == "RESUME_IMMUTABLE_BINDING_DRIFT"


def test_real_remote_collector_builds_valid_hashed_plan() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _bare, work = repositories(Path(directory))
        head = run(work, "rev-parse", "HEAD")
        binding = resume.remote_repository_binding(
            work, local_head=head, local_branch="main",
        )
        fixture = fixture_plan()
        approval = {
            **fixture["approval_identity"],
            "resume_implementation_head": head,
        }
        plan = resume.build_plan(
            approval_identity=approval,
            remote_repository_binding=binding,
            migration_054_schema_contract=fixture["migration_054_schema_contract"],
            journal_state=fixture["journal_state"],
            persistent_state=fixture["persistent_state"],
            runtime_convergence=fixture["runtime_convergence"],
            systemd_runtime=fixture["systemd_runtime"],
            docker_runtime=fixture["docker_runtime"],
            security_and_recovery=fixture["security_and_recovery"],
            promotion_id=str(approval["promotion_id"]),
        )
        resume.validate_plan(plan)
        canonical = promotion.canonical_json(plan)
        digest = promotion.plan_hash(plan)
        assert len(canonical) > 0 and len(digest) == 64

        def assert_complete(value) -> None:
            assert value is not None and value != ""
            if isinstance(value, dict):
                for item in value.values():
                    assert_complete(item)
            elif isinstance(value, list):
                for item in value:
                    assert_complete(item)

        assert_complete(plan)
        changed = deepcopy(plan)
        changed_head = "9" * 40
        changed["approval_identity"]["resume_implementation_head"] = changed_head
        changed["remote_repository_binding"]["local_head"] = changed_head
        changed["remote_repository_binding"]["remote_head"] = changed_head
        resume.validate_plan(changed)
        assert promotion.plan_hash(changed) != digest


def main() -> None:
    test_canonical_actual_remote_binding_and_fail_closed_cases()
    test_remote_fields_participate_in_hash_and_schema_is_required()
    test_real_remote_collector_builds_valid_hashed_plan()
    print("resume-v2 remote binding tests: OK")


if __name__ == "__main__":
    main()
