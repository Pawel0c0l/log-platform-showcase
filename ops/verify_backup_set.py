#!/usr/bin/env python3
"""Verify backup sets against the CURRENT platform's attested identity.

    .venv/bin/python ops/run_with_environment_identity.py -- .venv/bin/python -m ops.verify_backup_set 20260809_030000 [more...]

Run it behind `ops/run_with_environment_identity.py`, exactly as every host job
does via `/usr/local/bin/log-job-runner.sh`. Attestation needs
`LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID` and the four expected PostgreSQL
values, which live in the repository `.env` rather than in
`/etc/log-platform/environment-identity.env`. Invoked bare, this module exits 1
with `EXPECTED_PLATFORM_IDENTITY_MISSING` before verifying anything — fail-closed,
but it stops a legitimate restore for the wrong reason.

This exists because the identity pins are the part an operator forgets. The
underlying `ops/backup_manifest.py verify` CLI takes `--expected-environment`,
`--expected-database` and `--expected-platform-uuid` as *optional* flags, and the
documented restore and post-retention procedures both omitted them. A valid,
self-contained, correctly hashed **staging** backup therefore reported
`status: VALID` inside the production restore procedure — structurally perfect and
completely unsuitable for the target.

So this helper does not accept identity as an argument. It attests the target
itself, through the same path retention uses
(`ops.backup_retention.load_authoritative_identity()` →
`jobs.common.environment_identity.attest_platform_identity()` reading
`ops_control.environment_identity`), and there is no flag to override it. A
procedure that calls this cannot omit the pins.

Contract:

* every named set is checked under the full contract — exact timestamp-derived
  members, environment/database/platform_uuid pins, mode, size, sha256 and full
  gzip/tar traversal;
* **aggregate fail-closed**: exit 1 if *any* set fails, after reporting every
  result, so `for ... || echo FAILED` shell patterns are unnecessary and the
  caller cannot accidentally swallow a failure;
* exit 2 if the authoritative identity cannot be established at all — an
  unattestable target is never a licence to proceed;
* `repository_commit` is deliberately not pinned: it records the commit a backup
  was taken at, so requiring it to equal current HEAD would reject every backup
  older than the last commit. It is provenance, not deployment identity.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.backup_retention import (  # noqa: E402
    DEFAULT_BACKUP_DIR,
    AuthoritativeIdentity,
    discover_sets,
    load_authoritative_identity,
    verify_set,
)

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_NO_IDENTITY = 2


def verify_timestamps(
    timestamps: Sequence[str],
    *,
    backup_dir: Path,
    identity: AuthoritativeIdentity,
) -> list[dict[str, Any]]:
    """Full-contract verification of each named set. Unknown sets are failures."""
    present = {item.timestamp: item for item in discover_sets(backup_dir)}
    results: list[dict[str, Any]] = []
    for stamp in timestamps:
        item = present.get(stamp)
        if item is None:
            results.append(
                {"timestamp": stamp, "verified": False,
                 "reason": "backup set not found in backup directory"}
            )
            continue
        checked = verify_set(item, identity=identity, check_archives=True)
        results.append(
            {"timestamp": stamp, "verified": bool(checked.verified),
             "reason": checked.invalid_reason}
        )
    return results


def main(
    argv: Sequence[str] | None = None,
    *,
    identity_loader: Callable[[], AuthoritativeIdentity] = load_authoritative_identity,
) -> int:
    """`identity_loader` is injected only by tests; there is no CLI flag for it."""
    parser = argparse.ArgumentParser(
        description="Verify backup sets against the attested current platform identity",
    )
    parser.add_argument("timestamps", nargs="+", metavar="YYYYmmdd_HHMMSS")
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Machine-readable report")
    args = parser.parse_args(argv)

    backup_dir = args.backup_dir or Path(DEFAULT_BACKUP_DIR)

    try:
        identity = identity_loader()
    except Exception as exc:
        # Fail closed. Not knowing who this platform is means not knowing whether
        # the backup belongs to it, which is exactly when restoring is dangerous.
        report = {
            "schema": "log-platform-backup-verification/v1",
            "ok": False,
            "error": f"authoritative identity unavailable: {type(exc).__name__}: {exc}",
        }
        print(json.dumps(report, indent=2, sort_keys=True) if args.json else report["error"],
              file=sys.stderr)
        return EXIT_NO_IDENTITY

    results = verify_timestamps(
        args.timestamps, backup_dir=backup_dir, identity=identity
    )
    failures = [item for item in results if not item["verified"]]
    report = {
        "schema": "log-platform-backup-verification/v1",
        "ok": not failures,
        "authoritative_identity": identity.as_dict(),
        "backup_dir": str(backup_dir),
        "results": results,
        "failed": [item["timestamp"] for item in failures],
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"target: environment={identity.environment} database={identity.database} "
            f"platform_uuid={identity.platform_uuid}"
        )
        for item in results:
            status = "VALID" if item["verified"] else f"INVALID ({item['reason']})"
            print(f"  {item['timestamp']}: {status}")
        if failures:
            # Name them, and still exit non-zero. Reporting must not soften the
            # verdict the exit code carries.
            print(
                f"FAILED: {len(failures)} of {len(results)} sets did not verify against "
                f"this platform: {', '.join(item['timestamp'] for item in failures)}",
                file=sys.stderr,
            )
    return EXIT_INVALID if failures else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
