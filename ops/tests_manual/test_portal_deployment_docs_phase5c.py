#!/usr/bin/env python3
"""Manual deployment-documentation checks for Phase 5C.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_deployment_docs_phase5c.py

These checks inspect committed examples/docs only. They do not require root,
systemd, nginx, Docker, Postgres, or a running API.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_EXAMPLE = REPO_ROOT / "ops/systemd/log-platform-api.service.example"
NGINX_EXAMPLE = REPO_ROOT / "ops/nginx/log-platform.conf.example"
INFRA_DOC = REPO_ROOT / "docs/02_infrastructure.md"
SECURITY_DOC = REPO_ROOT / "docs/06_security.md"
OPS_DOC = REPO_ROOT / "docs/07_operations.md"
DR_DOC = REPO_ROOT / "docs/09_disaster_recovery.md"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _read(path: Path) -> str:
    assert path.exists(), f"missing file: {path.relative_to(REPO_ROOT)}"
    return path.read_text(encoding="utf-8")


def _assert_no_secret_literals(text: str, label: str) -> None:
    lowered = text.lower()
    forbidden = [
        "super-secret",
        "temporary-password",
        "secret_api_password",
        "secret_db_password",
        "api_write_token=sk_",
        "api_read_token=sk_",
        "postgres_password=telematics",
        "minio_root_password=telematics",
    ]
    for marker in forbidden:
        assert marker not in lowered, f"possible real secret marker in {label}: {marker}"


def test_systemd_service_example_is_safe_and_localhost_bound() -> None:
    text = _read(SERVICE_EXAMPLE)
    _assert_no_secret_literals(text, "systemd example")
    assert "Description=Log Platform API and Portal" in text
    assert "EnvironmentFile=/etc/log-platform/api.env" in text
    assert "WorkingDirectory=/opt/log-platform" in text
    assert "uvicorn api.main:app" in text
    assert "--host 127.0.0.1" in text
    assert "--port 8000" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=5" in text
    assert "NoNewPrivileges=true" in text
    assert "PrivateTmp=true" in text
    assert "ProtectSystem=full" in text
    assert "ReadWritePaths=/opt/log-platform" in text
    assert "0.0.0.0" not in text
    print("PASS: systemd API service example is localhost-bound and hardened")


def test_nginx_reverse_proxy_example_preserves_forwarded_headers() -> None:
    text = _read(NGINX_EXAMPLE)
    _assert_no_secret_literals(text, "nginx example")
    assert "proxy_pass http://127.0.0.1:8000" in text
    assert "proxy_set_header Host $host" in text
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for" in text
    assert "proxy_set_header X-Forwarded-Proto $scheme" in text
    assert "client_max_body_size" in text
    assert "TLS" in text or "HTTPS" in text
    assert "0.0.0.0" not in text
    print("PASS: nginx example proxies to localhost and preserves forwarded headers")


def test_infrastructure_docs_list_real_required_env_without_invented_mode() -> None:
    text = _read(INFRA_DOC)
    for name in [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "MINIO_ENDPOINT",
        "MINIO_BUCKET",
        "MINIO_SECURE",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "API_READ_TOKEN",
        "API_WRITE_TOKEN",
        "ARTIFACT_EXPLORER_SESSION_SECRET",
        "BUSINESS_TIMEZONE",
        "LOG_API_URL",
    ]:
        assert name in text, name
    assert "no generic `APP_ENV`" in text
    assert "not ENV variables" in text
    assert "direct database export cap `20000`" in text

    env_example = _read(ENV_EXAMPLE)
    assert "BUSINESS_TIMEZONE=Europe/Warsaw" in env_example
    print("PASS: infrastructure docs and .env.example cover real API/portal env")


def test_operations_docs_include_deploy_checks_logs_and_rollback() -> None:
    text = _read(OPS_DOC)
    for phrase in [
        "ops/systemd/log-platform-api.service.example",
        "ops/nginx/log-platform.conf.example",
        "reverse proxy",
        "HTTPS",
        "bash ops/db_migrate.sh",
        "scripts/bootstrap_portal_admin.py",
        "ops/checks/check_portal_ready.py",
        "journalctl -u log-platform-api -f",
        "journalctl -u log-platform-api --since",
        "./ops/backup.sh",
        "gzip -t backups/postgres_YYYYmmdd_HHMMSS.sql.gz",
        "systemctl restart log-platform-api.service",
        "/artifact-explorer/login",
        "/user",
        "/admin",
        "/admin/audit",
        "For rollback",
    ]:
        assert phrase in text, phrase
    print("PASS: operations docs include deploy, checks, logging, and rollback guidance")


def test_security_and_dr_docs_cover_https_and_backup_touchpoints() -> None:
    security = _read(SECURITY_DOC)
    for phrase in [
        "uvicorn bound to `127.0.0.1`",
        "trusted reverse proxy",
        "HTTPS termination",
        "X-Forwarded-For",
        "0.0.0.0",
        "ARTIFACT_EXPLORER_SESSION_SECRET",
    ]:
        assert phrase in security, phrase

    dr = _read(DR_DOC)
    for phrase in [
        "Backup przed deployem",
        "./ops/backup.sh",
        "gzip -t backups/postgres_YYYYmmdd_HHMMSS.sql.gz",
        "tar -tzf backups/minio_YYYYmmdd_HHMMSS.tar.gz",
        "portal/local-UI tables",
        "MinIO/object storage remains a separate backup artifact",
        "Workflow A client business databases",
    ]:
        assert phrase in dr, phrase
    print("PASS: security and DR docs cover HTTPS/reverse proxy and backup touchpoints")


def main() -> int:
    test_systemd_service_example_is_safe_and_localhost_bound()
    test_nginx_reverse_proxy_example_preserves_forwarded_headers()
    test_infrastructure_docs_list_real_required_env_without_invented_mode()
    test_operations_docs_include_deploy_checks_logs_and_rollback()
    test_security_and_dr_docs_cover_https_and_backup_touchpoints()
    print("PASS: Phase 5C portal deployment documentation checks completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
