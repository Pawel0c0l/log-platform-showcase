#!/usr/bin/env python3
"""Request-framing contract for `POST /api/session` (Driver Eco Dashboard V1).

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_session_framing.py

WHAT THIS SUITE REPLACES

The endpoint's memory bound used to be gated on obtaining a BYOB reader for the
incoming `Request.body`, because only a fixed-size read into a buffer the Worker
allocates bounds per-read cost independently of the peer's chunking. Deployed
verification on the real Cloudflare runtime settled that question negatively and
definitively: hundreds of observations showed `body_read_mode = "default"`, so
`getReader({ mode: "byob" })` never succeeds there. A release gate the platform
cannot satisfy is not a safety property.

The bound is therefore taken from HTTP framing, which the runtime does expose:

    POST /api/session must present exactly one canonical Content-Length no
    larger than 512 bytes BEFORE the Worker reads the body; the bytes actually
    read are then independently bounded by min(declared, 512) and checked
    against the declaration in both directions.

WHAT IS ASSERTED HERE

  1. the accepted/rejected declared-length grammar, one probe per form;
  2. that a rejected form is refused BEFORE the reader, with no D1 statement,
     no session and no Set-Cookie — proved with a body stream that blocks
     forever if it is read;
  3. that the rate limiter still decides FIRST, above the framing gate;
  4. actual-versus-declared enforcement in both directions;
  5. that BOTH reader modes satisfy the same invariant, so neither is a gate;
  6. that the real frontend bootstrap request still succeeds;
  7. that no framing diagnostic names a body, a capability or a client address.

Drives ops/tests_manual/eco_session_framing_harness.mjs, which executes the real
Cloudflare Worker against in-memory D1/R2/ASSETS/rate-limit bindings. No
wrangler, no credentials, no remote Cloudflare resource, no deployment, no
production data.

Synthetic data only. No capability, session id or client address is printed by
this suite or by the harness it drives.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DELIVERY = REPO_ROOT / "delivery" / "driver_eco_dashboard"
HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_session_framing_harness.mjs"

MAX_SESSION_REQUEST_BYTES = 512

# A mode may be named only once a reader has actually been created.
READER_MODES = {"byob", "default", "buffered"}

# Every form the gate must refuse, with the internal diagnostic it must report.
# The EXTERNAL answer is deliberately identical for all of them.
REJECTED_FORMS = {
    "missing": "DECLARED_MISSING",
    "empty": "DECLARED_EMPTY",
    "blank": "DECLARED_EMPTY",
    "malformed_text": "DECLARED_MALFORMED",
    "duplicated": "DECLARED_AMBIGUOUS",
    "comma_joined_conflict": "DECLARED_AMBIGUOUS",
    "signed_positive": "DECLARED_MALFORMED",
    "signed_negative": "DECLARED_MALFORMED",
    "decimal": "DECLARED_MALFORMED",
    "exponent": "DECLARED_MALFORMED",
    "hexadecimal": "DECLARED_MALFORMED",
    "trailing_unit": "DECLARED_MALFORMED",
    "internal_space": "DECLARED_MALFORMED",
    "out_of_range": "DECLARED_RANGE",
    "over_ceiling": "DECLARED_TOO_LARGE",
    "far_over_ceiling": "DECLARED_TOO_LARGE",
}

# Forms the gate must let through to the reader.
ACCEPTED_FORMS = ("exactly_at_ceiling", "small_valid", "zero_padded")

_CACHE: dict = {}


def scenarios() -> dict:
    if not _CACHE:
        result = subprocess.run(
            ["node", str(HARNESS)], capture_output=True, text=True,
            cwd=str(REPO_ROOT), timeout=900,
        )
        if result.returncode != 0:
            raise AssertionError(f"harness failed: {result.stderr.strip()[:400]}")
        _CACHE.update(json.loads(result.stdout))
        broken = {name: value.get("harness_error") for name, value in _CACHE.items()
                  if isinstance(value, dict) and value.get("harness_error")}
        if broken:
            raise AssertionError(f"harness scenario errors: {broken}")
    return _CACHE


def source(relative: str) -> str:
    return (DELIVERY / relative).read_text(encoding="utf-8")


# --- the framing gate -------------------------------------------------------


def test_every_unacceptable_declaration_is_refused_before_the_reader() -> None:
    """Sixteen refused forms, each proved not to have opened the body.

    The fixture body yields one small chunk and then never resolves. A response
    that arrives at all is therefore proof that the Worker did not read it —
    stronger than inferring it from a byte count, which the stream's own queue
    prefetch could explain.
    """
    case = scenarios()["declared_length_grammar"]
    for label, reason in REJECTED_FORMS.items():
        probe = case[label]
        assert probe["status"] == 400, (label, probe["status"])
        assert probe["reason"] == reason, (label, probe)

        # The body was never opened.
        assert probe["body_reader_blocked"] is False, (label, probe)
        assert probe["body_pulls"] <= 1, (label, probe)  # 1 = the queue prefetch
        assert probe["reader_entered"] is False, (label, probe)
        assert probe["body_read_mode"] is None, (label, probe)
        assert probe["bytes_read"] == 0, (label, probe)

        # And nothing behind the gate ran.
        assert probe["d1_statements"] == 0, (label, probe)
        assert probe["sessions"] == 0, (label, probe)
        assert probe["set_cookie"] is None, (label, probe)

        # The rate limiter decided first, so a framing failure is still counted
        # and cannot be used as an unlimited pre-filter.
        assert probe["limiter_calls"] == 1, (label, probe)

        # Full security headers and a private cache policy on every refusal.
        assert probe["cache_control"] == "private, no-store, max-age=0, must-revalidate", (label, probe)
        assert probe["csp_present"] is True, (label, probe)

    print(f"PASS test_every_unacceptable_declaration_is_refused_before_the_reader "
          f"({len(REJECTED_FORMS)} forms)")


def test_the_external_answer_gives_away_no_protocol_distinction() -> None:
    """One status and one body for every framing failure.

    The internal vocabulary distinguishes eight causes; the caller sees one. A
    411/413/400 split would tell an attacker which of "you sent no length", "you
    sent a broken length" and "you sent too much" applied.
    """
    case = scenarios()["declared_length_grammar"]
    bodies = {case[label]["body"] for label in REJECTED_FORMS}
    statuses = {case[label]["status"] for label in REJECTED_FORMS}
    assert statuses == {400}, statuses
    assert len(bodies) == 1, bodies
    assert json.loads(bodies.pop()) == {"error": "INVALID_LINK"}

    # The same answer a malformed capability gets, so framing is not a
    # distinguishable class of failure from outside.
    print("PASS test_the_external_answer_gives_away_no_protocol_distinction")


def test_an_acceptable_declaration_reaches_the_reader() -> None:
    """The gate is a gate, not a wall: 512, 60 and a zero-padded 60 pass.

    Here STALLED is the proof of success — the framing gate let the request
    through, the reader was entered, and the fixture stream then blocked. A
    refusal at this point would mean the gate had rejected an acceptable form.
    """
    case = scenarios()["declared_length_grammar"]
    for label in ACCEPTED_FORMS:
        probe = case[label]
        assert probe["status"] == "STALLED", (label, probe)
        assert probe["body_reader_blocked"] is True, \
            (label, "the reader must have been entered", probe)
        assert probe["limiter_calls"] == 1, (label, probe)
        # Still nothing persisted: the body never completed.
        assert probe["d1_statements"] == 0, (label, probe)
        assert probe["sessions"] == 0, (label, probe)

    # A declaration of zero is accepted by the gate and then fails the exact
    # contract, because the fixture delivers 8 bytes. That is the reader's job,
    # not the gate's, and the distinction is asserted rather than blurred.
    zero = case["zero"]
    assert zero["status"] == 400, zero
    assert zero["reason"] == "DECLARED_MISMATCH", zero
    assert zero["reader_entered"] is True, zero
    print("PASS test_an_acceptable_declaration_reaches_the_reader")


def test_the_declared_length_parser_grammar() -> None:
    """`1*DIGIT`, and nothing else, with an explicit range bound."""
    case = scenarios()["declared_length_parser"]
    parse = case["parse"]

    # Accepted: a bare decimal count. Leading zeros are `1*DIGIT` too and denote
    # exactly one value, so they are accepted deliberately — the ambiguity this
    # gate exists to refuse is TWO declarations for one body, not zero padding.
    assert parse["zero"] == {"ok": True, "length": 0}
    assert parse["small"] == {"ok": True, "length": 60}
    assert parse["at_ceiling"] == {"ok": True, "length": MAX_SESSION_REQUEST_BYTES}
    assert parse["zero_padded"] == {"ok": True, "length": 60}, parse["zero_padded"]
    assert parse["zero_padded_long"] == {"ok": True, "length": 60}, \
        "padding must be measured by significance, not by string length"
    assert parse["fifteen_digits"]["ok"] is True

    # Refused, by cause.
    assert parse["missing"] == {"ok": False, "reason": "DECLARED_MISSING"}
    assert parse["empty"] == {"ok": False, "reason": "DECLARED_EMPTY"}
    assert parse["blank"] == {"ok": False, "reason": "DECLARED_EMPTY"}
    assert parse["comma"] == {"ok": False, "reason": "DECLARED_AMBIGUOUS"}
    for label in ("signed_plus", "signed_minus", "decimal", "exponent", "hex",
                  "unit", "internal_space"):
        assert parse[label] == {"ok": False, "reason": "DECLARED_MALFORMED"}, (label, parse[label])
    assert parse["sixteen_digits"] == {"ok": False, "reason": "DECLARED_RANGE"}

    # The parser itself carries no ceiling; `requireDeclaredLength` adds it, so
    # the two responsibilities stay separable and testable.
    assert parse["above_ceiling"] == {"ok": True, "length": MAX_SESSION_REQUEST_BYTES + 1}
    bounded = case["bounded"]
    assert bounded["at_ceiling"] == {"ok": True, "length": MAX_SESSION_REQUEST_BYTES}
    assert bounded["above_ceiling"] == {"ok": False, "reason": "DECLARED_TOO_LARGE"}
    assert bounded["missing"] == {"ok": False, "reason": "DECLARED_MISSING"}

    # Every diagnostic must survive lib/log.js's secret scrubber, which redacts
    # any opaque run of 24+ word characters.
    for reason in case["vocabulary"].values():
        assert len(reason) < 24, f"{reason} would be scrubbed to [redacted]"
        assert re.fullmatch(r"[A-Z_]+", reason), reason
    print("PASS test_the_declared_length_parser_grammar")


# --- actual versus declared -------------------------------------------------


def test_actual_bytes_must_equal_the_declaration() -> None:
    """The exact contract, in both directions, and the two bounds kept apart."""
    case = scenarios()["actual_versus_declared"]

    # Honest framing: accepted, and the count equals the declaration.
    for label in ("exact_small", "exact_at_ceiling", "exact_zero"):
        probe = case[label]
        assert probe["ok"] is True, (label, probe)
        assert probe["bytes_read"] == probe["declared"] == probe["actual"], (label, probe)
        assert probe["bytes_read"] <= MAX_SESSION_REQUEST_BYTES, (label, probe)

    # More than declared, still inside the endpoint ceiling: the DECLARATION is
    # the bound that trips, and the reason says so.
    longer = case["longer_than_declared"]
    assert longer["ok"] is False, longer
    assert longer["reason"] == "DECLARED_MISMATCH", longer
    assert longer["bytes_read"] <= MAX_SESSION_REQUEST_BYTES, longer

    # More than the endpoint ceiling: the CEILING is the bound that trips. This
    # is the independent defence-in-depth layer, and it does not depend on the
    # declaration being honest.
    over = case["longer_than_ceiling"]
    assert over["ok"] is False, over
    assert over["reason"] == "STREAM_TOO_LARGE", over

    # Fewer than declared: a framing failure, refused rather than parsed as a
    # truncated document.
    short = case["shorter_than_declared"]
    assert short["ok"] is False, short
    assert short["reason"] == "DECLARED_MISMATCH", short
    assert short["bytes_read"] == short["actual"], short

    # A declaration above the ceiling never reaches the reader at all.
    refused = case["declared_over_ceiling"]
    assert refused["ok"] is False, refused
    assert refused["reason"] == "DECLARED_TOO_LARGE", refused
    assert refused["mode"] == "declared", refused
    assert refused["bytes_read"] == 0, refused
    print("PASS test_actual_bytes_must_equal_the_declaration")


# --- reader modes -----------------------------------------------------------


def test_both_reader_modes_satisfy_the_same_invariant() -> None:
    """BYOB is an optimisation and a telemetry signal, not a release condition.

    Both modes are exercised against the same 60-byte browser-shaped payload —
    the byte count the deployed runtime actually reported — and both must
    produce the same bytes under the same bound.
    """
    case = scenarios()["both_reader_modes_are_bounded"]
    assert case["payload_bytes"] == 60, case["payload_bytes"]

    # Identical outcome from both paths: same bytes, same bound, same success.
    for label in ("native", "byob"):
        probe = case[label]
        assert probe["ok"] is True, (label, probe)
        assert probe["mode"] in READER_MODES, (label, probe)
        assert probe["bytes_read"] == case["payload_bytes"], (label, probe)
        assert probe["bytes_read"] <= MAX_SESSION_REQUEST_BYTES, (label, probe)
        assert probe["text_matches"] is True, (label, probe)

    # The mode a plain `Request.body` yields is a PLATFORM FACT, recorded rather
    # than required: Node 18 gives `default`, Node 22 gives `byob`, and
    # Cloudflare gives `default`. Three answers, one invariant — which is
    # precisely why the mode cannot be a release condition.
    assert case["native"]["mode"] in READER_MODES, case["native"]

    # The BYOB path is real and reachable, not dead code kept for appearances.
    assert case["byob_path_is_reachable"] is True, case
    assert case["byob"]["mode"] == "byob", case["byob"]
    assert case["byob_buffer_bytes"] == 1024, case

    # And the code still tries BYOB first, then falls back.
    body = source("worker/lib/body.js")
    assert "tryByobReader(stream)" in body
    assert "readWithByob(byob, ceiling)" in body
    assert "readWithDefaultReader(stream.getReader(), ceiling)" in body
    print("PASS test_both_reader_modes_satisfy_the_same_invariant "
          f"(this runtime's Request.body -> {case['native']['mode']} on {case['runtime']})")


def test_no_document_or_test_calls_the_default_reader_a_release_blocker() -> None:
    """The retracted doctrine must not survive anywhere it was written.

    Deployed evidence that BYOB is unavailable is preserved as history; the
    REQUIREMENT that BYOB be proven before release is not.
    """
    readme = source("README.md")
    docs28 = (REPO_ROOT / "docs" / "28_driver_eco_dashboard_v1_snapshot_foundation.md").read_text(
        encoding="utf-8")

    retracted = (
        "Verify BYOB on the real runtime",
        "real-runtime confirmation is a release gate",
        "Until 1 is confirmed",
    )
    for text, label in ((readme, "README.md"), (docs28, "docs/28")):
        for claim in retracted:
            assert claim not in text, f"{label} still requires BYOB for release: {claim}"

    # And the replacement invariant is stated where the old gate was.
    assert "acceptable declared body size" in readme, \
        "the README must state the framing invariant that replaced the BYOB gate"
    assert "opportunistic optimisation" in readme, \
        "the README must state that BYOB is now an optional optimisation"
    assert "an accepted production mode" in readme, \
        "the README must state that the default reader is production-acceptable"
    assert "neither mode is a deployment blocker" in readme.lower(), \
        "the README must state that neither reader mode blocks deployment"

    # The historical finding itself is preserved, not rewritten away.
    assert "not a byte stream" in readme, \
        "the deployed BYOB finding must remain recorded"
    print("PASS test_no_document_or_test_calls_the_default_reader_a_release_blocker")


# --- ordering ---------------------------------------------------------------


def test_the_rate_limiter_still_decides_first() -> None:
    """429 outranks every framing verdict.

    If the framing gate ran first, a caller could send unlimited undeclared
    requests and get an uncounted 400 forever.
    """
    case = scenarios()["rate_limit_still_runs_first"]
    assert case["undeclared_status"] == 429, case
    assert case["oversized_declared_status"] == 429, case
    assert case["malformed_declared_status"] == 429, case
    assert case["undeclared_retry_after"] == "60", case
    assert case["limiter_calls"] == 3, case
    assert case["d1_statements"] == 0, case
    assert case["sessions"] == 0, case
    assert case["body_pulls"] <= 1, case
    assert [event["reason"] for event in case["events"]] == ["RATE_LIMITED"] * 3, case["events"]
    print("PASS test_the_rate_limiter_still_decides_first")


def test_the_route_applies_the_gate_before_the_read() -> None:
    """Source-level ordering, so a future edit cannot silently reorder it."""
    worker = source("worker/index.js")
    limit = worker.index("await limitSessionExchange(")
    content_type = worker.index("readPublisherContentType(request.headers)")
    gate = worker.index("requireDeclaredLength(request.headers")
    read = worker.index("await readBoundedBody(request, MAX_SESSION_REQUEST_BYTES")
    digest = worker.index("await capabilityDigest(raw")
    store = worker.index("new D1AuthorizationStore(env.AUTHORIZATION_DB)")

    assert limit < content_type < gate < read < store < digest, (
        "order must be: rate limit -> content type -> framing gate -> body read "
        "-> authorization store -> capability digest")

    # The declared length the gate produced is what the reader is bounded by;
    # the reader must not re-derive it from the header on this route.
    assert "declaredLength: framing.length" in worker
    print("PASS test_the_route_applies_the_gate_before_the_read")


# --- frontend compatibility -------------------------------------------------


def test_the_real_frontend_bootstrap_still_succeeds() -> None:
    """The browser request shape satisfies the strict contract unchanged.

    `Content-Length` is a forbidden header name for scripts: `boot.js` cannot
    set it and must not try. The browser computes it from the body, and the
    Cloudflare edge presents it to the Worker — which is exactly what the
    deployed verification observed (a 60-byte session body read in full).
    """
    case = scenarios()["frontend_bootstrap_is_compatible"]

    # The frontend does not, and may not, set the header itself.
    assert case["boot_sets_content_length"] is False, \
        "boot.js must not attempt to set a forbidden header"
    assert case["boot_uses_json_stringify"] is True
    assert case["boot_endpoint"] is True

    # The framing the browser produces satisfies the gate with room to spare.
    assert case["declared_matches_payload"] is True, case
    assert case["declared_header"] == str(case["payload_bytes"]), case
    assert case["payload_bytes"] < case["ceiling"], case
    # 60 bytes is precisely what the deployed runtime reported for this shape.
    assert case["payload_bytes"] == 60, case

    # And it still establishes a session.
    assert case["status"] == 204, case
    assert case["sets_cookie"] is True, case
    assert case["sessions"] == 1, case
    assert case["established_ttl"] == 1800, case
    assert case["logs_leak_capability"] is False, case

    # The deployed workers.dev probe shape — same framing, unknown capability —
    # reads the body and is refused at the store, with the evidence intact.
    probe = case["probe_event"]
    assert case["probe_status"] == 401, case
    assert probe["reason"] == "NOT_ACTIVE", probe
    assert probe["reader_entered"] is True, probe
    assert probe["body_read_mode"] in READER_MODES, probe
    assert probe["bytes_read"] == case["probe_payload_bytes"], probe
    assert probe["bytes_read"] <= MAX_SESSION_REQUEST_BYTES, probe
    # The local development server must not strip the header on its way in,
    # or local verification would exercise a shape production never sees. It
    # copies every incoming Node request header verbatim, which includes the
    # `Content-Length` the browser computed.
    serve = source("local/serve.js")
    assert "for (const [name, value] of Object.entries(nodeRequest.headers))" in serve, \
        "the local server must forward the incoming request headers verbatim"
    assert not re.search(r"headers\.delete\(\s*[\"']content-length", serve, re.I), \
        "the local server must not drop the declared length"

    print("PASS test_the_real_frontend_bootstrap_still_succeeds "
          f"({case['payload_bytes']} B declared, read in {probe['body_read_mode']} mode)")


# --- privacy ----------------------------------------------------------------


def test_framing_diagnostics_leak_nothing() -> None:
    """Six refusals over a body carrying a real capability, and no leak."""
    case = scenarios()["framing_diagnostics_are_private"]
    assert case["leaks_capability"] is False, case
    assert case["leaks_body_marker"] is False, case
    assert case["leaks_client_ip"] is False, case
    assert case["leaks_rate_limit_key"] is False, case
    assert case["reason_charset_ok"] is True, \
        "a diagnostic must be a fixed vocabulary word, never echoed request data"
    assert set(case["reasons"]) == {
        "DECLARED_MISSING", "DECLARED_EMPTY", "DECLARED_MALFORMED",
        "DECLARED_AMBIGUOUS", "DECLARED_TOO_LARGE", "DECLARED_MISMATCH",
    }, case["reasons"]

    # The declared size itself is never emitted as a field: the accepted size is
    # already observable as `bytes_read` on a successful read, so there is no
    # reason to echo a rejected one back into a log.
    worker = source("worker/index.js")
    assert "declared_length" not in worker
    assert "framing.length," not in worker.replace("declaredLength: framing.length,", "")
    print("PASS test_framing_diagnostics_leak_nothing")


# --- scope ------------------------------------------------------------------


def test_the_publisher_upload_path_was_not_redesigned() -> None:
    """The new exact contract is scoped to the route that asked for it.

    The credential-gated publisher upload keeps its previous behaviour: the
    header still refuses an oversized declaration before the read, but it does
    not become a promise the body has to match.
    """
    worker = source("worker/index.js")
    assert "readBoundedBody(request, MAX_PUBLISH_REQUEST_BYTES)" in worker, \
        "the publisher read must not have acquired an explicit declaration"
    assert worker.count("requireDeclaredLength(") == 1, \
        "the framing gate belongs to the session exchange only"

    body = source("worker/lib/body.js")
    assert "Only an EXPLICITLY supplied declaration activates the exact contract" in body
    assert "const MAX_PUBLISH_REQUEST_BYTES = 256 * 1024;" in worker
    print("PASS test_the_publisher_upload_path_was_not_redesigned")


def test_no_compatibility_flag_was_added() -> None:
    """No compatibility date bump and no flags were added to chase BYOB.

    Neither `streams_byob_reader_detaches_buffer` (default since 2021-11-10) nor
    `internal_stream_byob_return_view` (default since 2024-05-13) changes the
    TYPE of the incoming `Request.body` stream — both only alter the behaviour
    of a BYOB reader that already exists. Adding either would change nothing
    here while activating unrelated runtime behaviour.
    """
    wrangler = source("wrangler.toml")
    assert "compatibility_flags" not in wrangler, \
        "no compatibility flag is justified by the BYOB investigation"
    assert re.search(r'^compatibility_date = "2026-08-01"$', wrangler, re.M), \
        "the compatibility date must not have been bumped speculatively"
    assert "workers_dev = false" in wrangler, "workers.dev must remain disabled"
    # The session ceiling is unchanged and still authoritative.
    assert "const MAX_SESSION_REQUEST_BYTES = 512;" in source("worker/index.js")
    print("PASS test_no_compatibility_flag_was_added")


def main() -> None:
    test_every_unacceptable_declaration_is_refused_before_the_reader()
    test_the_external_answer_gives_away_no_protocol_distinction()
    test_an_acceptable_declaration_reaches_the_reader()
    test_the_declared_length_parser_grammar()
    test_actual_bytes_must_equal_the_declaration()
    test_both_reader_modes_satisfy_the_same_invariant()
    test_no_document_or_test_calls_the_default_reader_a_release_blocker()
    test_the_rate_limiter_still_decides_first()
    test_the_route_applies_the_gate_before_the_read()
    test_the_real_frontend_bootstrap_still_succeeds()
    test_framing_diagnostics_leak_nothing()
    test_the_publisher_upload_path_was_not_redesigned()
    test_no_compatibility_flag_was_added()
    print("Driver Eco Dashboard V1 session request-framing contract passed")


if __name__ == "__main__":
    main()
