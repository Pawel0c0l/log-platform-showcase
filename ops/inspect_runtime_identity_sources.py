#!/usr/bin/env python3
"""Read-only redacted inventory of runtime identity sources and active processes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.environment_identity_file import IDENTITY_KEY, identity_assignments_in_env_file
from ops.runtime_identity_inspection import RootInspectionError, inspect_root_sources
from ops.runtime_identity_readiness import _docker_api_value, _systemd_api_value


def main() -> int:
    repo_source = REPO_ROOT / ".env"
    values = identity_assignments_in_env_file(repo_source)
    user_source = {
        "logical_source": "repository_environment",
        "path": str(repo_source),
        "exists": repo_source.exists(),
        "active_assignment_count": len(values),
        "canonical_value": values[0] if len(values) == 1 else None,
        "duplicate_definitions": len(values) > 1,
    }
    try:
        privileged: object = inspect_root_sources()
        classification = "RUNTIME_IDENTITY_SOURCES_INSPECTED"
    except RootInspectionError as exc:
        privileged = {
            "classification": exc.classification,
            "reason_code": exc.reason_code,
            **exc.details,
        }
        classification = exc.classification
    systemd_status, systemd_value = _systemd_api_value()
    docker_status, docker_value = _docker_api_value(REPO_ROOT)
    output = {
        "classification": classification,
        "writes_performed": False,
        "identity_key": IDENTITY_KEY,
        "user_readable_sources": [user_source],
        "root_owned_sources": privileged,
        "active_process_identity": [
            {"component": "systemd_api", "status": systemd_status, "effective_environment": systemd_value},
            {"component": "docker_api", "status": docker_status, "effective_environment": docker_value},
        ],
    }
    print(json.dumps(output, sort_keys=True, indent=2))
    return 0 if classification == "RUNTIME_IDENTITY_SOURCES_INSPECTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
