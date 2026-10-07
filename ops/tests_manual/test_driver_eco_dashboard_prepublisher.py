#!/usr/bin/env python3
"""Pre-publisher security gate for the Driver Eco Dashboard delivery boundary.

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_prepublisher.py

Closes the items the independent re-review required before publisher
development may begin:

  1. real-D1 rotation retry classification (and a permanent detector for the
     production/emulator projection divergence that hid it);
  2. streaming request-body bound on the unauthenticated exchange endpoint;
  3. the durable publication/idempotency contract, including raw-bearer
     response-loss recovery;
  4. the exact cross-language subject-binding vectors (JS vs Python);
  5. the canonical snapshot publication interface as the only publisher path;
  6. the machine-authenticated publisher write transport;
  7. a local publisher-write -> driver-read integration proof.

Drives `ops/tests_manual/eco_prepublisher_harness.mjs`, which executes the real
Worker against projection-accurate in-memory bindings. No wrangler, no
credentials, no remote Cloudflare resource, no e-mail, no production data.

No capability, session id, machine credential or object key is printed.
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
if str(REPO_ROOT / "ops" / "tests_manual") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "ops" / "tests_manual"))

DELIVERY = REPO_ROOT / "delivery" / "driver_eco_dashboard"
SPEC = DELIVERY / "spec"
FRONTEND = REPO_ROOT / "assets" / "driver_eco_dashboard"
HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_prepublisher_harness.mjs"

UNIT_SEPARATOR = "\u001F"

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
    without_block = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"^\s*//.*$", " ", without_block, flags=re.M)


def node(script: str, stdin: str | None = None) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=stdin, capture_output=True, text=True,
        cwd=str(REPO_ROOT), timeout=180, check=True,
    )
    return json.loads(result.stdout)


# --- 1. D1 projection and rotation retry classification ---------------------


def test_capability_projection_names_every_state_column() -> None:
    case = scenarios()["projection_includes_rotation_state"]
    assert case["includes_rotated_to"] is True, "rotation state cannot be read without rotated_to"
    assert case["no_select_star"] is True, "the projection must be explicit, not SELECT *"
    assert case["projected_columns"] == case["declared_columns"]
    assert "rotated_to" in case["projected_columns"]
    assert "revoked_at" in case["projected_columns"]

    store = source("worker/lib/store.js")
    assert "CAPABILITY_COLUMNS" in store
    assert "SELECT *" not in code_only(store)
    # Both grant lookups use the one declared column list.
    assert store.count("SELECT ${CAPABILITY_COLUMNS}") == 2
    print("PASS test_capability_projection_names_every_state_column")


def test_emulator_models_real_projection() -> None:
    case = scenarios()["emulator_models_projection"]
    assert case["narrow_columns"] == ["capability_id", "revoked_at"]
    assert case["narrow_hides_rotated_to"] is True, "the double must not supply omitted columns"
    assert case["narrow_hides_subject"] is True
    assert case["unknown_column_rejected"] is True, "a typo'd column must fail, not return undefined"

    bindings = source("local/memory_bindings.js")
    assert "_project(" in bindings
    assert code_only(bindings).count("{ ...row }") <= 1, "full-row returns hide projection bugs"
    print("PASS test_emulator_models_real_projection")


def test_narrowed_projection_still_reproduces_the_defect() -> None:
    """The permanent detector for the divergence the review found.

    Rotation is re-run against a deliberately pre-fix projection. If the
    emulator ever becomes permissive again, these two statuses converge and this
    test fails.
    """
    case = scenarios()["narrowed_projection_reproduces_the_defect"]
    assert case["correct_projection_status"] == "ALREADY_ROTATED"
    assert case["narrowed_projection_status"] == "REVOKED", \
        "the omitted-column defect must still be reproducible"
    assert case["defect_is_detectable"] is True
    assert case["narrowed_minted_nothing"] is True, "even misclassified, nothing may be minted"
    print("PASS test_narrowed_projection_still_reproduces_the_defect")


def test_rotation_retry_classification() -> None:
    case = scenarios()["rotation_retry_matrix"]
    assert case["first"] == "ROTATED"
    assert case["retry"] == "ALREADY_ROTATED"
    assert case["later_retry"] == "ALREADY_ROTATED"
    # A lost response looks exactly like a retry; it must not read as REVOKED.
    assert case["response_loss_retry"] == "ALREADY_ROTATED"
    assert case["retry_names_successor"] is True, "the contract returns the existing successor id"
    assert case["later_retry_names_successor"] is True
    assert case["retry_minted_zero_rows"] is True

    for width in (2, 8, 32):
        contended = case[f"contention_{width}"]
        assert contended["rotated"] == 1, (width, contended)
        assert contended["conflicts"] == width - 1, (width, contended)
        assert contended["live"] == 1, (width, contended)
        assert contended["rows"] == 2, (width, contended)

    assert case["revoke_before_rotate"] == "REVOKED"
    assert case["rotate_before_revoke"]["revoke_status"] == "ALREADY_REVOKED"
    assert case["rotate_before_revoke"]["live"] == 1
    print("PASS test_rotation_retry_classification")


# --- 2. bounded request body ------------------------------------------------
#
# The previous version of this section asserted a ~1 KiB consumption ceiling.
# That number came from the fixture, which emitted ~1 KiB chunks — not from the
# code, which reads whole chunks and therefore consumes whatever size the peer
# (or an upstream buffer) chose to deliver. The assertions below are written
# PER READ MODE so no claim outruns what the runtime actually provides.


def test_byob_availability_is_established_from_the_runtime() -> None:
    """What the platform actually offers, recorded rather than assumed.

    A fixed-size (BYOB) read is the only mechanism that bounds per-read memory
    independently of attacker chunk size. Cloudflare documents
    `getReader({ mode: "byob" })` on `ReadableStream`, but does NOT document
    whether an inbound `Request.body` is a byte stream in the deployed runtime,
    so the implementation attempts BYOB and falls back — and this test records
    which path applies here instead of pretending.
    """
    case = scenarios()["byob_platform_probe"]
    # The BYOB code path is real and exercised, not dead code: a stream that IS
    # a byte stream yields a BYOB reader in this runtime.
    assert case["byte_stream_supports_byob"] is True, case
    assert case["byob_buffer_bytes"] == 1024, case
    # Whether an inbound Request.body does is a platform fact, not a target.
    assert isinstance(case["request_body_supports_byob"], bool)
    mode = "BYOB" if case["request_body_supports_byob"] else "default-reader"
    print(f"PASS test_byob_availability_is_established_from_the_runtime "
          f"(Request.body -> {mode} on {case['runtime']})")


def test_declared_size_gate_consumes_nothing() -> None:
    """The one bound that holds unconditionally, in every mode."""
    declared = scenarios()["session_body_is_bounded"]["declared_oversize"]
    assert declared["mode"] == "declared"
    assert declared["reason"] == "DECLARED_TOO_LARGE"
    assert declared["bytes_read"] == 0, "nothing may be consumed when the size is declared"
    print("PASS test_declared_size_gate_consumes_nothing")


def test_one_huge_chunk_is_bounded_only_in_byob_mode() -> None:
    """The probe the previous suite lacked, asserted honestly in both modes.

    A single 5 MiB chunk is offered with no Content-Length. `largest_chunk_bytes`
    is the number that separates a genuine per-read bound from a fixture that
    merely sent small chunks.
    """
    case = scenarios()["session_body_is_bounded"]

    # BYOB mode: the buffer is ours, so one 5 MiB source chunk is delivered in
    # 1 KiB pieces and refused after the first. This is the real guarantee.
    byob = case["one_chunk_five_mib_byte_stream"]
    assert byob["mode"] == "byob", byob
    assert byob["reason"] == "STREAM_TOO_LARGE"
    assert byob["largest_chunk_bytes"] <= byob["buffer_bytes"], byob
    assert byob["bytes_read"] <= byob["buffer_bytes"], byob

    # Default-reader mode: the logical limit holds and the request is refused,
    # but one read hands us the whole chunk. The test asserts THAT, rather than
    # a small ceiling the mode does not provide.
    plain = case["one_chunk_five_mib"]
    assert plain["reason"] == "STREAM_TOO_LARGE", plain
    if plain["mode"] == "default":
        assert plain["largest_chunk_bytes"] == plain["total_offered"], (
            "in default-reader mode one read delivers the whole chunk; a test that "
            "claims otherwise is asserting a property of its fixture")
    else:
        assert plain["mode"] == "byob"
        assert plain["largest_chunk_bytes"] <= 1024, plain
    print(f"PASS test_one_huge_chunk_is_bounded_only_in_byob_mode "
          f"(byob<={byob['bytes_read']} B, {plain['mode']} mode saw "
          f"{plain['largest_chunk_bytes']} B in one read)")


def test_tiny_chunk_flood_trips_the_ceiling_mid_stream() -> None:
    """1 MiB offered in 17-byte chunks: refused near 512 B, not at the end."""
    chunked = scenarios()["session_body_is_bounded"]["chunked_oversize"]
    assert chunked["reason"] == "STREAM_TOO_LARGE"
    assert chunked["total_offered"] == 1024 * 1024
    assert 512 < chunked["bytes_read"] <= 512 + 64, chunked
    assert chunked["largest_chunk_bytes"] <= 64, chunked
    assert chunked["cancelled"] is True, "the remainder must be cancelled, not drained"
    print("PASS test_tiny_chunk_flood_trips_the_ceiling_mid_stream")


def test_body_limit_boundaries_and_header_parsing() -> None:
    case = scenarios()["session_body_is_bounded"]
    assert case["exactly_at_limit"]["ok"] is True
    assert case["exactly_at_limit"]["bytes_read"] == 512
    assert case["limit_plus_one"]["ok"] is False

    # A malformed Content-Length is not trusted in either direction.
    malformed = case["malformed_content_length"]
    assert malformed["reason"] == "STREAM_TOO_LARGE"
    assert case["declared_length_parse"]["absent"] is None
    assert case["declared_length_parse"]["malformed"] is None

    worker = code_only(source("worker/index.js"))
    assert "readBoundedBody" in worker
    assert "await request.text()" not in worker, "the exchange must not buffer the whole body"
    print("PASS test_body_limit_boundaries_and_header_parsing")


def test_session_endpoint_rejects_oversized_bodies() -> None:
    case = scenarios()["session_endpoint_rejects_oversized_bodies"]
    # The oversized fixture declares no `Content-Length` at all, which the
    # framing gate now refuses BEFORE the body is opened. So the stream is
    # never read and never cancelled — there is nothing to cancel. The only
    # bytes the counter can see are the ones `ReadableStream` prefetched into
    # its own queue, which is a property of the fixture, not of the Worker.
    assert case["oversized_status"] == 400
    assert case["oversized_stream_cancelled"] is False, \
        "an undeclared body is refused before a reader exists"
    assert case["oversized_bytes_pulled"] <= 4096, case["oversized_bytes_pulled"]
    assert case["oversized_total_offered"] == 5 * 1024 * 1024
    # Declared oversize: refused, and our code reads nothing.
    assert case["declared_oversize_status"] == 400
    assert case["oversized_body_echoes_capability"] is False
    assert case["padded_status"] == 400, "an oversized but valid-looking body is still refused"
    # The normal bootstrap keeps working.
    assert case["normal_status"] == 204
    assert case["normal_sets_cookie"] is True
    assert case["logs_leak_capability"] is False
    print("PASS test_session_endpoint_rejects_oversized_bodies")


def test_no_false_chunk_size_guarantee_is_claimed_anywhere() -> None:
    """Code and documentation must not out-claim the platform.

    The body module still has to name its modes and say what each does and does
    not provide. What changed is WHICH claim is load-bearing: the bound now
    comes from the fail-closed request-framing gate, and the reader mode is
    telemetry. Neither the retracted peak-memory claim nor the retracted
    "BYOB is a release gate" doctrine may reappear.
    """
    body = source("worker/lib/body.js")
    for required in ("BODY_READ_MODE", "largestChunkBytes", "tryByobReader",
                     "does not", "FAIL-CLOSED"):
        assert required in body, required
    # No absolute consumption guarantee may survive in CODE comments. The
    # prose documents quote the retracted claim on purpose, so they are checked
    # positively (below) rather than by absence.
    FALSE_CLAIMS = (
        "costs the ceiling, not its own size",
        "never buffers more than",
        "the protocol needs is ever buffered",
        "no body larger than required is buffered",
    )
    for relative in ("worker/index.js", "worker/lib/body.js", "worker/lib/http.js"):
        text = source(relative)
        for claim in FALSE_CLAIMS:
            assert claim not in text, f"{relative} still claims: {claim}"
    # `/api/session` states the CURRENT contract at its call site: the bound
    # comes from request framing, which the runtime exposes, and not from the
    # reader mode, which deployed verification showed it does not offer.
    session_route = source("worker/index.js")
    assert "requireDeclaredLength" in session_route
    assert "FAIL-CLOSED" in session_route
    assert "a release condition" in session_route, \
        "the call site must state that reader mode is not a release condition"

    # REPO_MAP.md routes agents here; it must not restore the retracted claim.
    repo_map = (REPO_ROOT / "REPO_MAP.md").read_text(encoding="utf-8")
    assert "costs the ceiling and not its own size" not in repo_map, \
        "REPO_MAP.md still carries the retracted body-memory claim"
    assert "No runtime-independent peak-memory bound is claimed" in repo_map

    readme = source("README.md")
    assert "BYOB" in readme
    print("PASS test_no_false_chunk_size_guarantee_is_claimed_anywhere")


# --- 3. publication idempotency and bearer-loss recovery --------------------


def test_publication_operation_lifecycle() -> None:
    case = scenarios()["publication_operation_lifecycle"]
    assert case["first_status"] == 201
    assert case["first_state"] == "GRANT_MINTED"
    assert case["first_returned_bearer"] is True, "the raw bearer is returned exactly once"

    # A retry never mints a second bearer.
    assert case["retry_status"] == 200
    assert case["retry_result"] == "ALREADY_PUBLISHED"
    assert case["retry_returned_bearer"] is False
    assert case["retry_minted_zero_rows"] is True

    assert case["intent_status"] == "RECORDED"
    assert case["intent_repeat_status"] == "ALREADY_RECORDED", "phase recording is idempotent"
    assert case["delivered_state"] == "DELIVERED"
    # A republish of a delivered operation is terminal, and says so.
    assert case["retry_after_delivered"] == "ALREADY_DELIVERED"

    assert case["objects_written"] == 1
    assert case["live_grants"] == 1
    print("PASS test_publication_operation_lifecycle")


def test_publication_operations_are_conflict_safe_and_isolated() -> None:
    case = scenarios()["publication_conflict_and_isolation"]
    assert case["first_status"] == 201
    # Reusing an id for different content is a caller bug, not a retry.
    assert case["conflicting_payload_status"] == 409
    assert case["conflicting_payload_error"] == "OPERATION_CONFLICT"
    assert case["conflicting_subject_status"] == 409
    assert case["conflicts_wrote_nothing"] is True

    assert case["distinct_operation_status"] == 201
    assert case["distinct_operations_independent"] is True

    # Concurrent execution of one logical operation.
    assert case["concurrent_published"] == 1, case
    assert case["concurrent_replayed"] == 3, case
    # The forced-interleaving proof at 2/8/32/128 lives in
    # test_driver_eco_dashboard_publication_transaction.py; this is the
    # smoke-level check that the route still behaves under plain concurrency.
    assert case["concurrent_bearers"] == 1, "only one attempt may mint a bearer"
    assert case["concurrent_objects"] == 1
    assert case["concurrent_live_grants"] == 1
    print("PASS test_publication_operations_are_conflict_safe_and_isolated")


def test_raw_bearer_loss_has_a_safe_recovery_path() -> None:
    case = scenarios()["bearer_loss_recovery"]
    # A blind retry after a lost response never mints.
    assert case["blind_retry_status"] == 200
    assert case["blind_retry_bearer"] is False

    # Recovery is explicit, mints a replacement, and kills the unusable one.
    assert case["recovered_status"] == 200
    assert case["recovered_bearer"] is True
    assert case["recovered_is_different"] is True
    # The initial mint is generation 1, so the first recovery is generation 2.
    assert case["bearer_generation"] == 2
    assert case["live_after_recovery"] == 1, "recovery must never leave two live links"
    assert case["old_bearer_exchange"] == 401, "the lost bearer must be dead"
    assert case["new_bearer_exchange"] == 204

    # Repeating recovery stays safe.
    assert case["second_recovery_status"] == 200
    assert case["second_generation"] == 3
    assert case["live_after_second"] == 1

    # Once delivered, the driver holds a working link: recovery is refused.
    assert case["after_delivered_status"] == 409
    assert case["after_delivered_reason"] == "ALREADY_DELIVERED"
    assert case["unknown_operation_status"] == 404

    # The contract never claims a raw bearer can be recovered from storage.
    publication = source("worker/lib/publication.js")
    assert "generateCapability" in publication
    schema = source("schema/001_authorization.sql")
    assert "bearer_generation" in schema
    assert re.search(r"\bcapability\s+TEXT", schema) is None, "no raw bearer column anywhere"
    print("PASS test_raw_bearer_loss_has_a_safe_recovery_path")


def test_operation_ids_cannot_reach_dashboard_data() -> None:
    case = scenarios()["operation_ids_cannot_reach_dashboard_data"]
    assert case["as_capability"] == 401
    assert case["as_session_cookie"] == 401
    assert case["as_publisher_credential"] == 404
    assert case["operation_row_has_no_digest"] is True
    print("PASS test_operation_ids_cannot_reach_dashboard_data")


def test_host_persistence_contract_is_documented() -> None:
    from jobs.ecodriving_dashboard.publication import HOST_PERSISTENCE_CONTRACT

    assert "operation_id" in HOST_PERSISTENCE_CONTRACT["before_publish"]
    assert "payload_digest" in HOST_PERSISTENCE_CONTRACT["before_publish"]
    assert "subject_ref" in HOST_PERSISTENCE_CONTRACT["before_publish"]
    assert "capability" in HOST_PERSISTENCE_CONTRACT["immediately_on_response"]
    assert "capability_id" in HOST_PERSISTENCE_CONTRACT["immediately_on_response"]
    assert "delivery_intent_recorded" in HOST_PERSISTENCE_CONTRACT["before_delivery"]
    assert "delivered_confirmed" in HOST_PERSISTENCE_CONTRACT["after_delivery"]
    print("PASS test_host_persistence_contract_is_documented")


# --- 4. cross-language subject binding --------------------------------------


JS_VECTOR_SCRIPT = r"""
const path = await import('node:path');
const root = process.cwd();
await import(path.join(root, 'delivery/driver_eco_dashboard/local/node_runtime.js'));
const c = await import(path.join(root, 'delivery/driver_eco_dashboard/worker/lib/capability.js'));
const fs = await import('node:fs');
const spec = JSON.parse(fs.readFileSync(
  'delivery/driver_eco_dashboard/spec/subject_binding_v1_vectors.json', 'utf8'));
const out = [];
for (const v of spec.vectors) {
  out.push({
    name: v.name,
    material_hex: Buffer.from(c.subjectBindingMaterial(v.subject_ref, v.object_key), 'utf8').toString('hex'),
    digest_sha256: await c.subjectBindingDigest(v.subject_ref, v.object_key),
    digest_hmac_sha256: await c.subjectBindingDigest(v.subject_ref, v.object_key, spec.test_pepper),
  });
}
process.stdout.write(JSON.stringify(out));
"""


def test_subject_binding_vectors_agree_across_languages() -> None:
    from jobs.ecodriving_dashboard.subject_binding import (
        SUBJECT_BINDING_DOMAIN, subject_binding_digest, subject_binding_material,
    )

    vectors = json.loads((SPEC / "subject_binding_v1_vectors.json").read_text(encoding="utf-8"))
    assert vectors["domain"] == SUBJECT_BINDING_DOMAIN
    assert vectors["output_encoding"] == "lowercase hex"
    assert len(vectors["vectors"]) >= 20, len(vectors["vectors"])
    pepper = vectors["test_pepper"]

    js_by_name = {entry["name"]: entry for entry in node(JS_VECTOR_SCRIPT)}

    for vector in vectors["vectors"]:
        name = vector["name"]
        subject, key = vector["subject_ref"], vector["object_key"]

        python_material = subject_binding_material(subject, key).encode("utf-8").hex()
        python_sha = subject_binding_digest(subject, key)
        python_hmac = subject_binding_digest(subject, key, pepper)

        # documented == python == javascript, for the material and both digests
        assert python_material == vector["material_hex"], name
        assert python_sha == vector["digest_sha256"], name
        assert python_hmac == vector["digest_hmac_sha256"], name

        assert js_by_name[name]["material_hex"] == vector["material_hex"], name
        assert js_by_name[name]["digest_sha256"] == vector["digest_sha256"], name
        assert js_by_name[name]["digest_hmac_sha256"] == vector["digest_hmac_sha256"], name

        assert len(python_sha) == 64 and python_sha == python_sha.lower()

    # Coverage the specification promises.
    names = {vector["name"] for vector in vectors["vectors"]}
    for required in ("non_ascii_polish", "non_ascii_cjk", "non_ascii_emoji",
                     "combining_sequence", "precomposed_sequence",
                     "separator_like_pipe", "separator_like_pipe_alt",
                     "long_subject_255", "long_subject_256",
                     "empty_subject", "empty_key", "one_byte_difference"):
        assert required in names, required

    digests = [vector["digest_sha256"] for vector in vectors["vectors"]]
    assert len(set(digests)) == len(digests), "every vector must produce a distinct digest"
    print(f"PASS test_subject_binding_vectors_agree_across_languages ({len(digests)} vectors)")


def test_subject_binding_encoding_is_unambiguous() -> None:
    from jobs.ecodriving_dashboard.subject_binding import (
        SubjectBindingError, subject_binding_digest, subject_binding_material,
    )

    # The classic separator collision must not exist.
    assert subject_binding_digest("a", "b|c") != subject_binding_digest("a|b", "c")
    assert subject_binding_digest("", "ab") != subject_binding_digest("a", "b")
    # A one-byte change anywhere changes the digest.
    assert subject_binding_digest("subject-A", "aa/x.json") != subject_binding_digest("subject-B", "aa/x.json")
    assert subject_binding_digest("subject-A", "aa/x.json") != subject_binding_digest("subject-A", "aa/y.json")
    # Peppered and unpeppered forms are never interchangeable.
    assert subject_binding_digest("s", "k") != subject_binding_digest("s", "k", "pepper")

    # Length prefixes are UTF-8 byte counts, not code-point counts.
    material = subject_binding_material("żółć", "k")
    assert UNIT_SEPARATOR + "8" + UNIT_SEPARATOR in material

    # The separator can never be smuggled into a field.
    for bad_subject, bad_key in ((f"a{UNIT_SEPARATOR}b", "k"), ("s", f"a{UNIT_SEPARATOR}b")):
        try:
            subject_binding_material(bad_subject, bad_key)
        except SubjectBindingError:
            pass
        else:
            raise AssertionError("the separator must be rejected inside a field")

    # No normalisation: composed and decomposed forms stay distinct.
    assert subject_binding_digest("é", "k") != subject_binding_digest("é", "k")

    spec = (SPEC / "subject_binding_v1.md").read_text(encoding="utf-8")
    for required in ("U+001F", "length", "normalis", "domain", "lowercase hex"):
        assert required.lower() in spec.lower(), required
    print("PASS test_subject_binding_encoding_is_unambiguous")


# --- 5. canonical snapshot publication interface ----------------------------
#
# The review found the previous boundary too permissive in two ways:
#   * `canonical_bytes(mapping, ...)` accepted ANY structurally valid mapping,
#     so a copied-and-mutated document was publishable;
#   * `banned_values` defaulted to empty, so the value-level privacy sweep was
#     silently optional.
# The tests below assert the supported interface — not merely that one helper
# can reject a value if it is explicitly configured to.


def _privacy(fx):
    from jobs.ecodriving_dashboard.publication import PrivacyContext
    return PrivacyContext(
        identity_key=fx.SYNTHETIC_IDENTITY_KEY,
        client_code=fx.SYNTHETIC_CLIENT_CODE,
        person_names=("Jan Kowalski",),
        email_addresses=("jan.kowalski@example.invalid",),
    )


def _publishable(fx, **overrides):
    """One snapshot through the ONLY supported publisher entry point."""
    from datetime import date
    from jobs.ecodriving_dashboard.publication import build_publishable_snapshot

    days = fx.daily_inputs(date(2026, 7, 1), fx.WEEKLY_DAY_KM, fx.ACCEPTABLE_TOTALS)
    current = fx.period_from_days(fx.CURRENT_WEEKLY, days, ranking=fx.RANKED_FACTS)
    previous_days = fx.daily_inputs(date(2026, 7, 1), fx.PREVIOUS_DAY_KM, fx.PREVIOUS_TOTALS)
    previous = fx.period_from_days(fx.PREVIOUS_WEEKLY, previous_days,
                                   ranking=fx.PREVIOUS_RANKED_FACTS)
    params = dict(
        privacy=_privacy(fx),
        generated_at_utc=fx.GENERATED_AT,
        period_type="weekly",
        current=current,
        previous=previous,
        days=days,
        series=fx.FULL_SERIES,
    )
    params.update(overrides)
    return build_publishable_snapshot(**params)


def test_only_the_builder_result_can_be_serialised() -> None:
    """PART L — an arbitrary mapping has no route to publishable bytes."""
    import eco_dashboard_fixtures as fx
    from jobs.ecodriving_dashboard import publication as pub

    snapshot = _publishable(fx)
    body = pub.serialize_publishable_snapshot(snapshot)
    assert isinstance(body, bytes) and len(body) > 0
    assert json.loads(body.decode("utf-8"))["schema_version"] == 1
    # PART N item 5 — the digest is over exactly these bytes.
    assert snapshot.payload_digest == pub.payload_digest(body)

    # The general-purpose mapping -> bytes function is not part of the API.
    assert not hasattr(pub, "canonical_bytes"), "the ad-hoc mapping API still exists"
    assert hasattr(pub, "_canonical_bytes"), "the internal helper should remain, privately"
    public_names = [name for name in dir(pub) if not name.startswith("_")]
    assert "build_publishable_snapshot" in public_names
    assert "serialize_publishable_snapshot" in public_names

    # Nothing that merely LOOKS like a publishable snapshot is accepted.
    class LookAlike:
        body = b"{}"
        payload_digest = pub.payload_digest(b"{}")

    for impostor in (
        {"body": body, "payload_digest": snapshot.payload_digest},
        LookAlike(),
        body,
        None,
        snapshot.document,
    ):
        try:
            pub.serialize_publishable_snapshot(impostor)
        except pub.PublicationRefused:
            pass
        else:
            raise AssertionError(f"impostor accepted: {type(impostor).__name__}")

    # The result type cannot be constructed outside the builder either.
    try:
        pub.PublishableSnapshot(body=body, payload_digest=snapshot.payload_digest,
                                document={}, internal={}, privacy_values_applied=1)
    except pub.PublicationRefused as error:
        assert error.assertion == "A0"
    else:
        raise AssertionError("a publishable snapshot was fabricated directly")

    # A tampered body cannot be uploaded under the digest the Worker expects.
    tampered = _publishable(fx)
    object.__setattr__(tampered, "body", body + b" ")
    try:
        pub.serialize_publishable_snapshot(tampered)
    except pub.PublicationRefused as error:
        assert error.assertion == "A0"
    else:
        raise AssertionError("tampered bytes were serialised")
    print("PASS test_only_the_builder_result_can_be_serialised")


def test_privacy_context_is_mandatory_and_cannot_be_empty() -> None:
    """PART M — the A12 value sweep is not an optional caller argument."""
    import inspect
    import eco_dashboard_fixtures as fx
    from jobs.ecodriving_dashboard import publication as pub
    from jobs.ecodriving_dashboard import snapshot_builder, snapshot_contract

    signature = inspect.signature(pub.build_publishable_snapshot)
    assert "banned_values" not in signature.parameters, (
        "the publisher API still takes a bare optional banned_values")
    privacy = signature.parameters["privacy"]
    assert privacy.default is inspect.Parameter.empty, "privacy context is optional"

    # The layers underneath no longer default the sweep to empty either.
    for function in (snapshot_builder.build_driver_snapshot,
                     snapshot_contract.assert_snapshot_document):
        parameter = inspect.signature(function).parameters["banned_values"]
        assert parameter.default is inspect.Parameter.empty, function.__name__

    # Omitting the context is a TypeError, not a silently disabled check.
    try:
        _publishable(fx, privacy=None)
    except pub.PublicationRefused as error:
        assert error.assertion == "A12"
    else:
        raise AssertionError("a snapshot was published with no privacy context")

    # An empty context is refused at construction.
    for bad in ({"identity_key": "", "client_code": "C1"},
                {"identity_key": "K1", "client_code": "   "}):
        try:
            pub.PrivacyContext(**bad)
        except pub.PublicationRefused as error:
            assert error.assertion == "A12"
        else:
            raise AssertionError(f"empty privacy context accepted: {bad}")

    # The declared values really are applied.
    snapshot = _publishable(fx)
    assert snapshot.privacy_values_applied >= 4, snapshot.privacy_values_applied
    context = _privacy(fx)
    assert fx.SYNTHETIC_IDENTITY_KEY in context.forbidden_values()
    assert "Jan Kowalski" in context.forbidden_values()

    # A leaked identifier is caught without the caller having to remember it.
    leaking = pub.PrivacyContext(identity_key=fx.SYNTHETIC_IDENTITY_KEY,
                                 client_code=fx.SYNTHETIC_CLIENT_CODE,
                                 person_names=("2026-07-W3",))
    try:
        _publishable(fx, privacy=leaking)
    except pub.PublicationRefused as error:
        assert error.assertion == "A12"
        assert "2026-07-W3" not in str(error), "a refusal must not echo what it caught"
    else:
        raise AssertionError("a declared forbidden value was published")
    print(f"PASS test_privacy_context_is_mandatory_and_cannot_be_empty "
          f"({snapshot.privacy_values_applied} values applied)")


def test_fixed_vocabulary_fields_cannot_carry_source_strings() -> None:
    """PART M/O — an allowlisted label is not a free-text field."""
    import copy
    import eco_dashboard_fixtures as fx
    from jobs.ecodriving_dashboard.publication import PublicationRefused, _canonical_bytes
    from jobs.ecodriving_dashboard.snapshot_contract import (
        FIXED_FORMAT, FIXED_VOCABULARY, assert_fixed_vocabulary,
    )

    document = _publishable(fx).document
    banned = _privacy(fx).forbidden_values()
    assert _canonical_bytes(document, banned_values=banned)

    weekly = ("periods", "weekly", "current")

    def mutate(path, key, value):
        candidate = copy.deepcopy(document)
        node = candidate
        for step in path:
            node = node[step]
        node[key] = value
        return candidate

    # Exactly the two payloads the review demonstrated getting through.
    cases = {
        "person_name_in_label":
            mutate(weekly + ("categories", 0), "label", "Jan Kowalski"),
        "markup_in_label":
            mutate(weekly + ("categories", 0), "label", "<img src=x onerror=alert(1)>"),
        "person_name_in_short_label":
            mutate(weekly + ("categories", 1), "short_label", "Kowalski J."),
        "arbitrary_band_label":
            mutate(weekly + ("categories", 1), "band_label", "kierowca 42"),
        "arbitrary_rating_type": mutate(weekly, "rating_type", "wspaniały"),
        "arbitrary_ranking_state": mutate(weekly, "ranking_state", "ZWOLNIONY"),
        "arbitrary_status": mutate(weekly + ("categories", 0), "status", "czerwony"),
        "arbitrary_coaching_code":
            mutate(weekly + ("coaching", 0), "code", "SEE_YOUR_MANAGER"),
        "email_in_period_label": mutate(weekly, "period_label", "jan@example.invalid"),
        "free_text_timezone": mutate((), "timezone", "Jan Kowalski"),
        "arbitrary_weekday": mutate(weekly + ("days", 0), "weekday_short", "Kowalski"),
        "undeclared_string_field": mutate(weekly, "coach_note", "Zadzwoń do Jana"),
    }
    # The two payloads the review actually demonstrated must be caught by A15
    # itself — the vocabulary rule — not incidentally by some other assertion.
    must_be_a15 = {
        "person_name_in_label", "markup_in_label", "person_name_in_short_label",
        "arbitrary_band_label", "arbitrary_weekday", "free_text_timezone",
        "undeclared_string_field",
    }
    # `email_in_period_label` is caught by A7 (weekly period labels must match
    # their own start month) before A15 ever runs. It is still refused, which
    # is the point; asserting the exact assertion there would only encode the
    # order the checks happen to run in.
    caught: dict[str, str] = {}
    for name, candidate in cases.items():
        try:
            _canonical_bytes(candidate, banned_values=banned)
        except PublicationRefused as error:
            caught[name] = error.assertion
            assert "Kowalski" not in str(error), "a refusal must not echo what it caught"
        else:
            raise AssertionError(f"{name} was published")
    for name in must_be_a15:
        assert caught[name] == "A15", (name, caught[name])

    # The vocabulary really is the product contract's own, not a local table.
    assert FIXED_VOCABULARY["key"] >= {"overrev", "harsh_braking"}
    assert "Nadmierne obroty" in FIXED_VOCABULARY["label"]
    assert FIXED_VOCABULARY["code"] == frozenset(
        {"LARGEST_LOSS", "MOST_IMPROVED", "MOST_DETERIORATED", "BEST_OPPORTUNITY"})
    # Genuinely dynamic values stay dynamic: dates, instants and period labels
    # are validated by shape, not enumerated.
    for dynamic in ("period_label", "date", "generated_at_utc", "basis_period_label"):
        assert dynamic in FIXED_FORMAT and dynamic not in FIXED_VOCABULARY, dynamic
    assert_fixed_vocabulary(document)
    print(f"PASS test_fixed_vocabulary_fields_cannot_carry_source_strings "
          f"({len(cases)} vectors, {len(must_be_a15)} caught by A15 itself)")


def test_ad_hoc_documents_are_never_publishable() -> None:
    """PART O — the mutated/hand-built cases, through the supported API."""
    import copy
    import eco_dashboard_fixtures as fx
    from jobs.ecodriving_dashboard.publication import PublicationRefused, _canonical_bytes

    document = _publishable(fx).document
    banned = _privacy(fx).forbidden_values()

    def broken(mutator):
        candidate = copy.deepcopy(document)
        mutator(candidate)
        return candidate

    def set_ranking(candidate):
        # Out of range for the participant count that is published alongside
        # it. The contract has no roster and cannot recompute a ranking, but it
        # can and does refuse a position that contradicts its own block.
        candidate["periods"]["weekly"]["current"]["ranking_position"] = 9999

    def add_internal(candidate):
        candidate["periods"]["weekly"]["current"]["driver_id"] = 41

    def leak_identity(candidate):
        candidate["periods"]["weekly"]["current"]["period_label"] = fx.SYNTHETIC_IDENTITY_KEY

    candidates = {
        "empty": {},
        "identity_only": {"contract_id": "driver_eco_dashboard_snapshot", "schema_version": 1},
        "wrong_contract_id": broken(lambda d: d.__setitem__("contract_id", "other")),
        "wrong_schema_version": broken(lambda d: d.__setitem__("schema_version", 2)),
        "manipulated_ranking": broken(set_ranking),
        # A ranking value that is merely WRONG but internally consistent is not
        # detectable from the document alone. It is excluded from the supported
        # API a different way: `build_publishable_snapshot` takes
        # `PeriodInput`/`RankingFacts` and computes the block itself, so there
        # is no document parameter to manipulate — see the serialiser check
        # below, which every candidate here also fails.
        "forbidden_internal_field": broken(add_internal),
        "leaked_identity_value": broken(leak_identity),
        "not_a_mapping": "not a mapping",
        "list": [],
    }
    for name, candidate in candidates.items():
        try:
            _canonical_bytes(candidate, banned_values=banned)
        except PublicationRefused:
            pass
        else:
            raise AssertionError(f"ad-hoc document accepted: {name}")

    # And none of them can even reach the supported serialiser.
    from jobs.ecodriving_dashboard import publication as pub
    for candidate in candidates.values():
        try:
            pub.serialize_publishable_snapshot(candidate)
        except pub.PublicationRefused:
            pass
        else:
            raise AssertionError("an ad-hoc document reached the serialiser")
    print(f"PASS test_ad_hoc_documents_are_never_publishable ({len(candidates)} vectors)")


def test_canonical_serialisation_is_deterministic() -> None:
    """PART N — identical semantics, identical bytes, whatever the ordering."""
    import copy
    import json as jsonlib
    import random
    import eco_dashboard_fixtures as fx
    from jobs.ecodriving_dashboard.publication import PublicationRefused, _canonical_bytes
    from jobs.ecodriving_dashboard.snapshot_contract import canonical_json_bytes

    first = _publishable(fx)
    second = _publishable(fx)
    # 1. the same validated snapshot, built twice, is byte-identical.
    assert first.body == second.body
    assert first.payload_digest == second.payload_digest

    # 2. reordering every mapping changes nothing.
    def shuffled(node, rng):
        if isinstance(node, dict):
            items = [(k, shuffled(v, rng)) for k, v in node.items()]
            rng.shuffle(items)
            return dict(items)
        if isinstance(node, list):
            return [shuffled(v, rng) for v in node]
        return node

    rng = random.Random(20260819)
    reordered = shuffled(copy.deepcopy(first.document), rng)
    assert list(reordered.keys()) != list(first.document.keys()), "the shuffle did nothing"
    assert canonical_json_bytes(reordered) == first.body

    # 3. one semantic change moves the digest.
    changed = copy.deepcopy(first.document)
    changed["periods"]["weekly"]["current"]["eco_score_total"] += 1
    from jobs.ecodriving_dashboard.publication import payload_digest
    assert payload_digest(canonical_json_bytes(changed)) != first.payload_digest

    # Encoding properties, each asserted directly.
    text = first.body.decode("utf-8")
    # Unescaped UTF-8: Polish labels are literal bytes, not \uXXXX escapes.
    assert "\\u" not in text, "canonical output must be unescaped UTF-8"
    assert "Gwałtowne hamowania" in text
    # Compact separators and sorted keys, asserted exactly rather than by
    # looking for substrings that could occur inside a value.
    reparsed = jsonlib.loads(text)
    assert text == jsonlib.dumps(
        reparsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert reparsed == jsonlib.loads(jsonlib.dumps(first.document))
    assert reparsed["schema_version"] == 1, "the schema version is retained"

    # Non-JSON values have no canonical form and are refused, not coerced.
    from decimal import Decimal
    for bad in (float("nan"), float("inf"), Decimal("1.5"), {1: "int key"}, {"s"}):
        candidate = copy.deepcopy(first.document)
        candidate["periods"]["weekly"]["current"]["total_kilometers"] = bad
        try:
            canonical_json_bytes(candidate)
        except Exception as error:  # SnapshotContractError subclass
            assert "A16" in str(error) or "A15" in str(error), (bad, error)
        else:
            raise AssertionError(f"non-canonical value accepted: {bad!r}")

    # Stability across processes: a fresh interpreter, with a different hash
    # seed, must reproduce the same digest.
    child = (
        "import importlib.util, sys\n"
        "sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
        "import eco_dashboard_fixtures as fx\n"
        "spec = importlib.util.spec_from_file_location('prepub', %r)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(module._publishable(fx).payload_digest)\n"
    ) % (str(REPO_ROOT), str(REPO_ROOT / "ops" / "tests_manual"), str(Path(__file__).resolve()))
    result = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
        env={"PYTHONHASHSEED": "12345", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr[-600:]
    assert result.stdout.strip() == first.payload_digest, "digest is not stable across processes"

    try:
        _canonical_bytes({"contract_id": "driver_eco_dashboard_snapshot",
                          "schema_version": 1, "periods": {}}, banned_values=("x",))
    except PublicationRefused:
        pass
    else:
        raise AssertionError("an empty periods map was serialised")
    print("PASS test_canonical_serialisation_is_deterministic")


WORKER_GATE_SCRIPT = r"""
const path = await import('node:path');
const root = process.cwd();
await import(path.join(root, 'delivery/driver_eco_dashboard/local/node_runtime.js'));
const s = await import(path.join(root, 'delivery/driver_eco_dashboard/worker/lib/snapshot.js'));
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const payloads = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const out = {};
for (const [name, body] of Object.entries(payloads)) {
  const r = s.validateSnapshotText(body);
  out[name] = { ok: r.ok, reason: r.ok ? null : r.reason };
}
process.stdout.write(JSON.stringify(out));
"""


def test_canonical_bytes_round_trip_through_the_worker_gate() -> None:
    """PART N item 4 — the Worker's strict schema accepts canonical bytes."""
    import eco_dashboard_fixtures as fx
    from jobs.ecodriving_dashboard.snapshot_contract import canonical_json_bytes

    fixtures = fx.build_fixtures()
    payloads = {name: canonical_json_bytes(snapshot.document).decode("utf-8")
                for name, snapshot in fixtures.items()}
    payloads["supported_api"] = _publishable(fx).body.decode("utf-8")

    verdicts = node(WORKER_GATE_SCRIPT, stdin=json.dumps(payloads))
    assert len(verdicts) == len(payloads)
    for name, verdict in verdicts.items():
        assert verdict["ok"] is True, (name, verdict["reason"])
    print(f"PASS test_canonical_bytes_round_trip_through_the_worker_gate ({len(verdicts)} payloads)")


def test_trust_split_is_documented_and_enforced() -> None:
    module = (REPO_ROOT / "jobs" / "ecodriving_dashboard" / "publication.py").read_text(encoding="utf-8")
    assert "TRUST SPLIT" in module
    assert "HOST" in module and "WORKER" in module
    # Host side runs the business, vocabulary and value-level checks.
    assert "assert_snapshot_document" in module
    assert "serialize_document" in module
    # Worker side keeps the structural allowlist as defence in depth.
    worker_snapshot = source("worker/lib/snapshot.js")
    assert "validateSnapshotDocument" in worker_snapshot
    # The generator job goes through the supported entry point, not around it.
    job = (REPO_ROOT / "jobs" / "ecodriving_dashboard"
           / "job_eco_dashboard_snapshot.py").read_text(encoding="utf-8")
    assert "build_publishable_snapshot" in job
    assert "serialize_publishable_snapshot" in job
    assert "build_driver_snapshot" not in job, "the job bypasses the publisher API"
    print("PASS test_trust_split_is_documented_and_enforced")


# --- 6. publisher write transport -------------------------------------------


def test_publisher_transport_requires_machine_authentication() -> None:
    case = scenarios()["publisher_transport_authentication"]
    # Every unauthenticated shape answers identically and reveals nothing.
    for name in ("no_credential", "wrong_credential", "driver_capability_as_publisher",
                 "driver_session_as_publisher", "bearer_scheme_rejected",
                 "malformed_credential", "unconfigured_transport"):
        assert case[name] == 404, (name, case[name])
    assert case["valid_credential"] == 201
    # The publisher credential is useless on the driver path, and vice versa.
    assert case["publisher_token_as_driver_capability"] == 401
    assert case["leaks_credential"] is False
    assert case["methods"]["get_publish"] == 405

    auth = source("worker/lib/publisher_auth.js")
    assert "timingSafeEqualHex" in auth
    assert "PUBLISHER_KEY_DIGEST" in auth
    assert "NOT_CONFIGURED" in auth, "an unconfigured transport must fail closed"
    wrangler = source("wrangler.toml")
    assert "PUBLISHER_KEY_DIGEST" in wrangler, "the binding must be documented"
    assert re.search(r"^\s*PUBLISHER_KEY_DIGEST\s*=", wrangler, re.M) is None, \
        "no credential material in Git"
    print("PASS test_publisher_transport_requires_machine_authentication")


def test_publisher_transport_payload_and_key_rules() -> None:
    case = scenarios()["publisher_transport_payload_rules"]
    assert case["malformed_json"] == 422
    assert case["non_canonical_payload"] == 422, "an over-wide document must not be stored"
    assert case["privacy_invalid_payload"] == 422
    assert case["digest_mismatch"] == 400
    assert case["bad_operation_id"] == 400
    assert case["missing_subject"] == 400
    assert case["wrong_content_type"] == 400

    # There is no parameter for an R2 key at all, so a smuggling attempt is
    # simply ignored: the proof is that the caller's key was never created and
    # every stored key came from the Worker's own generator.
    assert case["caller_key_never_used"] is True
    assert case["keys_are_minted"] is True
    assert case["objects_written_by_probes"] == 1

    assert case["publish_status"] == 201
    assert case["binding_written"] is True, "the object must carry its subject binding"
    assert case["response_has_no_object_key"] is True
    assert case["response_has_no_subject"] is True

    worker = code_only(source("worker/index.js"))
    # The Worker supplies its own generator to the publication contract, which
    # claims the key inside the operation-creating INSERT. There is no code
    # path in which a caller-supplied key could be used.
    assert "mintObjectKey: mintObjectKey" in worker, "the Worker mints the key, never the caller"
    for banned in ("X-Object-Key", "body.object_key", "body.snapshot_object_key"):
        assert banned not in worker, banned
    print("PASS test_publisher_transport_payload_and_key_rules")


# --- 7. local end-to-end write -> read --------------------------------------


def test_publisher_write_to_driver_read() -> None:
    case = scenarios()["publisher_write_to_driver_read"]
    assert case["publish_status"] == 201
    assert case["exchange_status"] == 204
    assert case["snapshot_status"] == 200
    assert case["snapshot_matches_published"] is True
    assert case["objects"] == 1 and case["grants"] == 1 and case["operations"] == 1
    assert case["snapshot_cache_control"].startswith("private, no-store")
    assert case["response_has_no_subject"] is True
    assert case["response_has_no_object_key"] is True
    assert case["logs_leak_bearer"] is False
    assert case["logs_leak_publisher_token"] is False
    print("PASS test_publisher_write_to_driver_read")


# --- 10. test-double fidelity audit -----------------------------------------


def test_test_double_fidelity() -> None:
    case = scenarios()["test_double_fidelity"]
    assert case["r2_metadata_round_trip"] is True
    assert case["r2_missing_returns_null"] is True
    assert case["duplicate_digest_rejected"] is True
    assert case["defaults"]["revoked_at_is_null"] is True
    assert case["defaults"]["rotated_to_is_null"] is True
    assert case["defaults"]["session_epoch_zero"] is True
    assert case["revoke_unknown_changes"] == "UNKNOWN"
    assert case["revoke_real_changes"] == "REVOKED"
    assert case["revoke_repeat_changes"] == "ALREADY_REVOKED"
    assert case["rollback_threw"] is True
    assert case["rollback_restored"] is True
    print("PASS test_test_double_fidelity")


def main() -> None:
    test_capability_projection_names_every_state_column()
    test_emulator_models_real_projection()
    test_narrowed_projection_still_reproduces_the_defect()
    test_rotation_retry_classification()
    test_byob_availability_is_established_from_the_runtime()
    test_declared_size_gate_consumes_nothing()
    test_one_huge_chunk_is_bounded_only_in_byob_mode()
    test_tiny_chunk_flood_trips_the_ceiling_mid_stream()
    test_body_limit_boundaries_and_header_parsing()
    test_session_endpoint_rejects_oversized_bodies()
    test_no_false_chunk_size_guarantee_is_claimed_anywhere()
    test_publication_operation_lifecycle()
    test_publication_operations_are_conflict_safe_and_isolated()
    test_raw_bearer_loss_has_a_safe_recovery_path()
    test_operation_ids_cannot_reach_dashboard_data()
    test_host_persistence_contract_is_documented()
    test_subject_binding_vectors_agree_across_languages()
    test_subject_binding_encoding_is_unambiguous()
    test_only_the_builder_result_can_be_serialised()
    test_privacy_context_is_mandatory_and_cannot_be_empty()
    test_fixed_vocabulary_fields_cannot_carry_source_strings()
    test_ad_hoc_documents_are_never_publishable()
    test_canonical_serialisation_is_deterministic()
    test_canonical_bytes_round_trip_through_the_worker_gate()
    test_trust_split_is_documented_and_enforced()
    test_publisher_transport_requires_machine_authentication()
    test_publisher_transport_payload_and_key_rules()
    test_publisher_write_to_driver_read()
    test_test_double_fidelity()
    print("Driver Eco Dashboard V1 pre-publisher security gate tests passed")


if __name__ == "__main__":
    main()
