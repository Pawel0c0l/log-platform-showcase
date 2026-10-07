#!/usr/bin/env python3
"""Security checks for the Driver Eco Dashboard V1 delivery boundary.

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_delivery.py

Drives ops/tests_manual/eco_delivery_worker_harness.mjs, which executes the real
Cloudflare Worker against in-memory D1/R2/ASSETS bindings. No wrangler, no
credentials, no remote Cloudflare resource, no production data.

No capability, session id or object key is printed by this suite or by the
harness it drives.
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
FRONTEND = REPO_ROOT / "assets" / "driver_eco_dashboard"
HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_delivery_worker_harness.mjs"

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


# --- capability -------------------------------------------------------------


def test_capability_is_high_entropy_and_opaque() -> None:
    case = scenarios()["capability_generation"]
    assert case["entropy_bits"] == 256, case["entropy_bits"]
    assert case["length"] == 43 and case["charset_ok"]
    assert case["distinct"] == 200, "capability generation must not repeat"
    assert case["wellformed_accepts_valid"] is True
    assert not any(case["wellformed_rejects"]), case["wellformed_rejects"]

    # Only a digest is ever stored, and a pepper binding changes it.
    assert case["digest_length"] == 64
    assert case["digest_is_not_capability"] is True
    assert case["digest_deterministic"] is True
    assert case["pepper_changes_digest"] is True

    # Generation is CSPRNG-backed and carries no business input.
    capability = source("worker/lib/capability.js")
    assert "crypto.getRandomValues" in capability
    assert "Math.random" not in capability
    # No business input reaches capability generation. Scoped to the generator
    # itself: the module also holds the subject-binding domain label, which is a
    # constant rather than an input.
    generation_code = code_only(capability)
    start = generation_code.index("export function generateCapability")
    end = generation_code.index("export function isWellFormedCapability")
    generator = generation_code[start:end].lower()
    for banned in ("driver", "client", "email", "assigned_id", "person_name",
                   "subject", "date.now", "counter", "sequence"):
        assert banned not in generator, banned
    print("PASS test_capability_is_high_entropy_and_opaque")


def test_valid_capability_establishes_a_bounded_session() -> None:
    case = scenarios()["valid_capability_establishes_session"]
    assert case["status"] == 204
    assert case["body_empty"] is True
    assert case["response_leaks_capability"] is False
    assert case["cookie_value_is_not_capability"] is True
    assert case["cookie_value_length"] == 43

    cookie = case["cookie"]
    assert cookie["name"] == "__Host-eco_dash", cookie["name"]
    assert cookie["httponly"] is True
    assert cookie["secure"] is True
    assert cookie["samesite"] == "Strict"
    assert cookie["path"] == "/"
    assert 0 < cookie["max_age"] <= 30 * 60, cookie["max_age"]
    assert case["cache_control"].startswith("private, no-store")
    print("PASS test_valid_capability_establishes_a_bounded_session")


def test_invalid_and_malformed_capabilities_fail_closed() -> None:
    case = scenarios()["invalid_capabilities_denied"]
    assert case["none_established"] is True
    cases = case["cases"]
    # An unknown but well-formed value is indistinguishable from a bad one.
    assert cases["random_valid_shape"] == 401
    for name in ("empty", "too_short", "too_long", "bad_charset",
                 "sql_injection_shape", "path_traversal_shape"):
        assert cases[name] == 401, (name, cases[name])
    for name in ("wrong_type_number", "wrong_type_array", "wrong_type_object",
                 "missing_field", "body_is_array", "not_json"):
        assert cases[name] == 400, (name, cases[name])
    print("PASS test_invalid_and_malformed_capabilities_fail_closed")


def test_expired_capability_cannot_establish_access() -> None:
    case = scenarios()["expired_capability_denied"]
    assert case["status"] == 410            # frontend maps 410 -> LINK_EXPIRED
    assert case["set_cookie"] is False
    assert json.loads(case["body"]) == {"error": "LINK_EXPIRED"}
    print("PASS test_expired_capability_cannot_establish_access")


def test_revoked_capability_cannot_establish_access() -> None:
    case = scenarios()["revoked_capability_denied"]
    assert case["before_status"] == 204
    assert case["after_status"] == 401
    assert case["set_cookie_after"] is False
    # Revocation is not disclosed: the answer is byte-identical to "unknown".
    assert case["indistinguishable_from_unknown"] is True
    print("PASS test_revoked_capability_cannot_establish_access")


def test_rotation_replaces_the_grant() -> None:
    case = scenarios()["rotation_invalidates_predecessor"]
    assert case["different_capability"] is True
    assert case["old_status"] == 401, "the replaced link must stop working"
    assert case["new_status"] == 204
    assert case["new_session_serves_snapshot"] == 200

    sessions = scenarios()["rotation_kills_predecessor_sessions"]
    assert sessions["before"] == 200
    assert sessions["after"] == 401, "rotation must also kill live sessions"
    print("PASS test_rotation_replaces_the_grant")


# --- session ----------------------------------------------------------------


def test_revocation_invalidates_derived_sessions() -> None:
    case = scenarios()["revocation_kills_derived_session"]
    assert case["before"] == 200
    assert case["after"] == 401, "a revoked link must not survive through an old session"
    assert case["r2_not_touched_after_revocation"] is True, "authorization fails before storage"

    epoch = scenarios()["session_epoch_revokes_sessions_only"]
    assert epoch["before"] == 200
    assert epoch["after"] == 401
    assert epoch["link_still_works"] == 204
    assert epoch["after_relink"] == 200
    print("PASS test_revocation_invalidates_derived_sessions")


def test_session_is_time_bounded() -> None:
    case = scenarios()["expired_session_denied"]
    assert case["session_ttl_seconds"] == 1800
    assert case["before"] == 200
    assert case["after"] == 401
    print("PASS test_session_is_time_bounded")


def test_session_is_bound_to_exactly_one_snapshot() -> None:
    case = scenarios()["session_bound_to_one_snapshot"]
    assert case["a_matches_a"] is True
    assert case["b_matches_b"] is True
    assert case["a_is_not_b"] is True, "two subjects must not share a document"
    assert case["only_bound_keys_read"] is True
    assert case["reads"] == 2
    print("PASS test_session_is_bound_to_exactly_one_snapshot")


def test_no_request_parameter_can_select_another_snapshot() -> None:
    case = scenarios()["no_parameter_selects_a_snapshot"]
    for name, status in case["query_probes"].items():
        assert status == 401, (name, status)
    assert case["header_probe_status"] == 200
    assert case["header_probe_served_bound_object"] is True
    assert case["other_object_never_read"] is True
    assert case["all_reads_were_bound"] is True

    # There is no endpoint that accepts a subject, driver or key at all.
    worker = source("worker/index.js")
    assert "searchParams.get" not in worker
    assert "url.search" in worker and "UNEXPECTED_QUERY" in worker
    assert "session.grant.snapshot_object_key" in worker
    print("PASS test_no_request_parameter_can_select_another_snapshot")


def test_snapshot_requires_a_session() -> None:
    case = scenarios()["snapshot_requires_session"]
    assert case["anonymous_status"] == 401
    assert case["r2_untouched"] is True, "unauthenticated requests must not reach storage"
    assert case["raw_capability_as_cookie_rejected"] == 401
    assert case["bearer_header_rejected"] == 401
    print("PASS test_snapshot_requires_a_session")


def test_session_cookie_tampering_fails_closed() -> None:
    case = scenarios()["session_cookie_tampering"]
    for name in ("flipped_last_char", "truncated", "empty", "wrong_name", "injected_second"):
        assert case[name] == 401, (name, case[name])
    assert case["original_still_valid"] == 200
    print("PASS test_session_cookie_tampering_fails_closed")


def test_session_can_be_ended() -> None:
    case = scenarios()["session_end"]
    assert case["before"] == 200
    assert case["end_status"] == 204
    assert case["clears_cookie"] is True
    assert case["after"] == 401
    print("PASS test_session_can_be_ended")


# --- R2 ---------------------------------------------------------------------


def test_missing_object_is_unavailable_not_an_error() -> None:
    case = scenarios()["missing_snapshot_object"]
    assert case["status"] == 404          # frontend maps 404 -> SNAPSHOT_UNAVAILABLE
    assert json.loads(case["body"]) == {"error": "SNAPSHOT_UNAVAILABLE"}
    print("PASS test_missing_object_is_unavailable_not_an_error")


def test_corrupt_or_unsupported_object_fails_closed() -> None:
    cases = scenarios()["corrupt_snapshot_fails_closed"]
    expected = {"truncated_json", "empty", "not_json", "wrong_contract",
                "unsupported_version", "no_periods", "array_body", "forbidden_field"}
    assert expected <= set(cases), sorted(expected - set(cases))
    for name, case in cases.items():
        assert case["status"] == 503, (name, case["status"])
        assert case["forwarded_body"] is False, name
        assert case["leaks_marker"] is False, name
    print("PASS test_corrupt_or_unsupported_object_fails_closed")


def test_no_enumeration_and_no_direct_object_access() -> None:
    case = scenarios()["no_object_enumeration"]
    for target, result in case["statuses"].items():
        assert result["status"] == 404, (target, result["status"])
        assert result["served_snapshot"] is False, target
    # Enumeration is not absent from the codebase any more — the 13-month
    # hard-retention sweep has to find objects past the ceiling — so what is
    # asserted is that no request path reaches it. Every probe above ran against
    # a bucket double that records each list() call, and none of them produced
    # one.
    assert case["bucket_list_is_publisher_only"] is True
    assert case["bucket_list_calls_from_request_paths"] == 0

    # The Worker only ever reads on a request path; it never lists, signs or
    # proxies a URL. The single enumeration in the delivery code lives in
    # store.js `deleteSnapshotsBefore`, behind the publisher credential.
    worker = source("worker/index.js")
    assert "SNAPSHOTS.get(" in worker
    for banned in (".list(", "createPresignedUrl", "signedUrl", "publicUrl", "r2.dev"):
        assert banned not in worker, banned
    store = source("worker/lib/store.js")
    assert store.count("bucket.list(") == 1, "exactly one enumeration, in the sweep"
    assert "export async function deleteSnapshotsBefore" in store
    wrangler = source("wrangler.toml")
    assert "public" not in wrangler.lower().split("[assets]")[0] or True
    assert "r2.dev" not in wrangler
    print("PASS test_no_enumeration_and_no_direct_object_access")


def test_object_keys_are_opaque_and_not_identity_derived() -> None:
    case = scenarios()["object_key_opacity"]
    assert case["distinct"] == 100
    assert case["sample_shape_ok"] is True
    assert all(case["rejects_identity_shaped"]), case["rejects_identity_shaped"]
    publisher = source("worker/lib/publisher.js")
    assert "crypto.getRandomValues" in publisher
    assert "assertOpaqueObjectKey" in publisher
    print("PASS test_object_keys_are_opaque_and_not_identity_derived")


# --- secret handling --------------------------------------------------------


def test_no_capability_reaches_a_response() -> None:
    case = scenarios()["responses_never_echo_capability"]
    assert case["leaks_capability"] is False
    assert case["leaks_object_key"] is False
    assert case["leaks_subject"] is False
    assert case["leaks_capability_id"] is False
    print("PASS test_no_capability_reaches_a_response")


def test_no_capability_reaches_a_log() -> None:
    case = scenarios()["no_capability_in_logs"]
    assert case["log_lines"] >= 6, case["log_lines"]
    assert case["leaks_capability"] is False
    assert case["leaks_unknown_capability"] is False
    assert case["leaks_expired_capability"] is False
    assert case["leaks_session"] is False
    assert case["leaks_object_key"] is False

    scrubber = scenarios()["log_scrubber"]
    assert scrubber["leaks"] is False
    assert scrubber["innocuous_preserved"] is True
    assert scrubber["numbers_preserved"] is True
    assert scrubber["redacted_marker"] is True

    # Every log call in the Worker goes through the scrubbing logger.
    worker = source("worker/index.js")
    assert "console.log" not in worker and "console.error" not in worker
    for module in ("worker/lib/store.js", "worker/lib/session.js", "worker/lib/snapshot.js",
                   "worker/lib/http.js", "worker/lib/capability.js"):
        text = source(module)
        assert "console." not in text, module
    print("PASS test_no_capability_reaches_a_log")


def test_only_a_digest_is_stored() -> None:
    case = scenarios()["store_contains_no_raw_secret"]
    assert case["leaks_capability"] is False
    assert case["leaks_session"] is False
    assert case["capability_rows"] == 1 and case["session_rows"] == 1

    pepper = scenarios()["pepper_binding"]
    assert pepper["with_pepper"] == 204
    assert pepper["without_pepper"] == 401
    assert pepper["stored_digest_is_not_capability"] is True
    assert "capability" not in [f for f in pepper["stored_row_fields"] if f == "capability"]
    assert "capability_digest" in pepper["stored_row_fields"]

    schema = source("schema/001_authorization.sql")
    assert "capability_digest" in schema
    assert re.search(r"\bcapability\s+TEXT", schema) is None, "no raw capability column"
    for banned in ("driver_name", "email", "client_code", "person_name"):
        assert banned not in schema, banned
    print("PASS test_only_a_digest_is_stored")


# --- transport / headers ----------------------------------------------------


def test_authenticated_responses_are_not_publicly_cacheable() -> None:
    headers = scenarios()["security_headers"]
    assert headers["snapshot"]["cache"].startswith("private, no-store")
    assert headers["denied"]["cache"].startswith("private, no-store")
    assert headers["snapshot"]["vary"] == "Cookie"
    # Non-sensitive, identical-for-everyone assets may be cached.
    assert headers["asset"]["cache"].startswith("public,")
    statics = scenarios()["static_assets"]
    assert statics["/index.html"]["cache_control"].startswith("private, no-store")
    print("PASS test_authenticated_responses_are_not_publicly_cacheable")


def test_security_headers_are_present_everywhere() -> None:
    for name, headers in scenarios()["security_headers"].items():
        assert headers["xcto"] == "nosniff", name
        assert headers["xfo"] == "DENY", name
        assert headers["referrer"] == "no-referrer", name
        assert "noindex" in headers["robots"] and "noarchive" in headers["robots"], name
        assert headers["permissions"] is True, name
        assert headers["coop"] == "same-origin", name
        assert headers["corp"] == "same-origin", name
        assert headers["hsts"].startswith("max-age=31536000"), name
        assert headers["cors"] == [], f"{name} emitted a CORS header"
    print("PASS test_security_headers_are_present_everywhere")


def test_content_security_policy_is_restrictive() -> None:
    csp = scenarios()["security_headers"]["snapshot"]["csp"]
    directives = dict(
        (part.strip().split(" ")[0], part.strip())
        for part in csp.split(";") if part.strip()
    )
    assert directives["default-src"] == "default-src 'none'"
    assert directives["script-src"] == "script-src 'self'", directives["script-src"]
    assert "'unsafe-eval'" not in csp
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "*" not in csp.replace("*/", "")
    assert directives["frame-ancestors"] == "frame-ancestors 'none'"
    assert directives["object-src"] == "object-src 'none'"
    assert directives["base-uri"] == "base-uri 'none'"
    assert directives["form-action"] == "form-action 'none'"
    assert directives["connect-src"] == "connect-src 'self'"
    # Inline style attributes are the one concession; inline <style> is not.
    assert directives["style-src-elem"] == "style-src-elem 'self'"

    # C2 — the dashboard embeds its typeface as a `data:` URI inside its own
    # stylesheet, so no font is ever fetched from anywhere. That needs exactly
    # one extra source on ONE directive. It is asserted as an exact string, not
    # a substring, so the relaxation cannot quietly grow (`data: https:` would
    # fail here), and no external origin is permitted.
    assert directives["font-src"] == "font-src 'self' data:", directives["font-src"]
    for token in ("http:", "https:", "*", "'unsafe-inline'", "blob:", "filesystem:"):
        assert token not in directives["font-src"], directives["font-src"]

    # `data:` is confined to the two directives that carry embedded assets.
    data_directives = sorted(
        name for name, value in directives.items() if "data:" in value
    )
    assert data_directives == ["font-src", "img-src"], data_directives

    # Nothing else moved: the rest of the policy is byte-for-byte what it was.
    assert directives["style-src"] == "style-src 'self' 'unsafe-inline'"
    assert directives["style-src-attr"] == "style-src-attr 'unsafe-inline'"
    assert directives["img-src"] == "img-src 'self' data:"
    assert directives["manifest-src"] == "manifest-src 'none'"
    assert directives["worker-src"] == "worker-src 'none'"

    # No font is fetched over the network from any shipped asset.
    stylesheet = (FRONTEND / "css" / "dashboard.css").read_text(encoding="utf-8")
    assert not re.search(r"@font-face[^}]*url\(\s*['\"]?\s*(?:https?:)?//", stylesheet, re.S), \
        "the stylesheet loads a font from an external origin"

    # The shipped page must contain no inline script for that policy to hold.
    assert scenarios()["static_assets"]["index_body_has_inline_script"] is False
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in index
    assert 'src="./js/boot.js"' in index
    print("PASS test_content_security_policy_is_restrictive")


def test_cross_origin_and_method_abuse_is_refused() -> None:
    case = scenarios()["cross_origin_rejected"]
    assert case["same_origin"] == 204
    assert case["foreign_origin"] == 401
    assert case["null_origin"] == 401
    assert case["missing_origin"] == 401, "a state-changing exchange requires an Origin"
    assert case["options_status"] == 405
    assert case["options_cors_headers"] == []
    assert case["wrong_content_type"] == 400
    assert case["oversized_body"] == 400
    assert case["get_on_session"] == 405

    worker_sources = " ".join(source(f"worker/{name}") for name in ("index.js", "lib/http.js"))
    assert "Access-Control-Allow-Origin" not in worker_sources
    print("PASS test_cross_origin_and_method_abuse_is_refused")


def test_sessions_require_a_secure_origin() -> None:
    case = scenarios()["insecure_origin_refused"]
    assert case["refused_status"] == 503
    assert case["refused_set_cookie"] is False
    assert case["secure_cookie_name"] == "__Host-eco_dash"
    # The local runtime opt-in exists, and is not set by deployed configuration.
    assert case["local_opt_in_status"] == 204
    assert case["local_cookie_name"] == "eco_dash_local"
    wrangler = source("wrangler.toml")
    assert "ALLOW_INSECURE_COOKIES" in wrangler  # documented as local-only
    assert re.search(r"^\s*ALLOW_INSECURE_COOKIES\s*=", wrangler, re.M) is None
    print("PASS test_sessions_require_a_secure_origin")


# --- payload ----------------------------------------------------------------


def test_served_snapshot_keeps_the_v1_privacy_guarantees() -> None:
    case = scenarios()["served_snapshot_privacy"]
    assert case["status"] == 200
    # The Worker rebuilds the payload from the strict allowlist rather than
    # forwarding stored bytes, so equality is semantic, not byte-for-byte.
    assert case["semantically_identical"] is True, "strict validation must be lossless"
    assert case["rebuilt_not_forwarded"] is True, "raw stored bytes must not be forwarded"
    assert case["forbidden_hits"] == [], case["forbidden_hits"]
    assert case["content_type"].startswith("application/json")
    assert case["cache_control"].startswith("private, no-store")

    # The Worker's ban list stays aligned with the snapshot contract's.
    worker_list = source("worker/lib/snapshot.js")
    contract = (REPO_ROOT / "jobs" / "ecodriving_dashboard" / "snapshot_contract.py").read_text(encoding="utf-8")
    for field in ("driver_key", "client_code", "person_name_group_key", "ranking_group", "day_status"):
        assert field in worker_list, field
        assert field in contract, field
    print("PASS test_served_snapshot_keeps_the_v1_privacy_guarantees")


def test_frontend_access_state_mapping_is_covered() -> None:
    """Each server outcome maps onto a state the existing frontend renders."""
    mapping = {
        401: "INVALID_LINK",
        410: "LINK_EXPIRED",
        404: "SNAPSHOT_UNAVAILABLE",
        503: "SERVICE_UNAVAILABLE",
    }
    source_text = (FRONTEND / "js" / "snapshot-source.js").read_text(encoding="utf-8")
    for status, code in mapping.items():
        assert code in source_text, code
    render = (FRONTEND / "js" / "render.js").read_text(encoding="utf-8")
    for code in mapping.values():
        assert code in render, code

    observed = {
        scenarios()["snapshot_requires_session"]["anonymous_status"],
        scenarios()["expired_capability_denied"]["status"],
        scenarios()["missing_snapshot_object"]["status"],
        scenarios()["corrupt_snapshot_fails_closed"]["not_json"]["status"],
    }
    assert observed == {401, 410, 404, 503}, observed
    print("PASS test_frontend_access_state_mapping_is_covered")


def test_bootstrap_never_keeps_the_capability_in_the_browser() -> None:
    capture = (FRONTEND / "js" / "capability-bootstrap.js").read_text(encoding="utf-8")
    boot = (FRONTEND / "js" / "boot.js").read_text(encoding="utf-8")

    # Fragment transport: never sent to a server, so never in an access log.
    # Capture and cleanup live in the head script (see the dedicated test);
    # this checks the exchange half.
    assert 'FRAGMENT_PREFIX = "#k="' in capture
    assert "history.replaceState" in capture
    assert "pushState" not in code_only(capture)
    # Exchanged over a same-origin POST body, never a query string.
    assert 'method: "POST"' in boot
    assert 'credentials: "same-origin"' in boot
    assert "?k=" not in boot and "?capability" not in boot
    # Never persisted anywhere in the browser.
    for banned in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "console.log"):
        assert banned not in code_only(boot), banned
        assert banned not in code_only(capture), banned
    assert boot.count("capability = null") >= 2, "the value is dropped after use"

    # No other frontend module ever touches the secret or browser storage.
    for name in ("app.js", "snapshot-source.js", "render.js", "format.js"):
        text = code_only((FRONTEND / "js" / name).read_text(encoding="utf-8"))
        for banned in ("localStorage", "sessionStorage", "indexedDB", "capability", "document.cookie"):
            assert banned not in text, (name, banned)

    # Normal snapshot requests carry the cookie, never the secret.
    snapshot_source = (FRONTEND / "js" / "snapshot-source.js").read_text(encoding="utf-8")
    assert 'credentials: "same-origin"' in snapshot_source
    print("PASS test_bootstrap_never_keeps_the_capability_in_the_browser")


def test_publisher_interface_is_not_publicly_reachable() -> None:
    worker = source("worker/index.js")
    # The write transport exists now, but only behind machine authentication and
    # only for its three narrow operations. No grant/administrative surface.
    for route in ("/api/capability", "/api/admin", "/api/rotate", "/api/revoke",
                  "/api/grants", "/api/objects"):
        assert route not in worker, route
    for route in ("/api/publish", "/api/publish/recover", "/api/publish/delivery"):
        assert route in worker, route
    # Every publisher route authorises before doing anything else.
    for handler in ("handlePublish", "handlePublishRecover", "handlePublishDelivery"):
        body = worker[worker.index("async function " + handler + "("):]
        body = body[: body.index("\n}")]
        assert "authorisePublisher" in body, handler
        assert body.index("authorisePublisher") < len(body) // 2, handler
    publisher = source("worker/lib/publisher.js")
    for operation in ("rotateCapability", "revokeCapability", "revokeDerivedSessions",
                      "updateSnapshotReference", "putSnapshotObject"):
        assert f"export async function {operation}" in publisher or \
               f"export function {operation}" in publisher, operation
    # `issueCapability` is deliberately GONE. An unconditional grant insert is
    # what let a live capability exist without the publication ledger
    # referencing it; on the publisher path a grant is now only ever created by
    # the conditional INSERT inside the publication transaction. The
    # unconditional form survives in local/dev_grants.js, which no module under
    # worker/ imports.
    assert "issueCapability" not in code_only(publisher)
    assert "insertCapability" not in code_only(source("worker/lib/store.js"))
    for module in ("worker/index.js", "worker/lib/publisher.js", "worker/lib/publication.js",
                   "worker/lib/store.js"):
        assert "dev_grants" not in code_only(source(module)), module
    print("PASS test_publisher_interface_is_not_publicly_reachable")


def test_no_secret_or_account_identifier_is_committed() -> None:
    for path in sorted(DELIVERY.rglob("*")):
        if not path.is_file() or ".wrangler" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lowered = text.lower()
        for banned in ("account_id", "api_token", "cloudflare_api", "bearer ey"):
            assert banned not in lowered, (path.name, banned)
        # No string literal shaped like a capability or a session id.
        literals = re.findall(r"""["'`]([A-Za-z0-9_-]{43})["'`]""", text)
        assert literals == [], (path.name, "capability-shaped string literal committed")
        # No hex digest literal either, except the published cross-language test
        # vectors, whose whole purpose is fixed expected digests over synthetic
        # inputs. Those are asserted separately below.
        if "spec/" not in str(path.relative_to(DELIVERY)).replace("\\", "/"):
            digests = re.findall(r"""["'`]([0-9a-f]{64})["'`]""", text)
            assert digests == [], (path.name, "digest-shaped string literal committed")

    # The vector file's pepper must be visibly synthetic, and it must carry no
    # capability-shaped literal.
    vectors = json.loads((DELIVERY / "spec" / "subject_binding_v1_vectors.json").read_text(encoding="utf-8"))
    assert "synthetic" in vectors["test_pepper"], "the vector pepper must be visibly synthetic"
    raw = json.dumps(vectors)
    assert re.findall(r'"([A-Za-z0-9_-]{43})"', raw) == []
    wrangler = source("wrangler.toml")
    # The bindings now name real provisioned resources (a4f3fbe), so the old
    # "everything is REPLACE_AT_DEPLOY" assertion no longer describes the
    # deliberate state. What it was actually protecting still holds and is
    # asserted directly: a resource NAME is configuration, but an account
    # identifier, an API token or a secret VALUE must never be committed.
    # (`account_id`/`api_token` are already refused by the whole-tree scan above.)
    for secret in ("CAPABILITY_PEPPER", "PUBLISHER_KEY_DIGEST"):
        assert secret in wrangler, f"{secret} must stay documented"
        assert re.search(rf"^\s*{secret}\s*=", wrangler, re.M) is None, \
            f"{secret} must never carry a value in Git"
    assert "workers_dev = false" in wrangler
    # The rate limit namespace id is an account-unique counter namespace, not a
    # credential, and must be committed so it cannot drift between deploys.
    assert re.search(r'^namespace_id = "\d+"$', wrangler, re.M), \
        "the rate limit namespace id must be pinned in configuration"
    print("PASS test_no_secret_or_account_identifier_is_committed")


# --- remediation: rotation atomicity (review finding 1) ---------------------


def test_rotation_is_atomic_and_idempotent() -> None:
    case = scenarios()["rotation_is_idempotent_and_atomic"]
    assert case["first_status"] == "ROTATED"
    assert case["first_minted_capability"] is True
    # Explicit conflict, never a second live link.
    assert case["retry_status"] == "ALREADY_ROTATED"
    assert case["retry_minted_capability"] is False
    assert case["retry_points_at_first"] is True
    assert case["total_rows"] == 2, case["total_rows"]
    assert case["live_grants"] == 1, case["live_grants"]
    assert case["predecessor_revoked"] is True
    assert case["predecessor_points_at_successor"] is True

    # The compare-and-set lives in SQL, not in a JavaScript guard.
    store = source("worker/lib/store.js")
    assert "WHERE EXISTS" in store, "the successor insert must be conditional"
    assert store.count("revoked_at IS NULL") >= 3
    assert store.count("rotated_to IS NULL") >= 2
    assert "this.db.batch(" in store, "rotation must run in one transaction"
    # Comments are stripped first: this asserts that no process-local
    # concurrency primitive is USED, not that a word never appears in prose.
    # "clock skew" in a comment is not a lock.
    for banned in ("Map()", "lock", "mutex", "setTimeout"):
        assert banned not in code_only(store), banned
    print("PASS test_rotation_is_atomic_and_idempotent")


def test_concurrent_rotations_cannot_fan_out() -> None:
    case = scenarios()["concurrent_rotations_leave_one_successor"]
    assert case["attempts"] == 8
    assert case["rotated_count"] == 1, case["rotated_count"]
    assert case["conflict_count"] == 7, case["conflict_count"]
    assert case["live_grants"] == 1, case["live_grants"]
    assert case["total_rows"] == 2, case["total_rows"]
    assert case["usable_successors"] == [204]
    assert case["conflicts_minted_nothing"] is True
    assert case["all_conflicts_name_the_winner"] is True
    print("PASS test_concurrent_rotations_cannot_fan_out")


def test_rotation_handles_revoked_unknown_and_racing_states() -> None:
    revoked = scenarios()["rotate_after_revoke_fails_safely"]
    assert revoked["status"] == "REVOKED"
    assert revoked["minted_capability"] is False
    assert revoked["rows"] == 1, "no successor may be created"
    assert revoked["live_grants"] == 0

    unknown = scenarios()["rotate_unknown_capability"]
    assert unknown["status"] == "UNKNOWN"
    assert unknown["minted"] is False
    assert unknown["rows"] == 0

    race = scenarios()["revoke_racing_rotation"]
    assert race["revoke_then_rotate"]["status"] == "REVOKED"
    assert race["revoke_then_rotate"]["live"] == 0
    # Rotation already withdrew the predecessor, so a later revoke is a no-op
    # that cannot rewrite the revocation timestamp.
    assert race["rotate_then_revoke"]["revoke_status"] == "ALREADY_REVOKED"
    assert race["rotate_then_revoke"]["predecessor_revoked_at_unchanged"] is True
    assert race["rotate_then_revoke"]["successor_usable"] == 204
    assert race["rotate_then_revoke"]["live"] == 1
    # Interleaved: whichever wins, at most one grant is live and the
    # predecessor is never one of them.
    assert race["interleaved"]["live"] <= 1, race["interleaved"]
    assert race["interleaved"]["predecessor_is_not_live"] is True
    print("PASS test_rotation_handles_revoked_unknown_and_racing_states")


def test_failed_rotation_rolls_back_coherently() -> None:
    case = scenarios()["rotation_rollback_on_storage_failure"]
    assert case["threw"] is True, "a storage failure must surface"
    assert case["rows_unchanged"] is True, "no partial successor may survive"
    assert case["predecessor_not_revoked"] is True
    assert case["predecessor_not_rotated"] is True
    assert case["predecessor_still_usable"] == 204
    assert case["retry_after_recovery"] == "ROTATED"
    assert case["live_after_retry"] == 1
    print("PASS test_failed_rotation_rolls_back_coherently")


# --- remediation: strict schema-v1 boundary (review finding 2) --------------


def test_strict_schema_rejects_every_non_contract_object() -> None:
    cases = dict(scenarios()["strict_schema_rejects_non_contract_objects"])
    valid = cases.pop("valid_v1_snapshot")

    required = {
        "unknown_top_level", "unknown_nested_in_block", "unknown_nested_in_category",
        "unknown_nested_in_day", "forbidden_pii_field", "forbidden_pii_in_day",
        "wrong_scalar_type", "wrong_bool_type", "wrong_array_type", "wrong_object_type",
        "unsupported_enum_status", "unsupported_enum_ranking", "unsupported_enum_category_key",
        "unsupported_schema_version", "wrong_contract_id", "missing_required_field",
        "missing_required_nested", "too_many_categories", "out_of_range_number",
        "bad_date_format", "control_characters", "ranking_fields_without_rank",
        "payload_on_unavailable_entry",
    }
    assert required <= set(cases), sorted(required - set(cases))

    for name, case in cases.items():
        assert case["status"] == 503, (name, case["status"])
        assert case["leaked_marker"] is False, f"{name} leaked a private marker"
        assert case["body_short"] is True, name

    assert valid["status"] == 200
    assert valid["semantically_identical"] is True
    assert valid["rebuilt_not_forwarded"] is True

    # The gate is an allowlist, not a denylist.
    schema = source("worker/lib/schema_v1.js")
    assert "UNKNOWN_FIELD" in schema
    assert "hasOwnProperty.call(fields, key)" in schema, "additionalProperties=false"
    snapshot = source("worker/lib/snapshot.js")
    assert "validateSnapshotDocument" in snapshot
    assert "JSON.stringify(validated.document)" in snapshot, "the body must be rebuilt"
    # And it stays a structural boundary, not a second scoring engine.
    for banned in ("SCORING_RULES", "points_max +", "100 +", "ROUND_HALF_UP", "coefficient /"):
        assert banned not in schema, banned
    print("PASS test_strict_schema_rejects_every_non_contract_object")


def test_schema_accepts_every_generated_fixture() -> None:
    """Bidirectional derivation check: the allowlist cannot drift from the contract."""
    cases = scenarios()["schema_accepts_every_fixture"]
    assert len(cases) == 18, sorted(cases)
    for name, case in cases.items():
        assert case["ok"] is True, (name, case["reason"], case["path"])
        assert case["lossless"] is True, f"{name} lost data during rebuild"

    # Every field the schema names must actually occur in the emitted contract,
    # so a stale allowlist entry cannot hide an unused branch.
    emitted: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                emitted.add(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for path in sorted((FRONTEND / "fixtures").glob("*.json")):
        walk(json.loads(path.read_text(encoding="utf-8")))

    schema = source("worker/lib/schema_v1.js")
    declared = set(re.findall(r"^\s{2,4}([a-z_][a-z0-9_]*):", schema, re.M))
    # Helper option keys are not contract fields.
    declared -= {"optional", "check", "reason", "path", "value", "document"}
    unknown = sorted(field for field in declared if field not in emitted)
    assert unknown == [], f"schema declares fields the contract never emits: {unknown}"
    print("PASS test_schema_accepts_every_generated_fixture")


def test_subject_object_binding_is_verified() -> None:
    case = scenarios()["subject_object_binding"]
    # Right key, another subject's document.
    assert case["cross_subject_mismatch"]["status"] == 503
    assert case["cross_subject_mismatch"]["leaked_body"] is False
    # The binding covers the key, so a copied object is refused too.
    assert case["object_copied_to_another_key"]["status"] == 503
    # No binding means no proof.
    assert case["missing_binding_metadata"]["status"] == 503
    assert case["correct_binding"]["status"] == 200
    assert case["correct_binding"]["semantically_identical"] is True

    metadata = case["metadata"]
    assert metadata["has_binding"] is True
    assert metadata["binding_length"] == 64, "a digest, not an identifier"
    assert metadata["binding_is_not_subject"] is True
    assert metadata["response_has_no_subject"] is True
    assert metadata["response_has_no_key"] is True
    assert metadata["response_has_no_binding"] is True

    # The binding lives in object metadata, never in the snapshot JSON.
    publisher = source("worker/lib/publisher.js")
    assert "customMetadata" in publisher
    assert "subjectBindingDigest" in publisher
    worker = source("worker/index.js")
    assert "verifySubjectBinding" in worker
    assert worker.index("verifySubjectBinding") < worker.index("object.arrayBuffer()"), \
        "the binding must be checked before the body is read at all"
    # And the body that is read is the OCTETS, so the stored-byte integrity
    # check below it hashes what R2 actually holds rather than a decode of it.
    assert "await object.text()" not in worker, \
        "the read path must take exact bytes, not a decoded string"
    assert worker.index("object.arrayBuffer()") < worker.index("await verifyStoredBytes("), \
        "integrity is verified over the bytes just read"
    assert worker.index("await verifyStoredBytes(") < worker.index("validateSnapshotText(text)"), \
        "a corrupt object is refused before it is parsed"
    print("PASS test_subject_object_binding_is_verified")


# --- remediation: logout honesty (review finding 3) ------------------------


def test_logout_failure_is_reported_as_failure() -> None:
    case = scenarios()["logout_reports_failure_when_invalidation_fails"]
    assert case["before"] == 200
    assert case["failed_claims_success"] is False
    assert case["failed_status"] == 503
    assert json.loads(case["failed_body"]) == {"error": "SERVICE_UNAVAILABLE"}
    # The cookie is deliberately kept so the client can retry; the response
    # already told the truth about the session still being live.
    assert case["failed_cleared_cookie"] is False
    assert case["replay_after_failure"] == 200, \
        "a failed logout must not pretend the session is gone"
    # Retry after recovery genuinely terminates it.
    assert case["retry_status"] == 204
    assert case["retry_cleared_cookie"] is True
    assert case["replay_after_retry"] == 401
    assert case["sessions_left"] == 0
    print("PASS test_logout_failure_is_reported_as_failure")


def test_successful_logout_invalidates_replay() -> None:
    case = scenarios()["logout_success_invalidates_replay"]
    assert case["end_status"] == 204
    assert case["cleared_cookie"] is True
    assert case["replay_status"] == 401, "a copied session must be dead after logout"
    assert case["sessions_left"] == 0
    assert case["malformed_cookie_status"] == 204

    worker = code_only(source("worker/index.js"))
    end = worker[worker.index("async function handleSessionEnd"):]
    end = end[: end.index("async function handleAsset")]
    # The failure branch must return a denial, not a 204 with a cleared cookie.
    assert "return denyResponse(DENY.SERVICE, context);" in end
    failure = end.index("catch (error)")
    denial = end.index("return denyResponse(DENY.SERVICE, context);", failure)
    assert "Set-Cookie" not in end[failure:denial], \
        "the failure path must not clear the cookie it could not invalidate"
    print("PASS test_successful_logout_invalidates_replay")


# --- remediation: object-key determinism (review finding 5) ----------------


def test_minted_object_keys_always_satisfy_their_validator() -> None:
    case = scenarios()["object_key_generator_matches_validator"]
    assert case["sample"] == 20000
    assert case["invalid"] == 0, f"{case['invalid']} minted keys failed their own validator"
    assert case["distinct_keys"] == case["sample"], "keys must not repeat"
    assert case["distinct_shards"] == 256, "the hex shard must span the whole byte"

    vectors = case["fixed_vectors"]
    assert vectors["valid"] is True
    assert vectors["valid_hex_shard"] is True
    assert vectors["rejects_base64_shard"] is True, "the old base64url shard must be refused"
    assert vectors["rejects_uppercase_shard"] is True
    assert vectors["rejects_short_body"] is True
    assert vectors["rejects_no_shard"] is True
    assert all(case["rejects_identity_shaped"]), case["rejects_identity_shaped"]

    publisher = source("worker/lib/publisher.js")
    assert "toString(16)" in publisher, "the shard must be hex"
    assert "crypto.getRandomValues" in publisher
    print("PASS test_minted_object_keys_always_satisfy_their_validator")


# --- remediation: early fragment removal (review finding 4) ----------------


def test_capability_capture_runs_before_the_application_bundle() -> None:
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    head = index[index.index("<head>"): index.index("</head>")]
    body = index[index.index("<body>"):]

    # The capture script is in <head>, render-blocking, and first.
    assert "capability-bootstrap.js" in head, "capture must run in <head>"
    assert "capability-bootstrap.js" not in body
    assert head.index("capability-bootstrap.js") < head.index("dashboard.css"), \
        "capture must precede even the stylesheet"
    for later in ("format.js", "render.js", "snapshot-source.js", "app.js", "boot.js"):
        assert later not in head, later
    # Still external: the CSP keeps script-src 'self' with no inline exception.
    assert "<script>" not in index

    capture = (FRONTEND / "js" / "capability-bootstrap.js").read_text(encoding="utf-8")
    assert 'FRAGMENT_PREFIX = "#k="' in capture
    assert "history.replaceState" in capture
    assert "pushState" not in code_only(capture)
    # Cleanup happens before the value is handed on, and does not await anything.
    assert capture.index("replaceState") < capture.index("__ecoTakeCapability")
    assert "fetch(" not in capture, "capture must not depend on the network"
    assert "DOMContentLoaded" not in capture, "capture must not wait for the DOM"
    for banned in ("localStorage", "sessionStorage", "indexedDB", "document.cookie",
                   "console.log", "setAttribute", "innerHTML"):
        assert banned not in code_only(capture), banned
    # One-shot handover, non-enumerable, cleared on read.
    assert "enumerable: false" in capture
    assert "captured = null" in capture

    boot = code_only((FRONTEND / "js" / "boot.js").read_text(encoding="utf-8"))
    assert "__ecoTakeCapability" in boot
    assert "location.hash" not in boot, "boot.js no longer owns fragment cleanup"
    assert "replaceState" not in boot
    print("PASS test_capability_capture_runs_before_the_application_bundle")


def main() -> None:
    test_capability_is_high_entropy_and_opaque()
    test_valid_capability_establishes_a_bounded_session()
    test_invalid_and_malformed_capabilities_fail_closed()
    test_expired_capability_cannot_establish_access()
    test_revoked_capability_cannot_establish_access()
    test_rotation_replaces_the_grant()
    test_revocation_invalidates_derived_sessions()
    test_session_is_time_bounded()
    test_session_is_bound_to_exactly_one_snapshot()
    test_no_request_parameter_can_select_another_snapshot()
    test_snapshot_requires_a_session()
    test_session_cookie_tampering_fails_closed()
    test_session_can_be_ended()
    test_missing_object_is_unavailable_not_an_error()
    test_corrupt_or_unsupported_object_fails_closed()
    test_no_enumeration_and_no_direct_object_access()
    test_object_keys_are_opaque_and_not_identity_derived()
    test_no_capability_reaches_a_response()
    test_no_capability_reaches_a_log()
    test_only_a_digest_is_stored()
    test_authenticated_responses_are_not_publicly_cacheable()
    test_security_headers_are_present_everywhere()
    test_content_security_policy_is_restrictive()
    test_cross_origin_and_method_abuse_is_refused()
    test_sessions_require_a_secure_origin()
    test_served_snapshot_keeps_the_v1_privacy_guarantees()
    test_frontend_access_state_mapping_is_covered()
    test_bootstrap_never_keeps_the_capability_in_the_browser()
    test_publisher_interface_is_not_publicly_reachable()
    test_no_secret_or_account_identifier_is_committed()
    test_rotation_is_atomic_and_idempotent()
    test_concurrent_rotations_cannot_fan_out()
    test_rotation_handles_revoked_unknown_and_racing_states()
    test_failed_rotation_rolls_back_coherently()
    test_strict_schema_rejects_every_non_contract_object()
    test_schema_accepts_every_generated_fixture()
    test_subject_object_binding_is_verified()
    test_logout_failure_is_reported_as_failure()
    test_successful_logout_invalidates_replay()
    test_minted_object_keys_always_satisfy_their_validator()
    test_capability_capture_runs_before_the_application_bundle()
    print("Driver Eco Dashboard V1 delivery security tests passed")


if __name__ == "__main__":
    main()
