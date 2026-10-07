#!/usr/bin/env python3
"""Lost-bearer recovery object-integrity gate.

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_recovery_integrity.py

Closes the finding the independent R2 retry-integrity re-review raised.

MEDIUM   `recoverLostBearer()` bypassed object integrity inspection. It could
         change authorization state even though the authoritative snapshot
         object could not be proven valid. Confirmed:

             R2 unreadable -> recoverLostBearer() -> RECOVERED
             -> new bearer emitted
             -> bearer generation advanced 1 -> 2
             -> predecessor grant replaced.

THE CONTRACT THIS SUITE ASSERTS

  * there is ONE authoritative server-side object inspection contract —
    `inspectSnapshotObject`, reached as `services.inspectObject` — and BOTH
    operations that mutate or advance authorization state for an existing
    publication go through it: normal publication retry and lost-bearer
    recovery. Recovery has no separate, weaker integrity logic;
  * recovery proceeds ONLY for PRESENT_VALID;
  * ABSENT, PRESENT_INVALID and UNREADABLE all block recovery. Storage failure
    is never a reason to rotate authorization, and an absent object under an
    authoritative grant is a refusal rather than an opportunity;
  * a refused recovery performs ZERO authorization mutation, measured column by
    column: operation state, capability/grant id, bearer generation, grant row
    count, live grant count, predecessor revocation/rotation, replacement grant
    count, bearers returned, R2 bytes and R2 put count — and the recovery
    transaction is never even issued to D1;
  * restoring the exact valid object lets recovery succeed normally;
  * healthy recovery contention still yields exactly one replacement;
  * DELIVERED remains terminal against recovery.

`ops/tests_manual/eco_recovery_integrity_harness.mjs` drives the real Worker
over the local D1/R2 doubles with canonical bytes produced by the real Python
publisher — not a fixture file and not JavaScript.

Synthetic data only. No wrangler, no credentials, no remote Cloudflare
resource, no e-mail, no production data. No capability, session id, machine
credential or object key is printed.
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
if str(REPO_ROOT / "ops" / "tests_manual") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "ops" / "tests_manual"))

DELIVERY = REPO_ROOT / "delivery" / "driver_eco_dashboard"
HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_recovery_integrity_harness.mjs"

_CACHE: dict = {}


def _payloads() -> dict:
    """Reuse the byte-integrity suite's real host payloads verbatim."""
    from test_driver_eco_dashboard_byte_integrity import _host_payloads

    return _host_payloads()


def scenarios() -> dict:
    if not _CACHE:
        payload = {k: v for k, v in _payloads().items() if not k.startswith("_")}
        result = subprocess.run(
            ["node", str(HARNESS)], input=json.dumps(payload),
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=1200,
        )
        if result.returncode != 0:
            raise AssertionError(f"harness failed: {result.stderr.strip()[:900]}")
        _CACHE.update(json.loads(result.stdout))
        broken = {name: value.get("harness_error") for name, value in _CACHE.items()
                  if isinstance(value, dict) and value.get("harness_error")}
        if broken:
            stacks = [v.get("stack") for v in _CACHE.values()
                      if isinstance(v, dict) and v.get("stack")]
            raise AssertionError(f"harness scenario errors: {broken} :: {stacks[:1]}")
    return _CACHE


def source(relative: str) -> str:
    return (DELIVERY / relative).read_text(encoding="utf-8")


def code(relative: str) -> str:
    """Source with comments removed, so a prose mention is not evidence."""
    text = source(relative)
    text = re.sub(r"/\*[\s\S]*?\*/", " ", text)
    text = re.sub(r"(?m)^\s*//.*$", " ", text)
    return text


# --- shared shape of a fail-closed recovery ----------------------------------


def assert_recovery_fails_closed(case: dict, label: str) -> None:
    """Every requirement a refused recovery must satisfy, in one place.

    Nothing here is inferred from the HTTP status. Each authorization column
    the recovery transaction would have moved is compared directly across the
    call, because a 409 with a rotated generation would satisfy a status-only
    assertion and would be exactly the defect.
    """
    # The refusal itself.
    assert case["refused_bearer"] is False, f"{label}: a replacement bearer was returned"
    assert case["refused_status"] in {409, 503}, f"{label}: {case['refused_status']}"
    assert case["refused_error"] in {"OBJECT_INTEGRITY_FAILURE", "OBJECT_UNREADABLE"}, \
        f"{label}: {case['refused_error']}"
    assert case["refused_next_action"] in {
        "INVESTIGATE_OBJECT_INTEGRITY", "RETRY_AFTER_STORAGE_RECOVERS"}, label
    # The refusal must not describe the private object.
    assert case["refused_body_names_invariant"] is False, \
        f"{label}: the response named the failing invariant"

    # THE precondition claim, measured: the D1 recovery transaction never ran.
    assert case["recovery_statements_issued"] == 0, \
        f"{label}: a recovery statement reached D1 despite the refusal"

    # ZERO AUTHORIZATION MUTATION.
    assert case["state_before"] == case["state_after"] == "GRANT_MINTED", label
    assert case["capability_id_unchanged"] is True, f"{label}: the grant identity moved"
    assert case["generation_before"] == case["generation_after"] == 1, \
        f"{label}: the bearer generation advanced"
    assert case["grant_rows_before"] == case["grant_rows_after"] == 1, \
        f"{label}: a replacement grant row was created"
    assert case["live_grants_before"] == case["live_grants_after"] == 1, label
    assert case["predecessor_revoked_before"] == case["predecessor_revoked_after"] is None, \
        f"{label}: the predecessor was revoked"
    assert case["predecessor_rotated_before"] == case["predecessor_rotated_after"] is False, \
        f"{label}: the predecessor was superseded"
    assert case["predecessor_is_current_after"] is True, \
        f"{label}: the predecessor stopped being the operation's grant"
    assert case["ledger_digest_before"] == case["ledger_digest_after"], label

    # ZERO OBJECT MUTATION: inspection is read-only and never repairs.
    assert case["put_count_delta"] == 0, f"{label}: recovery wrote to R2"
    assert case["bytes_unchanged"] is True, f"{label}: the stored octets changed"

    # The authorization the host already holds is undamaged.
    assert case["predecessor_session_status"] == 204, \
        f"{label}: the refusal degraded the existing bearer"

    # And the refusal did not poison the operation.
    assert case["resumed_status"] == 200, f"{label}: recovery did not converge after repair"
    assert case["resumed_result"] == "RECOVERED", label
    assert case["resumed_bearer"] is True, label
    assert case["resumed_generation"] == 2, label
    assert case["resumed_grant_rows"] == 2, label
    assert case["resumed_live_grants"] == 1, label
    assert case["resumed_predecessor_revoked"] is True, label
    assert case["resumed_predecessor_rotated"] is True, label
    assert case["resumed_bytes_are_host_bytes"] is True, label
    assert case["replacement_session_status"] == 204, label
    assert case["replacement_snapshot_status"] == 200, label


# --- A. healthy object --------------------------------------------------------


def test_a_healthy_object_recovers_normally() -> None:
    case = scenarios()["a_healthy_object_recovers"]
    assert case["publish_status"] == 201
    assert case["recover_status"] == 200
    assert case["recover_result"] == "RECOVERED"
    # Exactly one replacement grant, one generation step, one raw bearer.
    assert case["replacement_grants"] == 1
    assert case["bearers_returned"] == 1
    assert case["generation_before"] == 1
    assert case["generation_after"] == 2
    assert case["grant_rows_after"] == 2
    assert case["live_grants_after"] == 1
    assert case["capability_changed"] is True
    # The predecessor becomes invalid.
    assert case["predecessor_revoked"] is True
    assert case["predecessor_rotated"] is True
    assert case["predecessor_is_current_after"] is False
    assert case["predecessor_session_status"] == 401
    # The gate is read-only: proving the object never rewrites it.
    assert case["put_count_delta"] == 0
    assert case["bytes_unchanged"] is True
    assert case["bytes_are_host_bytes"] is True
    assert case["metadata_digest"] == case["ledger_digest_after"] == _payloads()["digest_a"]
    # The replacement link genuinely works.
    assert case["replacement_session_status"] == 204
    assert case["replacement_snapshot_status"] == 200
    print("PASS test_a_healthy_object_recovers_normally")


# --- B..H. every object-integrity failure ------------------------------------


def test_b_absent_object_blocks_recovery() -> None:
    """An operation claiming an authoritative snapshot whose object is gone."""
    case = scenarios()["b_absent_object_blocks_recovery"]
    assert_recovery_fails_closed(case, "absent object")
    # ABSENT is a refusal for recovery, not a write opportunity.
    assert case["refused_error"] == "OBJECT_INTEGRITY_FAILURE"
    assert case["refused_status"] == 409
    print("PASS test_b_absent_object_blocks_recovery")


def test_c_body_corruption_blocks_recovery() -> None:
    case = scenarios()["c_body_corruption_blocks_recovery"]
    assert_recovery_fails_closed(case, "body corruption")
    assert case["refused_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_c_body_corruption_blocks_recovery")


def test_d_metadata_digest_corruption_blocks_recovery() -> None:
    case = scenarios()["d_metadata_digest_corruption_blocks_recovery"]
    assert_recovery_fails_closed(case, "metadata digest corruption")
    assert case["refused_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_d_metadata_digest_corruption_blocks_recovery")


def test_e_missing_metadata_digest_blocks_recovery() -> None:
    case = scenarios()["e_missing_metadata_digest_blocks_recovery"]
    assert_recovery_fails_closed(case, "missing metadata digest")
    assert case["refused_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_e_missing_metadata_digest_blocks_recovery")


def test_f_coordinated_rewrite_blocks_recovery() -> None:
    """Body and metadata rewritten together; only the ledger disagrees.

    The substituted object is self-consistent, so a self-consistency check
    alone would have passed it. That is why the ledger digest is an authority.
    """
    case = scenarios()["f_coordinated_rewrite_blocks_recovery"]
    assert_recovery_fails_closed(case, "coordinated rewrite")
    assert case["refused_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_f_coordinated_rewrite_blocks_recovery")


def test_g_subject_binding_corruption_blocks_recovery() -> None:
    case = scenarios()["g_subject_binding_corruption_blocks_recovery"]
    assert_recovery_fails_closed(case, "subject binding corruption")
    assert case["refused_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_g_subject_binding_corruption_blocks_recovery")


def test_h_missing_subject_binding_blocks_recovery() -> None:
    case = scenarios()["h_missing_subject_binding_blocks_recovery"]
    assert_recovery_fails_closed(case, "missing subject binding")
    assert case["refused_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_h_missing_subject_binding_blocks_recovery")


# --- I..K. an unreadable object ----------------------------------------------


def test_i_r2_get_failure_blocks_recovery() -> None:
    """THE confirmed finding: R2 unreadable used to return RECOVERED."""
    case = scenarios()["i_get_failure_blocks_recovery"]
    assert_recovery_fails_closed(case, "get failure")
    assert case["refused_error"] == "OBJECT_UNREADABLE", \
        "a thrown get was not distinguished from a corrupt object"
    assert case["refused_status"] == 503
    print("PASS test_i_r2_get_failure_blocks_recovery")


def test_j_body_read_failure_blocks_recovery() -> None:
    case = scenarios()["j_body_read_failure_blocks_recovery"]
    assert_recovery_fails_closed(case, "body read failure")
    assert case["refused_error"] == "OBJECT_UNREADABLE"
    print("PASS test_j_body_read_failure_blocks_recovery")


def test_k_metadata_access_failure_blocks_recovery() -> None:
    case = scenarios()["k_metadata_failure_blocks_recovery"]
    assert_recovery_fails_closed(case, "metadata access failure")
    assert case["refused_error"] == "OBJECT_UNREADABLE"
    print("PASS test_k_metadata_access_failure_blocks_recovery")


# --- L..O. a malformed RESOLVED result ---------------------------------------


def test_l_a_resolved_undefined_blocks_recovery() -> None:
    """`undefined` is not the documented missing-key value; it is malformed."""
    case = scenarios()["l_undefined_result_blocks_recovery"]
    assert_recovery_fails_closed(case, "resolved undefined")
    assert case["refused_error"] == "OBJECT_UNREADABLE", \
        "a malformed result was classified as an object-integrity verdict"
    assert case["refused_status"] == 503
    print("PASS test_l_a_resolved_undefined_blocks_recovery")


def test_m_a_result_without_a_key_blocks_recovery() -> None:
    """THE confirmed finding: valid body and metadata, no echoed key, RECOVERED."""
    case = scenarios()["m_result_without_key_blocks_recovery"]
    assert_recovery_fails_closed(case, "result without a key")
    assert case["refused_error"] == "OBJECT_UNREADABLE"
    assert case["refused_status"] == 503
    print("PASS test_m_a_result_without_a_key_blocks_recovery")


def test_n_a_non_string_key_blocks_recovery() -> None:
    case = scenarios()["n_non_string_key_blocks_recovery"]
    assert_recovery_fails_closed(case, "non-string key")
    assert case["refused_error"] == "OBJECT_UNREADABLE"
    print("PASS test_n_a_non_string_key_blocks_recovery")


def test_o_a_wrong_string_key_keeps_its_integrity_verdict() -> None:
    """A well-formed key naming another object stays an integrity refusal."""
    case = scenarios()["o_wrong_string_key_blocks_recovery"]
    assert_recovery_fails_closed(case, "wrong string key")
    assert case["refused_error"] == "OBJECT_INTEGRITY_FAILURE", \
        "the pre-existing wrong-key refusal was weakened"
    assert case["refused_status"] == 409
    print("PASS test_o_a_wrong_string_key_keeps_its_integrity_verdict")


# --- P..Q. an ARRAY result ---------------------------------------------------


def test_p_a_bare_array_blocks_recovery() -> None:
    case = scenarios()["p_bare_array_blocks_recovery"]
    assert_recovery_fails_closed(case, "bare array")
    assert case["refused_error"] == "OBJECT_UNREADABLE"
    assert case["refused_status"] == 503
    print("PASS test_p_a_bare_array_blocks_recovery")


def test_q_a_decorated_array_blocks_recovery() -> None:
    """THE confirmed blocker: an array carrying every property a result has."""
    case = scenarios()["q_decorated_array_blocks_recovery"]
    assert_recovery_fails_closed(case, "decorated array")
    assert case["refused_error"] == "OBJECT_UNREADABLE"
    assert case["refused_status"] == 503
    print("PASS test_q_a_decorated_array_blocks_recovery")


# --- the ONE integrity gate ---------------------------------------------------


def test_recovery_uses_the_shared_inspection_contract() -> None:
    """Measured, not read off the source.

    `services.inspectObject` is wrapped in a counter around the REAL
    `inspectSnapshotObject` and `recoverLostBearer` is called directly. If
    recovery carried its own integrity logic the counter would stay at zero.
    """
    case = scenarios()["recovery_uses_the_shared_inspection_contract"]
    assert case["inspections"] == 2, "recovery did not consult the shared inspection"
    for call in case["inspection_calls"]:
        # The inspection is bound to the operation-owned identity, not to
        # anything the caller supplied.
        assert call["key_is_operation_owned"] is True
        assert call["subject_is_operation_subject"] is True
        assert call["digest_is_ledger_digest"] is True
    assert case["inspection_calls"][0]["state"] == "PRESENT_VALID"
    assert case["healthy_status"] == "RECOVERED"
    assert case["healthy_bearer"] is True
    assert case["inspection_calls"][1]["state"] == "UNREADABLE"
    assert case["broken_status"] == "OBJECT_UNREADABLE"
    assert case["broken_bearer"] is False
    assert case["generation_unchanged"] is True
    assert case["grant_rows_unchanged"] is True
    print("PASS test_recovery_uses_the_shared_inspection_contract")


def test_there_is_one_object_inspection_implementation() -> None:
    """No second, weaker integrity path may exist for recovery."""
    publisher = code("worker/lib/publisher.js")
    publication = code("worker/lib/publication.js")
    index = code("worker/index.js")

    # Exactly one inspection function, in one module.
    assert publisher.count("export async function inspectSnapshotObject") == 1
    assert "inspectSnapshotObject" not in publication, \
        "publication.js reaches around the services bundle"
    # One services bundle wires it, for every publication route.
    assert index.count("services.inspectObject = ") == 1
    assert index.count("inspectSnapshotObject(services, params)") == 1

    # Both authorization-moving operations call it, and nothing else does its
    # integrity work by hand.
    publish_body = publication.split("export async function publishSnapshot")[1] \
                              .split("export async function recoverLostBearer")[0]
    recover_body = publication.split("export async function recoverLostBearer")[1] \
                              .split("export async function recordDeliveryPhase")[0]
    assert "services.inspectObject(" in publish_body
    assert "services.inspectObject(" in recover_body
    for forbidden in ("payloadDigest(", "subjectBindingDigest(", "customMetadata"):
        assert forbidden not in recover_body, \
            f"recovery re-implements integrity checking: {forbidden}"

    # Recovery permits exactly one verdict, and its refusals are fail-closed.
    assert "OBJECT_STATE.PRESENT_VALID" in recover_body
    assert "OBJECT_STATE.UNREADABLE" in recover_body
    assert "OBJECT_STATE.ABSENT" in recover_body
    assert "OBJECT_STATE.PRESENT_INVALID" in recover_body
    # The gate precedes the transaction in the only order that matters.
    assert recover_body.index("services.inspectObject(") < \
        recover_body.index("recoverGrantTransactionally("), \
        "the integrity gate does not precede the recovery transaction"

    # And both refusals are rendered by one shared responder, so the two routes
    # cannot drift apart again.
    assert index.count("function objectStateRefusal(") == 1
    assert index.count('objectStateRefusal(result, env, "') == 2, \
        "the publish and recover routes do not both use the shared responder"
    print("PASS test_there_is_one_object_inspection_implementation")


def test_the_recovery_transaction_pins_the_inspected_identity() -> None:
    """The D1 compare-and-set names the object identity that was proven."""
    publication = code("worker/lib/publication.js")
    recovery_sql = publication.split("async recoverGrantTransactionally")[1] \
                              .split("async advanceDelivery")[0]
    # Both immutable identity columns are guarded, in both statements that can
    # move authorization.
    assert recovery_sql.count("payload_digest = ?") == 2, recovery_sql[:400]
    assert recovery_sql.count("snapshot_object_key = ?") == 2
    assert "params.payload_digest" in recovery_sql
    assert "params.snapshot_object_key" in recovery_sql
    # No UPDATE in this module rewrites either column, which is what makes the
    # guard total rather than advisory. Only the SET clause is examined; both
    # columns legitimately appear in WHERE guards.
    updates = re.findall(r"UPDATE eco_publication_operation\s+SET([\s\S]*?)WHERE", publication)
    assert len(updates) >= 4, updates
    for assignments in updates:
        assert "payload_digest" not in assignments, assignments
        assert "snapshot_object_key" not in assignments, assignments
    print("PASS test_the_recovery_transaction_pins_the_inspected_identity")


# --- the recurrence detector --------------------------------------------------


def test_recovery_never_stops_consulting_object_inspection() -> None:
    """The focused recurrence test, with its own mutation proof.

    `guarded` is the production module: a valid publication, R2 get injected to
    fail, recovery attempted. `bypassed` is the SAME scenario against a copy of
    `publication.js` with the PRESENT_VALID gate deleted and nothing else
    changed — it must rotate, which is what proves the assertions below test
    the guard rather than some incidental refusal.
    """
    case = scenarios()["recurrence_recovery_consults_object_inspection"]
    guarded = case["guarded"]
    # The object logically exists and is healthy; only the read is broken.
    assert guarded["object_logically_exists"] is True
    assert guarded["status"] == 503
    assert guarded["error"] == "OBJECT_UNREADABLE"
    assert guarded["bearer"] is False
    # THE assertions.
    assert guarded["generation_before"] == guarded["generation_after"] == 1
    assert guarded["replacement_grants"] == 0
    assert guarded["live_grants_after"] == 1
    assert guarded["predecessor_still_current"] is True
    assert guarded["recovery_statements_issued"] == 0
    # Once storage recovers the operation converges, with no repair.
    assert guarded["healed"]["status"] == 200
    assert guarded["healed"]["result"] == "RECOVERED"
    assert guarded["healed"]["bearer"] is True
    assert guarded["healed"]["generation"] == 2
    assert guarded["healed"]["live_grants"] == 1
    assert guarded["healed"]["puts_total"] == 1

    bypassed = case["bypassed"]
    assert bypassed.get("mutation_error") is None, bypassed
    assert bypassed["gate_removed"] is True
    assert bypassed["publish_gate_intact"] is True, \
        "the mutation removed the publication gate too, so it proves nothing"
    # Without the gate, the exact scenario above rotates authorization. That is
    # the finding, and it is what makes this test load-bearing.
    assert bypassed["status"] == "RECOVERED"
    assert bypassed["bearer"] is True
    assert bypassed["generation_before"] == 1
    assert bypassed["generation_after"] == 2
    assert bypassed["replacement_grants"] == 1
    assert bypassed["predecessor_revoked"] is True
    print("PASS test_recovery_never_stops_consulting_object_inspection")


def test_recovery_requires_a_proven_object_key() -> None:
    """RECURRENCE DETECTOR: absence of a key is not evidence of equality.

    The object is present with an intact body and intact metadata; only the
    echoed key is gone. The confirmed defect skipped the comparison, returned
    PRESENT_VALID and ran recovery to completion: generation 1 -> 2, the
    predecessor revoked and superseded, one replacement grant, one new raw
    bearer.

    The mutation proof is what makes this load-bearing: the same scenario runs
    against a copy of `publisher.js` whose key check is reverted to the
    conditional form. That copy must ROTATE.
    """
    case = scenarios()["recurrence_recovery_requires_a_proven_key"]
    guarded, bypassed = case["guarded"], case["bypassed"]

    # The fixture must be the real one, or the test proves nothing.
    assert guarded["object_logically_exists"] is True
    assert guarded["body_and_metadata_intact"] is True, \
        "the fixture corrupted more than the echoed key"

    assert guarded["status"] == 503
    assert guarded["error"] == "OBJECT_UNREADABLE"
    assert guarded["bearer"] is False, "a replacement bearer was returned"
    # THE assertions.
    assert guarded["generation_before"] == guarded["generation_after"] == 1, \
        "the bearer generation advanced over an unprovable object"
    assert guarded["replacement_grants"] == 0, "a replacement grant was created"
    assert guarded["capability_id_unchanged"] is True
    assert guarded["live_grants_before"] == guarded["live_grants_after"] == 1
    assert guarded["predecessor_revoked_before"] == guarded["predecessor_revoked_after"] is None
    assert guarded["predecessor_rotated_after"] is False
    assert guarded["predecessor_still_current"] is True
    assert guarded["recovery_statements_issued"] == 0, \
        "a recovery statement reached D1 despite the refusal"
    assert guarded["put_count_delta"] == 0
    # The bearer the host already holds is untouched and still reads.
    assert guarded["predecessor_session_status"] == 204
    assert guarded["predecessor_snapshot_status"] == 200

    # Exact restoration: recovery then proceeds normally, once.
    restored = guarded["restored"]
    assert restored["status"] == 200
    assert restored["result"] == "RECOVERED"
    assert restored["bearer"] is True
    assert restored["generation"] == 2, "generation did not advance exactly once"
    assert restored["replacement_grants"] == 1
    assert restored["live_grants"] == 1
    assert restored["predecessor_revoked"] is True
    assert restored["predecessor_rotated"] is True
    assert restored["predecessor_session_status"] == 401, \
        "the predecessor is still usable after a successful recovery"
    assert restored["replacement_session_status"] == 204
    assert restored["replacement_snapshot_status"] == 200
    assert restored["put_count_delta"] == 0, "recovery wrote to R2"

    # THE MUTATION PROOF.
    assert "mutation_error" not in bypassed, bypassed
    assert bypassed["mutant_verdict_state"] == "PRESENT_VALID", \
        "the mutant did not reproduce the defect; the detector proves nothing"
    assert bypassed["mutant_verdict_reusable"] is True
    assert bypassed["status"] == "RECOVERED"
    assert bypassed["bearer"] is True
    assert bypassed["generation_before"] == 1
    assert bypassed["generation_after"] == 2, \
        "the mutant did not rotate, so the guarded assertions are not load-bearing"
    assert bypassed["replacement_grants"] == 1
    assert bypassed["predecessor_revoked"] is True
    assert bypassed["predecessor_rotated"] is True
    print("PASS test_recovery_requires_a_proven_object_key")


def test_recovery_rejects_a_decorated_array_result() -> None:
    """RECURRENCE DETECTOR: an array cannot move authorization.

    The stored object is untouched; only the RESULT is an Array carrying the
    operation-owned key, a reader over the real octets and the real metadata,
    so every content check would pass. The confirmed defect classified it
    PRESENT_VALID and rotated: generation 1 -> 2, predecessor replaced, one
    replacement bearer.
    """
    case = scenarios()["recurrence_recovery_rejects_decorated_array"]
    carried, guarded, bypassed = case["carried"], case["guarded"], case["bypassed"]

    # The fixture must genuinely be the masquerading value.
    assert carried["is_array"] is True
    assert carried["typeof_is_object"] is True, \
        "the generic object test no longer accepts an array; the case proves nothing"
    for field in ("key_matches", "has_body_reader", "body_is_the_stored_octets",
                  "metadata_digest_is_correct", "metadata_binding_present"):
        assert carried[field] is True, f"the decorated array lacks {field}"

    assert guarded["object_logically_exists"] is True
    assert guarded["status"] == 503
    assert guarded["error"] == "OBJECT_UNREADABLE"
    assert guarded["bearer"] is False
    # ZERO AUTHORIZATION MUTATION.
    assert guarded["state_before"] == guarded["state_after"] == "GRANT_MINTED"
    assert guarded["generation_before"] == guarded["generation_after"] == 1
    assert guarded["capability_id_unchanged"] is True
    assert guarded["replacement_grants"] == 0
    assert guarded["live_grants_before"] == guarded["live_grants_after"] == 1
    assert guarded["predecessor_revoked_before"] == guarded["predecessor_revoked_after"] is None
    assert guarded["predecessor_rotated_after"] is False
    assert guarded["predecessor_still_current"] is True
    assert guarded["recovery_statements_issued"] == 0, \
        "a recovery statement reached D1 despite the refusal"
    assert guarded["put_count_delta"] == 0
    assert guarded["bytes_unchanged"] is True
    # The predecessor remains authoritative because recovery did not occur.
    assert guarded["predecessor_session_status"] == 204
    assert guarded["predecessor_snapshot_status"] == 200

    restored = guarded["restored"]
    assert restored["status"] == 200
    assert restored["result"] == "RECOVERED"
    assert restored["bearer"] is True
    assert restored["generation"] == 2
    assert restored["replacement_grants"] == 1
    assert restored["live_grants"] == 1
    assert restored["predecessor_revoked"] is True
    assert restored["predecessor_rotated"] is True
    assert restored["predecessor_session_status"] == 401
    assert restored["replacement_session_status"] == 204
    assert restored["replacement_snapshot_status"] == 200
    assert restored["put_count_delta"] == 0
    assert restored["bytes_are_host_bytes"] is True
    assert restored["ledger_digest_unchanged"] is True

    # THE MUTATION PROOF.
    assert "mutation_error" not in bypassed, bypassed
    assert bypassed["mutant_verdict_state"] == "PRESENT_VALID", \
        "the mutant did not reproduce the defect; the detector proves nothing"
    assert bypassed["mutant_verdict_reusable"] is True
    assert bypassed["status"] == "RECOVERED"
    assert bypassed["bearer"] is True
    assert bypassed["generation_before"] == 1
    assert bypassed["generation_after"] == 2, \
        "the mutant did not rotate, so the guarded assertions are not load-bearing"
    assert bypassed["replacement_grants"] == 1
    assert bypassed["predecessor_revoked"] is True
    assert bypassed["predecessor_rotated"] is True
    print("PASS test_recovery_rejects_a_decorated_array_result")


# --- concurrency and the rest of the state machine ---------------------------


def test_healthy_recovery_contention_still_yields_one_replacement() -> None:
    case = scenarios()["healthy_recovery_contention"]
    for callers in (2, 8, 32):
        label = f"callers_{callers}"
        result = case[label]
        assert result["recovered"] == 1, f"{label}: {result['recovered']} replacements"
        assert result["bearers_returned"] == 1, label
        assert result["safe_conflicts"] == callers - 1, label
        assert result["conflict_reasons"] == ["SUPERSEDED"], label
        # The gate must not turn a healthy race into an integrity failure.
        assert result["integrity_refusals"] == 0, label
        assert result["generation_delta"] == 1, label
        assert result["grant_rows"] == 2, label
        assert result["live_grants"] == 1, label
        assert result["put_count_delta"] == 0, label
        assert result["bytes_are_host_bytes"] is True, label
        assert result["state_after"] == "GRANT_MINTED", label

    forced = case["forced_16"]
    assert forced["recovered"] == 1
    assert forced["bearers_returned"] == 1
    assert forced["generation_after"] == 2
    assert forced["grant_rows"] == 2
    assert forced["live_grants"] == 1
    assert forced["put_count_delta"] == 0
    print("PASS test_healthy_recovery_contention_still_yields_one_replacement (2/8/32 + 16 forced)")


def test_recovery_against_delivery_rollback_retry_and_revocation() -> None:
    case = scenarios()["recovery_against_terminal_and_rollback"]

    delivered = case["delivered"]
    assert delivered["state_before"] == "DELIVERED"
    assert delivered["status"] == 409
    assert delivered["result"] == "NOT_RECOVERABLE"
    assert delivered["reason"] == "ALREADY_DELIVERED"
    assert delivered["bearer"] is False
    assert delivered["generation_before"] == delivered["generation_after"] == 1
    assert delivered["grant_rows_unchanged"] is True
    # Terminal is decided before the object is consulted, so a healthy object
    # cannot make a delivered operation recoverable.
    assert delivered["recovery_statements_issued"] == 0
    assert delivered["delivered_bearer_session_status"] == 204
    assert delivered["delivered_bearer_snapshot_status"] == 200

    rollback = case["rollback"]
    assert rollback["status"] == 503
    assert rollback["generation_before"] == rollback["generation_after"] == 1
    assert rollback["grant_rows_after"] == 1
    assert rollback["live_grants_after"] == 1
    assert rollback["predecessor_revoked_after"] is False
    assert rollback["retry_status"] == 200
    assert rollback["retry_result"] == "RECOVERED"
    assert rollback["retry_generation"] == 2
    assert rollback["retry_live_grants"] == 1

    retry = case["publish_retry"]
    assert retry["retry_status"] == 200
    assert retry["retry_result"] == "ALREADY_PUBLISHED"
    assert retry["retry_bearer"] is False
    assert retry["recover_status"] == 200
    assert retry["recover_result"] == "RECOVERED"
    assert retry["generation_before"] == 1
    assert retry["generation_after"] == 2
    assert retry["live_grants_after"] == 1
    assert retry["grant_rows_after"] == 2

    revoked = case["predecessor_revoked"]
    assert revoked["status"] == 409
    assert revoked["result"] == "NOT_RECOVERABLE"
    assert revoked["reason"] == "GRANT_NOT_ELIGIBLE"
    assert revoked["bearer"] is False
    assert revoked["generation_before"] == revoked["generation_after"] == 1
    assert revoked["grant_rows_unchanged"] is True
    print("PASS test_recovery_against_delivery_rollback_retry_and_revocation")


# --- protocol shape on the authorization-moving routes -----------------------


def test_recovery_and_delivery_routes_enforce_the_protocol_contract() -> None:
    """Singleton control headers and the exact media type, with zero effect.

    Measured on the routes that MOVE authorization, not only on `/api/publish`:
    every rejected shape must leave the generation, the grant identity, the
    grant rows and R2 untouched, and must not issue a single recovery
    statement to D1.
    """
    case = scenarios()["recovery_route_protocol_shape"]

    accepted = {
        "recover_baseline", "recover_content_type_absent_is_fine",
        "recover_content_type_exact", "recover_content_type_charset",
        "delivery_baseline",
    }
    for name in accepted:
        assert case[name]["status"] == 200, (name, case[name])

    # The three recover forms that are accepted must all actually recover.
    for name in ("recover_baseline", "recover_content_type_absent_is_fine",
                 "recover_content_type_exact", "recover_content_type_charset"):
        assert case[name]["bearer"] is True, name
        assert case[name]["generation_after"] == 2, name

    rejected = {
        # Singleton control headers.
        "recover_operation_duplicate_same": "AMBIGUOUS_CONTROL_HEADER",
        "recover_operation_duplicate_different": "AMBIGUOUS_CONTROL_HEADER",
        "recover_operation_comma_joined": "AMBIGUOUS_CONTROL_HEADER",
        "recover_operation_missing": "INVALID_CONTROL_HEADER",
        # A combined credential fails closed exactly like any auth failure.
        "recover_authorization_duplicate_same": "SNAPSHOT_UNAVAILABLE",
        "recover_authorization_duplicate_different": "SNAPSHOT_UNAVAILABLE",
        # Media type: one unambiguous value, parsed for equality.
        "recover_content_type_duplicated": "AMBIGUOUS_CONTROL_HEADER",
        "recover_content_type_jsonp": "INVALID_CONTROL_HEADER",
        "recover_content_type_json_seq": "INVALID_CONTROL_HEADER",
        "delivery_content_type_duplicated": "AMBIGUOUS_CONTROL_HEADER",
        "delivery_content_type_jsonp": "INVALID_CONTROL_HEADER",
    }
    for name, error in rejected.items():
        result = case[name]
        assert result["status"] in {400, 404}, (name, result)
        assert result["error"] == error, (name, result)
        assert result["bearer"] is False, name
        # ZERO side effect: the request never reached a mutation.
        assert result["generation_before"] == result["generation_after"] == 1, name
        assert result["capability_unchanged"] is True, name
        assert result["grant_rows_before"] == result["grant_rows_after"] == 1, name
        assert result["live_grants_after"] == 1, name
        assert result["state_after"] == "GRANT_MINTED", name
        assert result["put_count_delta"] == 0, name
        assert result["recovery_statements_issued"] == 0, name

    # And the contract is one parser, not a per-route re-implementation.
    index = code("worker/index.js")
    assert index.count("function checkOptionalPublisherContentType(") == 1
    # EVERY body-less publisher route applies it: recover, delivery and the
    # authorization-state maintenance route. The count is asserted rather than
    # bounded below so a NEW body-less route cannot quietly skip the contract.
    assert index.count("= checkOptionalPublisherContentType(request);") == 3, \
        "a body-less publisher route does not apply the media-type contract"
    assert "startsWith(\"application/json\")" not in index, "a prefix match reappeared"
    print("PASS test_recovery_and_delivery_routes_enforce_the_protocol_contract")


# --- local end to end ---------------------------------------------------------


def test_local_end_to_end_publish_recover_corrupt_restore_deliver() -> None:
    case = scenarios()["local_end_to_end"]
    # 1. canonical bytes -> publisher route -> R2 -> grant -> session -> 200.
    assert case["publish_status"] == 201
    assert case["publish_result"] == "PUBLISHED"
    assert case["first_session_status"] == 204
    assert case["first_snapshot_status"] == 200
    assert case["ledger_digest"] == _payloads()["digest_a"]
    assert case["stored_bytes_are_host_bytes"] is True

    # 2. healthy recovery: replacement works, predecessor denied, bytes stable.
    assert case["recover_status"] == 200
    assert case["recover_result"] == "RECOVERED"
    assert case["replacement_session_status"] == 204
    assert case["replacement_snapshot_status"] == 200
    assert case["snapshot_digest_stable"] is True
    assert case["predecessor_session_after_recovery"] == 401

    # 3. every invalid/unreadable object state refuses with zero mutation, and
    #    the bearer the host already holds keeps working throughout.
    expected = {
        "absent": 409, "body": 409, "metadata_digest": 409, "metadata_missing": 409,
        "binding": 409, "get_failure": 503, "body_failure": 503, "metadata_failure": 503,
    }
    assert set(case["refusals"]) == set(expected)
    for name, status in expected.items():
        refusal = case["refusals"][name]
        assert refusal["status"] == status, (name, refusal)
        assert refusal["error"] == ("OBJECT_INTEGRITY_FAILURE" if status == 409
                                    else "OBJECT_UNREADABLE"), name
        assert refusal["bearer"] is False, name
        assert refusal["generation_unchanged"] is True, name
        assert refusal["capability_unchanged"] is True, name
        assert refusal["grant_rows_unchanged"] is True, name
        assert refusal["live_grants_unchanged"] is True, name
        assert refusal["predecessor_revoked_unchanged"] is True, name
        assert refusal["put_count_delta"] == 0, name
        assert refusal["current_bearer_session_status"] == 204, name

    # 4. restore the valid object: recovery succeeds again.
    assert case["restored_recover_status"] == 200
    assert case["restored_recover_result"] == "RECOVERED"
    assert case["restored_session_status"] == 204
    assert case["restored_snapshot_status"] == 200
    assert case["restored_snapshot_digest"] == case["first_snapshot_digest"]

    # 5. DELIVERED: recovery denied, delivered bearer still valid.
    assert case["delivered_state"] == "DELIVERED"
    assert case["delivered_recovery_status"] == 409
    assert case["delivered_recovery_result"] == "NOT_RECOVERABLE"
    assert case["delivered_recovery_reason"] == "ALREADY_DELIVERED"
    assert case["delivered_recovery_bearer"] is False
    assert case["delivered_generation_unchanged"] is True
    assert case["delivered_bearer_session_status"] == 204
    assert case["delivered_bearer_snapshot_status"] == 200

    # One object, one write, for the whole sequence.
    assert case["total_puts"] == 1
    assert case["distinct_objects"] == 1
    print("PASS test_local_end_to_end_publish_recover_corrupt_restore_deliver")


# --- test-double fidelity -----------------------------------------------------


def test_the_r2_double_expresses_every_state_recovery_needs() -> None:
    """The emulator must be able to express the defect, or nothing above holds."""
    double = code("local/memory_bindings.js")
    bucket = double.split("export class MemoryR2")[1].split("export function createAssetsBinding")[0]
    for hook in ("failGetFor", "failBodyReadFor", "failMetadataFor", "healObject", "putLog",
                 # ...and each malformed RESOLVED shape, which is neither a
                 # failure nor an absence and must not collapse into either.
                 "resolveUndefinedFor", "omitEchoedKeyFor", "echoKeyAs",
                 "resolveBareArrayFor", "resolveDecoratedArrayFor"):
        assert hook in bucket, hook
    # The decorated array is a real Array carrying the real octets and the real
    # metadata; it is never normalised into an ordinary object.
    assert "const decorated = [];" in bucket
    assert "decorated.customMetadata = stored.customMetadata;" in bucket
    # Malformed results are produced verbatim, never normalised.
    assert "return undefined;" in bucket
    assert "delete object.key;" in bucket
    getter = bucket.split("async get(key)")[1].split("async delete(key)")[0]
    # A get failure must throw. It must never become `null`.
    assert getter.count("return null") == 1
    assert "if (!this.objects.has(key)) return null;" in getter
    assert getter.count("throw new Error") >= 3
    assert "Object.defineProperty(object" in getter

    # The D1 double must route the recovery statements correctly now that both
    # publication-path grant inserts compare payload_digest.
    d1 = double.split("export class MemoryD1")[1].split("export class MemoryR2")[0]
    assert 'sql.includes("capability_id IS NULL")' in d1, \
        "the D1 double cannot tell an initial grant from a recovery replacement"
    print("PASS test_the_r2_double_expresses_every_state_recovery_needs")


def main() -> None:
    test_a_healthy_object_recovers_normally()
    test_b_absent_object_blocks_recovery()
    test_c_body_corruption_blocks_recovery()
    test_d_metadata_digest_corruption_blocks_recovery()
    test_e_missing_metadata_digest_blocks_recovery()
    test_f_coordinated_rewrite_blocks_recovery()
    test_g_subject_binding_corruption_blocks_recovery()
    test_h_missing_subject_binding_blocks_recovery()
    test_i_r2_get_failure_blocks_recovery()
    test_j_body_read_failure_blocks_recovery()
    test_k_metadata_access_failure_blocks_recovery()
    test_l_a_resolved_undefined_blocks_recovery()
    test_m_a_result_without_a_key_blocks_recovery()
    test_n_a_non_string_key_blocks_recovery()
    test_o_a_wrong_string_key_keeps_its_integrity_verdict()
    test_p_a_bare_array_blocks_recovery()
    test_q_a_decorated_array_blocks_recovery()
    test_recovery_uses_the_shared_inspection_contract()
    test_there_is_one_object_inspection_implementation()
    test_the_recovery_transaction_pins_the_inspected_identity()
    test_recovery_never_stops_consulting_object_inspection()
    test_recovery_requires_a_proven_object_key()
    test_recovery_rejects_a_decorated_array_result()
    test_healthy_recovery_contention_still_yields_one_replacement()
    test_recovery_against_delivery_rollback_retry_and_revocation()
    test_recovery_and_delivery_routes_enforce_the_protocol_contract()
    test_local_end_to_end_publish_recover_corrupt_restore_deliver()
    test_the_r2_double_expresses_every_state_recovery_needs()
    print("Driver Eco Dashboard V1 recovery object-integrity gate passed")


if __name__ == "__main__":
    main()
