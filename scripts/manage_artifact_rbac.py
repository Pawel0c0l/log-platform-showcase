#!/usr/bin/env python3
"""Manage local Artifact Explorer users, roles, and permissions.

Examples:

  python3 scripts/manage_artifact_rbac.py create-user --username admin --admin
  python3 scripts/manage_artifact_rbac.py create-role --role-name stage2_clean
  python3 scripts/manage_artifact_rbac.py assign-role --username alice --role-name stage2_clean
  python3 scripts/manage_artifact_rbac.py grant-permission --role-name stage2_clean \
    --stage-name stage_2_clean --artifact-role cleaned --can-download
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import os
import secrets
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row


PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 240_000


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def hash_artifact_password(password: str, *, iterations: int = PASSWORD_HASH_ITERATIONS) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PASSWORD_HASH_ALGORITHM}${iterations}${_b64url_encode(salt)}${_b64url_encode(digest)}"


def db_conn():
    dsn = (
        f"host={os.getenv('POSTGRES_HOST','127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT','5432')} "
        f"dbname={os.getenv('POSTGRES_DB','logdb')} "
        f"user={os.getenv('POSTGRES_USER','loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD','')}"
    )
    return psycopg.connect(dsn, row_factory=dict_row)


def _bool(value: str) -> bool:
    value = str(value).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean: {value}")


def _password_from_args(args: argparse.Namespace) -> str:
    if args.password_env:
        value = os.getenv(args.password_env)
        if not value:
            raise SystemExit(f"Environment variable {args.password_env} is empty or missing")
        return value
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Confirm password: ")
    if not hmac.compare_digest(first, second):
        raise SystemExit("Passwords do not match")
    if not first:
        raise SystemExit("Password must not be empty")
    return first


def create_user(args: argparse.Namespace) -> None:
    password_hash = hash_artifact_password(_password_from_args(args))
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO artifact_users(username, password_hash, display_name, is_active, is_admin)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE SET
                  password_hash = EXCLUDED.password_hash,
                  display_name = EXCLUDED.display_name,
                  is_active = EXCLUDED.is_active,
                  is_admin = EXCLUDED.is_admin,
                  updated_at = now()
                RETURNING user_id, username, is_active, is_admin
                """,
                (args.username, password_hash, args.display_name, args.active, args.admin),
            )
            row = cur.fetchone()
        conn.commit()
    print(f"user {row['username']} saved user_id={row['user_id']} active={row['is_active']} admin={row['is_admin']}")


def create_role(args: argparse.Namespace) -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO artifact_roles(role_name, description)
                VALUES (%s, %s)
                ON CONFLICT (role_name) DO UPDATE SET
                  description = EXCLUDED.description,
                  updated_at = now()
                RETURNING role_id, role_name
                """,
                (args.role_name, args.description),
            )
            row = cur.fetchone()
        conn.commit()
    print(f"role {row['role_name']} saved role_id={row['role_id']}")


def assign_role(args: argparse.Namespace) -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM artifact_users WHERE username = %s", (args.username,))
            user = cur.fetchone()
            if not user:
                raise SystemExit(f"Unknown user: {args.username}")
            cur.execute("SELECT role_id FROM artifact_roles WHERE role_name = %s", (args.role_name,))
            role = cur.fetchone()
            if not role:
                raise SystemExit(f"Unknown role: {args.role_name}")
            cur.execute(
                """
                INSERT INTO artifact_user_roles(user_id, role_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (user["user_id"], role["role_id"]),
            )
        conn.commit()
    print(f"assigned role {args.role_name} to {args.username}")


def grant_permission(args: argparse.Namespace) -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role_id FROM artifact_roles WHERE role_name = %s", (args.role_name,))
            role = cur.fetchone()
            if not role:
                raise SystemExit(f"Unknown role: {args.role_name}")
            cur.execute(
                """
                INSERT INTO artifact_role_permissions(
                  role_id, can_view, can_preview, can_download, can_edit_annotations,
                  workflow_name, stage_name, artifact_role, report_type, client_code,
                  file_ext, layout_version, tag
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING permission_id
                """,
                (
                    role["role_id"],
                    args.can_view,
                    args.can_preview,
                    args.can_download,
                    args.can_edit_annotations,
                    args.workflow_name,
                    args.stage_name,
                    args.artifact_role,
                    args.report_type,
                    args.client_code,
                    args.file_ext,
                    args.layout_version,
                    args.tag,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    print(f"permission created permission_id={row['permission_id']} role={args.role_name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Artifact Explorer local RBAC")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-user")
    p.add_argument("--username", required=True)
    p.add_argument("--display-name")
    p.add_argument("--password-env", help="Read password from this environment variable instead of prompting")
    p.add_argument("--admin", action="store_true")
    p.add_argument("--active", type=_bool, default=True)
    p.set_defaults(func=create_user)

    p = sub.add_parser("create-role")
    p.add_argument("--role-name", required=True)
    p.add_argument("--description")
    p.set_defaults(func=create_role)

    p = sub.add_parser("assign-role")
    p.add_argument("--username", required=True)
    p.add_argument("--role-name", required=True)
    p.set_defaults(func=assign_role)

    p = sub.add_parser("grant-permission")
    p.add_argument("--role-name", required=True)
    p.add_argument("--can-view", type=_bool, default=True)
    p.add_argument("--can-preview", type=_bool, default=True)
    p.add_argument("--can-download", action="store_true")
    p.add_argument("--can-edit-annotations", action="store_true")
    p.add_argument("--workflow-name")
    p.add_argument("--stage-name")
    p.add_argument("--artifact-role")
    p.add_argument("--report-type")
    p.add_argument("--client-code")
    p.add_argument("--file-ext")
    p.add_argument("--layout-version", type=int)
    p.add_argument("--tag")
    p.set_defaults(func=grant_permission)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
