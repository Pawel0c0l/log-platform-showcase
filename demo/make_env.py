#!/usr/bin/env python3
"""Write demo/.env.demo from .env.example with generated secrets and demo ports.

Every CHANGE_ME placeholder becomes a random value, the platform identity gets
a fresh UUID, and host-facing endpoints point at the ports published by
demo/docker-compose.demo.yml. Nothing here is read from any other environment.
"""
from __future__ import annotations

import re
import secrets
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / "demo" / ".env.demo"

OVERRIDES = {
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "5433",
    "LOG_PLATFORM_EXPECTED_POSTGRES_HOST": "127.0.0.1",
    "LOG_PLATFORM_EXPECTED_POSTGRES_PORT": "5433",
    "LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID": str(uuid.uuid4()),
    "MINIO_ENDPOINT": "127.0.0.1:9010",
    "MINIO_HOST_ENDPOINT": "127.0.0.1:9010",
    "LOG_API_URL": "http://127.0.0.1:8010",
    "ARTIFACT_EXPLORER_BASE_URL": "http://127.0.0.1:8010",
    "REPORTS_DATA_DIR": str(ROOT / "demo" / "data" / "reports"),
}


def main() -> int:
    if TARGET.exists():
        print(f"{TARGET} already exists; delete it to regenerate", file=sys.stderr)
        return 0
    lines = []
    for line in EXAMPLE.read_text("utf-8").splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if not m:
            lines.append(line)
            continue
        key, value = m.group(1), m.group(2)
        if key in OVERRIDES:
            value = OVERRIDES[key]
        elif value.startswith("CHANGE_ME"):
            value = secrets.token_urlsafe(24)
        # The file is read both by Compose and by `source` in a shell, so a
        # value with whitespace or a comment character must be quoted.
        if re.search(r"""[\s#'"]""", value) and not (value.startswith('"') and value.endswith('"')):
            value = '"' + value.replace('"', '\\"') + '"'
        lines.append(f"{key}={value}")
    TARGET.write_text("\n".join(lines) + "\n", "utf-8")
    TARGET.chmod(0o600)
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
