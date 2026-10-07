#!/usr/bin/env python3
"""Focused tests for the loopback-only DSN guard.

Pure: no database, no live name resolution. Every resolution is performed by an
injected resolver so the expected answers are stated in the test rather than
inherited from whatever `/etc/hosts` happens to say on the machine running it.

Set no environment variable. This suite never connects to anything.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    UnsafeDsnError,
    parse_dsn_hosts,
    require_loopback_dsn,
)

FAILURES: list = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
    print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


def _resolver(mapping: dict):
    """A `getaddrinfo` stand-in returning exactly the declared addresses."""
    def resolve(host, _port, _family=0, _type=0):
        if host not in mapping:
            raise OSError(f"no such host: {host}")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))
            for address in mapping[host]
        ]
    return resolve


LOOPBACK_ONLY = _resolver({
    "localhost": ["127.0.0.1", "::1"],
    "db.example.com": ["203.0.113.10"],
    "internal.lan": ["192.168.1.50"],
    "split-horizon": ["127.0.0.1", "10.0.0.7"],
})


def _refuses(dsn, *, code: str, name: str, **kwargs) -> None:
    try:
        require_loopback_dsn(dsn, label="test dsn", resolver=LOOPBACK_ONLY,
                             env={}, **kwargs)
    except UnsafeDsnError as exc:
        _check(name, exc.code == code, f"got {exc.code}")
    else:
        _check(name, False, "accepted")


def _accepts(dsn, *, name: str, **kwargs) -> None:
    try:
        evidence = require_loopback_dsn(
            dsn, label="test dsn", resolver=LOOPBACK_ONLY, env={}, **kwargs
        )
    except UnsafeDsnError as exc:
        _check(name, False, f"refused {exc.code}")
    else:
        _check(name, bool(evidence.get("hosts")), f"{evidence}")


# ---------------------------------------------------------------------------
# Accepted shapes
# ---------------------------------------------------------------------------

print("\n-- accepted --")

_accepts(
    "postgresql://disposable:pw@127.0.0.1:55433/disposable",
    name="URI loopback DSN accepted",
)
_accepts(
    "postgresql://u:p@[::1]:5432/db",
    name="URI bracketed IPv6 loopback DSN accepted",
)
_accepts(
    "postgresql://u:p@localhost:5432/db",
    name="URI localhost resolving only to loopback accepted",
)
_accepts(
    "host=127.0.0.1 port=55433 dbname=disposable user=u password=p",
    name="keyword loopback DSN accepted",
)
_accepts(
    "host=localhost port=5432 dbname=db user=u",
    name="keyword localhost resolving only to loopback accepted",
)
_accepts(
    "host='127.0.0.1' port=5432 dbname=db password='has space'",
    name="keyword DSN with quoted values accepted",
)
_accepts(
    "host=/var/run/postgresql dbname=db",
    name="Unix-socket DSN accepted only when explicitly allowed",
    allow_unix_socket=True,
)
_accepts(
    "dbname=db user=u",
    name="host-less DSN accepted only when the socket fallback is allowed",
    allow_unix_socket=True,
)
_accepts(
    "host=127.0.0.1,127.0.0.1 port=5432,5433 dbname=db",
    name="multi-host DSN of only loopback members accepted",
)


# ---------------------------------------------------------------------------
# Refused shapes
# ---------------------------------------------------------------------------

print("\n-- refused --")

_refuses(
    "postgresql://u:p@203.0.113.10:5432/db",
    code="DSN_HOST_NOT_LOOPBACK",
    name="public host refused",
)
_refuses(
    "host=203.0.113.10 port=5432 dbname=db",
    code="DSN_HOST_NOT_LOOPBACK",
    name="keyword public host refused",
)
_refuses(
    "host=192.168.1.50 port=5432 dbname=db",
    code="DSN_HOST_NOT_LOOPBACK",
    name="RFC1918 host refused",
)
_refuses(
    "host=10.0.0.7 port=5432 dbname=db",
    code="DSN_HOST_NOT_LOOPBACK",
    name="RFC1918 10/8 host refused",
)
_refuses(
    "host=172.16.4.9 port=5432 dbname=db",
    code="DSN_HOST_NOT_LOOPBACK",
    name="RFC1918 172.16/12 host refused",
)
_refuses(
    "postgresql://u:p@db.example.com:5432/db",
    code="DSN_HOST_NOT_LOOPBACK",
    name="production hostname refused",
)
_refuses(
    "host=internal.lan dbname=db",
    code="DSN_HOST_NOT_LOOPBACK",
    name="LAN hostname refused",
)
_refuses(
    "host=split-horizon dbname=db",
    code="DSN_HOST_NOT_LOOPBACK",
    name="ambiguous host resolving to loopback AND a routable address refused",
)
_refuses(
    "host=127.0.0.1,203.0.113.10 port=5432,5432 dbname=db",
    code="DSN_HOST_NOT_LOOPBACK",
    name="mixed multi-host DSN refused",
)
_refuses(
    "postgresql://u:p@127.0.0.1,203.0.113.10/db",
    code="DSN_HOST_NOT_LOOPBACK",
    name="mixed multi-host URI DSN refused",
)
_refuses(
    "host=nowhere.invalid dbname=db",
    code="DSN_HOST_UNRESOLVABLE",
    name="unresolvable host refused rather than assumed local",
)
_refuses(
    "",
    code="DSN_MALFORMED",
    name="empty DSN refused",
)
_refuses(
    None,
    code="DSN_MALFORMED",
    name="non-string DSN refused",
)
_refuses(
    "this is not a dsn",
    code="DSN_MALFORMED",
    name="malformed DSN refused",
)
_refuses(
    "host",
    code="DSN_MALFORMED",
    name="keyword without a value refused",
)
_refuses(
    "dbname=db user=u",
    code="DSN_HOST_MISSING",
    name="missing host with an unsafe fallback refused",
)
_refuses(
    "host='' dbname=db",
    code="DSN_HOST_MISSING",
    name="explicitly empty host refused",
)
_refuses(
    # libpq skips whitespace after '=', so this reads the *next* pair as the
    # host value. The guard matches that reading rather than silently treating
    # it as "no host", and refuses what it cannot prove local.
    "host= dbname=db",
    code="DSN_HOST_UNRESOLVABLE",
    name="whitespace-separated empty host refused, matching libpq's reading",
)
_refuses(
    "postgresql:///db",
    code="DSN_HOST_MISSING",
    name="URI with no authority refused when the socket fallback is not allowed",
)
_refuses(
    "host=/var/run/postgresql dbname=db",
    code="DSN_UNIX_SOCKET_NOT_ALLOWED",
    name="Unix socket refused unless the suite explicitly allows it",
)


# ---------------------------------------------------------------------------
# PGHOST fallback
# ---------------------------------------------------------------------------

print("\n-- PGHOST fallback --")

try:
    require_loopback_dsn(
        "dbname=db user=u", label="test dsn", resolver=LOOPBACK_ONLY,
        env={"PGHOST": "db.example.com"}, allow_unix_socket=True,
    )
except UnsafeDsnError as exc:
    _check(
        "an inherited PGHOST pointing at a remote server refuses even when the "
        "socket fallback is allowed",
        exc.code == "DSN_HOST_NOT_LOOPBACK", exc.code,
    )
else:
    _check("an inherited remote PGHOST refuses", False, "accepted")

try:
    evidence = require_loopback_dsn(
        "dbname=db user=u", label="test dsn", resolver=LOOPBACK_ONLY,
        env={"PGHOST": "127.0.0.1"},
    )
except UnsafeDsnError as exc:
    _check("a loopback PGHOST is accepted", False, exc.code)
else:
    _check(
        "a loopback PGHOST is accepted",
        evidence["hosts"][0]["host"] == "127.0.0.1", f"{evidence}",
    )


# ---------------------------------------------------------------------------
# Host extraction is credential-free and never resolves
# ---------------------------------------------------------------------------

print("\n-- parsing --")

_check(
    "userinfo containing '@' does not confuse host extraction",
    parse_dsn_hosts("postgresql://u:p@ss@127.0.0.1:5432/db") == ["127.0.0.1"],
    f"{parse_dsn_hosts('postgresql://u:p@ss@127.0.0.1:5432/db')}",
)
_check(
    "a host= query parameter overrides the URI authority",
    parse_dsn_hosts(
        "postgresql://u:p@ignored:5432/db?host=127.0.0.1"
    ) == ["127.0.0.1"],
)
_check(
    "an unbracketed IPv6 literal is not split on its colons",
    parse_dsn_hosts("host=::1 dbname=db") == ["::1"],
)
_check(
    "no host key yields an empty list rather than a guess",
    parse_dsn_hosts("dbname=db user=u") == [],
)

try:
    require_loopback_dsn(
        "host=203.0.113.10 password=SUPERSECRET dbname=db",
        label="test dsn", resolver=LOOPBACK_ONLY, env={},
    )
except UnsafeDsnError as exc:
    _check(
        "a refusal never echoes the DSN password",
        "SUPERSECRET" not in str(exc), str(exc),
    )
else:
    _check("a refusal never echoes the DSN password", False, "accepted")


# ---------------------------------------------------------------------------

print("")
if FAILURES:
    print(f"FAIL — {len(FAILURES)} check(s) failed:")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("OK - loopback-only PostgreSQL DSN guard checks passed")
