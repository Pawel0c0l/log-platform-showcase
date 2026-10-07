"""Driver Eco Dashboard V1 — per-client dashboard MAILING rollout permission.

WHAT THIS DECIDES, AND WHAT IT REFUSES TO DECIDE

Exactly one question: **may this client's Eco Driving e-mails carry a dashboard
at all?**

It decides nothing about whether a particular run asked for one — that is the
`--with-dashboard` opt-in resolved by
`jobs.ecodriving_dashboard.eco_mailing_integration.DashboardLinkSettings` — and
nothing about whether the publisher is configured, reachable or healthy. The two
are deliberately independent, and dashboard-enabled sending requires BOTH:

    explicit --with-dashboard on the invocation
  AND
    explicit client-level rollout permission declared here

Neither alone is sufficient, and neither implies the other. In particular this
permission is NOT derived from `ECO_DASHBOARD_BASE_URL` /
`ECO_DASHBOARD_PUBLISHER_URL` being set: "the platform could publish" and "this
client's drivers may be mailed a dashboard" are different decisions with
different owners, and collapsing them would make provisioning a URL a silent
rollout.

WHY A DECLARATION FILE AND NOT A FEATURE-FLAG PLATFORM

The repository already states this kind of durable, reviewable contract as a
JSON declaration read by one module — `db/schema_requirements.json` for the
release schema gate, `ops/watchdog_expectations.json` for the watchdog. This
follows that convention and nothing more: enabling a client later is one
explicit edit to `ops/eco_dashboard_mailing_rollout.json`, reviewable as a
one-line diff, with no change to any mailing business logic.

FAIL-CLOSED IS THE WHOLE POINT

  * a client not named in the declaration is DISABLED. New and unknown clients
    therefore never inherit a rollout;
  * a wildcard entry is MALFORMED, not "everyone". "All clients enabled" must
    not be expressible, so it cannot become the default by accident, by a
    copy-paste, or by a merge;
  * a duplicate client entry is MALFORMED rather than last-one-wins, because a
    file that states a client twice does not state a decision;
  * an unknown key, a non-boolean `enabled`, a wrong contract identity or
    unparseable JSON is MALFORMED. No permissive coercion exists;
  * a source that cannot be read at all is UNREADABLE, kept distinct from
    MALFORMED so an operator knows whether to look at the filesystem or at the
    declaration.

Every one of those outcomes REFUSES. None of them ever produces "enabled".

WHEN IT IS CONSULTED

Only when a run has already opted in with `--with-dashboard`. A legacy run —
the default — never reads this file, never requires it to exist and cannot be
affected by its contents.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Contract identity, in the repository's established "<slug>/<n>" form. A
#: declaration that does not name it is refused rather than guessed at.
ROLLOUT_CONTRACT = "eco-dashboard-mailing-rollout/1"

#: THE production declaration. One file, in the repository, under review.
DEFAULT_ROLLOUT_PATH = REPO_ROOT / "ops" / "eco_dashboard_mailing_rollout.json"

#: Test/staging seam. Points the gate at an alternative declaration so the
#: enabled path can be exercised deterministically without touching the
#: production file. It cannot widen anything: the alternative declaration is
#: parsed by exactly the same fail-closed rules, so it still has to name every
#: client it enables and still cannot express a wildcard.
ENV_ROLLOUT_FILE = "ECO_DASHBOARD_MAILING_ROLLOUT_FILE"

_ALLOWED_TOP_LEVEL_KEYS = frozenset({"$comment", "contract", "clients"})
_ALLOWED_CLIENT_KEYS = frozenset({"client_code", "enabled", "note"})

#: Shapes that would mean "everything". Refused as malformed declarations.
_WILDCARD_CLIENT_CODES = frozenset({"*", "ALL", "ANY", "DEFAULT", "%"})

UNREADABLE = "ECO_DASHBOARD_MAILING_ROLLOUT_UNREADABLE"
MALFORMED = "ECO_DASHBOARD_MAILING_ROLLOUT_MALFORMED"
NOT_ENABLED = "ECO_DASHBOARD_MAILING_ROLLOUT_NOT_ENABLED"


class DashboardRolloutDeclarationError(RuntimeError):
    """The rollout declaration could not be read or could not be trusted."""

    def __init__(self, code: str, message: str, source: str = "") -> None:
        self.code = code
        self.source = source
        super().__init__(f"{code}: {message}")


class DashboardMailingNotEnabled(RuntimeError):
    """This client's dashboard mailing rollout is not enabled.

    Deliberately its own type. It is not a configuration defect, not a transport
    failure and not a per-driver product state: it is an owner decision that has
    not been taken yet, and an operator reading it should understand exactly
    that. It names the client code and nothing else — no credential, no
    endpoint, no capability, no publication detail.
    """

    def __init__(self, client_code: str, source: str) -> None:
        self.code = NOT_ENABLED
        self.client_code = client_code
        self.source = source
        super().__init__(
            f"{NOT_ENABLED}: dashboard mailing rollout is not enabled for client "
            f"{client_code or '(none)'}; it is declared in {source}. Run without "
            f"--with-dashboard to send the ordinary Eco e-mail."
        )


def normalize_client_code(client_code: Any) -> str:
    return str(client_code or "").strip().upper()


@dataclass(frozen=True)
class DashboardMailingRollout:
    """The parsed declaration. Immutable, and answers exactly one question."""

    source: str
    enabled_clients: frozenset
    declared_clients: frozenset

    def is_enabled(self, client_code: Any) -> bool:
        code = normalize_client_code(client_code)
        if not code:
            # An unnamed client is not a client. Refusing here is what keeps a
            # blank/missing client_code from reaching a permission decision.
            return False
        return code in self.enabled_clients

    def require(self, client_code: Any) -> str:
        """Return the normalised client code, or refuse.

        Raises `DashboardMailingNotEnabled`. It raises BEFORE the caller can
        reach a publication, a capability, a send-log reservation or SMTP,
        because the caller invokes it before constructing any of them.
        """
        code = normalize_client_code(client_code)
        if not self.is_enabled(code):
            raise DashboardMailingNotEnabled(code, self.source)
        return code

    def summary(self) -> dict:
        """Non-secret description of the decision basis, safe for a run summary."""
        return {
            "dashboard_rollout_source": self.source,
            "dashboard_rollout_declared_clients": sorted(self.declared_clients),
            "dashboard_rollout_enabled_clients": sorted(self.enabled_clients),
        }


def _malformed(message: str, source: str) -> DashboardRolloutDeclarationError:
    return DashboardRolloutDeclarationError(MALFORMED, message, source)


def _parse_clients(raw: Any, source: str) -> tuple[frozenset, frozenset]:
    if not isinstance(raw, list):
        raise _malformed("`clients` must be a list", source)
    declared: set = set()
    enabled: set = set()
    for index, entry in enumerate(raw):
        where = f"clients[{index}]"
        if not isinstance(entry, dict):
            raise _malformed(f"{where} must be an object", source)
        unknown = sorted(set(entry) - _ALLOWED_CLIENT_KEYS)
        if unknown:
            raise _malformed(f"{where} has unknown keys: {', '.join(unknown)}", source)
        if "client_code" not in entry or "enabled" not in entry:
            raise _malformed(
                f"{where} must declare both client_code and enabled", source)
        raw_code = entry["client_code"]
        if not isinstance(raw_code, str) or not raw_code.strip():
            raise _malformed(f"{where}.client_code must be a non-empty string", source)
        code = normalize_client_code(raw_code)
        if code in _WILDCARD_CLIENT_CODES:
            raise _malformed(
                f"{where}.client_code {raw_code!r} is a wildcard; every client "
                "must be named explicitly", source)
        if code in declared:
            raise _malformed(f"{where} declares {code} a second time", source)
        flag = entry["enabled"]
        # `isinstance(True, int)` is True in Python, so the bool test must come
        # first and the int test must be an explicit refusal: `"enabled": 1` is
        # a declaration nobody wrote deliberately.
        if not isinstance(flag, bool):
            raise _malformed(f"{where}.enabled must be true or false", source)
        note = entry.get("note")
        if note is not None and not isinstance(note, str):
            raise _malformed(f"{where}.note must be a string", source)
        declared.add(code)
        if flag:
            enabled.add(code)
    return frozenset(enabled), frozenset(declared)


def parse_rollout(document: Any, *, source: str) -> DashboardMailingRollout:
    """Parse a already-decoded declaration. Fail-closed, no coercion."""
    if not isinstance(document, dict):
        raise _malformed("the declaration must be a JSON object", source)
    unknown = sorted(set(document) - _ALLOWED_TOP_LEVEL_KEYS)
    if unknown:
        raise _malformed(f"unknown top-level keys: {', '.join(unknown)}", source)
    if document.get("contract") != ROLLOUT_CONTRACT:
        raise _malformed(
            f"contract must be {ROLLOUT_CONTRACT!r}, got "
            f"{document.get('contract')!r}", source)
    if "clients" not in document:
        raise _malformed("the declaration must carry a `clients` list", source)
    enabled, declared = _parse_clients(document["clients"], source)
    return DashboardMailingRollout(
        source=source, enabled_clients=enabled, declared_clients=declared)


def rollout_path(path: Any = None) -> Path:
    """Which declaration this process reads. Explicit argument wins, then env."""
    if path:
        return Path(path)
    override = str(os.getenv(ENV_ROLLOUT_FILE) or "").strip()
    if override:
        return Path(override)
    return DEFAULT_ROLLOUT_PATH


def load_rollout(path: Any = None) -> DashboardMailingRollout:
    """Read and parse the declaration, or refuse.

    Never returns a permissive default. A missing, unreadable, invalid or
    untrusted declaration is a refusal, so a rollout can only ever be widened by
    a declaration that actually says so.
    """
    resolved = rollout_path(path)
    source = str(resolved)
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as error:
        raise DashboardRolloutDeclarationError(
            UNREADABLE,
            f"the dashboard mailing rollout declaration could not be read: "
            f"{type(error).__name__}",
            source) from None
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Readable but not valid JSON is a DECLARATION defect, not a filesystem
        # one. Keeping the two apart tells an operator where to look.
        raise _malformed("the declaration is not valid JSON", source) from None
    return parse_rollout(document, source=source)


def require_dashboard_mailing_enabled(
    client_code: Any, *,
    rollout: Optional[DashboardMailingRollout] = None,
    path: Any = None,
) -> str:
    """THE gate. Returns the normalised client code, or raises.

    `rollout` is the injection seam used by deterministic tests; production
    passes nothing and reads the declaration.
    """
    active = rollout if rollout is not None else load_rollout(path)
    return active.require(client_code)


def enabled_client_codes(
    *, rollout: Optional[DashboardMailingRollout] = None, path: Any = None
) -> Iterable[str]:
    active = rollout if rollout is not None else load_rollout(path)
    return sorted(active.enabled_clients)


def write_rollout_declaration(
    destination: Any, clients: Mapping[str, bool], *, note: str = ""
) -> Path:
    """Write a declaration. For TESTS and staging seams, never for production.

    Production's declaration is an edited, reviewed repository file; this exists
    so a suite can prove the enabled path without ever mutating it.
    """
    target = Path(destination)
    document = {
        "contract": ROLLOUT_CONTRACT,
        "clients": [
            {"client_code": normalize_client_code(code), "enabled": bool(flag),
             **({"note": note} if note else {})}
            for code, flag in clients.items()
        ],
    }
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return target
