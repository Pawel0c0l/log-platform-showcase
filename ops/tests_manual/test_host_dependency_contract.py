#!/usr/bin/env python3
"""The native host virtualenv can actually start the API — proven from the manifests.

`api/main.py` imports `api/row_reference.py` at module level, and that module
imports `cryptography` unconditionally. The release-candidate audit found
`cryptography` declared only in `api/requirements.txt` — the file the CONTAINER
image installs — while `docs/11` §2 builds the native host's `.venv` from
`requirements-host.txt`, and the systemd unit runs `.venv/bin/uvicorn
api.main:app` out of that same venv. Hiding the module reproduced the
consequence exactly:

    API_IMPORT_FAILS_WITHOUT_CRYPTOGRAPHY=True

The package reached the running host only because someone installed it by hand
(a version, at that, matching no committed pin). A dependency that exists only
because of a manual step is not a deployable contract: the next host built from
the committed documentation does not get it.

This suite is the standing guard. It is a STATIC check on purpose — it installs
nothing, needs no network and no database, so it can run anywhere and cannot be
satisfied by whatever happens to be in the current interpreter. It asserts:

  1. every unconditional third-party import the API makes is covered by the
     host installation contract;
  2. that contract is ONE declaration with ONE pin per package, so the image and
     the host can never drift apart;
  3. `docs/07` and `docs/11` agree on the install command.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
        ops/tests_manual/test_host_dependency_contract.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOST_MANIFEST = ROOT / "requirements-host.txt"
API_MANIFEST = ROOT / "api/requirements.txt"

FAILURES: list[str] = []


def _ok(message: str) -> None:
    print(f"PASS: {message}")


# The import name and the distribution name differ often enough that guessing is
# wrong. Only the packages the API actually imports need an entry; an import
# with no entry is reported rather than silently skipped, so a NEW third-party
# dependency cannot slip past this suite by being unknown to it.
DISTRIBUTION_BY_IMPORT = {
    "boto3": "boto3",
    "cryptography": "cryptography",
    "dotenv": "python-dotenv",
    "fastapi": "fastapi",
    "multipart": "python-multipart",
    "openpyxl": "openpyxl",
    "pandas": "pandas",
    "psycopg": "psycopg",
    "requests": "requests",
    "uvicorn": "uvicorn",
    "xlrd": "xlrd",
    "yaml": "PyYAML",
    "minio": "minio",
}

# Everything under `api/` is first-party regardless of how it is imported:
# `api/main.py` imports its siblings both as `.row_reference` (package) and as
# `row_reference` (flat, the container's `WORKDIR /app` layout), so a bare
# top-level name that names a module in `api/` is not a distribution.
def _first_party_names() -> set[str]:
    names = {"api", "ops", "jobs", "scripts", "delivery"}
    for path in (ROOT / "api").rglob("*.py"):
        names.add(path.stem)
        names.add(path.parent.name)
    return names


def _api_source_files() -> list[Path]:
    """Every module reachable at API import time, by directory rather than by name."""
    files = [ROOT / "api/main.py"]
    for pattern in ("api/*.py", "api/report_explorer/*.py",
                    "api/portal_ui/*.py", "api/artifacts/*.py"):
        files.extend(sorted(ROOT.glob(pattern)))
    return sorted(set(files))


def _scan_imports() -> tuple[set[str], set[str]]:
    """Third-party imports of the API, split by when they are needed.

    Returns `(start_up, lazy)`. A MODULE-LEVEL import runs the moment
    `api.main` is imported, so its absence is a start-up failure — that is the
    set the audit's defect lived in. A function-local import (`openpyxl` and
    `xlrd` inside `artifacts/preview.py`, `dotenv` inside `platform_prune.py`)
    only fails when that code path runs.

    BOTH sets must be declared. The distinction is kept because the failure
    modes differ and the messages should say which one is at stake, not because
    a lazily imported package may go undeclared.
    """
    first_party = _first_party_names()
    stdlib = set(sys.stdlib_module_names)
    start_up: set[str] = set()
    lazy: set[str] = set()

    def _names(node) -> list[str]:
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        if isinstance(node, ast.ImportFrom):
            if node.level:               # relative: first-party by construction
                return []
            return [node.module] if node.module else []
        return []

    for path in _api_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Module level = a direct child of the module body, or of a `try` /
        # `if` that is itself at module level. `api/main.py` imports its own
        # siblings through exactly such a try/except (package vs flat layout),
        # so treating every `try` as lazy would hide real start-up imports.
        module_level: set[int] = set()

        def _walk_top(body) -> None:
            for stmt in body:
                if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    module_level.add(id(stmt))
                elif isinstance(stmt, (ast.Try, ast.If)):
                    _walk_top(stmt.body)
                    _walk_top(stmt.orelse)
                    _walk_top(getattr(stmt, "finalbody", []))
                    for handler in getattr(stmt, "handlers", []):
                        _walk_top(handler.body)

        _walk_top(tree.body)

        for node in ast.walk(tree):
            for name in _names(node):
                top = name.split(".")[0]
                if not top or top in stdlib or top in first_party:
                    continue
                (start_up if id(node) in module_level else lazy).add(top)

    return start_up, lazy - start_up


def _unconditional_imports() -> set[str]:
    """Every third-party package the API imports, start-up or lazy."""
    start_up, lazy = _scan_imports()
    return start_up | lazy


def _parse_manifest(path: Path, *, _seen: set[Path] | None = None) -> dict[str, str]:
    """Resolve a requirements file the way pip does, following `-r` includes.

    Returns {canonical distribution name: pinned version}. Nested paths resolve
    relative to the INCLUDING file's directory, which is pip's rule and the
    reason `-r api/requirements.txt` works from any working directory.
    """
    seen = _seen if _seen is not None else set()
    resolved = path.resolve()
    if resolved in seen:
        raise AssertionError(f"circular requirements include at {path}")
    seen.add(resolved)

    pins: dict[str, str] = {}
    for raw in resolved.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        include = re.match(r"^-r\s+(.+)$", line)
        if include:
            nested = (resolved.parent / include.group(1).strip()).resolve()
            for dist, version in _parse_manifest(nested, _seen=seen).items():
                previous = pins.get(dist)
                assert previous in (None, version), (
                    f"{dist} is pinned twice at different versions: "
                    f"{previous} and {version}"
                )
                pins[dist] = version
            continue
        spec = re.match(r"^([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;]+)", line)
        assert spec, f"{path.name}: unparsable requirement {line!r}"
        dist = spec.group(1).lower().replace("_", "-")
        version = spec.group(2)
        previous = pins.get(dist)
        assert previous in (None, version), (
            f"{dist} is pinned twice at different versions: {previous} and {version}"
        )
        pins[dist] = version
    return pins


# ===========================================================================
# 1. The contract covers what the API imports
# ===========================================================================

def test_every_unconditional_api_import_is_declared_for_the_host() -> None:
    start_up, lazy = _scan_imports()
    imports = start_up | lazy
    assert imports, "the import scan found nothing; the scan itself is broken"
    assert "cryptography" in start_up, (
        "cryptography must be seen as a START-UP import: api/main.py imports "
        "api/row_reference.py at module level. If this fails the scan stopped "
        "modelling the defect it exists to prevent."
    )

    unknown = sorted(name for name in imports if name not in DISTRIBUTION_BY_IMPORT)
    assert not unknown, (
        f"the API imports {unknown}, which this suite cannot map to a "
        "distribution. Add it to DISTRIBUTION_BY_IMPORT and to the host "
        "contract — an unmapped import is an undeclared dependency."
    )

    host = _parse_manifest(HOST_MANIFEST)
    required = {DISTRIBUTION_BY_IMPORT[name].lower(): name for name in sorted(imports)}
    missing = sorted(
        f"{dist} (imported as {module!r})"
        for dist, module in required.items() if dist not in host
    )
    assert not missing, (
        "the native host contract requirements-host.txt does not install: "
        + "; ".join(missing)
        + ". The systemd unit runs .venv/bin/uvicorn api.main:app out of that "
        "venv, so a fresh host built from docs/11 §2 would fail to start."
    )
    _ok(f"all {len(required)} API imports are declared for the host — "
        f"start-up: {', '.join(sorted(DISTRIBUTION_BY_IMPORT[n] for n in start_up))}; "
        f"lazy: {', '.join(sorted(DISTRIBUTION_BY_IMPORT[n] for n in lazy)) or 'none'}")


def test_cryptography_specifically_is_reachable_from_the_host_manifest() -> None:
    """The exact package the audit proved absent, named rather than implied."""
    host = _parse_manifest(HOST_MANIFEST)
    assert "cryptography" in host, (
        "requirements-host.txt must install cryptography: api/main.py imports "
        "api/row_reference.py at module level and that module imports it "
        "unconditionally"
    )
    api = _parse_manifest(API_MANIFEST)
    assert api.get("cryptography") == host["cryptography"], (
        "cryptography must carry ONE pin across the image and the host, not two",
        api.get("cryptography"), host["cryptography"],
    )
    source = (ROOT / "api/row_reference.py").read_text(encoding="utf-8")
    assert "from cryptography" in source, "the premise of this test changed"
    _ok(f"cryptography=={host['cryptography']} is one pin, shared by the image "
        "and the native host")


def test_the_host_contract_includes_the_api_contract_once() -> None:
    """One declaration, not two hand-synchronized lists."""
    text = HOST_MANIFEST.read_text(encoding="utf-8")
    includes = re.findall(r"^-r\s+(.+)$", text, flags=re.MULTILINE)
    assert includes == ["api/requirements.txt"], includes

    host = _parse_manifest(HOST_MANIFEST)
    api = _parse_manifest(API_MANIFEST)
    # Inclusion is real: every API pin is in the host set, at the same version.
    for dist, version in api.items():
        assert host.get(dist) == version, (dist, version, host.get(dist))
    # And the host adds only host-only packages on top.
    extra = sorted(set(host) - set(api))
    assert extra, "the host contract should still add its own packages"
    _ok(f"the host contract includes api/requirements.txt and adds only "
        f"{', '.join(extra)}")


def test_no_package_is_pinned_twice_at_two_versions() -> None:
    """`_parse_manifest` raises on divergence; this states it as a check."""
    _parse_manifest(HOST_MANIFEST)
    _parse_manifest(API_MANIFEST)
    _ok("no package is pinned at two different versions across the manifests")


# ===========================================================================
# 2. The documentation matches the contract
# ===========================================================================

def test_the_two_deployment_documents_agree_on_the_install_command() -> None:
    """`docs/07` and `docs/11` disagreed; the disagreement was the defect."""
    readiness = (ROOT / "docs/11_operational_readiness.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs/07_operations.md").read_text(encoding="utf-8")

    for label, text in (("docs/11", readiness), ("docs/07", operations)):
        assert "-r requirements-host.txt" in text, label
        # Neither document may tell an operator to install the API manifest as a
        # SEPARATE step: that is the two-source-of-truth shape that drifted.
        assert not re.search(r"-r\s+api/requirements\.txt\s+-r\s+requirements-host\.txt", text), (
            f"{label} still prescribes the two-file install that allowed the "
            "host and the image to diverge"
        )
    assert "requirements-host.txt" in readiness and "complete" in readiness
    _ok("docs/07 and docs/11 prescribe the same single-file host install")


def test_the_systemd_unit_really_runs_that_virtualenv() -> None:
    """The contract only matters because the unit executes this venv."""
    unit = (ROOT / "ops/systemd/log-platform-api.service.example").read_text(encoding="utf-8")
    assert "/.venv/bin/uvicorn api.main:app" in unit, unit
    installer = (ROOT / "ops/systemd/install_log_platform_api_service.sh").read_text(encoding="utf-8")
    assert 'venv_path="${REPO_ROOT}/.venv"' in installer, "the default venv path moved"
    _ok("the systemd unit executes .venv/bin/uvicorn api.main:app, so the host "
        "contract is the start-up contract")


def test_the_release_bound_unit_reaches_the_same_virtualenv() -> None:
    """Binding CODE to the release must not have forked the DEPENDENCY contract.

    The production unit no longer names an interpreter: it hands `uvicorn` to
    /usr/local/bin/log-ops-runner.sh, which execs `<release>/.venv/bin/python -m`.
    `<release>/.venv` is a symlink to this same checkout's virtualenv — the
    release layout links it rather than copying it — so requirements-host.txt is
    still the single source of truth for what the API can import. If that ever
    stops being true, the host contract stops describing production and this
    fails rather than going quietly stale.
    """
    unit = (ROOT / "ops/systemd/proposed/log-platform-api.service").read_text(encoding="utf-8")
    exec_start = [line.partition("=")[2].strip() for line in unit.splitlines()
                  if line.startswith("ExecStart=")]
    assert len(exec_start) == 1, exec_start
    argv = exec_start[0].split()
    assert argv[0] == "/usr/local/bin/log-ops-runner.sh", argv
    assert argv[1] == "uvicorn" and "api.main:app" in argv, argv

    launcher = (ROOT / "ops/systemd/proposed/log-ops-runner.sh").read_text(encoding="utf-8")
    assert 'VENV_PY="${BASE_DIR}/.venv/bin/python"' in launcher, (
        "the launcher no longer runs the release's linked virtualenv")
    assert 'exec "${VENV_PY}" -m "${MODULE}"' in launcher, (
        "the launcher no longer execs the module through that interpreter")

    # And the release layout must keep linking, not copying, that virtualenv.
    boundary = (ROOT / "ops/release_boundary.py").read_text(encoding="utf-8")
    assert 'RUNTIME_LINK_VENV = ".venv"' in boundary, "the runtime link name moved"
    _ok("the release-bound unit reaches the same virtualenv through the release "
        "runtime link, so the host contract still describes production")


def _run_all() -> None:
    test_every_unconditional_api_import_is_declared_for_the_host()
    test_cryptography_specifically_is_reachable_from_the_host_manifest()
    test_the_host_contract_includes_the_api_contract_once()
    test_no_package_is_pinned_twice_at_two_versions()
    test_the_two_deployment_documents_agree_on_the_install_command()
    test_the_systemd_unit_really_runs_that_virtualenv()
    test_the_release_bound_unit_reaches_the_same_virtualenv()
    print("\nALL PASS: RC_HOST_DEPENDENCY_CONTRACT")


if __name__ == "__main__":
    _run_all()
