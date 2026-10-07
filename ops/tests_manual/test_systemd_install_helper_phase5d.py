#!/usr/bin/env python3
"""Manual checks for the Phase 5D systemd API service installer.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_systemd_install_helper_phase5d.py

These checks use only --dry-run and temporary files. They do not require root,
systemd, sudo, Postgres, or a running API.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "ops/systemd/install_log_platform_api_service.sh"
OPS_DOC = REPO_ROOT / "docs/07_operations.md"


SECRET_MARKERS = (
    "SUPER_SECRET_SHOULD_NOT_PRINT",
    "API_WRITE_TOKEN=real-token",
    "POSTGRES_PASSWORD=real-password",
    "MINIO_ROOT_PASSWORD=real-minio-password",
)


def _read(path: Path) -> str:
    assert path.exists(), f"missing file: {path.relative_to(REPO_ROOT)}"
    return path.read_text(encoding="utf-8")


def _run_helper(args: list[str]) -> str:
    result = subprocess.run(
        ["bash", str(HELPER), *args],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def _make_tmp_layout(tmp: Path) -> tuple[Path, Path, Path]:
    workdir = tmp / "repo"
    venv = tmp / "venv"
    env_file = tmp / "api.env"
    workdir.mkdir()
    (venv / "bin").mkdir(parents=True)
    env_file.write_text(
        "POSTGRES_PASSWORD=real-password\n"
        "API_WRITE_TOKEN=real-token\n"
        "MINIO_ROOT_PASSWORD=real-minio-password\n"
        "SUPER_SECRET_SHOULD_NOT_PRINT=1\n",
        encoding="utf-8",
    )
    return workdir, venv, env_file


def _assert_no_secret_values(output: str) -> None:
    for marker in SECRET_MARKERS:
        assert marker not in output, marker


def test_helper_exists_and_dry_run_defaults_to_localhost() -> None:
    assert HELPER.exists(), HELPER
    assert HELPER.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    with tempfile.TemporaryDirectory() as raw:
        workdir, venv, env_file = _make_tmp_layout(Path(raw))
        output = _run_helper([
            "--dry-run",
            "--user", "portaluser",
            "--group", "portalgroup",
            "--workdir", str(workdir),
            "--env-file", str(env_file),
            "--venv", str(venv),
        ])
    assert "===== DRY RUN: generated log-platform-api.service =====" in output
    assert "Description=Log Platform API and Portal" in output
    assert "ExecStart=" in output and "api.main:app" in output
    assert "--host 127.0.0.1" in output
    assert "--port 8000" in output
    assert "sudo systemctl daemon-reload" in output
    assert "sudo systemctl status log-platform-api --no-pager" in output
    assert "journalctl -u log-platform-api -n 100 --no-pager" in output
    _assert_no_secret_values(output)
    print("PASS: helper exists and dry-run defaults to localhost without printing env contents")


def test_custom_dry_run_values_are_reflected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        workdir, venv, env_file = _make_tmp_layout(Path(raw))
        output = _run_helper([
            "--dry-run",
            "--service-name", "custom-platform-api",
            "--user", "svcuser",
            "--group", "svcgroup",
            "--workdir", str(workdir),
            "--env-file", str(env_file),
            "--venv", str(venv),
            "--host", "127.0.0.2",
            "--port", "18000",
            "--enable",
            "--restart",
        ])
    assert "Target unit: /etc/systemd/system/custom-platform-api.service" in output
    assert "User=svcuser" in output
    assert "Group=svcgroup" in output
    assert f"WorkingDirectory={workdir}" in output
    assert f"EnvironmentFile={env_file}" in output
    assert f"ExecStart={venv}/bin/uvicorn api.main:app --host 127.0.0.2 --port 18000" in output
    assert "sudo systemctl enable custom-platform-api.service" in output
    assert "sudo systemctl restart custom-platform-api.service" in output
    _assert_no_secret_values(output)
    print("PASS: custom dry-run values are reflected in generated unit and commands")


def test_operations_docs_cover_unit_not_found_recovery_and_checks() -> None:
    text = _read(OPS_DOC)
    for phrase in [
        "The example file alone does not install a systemd unit",
        "Unit log-platform-api.service not found",
        "ops/systemd/install_log_platform_api_service.sh",
        "--dry-run",
        "sudo systemctl daemon-reload",
        "sudo systemctl enable",
        "sudo systemctl restart",
        "sudo systemctl status log-platform-api --no-pager",
        "journalctl -u log-platform-api -n 100 --no-pager",
        "./ops/db_migrate.sh",
        'PYTHONPATH="$PWD" python3 ops/checks/check_portal_ready.py',
    ]:
        assert phrase in text, phrase
    print("PASS: operations docs cover install recovery, systemctl flow, logs, migrations, and readiness")


def main() -> int:
    test_helper_exists_and_dry_run_defaults_to_localhost()
    test_custom_dry_run_values_are_reflected()
    test_operations_docs_cover_unit_not_found_recovery_and_checks()
    print("PASS: Phase 5D systemd install helper checks completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
