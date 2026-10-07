#!/usr/bin/env python3
"""Loopback-only DSN validation for destructive PostgreSQL test suites.

WHY THIS EXISTS.
    Several manual suites in this directory are genuinely destructive: they
    `DROP SCHEMA ... CASCADE`, drop and recreate tables, apply migrations and
    write rows. They are correct against a disposable local instance and
    catastrophic against anything else, and the only thing standing between the
    two is the DSN an operator happened to export.

    A per-suite ad-hoc check is not enough, because the suites disagreed about
    what "local" means: some installed a socket-level guard, some checked
    nothing, and none of them looked at the DSN *before* opening a connection.
    This module is the one place that decides, and it decides before a
    connection is attempted.

WHAT IT ACCEPTS.
    Only a DSN whose host resolves **exclusively** to loopback:

      * the literals `127.0.0.1` (and the rest of `127.0.0.0/8`) and `::1`;
      * a name such as `localhost` **only** when every address it resolves to is
        loopback — a name with one loopback and one routable address is
        ambiguous and refused, not accepted on the strength of the good one;
      * a Unix-domain socket path, and only when the caller passes
        `allow_unix_socket=True`, because a socket path is a local-instance
        decision the suite must make deliberately.

WHAT IT REFUSES.
    Public addresses, RFC1918/LAN addresses, link-local and CGNAT ranges, any
    hostname that resolves to any of them, a multi-host DSN with a single
    non-loopback member, a malformed DSN, and a DSN with no host at all when the
    absent host could still fall back to a configured remote server through
    `PGHOST`.

WHAT IT IS NOT.
    Not a security boundary. An operator who controls the environment can point
    a loopback port at anything, and a local instance can be a production one.
    It is a fail-closed guard against the realistic accident — a copied
    production DSN, an inherited `PGHOST`, a forgotten export — and it is
    deliberately strict enough that the accident cannot pass quietly.

TEST-ONLY.
    Nothing in `jobs/`, `api/` or `scripts/` imports this module. It exists for
    `ops/tests_manual/` and the hardening campaign runner.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

#: URI schemes libpq accepts.
URI_SCHEMES = ("postgresql://", "postgres://")

#: Environment variable libpq falls back to when a DSN carries no host.
PGHOST_ENV = "PGHOST"


class UnsafeDsnError(RuntimeError):
    """A DSN a destructive suite must not connect to. Never advisory."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str) -> UnsafeDsnError:
    return UnsafeDsnError(code, message)


# ---------------------------------------------------------------------------
# Host extraction
# ---------------------------------------------------------------------------

def _is_socket_path(host: str) -> bool:
    """A Unix-domain socket directory or abstract socket name."""
    return host.startswith("/") or host.startswith("@")


def _split_keyword_dsn(dsn: str) -> Dict[str, str]:
    """Parse a libpq keyword/value DSN, honoring quotes and backslash escapes.

    Hand-rolled rather than delegated so this module has no dependency on
    `psycopg` being importable — the guard must be usable to refuse a DSN even
    in an environment where the driver is missing.
    """
    items: Dict[str, str] = {}
    index = 0
    length = len(dsn)
    while index < length:
        while index < length and dsn[index].isspace():
            index += 1
        if index >= length:
            break
        start = index
        while index < length and dsn[index] not in "= \t":
            index += 1
        key = dsn[start:index]
        while index < length and dsn[index].isspace():
            index += 1
        if index >= length or dsn[index] != "=":
            raise _refuse(
                "DSN_MALFORMED",
                f"keyword {key!r} in the DSN has no '=' value",
            )
        index += 1
        while index < length and dsn[index].isspace():
            index += 1
        value_chars: List[str] = []
        if index < length and dsn[index] == "'":
            index += 1
            while index < length and dsn[index] != "'":
                if dsn[index] == "\\" and index + 1 < length:
                    index += 1
                value_chars.append(dsn[index])
                index += 1
            if index >= length:
                raise _refuse("DSN_MALFORMED", "unterminated quoted DSN value")
            index += 1
        else:
            while index < length and not dsn[index].isspace():
                if dsn[index] == "\\" and index + 1 < length:
                    index += 1
                value_chars.append(dsn[index])
                index += 1
        if not key:
            raise _refuse("DSN_MALFORMED", "empty keyword in the DSN")
        items[key] = "".join(value_chars)
    return items


def _hosts_from_uri(dsn: str) -> List[str]:
    parsed = urlsplit(dsn)
    if not parsed.scheme:
        raise _refuse("DSN_MALFORMED", "the URI DSN carries no scheme")
    # A `host=` query parameter overrides the authority, as libpq does.
    query_hosts = parse_qs(parsed.query).get("host")
    if query_hosts:
        return [h for raw in query_hosts for h in str(raw).split(",")]

    netloc = parsed.netloc
    # Strip userinfo; a password may legitimately contain '@'.
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    if not netloc:
        return []
    hosts: List[str] = []
    for piece in netloc.split(","):
        piece = piece.strip()
        if not piece:
            hosts.append("")
            continue
        if piece.startswith("["):
            closing = piece.find("]")
            if closing < 0:
                raise _refuse(
                    "DSN_MALFORMED",
                    "unterminated bracketed IPv6 host in the URI DSN",
                )
            hosts.append(piece[1:closing])
            continue
        # A bare IPv6 literal contains several colons; only a single trailing
        # colon separates a port.
        if piece.count(":") > 1:
            hosts.append(piece)
            continue
        hosts.append(piece.split(":", 1)[0])
    return [unquote(h) for h in hosts]


def parse_dsn_hosts(dsn: object) -> List[str]:
    """Every host a DSN would connect to, in order. Never resolves anything.

    Returns `[]` when the DSN names no host at all — a distinct condition from
    naming an unsafe one, and classified separately by the caller.
    """
    if not isinstance(dsn, str) or not dsn.strip():
        raise _refuse("DSN_MALFORMED", "the DSN is empty")
    text = dsn.strip()
    if text.lower().startswith(URI_SCHEMES):
        return [h for h in _hosts_from_uri(text)]
    if "=" not in text:
        raise _refuse(
            "DSN_MALFORMED",
            "the DSN is neither a postgresql:// URI nor keyword/value pairs",
        )
    items = _split_keyword_dsn(text)
    raw = items.get("host")
    if raw is None:
        return []
    return [piece for piece in raw.split(",")]


# ---------------------------------------------------------------------------
# Loopback classification
# ---------------------------------------------------------------------------

def _addresses_for(
    host: str, resolver: Callable[..., Sequence]
) -> List[str]:
    try:
        infos = resolver(host, None, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise _refuse(
            "DSN_HOST_UNRESOLVABLE",
            f"host {host!r} could not be resolved, so it cannot be proven "
            "loopback",
        ) from exc
    addresses: List[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        addresses.append(str(sockaddr[0]))
    if not addresses:
        raise _refuse(
            "DSN_HOST_UNRESOLVABLE",
            f"host {host!r} resolved to no address at all",
        )
    return addresses


def _require_loopback_host(
    host: str, *, allow_unix_socket: bool, resolver: Callable[..., Sequence],
) -> Dict[str, object]:
    if _is_socket_path(host):
        if not allow_unix_socket:
            raise _refuse(
                "DSN_UNIX_SOCKET_NOT_ALLOWED",
                f"host {host!r} is a Unix-domain socket; this suite accepts one "
                "only when it explicitly opts in, because a socket path is a "
                "local-instance decision and not a proven disposable target",
            )
        return {"host": host, "kind": "unix_socket", "addresses": []}

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if not literal.is_loopback:
            raise _refuse(
                "DSN_HOST_NOT_LOOPBACK",
                f"host {host!r} is not a loopback address; a destructive suite "
                "runs only against a disposable local instance",
            )
        return {"host": host, "kind": "literal", "addresses": [str(literal)]}

    addresses = _addresses_for(host, resolver)
    non_loopback = [
        address for address in addresses
        if not ipaddress.ip_address(address).is_loopback
    ]
    if non_loopback:
        raise _refuse(
            "DSN_HOST_NOT_LOOPBACK",
            f"host {host!r} resolves to {sorted(set(addresses))}, which "
            f"includes the non-loopback address(es) {sorted(set(non_loopback))};"
            " a name is accepted only when every address it resolves to is "
            "loopback",
        )
    return {"host": host, "kind": "resolved", "addresses": addresses}


def require_loopback_dsn(
    dsn: object,
    *,
    label: str,
    allow_unix_socket: bool = False,
    resolver: Optional[Callable[..., Sequence]] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, object]:
    """Prove a DSN targets only loopback, or raise `UnsafeDsnError`.

    Call this **before** opening the connection and before issuing any
    statement. It performs no connection of its own; the only network operation
    it may do is name resolution, and a name that cannot be resolved is refused
    rather than assumed local.

    Returns bounded evidence — hosts and the addresses they resolved to. No
    credential from the DSN is ever returned, logged or included in an error.
    """
    resolve = socket.getaddrinfo if resolver is None else resolver
    environment = os.environ if env is None else env

    hosts = parse_dsn_hosts(dsn)

    if not hosts:
        # libpq falls back to PGHOST, and only then to the default socket
        # directory. An inherited PGHOST pointing at a real server is exactly
        # the accident this refuses.
        fallback = str(environment.get(PGHOST_ENV) or "").strip()
        if fallback:
            hosts = [piece for piece in fallback.split(",")]
        elif allow_unix_socket:
            return {
                "label": label,
                "hosts": [{"host": "(default unix socket)",
                           "kind": "unix_socket", "addresses": []}],
            }
        else:
            raise _refuse(
                "DSN_HOST_MISSING",
                f"{label} names no host and {PGHOST_ENV} is unset; the "
                "connection would fall back to a default that this suite has "
                "not proven local",
            )

    evidence: List[Dict[str, object]] = []
    for host in hosts:
        if not str(host).strip():
            raise _refuse(
                "DSN_HOST_MISSING",
                f"{label} contains an empty host entry; an empty host could "
                "still fall back to a configured remote server",
            )
        evidence.append(
            _require_loopback_host(
                str(host).strip(),
                allow_unix_socket=allow_unix_socket,
                resolver=resolve,
            )
        )
    return {"label": label, "hosts": evidence}


def require_loopback_dsn_or_exit(
    dsn: object, *, label: str, allow_unix_socket: bool = False,
) -> Dict[str, object]:
    """`require_loopback_dsn`, reported as a suite-level refusal.

    Used at the top of a destructive suite's `main()`: it prints one bounded
    line naming the refusal code — never the DSN, which carries a password —
    and exits non-zero without connecting.
    """
    import sys

    try:
        return require_loopback_dsn(
            dsn, label=label, allow_unix_socket=allow_unix_socket,
        )
    except UnsafeDsnError as exc:
        print(
            f"REFUSED: {label} is not a loopback-only PostgreSQL DSN "
            f"({exc.code}). This suite is destructive and runs only against a "
            "disposable local instance."
        )
        sys.exit(2)
