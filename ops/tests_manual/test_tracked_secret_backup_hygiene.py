#!/usr/bin/env python3
"""Deterministic guard: credential-bearing backup files must never be tracked.

Filename- and pattern-based only. This test never reads, prints or hashes a
secret value; it asserts facts about the Git index and the ignore policy.

Run:  python3 ops/tests_manual/test_tracked_secret_backup_hygiene.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Tracked paths matching any of these are a secret-hygiene regression.
FORBIDDEN_TRACKED = [
    re.compile(r"(^|/)\.env\.bak(\..*)?$"),
    re.compile(r"(^|/)\.env\..*\.bak(\..*)?$"),
    re.compile(r"(^|/)\.env\.backup(\..*)?$"),
    re.compile(r"(^|/)\.env\.(orig|save|old)$"),
    re.compile(r"(^|/)\.env~$"),
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.local$"),
    re.compile(r"(^|/)\.claude/settings\.local\.json$"),
    re.compile(r"(^|/)\.claude/settings\.local\.json\..+$"),
]

# Tracked offenders the owner has NOT yet authorized removing. Each entry is a
# real, still-open hygiene defect: it is exempted from the failure so the guard
# stays green on the authorized scope, and is reported loudly on every run so it
# cannot be forgotten. Removing the file must also remove its entry — a stale
# exemption is itself a failure.
#
# EMPTY IS THE CORRECT STATE. .claude/settings.local.json.backup-20260808 was
# the last entry: a machine-local Claude permissions backup committed in
# 2756f9b, holding only a "permissions" object and no credential material. The
# owner authorized untracking it for the portal V1 release candidate, so it was
# removed from the index (the machine-local copy is kept, now ignored) and its
# exemption deleted with it. The set stays here, empty, because the stale-entry
# check above is what makes an exemption impossible to leave behind silently.
KNOWN_PENDING_OWNER_AUTHORIZATION: set[str] = set()

# Committed templates that must REMAIN tracked and must NOT be ignored.
REQUIRED_TRACKED = [
    ".env.example",
    ".claude/settings.json",
]

# Representative paths the ignore policy must reject.
MUST_BE_IGNORED = [
    ".env.bak.20260101_000000",
    ".env.bak.quote-fix.20260101_000000",
    ".env.stage1.bak",
    ".env.backup.20260101",
    ".env.orig",
    ".claude/settings.local.json",
    ".claude/settings.local.json.before-emergency-recovery",
    ".claude/settings.local.json.backup-20260808",
]

# Paths the ignore policy must NOT swallow.
MUST_NOT_BE_IGNORED = [
    ".env.example",
    ".claude/settings.json",
    "docs/env.stage1.example",
]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def _is_ignored(path: str) -> bool:
    # check-ignore exits 0 when the path IS ignored, 1 when it is not.
    return subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).returncode == 0


def main() -> int:
    failures: list[str] = []
    tracked = [p for p in _git("ls-files").splitlines() if p]

    matched = {p for p in tracked if any(rx.search(p) for rx in FORBIDDEN_TRACKED)}
    offenders = sorted(matched - KNOWN_PENDING_OWNER_AUTHORIZATION)
    if offenders:
        failures.append(
            "tracked secret-backup class present (paths only):\n    "
            + "\n    ".join(offenders)
        )

    stale = sorted(KNOWN_PENDING_OWNER_AUTHORIZATION - matched)
    if stale:
        failures.append(
            "stale exemption — these are no longer tracked, delete them from "
            "KNOWN_PENDING_OWNER_AUTHORIZATION:\n    " + "\n    ".join(stale)
        )

    for required in REQUIRED_TRACKED:
        if required not in tracked:
            failures.append(f"required committed file is missing from the index: {required}")

    for path in MUST_BE_IGNORED:
        if not _is_ignored(path):
            failures.append(f".gitignore does not cover a secret-backup path: {path}")

    for path in MUST_NOT_BE_IGNORED:
        if _is_ignored(path):
            failures.append(f".gitignore wrongly swallows a committed template: {path}")

    if failures:
        print("SECRET_BACKUP_HYGIENE: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("SECRET_BACKUP_HYGIENE: PASS")
    for path in sorted(KNOWN_PENDING_OWNER_AUTHORIZATION):
        print(f"  PENDING_OWNER_AUTHORIZATION (still tracked, not a credential): {path}")
    print(f"  tracked files scanned: {len(tracked)}")
    print(f"  forbidden patterns enforced: {len(FORBIDDEN_TRACKED)}")
    print(f"  ignore assertions: {len(MUST_BE_IGNORED)} positive, {len(MUST_NOT_BE_IGNORED)} negative")
    return 0


if __name__ == "__main__":
    sys.exit(main())
