#!/usr/bin/env python3
"""R2 retry-integrity and publisher protocol-shape gate.

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_retry_integrity.py

Closes the two findings the independent canonical-byte-integrity re-review
raised.

BLOCKER  Retry could overwrite or advance over an unreadable/incoherent owned
         R2 object. Object inspection returned a boolean `present`, and an
         unreadable object was reported as `{present: false, unreadable: true}`.
         So an R2 `get()` that threw was answered with another `put()` that
         overwrote an object nobody had read; and an object whose body still
         hashed correctly but whose digest metadata or subject binding had been
         corrupted or removed advanced the ledger to GRANT_MINTED, returned
         201, minted one live grant, and left a driver read failing 503.

MEDIUM   Publisher metadata/media-type parsing was broader than documented.
         `application/jsonp` was accepted because the check was
         `startsWith("application/json")`, and a duplicated
         `X-Publication-Subject` was combined by the runtime into `"a, a"` and
         accepted as a new subject.

WHAT THIS SUITE ASSERTS

  * ABSENT / PRESENT_VALID / PRESENT_INVALID / UNREADABLE are four distinct
    object states and an ERROR IS NEVER AN ABSENCE;
  * only a definitive absence, in the pre-write state, permits an R2 write;
  * reuse of an existing object requires ALL of: readable body, body hash ==
    ledger digest, present `payload_digest` metadata equal to the body hash,
    the contracted digest algorithm, present and valid subject binding for this
    subject / this key / this binding version, and the owned key;
  * every failure is measured, not asserted: response status, R2 put count,
    exact stored octets before and after, operation phase before and after,
    grant row count, live grant count and bearer-return count;
  * the publisher accepts exactly `application/json` (optionally
    `; charset=utf-8`) and refuses every JSON-prefix lookalike;
  * every publication control header is a singleton and no combined form is
    accepted;
  * an invalid protocol shape has ZERO side effects.

The canonical bytes come from the real host publisher, not from a fixture file
and not from JavaScript. `ops/tests_manual/eco_retry_integrity_harness.mjs`
runs them through the real Worker over local D1/R2 doubles.

Synthetic data only. No wrangler, no credentials, no remote Cloudflare
resource, no e-mail, no production data. No capability, session id, machine
credential or object key is printed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "ops" / "tests_manual") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "ops" / "tests_manual"))

DELIVERY = REPO_ROOT / "delivery" / "driver_eco_dashboard"
HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_retry_integrity_harness.mjs"

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
    import re

    text = source(relative)
    text = re.sub(r"/\*[\s\S]*?\*/", " ", text)
    text = re.sub(r"(?m)^\s*//.*$", " ", text)
    return text


# --- shared shape of a fail-closed case --------------------------------------


def assert_fails_closed(case: dict, label: str) -> None:
    """Every requirement a fail-closed retry must satisfy, in one place.

    Measured, not assumed: the put count, the exact octets, the operation
    phase, the ledger digest, the grant rows and the bearer are all compared
    across the retry.
    """
    # The window this is measured in must be the real one: the operation owns
    # its key and the object was written, but nothing has been granted.
    assert case["crash_status"] == 503, label
    assert case["phase_after_crash"] in {"CREATED", "SNAPSHOT_WRITTEN"}, \
        f"{label}: the crash did not land in the critical window"

    assert case["retry_bearer"] is False, f"{label}: a bearer was returned"
    assert case["retry_status"] in {409, 503}, f"{label}: {case['retry_status']}"
    assert case["retry_error"] in {"OBJECT_INTEGRITY_FAILURE", "OBJECT_UNREADABLE"}, label
    assert case["retry_next_action"] in {
        "INVESTIGATE_OBJECT_INTEGRITY", "RETRY_AFTER_STORAGE_RECOVERS"}, label
    # ZERO OVERWRITE. This is the assertion the blocker is about.
    assert case["put_count_delta"] == 0, f"{label}: the retry wrote to R2"
    assert case["bytes_unchanged"] is True, f"{label}: the stored octets changed"
    # ZERO ADVANCEMENT.
    assert case["phase_before"] == case["phase_after"], f"{label}: the ledger advanced"
    assert case["ledger_digest_before"] == case["ledger_digest_after"], label
    assert case["grant_rows_after"] == case["grant_rows_before"] == 0, \
        f"{label}: a grant was created"
    assert case["live_grants_after"] == 0, label
    assert case["distinct_objects_after"] <= 1, f"{label}: a second object appeared"
    # The refusal must not describe the private object.
    assert case["retry_body_names_invariant"] is False, \
        f"{label}: the response named the failing invariant"
    # And the refusal must not have poisoned the operation.
    assert case["resumed_status"] == 201, f"{label}: the operation did not converge after repair"
    assert case["resumed_result"] == "PUBLISHED", label
    assert case["resumed_bearer"] is True, label
    assert case["resumed_phase"] == "GRANT_MINTED", label
    assert case["resumed_live_grants"] == 1, label
    assert case["resumed_bytes_are_host_bytes"] is True, label


# --- A. a valid existing object is reused ------------------------------------


def test_a_valid_existing_object_is_reused_without_a_write() -> None:
    case = scenarios()["a_valid_existing_object_is_reused"]
    assert case["crash_status"] == 503
    assert case["phase_after_crash"] == "CREATED"

    assert case["retry_status"] == 201
    assert case["retry_result"] == "PUBLISHED"
    assert case["retry_bearer"] is True
    # ZERO destructive overwrite: the object was proven, not rewritten.
    assert case["put_count_delta"] == 0, "the retry rewrote an object it could have reused"
    assert case["reads_happened"] is True, "the retry did not inspect the object at all"
    assert case["bytes_unchanged"] is True
    assert case["bytes_are_host_bytes"] is True
    assert case["metadata_digest_after"] == _payloads()["digest_a"]
    # One authoritative object, normal one-grant semantics.
    assert case["distinct_objects"] == 1
    assert case["grant_rows"] == 1
    assert case["live_grants"] == 1
    assert case["phase_after"] == "GRANT_MINTED"
    # And the resulting link genuinely works.
    assert case["session_status"] == 204
    assert case["snapshot_status"] == 200
    print("PASS test_a_valid_existing_object_is_reused_without_a_write")


def test_a_valid_object_is_reproven_before_a_grant_is_minted() -> None:
    """A retry arriving at SNAPSHOT_WRITTEN proves the object before granting."""
    case = scenarios()["a2_valid_object_at_snapshot_written"]
    assert case["phase_after_crash"] == "SNAPSHOT_WRITTEN"
    assert case["retry_status"] == 201
    assert case["retry_result"] == "PUBLISHED"
    assert case["put_count_delta"] == 0
    assert case["bytes_unchanged"] is True
    assert case["phase_after"] == "GRANT_MINTED"
    assert case["live_grants"] == 1
    assert case["distinct_objects"] == 1
    print("PASS test_a_valid_object_is_reproven_before_a_grant_is_minted")


# --- B..H. every existing-object integrity failure ---------------------------


def test_b_body_only_corruption_fails_closed() -> None:
    assert_fails_closed(scenarios()["b_body_only_corruption"], "body corruption")
    assert scenarios()["b_body_only_corruption"]["retry_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_b_body_only_corruption_fails_closed")


def test_c_metadata_digest_corruption_fails_closed() -> None:
    case = scenarios()["c_metadata_digest_corruption"]
    assert_fails_closed(case, "metadata digest corruption")
    assert case["retry_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_c_metadata_digest_corruption_fails_closed")


def test_d_missing_metadata_digest_fails_closed() -> None:
    """The confirmed scenario: correct bytes, no digest metadata, 201 before."""
    case = scenarios()["d_metadata_digest_missing"]
    assert_fails_closed(case, "missing metadata digest")
    assert case["retry_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_d_missing_metadata_digest_fails_closed")


def test_e_coordinated_rewrite_fails_against_the_ledger() -> None:
    """Body and metadata rewritten together; only the ledger disagrees."""
    case = scenarios()["e_coordinated_rewrite_against_ledger"]
    assert_fails_closed(case, "coordinated rewrite")
    assert case["retry_error"] == "OBJECT_INTEGRITY_FAILURE"
    # The substituted object was self-consistent, so a self-consistency check
    # alone would have passed it. That is why the ledger is an authority.
    assert case["bytes_after"] != case["ledger_digest_after"]
    print("PASS test_e_coordinated_rewrite_fails_against_the_ledger")


def test_f_subject_binding_corruption_fails_closed() -> None:
    case = scenarios()["f_subject_binding_corrupted"]
    assert_fails_closed(case, "subject binding corruption")
    assert case["retry_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_f_subject_binding_corruption_fails_closed")


def test_g_missing_subject_binding_fails_closed() -> None:
    case = scenarios()["g_subject_binding_missing"]
    assert_fails_closed(case, "missing subject binding")
    assert case["retry_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_g_missing_subject_binding_fails_closed")


def test_h_all_integrity_metadata_missing_fails_closed() -> None:
    case = scenarios()["h_all_integrity_metadata_missing"]
    assert_fails_closed(case, "all integrity metadata missing")
    assert case["retry_error"] == "OBJECT_INTEGRITY_FAILURE"
    print("PASS test_h_all_integrity_metadata_missing_fails_closed")


# --- I..J. an unreadable object -----------------------------------------------


def test_i_r2_get_that_throws_fails_closed() -> None:
    case = scenarios()["i_get_throws"]
    assert_fails_closed(case, "get throws")
    assert case["retry_error"] == "OBJECT_UNREADABLE", \
        "a thrown get was not distinguished from a corrupt object"
    assert case["retry_status"] == 503
    print("PASS test_i_r2_get_that_throws_fails_closed")


def test_j_body_read_that_throws_fails_closed() -> None:
    case = scenarios()["j_body_read_throws"]
    assert_fails_closed(case, "body read throws")
    assert case["retry_error"] == "OBJECT_UNREADABLE"
    print("PASS test_j_body_read_that_throws_fails_closed")


def test_j_metadata_read_that_throws_fails_closed() -> None:
    case = scenarios()["j2_metadata_read_throws"]
    assert_fails_closed(case, "metadata read throws")
    assert case["retry_error"] == "OBJECT_UNREADABLE"
    print("PASS test_j_metadata_read_that_throws_fails_closed")


# --- K. definitive absence ----------------------------------------------------


def test_k_only_definitive_absence_permits_the_write() -> None:
    case = scenarios()["k_definitive_absence_permits_the_write"]
    # A genuine absence in the pre-write state: exactly one put, then the
    # normal atomic state machine.
    assert case["puts_before_first_publish"] == 0
    assert case["first_status"] == 201
    assert case["first_result"] == "PUBLISHED"
    assert case["first_bearer"] is True
    assert case["puts_after"] == 1
    assert case["distinct_objects"] == 1
    assert case["bytes_are_host_bytes"] is True
    assert case["phase_after"] == "GRANT_MINTED"
    assert case["live_grants"] == 1

    # An object that VANISHED after the ledger recorded it is not an absence to
    # write into: re-creating it would be the silent repair this forbids.
    assert case["vanished_phase_before"] == "SNAPSHOT_WRITTEN"
    assert case["vanished_status"] == 409
    assert case["vanished_error"] == "OBJECT_INTEGRITY_FAILURE"
    assert case["vanished_bearer"] is False
    assert case["vanished_put_delta"] == 0, "a vanished object was silently re-created"
    assert case["vanished_phase_after"] == "SNAPSHOT_WRITTEN"
    assert case["vanished_grant_rows"] == 0
    print("PASS test_k_only_definitive_absence_permits_the_write")


# --- L..O. a malformed RESOLVED result ---------------------------------------


def test_l_a_resolved_undefined_fails_closed() -> None:
    """The confirmed blocker: `get()` resolved `undefined` and a write followed.

    Cloudflare documents exactly one missing-key representation for this API,
    and it is `null`. `undefined` is a storage response this code cannot
    account for, and an unaccountable response may not be answered with a put.
    """
    case = scenarios()["l_result_undefined_fails_closed"]
    assert_fails_closed(case, "resolved undefined")
    assert case["retry_error"] == "OBJECT_UNREADABLE", \
        "a malformed result was classified as an object-integrity verdict"
    assert case["retry_status"] == 503
    print("PASS test_l_a_resolved_undefined_fails_closed")


def test_m_a_result_without_a_key_fails_closed() -> None:
    """The second confirmed blocker: valid body and metadata, no echoed key."""
    case = scenarios()["m_result_without_key_fails_closed"]
    assert_fails_closed(case, "result without a key")
    assert case["retry_error"] == "OBJECT_UNREADABLE"
    assert case["retry_status"] == 503
    print("PASS test_m_a_result_without_a_key_fails_closed")


def test_n_a_non_string_key_fails_closed() -> None:
    case = scenarios()["n_result_non_string_key_fails_closed"]
    assert_fails_closed(case, "non-string key")
    assert case["retry_error"] == "OBJECT_UNREADABLE"
    print("PASS test_n_a_non_string_key_fails_closed")


def test_o_a_wrong_string_key_keeps_its_integrity_verdict() -> None:
    """A well-formed key naming another object stays an integrity failure."""
    case = scenarios()["o_result_wrong_string_key_fails_closed"]
    assert_fails_closed(case, "wrong string key")
    assert case["retry_error"] == "OBJECT_INTEGRITY_FAILURE", \
        "the pre-existing wrong-key classification was weakened"
    assert case["retry_status"] == 409
    print("PASS test_o_a_wrong_string_key_keeps_its_integrity_verdict")


# --- P..Q. an ARRAY result ---------------------------------------------------


def test_p_a_bare_array_fails_closed() -> None:
    case = scenarios()["p_bare_array_fails_closed"]
    assert_fails_closed(case, "bare array")
    assert case["retry_error"] == "OBJECT_UNREADABLE"
    assert case["retry_status"] == 503
    print("PASS test_p_a_bare_array_fails_closed")


def test_q_a_decorated_array_fails_closed() -> None:
    """THE confirmed blocker: an array carrying every property a result has."""
    case = scenarios()["q_decorated_array_fails_closed"]
    assert_fails_closed(case, "decorated array")
    assert case["retry_error"] == "OBJECT_UNREADABLE"
    assert case["retry_status"] == 503
    print("PASS test_q_a_decorated_array_fails_closed")


# --- the recurrence detector --------------------------------------------------


def test_unreadable_is_never_treated_as_absent() -> None:
    """THE recurrence detector for the exact reviewed defect.

    The object logically exists, with correct bytes and correct metadata, and
    only the read is broken. Under the previous implementation this reported
    `present: false`, the retry took the write branch and overwrote it. The
    assertion is deliberately blunt: the R2 put count does not move.
    """
    case = scenarios()["recurrence_unreadable_is_not_absent"]

    for phase in ("created", "snapshot_written"):
        state = case[phase]
        assert state["object_logically_exists"] is True, \
            f"{phase}: the fixture must have an object present, or it proves nothing"
        # THE assertion.
        assert state["put_count_delta"] == 0, \
            f"{phase}: an unreadable object was overwritten — UNREADABLE was treated as ABSENT"
        assert state["bytes_unchanged"] is True, phase
        assert state["retry_status"] == 503, phase
        assert state["retry_error"] == "OBJECT_UNREADABLE", phase
        assert state["retry_bearer"] is False, phase
        assert state["grant_rows"] == 0, f"{phase}: a grant was minted over an unreadable object"
        assert state["phase_after"] == state["phase_after_crash"], phase
        # Once storage recovers the operation converges, with no repair and no
        # second object.
        assert state["recovered"]["status"] == 201, phase
        assert state["recovered"]["result"] == "PUBLISHED", phase
        assert state["recovered"]["bearer"] is True, phase
        assert state["recovered"]["puts_total"] == 1, \
            f"{phase}: the object was written more than once in total"
        assert state["recovered"]["bytes_are_host_bytes"] is True, phase
        assert state["recovered"]["live_grants"] == 1, phase

    unit = case["unit"]
    assert unit["absent_state"] == "ABSENT"
    assert unit["absent_writable"] is True
    assert unit["unreadable_state"] == "UNREADABLE"
    assert unit["unreadable_writable"] is False
    assert unit["unreadable_reusable"] is False
    assert unit["states_differ"] is True, "absence and unreadability are the same value again"
    # A missing bucket binding is an inability to determine state, not "write it".
    assert unit["no_bucket_state"] == "UNREADABLE"
    assert unit["no_bucket_writable"] is False
    print("PASS test_unreadable_is_never_treated_as_absent")


def test_a_resolved_undefined_is_never_treated_as_absent() -> None:
    """RECURRENCE DETECTOR: `undefined` must not be an absence.

    The object is present in the bucket with correct bytes and correct
    metadata; only the RESULT is malformed. The confirmed defect classified
    that ABSENT, wrote over the object, granted and returned a raw bearer.

    The mutation proof is what makes this load-bearing: the same scenario is
    run against a copy of `publisher.js` whose ABSENT rule is reverted to
    `object === null || object === undefined`. That copy must WRITE. If it
    does not, this test is passing for some incidental reason.
    """
    case = scenarios()["recurrence_undefined_result_is_not_absent"]

    for phase in ("created", "snapshot_written"):
        state = case["guarded"][phase]
        assert state["object_logically_exists"] is True, \
            f"{phase}: the fixture must have an object present, or it proves nothing"
        # THE assertion.
        assert state["put_count_delta"] == 0, \
            f"{phase}: a malformed result was answered with a write"
        assert state["bytes_unchanged"] is True, phase
        assert state["retry_status"] == 503, phase
        assert state["retry_error"] == "OBJECT_UNREADABLE", phase
        assert state["retry_bearer"] is False, phase
        assert state["grant_rows"] == 0, f"{phase}: a grant was minted"
        assert state["live_grants"] == 0, phase
        assert state["phase_after"] == state["phase_after_crash"], \
            f"{phase}: the publication ledger advanced"
        # Restoring the exact result behaviour converges, with no repair.
        assert state["restored"]["status"] == 201, phase
        assert state["restored"]["result"] == "PUBLISHED", phase
        assert state["restored"]["bearer"] is True, phase
        assert state["restored"]["puts_total"] == 1, \
            f"{phase}: the object was written more than once in total"
        assert state["restored"]["bytes_are_host_bytes"] is True, phase
        assert state["restored"]["live_grants"] == 1, phase

    # THE MUTATION PROOF.
    bypassed = case["bypassed"]
    assert "mutation_error" not in bypassed, bypassed
    assert bypassed["mutant_verdict_state"] == "ABSENT", \
        "the mutant did not reproduce the defect; the detector proves nothing"
    assert bypassed["mutant_verdict_writable"] is True
    assert bypassed["put_count_delta"] == 1, \
        "the mutant did not write, so the guarded assertion is not load-bearing"
    assert bypassed["result_status"] == "PUBLISHED"
    assert bypassed["bearer"] is True
    assert bypassed["grant_rows"] == 1

    unit = case["unit"]
    assert unit["null_state"] == "ABSENT"
    assert unit["null_writable"] is True
    assert unit["undefined_state"] == "UNREADABLE"
    assert unit["undefined_reason"] == "RESULT_UNDEFINED"
    assert unit["undefined_writable"] is False
    assert unit["undefined_reusable"] is False
    assert unit["states_differ"] is True, "`null` and `undefined` are the same verdict again"
    print("PASS test_a_resolved_undefined_is_never_treated_as_absent")


def test_present_valid_requires_a_proven_object_key() -> None:
    """RECURRENCE DETECTOR: absence of a key is not evidence of equality.

    The confirmed defect compared only when a string key happened to be
    present, so a result carrying no key skipped the comparison and reached
    PRESENT_VALID. The mutant restores exactly that conditional and must reach
    PRESENT_VALID where production refuses.
    """
    case = scenarios()["recurrence_key_equality_is_mandatory"]
    guarded, bypassed = case["guarded"], case["bypassed"]

    assert guarded["healthy"]["state"] == "PRESENT_VALID"
    assert guarded["healthy"]["reusable"] is True
    # Missing and non-string keys cannot establish identity at all.
    assert guarded["key_missing"]["state"] == "UNREADABLE", guarded["key_missing"]
    assert guarded["key_missing"]["reason"] == "KEY_MISSING"
    assert guarded["key_non_string"]["state"] == "UNREADABLE", guarded["key_non_string"]
    assert guarded["key_non_string"]["reason"] == "KEY_NOT_STRING"
    for name in ("key_missing", "key_non_string", "key_wrong_string"):
        assert guarded[name]["reusable"] is False, name
        assert guarded[name]["writable"] is False, name
    # A well-formed key naming another object keeps its existing verdict.
    assert guarded["key_wrong_string"]["state"] == "PRESENT_INVALID"
    assert guarded["key_wrong_string"]["reason"] == "KEY_MISMATCH"

    # THE MUTATION PROOF.
    assert "mutation_error" not in bypassed, bypassed
    assert bypassed["key_missing"]["state"] == "PRESENT_VALID", \
        "the mutant did not reproduce the defect; the detector proves nothing"
    assert bypassed["key_missing"]["reusable"] is True
    assert bypassed["key_non_string"]["state"] == "PRESENT_VALID"
    # ...and the mutation is narrow: the wrong-key case is unaffected by it.
    assert bypassed["key_wrong_string"]["state"] == "PRESENT_INVALID"
    assert bypassed["healthy"]["state"] == "PRESENT_VALID"
    print("PASS test_present_valid_requires_a_proven_object_key")


def test_a_decorated_array_is_never_a_valid_object() -> None:
    """RECURRENCE DETECTOR: `typeof [] === "object"` is not a shape check.

    The array carries the operation-owned key, a body reader over the real
    stored octets, the real digest metadata and the real subject binding — so
    every CONTENT check would pass. Only the container is wrong, and the
    confirmed defect let it reach PRESENT_VALID, then PUBLISHED, GRANT_MINTED,
    one grant and one raw bearer.

    The mutation proof removes only the array rejection: that copy must reach
    PRESENT_VALID and mutate.
    """
    case = scenarios()["recurrence_decorated_array_is_not_an_object"]

    # The fixture must genuinely be the masquerading value, or nothing holds.
    carried = case["carried"]
    assert carried["is_array"] is True
    assert carried["typeof_is_object"] is True, \
        "the generic object test no longer accepts an array; the case proves nothing"
    for field in ("key_matches", "has_body_reader", "body_is_the_stored_octets",
                  "metadata_digest_is_correct", "metadata_binding_present"):
        assert carried[field] is True, f"the decorated array lacks {field}"
    assert carried["binding_version"] == "1"

    guarded = case["guarded"]
    assert guarded["healthy"]["state"] == "PRESENT_VALID"
    # THE shape assertions, and the ordering claim: the array is refused for
    # being an array, NOT for failing some later content check.
    for name in ("bare_array", "decorated_array"):
        assert guarded[name]["state"] == "UNREADABLE", f"{name}: {guarded[name]}"
        assert guarded[name]["reason"] == "RESULT_ARRAY", \
            f"{name}: refused for {guarded[name]['reason']}, not for being an array"
        assert guarded[name]["reusable"] is False, name
        assert guarded[name]["writable"] is False, name
    assert guarded["after_restoration"]["state"] == "PRESENT_VALID"

    for phase in ("created", "snapshot_written"):
        routed = case["routed"][phase]
        assert routed["object_logically_exists"] is True, phase
        assert routed["retry_status"] == 503, phase
        assert routed["retry_error"] == "OBJECT_UNREADABLE", phase
        assert routed["retry_bearer"] is False, phase
        # ZERO MUTATION.
        assert routed["put_count_delta"] == 0, f"{phase}: an array result caused a write"
        assert routed["bytes_unchanged"] is True, phase
        assert routed["phase_before"] == routed["phase_after"], \
            f"{phase}: the publication ledger advanced"
        assert routed["ledger_digest_unchanged"] is True, phase
        assert routed["grant_rows"] == 0, f"{phase}: a grant was minted over an array"
        assert routed["live_grants"] == 0, phase
        assert routed["distinct_objects"] == 1, phase
        # Restoration converges, once.
        restored = routed["restored"]
        assert restored["status"] == 201, phase
        assert restored["result"] == "PUBLISHED", phase
        assert restored["bearer"] is True, phase
        assert restored["puts_total"] == 1, phase
        assert restored["distinct_objects"] == 1, phase
        assert restored["bytes_are_host_bytes"] is True, phase
        assert restored["phase"] == "GRANT_MINTED", phase
        assert restored["grant_rows"] == restored["live_grants"] == 1, phase

    # THE MUTATION PROOF.
    bypassed = case["bypassed"]
    assert "mutation_error" not in bypassed, bypassed
    assert bypassed["unit"]["decorated_array"]["state"] == "PRESENT_VALID", \
        "the mutant did not reproduce the defect; the detector proves nothing"
    assert bypassed["unit"]["decorated_array"]["reusable"] is True
    assert bypassed["unit"]["healthy"]["state"] == "PRESENT_VALID"
    # The mutation is narrow: a BARE array still fails, on the key rule.
    assert bypassed["unit"]["bare_array"]["state"] == "UNREADABLE"
    # And what the publication path then does with that verdict.
    assert bypassed["result_status"] == "PUBLISHED"
    assert bypassed["bearer"] is True, \
        "the mutant minted no bearer, so the guarded assertions are not load-bearing"
    assert bypassed["grant_rows"] == 1
    assert bypassed["phase"] == "GRANT_MINTED"
    print("PASS test_a_decorated_array_is_never_a_valid_object")


def test_the_object_state_model_is_explicit_in_the_source() -> None:
    """No boolean-existence contract survives on the publication path."""
    publisher = code("worker/lib/publisher.js")
    publication = code("worker/lib/publication.js")
    worker = code("worker/index.js")

    for state in ("ABSENT", "PRESENT_VALID", "PRESENT_INVALID", "UNREADABLE"):
        assert state in publisher, state
    # The old boolean probe is gone from every module that could act on it.
    assert "snapshotObjectDigest" not in publisher
    assert "snapshotObjectDigest" not in worker
    assert "objectDigest" not in publication, "the boolean probe is still reachable"
    assert "objectDigest" not in worker
    assert "existing.present" not in publication
    # And the write is guarded on ABSENT specifically, not on "not present".
    assert "OBJECT_STATE.ABSENT" in publication
    assert "OBJECT_STATE.PRESENT_VALID" in publication
    assert "OBJECT_STATE.PRESENT_INVALID" in publication
    assert "OBJECT_STATE.UNREADABLE" in publication
    print("PASS test_the_object_state_model_is_explicit_in_the_source")


def test_object_inspection_requires_every_invariant() -> None:
    """Body hash alone is never enough, and every named check is reachable."""
    model = scenarios()["object_state_model"]

    assert model["valid"]["state"] == "PRESENT_VALID"
    assert model["valid"]["reusable"] is True
    assert model["valid"]["writable"] is False

    expected = {
        "wrong_algorithm": ("PRESENT_INVALID", "DIGEST_ALGORITHM_MISMATCH"),
        "wrong_binding_version": ("PRESENT_INVALID", "SUBJECT_BINDING_VERSION"),
        "wrong_subject": ("PRESENT_INVALID", "SUBJECT_BINDING_MISMATCH"),
        "wrong_ledger_digest": ("PRESENT_INVALID", "LEDGER_DIGEST_MISMATCH"),
        "malformed_ledger_digest": ("PRESENT_INVALID", "LEDGER_DIGEST_MALFORMED"),
        "missing_subject_ref": ("PRESENT_INVALID", "SUBJECT_REF_MISSING"),
        # A resolved get with no readable body is uninterpretable, NOT empty
        # and NOT absent — the conditional-response shape R2 documents.
        "body_undefined": ("UNREADABLE", "UNINTERPRETABLE_RESULT"),
        # Every malformed RESOLVED result. `null` is the one absence; nothing
        # else may be read as one, and no result whose key cannot be proven
        # equal to the owned key may be read as an object.
        "result_undefined": ("UNREADABLE", "RESULT_UNDEFINED"),
        "result_missing_return": ("UNREADABLE", "RESULT_UNDEFINED"),
        "result_string": ("UNREADABLE", "RESULT_MALFORMED"),
        "result_number": ("UNREADABLE", "RESULT_MALFORMED"),
        "result_false": ("UNREADABLE", "RESULT_MALFORMED"),
        "result_true": ("UNREADABLE", "RESULT_MALFORMED"),
        "double_result_undefined": ("UNREADABLE", "RESULT_UNDEFINED"),
        "double_key_missing": ("UNREADABLE", "KEY_MISSING"),
        "double_key_undefined_property": ("UNREADABLE", "KEY_NOT_STRING"),
        "double_key_number": ("UNREADABLE", "KEY_NOT_STRING"),
        "double_key_null": ("UNREADABLE", "KEY_NOT_STRING"),
        "double_key_object": ("UNREADABLE", "KEY_NOT_STRING"),
        "double_key_wrong_string": ("PRESENT_INVALID", "KEY_MISMATCH"),
        # `typeof [] === "object"`, so an array must be refused explicitly.
        "double_bare_array": ("UNREADABLE", "RESULT_ARRAY"),
        "double_decorated_array": ("UNREADABLE", "RESULT_ARRAY"),
    }
    # The ONE definitive absence, and the only verdict that permits a write.
    assert model["result_null"]["state"] == "ABSENT"
    assert model["result_null"]["writable"] is True
    # Every malformed shape restored exactly: the object itself is untouched.
    assert model["after_restoration"]["state"] == "PRESENT_VALID"
    assert model["after_restoration"]["reusable"] is True

    for name, (state, reason) in expected.items():
        assert model[name]["state"] == state, f"{name}: {model[name]}"
        assert model[name]["reason"] == reason, f"{name}: {model[name]}"
        assert model[name]["reusable"] is False, name
        assert model[name]["writable"] is False, name
    print("PASS test_object_inspection_requires_every_invariant")


def test_ordinary_retry_contains_no_repair_path() -> None:
    """No delete, no unconditional put, no metadata rewrite on the retry path."""
    publication = code("worker/lib/publication.js")
    publisher = code("worker/lib/publisher.js")

    assert ".delete(" not in publication
    assert "bucket.delete" not in publisher
    # putObject is called exactly once in the module, under the ABSENT branch.
    assert publication.count("services.putObject(") == 1, \
        "the publication path has more than one write site"
    # And the inspection function never writes anything at all.
    inspection = publisher.split("export async function inspectSnapshotObject")[1]
    for forbidden in (".put(", ".delete(", "putSnapshotObject"):
        assert forbidden not in inspection, forbidden
    print("PASS test_ordinary_retry_contains_no_repair_path")


# --- Finding 2: the media-type contract --------------------------------------


def assert_no_side_effects(case: dict, label: str) -> None:
    assert case["operations"] == 0, f"{label}: an operation row was created"
    assert case["objects"] == 0, f"{label}: an R2 object was created"
    assert case["puts"] == 0, f"{label}: R2 was written"
    assert case["grants"] == 0, f"{label}: a grant was created"
    assert case["bearer"] is False, f"{label}: a bearer was returned"


def test_only_the_documented_json_media_type_is_accepted() -> None:
    case = scenarios()["content_type_contract"]

    accepted = ("exact", "charset_utf8", "charset_utf8_no_space",
                "charset_uppercase", "charset_quoted")
    for name in accepted:
        assert case[name]["status"] == 201, f"{name}: {case[name]['status']}"
        assert case[name]["parser"]["ok"] is True, name

    # THE confirmed defect and its whole family.
    rejected = ("jsonp", "json_patch", "json_seq", "jsonfoo", "text_json",
                "text_application_json", "charset_evil", "unknown_parameter",
                "empty", "wildcard")
    for name in rejected:
        assert case[name]["status"] == 400, f"{name}: {case[name]['status']}"
        assert case[name]["error"] == "INVALID_CONTENT_TYPE", name
        assert case[name]["parser"]["ok"] is False, name
        assert_no_side_effects(case[name], name)

    # The specific reasons, so a future loosening is visible.
    assert case["jsonp"]["parser"]["reason"] == "UNSUPPORTED_TYPE"
    assert case["jsonp"]["parser"]["essence"] == "application/jsonp"
    assert case["json_patch"]["parser"]["reason"] == "UNSUPPORTED_TYPE"
    assert case["json_seq"]["parser"]["reason"] == "UNSUPPORTED_TYPE"
    assert case["text_json"]["parser"]["reason"] == "UNSUPPORTED_TYPE"
    assert case["text_application_json"]["parser"]["reason"] == "MALFORMED"
    assert case["charset_evil"]["parser"]["reason"] == "UNSUPPORTED_PARAMETER"
    assert case["unknown_parameter"]["parser"]["reason"] == "UNSUPPORTED_PARAMETER"

    # Duplicated Content-Type: the runtime combines the field lines, and one
    # body cannot declare two media types even when they agree.
    assert "," in case["duplicated"]["combined_value"], \
        "the runtime did not combine the duplicate; the case proves nothing"
    assert case["duplicated"]["status"] == 400
    assert case["duplicated"]["error"] == "INVALID_CONTENT_TYPE"
    assert_no_side_effects(case["duplicated"], "duplicated content-type")
    assert case["duplicated_conflicting"]["status"] == 400
    assert_no_side_effects(case["duplicated_conflicting"], "conflicting content-type")
    print("PASS test_only_the_documented_json_media_type_is_accepted")


def test_media_type_is_parsed_and_not_prefix_matched() -> None:
    worker = code("worker/index.js")
    protocol = code("worker/lib/protocol.js")
    # No prefix test survives anywhere on the request path.
    assert 'startsWith("application/json")' not in worker
    assert "startsWith(\"application/json\")" not in protocol
    # The comparison is an equality against the one documented type.
    assert "PUBLISHER_MEDIA_TYPE" in protocol
    assert "essence !== PUBLISHER_MEDIA_TYPE" in protocol
    # And an unknown parameter is refused rather than ignored.
    assert "UNSUPPORTED_PARAMETER" in protocol
    print("PASS test_media_type_is_parsed_and_not_prefix_matched")


# --- Finding 2: singleton control headers ------------------------------------


def test_publication_control_headers_are_singletons() -> None:
    case = scenarios()["singleton_header_contract"]

    assert case["baseline_valid"]["status"] == 201, \
        "the valid baseline must succeed, or the rejections prove nothing"
    assert case["baseline_valid"]["bearer"] is True

    # The runtime really does combine duplicates; assert that first so the
    # rejections below are known to be exercising the real representation.
    assert case["runtime_combination"]["subject"] is not None
    assert "," in case["runtime_combination"]["subject"]
    assert "," in case["runtime_combination"]["authorization"]

    ambiguous = (
        "operation_duplicate_same", "operation_duplicate_different",
        "operation_comma_joined",
        "subject_duplicate_same", "subject_duplicate_different",
        "subject_comma_joined",
        "digest_duplicate_same", "digest_duplicate_different",
        "digest_comma_joined",
    )
    for name in ambiguous:
        assert case[name]["status"] == 400, f"{name}: {case[name]['status']}"
        assert case[name]["error"] in {"AMBIGUOUS_CONTROL_HEADER", "INVALID_CONTROL_HEADER"}, \
            f"{name}: {case[name]['error']}"
        assert_no_side_effects(case[name], name)

    # A duplicated header is refused AS AMBIGUOUS, not incidentally by a
    # downstream validator that happened to be narrow.
    for name in ("operation_duplicate_same", "subject_duplicate_same", "digest_duplicate_same"):
        assert case[name]["error"] == "AMBIGUOUS_CONTROL_HEADER", name

    for name in ("operation_missing", "subject_missing", "digest_missing"):
        assert case[name]["status"] == 400, name
        assert_no_side_effects(case[name], name)
    print("PASS test_publication_control_headers_are_singletons")


def test_publisher_authorization_still_fails_closed() -> None:
    """Existing auth guarantees preserved; combined credentials refused."""
    case = scenarios()["singleton_header_contract"]
    for name in ("authorization_duplicate_same", "authorization_duplicate_different",
                 "authorization_comma_joined", "authorization_missing",
                 "authorization_wrong_scheme"):
        # Unchanged: the transport answers 404 to every unauthenticated caller
        # and never says whether a credential was presented or merely wrong.
        assert case[name]["status"] == 404, f"{name}: {case[name]['status']}"
        assert_no_side_effects(case[name], name)
    print("PASS test_publisher_authorization_still_fails_closed")


def test_recover_and_delivery_routes_share_the_contract() -> None:
    case = scenarios()["other_routes_reject_ambiguous_headers"]
    assert case["recover_duplicate_status"] == 400
    assert case["recover_duplicate_bearer"] is False, \
        "an ambiguous recover request minted a replacement bearer"
    assert case["delivery_duplicate_operation_status"] == 400
    assert case["delivery_duplicate_capability_status"] == 400
    assert case["delivery_duplicate_phase_status"] == 400
    # Zero side effects on the delivery ledger.
    assert case["phase_before"] == case["phase_after"] == "GRANT_MINTED"
    assert case["grant_rows_unchanged"] is True
    assert case["bearer_generation_unchanged"] is True
    # And singleton headers still work.
    assert case["singleton_intent_status"] == 200
    assert case["singleton_intent_result"] == "RECORDED"
    print("PASS test_recover_and_delivery_routes_share_the_contract")


def test_request_shape_is_validated_before_any_mutation() -> None:
    """Static proof of ordering: no store is reached before the shape gate."""
    worker = code("worker/index.js")
    body = worker.split("async function handlePublish(")[1].split("async function handlePublishRecover")[0]
    for marker in ("readPublicationHeaders(", "readPublisherContentType("):
        assert marker in body, marker
    # Authentication, then shape, then anything that can mutate state.
    auth_at = body.index("authorisePublisher(")
    shape_at = body.index("readPublicationHeaders(")
    media_at = body.index("readPublisherContentType(")
    publish_at = body.index("publishSnapshot(")
    assert auth_at < shape_at < media_at < publish_at, \
        "protocol validation does not precede publication"
    # And nothing that touches a store appears before the shape gate.
    assert "publicationServices(" not in body[:shape_at]
    print("PASS test_request_shape_is_validated_before_any_mutation")


# --- regression: contention with the new gate in place -----------------------


def test_contention_invariants_survive_the_new_gate() -> None:
    case = scenarios()["contention_after_the_fix"]
    for callers in (2, 8, 32, 128):
        state = case[f"callers_{callers}"]
        label = f"{callers} callers"
        assert state["operations"] == 1, label
        assert state["distinct_objects"] == 1, label
        assert state["distinct_put_keys"] == 1, label
        assert state["grant_rows"] == 1, label
        assert state["live_grants"] == 1, label
        assert state["bearers_returned"] == 1, label
        assert state["created_statuses"] == 1, label
        # The fail-closed gate must not fire on a healthy concurrent publish.
        assert state["integrity_failures"] == 0, \
            f"{label}: the new gate refused a legitimate concurrent caller"
        assert state["state"] == "GRANT_MINTED", label
        assert state["bytes_are_host_bytes"] is True, label
        assert state["ledger_digest"] == _payloads()["digest_a"], label
    print("PASS test_contention_invariants_survive_the_new_gate")


# --- test-double fidelity -----------------------------------------------------


def test_the_r2_double_does_not_normalise_failure_into_absence() -> None:
    """The emulator must be able to express the defect, or nothing above holds."""
    double = code("local/memory_bindings.js")
    bucket = double.split("export class MemoryR2")[1].split("export function createAssetsBinding")[0]
    # Each failure mode has its own injection point, so none of them can only
    # be expressed as an absence.
    for hook in ("failGetFor", "failBodyReadFor", "failMetadataFor", "healObject",
                 # ...and each malformed RESOLVED shape, which is a different
                 # thing from a failure and must not collapse into one.
                 "resolveUndefinedFor", "omitEchoedKeyFor", "echoKeyAs",
                 "resolveBareArrayFor", "resolveDecoratedArrayFor"):
        assert hook in bucket, hook
    # The decorated array is a real Array carrying the real stored octets and
    # the real metadata; it is never normalised into an ordinary object.
    assert "const decorated = [];" in bucket
    assert "decorated.customMetadata = stored.customMetadata;" in bucket
    # A malformed result is produced verbatim, never normalised: `undefined` is
    # returned as `undefined` and a key omission is a delete, not `key:
    # undefined`, because "absent" and "present and empty" are distinct shapes.
    assert "return undefined;" in bucket
    assert "delete object.key;" in bucket

    getter = bucket.split("async get(key)")[1].split("async delete(key)")[0]
    # THE fidelity requirement: inside get(), the only null is the definitive
    # absence, and every failure throws.
    assert getter.count("return null") == 1, \
        "MemoryR2.get has a null return other than the definitive absence"
    assert "if (!this.objects.has(key)) return null;" in getter
    assert getter.count("throw new Error") >= 3, \
        "MemoryR2.get does not throw for get failures"
    # Metadata failure is an accessor, which a plain data property cannot model.
    assert "Object.defineProperty(object" in getter
    print("PASS test_the_r2_double_does_not_normalise_failure_into_absence")


def main() -> None:
    test_a_valid_existing_object_is_reused_without_a_write()
    test_a_valid_object_is_reproven_before_a_grant_is_minted()
    test_b_body_only_corruption_fails_closed()
    test_c_metadata_digest_corruption_fails_closed()
    test_d_missing_metadata_digest_fails_closed()
    test_e_coordinated_rewrite_fails_against_the_ledger()
    test_f_subject_binding_corruption_fails_closed()
    test_g_missing_subject_binding_fails_closed()
    test_h_all_integrity_metadata_missing_fails_closed()
    test_i_r2_get_that_throws_fails_closed()
    test_j_body_read_that_throws_fails_closed()
    test_j_metadata_read_that_throws_fails_closed()
    test_k_only_definitive_absence_permits_the_write()
    test_l_a_resolved_undefined_fails_closed()
    test_m_a_result_without_a_key_fails_closed()
    test_n_a_non_string_key_fails_closed()
    test_o_a_wrong_string_key_keeps_its_integrity_verdict()
    test_p_a_bare_array_fails_closed()
    test_q_a_decorated_array_fails_closed()
    test_unreadable_is_never_treated_as_absent()
    test_a_resolved_undefined_is_never_treated_as_absent()
    test_present_valid_requires_a_proven_object_key()
    test_a_decorated_array_is_never_a_valid_object()
    test_the_object_state_model_is_explicit_in_the_source()
    test_object_inspection_requires_every_invariant()
    test_ordinary_retry_contains_no_repair_path()
    test_only_the_documented_json_media_type_is_accepted()
    test_media_type_is_parsed_and_not_prefix_matched()
    test_publication_control_headers_are_singletons()
    test_publisher_authorization_still_fails_closed()
    test_recover_and_delivery_routes_share_the_contract()
    test_request_shape_is_validated_before_any_mutation()
    test_contention_invariants_survive_the_new_gate()
    test_the_r2_double_does_not_normalise_failure_into_absence()
    print("Driver Eco Dashboard V1 R2 retry-integrity gate passed")


if __name__ == "__main__":
    main()
