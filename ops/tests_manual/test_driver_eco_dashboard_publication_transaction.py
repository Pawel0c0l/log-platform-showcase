#!/usr/bin/env python3
"""Publication transaction gate for the Driver Eco Dashboard delivery boundary.

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_publication_transaction.py

Closes the blocking items the independent re-review raised before publisher
development may begin:

  BLOCKER 1  concurrent publication fanned out live links — N callers, N
             bearers, N objects, N grants for one logical operation;
  BLOCKER 2  publication and recovery crash transitions were not atomic, so a
             live grant could exist that the operation ledger did not
             reference, and a recovery could invalidate a DELIVERED bearer.

Every race here is FORCED, not hoped for: the local D1/R2 doubles park callers
at named transition sites and this suite releases them in a chosen order. The
Worker and its libraries contain no test hook, so the code under test is
byte-identical to the code that would deploy.

Drives `ops/tests_manual/eco_publication_transaction_harness.mjs`. No wrangler,
no credentials, no remote Cloudflare resource, no e-mail, no production data.

No capability, session id, machine credential or object key is printed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DELIVERY = REPO_ROOT / "delivery" / "driver_eco_dashboard"
HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_publication_transaction_harness.mjs"

_CACHE: dict = {}


def scenarios() -> dict:
    if not _CACHE:
        result = subprocess.run(
            ["node", str(HARNESS)], capture_output=True, text=True,
            cwd=str(REPO_ROOT), timeout=900,
        )
        if result.returncode != 0:
            raise AssertionError(f"harness failed: {result.stderr.strip()[:600]}")
        _CACHE.update(json.loads(result.stdout))
        broken = {name: value.get("harness_error") for name, value in _CACHE.items()
                  if isinstance(value, dict) and value.get("harness_error")}
        if broken:
            raise AssertionError(f"harness scenario errors: {broken}")
    return _CACHE


def source(relative: str) -> str:
    return (DELIVERY / relative).read_text(encoding="utf-8")


def assert_one_authoritative_grant(state: dict, where: str) -> None:
    """THE invariant, asserted identically everywhere it must hold.

    `authoritative_live_grants` counts grants that are simultaneously live and
    referenced by an operation ledger. `orphan_live_grants` counts live grants
    no ledger references — the state that made a bearer unrecoverable.
    """
    assert state["authoritative_live_grants"] <= 1, f"{where}: {state}"
    assert state["orphan_live_grants"] == 0, f"{where}: orphaned live grant {state}"
    assert state["ledger_references_existing_grant"], f"{where}: ledger names a missing grant"
    assert state["owned_object_keys"] <= 1, f"{where}: more than one owned object key"
    assert state["distinct_objects_written"] <= 1, f"{where}: more than one object written"
    assert state["r2_objects"] <= 1, f"{where}: more than one R2 object"
    assert state["operations"] >= 1, where


# --- PART A/B/F: one authoritative operation under contention ----------------


def _assert_contention(case: dict, expected_callers: int) -> None:
    where = f"contention x{expected_callers}"
    assert case["callers"] == expected_callers
    # Exactly one caller may win the transition to the grant-authoritative
    # state, and only that winner receives a raw capability.
    assert case["status_201"] == 1, f"{where}: {case['status_201']} callers published"
    assert case["raw_bearer_emissions"] == 1, f"{where}: {case['raw_bearer_emissions']} bearers"
    assert case["distinct_raw_bearers"] == 1, where
    assert case["published_result"] == "PUBLISHED", where
    # Everyone else gets an idempotent, conflict-safe answer.
    assert case["status_200"] == expected_callers - 1, where
    assert case["other_status"] == 0, where
    assert case["idempotent_responses"] == expected_callers - 1, where
    assert case["operation_state"] == "GRANT_MINTED", where
    assert case["bearer_generation"] == 1, where
    assert case["total_grants"] == 1, f"{where}: {case['total_grants']} grants exist"
    assert_one_authoritative_grant(case, where)
    # No response may leave the host guessing what to do next.
    assert set(case["next_actions"]) == {"PERSIST_BEARER", "USE_PERSISTED_BEARER_OR_RECOVER"}, where


def test_concurrent_publication_converges_at_every_scale() -> None:
    for count in (2, 8, 32, 128):
        _assert_contention(scenarios()[f"contention_{count}"], count)
    print("PASS test_concurrent_publication_converges_at_every_scale (2/8/32/128)")


def test_contention_holds_under_repetition() -> None:
    """A race that only sometimes loses is still a defect."""
    case = scenarios()["contention_repeated"]
    assert case["rounds"] == 25
    assert case["max_status_201"] == 1 and case["min_status_201"] == 1, case
    assert case["max_live_grants"] == 1, case
    assert case["max_distinct_objects"] == 1, case
    assert case["max_bearers"] == 1, case
    assert case["any_orphan"] is False, case
    print(f"PASS test_contention_holds_under_repetition ({case['rounds']} rounds x4 callers)")


# --- PART I: forced interleavings at every security-critical boundary --------


def test_forced_races_at_every_transition_boundary() -> None:
    """The former fan-out, reproduced deterministically and proved impossible.

    Each case parks eight callers at exactly one transition site and releases
    them together, so the interleaving under test is executed rather than
    hoped for.
    """
    boundaries = {
        "forced_race_at_operation_claim": "operation_claim",
        "forced_race_at_object_write": "object_put",
        "forced_race_at_snapshot_written": "snapshot_written",
        "forced_race_before_grant_transaction": "grant_transaction",
        "forced_race_after_grant_transaction": "grant_transaction",
    }
    for name, site in boundaries.items():
        case = scenarios()[name]
        assert case["site"] == site, name
        assert case["parked_together"] == 8, f"{name}: race was not actually forced"
        assert case["status_201"] == 1, f"{name}: {case['status_201']} publishers won"
        assert case["raw_bearer_emissions"] == 1, name
        assert case["total_grants"] == 1, name
        assert_one_authoritative_grant(case, name)
    print(f"PASS test_forced_races_at_every_transition_boundary ({len(boundaries)} boundaries x8)")


def test_serialised_release_cannot_mint_a_second_bearer() -> None:
    """32 callers released one at a time, each running against a moved state."""
    case = scenarios()["forced_serialised_release_at_grant"]
    assert case["released_one_at_a_time"] == 32, case
    assert case["status_201"] == 1, case
    assert case["raw_bearer_emissions"] == 1, case
    assert case["total_grants"] == 1, case
    assert_one_authoritative_grant(case, "serialised release")
    print("PASS test_serialised_release_cannot_mint_a_second_bearer")


# --- PART J: same-operation conflicts ---------------------------------------


def test_same_operation_with_different_content_fails_safely() -> None:
    case = scenarios()["same_operation_conflicts"]
    assert case["first_status"] == 201
    for field in ("different_subject_status", "different_payload_status"):
        assert case[field] == 409, case
    assert case["different_subject_error"] == "OPERATION_CONFLICT"
    assert case["different_payload_error"] == "OPERATION_CONFLICT"
    # A conflict tells the host to open a new operation, never to reissue.
    assert case["different_subject_next_action"] == "OPEN_NEW_OPERATION", case
    assert case["nothing_mutated"] is True, "a conflict mutated state"
    assert case["conflict_emitted_bearer"] is False, "a conflict emitted a bearer"
    assert case["fresh_conflict_status"] == 409
    assert_one_authoritative_grant(case["fresh_world"], "fresh conflict")
    print("PASS test_same_operation_with_different_content_fails_safely")


# --- PART H: host crash matrix ----------------------------------------------


def test_every_crash_window_converges_safely() -> None:
    """Stages 1-5 of the crash matrix, each injected and then retried.

    The requirement is not that a failure is impossible but that the state it
    leaves has exactly one safe next action, and that taking it converges.
    """
    windows = {case["window"]: case for case in scenarios()["crash_windows"]}
    assert set(windows) == {
        "before_r2_write", "after_r2_before_grant", "at_snapshot_written",
        "inside_grant_transaction", "before_any_request",
    }, sorted(windows)

    for name, case in windows.items():
        # Nothing may survive a failed window that the ledger does not own.
        assert case["orphan_during"] == 0, f"{name}: orphaned grant mid-failure"
        if name != "before_any_request":
            assert case["failure_status"] == 503, f"{name}: {case['failure_status']}"
            # A grant is never created by a window that failed before or
            # inside the grant transaction.
            assert case["grants_during"] == 0, f"{name}: {case['grants_during']} grants"
        # The retry converges and mints exactly one bearer, once.
        assert case["retry_status"] == 201, f"{name}: retry {case['retry_status']}"
        assert case["retry_result"] == "PUBLISHED", name
        assert case["retry_bearer"] is True, name
        assert case["second_retry_result"] == "ALREADY_PUBLISHED", name
        assert case["second_retry_bearer"] is False, f"{name}: second retry minted a bearer"
        assert case["final"]["total_grants"] == 1, name
        assert_one_authoritative_grant(case["final"], name)

    # The transaction really did roll back: the first statement of the batch
    # ran and then the batch aborted, and no capability survived.
    assert windows["inside_grant_transaction"]["state_during"] == "SNAPSHOT_WRITTEN"
    assert windows["inside_grant_transaction"]["grants_during"] == 0
    print(f"PASS test_every_crash_window_converges_safely ({len(windows)} windows)")


def test_response_loss_after_commit_is_unambiguous() -> None:
    """Crash matrix stages 5 and 6 — the only window with an unheld live bearer.

    The state is deliberately NOT ambiguous: the operation is
    grant-authoritative, the ledger names the grant, a plain retry mints
    nothing, and the response says explicitly that recovery is the way back.
    """
    case = scenarios()["response_loss_after_commit"]
    assert case["state_at_commit"] == "GRANT_MINTED", case
    assert case["grants_at_commit"] == 1, case
    assert case["ledger_referenced_at_commit"] is True, case
    assert case["orphan_at_commit"] == 0, "the committed grant was not in the ledger"
    assert case["retry_status"] == 200
    assert case["retry_result"] == "ALREADY_PUBLISHED"
    assert case["retry_next_action"] == "USE_PERSISTED_BEARER_OR_RECOVER"
    assert case["retry_bearer"] is False, "a retry replayed a bearer"
    assert case["retry_bearer_recoverable"] is True
    assert_one_authoritative_grant(case["final"], "response loss")
    print("PASS test_response_loss_after_commit_is_unambiguous")


# --- PART D/K: raw-bearer loss recovery -------------------------------------


def test_recovery_replaces_exactly_one_grant() -> None:
    case = scenarios()["recovery_normal_and_retry"]
    assert case["recovered_status"] == 200
    assert case["recovered_result"] == "RECOVERED"
    assert case["recovered_bearer"] is True
    assert case["recovered_is_different"] is True, "recovery replayed the old bearer"
    assert case["superseded_predecessor"] is True
    assert case["generation_after_first"] == 2, case
    # The old bearer is dead the moment the replacement exists; there is never
    # an instant with two usable links.
    assert case["old_bearer_exchange"] == 401, case
    assert case["new_bearer_exchange"] == 204, case
    assert_one_authoritative_grant(case["after_first"], "after first recovery")
    # A further explicit recovery is still exactly one replacement.
    assert case["second_recovery_result"] == "RECOVERED"
    assert case["generation_after_second"] == 3
    assert_one_authoritative_grant(case["after_second"], "after second recovery")
    print("PASS test_recovery_replaces_exactly_one_grant")


def test_concurrent_recovery_leaves_one_replacement() -> None:
    for count in (2, 8, 32):
        case = scenarios()[f"recovery_concurrent_{count}"]
        where = f"{count} concurrent recoveries"
        assert case["callers"] == count
        assert case["recovered"] == 1, f"{where}: {case['recovered']} replacements minted"
        assert case["distinct_replacement_bearers"] == 1, where
        assert case["refused"] == count - 1, where
        assert case["refusal_reasons"] == ["SUPERSEDED"], where
        assert case["bearer_generation"] == 2, where
        assert_one_authoritative_grant(case, where)

    forced = scenarios()["recovery_forced_race"]
    assert forced["parked_together"] == 16, "the recovery race was not actually forced"
    assert forced["recovered"] == 1, forced
    assert forced["refused"] == 15, forced
    assert forced["bearer_generation"] == 2, forced
    assert_one_authoritative_grant(forced, "forced recovery race")
    print("PASS test_concurrent_recovery_leaves_one_replacement (2/8/32 + 16 forced)")


def test_recovery_edge_cases_never_leave_two_live_grants() -> None:
    cases = scenarios()["recovery_edge_cases"]

    assert cases["unknown_operation"] == 404

    # No grant yet: refused, and the host is told to retry publish rather than
    # to issue a link some other way.
    no_grant = cases["no_grant_yet"]
    assert no_grant["status"] == 409 and no_grant["reason"] == "NO_GRANT_YET", no_grant
    assert no_grant["next_action"] == "RETRY_PUBLISH", no_grant
    assert no_grant["grants"] == 0, no_grant

    # Racing an independent revoke: nothing is written, and the ledger is not
    # left pointing at a grant that was never created.
    revoke = cases["racing_revoke"]
    assert revoke["status"] == 409 and revoke["reason"] == "GRANT_NOT_ELIGIBLE", revoke
    assert revoke["invariants"]["total_grants"] == 1, revoke
    assert_one_authoritative_grant(revoke["invariants"], "recovery racing revoke")

    # Transaction failure, then a clean retry.
    failure = cases["transaction_failure"]
    assert failure["failed_status"] == 503
    assert failure["grants_during"] == 1, "a failed recovery created a grant"
    assert failure["orphan_during"] == 0
    assert failure["retry_status"] == 200 and failure["retry_generation"] == 2
    assert_one_authoritative_grant(failure["invariants"], "recovery transaction failure")

    # Rollback INSIDE the recovery transaction: neither the replacement nor
    # the ledger move survives.
    rollback = cases["rollback_inside_transaction"]
    assert rollback["status"] == 503
    assert rollback["grants_after_rollback"] == 1, "rollback left a replacement grant"
    assert rollback["generation_after_rollback"] == 1, "rollback advanced the generation"
    assert rollback["orphan_after_rollback"] == 0
    assert rollback["retry_status"] == 200
    assert_one_authoritative_grant(rollback["invariants"], "recovery rollback")

    # Response loss after a successful recovery: the replacement is
    # unrecoverable by design, so a further retry rotates again — and still
    # leaves exactly one live grant.
    loss = cases["response_loss_after_recovery"]
    assert loss["at_commit_live"] == 1 and loss["at_commit_orphan"] == 0, loss
    assert loss["at_commit_generation"] == 2, loss
    assert loss["retry_status"] == 200 and loss["retry_generation"] == 3, loss
    assert_one_authoritative_grant(loss["invariants"], "recovery response loss")

    # Recovery concurrent with a plain publish retry.
    mixed = cases["recovery_during_publish_retry"]
    assert mixed["publish_bearer"] is False, "a publish retry minted a bearer during recovery"
    assert_one_authoritative_grant(mixed["invariants"], "recovery during publish retry")
    print("PASS test_recovery_edge_cases_never_leave_two_live_grants (7 cases)")


# --- PART E: delivery vs recovery terminal-state race -----------------------


def test_delivered_capability_is_never_invalidated() -> None:
    case = scenarios()["delivery_then_recovery"]
    assert case["intent_result"] == "RECORDED"
    assert case["delivered_result"] == "RECORDED"
    assert case["delivered_state"] == "DELIVERED"
    assert case["recovery_status"] == 409
    assert case["recovery_reason"] == "ALREADY_DELIVERED"
    # The link in the driver's mailbox still works.
    assert case["delivered_bearer_still_valid"] == 204, case
    assert case["bearer_generation"] == 1, "delivery changed the bearer generation"
    # A republish of a delivered operation is terminal, not an invitation.
    assert case["republish_result"] == "ALREADY_DELIVERED"
    assert case["republish_next_action"] == "NONE"
    assert case["republish_bearer"] is False
    assert_one_authoritative_grant(case, "delivered operation")
    print("PASS test_delivered_capability_is_never_invalidated")


def test_recovery_decided_before_delivery_cannot_commit_after_it() -> None:
    """The exact forced interleaving the review demanded.

    The recovery is parked at its own transaction boundary while the operation
    is still recoverable; DELIVERED is then driven to completion; only then is
    the recovery released. Because the DELIVERED refusal is a predicate INSIDE
    the transaction rather than a SELECT before it, the recovery commits
    nothing.
    """
    case = scenarios()["forced_recovery_commits_after_delivery"]
    assert case["state_when_recovery_decided"] == "DELIVERY_INTENT_RECORDED", case
    assert case["delivered_result"] == "RECORDED", case
    assert case["recovery_status"] == 409, case
    assert case["recovery_reason"] == "ALREADY_DELIVERED", case
    assert case["recovery_emitted_bearer"] is False, "a stale recovery minted a bearer"
    assert case["delivered_bearer_still_valid"] == 204, "the delivered bearer was invalidated"
    assert case["bearer_generation"] == 1, "a stale recovery advanced the generation"
    assert case["operation_state"] == "DELIVERED"
    assert_one_authoritative_grant(case, "recovery after delivery")
    print("PASS test_recovery_decided_before_delivery_cannot_commit_after_it")


def test_delivery_on_a_superseded_bearer_is_refused() -> None:
    """The mirror race: recovery wins, then the stale delivery arrives.

    Terminalising there would mark DELIVERED a grant the driver never
    received, and would then protect the wrong capability from recovery.
    """
    case = scenarios()["forced_delivery_on_superseded_bearer"]
    assert case["recovery_status"] == 200, case
    assert case["stale_delivery_status"] == 409, case
    assert case["stale_delivery_result"] == "CAPABILITY_SUPERSEDED", case
    assert case["state_after_stale_delivery"] == "DELIVERY_INTENT_RECORDED", case
    # Told which bearer is current, the host delivers that one and succeeds.
    assert case["corrected_delivery_result"] == "RECORDED", case
    assert case["corrected_state"] == "DELIVERED", case
    assert_one_authoritative_grant(case, "superseded delivery")
    print("PASS test_delivery_on_a_superseded_bearer_is_refused")


# --- PART B/C/S: structural guarantees --------------------------------------


def test_grant_cannot_exist_without_the_ledger_referencing_it() -> None:
    case = scenarios()["grant_ledger_atomicity"]
    # The reviewed state is now unrepresentable, not merely avoided.
    assert case["ledger_without_grant"] == "refused_by_check", case
    assert case["duplicate_capability_reference"] == "refused_by_unique_index", case
    # Both authoritative transitions are one D1 batch, i.e. one transaction.
    assert case["grant_transaction_uses_batch"] is True
    assert case["recovery_transaction_uses_batch"] is True
    # No unconditional grant-insert helper survives in production code.
    assert case["store_has_no_insert_capability"] is True
    assert case["publisher_has_no_issue_capability"] is True
    assert case["worker_never_imports_dev_grants"] is True
    # Recovery is a publication-specific transaction, NOT generic rotate
    # followed by a separate ledger update.
    assert case["recovery_avoids_generic_rotation"] is True

    publication = source("worker/lib/publication.js")
    assert "recordRecoveredGrant" not in publication, "the non-atomic helper survives"
    assert "beginPublication" not in publication, "the pre-transaction entry point survives"
    schema = source("schema/001_authorization.sql")
    assert "chk_eco_publication_grant_ledger" in schema
    assert "uq_eco_publication_capability" in schema
    assert "uq_eco_publication_object_key" in schema
    print("PASS test_grant_cannot_exist_without_the_ledger_referencing_it")


def test_every_publisher_route_uses_the_authoritative_contract() -> None:
    case = scenarios()["publisher_routes_use_the_contract"]
    # The COMPLETE publisher surface, asserted as an exact list so a new route
    # cannot appear without this contract being reconsidered.
    # `/api/publish/maintenance` retires dead authorization state (expired
    # sessions) and is in the same authenticated boundary as the other three:
    # it is `404` to every unauthenticated caller below, it inserts no grant
    # and it writes no object.
    assert case["publisher_routes"] == ["/api/publish", "/api/publish/recover",
                                        "/api/publish/delivery",
                                        "/api/publish/maintenance"], case
    # Unauthenticated callers cannot even learn the routes exist.
    assert set(case["anonymous_status"].values()) == {404}, case
    assert case["all_handlers_authorise"] is True
    assert case["publish_uses_transaction"] is True
    assert case["recover_uses_transaction"] is True
    assert case["delivery_uses_transaction"] is True
    assert case["delivery_requires_capability"] is True, "delivery does not name its bearer"
    assert case["no_handler_inserts_a_grant"] is True
    assert case["no_handler_puts_an_object"] is True
    print(f"PASS test_every_publisher_route_uses_the_authoritative_contract "
          f"({len(case['publisher_routes'])} routes)")


def test_one_operation_owns_exactly_one_object() -> None:
    case = scenarios()["object_ownership"]
    assert case["publish_status"] == 201
    assert case["key_is_opaque"] is True
    # Three publishes of the same operation write one key, not three.
    assert case["keys_written_after_three_publishes"] == 1, case
    assert case["r2_objects"] == 1, case
    assert case["response_leaks_object_key"] is False
    # A write that failed before any object existed orphans nothing: the key
    # is claimed in the same statement that creates the operation row.
    assert case["orphan_state"]["objects"] == 0, case
    assert case["orphan_state"]["owned_key_present"] is True, case
    assert case["retry_reused_owned_key"] is True, "a retry minted a second key"
    assert_one_authoritative_grant(case["after_retry"], "object ownership retry")
    print("PASS test_one_operation_owns_exactly_one_object")


def test_barriers_exist_only_in_local_test_infrastructure() -> None:
    """No production debug endpoint, and no test hook in shipped code."""
    for relative in ("worker/index.js", "worker/lib/publication.js",
                     "worker/lib/store.js", "worker/lib/publisher.js"):
        text = source(relative)
        for forbidden in ("Barrier", "onSite", "failAt", "barrier(", "__test"):
            assert forbidden not in text, f"{relative} contains the test hook {forbidden!r}"
    bindings = source("local/memory_bindings.js")
    assert "export class Barrier" in bindings
    assert "export const SITE" in bindings
    assert "failInBatchAfter" in bindings
    print("PASS test_barriers_exist_only_in_local_test_infrastructure")


# --- local end-to-end -------------------------------------------------------

E2E_HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_publication_e2e_harness.mjs"


def test_local_end_to_end_publish_recover_deliver() -> None:
    """The whole chain, starting from real canonical bytes, synthetic data only.

    canonical builder -> canonical bytes -> authenticated publisher route ->
    one operation-owned object -> atomic grant -> one raw bearer -> exchange ->
    session -> parameterless snapshot -> binding + schema -> frontend-safe JSON;
    then lost-response recovery, then DELIVERED with recovery denied.
    """
    sys.path.insert(0, str(REPO_ROOT / "ops" / "tests_manual"))
    import eco_dashboard_fixtures as fx
    from jobs.ecodriving_dashboard.publication import (
        PrivacyContext, build_publishable_snapshot, serialize_publishable_snapshot,
    )
    from datetime import date

    days = fx.daily_inputs(date(2026, 7, 1), fx.WEEKLY_DAY_KM, fx.ACCEPTABLE_TOTALS)
    current = fx.period_from_days(fx.CURRENT_WEEKLY, days, ranking=fx.RANKED_FACTS)
    previous_days = fx.daily_inputs(date(2026, 7, 1), fx.PREVIOUS_DAY_KM, fx.PREVIOUS_TOTALS)
    previous = fx.period_from_days(fx.PREVIOUS_WEEKLY, previous_days,
                                   ranking=fx.PREVIOUS_RANKED_FACTS)
    publishable = build_publishable_snapshot(
        privacy=PrivacyContext(identity_key=fx.SYNTHETIC_IDENTITY_KEY,
                               client_code=fx.SYNTHETIC_CLIENT_CODE,
                               person_names=("Jan Kowalski",)),
        generated_at_utc=fx.GENERATED_AT,
        period_type="weekly", current=current, previous=previous,
        days=days, series=fx.FULL_SERIES,
    )
    body = serialize_publishable_snapshot(publishable)

    result = subprocess.run(
        ["node", str(E2E_HARNESS)],
        input=json.dumps({"canonical_base64": base64.b64encode(body).decode("ascii"),
                          "payload_digest": publishable.payload_digest}),
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
    )
    assert result.returncode == 0, result.stderr[-800:]
    case = json.loads(result.stdout)

    # The digest the host declares is the digest of the exact bytes it sends.
    assert case["host_digest_matches_body"] is True
    host_digest = hashlib.sha256(body).hexdigest()
    assert host_digest == publishable.payload_digest

    publish = case["publish"]
    assert publish["status"] == 201 and publish["result"] == "PUBLISHED"
    assert publish["next_action"] == "PERSIST_BEARER"
    assert publish["state"] == "GRANT_MINTED"
    assert publish["bearer_returned"] is True
    assert publish["response_omits_object_key"] is True
    assert publish["response_omits_subject"] is True

    obj = case["object"]
    assert obj["distinct_keys_written"] == 1 and obj["r2_objects"] == 1
    assert obj["key_is_opaque"] is True
    assert obj["binding_matches"] is True and obj["binding_version"] == "1"

    # --- BYTE IDENTITY, the invariant the byte-integrity review demanded ----
    # Host canonical bytes == R2 stored bytes, octet for octet — not merely
    # "the same document".
    assert obj["stored_bytes_equal_host_bytes"] is True, "R2 does not hold the host bytes"
    assert obj["stored_bytes_hex_equals_host"] is True
    # SHA-256(host bytes) == ledger payload_digest == SHA-256(R2 body).
    assert obj["host_digest"] == host_digest
    assert obj["ledger_digest"] == host_digest, "the ledger digest is not the host digest"
    assert obj["stored_digest"] == host_digest, "the stored object does not hash to the ledger digest"
    assert obj["metadata_digest"] == host_digest
    # And this is why storing the strict-schema rebuild was a defect and not a
    # formatting preference: for real canonical host output it is a DIFFERENT
    # byte sequence. If this stops being true the detector has gone blind.
    assert obj["schema_rebuild_differs_from_host_bytes"] is True, \
        "the schema rebuild matched the host bytes; the detector proves nothing"

    driver = case["driver"]
    assert driver["exchange_status"] == 204
    assert driver["cookie_is_httponly"] is True
    assert driver["cookie_is_samesite_strict"] is True
    assert driver["snapshot_status"] == 200
    assert driver["snapshot_cache_control"].startswith("private, no-store")
    assert driver["snapshot_content_type"].startswith("application/json")
    assert driver["snapshot_matches_canonical"] is True, "the served JSON is not the published one"
    assert driver["snapshot_omits_subject"] is True
    assert driver["snapshot_omits_object_key"] is True
    assert driver["parameterised_read_refused"] == 401, driver

    recovery = case["recovery"]
    assert recovery["blind_retry_result"] == "ALREADY_PUBLISHED"
    assert recovery["blind_retry_next_action"] == "USE_PERSISTED_BEARER_OR_RECOVER"
    assert recovery["blind_retry_bearer"] is False
    assert recovery["recover_status"] == 200 and recovery["recover_result"] == "RECOVERED"
    assert recovery["replacement_is_different"] is True
    assert recovery["superseded_capability_id"] is True
    assert recovery["bearer_generation"] == 2
    assert recovery["live_grants"] == 1, "recovery left more than one live grant"
    assert recovery["old_bearer_exchange"] == 401, "the lost bearer still works"
    assert recovery["old_session_snapshot"] == 401, "a session from the lost bearer survived"
    assert recovery["new_bearer_exchange"] == 204
    assert recovery["new_snapshot_status"] == 200
    assert recovery["new_snapshot_matches_canonical"] is True
    assert recovery["objects_still"] == 1, "recovery created a second object"

    delivery = case["delivery"]
    assert delivery["intent_result"] == "RECORDED"
    assert delivery["delivered_result"] == "RECORDED"
    assert delivery["delivered_state"] == "DELIVERED"
    assert delivery["recovery_status"] == 409
    assert delivery["recovery_reason"] == "ALREADY_DELIVERED"
    assert delivery["recovery_bearer"] is False
    assert delivery["delivered_bearer_exchange"] == 204, "the delivered bearer was invalidated"
    assert delivery["delivered_bearer_snapshot"] == 200
    assert delivery["republish_result"] == "ALREADY_DELIVERED"
    assert delivery["republish_next_action"] == "NONE"
    assert delivery["bearer_generation"] == 2, "delivery moved the bearer generation"
    assert delivery["final_live_grants"] == 1
    assert delivery["final_objects"] == 1
    assert delivery["final_operations"] == 1
    # Recovery and delivery are authorization transitions; neither may touch
    # the authoritative snapshot bytes or the digest that identifies them.
    assert delivery["stored_bytes_unchanged"] is True
    assert delivery["stored_digest_unchanged"] is True
    assert delivery["ledger_digest_unchanged"] is True

    assert all(value is False for value in case["hygiene"].values()), case["hygiene"]
    print("PASS test_local_end_to_end_publish_recover_deliver")


def main() -> None:
    test_concurrent_publication_converges_at_every_scale()
    test_contention_holds_under_repetition()
    test_forced_races_at_every_transition_boundary()
    test_serialised_release_cannot_mint_a_second_bearer()
    test_same_operation_with_different_content_fails_safely()
    test_every_crash_window_converges_safely()
    test_response_loss_after_commit_is_unambiguous()
    test_recovery_replaces_exactly_one_grant()
    test_concurrent_recovery_leaves_one_replacement()
    test_recovery_edge_cases_never_leave_two_live_grants()
    test_delivered_capability_is_never_invalidated()
    test_recovery_decided_before_delivery_cannot_commit_after_it()
    test_delivery_on_a_superseded_bearer_is_refused()
    test_grant_cannot_exist_without_the_ledger_referencing_it()
    test_every_publisher_route_uses_the_authoritative_contract()
    test_one_operation_owns_exactly_one_object()
    test_barriers_exist_only_in_local_test_infrastructure()
    test_local_end_to_end_publish_recover_deliver()
    print("Driver Eco Dashboard publication transaction gate passed")


if __name__ == "__main__":
    main()
