#!/usr/bin/env python3
"""Edge guards for the Driver Eco Dashboard V1 delivery boundary.

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_edge_guards.py

Covers the two application-layer guards that stand between the deployed Worker
and real driver traffic:

  1. WORKER-NATIVE RATE LIMITING on `POST /api/session` — the only route
     reachable before any credential is examined — and on nothing else.
  2. BODY-READER OBSERVABILITY — structured, privacy-safe evidence of which
     reader mode the runtime actually used, so the deployed BYOB contract can be
     established from a log line rather than assumed.

Drives ops/tests_manual/eco_edge_guards_harness.mjs, which executes the real
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
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DELIVERY = REPO_ROOT / "delivery" / "driver_eco_dashboard"
HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_edge_guards_harness.mjs"

# The reader mode a runtime may honestly report once a reader has been created.
READER_MODES = {"byob", "default", "buffered"}

_CACHE: dict = {}


def scenarios() -> dict:
    if not _CACHE:
        result = subprocess.run(
            ["node", str(HARNESS)], capture_output=True, text=True,
            cwd=str(REPO_ROOT), timeout=300,
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


def code_only(text: str) -> str:
    """Strip comments so a source assertion tests code, not prose."""
    without_block = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"^\s*//.*$", " ", without_block, flags=re.M)


# --- route scope ------------------------------------------------------------


def test_only_the_session_exchange_is_rate_limited() -> None:
    """The limiter must protect POST /api/session and cost nothing elsewhere.

    Rate limiting a read path would be a denial-of-service on the driver's own
    dashboard; rate limiting the asset bundle would break the page. Every other
    route is exercised against the SAME world as the one exchange that DID
    consult the limiter, so this is not "a fresh limiter saw nothing".
    """
    case = scenarios()["rate_limit_applies_only_to_the_session_exchange"]
    by_label = {probe["label"]: probe for probe in case["probes"]}

    assert by_label["post_session"]["limiter_calls"] == 1, by_label["post_session"]
    assert by_label["post_session"]["status"] == 204

    for label in ("get_session", "post_session_end", "get_snapshot",
                  "post_publish", "post_publish_recover", "post_publish_delivery",
                  "unknown_api", "asset_document", "asset_bundle"):
        assert by_label[label]["limiter_calls"] == 0, (label, by_label[label])

    # Exactly one limiter call across ten requests.
    assert case["total_limiter_calls"] == 1, case["total_limiter_calls"]
    assert case["key_prefixed"] is True, "the key must be namespaced by operation"

    # The single call site, asserted in the source: no other module may reach
    # the binding, and no other route may reach the module.
    worker = code_only(source("worker/index.js"))
    assert worker.count("limitSessionExchange(") == 1, \
        "the limiter must have exactly one call site"
    # `SESSION_RATE_LIMIT_PERIOD_SECONDS` is imported for the Retry-After header;
    # the BINDING itself must only ever be touched inside lib/rate_limit.js.
    assert re.search(r"SESSION_RATE_LIMIT\b", worker) is None, \
        "index.js must reach the binding through lib/rate_limit.js, not by name"
    for module in ("worker/lib/store.js", "worker/lib/publisher.js",
                   "worker/lib/publication.js", "worker/lib/assets.js"):
        assert "rate_limit" not in code_only(source(module)), module
    print("PASS test_only_the_session_exchange_is_rate_limited")


# --- allowed request --------------------------------------------------------


def test_an_allowed_request_behaves_exactly_as_before() -> None:
    """Under the limit nothing about the exchange changes."""
    case = scenarios()["allowed_request_is_unchanged"]
    assert case["status"] == 204, case
    assert case["sets_cookie"] is True
    assert case["cache_control"] == "private, no-store, max-age=0, must-revalidate"
    assert case["sessions"] == 1, "the session must still be created"
    assert case["snapshot_status"] == 200, "the issued cookie must still work"
    assert case["limiter_calls"] == 1
    assert case["logs_leak_capability"] is False
    print("PASS test_an_allowed_request_behaves_exactly_as_before")


# --- limited request --------------------------------------------------------


def test_a_limited_request_does_no_work_at_all() -> None:
    """429 must be a hard stop: no body read, no D1, no session, no cookie.

    The request body is a stream that hands over one prefetched chunk and then
    never resolves, so a Worker that entered the body reader would block. A
    prompt 429 with a single pull is positive evidence that it did not.
    """
    case = scenarios()["limited_request_does_no_work"]
    assert case["status"] == 429, case["status"]
    assert case["body_pulls"] == 1, \
        ("the body reader must not run", case["body_pulls"])
    assert case["body_reader_blocked"] is False
    assert case["d1_statements"] == 0, "no D1 statement may be prepared"
    assert case["sessions"] == 0, "no session may be created"
    assert case["set_cookie"] is None, "a limited request must not set a cookie"

    # The same private/no-store contract every other /api/* answer carries.
    assert case["cache_control"] == "private, no-store, max-age=0, must-revalidate"
    assert case["csp_present"] is True
    assert case["nosniff"] == "nosniff"
    assert case["retry_after"] == "60", "the window must be advertised honestly"

    # A stable, non-sensitive error contract.
    assert json.loads(case["body"]) == {"error": "RATE_LIMITED"}, case["body"]

    # The 429 must not become an oracle for whether a link exists: a VALID
    # capability, an unknown one and no capability at all answer identically,
    # byte for byte.
    assert case["valid_status"] == case["unknown_status"] == case["no_capability_status"] == 429
    assert case["valid_matches_unknown"] is True
    assert case["valid_sets_cookie"] is False
    assert case["logs_leak_capability"] is False

    # Nothing in the emitted events names an actor or a capability.
    for event in case["events"]:
        assert event["event"] == "session_exchange_rejected", event
        assert event["reason"] == "RATE_LIMITED", event
        assert set(event) == {"level", "event", "reason"}, \
            ("the limited path must log a reason and nothing else", event)
    print("PASS test_a_limited_request_does_no_work_at_all")


# --- actor key --------------------------------------------------------------


def test_the_actor_key_is_per_client_and_never_logged() -> None:
    """One bucket per client address — never one global bucket, never a log."""
    case = scenarios()["actor_key_is_per_client_and_never_logged"]
    assert case["call_count"] == 3
    assert case["distinct_keys"] == 2, \
        ("two client addresses must not share one bucket", case["distinct_keys"])
    assert case["repeat_is_stable"] is True, "the same actor must map to the same key"
    assert case["other_actor_differs"] is True
    assert case["key_shape_ok"] is True

    # The client address is personal data. It keys the limiter and reaches
    # nothing else.
    assert case["logs_leak_client_ip"] is False
    assert case["logs_leak_key"] is False
    assert case["logs_leak_capability"] is False

    # And the module itself never logs: it returns an outcome, not the key.
    rate_limit = code_only(source("worker/lib/rate_limit.js"))
    assert "logEvent" not in rate_limit, "the limiter module must not log"
    print("PASS test_the_actor_key_is_per_client_and_never_logged")


def test_client_address_parsing_is_conservative() -> None:
    """Only a single, well-formed, trusted address becomes a key."""
    case = scenarios()["client_address_parsing"]

    # Accepted, and normalised so one host cannot produce two buckets.
    assert case["ipv4"] == "203.0.113.10"
    assert case["ipv4_padded"] == "203.0.113.10"
    assert case["ipv6"] == "2001:db8::1"
    assert case["ipv6_uppercase"] == "2001:db8::ab", "IPv6 hex case must not fork the bucket"
    assert case["ipv6_mapped"] == "::ffff:203.0.113.10"

    # Refused. A duplicated header is comma-joined and guessing which half is
    # the peer would either merge two actors or split one; neither is a limiter.
    for label in ("absent", "empty", "blank", "ipv4_leading_zero", "ipv4_out_of_range",
                  "ipv4_short", "duplicated_header", "spaced_pair", "hostname", "overlong"):
        assert case[label] is None, (label, case[label])

    assert case["key_for_ipv4"] == "session:203.0.113.10"

    # Only the Cloudflare-set header is trusted. X-Forwarded-For is caller
    # supplied and must never key a limiter.
    rate_limit = code_only(source("worker/lib/rate_limit.js"))
    assert "CF-Connecting-IP" in rate_limit
    assert "X-Forwarded-For" not in rate_limit
    assert "x-forwarded-for" not in rate_limit.lower()
    print("PASS test_client_address_parsing_is_conservative")


# --- fail-closed ------------------------------------------------------------


def test_a_missing_actor_or_binding_fails_closed() -> None:
    """Production must not silently serve this endpoint unprotected.

    A deployment that lost `[[ratelimits]]`, a binding that threw, a binding
    that answered in an unknown shape, and a request with no trusted client
    address all refuse — before the body is read and before D1 is touched — and
    none of them is a 429, which stays reserved for a real limit hit.
    """
    case = scenarios()["missing_actor_or_binding_fails_closed"]
    expected = {
        "no_client_address": "NO_CLIENT_ADDRESS",
        "malformed_client_address": "NO_CLIENT_ADDRESS",
        "duplicated_client_address": "NO_CLIENT_ADDRESS",
        "binding_absent": "RATE_LIMITER_MISSING",
        "binding_throws": "RATE_LIMITER_FAILED",
        "binding_answers_nonsense": "RATE_LIMITER_FAILED",
    }
    for label, reason in expected.items():
        probe = case[label]
        assert probe["status"] == 503, (label, probe["status"])
        assert json.loads(probe["body"]) == {"error": "SERVICE_UNAVAILABLE"}, (label, probe["body"])
        assert probe["body_pulls"] == 1, (label, "the body reader must not run")
        assert probe["body_reader_blocked"] is False, label
        assert probe["d1_statements"] == 0, (label, probe["d1_statements"])
        assert probe["set_cookie"] is None, label
        assert probe["cache_control"] == "private, no-store, max-age=0, must-revalidate", label
        assert probe["level"] == "ERROR", (label, "a broken guard is an operator signal")
        assert probe["reason"] == reason, (label, probe["reason"])
        # No reader ran, so no read evidence may be claimed.
        assert probe["has_read_evidence"] is False, label

    # Every reason must survive the log scrubber. A 24+ character opaque token
    # is redacted as secret-shaped, which would blind exactly the deployment
    # failure these outcomes exist to report.
    for entry in case["reason_vocabulary"]:
        assert entry["survives_scrubber"] is True, entry

    # There is no fail-open seam: no environment flag can make a missing binding
    # acceptable, and the only default the module has is refusal.
    rate_limit = code_only(source("worker/lib/rate_limit.js"))
    assert "ALLOWED" in rate_limit
    assert re.search(r"return\s*\{\s*outcome:\s*RATE_LIMIT_OUTCOME\.ALLOWED", rate_limit) is None, \
        "ALLOWED must only ever be produced from a successful binding verdict"
    print("PASS test_a_missing_actor_or_binding_fails_closed")


# --- policy -----------------------------------------------------------------


def test_the_policy_is_sixty_requests_per_sixty_seconds() -> None:
    """60/60s per pre-authentication actor, and the config says the same.

    Deliberately generous: an IP key aggregates everyone behind one NAT or
    privacy proxy, so the control is sized against automated enumeration volume
    rather than against a second visitor in the same office.
    """
    case = scenarios()["policy_is_sixty_per_sixty"]
    assert case["allowed"] == 60, case["allowed"]
    assert case["refused"] == 5, case["refused"]
    # One actor exhausting its bucket must not touch anyone else.
    assert case["other_actor_status"] != 429, case["other_actor_status"]
    # And the window rolls rather than latching.
    assert case["next_window_status"] != 429, case["next_window_status"]

    assert case["module_limit"] == 60
    assert case["module_period"] == 60
    assert case["binding_name"] == "SESSION_RATE_LIMIT"

    # The deployed configuration must declare exactly this policy.
    wrangler = source("wrangler.toml")
    assert "[[ratelimits]]" in wrangler, "the binding must be declared"
    assert re.search(r'^name = "SESSION_RATE_LIMIT"$', wrangler, re.M), wrangler
    assert re.search(r"^simple = \{ limit = 60, period = 60 \}$", wrangler, re.M), \
        "the committed policy must be 60 requests per 60 seconds"
    # An account-unique positive integer, as a string, pinned in configuration.
    namespace = re.search(r'^namespace_id = "(\d+)"$', wrangler, re.M)
    assert namespace, "namespace_id must be a quoted positive integer"
    assert int(namespace.group(1)) > 0

    # Still no workers.dev, no route, no custom hostname in committed config.
    # (The prose above the binding explains WHY workers.dev matters; what must
    # not appear is an enabling directive.)
    assert re.search(r"^\s*workers_dev\s*=\s*false\s*$", wrangler, re.M), wrangler
    assert re.search(r"^\s*workers_dev\s*=\s*true", wrangler, re.M) is None
    for directive in (r"\[\[routes\]\]", r"^\s*route\s*=", r"^\s*routes\s*=",
                      r"\[\[custom_domains\]\]"):
        assert re.search(directive, wrangler, re.M) is None, directive
    print("PASS test_the_policy_is_sixty_requests_per_sixty_seconds")


# --- body-reader observability ---------------------------------------------


def test_the_deployed_body_reader_mode_is_observable() -> None:
    """A synthetic probe must be able to read the reader mode out of a log.

    Deployed verification settled the reader-mode question negatively — the
    Cloudflare incoming `Request.body` is not a byte stream, so the mode is
    always `default` there. The mode is therefore TELEMETRY, not a gate: this
    test asserts that the evidence stays honest and legible in every mode, and
    that neither mode is treated as a failure.
    """
    case = scenarios()["body_read_mode_is_observable"]

    # FRAMING GATE, oversized declaration. No reader was ever created, so the
    # mode must be represented as absent rather than guessed from the
    # configured preference.
    declared = case["declared_too_large"]["event"]
    assert declared["reason"] == "DECLARED_TOO_LARGE", declared
    assert declared["bytes_read"] == 0, declared
    assert declared["body_read_mode"] is None, \
        ("no reader ran, so no mode may be claimed", declared)
    assert declared["reader_entered"] is False, declared
    assert case["declared_too_large"]["status"] == 400

    # FRAMING GATE, no declaration at all. Fail-closed, and the strongest form
    # of "the body was not read": the fixture stream was never pulled from.
    undeclared = case["undeclared_stream"]
    event = undeclared["event"]
    assert undeclared["status"] == 400, undeclared
    assert event["reason"] == "DECLARED_MISSING", event
    assert event["reader_entered"] is False, event
    assert event["body_read_mode"] is None, event
    assert event["bytes_read"] == 0, event
    # 1 = the stream's own queue prefetch. Anything more, or a stalled reader,
    # would mean the Worker opened the body it had already refused.
    assert undeclared["body_pulls"] <= 1, undeclared
    assert undeclared["body_reader_blocked"] is False, \
        "an undeclared body must not be read at all"

    # INDEPENDENT BYTE CEILING. An accepted declaration over a stream that then
    # delivers far more: the reader runs, the running total is refused
    # mid-stream, and the remainder is cancelled rather than drained. This layer
    # holds in `default` mode and does not depend on BYOB.
    stream = case["declared_small_but_oversized_stream"]["event"]
    # The declaration said 512 and the stream delivered 4 KiB in one chunk, so
    # the bound that tripped is the endpoint ceiling itself, not merely the
    # declaration. The reason names the bound that was actually crossed.
    assert stream["reason"] == "STREAM_TOO_LARGE", stream
    assert stream["reader_entered"] is True, stream
    assert stream["body_read_mode"] in READER_MODES, stream
    assert 0 < stream["bytes_read"] <= 512 + stream["largest_chunk_bytes"], stream
    assert stream["bytes_read"] < case["declared_small_but_oversized_stream"]["total_offered"], \
        "the oversized body must not be drained"
    assert case["declared_small_but_oversized_stream"]["stream_cancelled"] is True

    # THE PROBE PATH. A small, well-formed body reads cleanly and is then
    # refused at the authorization store. This is what the deployed workers.dev
    # verification will send, and this event is what it will read back.
    probe = case["read_then_unknown_capability"]
    denied = probe["event"]
    assert probe["status"] == 401, probe["status"]
    assert denied["event"] == "session_exchange_denied"
    assert denied["reason"] == "NOT_ACTIVE", denied
    assert denied["reader_entered"] is True, denied
    assert denied["body_read_mode"] in READER_MODES, denied
    assert denied["bytes_read"] == probe["payload_bytes"], denied
    assert denied["bytes_read"] < 512, "a bounded, small read"
    # The capability itself is never named, so the probe reveals nothing.
    assert probe["logs_leak_capability"] is False
    # And the diagnostic surface stays minimal: one event, not a new INFO line
    # on top of the existing ones.
    assert probe["log_lines"] == 1, probe["log_lines"]

    # Every other post-read rejection carries the same evidence, so the probe
    # has more than one way to establish the mode.
    for label, reason in (("malformed_capability", "MALFORMED"),
                          ("unparseable_body", "BODY"),
                          ("wrong_body_shape", "BODY_SHAPE")):
        event = case[label]["event"]
        assert event["reason"] == reason, (label, event)
        assert event["reader_entered"] is True, (label, event)
        assert event["body_read_mode"] in READER_MODES, (label, event)
        assert event["bytes_read"] > 0, (label, event)

    # `byob` and `default` must be distinguishable values, not one string.
    assert case["modes"]["BYOB"] == "byob"
    assert case["modes"]["DEFAULT"] == "default"
    assert case["byob_buffer_bytes"] == 1024
    print("PASS test_the_deployed_body_reader_mode_is_observable "
          f"(this runtime read in {denied['body_read_mode']} mode)")


def test_read_evidence_is_honest_about_every_mode() -> None:
    """`bodyReadEvidence` never fabricates a mode it did not observe."""
    case = scenarios()["read_evidence_is_honest_about_every_mode"]

    assert case["declared"] == {
        "reader_entered": False, "body_read_mode": None,
        "bytes_read": 0, "largest_chunk_bytes": 0,
    }, case["declared"]
    assert case["missing"]["body_read_mode"] is None
    assert case["missing"]["reader_entered"] is False

    for mode in ("byob", "default", "buffered"):
        assert case[mode]["body_read_mode"] == mode, case[mode]
        assert case[mode]["reader_entered"] is True, case[mode]
        assert case[mode]["bytes_read"] == 61, case[mode]
    print("PASS test_read_evidence_is_honest_about_every_mode")


def test_the_body_reader_itself_was_not_weakened() -> None:
    """The framing gate was added on top of the reader, not instead of it."""
    body = source("worker/lib/body.js")
    code = code_only(body)
    for required in ("BODY_READ_MODE", "BYOB_BUFFER_BYTES", "tryByobReader",
                     "readWithByob", "readWithDefaultReader", "largestChunkBytes",
                     "STREAM_TOO_LARGE", "DECLARED_TOO_LARGE", "DECLARED_MISMATCH",
                     "reader.cancel"):
        assert required in code, required
    assert "export const BYOB_BUFFER_BYTES = 1024;" in body
    # BYOB is kept as an opportunistic path, not deleted.
    assert "readWithByob(byob, ceiling)" in code, \
        "BYOB must remain available where the runtime offers a byte stream"
    # The declared-size gate still refuses before the stream is touched.
    assert re.search(r"headerDeclared !== null && headerDeclared > maxBytes", code), \
        "the declared-length gate must survive"
    # The read is bounded by BOTH the declaration and the endpoint ceiling.
    assert "Math.min(declared, maxBytes)" in code, \
        "the effective ceiling must remain min(declared, maxBytes)"
    # And the session ceiling is unchanged.
    assert "const MAX_SESSION_REQUEST_BYTES = 512;" in source("worker/index.js")
    print("PASS test_the_body_reader_itself_was_not_weakened")


# --- privacy ----------------------------------------------------------------


def test_no_sensitive_value_reaches_a_log() -> None:
    """Across every guard path: no body, capability, session id or address."""
    case = scenarios()["no_sensitive_value_reaches_a_log"]
    assert case["log_lines"] > 0, "the paths under test must actually log"
    assert case["leaks_capability"] is False
    assert case["leaks_unknown_capability"] is False
    assert case["leaks_session_id"] is False
    assert case["leaks_client_ip"] is False
    assert case["leaks_body_marker"] is False
    assert case["established_status"] == 204

    # The evidence fields are bounded integers, a boolean and a fixed
    # vocabulary — by construction, not by convention.
    body = code_only(source("worker/lib/body.js"))
    evidence = body[body.index("export function bodyReadEvidence"):]
    # `read.bytesRead` is a count and is fine; the decoded text and the raw
    # octets are not, and neither is any request header.
    for banned in (r"read\.text\b", r"read\.bytes\b", r"request\.headers"):
        assert re.search(banned, evidence) is None, banned
    print("PASS test_no_sensitive_value_reaches_a_log")


# --- deployment posture -----------------------------------------------------


def test_the_candidate_is_not_a_deployment() -> None:
    """This is code and configuration. It enables nothing by itself."""
    wrangler = source("wrangler.toml")
    assert "workers_dev = false" in wrangler, "workers.dev must stay disabled in Git"
    assert re.search(r"^\s*account_id\s*=", wrangler, re.M) is None
    # The rate limit namespace is not a secret and carries no credential.
    assert re.search(r"^\s*(api_token|CLOUDFLARE_API_TOKEN)\s*=", wrangler, re.M) is None
    # Nothing in the delivery tree shells out to wrangler or the Cloudflare API.
    for path in sorted(DELIVERY.rglob("*.js")):
        if ".wrangler" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        assert "api.cloudflare.com" not in text, path.name
        assert "wrangler deploy" not in text, path.name
    print("PASS test_the_candidate_is_not_a_deployment")


def main() -> None:
    test_only_the_session_exchange_is_rate_limited()
    test_an_allowed_request_behaves_exactly_as_before()
    test_a_limited_request_does_no_work_at_all()
    test_the_actor_key_is_per_client_and_never_logged()
    test_client_address_parsing_is_conservative()
    test_a_missing_actor_or_binding_fails_closed()
    test_the_policy_is_sixty_requests_per_sixty_seconds()
    test_the_deployed_body_reader_mode_is_observable()
    test_read_evidence_is_honest_about_every_mode()
    test_the_body_reader_itself_was_not_weakened()
    test_no_sensitive_value_reaches_a_log()
    test_the_candidate_is_not_a_deployment()
    print("Driver Eco Dashboard V1 session edge guards passed")


if __name__ == "__main__":
    main()
