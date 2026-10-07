#!/usr/bin/env python3
"""Create or update the first local Artifact Explorer admin user."""
from __future__ import annotations

import argparse
import sys

from manage_artifact_rbac import _bool, create_user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or update an Artifact Explorer admin user")
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name")
    parser.add_argument("--password-env", help="Read password from this environment variable instead of prompting")
    parser.add_argument("--active", type=_bool, default=True)
    args = parser.parse_args(argv)
    args.admin = True
    create_user(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
