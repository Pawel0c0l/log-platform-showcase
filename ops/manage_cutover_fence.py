#!/usr/bin/env python3
"""Operator view of, and deliberate control over, the cutover execution fence.

    status    what the fence says and whether production may execute
    recovery  the four facts that must be established before clearing it
    allow     re-enable execution (mutating: needs --execute and --reason)

`allow` exists because nothing else may clear an unsafe fence. In particular a
cutover process ending does not clear it: the fence records that the *outcome*
was never established, and the process exiting adds no information about the
installed wrapper. Re-enabling execution is a decision a person makes after
looking at `recovery`, which is why it demands an explicit reason that is
written into the fence for the next reader.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.cutover_fence import (  # noqa: E402
    STATE_ALLOWED,
    CutoverFenceError,
    read_fence,
    recovery_report,
    write_fence,
)

DEFAULT_RELEASE_ROOT = Path("/opt/log-platform-release")


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release-root", default=str(DEFAULT_RELEASE_ROOT))
    parser.add_argument("--source-repo", default=str(REPO_ROOT))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="current fence state")
    sub.add_parser("recovery", help="facts to establish before clearing an unsafe fence")
    allow = sub.add_parser("allow", help="re-enable production execution")
    allow.add_argument("--reason", required=True, help="why execution is safe again")
    allow.add_argument("--execute", action="store_true", help="actually write the fence")
    args = parser.parse_args(argv)

    release_root = Path(args.release_root)
    if args.command == "status":
        _emit({"action": "status", **read_fence(release_root)})
        return 0
    if args.command == "recovery":
        _emit({"action": "recovery",
               **recovery_report(release_root, Path(args.source_repo))})
        return 0

    current = read_fence(release_root)
    if not args.execute:
        _emit({"action": "allow", "executed": False, "current": current,
               "reason": args.reason,
               "next": "re-run the identical command with --execute"})
        return 0
    write_fence(release_root, STATE_ALLOWED, reason=args.reason,
                cleared_from=str(current.get("state")))
    _emit({"action": "allow", "executed": True, "previous_state": current.get("state"),
           **read_fence(release_root)})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CutoverFenceError as exc:
        print(json.dumps({"classification": exc.classification, "details": exc.details},
                         indent=2, sort_keys=True, default=str), file=sys.stderr)
        raise SystemExit(2)
