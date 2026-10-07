#!/usr/bin/env python3
"""Bootstrap or update the first local UI admin for the portal surfaces.

This is a small operator wrapper around the same PBKDF2 password format used
by Artifact Explorer, User Portal, and Admin Portal login.
"""
from __future__ import annotations

import argparse
import getpass
import hmac
import os
import sys

from manage_artifact_rbac import _bool, db_conn, hash_artifact_password


def _password_from_args(args: argparse.Namespace) -> str:
    if args.password_env:
        password = os.getenv(args.password_env)
        if not password:
            raise SystemExit(f"Environment variable {args.password_env} is empty or missing")
        return password
    if args.password is not None:
        if not args.password:
            raise SystemExit("Password must not be empty")
        return args.password
    default_env_password = os.getenv("PORTAL_ADMIN_PASSWORD")
    if default_env_password:
        return default_env_password
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Confirm password: ")
    if not hmac.compare_digest(first, second):
        raise SystemExit("Passwords do not match")
    if not first:
        raise SystemExit("Password must not be empty")
    return first


def bootstrap_admin(args: argparse.Namespace) -> None:
    username = str(args.username or os.getenv("PORTAL_ADMIN_USERNAME") or "").strip()
    if not username:
        raise SystemExit("Username is required. Use --username or PORTAL_ADMIN_USERNAME.")
    password_hash = hash_artifact_password(_password_from_args(args))
    display_name = str(args.display_name or "").strip() or None
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO artifact_users(username, password_hash, display_name, is_active, is_admin)
                VALUES (%s, %s, %s, %s, TRUE)
                ON CONFLICT (username) DO UPDATE SET
                  password_hash = EXCLUDED.password_hash,
                  display_name = COALESCE(EXCLUDED.display_name, artifact_users.display_name),
                  is_active = EXCLUDED.is_active,
                  is_admin = TRUE,
                  updated_at = now()
                RETURNING user_id, username, is_active, is_admin
                """,
                (username, password_hash, display_name, args.active),
            )
            row = cur.fetchone()
        conn.commit()
    print(f"portal admin {row['username']} saved user_id={row['user_id']} active={row['is_active']} admin={row['is_admin']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap or update a local portal admin user")
    parser.add_argument("--username", help="Admin username. Defaults to PORTAL_ADMIN_USERNAME when unset.")
    parser.add_argument("--display-name")
    parser.add_argument(
        "--password-env",
        help="Environment variable containing the password. If omitted, PORTAL_ADMIN_PASSWORD is used when set before prompting.",
    )
    parser.add_argument(
        "--password",
        help="Password literal. Prefer --password-env to avoid shell history/process-list exposure.",
    )
    parser.add_argument("--active", type=_bool, default=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bootstrap_admin(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
