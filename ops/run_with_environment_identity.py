#!/usr/bin/env python3
"""Run an operator command with repo config plus canonical identity, safely."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.environment_identity_file import (  # noqa: E402
    CANONICAL_IDENTITY_FILE,
    IdentityFileError,
    apply_identity_to_environ,
    identity_assignments_in_env_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    repo_env = REPO_ROOT / ".env"
    if identity_assignments_in_env_file(repo_env):
        raise IdentityFileError(
            "CONFLICTING_REPOSITORY_IDENTITY",
            "repository .env must not define LOG_PLATFORM_TARGET_ENVIRONMENT after provisioning",
        )
    load_dotenv(repo_env, override=False)
    apply_identity_to_environ(path=CANONICAL_IDENTITY_FILE, reject_conflict=True)
    os.environ["LOG_PLATFORM_REQUIRE_CANONICAL_IDENTITY"] = "1"
    existing_pythonpath = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    os.execvpe(command[0], command, os.environ)
    return 127


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except IdentityFileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
