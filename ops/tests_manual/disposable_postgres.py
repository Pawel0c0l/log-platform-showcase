#!/usr/bin/env python3
"""A task-owned, disposable PostgreSQL 16 instance for destructive suites.

WHY THIS EXISTS.
    A destructive migration suite has two ways to get a database. It can accept
    a DSN from the environment and reset it, which is what the S13 suite did —
    and which means the suite drops `public` in whatever database the operator
    happened to export, with `postgres_dsn_safety` proving only that the host is
    loopback, not that the database is disposable. Or it can CREATE the instance
    it is about to destroy, which makes "disposable" a property of the thing
    rather than a promise about the environment.

    This module does the second. It starts a `postgres:16` container bound to a
    free loopback port, waits for it to accept connections, verifies the server
    is actually major version 16, creates a database of its own inside it, and
    removes the container again — in a `finally`, so an assertion failure, an
    exception or a KeyboardInterrupt all clean up.

WHAT IT NEVER DOES.
    It never connects to an instance it did not start, never accepts a DSN from
    the environment, never touches a shared or project container, and never
    drops or resets a database it did not create. There is therefore no
    environment variable that can point it at production.

    The container is bound to `127.0.0.1` on an ephemeral port, runs with
    `--rm`, and is named with a per-run token so two concurrent runs — this
    repository routinely has several agents working at once — cannot collide or
    remove each other's instance.

REQUIREMENTS.
    A working `docker` CLI and a locally available `postgres:16` image. Neither
    is installed by this module: when either is missing the caller is told so
    and reports the suite as not available, exactly as an absent DSN used to.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import time
import uuid

IMAGE = "postgres:16"
SUPERUSER = "postgres"
PASSWORD = "disposable"
READY_TIMEOUT_SECONDS = 90


class DisposablePostgresUnavailable(RuntimeError):
    """The environment cannot provide a disposable instance. Not a failure."""


def _docker(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=timeout
    )


def _require_docker() -> None:
    if shutil.which("docker") is None:
        raise DisposablePostgresUnavailable("the docker CLI is not on PATH")
    try:
        _docker("info", timeout=30)
    except subprocess.CalledProcessError as exc:
        raise DisposablePostgresUnavailable("the docker daemon is not reachable") from exc
    except subprocess.TimeoutExpired as exc:
        raise DisposablePostgresUnavailable("the docker daemon did not answer") from exc
    found = _docker("image", "inspect", IMAGE, check=False, timeout=60)
    if found.returncode != 0:
        raise DisposablePostgresUnavailable(
            f"the {IMAGE} image is not available locally; this module never pulls it"
        )


def _free_loopback_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(container: str, port: int) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        probe = _docker(
            "exec", container, "pg_isready", "-U", SUPERUSER, "-h", "127.0.0.1",
            check=False, timeout=30,
        )
        if probe.returncode == 0:
            # `pg_isready` inside the container can succeed before the published
            # port forwards, so the loopback socket is proved too.
            with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
                sock.settimeout(2)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    return
        last = (probe.stdout or probe.stderr or "").strip()
        time.sleep(0.5)
    raise DisposablePostgresUnavailable(
        f"the disposable instance did not become ready within {READY_TIMEOUT_SECONDS}s: {last}"
    )


def admin_dsn(port: int) -> str:
    return f"postgresql://{SUPERUSER}:{PASSWORD}@127.0.0.1:{port}/postgres"


def database_dsn(port: int, database: str) -> str:
    return f"postgresql://{SUPERUSER}:{PASSWORD}@127.0.0.1:{port}/{database}"


@contextlib.contextmanager
def disposable_postgres(*, label: str = "s13"):
    """Yield `(dsn, info)` for a fresh PostgreSQL 16 the caller alone owns.

    `info` carries the container name, the port and the reported server version,
    so a suite can state its evidence rather than assert it in the abstract.
    The container is removed in `finally` on every exit path.
    """
    _require_docker()
    token = uuid.uuid4().hex[:12]
    container = f"logplatform-{label}-disposable-{token}"
    database = f"{label}_disposable_{token}"
    port = _free_loopback_port()
    started = False
    try:
        _docker(
            "run", "--rm", "--detach",
            "--name", container,
            # Loopback only. The instance is never reachable off this host.
            "--publish", f"127.0.0.1:{port}:5432",
            "--env", f"POSTGRES_PASSWORD={PASSWORD}",
            "--env", f"POSTGRES_USER={SUPERUSER}",
            "--env", "POSTGRES_DB=postgres",
            # Nothing durable: the data directory is a tmpfs that disappears
            # with the container, so there is no volume to forget to remove.
            "--tmpfs", "/var/lib/postgresql/data:rw",
            IMAGE,
            "-c", "fsync=off", "-c", "full_page_writes=off",
            timeout=180,
        )
        started = True
        _wait_until_ready(container, port)

        import psycopg  # imported here so the module is usable without the driver

        with psycopg.connect(admin_dsn(port), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW server_version_num")
                version_num = int(str(cur.fetchone()[0]))
                cur.execute("SHOW server_version")
                version = str(cur.fetchone()[0])
                major = version_num // 10000
                if major != 16:
                    raise DisposablePostgresUnavailable(
                        f"the disposable instance reports PostgreSQL {major}, not 16 ({version})"
                    )
                # A database of this run's own, so every destructive statement
                # the suite issues lands somewhere nothing else can reach.
                cur.execute(f'CREATE DATABASE "{database}"')
        yield database_dsn(port, database), {
            "container": container,
            "port": port,
            "database": database,
            "server_version": version,
            "server_version_num": version_num,
        }
    finally:
        if started:
            # `--rm` removes the container on stop; `rm --force` is the belt to
            # that brace and is deliberately not allowed to raise, so a cleanup
            # problem can never mask the suite's own result.
            _docker("stop", "--time", "5", container, check=False, timeout=90)
            _docker("rm", "--force", container, check=False, timeout=60)


def main() -> None:
    """Prove the provisioner itself: start, verify, create, and clean up."""
    try:
        with disposable_postgres(label="selftest") as (dsn, info):
            assert "127.0.0.1" in dsn, dsn
            assert info["server_version_num"] // 10000 == 16, info
            container = info["container"]
            print(f"PASS: disposable PostgreSQL {info['server_version']} on 127.0.0.1:{info['port']}")
    except DisposablePostgresUnavailable as exc:
        print(f"DISPOSABLE_POSTGRES_NOT_AVAILABLE: {exc}")
        return
    remaining = _docker("ps", "--all", "--filter", f"name={container}", "--quiet", check=False)
    assert not (remaining.stdout or "").strip(), remaining.stdout
    print("PASS: the disposable instance is removed on exit")
    print("\nDISPOSABLE_POSTGRES_PROVISIONER_PASS")


if __name__ == "__main__":
    main()
