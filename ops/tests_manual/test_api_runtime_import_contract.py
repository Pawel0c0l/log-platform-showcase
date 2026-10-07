#!/usr/bin/env python3
"""Regression test for repository and real Compose API import contexts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "api"
EXPECTED_ROUTES = {
    "/user/eco-driving/api/providers": {"GET"},
    "/user/eco-driving/api/periods": {"GET"},
    "/user/eco-driving/api/ranking-entries": {"GET"},
    "/user/eco-driving/api/ranking-entry": {"GET"},
    "/user/eco-driving/api/ranking-entry/trips": {"GET"},
    "/user/eco-driving/api/ranking-entry/reconciliation": {"GET"},
    "/user/eco-driving": {"GET"},
    "/user/eco-driving/rankings": {"GET"},
    "/user/eco-driving/ranking-entry": {"GET"},
    "/user/eco-driving/ranking-entry/trips": {"GET"},
    # UI-20260820-09: the contributing-trip table as a spreadsheet. Same
    # permission pair as the page it downloads -- it calls the page's service.
    "/user/eco-driving/ranking-entry/trips/export": {"GET"},
    "/admin/client-access/eco-driving": {"GET"},
    "/admin/client-access/eco-driving/users/{user_id}": {"GET", "POST"},
    "/admin/client-access/eco-driving/groups/{group_id}": {"GET", "POST"},
}

PROBE = r"""
import json
import os
import sys
from pathlib import Path

mode = os.environ["IMPORT_CONTRACT_MODE"]
if mode == "repository":
    import api.main as target
    expected_prefix = "api.eco_driving_explorer"
    forbidden_prefix = "eco_driving_explorer"
else:
    import main as target
    expected_prefix = "eco_driving_explorer"
    forbidden_prefix = "api.eco_driving_explorer"

import importlib
errors = importlib.import_module(expected_prefix + ".errors")
registry = importlib.import_module(expected_prefix + ".registry")
provider = registry.get_provider("ALPHA00001", "driver", client_id="runtime-contract-probe")
identity_ok = (
    registry.ProviderNotFoundError is errors.ProviderNotFoundError
    and provider.__class__.__module__ == expected_prefix + ".alpha_driver_provider"
    and registry._REGISTRY[("ALPHA00001", "driver")] is provider.__class__
)

routes = []
for route in target.app.routes:
    if "eco-driving" not in route.path:
        continue
    methods = sorted(set(route.methods or ()) - {"HEAD", "OPTIONS"})
    routes.append([route.path, methods])
loaded = sorted(name for name in sys.modules if name == expected_prefix or name.startswith(expected_prefix + "."))
forbidden = sorted(name for name in sys.modules if name == forbidden_prefix or name.startswith(forbidden_prefix + "."))
resolved_paths = []
for entry in sys.path:
    try:
        resolved_paths.append(str(Path(entry or os.getcwd()).resolve()))
    except OSError:
        pass
print(json.dumps({
    "app": target.app is not None,
    "routes": routes,
    "loaded": loaded,
    "forbidden": forbidden,
    "api_parent_loaded": "api" in sys.modules,
    "identity_ok": identity_ok,
    "resolved_sys_path": resolved_paths,
}, sort_keys=True))
"""


def _clean_environment(mode: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["IMPORT_CONTRACT_MODE"] = mode
    return env


def _run_probe(mode: str, cwd: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=cwd,
        env=_clean_environment(mode),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"{mode} import failed with exit {result.returncode}: {result.stderr[-2000:]}"
        )
    assert "duplicate" not in result.stderr.lower(), result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"{mode} probe produced no result"
    return json.loads(lines[-1])


def _assert_routes(payload: dict) -> None:
    assert payload["app"] is True
    assert payload["identity_ok"] is True
    actual: dict[str, list[set[str]]] = {}
    for path, methods in payload["routes"]:
        actual.setdefault(path, []).append(set(methods))
    assert set(actual) == set(EXPECTED_ROUTES), (set(actual), set(EXPECTED_ROUTES))
    for path, expected_methods in EXPECTED_ROUTES.items():
        observed = actual[path]
        for method in expected_methods:
            assert sum(method in methods for methods in observed) == 1, (path, method, observed)
        assert set().union(*observed) == expected_methods, (path, observed)


def main() -> None:
    repository = _run_probe("repository", ROOT)
    _assert_routes(repository)
    assert repository["loaded"]
    assert repository["forbidden"] == []
    assert repository["api_parent_loaded"] is True
    print("PASS: repository-root import api.main loads every Eco route exactly once")

    compose = _run_probe("compose", API_DIR)
    _assert_routes(compose)
    assert compose["loaded"]
    assert compose["forbidden"] == []
    assert compose["api_parent_loaded"] is False
    assert str(ROOT.resolve()) not in compose["resolved_sys_path"]
    print("PASS: /app-style import main needs no repository root or api parent package")

    api_scoring = API_DIR / "eco_driving_explorer" / "eco_scoring.py"
    job_scoring = ROOT / "jobs" / "ecodriving" / "eco_scoring.py"
    assert api_scoring.read_text(encoding="utf-8").rstrip() == job_scoring.read_text(encoding="utf-8").rstrip()
    print("PASS: API-contained scoring helper is source-equivalent to the job helper")

    print("OK - API runtime import contract tests passed")


if __name__ == "__main__":
    main()
