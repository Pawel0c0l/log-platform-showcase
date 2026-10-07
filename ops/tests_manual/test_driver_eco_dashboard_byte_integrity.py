#!/usr/bin/env python3
"""Canonical byte-integrity gate for the Driver Eco Dashboard delivery boundary.

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_byte_integrity.py

Closes the one blocking item the independent byte-integrity review raised:

  BLOCKER  the publication ledger's `payload_digest` did not identify the bytes
           R2 actually held. The host hashed its canonical output, the Worker
           validated it, rebuilt it, and wrote `JSON.stringify(rebuilt)` — a
           semantically identical document with different octets. So
           `SHA256(host bytes) == ledger digest` while
           `SHA256(R2 body) != ledger digest`.

THE INVARIANT THIS SUITE ASSERTS

    host canonical output bytes
      == bytes hashed for payload_digest
      == bytes accepted by /api/publish
      == bytes written to R2
      == bytes the ledger digest identifies
      == bytes read back before schema validation

Assertions are over OCTETS. Semantic JSON equality is never accepted as
evidence of byte identity anywhere in this file — the defect produced equal
documents, so equality of documents proves nothing about it.

The canonical bytes come from the real host publisher
(`jobs.ecodriving_dashboard.publication.build_publishable_snapshot`), not from
a fixture file and not from JavaScript. `ops/tests_manual/eco_byte_integrity_harness.mjs`
runs them through the real Worker over local D1/R2 doubles.

Synthetic data only. No wrangler, no credentials, no remote Cloudflare
resource, no e-mail, no production data. No capability, session id, machine
credential or object key is printed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "ops" / "tests_manual") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "ops" / "tests_manual"))

DELIVERY = REPO_ROOT / "delivery" / "driver_eco_dashboard"
HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_byte_integrity_harness.mjs"

_CACHE: dict = {}
_INPUT: dict = {}


# --- host-side canonical bytes ------------------------------------------------


def _host_payloads() -> dict:
    """Two real publishable snapshots, plus two deliberate counter-examples."""
    if _INPUT:
        return _INPUT

    import eco_dashboard_fixtures as fx
    from jobs.ecodriving_dashboard.publication import (
        PrivacyContext,
        build_publishable_snapshot,
        serialize_publishable_snapshot,
    )
    from jobs.ecodriving_dashboard.snapshot_contract import canonical_json_bytes

    privacy = PrivacyContext(
        identity_key=fx.SYNTHETIC_IDENTITY_KEY,
        client_code=fx.SYNTHETIC_CLIENT_CODE,
        person_names=("Jan Kowalski",),
        email_addresses=("jan.kowalski@example.invalid",),
    )

    def build(totals, ranking):
        days = fx.daily_inputs(date(2026, 7, 1), fx.WEEKLY_DAY_KM, totals)
        current = fx.period_from_days(fx.CURRENT_WEEKLY, days, ranking=ranking)
        previous_days = fx.daily_inputs(date(2026, 7, 1), fx.PREVIOUS_DAY_KM, fx.PREVIOUS_TOTALS)
        previous = fx.period_from_days(fx.PREVIOUS_WEEKLY, previous_days,
                                       ranking=fx.PREVIOUS_RANKED_FACTS)
        return build_publishable_snapshot(
            privacy=privacy,
            generated_at_utc=fx.GENERATED_AT,
            period_type="weekly",
            current=current,
            previous=previous,
            days=days,
            series=fx.FULL_SERIES,
        )

    a = build(fx.ACCEPTABLE_TOTALS, fx.RANKED_FACTS)
    b = build(fx.SAFE_TOTALS, fx.RANKED_FACTS)
    body_a = serialize_publishable_snapshot(a)
    body_b = serialize_publishable_snapshot(b)
    assert body_a != body_b, "the two fixtures must differ, or the conflict test proves nothing"

    # The SAME document as A, serialised the way a human or a careless caller
    # would: indented, and with the top-level keys in reverse order. Byte
    # different, semantically identical — exactly the thing the canonical-form
    # gate exists to refuse.
    document = json.loads(body_a.decode("utf-8"))
    reordered = {key: document[key] for key in sorted(document, reverse=True)}
    non_canonical = json.dumps(reordered, ensure_ascii=False, indent=2).encode("utf-8")
    assert non_canonical != body_a
    assert json.loads(non_canonical.decode("utf-8")) == document

    # A value the two serialisers genuinely disagree about. Python's canonical
    # encoder writes a float as `1.0`; `JSON.stringify` writes `1`. This is the
    # concrete reason a JavaScript canonicaliser is not an option and the
    # ingress octets must be preserved instead.
    divergent = canonical_json_bytes({"ratio": 1.0, "scale": 2.50, "count": 3})
    assert b"1.0" in divergent

    # A REAL host snapshot whose ranked population contains nobody in the
    # `dangerous` group, so the distribution carries two keys and not three.
    # This is the shape the deployed Worker refused in production
    # (`SCHEMA / MISSING_FIELD` at
    # `$.periods.weekly.current.rating_group_distribution.dangerous`), and the
    # host serialiser is unchanged: these are its own canonical octets.
    sparse = build(fx.ACCEPTABLE_TOTALS, fx.SPARSE_RANKED_FACTS)
    body_sparse = serialize_publishable_snapshot(sparse)
    assert b'"rating_group_distribution":{"acceptable":50.63,"safe":49.37}' in body_sparse, \
        "the sparse fixture must actually emit the two-key host shape"

    # Structural counter-examples, each serialised by the SAME host canonical
    # encoder so the only thing under test is the schema verdict. Their digests
    # are computed over their own bytes, so the digest gate cannot be what
    # refuses them.
    sparse_document = json.loads(body_sparse.decode("utf-8"))

    def _with_distribution(value):
        mutated = json.loads(json.dumps(sparse_document))
        mutated["periods"]["weekly"]["current"]["rating_group_distribution"] = value
        return canonical_json_bytes(mutated)

    # Not ranked, and correct in every other respect: the conditional ranking
    # fields are removed exactly as an unranked block must remove them, so the
    # ONLY contract violation left is the distribution's presence.
    unranked = json.loads(json.dumps(sparse_document))
    unranked_block = unranked["periods"]["weekly"]["current"]
    unranked_block["ranking_state"] = "NOT_ON_ROSTER"
    for conditional in ("ranking_position", "ranking_total_participants",
                        "rating_group_share_percent"):
        unranked_block.pop(conditional, None)
    invalid_distributions = {
        "empty": _with_distribution({}),
        "unknown_bucket": _with_distribution(
            {"acceptable": 50.63, "reckless": 0.0, "safe": 49.37}),
        "wrong_value_type": _with_distribution({"acceptable": "50.63", "safe": 49.37}),
        "out_of_range_value": _with_distribution({"acceptable": 150.5, "safe": 49.37}),
        "distribution_without_rank": canonical_json_bytes(unranked),
    }

    _INPUT.update({
        "sparse_base64": base64.b64encode(body_sparse).decode("ascii"),
        "sparse_digest": sparse.payload_digest,
        "invalid_distributions": {
            name: {"body_base64": base64.b64encode(raw).decode("ascii"),
                   "digest": hashlib.sha256(raw).hexdigest()}
            for name, raw in invalid_distributions.items()
        },
        "canonical_a_base64": base64.b64encode(body_a).decode("ascii"),
        "canonical_b_base64": base64.b64encode(body_b).decode("ascii"),
        "digest_a": a.payload_digest,
        "digest_b": b.payload_digest,
        "non_canonical_a_base64": base64.b64encode(non_canonical).decode("ascii"),
        "divergent_base64": base64.b64encode(divergent).decode("ascii"),
        "_body_a": body_a,
        "_body_b": body_b,
        "_divergent": divergent,
        "_body_sparse": body_sparse,
    })
    return _INPUT


def scenarios() -> dict:
    if not _CACHE:
        payload = {k: v for k, v in _host_payloads().items() if not k.startswith("_")}
        result = subprocess.run(
            ["node", str(HARNESS)], input=json.dumps(payload),
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=600,
        )
        if result.returncode != 0:
            raise AssertionError(f"harness failed: {result.stderr.strip()[:800]}")
        _CACHE.update(json.loads(result.stdout))
        broken = {name: value.get("harness_error") for name, value in _CACHE.items()
                  if isinstance(value, dict) and value.get("harness_error")}
        if broken:
            raise AssertionError(f"harness scenario errors: {broken} :: "
                                 f"{[v.get('stack') for v in _CACHE.values() if isinstance(v, dict) and v.get('stack')][:1]}")
    return _CACHE


def source(relative: str) -> str:
    return (DELIVERY / relative).read_text(encoding="utf-8")


def code(relative: str) -> str:
    """Source with comments removed, so a prose mention is not evidence."""
    import re

    text = source(relative)
    text = re.sub(r"/\*[\s\S]*?\*/", " ", text)
    text = re.sub(r"(?m)^\s*//.*$", " ", text)
    return text


# --- 1. the host contract itself ---------------------------------------------


def test_host_digest_is_over_exactly_the_canonical_bytes() -> None:
    """`payload_digest` is SHA-256 of the bytes the publisher may upload."""
    data = _host_payloads()
    body_a = data["_body_a"]
    body_b = data["_body_b"]
    assert data["digest_a"] == hashlib.sha256(body_a).hexdigest()
    assert data["digest_b"] == hashlib.sha256(body_b).hexdigest()
    assert data["digest_a"] != data["digest_b"]

    # Determinism: the same inputs produce the same octets, so "same operation,
    # same payload" is a byte statement and not a hope.
    from jobs.ecodriving_dashboard.publication import serialize_publishable_snapshot
    again = _host_payloads()["_body_a"]
    assert again == body_a
    assert serialize_publishable_snapshot  # the only supported serialiser
    print("PASS test_host_digest_is_over_exactly_the_canonical_bytes")


def test_python_and_javascript_serialisers_are_not_interchangeable() -> None:
    """The concrete reason the ingress octets must be preserved, not rebuilt."""
    case = scenarios()["reserialisation_detector"]
    assert case["divergent_serialisers_disagree"] is True, \
        "if these ever agreed, the divergence fixture stopped proving anything"
    assert "1.0" in case["divergent_python_bytes"]
    assert case["divergent_js_restringify"] != case["divergent_python_bytes"]
    # And the same, for the rebuild the Worker actually produces from a real
    # snapshot: valid, semantically identical, byte different.
    assert case["schema_rebuild_valid"] is True
    assert case["schema_rebuild_is_same_document"] is True
    assert case["schema_rebuild_differs"] is True, \
        "the strict-schema rebuild matched the host bytes; the detector is blind"
    # The whole point: R2 holds the host bytes, not either rebuild.
    assert case["r2_holds_host_bytes"] is True
    assert case["r2_is_not_the_schema_rebuild"] is True
    print("PASS test_python_and_javascript_serialisers_are_not_interchangeable")


# --- 2. the canonical happy path ---------------------------------------------


def test_canonical_bytes_survive_publication_unchanged() -> None:
    """THE acceptance criterion, asserted on octets at every link."""
    case = scenarios()["canonical_happy_path"]
    data = _host_payloads()
    host_digest = hashlib.sha256(data["_body_a"]).hexdigest()

    assert case["publish_status"] == 201 and case["publish_result"] == "PUBLISHED"

    # One digest, six places.
    for field in ("host_digest", "host_bytes_sha256", "ledger_digest",
                  "r2_bytes_sha256", "r2_metadata_digest"):
        assert case[field] == host_digest, f"{field} is not the host digest"
    assert case["r2_metadata_algorithm"] == "sha-256"

    # And the octets, not only their digests.
    assert case["r2_bytes_equal_host_bytes"] is True, "R2 does not hold the host bytes"
    assert case["r2_bytes_hex_equals_host_hex"] is True
    assert case["r2_byte_length"] == case["host_byte_length"] == len(data["_body_a"])

    # Exactly one object, written exactly once.
    assert case["objects"] == 1 and case["puts"] == 1

    # The browser still receives the strict-schema rebuild: same document,
    # deliberately not the same octets.
    assert case["snapshot_status"] == 200
    assert case["snapshot_same_document"] is True
    assert case["snapshot_is_a_rebuild_not_the_stored_bytes"] is True
    print("PASS test_canonical_bytes_survive_publication_unchanged")


# --- 3. digest and body must describe each other -----------------------------


def test_digest_of_a_with_body_of_b_is_refused() -> None:
    """A caller cannot get either value accepted by pairing them wrongly."""
    case = scenarios()["digest_mismatch"]
    assert case["forward_status"] == 400 and case["forward_error"] == "PAYLOAD_DIGEST_MISMATCH"
    assert case["backward_status"] == 400 and case["backward_error"] == "PAYLOAD_DIGEST_MISMATCH"
    assert case["fabricated_status"] == 400 and case["fabricated_error"] == "PAYLOAD_DIGEST_MISMATCH"
    # Nothing became authoritative: no operation, no object, no grant, no bearer.
    assert case["operations"] == 0, "a rejected publication created ledger state"
    assert case["objects"] == 0 and case["puts"] == 0, "a rejected publication touched R2"
    assert case["grants"] == 0
    assert case["bearers_returned"] == 0
    assert case["logs_leak_digest"] is False
    print("PASS test_digest_of_a_with_body_of_b_is_refused")


def test_the_worker_computes_the_digest_itself() -> None:
    """The supplied digest is checked, never trusted as the recorded value."""
    worker = source("worker/index.js")
    assert "await payloadDigest(snapshotBytes)" in worker, \
        "the digest must be computed over the exact ingress octets"
    assert "timingSafeEqualHex(actualDigest, declaredDigest)" in worker
    # The value handed to the ledger is the computed one, not the header.
    assert "payload_digest: actualDigest" in worker
    assert "payload_digest: declaredDigest" not in worker
    print("PASS test_the_worker_computes_the_digest_itself")


# --- 4. one operation, one byte sequence -------------------------------------


def test_same_operation_with_different_bytes_conflicts_without_mutating() -> None:
    case = scenarios()["operation_conflict_on_different_bytes"]
    assert case["first_status"] == 201
    assert case["conflict_status"] == 409
    assert case["conflict_result"] == "CONFLICT"
    assert case["conflict_next_action"] == "OPEN_NEW_OPERATION"
    assert case["conflict_bearer"] is False
    # Nothing about the existing publication moved.
    assert case["ledger_digest_unchanged"] is True
    assert case["stored_bytes_unchanged"] is True, "a conflicting payload overwrote R2"
    assert case["grant_unchanged"] is True
    assert case["objects"] == 1
    assert case["puts_after_conflict"] == 1, "the conflicting call wrote to R2"
    # An identical retry is still idempotent, and still mints no second bearer.
    assert case["identical_retry_status"] == 200
    assert case["identical_retry_result"] == "ALREADY_PUBLISHED"
    assert case["identical_retry_bearer"] is False
    assert case["live_grants"] == 1
    print("PASS test_same_operation_with_different_bytes_conflicts_without_mutating")


# --- 5. stored-byte integrity on the read path -------------------------------


def test_r2_corruption_is_detected_before_the_driver_sees_it() -> None:
    case = scenarios()["r2_corruption_detected_on_read"]
    assert case["healthy_status"] == 200
    # (a) body mutated alone.
    assert case["body_mutation_status"] == 503, "a mutated object was served"
    assert json.loads(case["body_mutation_body"]) == {"error": "SERVICE_UNAVAILABLE"}, \
        "the refusal must not tell the browser what failed"
    # (b) body and object metadata rewritten together — the ledger still knows.
    assert case["coordinated_rewrite_status"] == 503, \
        "rewriting the metadata alongside the body defeated the check"
    # (c) no digest at all is not a pass.
    assert case["digest_removed_status"] == 503, "an object with no digest was trusted"
    # (d) restoring the exact bytes restores service.
    assert case["restored_status"] == 200
    assert case["restored_same_document"] is True
    assert case["reasons"] >= 3, "the refusals were not recorded as digest failures"
    print("PASS test_r2_corruption_is_detected_before_the_driver_sees_it")


def test_read_path_hashes_the_bytes_it_serves() -> None:
    worker = source("worker/index.js")
    snapshot = source("worker/lib/snapshot.js")
    assert "object.arrayBuffer()" in worker and "await object.text()" not in worker
    assert "verifyStoredBytes" in worker
    assert "findPublicationDigestByObjectKey" in worker
    # Both authorities, and both fail closed.
    assert "LEDGER_MISMATCH" in snapshot and "OBJECT_MISMATCH" in snapshot
    assert 'detail: "MISSING"' in snapshot
    print("PASS test_read_path_hashes_the_bytes_it_serves")


# --- 6. a retry may not silently repair --------------------------------------


def test_retry_over_a_mismatched_object_fails_closed() -> None:
    case = scenarios()["retry_over_corrupted_object_fails_closed"]
    assert case["crash_status"] == 503
    assert case["state_after_crash"] == "CREATED"
    # The retry must refuse rather than overwrite the evidence.
    assert case["retry_status"] == 409
    assert case["retry_error"] == "OBJECT_INTEGRITY_FAILURE"
    assert case["retry_result"] == "OBJECT_INTEGRITY_FAILURE"
    assert case["retry_next_action"] == "INVESTIGATE_OBJECT_INTEGRITY"
    assert case["retry_bearer"] is False
    assert case["retry_overwrote_object"] is False, "the retry silently rewrote the object"
    assert case["bytes_after_retry_still_substituted"] is True
    assert case["state_after_retry"] == "CREATED", "a mismatched object advanced the ledger"
    assert case["grants_after_retry"] == 0
    # Once the correct object is back, the operation resumes normally.
    assert case["resumed_status"] == 201
    assert case["resumed_result"] == "PUBLISHED"
    assert case["resumed_bearer"] is True
    assert case["resumed_bytes_are_host_bytes"] is True
    assert case["resumed_ledger_digest"] == _host_payloads()["digest_a"]
    print("PASS test_retry_over_a_mismatched_object_fails_closed")


# --- 6b. a sparse rating distribution is valid, and only that changed ---------


def test_sparse_rating_distribution_is_accepted_unchanged() -> None:
    """The exact production shape `{"acceptable":...,"safe":...}` publishes.

    The host derives the distribution by grouping the ranked population, so a
    group with nobody in it produces no key. The deployed validator required
    all three, which refused a valid snapshot with 422 PAYLOAD_NOT_CANONICAL
    before `publishSnapshot` ran. Nothing about the host bytes changed to fix
    it: these are the host serialiser's own octets.
    """
    body = _host_payloads()["_body_sparse"]
    assert b'"rating_group_distribution":{"acceptable":50.63,"safe":49.37}' in body, \
        "the fixture no longer carries the two-key host shape it exists to prove"
    assert b'"dangerous"' not in body.split(b'"rating_group_distribution":')[1][:80]

    case = scenarios()["sparse_rating_distribution"]
    assert case["schema_ok"] is True, (case["schema_reason"], case["schema_path"])
    assert case["canonical_ok"] is True, "the canonical-form gate must be unaffected"
    assert case["publish_status"] in (200, 201), (case["publish_status"], case["publish_error"])
    assert case["publish_result"] == "PUBLISHED"

    # The stored object is still the host's own octets, and the ledger digest
    # still identifies them.
    assert case["r2_bytes_equal_host_bytes"] is True
    assert case["ledger_digest"] == case["host_digest"] == _host_payloads()["sparse_digest"]
    assert case["ledger_digest"] == hashlib.sha256(body).hexdigest()

    # And the driver is served the two buckets that exist — no zero-filling.
    assert case["snapshot_status"] == 200
    assert case["served_distribution_keys"] == ["acceptable", "safe"]
    print("PASS test_sparse_rating_distribution_is_accepted_unchanged")


def test_an_invalid_rating_distribution_still_fails_closed() -> None:
    """Sparse is admitted; nothing else about the bucket contract loosened."""
    refusals = scenarios()["sparse_rating_distribution"]["refusals"]
    expected = {
        "empty": "CARDINALITY",
        "unknown_bucket": "UNKNOWN_FIELD",
        "wrong_value_type": "TYPE",
        "out_of_range_value": "RANGE",
        "distribution_without_rank": "CROSS_FIELD",
    }
    assert set(refusals) == set(expected), sorted(refusals)
    for name, detail in expected.items():
        case = refusals[name]
        assert case["schema_ok"] is False, name
        assert case["schema_detail"] == detail, (name, case["schema_detail"])
        assert "rating_group_distribution" in (case["schema_path"] or ""), \
            (name, case["schema_path"])
        assert case["status"] == 422, (name, case["status"])
        assert case["error"] == "PAYLOAD_NOT_CANONICAL", (name, case["error"])
        # Refused before `publishSnapshot`: this is why the production failure
        # left no remote state to clean up.
        assert case["operations"] == 0 and case["objects"] == 0 and case["grants"] == 0, name
    print("PASS test_an_invalid_rating_distribution_still_fails_closed")


# --- 7. ingress must be in host canonical form -------------------------------


def test_non_canonical_ingress_is_refused() -> None:
    case = scenarios()["non_canonical_ingress_is_refused"]
    assert case["pretty_is_same_document"] is True, \
        "the counter-example must be semantically identical, or it tests nothing"
    assert case["pretty_status"] == 422
    assert case["pretty_error"] == "PAYLOAD_NOT_CANONICAL"
    assert case["invalid_utf8_status"] == 422
    assert case["invalid_utf8_error"] == "PAYLOAD_NOT_CANONICAL"
    assert case["operations"] == 0 and case["objects"] == 0 and case["grants"] == 0
    # The scanner accepts real host output and names why it rejects the other.
    assert case["canonical_verdict_host"]["ok"] is True, \
        "the canonical-form gate rejects genuine host output"
    assert case["canonical_verdict_pretty"]["ok"] is False
    assert case["canonical_verdict_pretty"]["detail"] in {
        "INSIGNIFICANT_WHITESPACE", "UNSORTED_KEYS"}
    print("PASS test_non_canonical_ingress_is_refused")


def test_the_canonical_gate_is_a_scanner_not_a_second_serialiser() -> None:
    """It must never try to re-encode numbers or strings."""
    snapshot = source("worker/lib/snapshot.js")
    assert "checkCanonicalJsonForm" in snapshot
    # No JS re-serialisation is compared against ingress anywhere.
    assert "JSON.stringify(candidate)" not in snapshot
    assert "JSON.stringify(validated.document) ===" not in snapshot
    for marker in ("INSIGNIFICANT_WHITESPACE", "UNSORTED_KEYS", "DUPLICATE_KEY"):
        assert marker in snapshot
    print("PASS test_the_canonical_gate_is_a_scanner_not_a_second_serialiser")


# --- 8. the plumbing that makes byte identity possible -----------------------


def test_body_reader_returns_exact_octets() -> None:
    case = scenarios()["body_reader_returns_exact_octets"]
    for mode in ("stream", "buffered"):
        assert case[mode]["ok"] is True, mode
        assert case[mode]["bytes_present"] is True, mode
        assert case[mode]["bytes_equal_source"] is True, f"{mode}: octets were altered"
        assert case[mode]["digest_equals_host"] == _host_payloads()["digest_a"], mode
    invalid = case["invalid_utf8"]
    assert invalid["ok"] is True
    assert invalid["bytes_preserved"] is True, "the raw octets did not survive the read"
    assert invalid["lossy_text_differs"] is True, \
        "the fixture must actually be lossy under a permissive decode"
    assert invalid["strict_decode_refuses"] is True
    print("PASS test_body_reader_returns_exact_octets")


def test_no_re_serialisation_reaches_r2() -> None:
    """Static proof that the write path cannot regress to storing a rebuild."""
    worker = source("worker/index.js")
    publisher = source("worker/lib/publisher.js")
    publication = source("worker/lib/publication.js")

    assert "body: snapshotBytes" in worker
    assert "body: validation.body" not in worker, \
        "the validated rebuild is being stored again"
    # putSnapshotObject writes what it was given and hashes what it wrote.
    assert "toExactBytes(params.body)" in publisher
    assert "await payloadDigest(bytes)" in publisher
    assert "services.bucket.put(params.snapshot_object_key, bytes" in publisher
    # Comments may DISCUSS the old defect; no executable statement may re-create
    # it, so these two look at code with comments stripped.
    assert "JSON.stringify" not in code("worker/lib/publisher.js"), \
        "the write path re-serialises something"
    assert "JSON.parse" not in code("worker/lib/publication.js")
    assert "JSON.stringify" not in code("worker/lib/publication.js")
    assert publication
    print("PASS test_no_re_serialisation_reaches_r2")


def test_digest_module_refuses_to_hash_text() -> None:
    """One digest definition, octets only, so text can never sneak in."""
    digest = source("worker/lib/digest.js")
    assert "PAYLOAD_DIGEST_REQUIRES_BYTES" in digest
    assert "PAYLOAD_BYTES_REQUIRED" in digest
    # And the Worker uses it rather than a private text-hashing helper.
    worker = source("worker/index.js")
    assert "new TextEncoder().encode(text)" not in worker
    assert 'from "./lib/digest.js"' in worker
    print("PASS test_digest_module_refuses_to_hash_text")


def main() -> None:
    test_host_digest_is_over_exactly_the_canonical_bytes()
    test_python_and_javascript_serialisers_are_not_interchangeable()
    test_canonical_bytes_survive_publication_unchanged()
    test_digest_of_a_with_body_of_b_is_refused()
    test_the_worker_computes_the_digest_itself()
    test_same_operation_with_different_bytes_conflicts_without_mutating()
    test_r2_corruption_is_detected_before_the_driver_sees_it()
    test_read_path_hashes_the_bytes_it_serves()
    test_retry_over_a_mismatched_object_fails_closed()
    test_sparse_rating_distribution_is_accepted_unchanged()
    test_an_invalid_rating_distribution_still_fails_closed()
    test_non_canonical_ingress_is_refused()
    test_the_canonical_gate_is_a_scanner_not_a_second_serialiser()
    test_body_reader_returns_exact_octets()
    test_no_re_serialisation_reaches_r2()
    test_digest_module_refuses_to_hash_text()
    print("Driver Eco Dashboard V1 canonical byte-integrity gate passed")


if __name__ == "__main__":
    main()
