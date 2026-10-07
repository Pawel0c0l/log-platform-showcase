"""Host client for the Driver Eco Dashboard secure-delivery publisher API.

This is a thin, faithful mapping of the transport contract that
`delivery/driver_eco_dashboard/` already owns:

    POST /api/publish           canonical snapshot octets + authenticated headers
    POST /api/publish/recover   explicit lost-bearer recovery
    POST /api/publish/delivery  INTENT / DELIVERED phase, naming its capability

It reinvents none of that contract. Its only jobs are to (a) speak the exact
protocol shape the Worker parses — one unambiguous value per control header,
the exact publisher media type, the raw canonical bytes as the whole body —
and (b) turn every response into an explicit outcome the host state machine can
act on, including the one outcome an HTTP client normally hides: **the request
may have been committed and the answer lost**.

`TransportOutcomeUnknown` is that case, and it is deliberately a different type
from an ordinary failure. A host that cannot tell the two apart is a host that
will eventually re-publish something the Worker already committed.

Nothing here logs, prints or returns the machine credential, and the raw
capability appears in exactly one place: the `capability` field of a
`PublishOutcome`, which the caller must persist before doing anything else.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol

# The period vocabulary is defined once, by the delivery contract that also
# constrains the ledger column and derives the operation id from it. This
# module states the value on the wire; it does not get to have an opinion
# about which values exist.
from jobs.ecodriving_dashboard.delivery_contract import PERIOD_TYPES

#: Mirror of `worker/lib/publication.js::PUBLICATION_RESULT`. Duplicated as
#: strings rather than imported (different language, different process); the
#: local end-to-end suite drives the real Worker so a divergence is caught.
PUBLISHED = "PUBLISHED"
ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
ALREADY_DELIVERED = "ALREADY_DELIVERED"
IN_PROGRESS = "IN_PROGRESS"
CONFLICT = "CONFLICT"
UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
RECOVERED = "RECOVERED"
NOT_RECOVERABLE = "NOT_RECOVERABLE"
RECORDED = "RECORDED"
ALREADY_RECORDED = "ALREADY_RECORDED"
CAPABILITY_SUPERSEDED = "CAPABILITY_SUPERSEDED"
OBJECT_INTEGRITY_FAILURE = "OBJECT_INTEGRITY_FAILURE"
OBJECT_UNREADABLE = "OBJECT_UNREADABLE"

#: Mirror of `PUBLICATION_NEXT_ACTION`.
PERSIST_BEARER = "PERSIST_BEARER"
RETRY_PUBLISH = "RETRY_PUBLISH"
USE_PERSISTED_BEARER_OR_RECOVER = "USE_PERSISTED_BEARER_OR_RECOVER"
OPEN_NEW_OPERATION = "OPEN_NEW_OPERATION"
INVESTIGATE_OBJECT_INTEGRITY = "INVESTIGATE_OBJECT_INTEGRITY"
RETRY_AFTER_STORAGE_RECOVERS = "RETRY_AFTER_STORAGE_RECOVERS"
NONE = "NONE"

PUBLISHER_MEDIA_TYPE = "application/json"
PUBLISHER_SCHEME = "Publisher"

#: THE product identity every publisher request from this host carries.
#:
#: Shape follows the repository's established "<component-slug>/<n>" contract
#: identity convention (`ops/release_schema_preflight.SCHEMA_REQUIREMENTS_VERSION`,
#: `ops/audit_telematics_cold_start.COLD_START_BUNDLE_VERSION`, ...), which is also
#: exactly a valid HTTP `product/version` token, so no version infrastructure had
#: to be invented for it.
#:
#: WHY IT IS EXPLICIT AND NOT urllib's DEFAULT. Measured against the deployed
#: workers.dev edge: `Python-urllib/3.12` receives an empty HTTP 403 BEFORE the
#: Worker executes, while this value, a curl token and an absent header all reach
#: Worker-level response vocabulary. Shipping the default therefore meant that
#: host publishing could not work at all, and would fail as an opaque 403 that
#: named no cause.
#:
#: WHAT IT MAY NOT CONTAIN, by construction rather than by review: it is a module
#: constant, so it carries no host identity, no client or driver identity, no
#: credential, and it does not vary per request, per client or per run.
PUBLISHER_USER_AGENT = "log-platform-eco-dashboard-publisher/1"

#: A definite, observed HTTP 403.
#:
#: `delivery/driver_eco_dashboard/worker/` answers 400, 401, 404, 409 and 503 and
#: never 403, so a 403 observed here was produced by the EDGE/provider boundary
#: in front of the publisher application — the request was refused before the
#: Worker ran and therefore before any publication state could exist.
#:
#: It is a `SecureDeliveryError`, never a `TransportOutcomeUnknown`: an HTTP
#: response was received, so nothing about the outcome is ambiguous and none of
#: the retry semantics reserved for transport ambiguity apply to it. It is kept
#: distinct from `PROTOCOL_ERROR` so an operator reading a refusal can tell
#: "something in front of the publisher refused us" from "the publisher spoke a
#: protocol we could not map".
PUBLISHER_EDGE_FORBIDDEN = "PUBLISHER_EDGE_FORBIDDEN"

DELIVERY_PHASE_INTENT = "INTENT"
DELIVERY_PHASE_DELIVERED = "DELIVERED"

#: THE HEADER THAT DECIDES A CAPABILITY'S LIFETIME.
#:
#: The Worker holds the authoritative mapping — weekly 10 days, monthly 60
#: (`delivery/driver_eco_dashboard/worker/lib/capability_ttl.js`) — and this
#: host holds no second copy of it: duplicating the numbers is how the two ends
#: quietly stop agreeing. What travels the boundary is the FACT, not the
#: policy: which reporting period this publication is for.
#:
#: The value comes from `eco_dashboard_delivery_operation.period_type`, which is
#: constrained to `weekly`/`monthly` by the table, is part of the logical
#: delivery identity, and is one of the fields hashed into the operation id
#: (`delivery_contract.derive_operation_id`). An operation published as weekly
#: therefore cannot be recovered as monthly without being a different operation
#: id — a different publication entirely. It is never taken from a URL, a
#: template, a subject line or anything else presentational.
PUBLICATION_PERIOD_HEADER = "X-Publication-Period"


class SecureDeliveryError(RuntimeError):
    """The request definitively did not take effect."""

    def __init__(self, code: str, message: str, status: Optional[int] = None) -> None:
        self.code = code
        self.status = status
        super().__init__(f"{code}: {message}")


class TransportOutcomeUnknown(RuntimeError):
    """The request may or may not have been committed remotely.

    A timeout, a dropped connection, a lost response. The distinction from
    `SecureDeliveryError` is the entire reason this class exists: only the
    publisher API can say what actually happened, and the host must ask it
    rather than assume.
    """

    def __init__(self, operation: str, detail: str = "") -> None:
        self.operation = operation
        super().__init__(f"TRANSPORT_OUTCOME_UNKNOWN: {operation} {detail}".strip())


class SecureDeliveryTransport(Protocol):
    """Minimal transport seam.

    Production is HTTPS to the deployed Worker. Local verification points the
    same client at the real Worker running on an in-memory D1/R2 double, so the
    code under test is the deployed protocol handling, not a Python imitation
    of it.
    """

    def post(self, path: str, headers: Mapping[str, str],
             body: Optional[bytes]) -> tuple[int, Mapping[str, Any]]:
        ...


@dataclass(frozen=True)
class PublishOutcome:
    status: str
    next_action: str
    http_status: int
    operation: Mapping[str, Any] = field(default_factory=dict)
    #: THE raw bearer. Present only on `PUBLISHED`/`RECOVERED`, exactly once,
    #: and never written to a log, a URL query string or a diagnostic column.
    capability: Optional[str] = None
    capability_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    bearer_available: bool = False
    bearer_recoverable: bool = False
    error: Optional[str] = None
    #: `/api/publish/recover` names why a recovery was refused
    #: (`ALREADY_DELIVERED`, `NO_GRANT_YET`, `SUPERSEDED`,
    #: `GRANT_NOT_ELIGIBLE`). Kept because the safe next action differs per
    #: reason and collapsing them would lose exactly that.
    reason: Optional[str] = None

    @property
    def bearer_generation(self) -> int:
        try:
            return int(self.operation.get("bearer_generation") or 0)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return 0

    @property
    def remote_state(self) -> Optional[str]:
        return (self.operation or {}).get("state")


def _instant(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


class SecureDeliveryClient:
    """Speaks the publisher protocol; decides nothing about delivery policy."""

    def __init__(self, transport: SecureDeliveryTransport, *,
                 publisher_token: str) -> None:
        if not publisher_token:
            raise SecureDeliveryError("PUBLISHER_CREDENTIAL_MISSING",
                                      "a publisher machine credential is required")
        self._transport = transport
        self._token = publisher_token

    @property
    def credential(self) -> str:
        """For secret scrubbing only. Never for logging or reporting."""
        return self._token

    def _headers(self, extra: Mapping[str, str]) -> dict:
        headers = {"Authorization": f"{PUBLISHER_SCHEME} {self._token}"}
        for name, value in extra.items():
            text = str(value)
            # Every one of these is a singleton protocol-control header on the
            # Worker side. A comma there is read as a duplicated field line and
            # answered 400, so a malformed value is refused here rather than
            # sent and blamed on the server.
            if "," in text or "\n" in text or "\r" in text:
                raise SecureDeliveryError("AMBIGUOUS_CONTROL_HEADER",
                                          f"header {name} is not a single value")
            headers[name] = text
        return headers

    def _post(self, path: str, headers: Mapping[str, str],
              body: Optional[bytes]) -> tuple[int, Mapping[str, Any]]:
        try:
            status, payload = self._transport.post(path, headers, body)
        except TransportOutcomeUnknown:
            raise
        except Exception as error:  # transport-level: the outcome is unknown
            raise TransportOutcomeUnknown(path, type(error).__name__) from None
        return status, (payload if isinstance(payload, Mapping) else {})

    # --- publish ---------------------------------------------------------------

    def _period_header(self, period_type: str) -> str:
        """Refuse an unstatable period BEFORE any request exists.

        The Worker fails closed on an unknown period type as well, and that is
        the enforcement; this is the earlier refusal that keeps a malformed
        publication from ever reaching the network. Neither end has a default:
        a capability whose lifetime nobody chose is exactly what the
        period-scoped policy replaced.
        """
        value = str(period_type or "").strip()
        if value not in PERIOD_TYPES:
            raise SecureDeliveryError(
                "INVALID_PERIOD_TYPE",
                "a publication must state a known reporting period type")
        return value

    def publish(self, *, operation_id: str, subject_ref: str,
                payload_digest: str, body: bytes,
                period_type: str) -> PublishOutcome:
        """Publish the EXACT canonical snapshot octets.

        `body` is the byte sequence produced by
        `serialize_publishable_snapshot`, sent unchanged and undecorated:
        publication metadata travels in authenticated headers precisely so the
        snapshot never becomes a nested JSON value whose exact byte range
        cannot be recovered.
        """
        if not isinstance(body, (bytes, bytearray)):
            raise SecureDeliveryError("PAYLOAD_NOT_BYTES",
                                      "the publish body must be canonical bytes")
        headers = self._headers({
            "Content-Type": PUBLISHER_MEDIA_TYPE,
            "X-Publication-Operation": operation_id,
            "X-Publication-Subject": subject_ref,
            "X-Publication-Payload-Digest": payload_digest,
            PUBLICATION_PERIOD_HEADER: self._period_header(period_type),
        })
        status, payload = self._post("/api/publish", headers, bytes(body))
        return self._publish_outcome(status, payload, route="publish")

    def recover(self, *, operation_id: str, period_type: str) -> PublishOutcome:
        """THE explicit lost-bearer recovery. Never a re-publish.

        Used when the Worker holds an authoritative grant this host cannot
        present. The Worker replaces the grant in one transaction, so the lost
        predecessor is dead the moment this succeeds.

        The period type is restated because a recovery MINTS a replacement
        grant and therefore writes a fresh expiry, which must come from the same
        period-aware policy the original publication used. The recovered
        capability still belongs to THIS operation's reporting period and still
        resolves to that period's snapshot; recovery never retargets a delivery
        at a newer report.
        """
        headers = self._headers({
            "X-Publication-Operation": operation_id,
            PUBLICATION_PERIOD_HEADER: self._period_header(period_type),
        })
        status, payload = self._post("/api/publish/recover", headers, None)
        return self._publish_outcome(status, payload, route="recover")

    def record_delivery(self, *, operation_id: str, phase: str,
                        capability_id: str) -> PublishOutcome:
        """Move the remote delivery phase, naming the bearer being delivered.

        The capability id is mandatory in the protocol: without it a delivery
        decided before a concurrent recovery could terminalise the operation on
        a grant the driver never received.
        """
        if phase not in (DELIVERY_PHASE_INTENT, DELIVERY_PHASE_DELIVERED):
            raise SecureDeliveryError("INVALID_PHASE", "phase must be INTENT or DELIVERED")
        headers = self._headers({
            "X-Publication-Operation": operation_id,
            "X-Publication-Phase": phase,
            "X-Publication-Capability": capability_id,
        })
        status, payload = self._post("/api/publish/delivery", headers, None)
        return self._publish_outcome(status, payload, route="delivery")

    # --- authorization-state housekeeping --------------------------------------

    def compact_authorization_state(self) -> Mapping[str, Any]:
        """Ask the Worker to retire dead authorization state. NOT A REVOCATION.

        Removes expired browser sessions from D1 and nothing else. It cannot
        shorten, revoke or re-point a live grant, and it deliberately does not
        delete expired capability rows: those hold no secret, already answer
        `EXPIRED`, and are what makes an old link report `LINK_EXPIRED` instead
        of looking like a link that never existed. See the route's own comment
        in `worker/index.js`.

        Bounded per call. `batch_full` in the response means another call would
        remove more; the operation is a pure function of the clock, so calling
        it again, twice at once, or not at all are all safe.
        """
        headers = self._headers({})
        status, payload = self._post("/api/publish/maintenance", headers, None)
        if status != 200:
            raise SecureDeliveryError(
                str(payload.get("error") or "MAINTENANCE_REFUSED"),
                "authorization-state compaction was refused", status)
        return payload

    # --- response mapping ------------------------------------------------------

    def _publish_outcome(self, status: int, payload: Mapping[str, Any],
                         *, route: str) -> PublishOutcome:
        error = payload.get("error")
        result = payload.get("status")

        if status in (200, 201) and result:
            return PublishOutcome(
                status=str(result),
                next_action=str(payload.get("next_action") or NONE),
                http_status=status,
                operation=dict(payload.get("operation") or {}),
                capability=payload.get("capability"),
                capability_id=payload.get("capability_id")
                or (payload.get("operation") or {}).get("capability_id"),
                expires_at=_instant(payload.get("expires_at")),
                bearer_available=bool(payload.get("bearer_available")),
                bearer_recoverable=bool(payload.get("bearer_recoverable")),
            )

        if status == 409:
            # Every 409 is a definite refusal that mutated nothing. They are
            # kept distinct because their safe next actions differ: a payload
            # conflict is a host bug, an integrity failure is an incident, and
            # a superseded capability just means re-read and deliver the
            # current one.
            return PublishOutcome(
                status=str(result or error or CONFLICT),
                next_action=str(payload.get("next_action") or NONE),
                http_status=status,
                operation=dict(payload.get("operation") or {}),
                error=str(error) if error else None,
                reason=str(payload["reason"]) if payload.get("reason") else None,
            )

        if status == 503 and (error == OBJECT_UNREADABLE or result == OBJECT_UNREADABLE):
            return PublishOutcome(
                status=OBJECT_UNREADABLE,
                next_action=str(payload.get("next_action") or RETRY_AFTER_STORAGE_RECOVERS),
                http_status=status,
                operation=dict(payload.get("operation") or {}),
                error=OBJECT_UNREADABLE,
            )

        if status == 404:
            # Either an unknown operation, or an unauthenticated caller — the
            # boundary answers both identically on purpose, so the host must
            # not read this as "the operation definitely does not exist".
            raise SecureDeliveryError(
                str(error or "NOT_FOUND"),
                f"{route} was refused; credential or operation unknown", status)

        if status == 403:
            # A DEFINITE refusal at the edge/provider boundary. See
            # `PUBLISHER_EDGE_FORBIDDEN`: this is deliberately raised as a
            # `SecureDeliveryError` and never as `TransportOutcomeUnknown`,
            # because a 403 IS an observed HTTP response. Treating it as an
            # ambiguity would hand it the "ask the publisher what happened, then
            # retry" semantics that exist only for outcomes nobody observed.
            #
            # The code is fixed rather than read out of the body: an edge refusal
            # usually carries no publisher body at all, and the old
            # `error or "PROTOCOL_ERROR"` mapping is exactly what made this
            # measured Cloudflare refusal indistinguishable from a protocol
            # defect. Anything the boundary did say travels in the message.
            declared = str(error or result or "").strip()
            raise SecureDeliveryError(
                PUBLISHER_EDGE_FORBIDDEN,
                f"{route} was forbidden at the edge or provider boundary "
                + (f"(response declared {declared!r})" if declared
                   else "(the response carried no publisher body)"),
                status)

        if status >= 500:
            # A server-side failure with no committed-state statement. The
            # operation may or may not have advanced, so this is the unknown
            # outcome, not a definite failure.
            raise TransportOutcomeUnknown(route, f"HTTP {status}")

        raise SecureDeliveryError(str(error or "PROTOCOL_ERROR"),
                                  f"{route} refused the request", status)


# --- production transport -------------------------------------------------------


#: The only host labels a plaintext publisher endpoint may name. Everything
#: else must be HTTPS. `ipaddress.is_loopback` decides the numeric forms — so
#: the whole of 127.0.0.0/8 and `::1` are accepted — and `localhost` is the one
#: name form, because a name is resolved by the host's resolver rather than by
#: this function and no other name can be assumed to point at the loopback.
LOOPBACK_HOST_NAMES = frozenset({"localhost"})

#: One DNS label: letters, digits and inner hyphens, 1-63 octets, and never a
#: leading or trailing hyphen. This is the LDH rule of RFC 1123 §2.1 written
#: out, and it is what makes `-`, `_`, `%zz` and an empty label refusals rather
#: than hostnames. Punycode (`xn--...`) is ordinary LDH and passes unchanged,
#: so an internationalised custom domain in its wire form is not overrejected.
_DNS_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _validated_host(parts) -> str:
    """The syntactically valid host this URL names, or a refusal.

    WHY THIS IS NOT "IS IT NON-EMPTY". `urlsplit` answers "which substring is
    the host", not "could this ever be a host". `https://-`, `https://_` and
    `https://%zz` all parse and all yield a non-empty hostname, so a non-empty
    check let a URL that can never name a destination reach the point where a
    request carrying the machine credential was constructed. What the transport
    then did with it — fail to resolve, or resolve to something a local
    resolver invented — is not a property this host controls.

    It resolves NOTHING. The question is decidable from syntax alone, and a DNS
    lookup here would be a network effect performed by the check whose purpose
    is to run before network effects. Three shapes are legitimate and the
    established parsers decide two of them:

    * an IPv6 literal — accepted only in its bracketed form and only if
      `ipaddress` parses it, which is what refuses `[zz::1]`;
    * an IPv4 literal — `ipaddress` again, which is what refuses
      `999.999.999.999` that no label rule would catch;
    * a DNS hostname — LDH labels, 253 octets, at least one label.

    Raises `SecureDeliveryError` before any header, client or request exists.
    """
    import ipaddress

    hostname = parts.hostname
    if not hostname:
        raise SecureDeliveryError("PUBLISHER_ENDPOINT_MALFORMED",
                                  "the publisher endpoint names no host")
    # `urlsplit` strips the brackets from an IPv6 literal, so the netloc is the
    # only place that still says which form was written.
    bracketed = "[" in parts.netloc or "]" in parts.netloc
    if bracketed or ":" in hostname:
        if not bracketed:
            raise SecureDeliveryError(
                "PUBLISHER_ENDPOINT_MALFORMED",
                "an IPv6 publisher endpoint must be written in brackets")
        if "%" in hostname:
            # A scope/zone id names an INTERFACE on this machine, not a
            # routable destination, and `ipaddress` parses it happily.
            raise SecureDeliveryError(
                "PUBLISHER_ENDPOINT_MALFORMED",
                "the publisher endpoint must not carry an IPv6 zone id")
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            raise SecureDeliveryError(
                "PUBLISHER_ENDPOINT_MALFORMED",
                "the publisher endpoint is not a valid IPv6 literal") from None
        return hostname

    # A single trailing dot is the explicit root label and is legitimate.
    host = hostname[:-1] if hostname.endswith(".") else hostname
    if not host or not host.isascii():
        raise SecureDeliveryError("PUBLISHER_ENDPOINT_MALFORMED",
                                  "the publisher endpoint host is not a valid hostname")
    if len(host) > 253:
        raise SecureDeliveryError("PUBLISHER_ENDPOINT_MALFORMED",
                                  "the publisher endpoint host is too long")

    labels = host.split(".")
    # An all-numeric final label means an IP address was INTENDED, so it is
    # judged as one rather than as a hostname that happens to look like it.
    if labels[-1].isdigit():
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise SecureDeliveryError(
                "PUBLISHER_ENDPOINT_MALFORMED",
                "the publisher endpoint is not a valid IPv4 literal") from None
        return host

    for label in labels:
        if not _DNS_LABEL.match(label):
            raise SecureDeliveryError(
                "PUBLISHER_ENDPOINT_MALFORMED",
                "the publisher endpoint host is not a valid hostname")
    return host


def validate_publisher_endpoint(base_url: str) -> str:
    """Prove the destination before a credential is ever attached to it.

    WHY THIS IS PARSED AND NOT MATCHED. The machine credential travels in an
    `Authorization` header on every request, so "is this destination allowed to
    receive plaintext" is a question about the URL's HOST, and only a URL
    parser knows which part of a string that is. A prefix test does not:
    `http://localhost.attacker.invalid` and `http://127.0.0.1.attacker.invalid`
    both start with an accepted prefix and both name somebody else's server.
    `http://user@127.0.0.1@evil.invalid/` is the same trick with userinfo.

    Returns the normalised base URL. Raises `SecureDeliveryError` — before any
    header exists and therefore before the credential could reach a transport,
    a mock or a log — for anything else.
    """
    import ipaddress
    from urllib.parse import urlsplit

    text = str(base_url or "").strip()
    if not text:
        raise SecureDeliveryError("PUBLISHER_ENDPOINT_MISSING",
                                  "no publisher endpoint is configured")
    if any(ch.isspace() for ch in text):
        raise SecureDeliveryError("PUBLISHER_ENDPOINT_MALFORMED",
                                  "the publisher endpoint contains whitespace")
    base = text.rstrip("/")
    try:
        parts = urlsplit(base)
    except ValueError:
        raise SecureDeliveryError("PUBLISHER_ENDPOINT_MALFORMED",
                                  "the publisher endpoint could not be parsed") from None

    if parts.scheme not in ("https", "http"):
        raise SecureDeliveryError("INSECURE_PUBLISHER_ENDPOINT",
                                  "the publisher endpoint must be HTTPS")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        # Userinfo is never needed here and is the classic way to make a URL
        # look like it names one host while naming another.
        raise SecureDeliveryError("PUBLISHER_ENDPOINT_MALFORMED",
                                  "the publisher endpoint must not carry userinfo")
    if parts.query or parts.fragment:
        raise SecureDeliveryError("PUBLISHER_ENDPOINT_MALFORMED",
                                  "the publisher endpoint must be a bare base URL")
    try:
        parts.hostname
        port = parts.port  # raises ValueError on a malformed port
    except ValueError:
        raise SecureDeliveryError("PUBLISHER_ENDPOINT_MALFORMED",
                                  "the publisher endpoint port is not a number") from None
    if port is not None and not 0 < port < 65536:  # pragma: no cover - urlsplit guards
        raise SecureDeliveryError("PUBLISHER_ENDPOINT_MALFORMED",
                                  "the publisher endpoint port is out of range")

    # BEFORE the scheme branches, because a malformed host is malformed under
    # HTTPS too and this is the last point at which no credential-bearing
    # request could yet have been constructed.
    host = _validated_host(parts).lower()

    if parts.scheme == "https":
        return base

    loopback = host in LOOPBACK_HOST_NAMES
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        # The machine credential is a bearer token: plaintext transport to
        # anything but an actual loopback destination would hand it to the
        # network.
        raise SecureDeliveryError("INSECURE_PUBLISHER_ENDPOINT",
                                  "the publisher endpoint must be HTTPS")
    return base


class HttpSecureDeliveryTransport:
    """`urllib`-based transport. No new dependency, no implicit retry.

    Retry policy belongs to the host state machine, which is the only layer
    that knows whether a retry is idempotent. A transport that retried on its
    own would be issuing publish attempts nobody decided to make.

    The destination is validated in the constructor — that is, before a
    `SecureDeliveryClient` exists to hold a credential, and therefore before
    any code path could attach one to a request aimed at it.
    """

    def __init__(self, base_url: str, *, timeout_seconds: float = 30.0) -> None:
        self._base = validate_publisher_endpoint(base_url)
        self._timeout = float(timeout_seconds)

    def post(self, path: str, headers: Mapping[str, str],
             body: Optional[bytes]) -> tuple[int, Mapping[str, Any]]:
        import urllib.error
        import urllib.request

        # `data=None` on a POST is deliberate for the bodyless routes:
        # `urllib` adds `Content-Type: application/x-www-form-urlencoded`
        # whenever data is present and no media type was set, and the
        # publisher routes parse a declared media type for EQUALITY — so an
        # empty body would be refused for a header the caller never chose.
        request = urllib.request.Request(self._base + path, data=body, method="POST")
        for name, value in headers.items():
            request.add_header(name, value)
        # LAST, and unconditionally: `urllib` only supplies its own
        # `Python-urllib/x.y` default when the request carries no `User-agent`,
        # so setting it here is what removes the default from every route this
        # transport has. Setting it last also means no caller-supplied header can
        # replace the product identity with something else.
        request.add_header("User-Agent", PUBLISHER_USER_AGENT)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                return response.status, _decode(raw)
        except urllib.error.HTTPError as error:  # a real answer, just not 2xx
            raw = error.read()
            return error.code, _decode(raw)


def _decode(raw: bytes) -> Mapping[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}
