#!/usr/bin/env python3
"""Regression test for the Docker API production execution trust boundary.

Static, hermetic checks over the Compose definitions, the API Dockerfile, the
API build-context ignore list and the recorded refresh actions. No Docker daemon
is contacted and no runtime state is read, so this test is safe to run anywhere
the repository is checked out.

The invariant under test: production executes API code selected by the image,
not by the user-writable checkout, and it executes it as a non-root identity
with no capabilities and no privilege escalation.
"""

from __future__ import annotations

from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = ROOT / "docker-compose.yml"
DEV_COMPOSE = ROOT / "docker-compose.dev.yml"
DOCKERFILE = ROOT / "api" / "Dockerfile"
DOCKERIGNORE = ROOT / "api" / ".dockerignore"

# Compose auto-loads exactly these names beside the base file. None may exist in
# this repository, otherwise a plain `docker compose ...` invocation could pick
# up a development source bind without anybody asking for it.
AUTO_LOADED_OVERRIDE_NAMES = (
    "docker-compose.override.yml", "docker-compose.override.yaml",
    "compose.override.yml", "compose.override.yaml",
)
# Everything api/main.py imports at runtime; none may be excluded from the image.
REQUIRED_CONTEXT_ENTRIES = (
    "main.py", "client.py", "platform_prune.py", "suspected_bug.py",
    "timezone_utils.py", "row_reference.py", "__init__.py", "requirements.txt",
    "artifacts", "eco_driving_explorer",
)
REFRESH_ACTION_SOURCES = (
    ROOT / "ops/runtime_identity_readiness.py",
    ROOT / "ops/provision_runtime_environment_identity.py",
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dockerfile_instructions() -> list[tuple[str, str]]:
    """Return (instruction, argument) pairs with line continuations joined."""
    joined = re.sub(r"\\\s*\n\s*", " ", DOCKERFILE.read_text(encoding="utf-8"))
    instructions = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, argument = line.partition(" ")
        instructions.append((keyword.upper(), argument.strip()))
    return instructions


def test_production_compose_executes_image_code_as_non_root() -> None:
    api = _load(BASE_COMPOSE)["services"]["api"]
    assert "volumes" not in api, api.get("volumes")
    assert "./api:/app" not in BASE_COMPOSE.read_text(encoding="utf-8")
    assert api["user"] == "1000:1000", api.get("user")
    assert api["cap_drop"] == ["ALL"], api.get("cap_drop")
    assert "no-new-privileges:true" in api["security_opt"], api.get("security_opt")
    assert api["ports"] == ["127.0.0.1:8000:8000"], api["ports"]
    assert api["build"]["context"] == "./api"
    assert api["image"] == "log-platform-api:latest", api.get("image")
    assert api["working_dir"] == "/app"
    print("PASS: production API service runs image code as 1000:1000, no caps, no-new-privileges")


def test_production_compose_leaves_postgres_and_minio_alone() -> None:
    services = _load(BASE_COMPOSE)["services"]
    assert set(services) == {"postgres", "minio", "api"}, sorted(services)
    postgres, minio = services["postgres"], services["minio"]
    assert postgres["image"] == "postgres:16"
    assert postgres["ports"] == ["127.0.0.1:5432:5432"]
    assert postgres["volumes"] == ["pgdata:/var/lib/postgresql/data"]
    assert minio["image"] == "minio/minio:latest"
    assert minio["ports"] == ["9000:9000", "9001:9001"]
    assert minio["volumes"] == ["miniodata:/data"]
    for name, service in (("postgres", postgres), ("minio", minio)):
        for key in ("user", "cap_drop", "security_opt", "read_only"):
            assert key not in service, (name, key)
    print("PASS: Postgres and MinIO definitions are untouched by the API hardening")


def test_development_bind_is_opt_in_only() -> None:
    assert not DEV_COMPOSE.name.startswith("docker-compose.override")
    for name in AUTO_LOADED_OVERRIDE_NAMES:
        assert not (ROOT / name).exists(), f"{name} would be auto-loaded by plain `docker compose`"
    dev = _load(DEV_COMPOSE)
    assert set(dev["services"]) == {"api"}, sorted(dev["services"])
    assert dev["services"]["api"]["volumes"] == ["./api:/app"]
    print("PASS: the live source bind exists only in the opt-in docker-compose.dev.yml")


def test_image_bakes_source_and_runs_non_root() -> None:
    instructions = _dockerfile_instructions()
    args = {}
    for keyword, argument in instructions:
        if keyword == "ARG" and "=" in argument:
            name, _, value = argument.partition("=")
            args[name.strip()] = value.strip()

    users = [argument for keyword, argument in instructions if keyword == "USER"]
    assert users, "the image must declare an explicit runtime user"
    resolved = users[-1]
    for name, value in args.items():
        resolved = resolved.replace("${" + name + "}", value)
    uid = resolved.split(":")[0]
    assert uid.isdigit() and int(uid) != 0, resolved
    assert resolved == "1000:1000", resolved

    copies = [argument for keyword, argument in instructions if keyword == "COPY"]
    assert ". /app" in copies, copies
    # Mode normalization: COPY preserves build-context modes, and four
    # eco_driving_explorer admin modules are 0600 in the checkout.
    runs = " ".join(argument for keyword, argument in instructions if keyword == "RUN")
    assert "chmod 0644" in runs and "chmod 0755" in runs, runs
    assert "chown -R root:root /app" in runs, runs

    cmd = [argument for keyword, argument in instructions if keyword == "CMD"]
    assert cmd and "uvicorn" in cmd[-1] and "--port" in cmd[-1] and "8000" in cmd[-1], cmd
    print("PASS: image bakes /app, normalizes modes and runs as an explicit non-root uid")


def _ignore_regex(pattern: str) -> re.Pattern[str]:
    """Approximate Docker's ignore matching: `**/` spans zero or more directories."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        elif pattern[index] == "[":
            close = pattern.index("]", index)
            out.append(pattern[index:close + 1])
            index = close + 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(out) + "$")


def _is_excluded(relative: str, regexes: list[re.Pattern[str]]) -> bool:
    parts = relative.split("/")
    prefixes = ["/".join(parts[:count]) for count in range(1, len(parts) + 1)]
    return any(regex.match(prefix) for regex in regexes for prefix in prefixes)


def test_build_context_excludes_cache_noise_only() -> None:
    patterns = [
        line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for pattern in patterns:
        assert pattern not in {"*", "**", "/", "."}, pattern
    regexes = [_ignore_regex(pattern) for pattern in patterns]

    # Matched against the real build context, not against literal pattern text:
    # a bare `__pycache__` would leave the nested caches in the image.
    api_dir = ROOT / "api"
    excluded_cache = 0
    for path in sorted(api_dir.rglob("*")):
        relative = path.relative_to(api_dir).as_posix()
        is_cache = "__pycache__" in relative.split("/") or relative.endswith(".pyc")
        if is_cache:
            assert _is_excluded(relative, regexes), relative
            excluded_cache += 1
        elif path.is_file() and path.suffix == ".py":
            assert not _is_excluded(relative, regexes), relative
    assert excluded_cache >= 3, excluded_cache

    for entry in REQUIRED_CONTEXT_ENTRIES:
        assert (api_dir / entry).exists(), entry
        assert not _is_excluded(entry, regexes), entry
    assert any(regex.match(".env") for regex in regexes), patterns
    print(f"PASS: .dockerignore excludes {excluded_cache} cache paths and no application module")


def test_production_api_requires_the_stable_session_secret() -> None:
    """The production API must be given a stable `ARTIFACT_EXPLORER_SESSION_SECRET`.

    The application falls back to a per-process secret when the variable is
    absent. That fallback is fine for development, but in production it silently
    invalidates every session AND — since S6 — every copied Database Explorer
    `row=` URL on each container recreation, because the opaque row references
    are derived from the same secret.

    The contract is therefore the `:?required` form the identity variables
    already use: Compose refuses to start the service rather than letting the
    operator receive an ephemeral key by accident.
    """
    api = _load(BASE_COMPOSE)["services"]["api"]
    environment = api.get("environment") or {}
    assert isinstance(environment, dict), type(environment)
    declared = environment.get("ARTIFACT_EXPLORER_SESSION_SECRET")
    assert declared is not None, (
        "the production API service must declare ARTIFACT_EXPLORER_SESSION_SECRET; "
        "without it the row-reference key is ephemeral"
    )
    declared = str(declared)
    # Interpolated from the deployment environment, and required rather than
    # defaulted: a `:-` default would reintroduce the silent fallback.
    assert declared.startswith("${ARTIFACT_EXPLORER_SESSION_SECRET"), declared
    assert ":?" in declared, f"must be required, not optional or defaulted: {declared}"
    assert ":-" not in declared, f"must not carry a default value: {declared}"
    print("PASS: production Compose requires an externally supplied session/row-reference secret")


def test_no_secret_value_is_committed_in_deployment_config() -> None:
    """The contract is a variable reference; the value stays outside the repo."""
    for path in (BASE_COMPOSE, DEV_COMPOSE):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "ARTIFACT_EXPLORER_SESSION_SECRET" not in line:
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Every occurrence must be an interpolation, never an inline literal.
            _key, _, value = stripped.partition(":")
            value = value.strip()
            assert value.startswith("${"), (path.name, stripped)
    # A tracked env template may name the variable, but only with an obvious
    # placeholder — never a usable value.
    import subprocess

    placeholders = ("CHANGE_ME", "<", "REPLACE", "TODO", "EXAMPLE", "xxx")
    for candidate in (ROOT / ".env", ROOT / ".env.example"):
        if not candidate.exists():
            continue
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", candidate.name],
            cwd=ROOT, capture_output=True, text=True,
        )
        if tracked.returncode != 0:
            continue
        for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip().startswith("ARTIFACT_EXPLORER_SESSION_SECRET="):
                continue
            value = line.split("=", 1)[1].strip()
            assert not value or any(mark in value.upper() for mark in (p.upper() for p in placeholders)), (
                f"a tracked env file must carry a placeholder, not a usable secret: {candidate.name}"
            )
    print("PASS: the secret is referenced from the environment and no value is committed")


def test_recorded_refresh_actions_use_the_production_compose_file() -> None:
    for source in REFRESH_ACTION_SOURCES:
        text = source.read_text(encoding="utf-8")
        actions = re.findall(r'"(docker compose[^"]*force-recreate api)"', text)
        assert actions, source
        for action in actions:
            assert "-f docker-compose.yml" in action, (source.name, action)
    print("PASS: recorded API refresh actions pin the production Compose file")


def main() -> None:
    test_production_compose_executes_image_code_as_non_root()
    test_production_compose_leaves_postgres_and_minio_alone()
    test_development_bind_is_opt_in_only()
    test_image_bakes_source_and_runs_non_root()
    test_build_context_excludes_cache_noise_only()
    test_recorded_refresh_actions_use_the_production_compose_file()
    test_production_api_requires_the_stable_session_secret()
    test_no_secret_value_is_committed_in_deployment_config()
    print("OK - Docker API execution boundary contract tests passed")


if __name__ == "__main__":
    main()
