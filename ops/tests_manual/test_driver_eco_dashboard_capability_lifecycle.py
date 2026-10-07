#!/usr/bin/env python3
"""Driver Eco Dashboard V1 — period-scoped capability lifetimes and cleanup.

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_capability_lifecycle.py

WHAT THIS PROVES

The owner-approved lifecycle: a capability's lifetime is a property of the
REPORTING PERIOD it was issued for — weekly 10 days, monthly 60 — and a link
stays pinned to the historical snapshot of that period until its own expiry.

  * the lifetime is EXACT, and it is what the deployed route actually stamps,
    not what the policy table says it should;
  * an unknown, empty, missing or duplicated period type fails closed, with
    zero operations, zero grants and zero R2 objects created;
  * recovery mints its replacement under the SAME period-aware policy and
    refuses an unstatable period without touching authorization state;
  * consecutive periods OVERLAP: publishing W2 neither revokes nor re-points
    W1, and the three live links each keep showing their own report;
  * an expired link answers `LINK_EXPIRED` — a classification the grant row
    has to survive to be able to give — while the next period keeps working;
  * a session can never outlive its grant, and an expired grant establishes
    none at all;
  * the publisher-authenticated maintenance route retires expired sessions,
    is idempotent, touches no live state, and is invisible to every caller
    without the machine credential.

It drives `ops/tests_manual/eco_capability_lifecycle_harness.mjs`, which
executes the REAL Cloudflare Worker against in-memory D1/R2/ASSETS bindings.
No wrangler, no credentials, no remote Cloudflare resource, no production data.

No capability, session id or object key is printed by this suite or by the
harness it drives.
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
HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_capability_lifecycle_harness.mjs"

DAY = 86400
WEEKLY_DAYS = 10
MONTHLY_DAYS = 60

PASSED: list[str] = []
_CACHE: dict = {}


def scenarios() -> dict:
    if not _CACHE:
        result = subprocess.run(
            ["node", str(HARNESS)], capture_output=True, text=True,
            cwd=str(REPO_ROOT), timeout=600,
        )
        if result.returncode != 0:
            raise AssertionError(f"harness failed: {result.stderr.strip()[:600]}")
        _CACHE.update(json.loads(result.stdout))
        broken = {name: value.get("harness_error")
                  for name, value in _CACHE.items()
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


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}" if detail else label)


# --- the TTL policy ----------------------------------------------------------


def test_weekly_is_exactly_ten_days_and_monthly_exactly_sixty() -> None:
    """The numbers, measured on the route rather than read from the table."""
    case = scenarios()["period_ttl_is_exact"]
    check("a weekly capability lives exactly 10 days",
          case["weekly_lifetime_seconds"] == WEEKLY_DAYS * DAY,
          str(case["weekly_lifetime_seconds"]))
    check("a monthly capability lives exactly 60 days",
          case["monthly_lifetime_seconds"] == MONTHLY_DAYS * DAY,
          str(case["monthly_lifetime_seconds"]))
    check("and they are stated in whole days", case["weekly_days"] == WEEKLY_DAYS
          and case["monthly_days"] == MONTHLY_DAYS)
    # The route and the policy module must be the same statement, not two.
    check("the route stamps exactly what the policy table declares",
          case["policy_weekly"] == case["weekly_lifetime_seconds"]
          and case["policy_monthly"] == case["monthly_lifetime_seconds"])
    check("the policy table cannot be mutated at runtime", case["policy_is_frozen"])
    check("the two period types no longer share one lifetime",
          case["weekly_lifetime_seconds"] != case["monthly_lifetime_seconds"])
    PASSED.append("weekly_is_exactly_ten_days_and_monthly_exactly_sixty")


def test_there_is_exactly_one_authoritative_mapping() -> None:
    """No second lifetime table, and no default anywhere.

    The 45-day contract's defect was its SHAPE: a default is something a caller
    can omit, and every omission produced a lifetime nobody had chosen for that
    period. These assertions are what stop it coming back by a different name.
    """
    policy = source("worker/lib/capability_ttl.js")
    check("the policy module states both lifetimes",
          "10 * DAY_SECONDS" in policy and "60 * DAY_SECONDS" in policy)
    check("and it throws rather than defaulting",
          "throw error" in code_only(policy) and "UNKNOWN_PERIOD_TYPE" in policy)

    # No other module may state a capability lifetime.
    for relative in ("worker/index.js", "worker/lib/publisher.js",
                     "worker/lib/publication.js", "worker/lib/store.js",
                     "local/dev_grants.js"):
        body = code_only(source(relative))
        check(f"{relative} declares no capability lifetime of its own",
              "60 * 60 * 24" not in body and "86400" not in body, relative)
        check(f"{relative} carries no removed universal default",
              "DEFAULT_CAPABILITY_TTL_SECONDS" not in body, relative)

    # The `||` fallback that made an omitted lifetime invisible is gone.
    publisher = code_only(source("worker/lib/publisher.js"))
    check("rotation demands an explicit lifetime",
          "requireTtlSeconds(params.ttl_seconds)" in publisher)
    check("and does not fall back to anything",
          "params.ttl_seconds ||" not in publisher)

    publication = code_only(source("worker/lib/publication.js"))
    check("publication validates the lifetime before any state exists",
          publication.index("requireTtlSeconds")
          < publication.index("claimOperation"))
    check("both mints go through the validated value",
          publication.count("params.now + ttlSeconds") == 2, publication.count("params.now + ttlSeconds"))

    index = code_only(source("worker/index.js"))
    check("publish and recover both derive the lifetime from the period",
          index.count("capabilityTtlSeconds(periodType)") == 2)
    check("the period is refused before it can become a lifetime",
          index.count("isSupportedPeriodType(periodType)") == 2)
    PASSED.append("there_is_exactly_one_authoritative_mapping")


def test_an_unknown_period_type_fails_closed() -> None:
    """Refused, and refused BEFORE anything durable exists."""
    cases = scenarios()["unknown_period_fails_closed"]
    for label, case in cases.items():
        check(f"{label} is refused", case["status"] == 400, f"{label}: {case}")
        check(f"{label} creates no publication operation", case["operations"] == 0, label)
        check(f"{label} creates no grant", case["capabilities"] == 0, label)
        check(f"{label} writes no R2 object", case["objects"] == 0, label)
    check("a missing period is a control-header refusal",
          cases["missing"]["error"] == "INVALID_CONTROL_HEADER")
    check("a duplicated period is ambiguous, not resolved by picking one",
          cases["duplicated"]["error"] == "AMBIGUOUS_CONTROL_HEADER"
          and cases["duplicated_same"]["error"] == "AMBIGUOUS_CONTROL_HEADER")
    for label in ("unknown_word", "wrong_case", "numeric"):
        check(f"{label} is a period-type refusal",
              cases[label]["error"] == "INVALID_PERIOD_TYPE", label)
    PASSED.append("an_unknown_period_type_fails_closed")


def test_recovery_uses_the_same_period_aware_policy() -> None:
    """A rotation writes a fresh expiry, so it must use the same policy."""
    case = scenarios()["recovery_uses_the_same_policy"]
    check("a recovered weekly grant lives exactly 10 days",
          case["weekly"]["lifetime_seconds"] == WEEKLY_DAYS * DAY,
          str(case["weekly"]["lifetime_seconds"]))
    check("a recovered monthly grant lives exactly 60 days",
          case["monthly"]["lifetime_seconds"] == MONTHLY_DAYS * DAY,
          str(case["monthly"]["lifetime_seconds"]))
    for period in ("weekly", "monthly"):
        check(f"the {period} replacement supersedes its predecessor",
              case[period]["predecessor_superseded"] and case[period]["bearer_differs"])
        check(f"and the {period} replacement is a full fresh lifetime",
              case[period]["later_than_predecessor"])
    check("an unknown period refuses the recovery",
          case["unknown_period"]["status"] == 400
          and case["unknown_period"]["error"] == "INVALID_PERIOD_TYPE")
    check("and mutates no authorization state",
          case["unknown_period"]["capabilities_unchanged"])
    check("a missing period refuses the recovery",
          case["missing_period"]["status"] == 400
          and case["missing_period"]["capabilities_unchanged"])
    PASSED.append("recovery_uses_the_same_period_aware_policy")


def test_the_host_states_the_period_from_the_ledger_and_computes_no_lifetime() -> None:
    """The boundary carries the FACT; the Worker owns the POLICY.

    Duplicating the numbers on the host is how the two ends quietly stop
    agreeing, so the host must not contain a lifetime table at all — and the
    period it states must come from the immutable ledger column rather than
    from anything presentational.
    """
    client = (REPO_ROOT / "jobs" / "ecodriving_dashboard"
              / "secure_delivery_client.py").read_text(encoding="utf-8")
    check("the client names the period header",
          'PUBLICATION_PERIOD_HEADER = "X-Publication-Period"' in client)
    check("it refuses an unknown period before any request exists",
          "INVALID_PERIOD_TYPE" in client and "PERIOD_TYPES" in client)
    check("and it declares no lifetime of its own",
          not re.search(r"\b(10|60)\s*\*\s*(24|86400|DAY)", client))

    publisher = (REPO_ROOT / "jobs" / "ecodriving_dashboard"
                 / "publisher.py").read_text(encoding="utf-8")
    check("publish states the ledger's period type",
          "period_type=record.period_type," in publisher)
    check("recover states it too",
          "period_type=record.period_type)" in publisher)
    check("the host computes no capability lifetime",
          "timedelta(days=10)" not in publisher and "timedelta(days=60)" not in publisher)

    contract = (REPO_ROOT / "jobs" / "ecodriving_dashboard"
                / "delivery_contract.py").read_text(encoding="utf-8")
    check("and the period vocabulary is defined exactly once",
          contract.count('PERIOD_TYPES = ("weekly", "monthly")') == 1)
    PASSED.append("the_host_states_the_period_from_the_ledger_and_computes_no_lifetime")


# --- overlapping historical links -------------------------------------------


def test_a_newer_period_does_not_revoke_an_older_one() -> None:
    """The rejected model was one rolling URL per driver. This is the approved one.

    W1 on day 0, W2 on day 7, M1 the same day: three grants, three reports,
    three independent expiries, and an old e-mail keeps showing the report it
    was written about.
    """
    case = scenarios()["consecutive_periods_overlap"]
    check("W1 is still ACTIVE after W2 is published",
          case["w1_state_after_w2"] == "ACTIVE", case["w1_state_after_w2"])
    check("publishing W2 does not revoke W1", case["w1_not_revoked"])
    check("and does not rotate W1 onto a successor", case["w1_not_rotated"])
    check("W1 keeps exactly its own remaining ~3 days",
          case["w1_remaining_days"] == 3, str(case["w1_remaining_days"]))
    check("W2 gets its own full 10 days",
          case["w2_lifetime_days"] == WEEKLY_DAYS, str(case["w2_lifetime_days"]))
    check("and the monthly grant is independent at 60 days",
          case["m1_lifetime_days"] == MONTHLY_DAYS, str(case["m1_lifetime_days"]))
    check("three distinct grants exist at once",
          case["distinct_capability_ids"] == 3 and case["live_grants"] == 3,
          str(case))
    check("and the three links show three DIFFERENT reports",
          case["distinct_served_reports"] == 3, str(case["distinct_served_reports"]))
    for label in ("w1", "w2", "m1"):
        check(f"{label} still opens", case["seen"][label]["exchanged"]
              and case["seen"][label]["status"] == 200, label)
        check(f"{label} is snapshot-pinned across sessions",
              case["seen"][label]["stable_across_sessions"], label)
    PASSED.append("a_newer_period_does_not_revoke_an_older_one")


def test_an_expired_link_is_expired_and_not_unknown() -> None:
    case = scenarios()["w1_expires_on_its_own_schedule"]
    check("the expired link answers LINK_EXPIRED",
          case["w1_exchange_status"] == 410, str(case["w1_exchange_status"]))
    check("and establishes no session", case["w1_sets_no_cookie"])
    check("the store classifies it EXPIRED, not UNKNOWN",
          case["w1_classification"] == "EXPIRED", case["w1_classification"])
    check("the grant row survives so that classification is possible at all",
          case["w1_row_retained"])
    check("and the surviving row holds no raw bearer",
          case["w1_row_holds_no_raw_bearer"])
    check("the next period's link is unaffected",
          case["w2_still_works"] and case["w2_exchange_status"] == 204)
    PASSED.append("an_expired_link_is_expired_and_not_unknown")


# --- sessions ----------------------------------------------------------------


def test_a_session_never_outlives_its_grant() -> None:
    case = scenarios()["session_never_outlives_its_grant"]
    check("a session is issued while the grant is alive",
          case["exchanged_before_expiry"])
    check("the session expiry is capped at the grant expiry",
          case["session_capped_at_grant"]
          and case["session_expiry"] <= case["grant_expiry"],
          f"{case['session_expiry']} vs {case['grant_expiry']}")
    check("the cookie lifetime is capped too",
          case["cookie_max_age_within_grant"], str(case["cookie_max_age"]))
    check("an expired capability establishes NO session",
          case["expired_exchange_status"] == 410
          and case["expired_exchange_sets_no_cookie"]
          and case["no_new_session_after_expiry"])
    check("and a session issued before expiry stops working with the grant",
          case["stale_session_read_status"] == 401,
          str(case["stale_session_read_status"]))
    session = code_only(source("worker/index.js"))
    check("the cap is arithmetic in the route, not a convention",
          "Math.min(" in session and "SESSION_TTL_SECONDS" in session)
    PASSED.append("a_session_never_outlives_its_grant")


# --- authorization-state cleanup ---------------------------------------------


def test_maintenance_retires_dead_state_and_only_dead_state() -> None:
    case = scenarios()["maintenance_compacts_only_dead_state"]
    check("nothing is removed while every session is live",
          case["early_removed"] == 0 and case["sessions_unchanged_while_live"])
    check("expired sessions are removed once they are expired",
          case["first_status"] == 200 and case["first_removed"] == case["sessions_before"],
          str(case))
    check("a second pass is a no-op — the sweep is idempotent",
          case["second_removed"] == 0, str(case["second_removed"]))
    check("and it reports whether more work remains", case["batch_full"] is False)
    check("live grants are untouched",
          case["capabilities_after"] == case["capabilities_before"]
          and case["weekly_still_works"] and case["monthly_still_works"])
    check("the response says plainly that expired grants are retained",
          case["expired_capabilities_retained"] is True)
    check("and it echoes no secret", case["response_names_no_secret"])
    PASSED.append("maintenance_retires_dead_state_and_only_dead_state")


def test_maintenance_is_not_a_public_surface() -> None:
    """A cleanup route reachable without the machine credential would be a
    denial-of-service lever on live authorization state. It answers 404 —
    the same answer every other publisher route gives an unauthenticated
    caller, so its existence is not even confirmable."""
    case = scenarios()["maintenance_compacts_only_dead_state"]
    check("an anonymous caller cannot see it", case["anonymous_status"] == 404)
    check("a wrong machine token cannot see it", case["wrong_token_status"] == 404)
    check("a DRIVER session cannot reach it", case["driver_session_status"] == 404)
    check("and it is POST-only", case["wrong_method_status"] == 405)

    index = code_only(source("worker/index.js"))
    body = index[index.index("async function handlePublishMaintenance"):]
    check("the route authorises the publisher before doing anything",
          body.index("authorisePublisher") < body.index("deleteExpiredSessions"))
    # Capability deletion EXISTS now — the global 13-calendar-month retention
    # policy superseded "retain the tombstone forever" — but it is reachable
    # only through this publisher-authenticated route, and only for a grant that
    # is past the ceiling, already expired, and referenced by nothing.
    check("capability deletion happens after the publisher is authorised",
          body.index("authorisePublisher")
          < body.index("deleteRetiredCapabilitiesBefore"))
    check("the route deletes no capability except through the retention sweep",
          "deleteCapability" not in index
          and "DELETE FROM eco_capability" not in index)
    store = code_only(source("worker/lib/store.js"))
    check("the only capability deletion in the store is the ceiling sweep",
          store.count("DELETE FROM eco_capability") == 1)
    check("and it is gated on the ceiling, on expiry, and on both references",
          "c.issued_at < ?1" in store and "c.expires_at < ?2" in store
          and "FROM eco_session s" in store
          and "FROM eco_publication_operation p" in store)
    PASSED.append("maintenance_is_not_a_public_surface")


def test_expired_grants_are_retained_and_snapshots_are_untouched() -> None:
    """Capability expiry is NOT report deletion, and NOT row deletion."""
    case = scenarios()["expired_grants_survive_compaction"]
    check("the compaction succeeds", case["status"] == 200)
    check("the dead session is gone", case["sessions_left"] == 0
          and case["sessions_removed"] == 1)
    check("the expired grant row is retained", case["grant_row_retained"])
    check("it still classifies as EXPIRED",
          case["grant_classification"] == "EXPIRED", case["grant_classification"])
    check("so the old link still answers LINK_EXPIRED",
          case["expired_exchange_status"] == 410)
    check("and the historical snapshot object is untouched by bearer expiry",
          case["objects_retained"] == 1, str(case["objects_retained"]))

    schema = source("schema/001_authorization.sql")
    check("the schema records why expired grants are kept",
          "LINK_EXPIRED" in schema and "RETENTION IN R2 IS A SEPARATE CONCERN" in schema)
    index = code_only(source("worker/index.js"))
    check("and no route deletes an R2 object",
          ".delete(" not in index, "the Worker must never delete a snapshot")
    PASSED.append("expired_grants_are_retained_and_snapshots_are_untouched")


def main() -> None:
    test_weekly_is_exactly_ten_days_and_monthly_exactly_sixty()
    test_there_is_exactly_one_authoritative_mapping()
    test_an_unknown_period_type_fails_closed()
    test_recovery_uses_the_same_period_aware_policy()
    test_the_host_states_the_period_from_the_ledger_and_computes_no_lifetime()
    test_a_newer_period_does_not_revoke_an_older_one()
    test_an_expired_link_is_expired_and_not_unknown()
    test_a_session_never_outlives_its_grant()
    test_maintenance_retires_dead_state_and_only_dead_state()
    test_maintenance_is_not_a_public_surface()
    test_expired_grants_are_retained_and_snapshots_are_untouched()
    for name in PASSED:
        print(f"PASS {name}")
    print(f"\n{len(PASSED)} checks passed — period-scoped capability lifecycle")


if __name__ == "__main__":
    main()
