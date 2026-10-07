#!/usr/bin/env python3
"""Manual regressions for the local/dev Workflow B single-file replay command.

Run:
    PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/test_replay_workflow_b_file_to_stage3.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.checks import replay_workflow_b_file_to_stage3 as replay  # noqa: E402
from jobs.reports.stage3 import job_stage3  # noqa: E402


class PatchAttrs:
    def __init__(self, target, **attrs):
        self.target = target
        self.attrs = attrs
        self.originals = {}

    def __enter__(self):
        for name, value in self.attrs.items():
            self.originals[name] = getattr(self.target, name)
            setattr(self.target, name, value)

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.originals.items():
            setattr(self.target, name, value)


class FakePlatformConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return {"database": "prod_db", "user_name": "prod", "server_addr": "10.0.0.5", "server_port": 5432}


class FakeIdentityConn:
    def cursor(self):
        return FakeCursor()


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    openpyxl = __import__("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Random"
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _d1052_rows() -> list[list[object]]:
    return [
        [
            "Nr Rejestracyjny",
            "Data rozpoczęcia",
            "Data ukończenia",
            " Czas rozpoczęcia",
            "Czas zakończenia ",
            " przekroczenia obr/min",
            " >140kmh",
            ">160kmh",
            " >170kmh",
        ],
        ["WD12345", "2026-05-12", "2026-05-12", "08:30", "09:05", 2, 3, 4, 5],
    ]


def test_stage2_detection_without_filename_or_title_dependency() -> None:
    with tempfile.TemporaryDirectory(prefix="replay-stage2-") as tmp:
        path = Path(tmp) / "renamed_without_title.xlsx"
        path.write_bytes(_xlsx_bytes(_d1052_rows()))
        result = replay.normalize_detect_clean(path, expected_report_type="report_d105_2_ecodriving")
    assert result["detected_report_type"] == "report_d105_2_ecodriving", result
    assert result["cleaned_rows"] == 1, result
    assert result["clean_metadata"]["split_timestamp_columns"] is True, result
    print("PASS: replay command Stage 2 detection is structural, independent of filename/title")


def test_expected_report_type_mismatch_fails_safely() -> None:
    with tempfile.TemporaryDirectory(prefix="replay-mismatch-") as tmp:
        path = Path(tmp) / "renamed_without_title.xlsx"
        path.write_bytes(_xlsx_bytes(_d1052_rows()))
        try:
            replay.normalize_detect_clean(path, expected_report_type="report_207")
        except RuntimeError as exc:
            assert "Expected report_type" in str(exc), str(exc)
        else:
            raise AssertionError("expected report type mismatch to fail")
    print("PASS: replay command fails safely on expected report type mismatch")


def test_dry_run_mode_does_not_call_stage3_load() -> None:
    called = {"load": 0}

    def fail_load(*_args, **_kwargs):
        called["load"] += 1
        raise AssertionError("dry-run must not call Stage 3 load")

    fake_config = job_stage3.ClientDbConfig(
        client_code="ALPHA00001",
        client_id="client-id",
        client_db_host="127.0.0.1",
        client_db_port=5432,
        client_db_name="alpha_main",
        client_db_user="alpha_user",
        client_db_password_secret_ref="ALPHA_DB_PASSWORD",
        client_db_sslmode="prefer",
    )
    fake_stage2 = {
        "source_path": "/tmp/file.xlsx",
        "source_sha256": "a" * 64,
        "detected_report_type": "report_d105_2_ecodriving",
        "cleaned_rows": 1,
        "dataframe": object(),
    }
    fake_plan = {"errors": [], "would_insert_rows": 1}
    with PatchAttrs(
        replay,
        _load_dotenv_if_present=lambda: None,
        normalize_detect_clean=lambda *_args, **_kwargs: dict(fake_stage2),
        _load_client_config=lambda *_args, **_kwargs: fake_config,
        _build_stage3_plan=lambda *_args, **_kwargs: fake_plan,
        _run_stage3_load=fail_load,
    ), PatchAttrs(replay.job_stage3, _platform_pg_conn=lambda: FakePlatformConn()):
        result = replay.replay_file(
            client_code="ALPHA00001",
            file_path="/tmp/file.xlsx",
            expected_report_type="report_d105_2_ecodriving",
            load_stage3=False,
        )
    assert called["load"] == 0
    assert result["stage3_load"] is None
    assert result["stage3_plan"] == fake_plan
    print("PASS: replay command dry-run mode does not call Stage 3 load")


def _local_runtime():
    return replay.environment_identity.RuntimeIdentity(
        environment="local_dev",
        platform_identity_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
        postgres_host="127.0.0.1",
        postgres_port=5432,
        postgres_db="logdb",
        postgres_user="loguser",
    )


def _attestation(role: str, database: str, user: str, client_code=None):
    return replay.environment_identity.AttestedDatabaseIdentity(
        environment="local_dev",
        database_identity_id=(
            "bd7662a5-eeb4-4614-8720-d477abfcb227"
            if role == "platform"
            else "b454f82c-5857-4bab-8342-b7258e5cf7de"
        ),
        database_role=role,
        database_name=database,
        database_user=user,
        client_code=client_code,
    )


def test_load_stage3_accepts_verified_local_dev_identity() -> None:
    old_env = {name: os.environ.get(name) for name in [replay.LOCAL_REPLAY_ALLOW_ENV, "POSTGRES_HOST", "LOG_API_URL"]}
    os.environ[replay.LOCAL_REPLAY_ALLOW_ENV] = "1"
    os.environ["POSTGRES_HOST"] = "127.0.0.1"
    os.environ["LOG_API_URL"] = "http://127.0.0.1:8000"
    called = {"load": 0}
    config = job_stage3.ClientDbConfig(
        client_code="ALPHA00001",
        client_id="client-id",
        client_db_host="127.0.0.1",
        client_db_port=5432,
        client_db_name="alpha_main",
        client_db_user="alpha_user",
        client_db_password_secret_ref="ALPHA_DB_PASSWORD",
        client_db_sslmode="prefer",
        client_db_environment="local_dev",
        client_db_identity_id="b454f82c-5857-4bab-8342-b7258e5cf7de",
    )
    stage2 = {
        "source_path": "/tmp/file.xlsx",
        "source_sha256": "a" * 64,
        "detected_report_type": "report_d105_2_ecodriving",
        "cleaned_rows": 1,
        "dataframe": object(),
    }
    try:
        with PatchAttrs(
            replay,
            _load_dotenv_if_present=lambda: None,
            normalize_detect_clean=lambda *_args, **_kwargs: dict(stage2),
            _load_client_config=lambda *_args, **_kwargs: config,
            _build_stage3_plan=lambda *_args, **_kwargs: {"errors": []},
            _run_stage3_load=lambda *_args, **_kwargs: called.__setitem__("load", called["load"] + 1) or {"status": "OK"},
        ), PatchAttrs(
            replay.job_stage3,
            _platform_pg_conn=lambda: FakePlatformConn(),
            _client_business_pg_conn=lambda *_args, **_kwargs: FakePlatformConn(),
        ), PatchAttrs(
            replay.environment_identity,
            load_runtime_identity=lambda: _local_runtime(),
            attest_platform_identity=lambda *_args, **_kwargs: _attestation("platform", "logdb", "loguser"),
            attest_client_identity=lambda *_args, **_kwargs: _attestation("client_business", "alpha_main", "alpha_user", "ALPHA00001"),
        ):
            result = replay.replay_file(
                client_code="ALPHA00001",
                file_path="/tmp/file.xlsx",
                expected_report_type="report_d105_2_ecodriving",
                load_stage3=True,
            )
    finally:
        for name, value in old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    assert called["load"] == 1
    assert result["environment_identity_guard"]["ok"] is True


def test_load_stage3_refuses_production_target() -> None:
    production = replay.environment_identity.RuntimeIdentity(
        environment="production",
        platform_identity_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
        postgres_host="127.0.0.1",
        postgres_port=5432,
        postgres_db="logdb",
        postgres_user="loguser",
    )
    with PatchAttrs(replay, _load_dotenv_if_present=lambda: None), PatchAttrs(
        replay.job_stage3, _platform_pg_conn=lambda: FakePlatformConn()
    ), PatchAttrs(replay.environment_identity, load_runtime_identity=lambda: production):
        try:
            replay.replay_file(
                client_code="ALPHA00001",
                file_path="/tmp/file.xlsx",
                expected_report_type="report_d105_2_ecodriving",
                load_stage3=True,
            )
        except replay.environment_identity.EnvironmentIdentityError as exc:
            assert "local/dev-only" in str(exc)
        else:
            raise AssertionError("production replay load must be refused")


def main() -> None:
    test_stage2_detection_without_filename_or_title_dependency()
    test_expected_report_type_mismatch_fails_safely()
    test_dry_run_mode_does_not_call_stage3_load()
    test_load_stage3_accepts_verified_local_dev_identity()
    test_load_stage3_refuses_production_target()
    print("OK - Workflow B single-file replay command regressions passed")

if __name__ == "__main__":
    main()
