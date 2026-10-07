#!/usr/bin/env python3
"""Driver Eco Dashboard: the 13-calendar-month ceiling on D1 and R2.

Run:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_driver_eco_dashboard_hard_retention.py

WHAT THIS PROVES

The owner decision that supersedes "an expired capability never deletes the
historical report it pointed at, and the row is kept forever":

  * the report still OUTLIVES its link — a 40-day-old expired weekly grant, its
    ledger row and its snapshot are all untouched;
  * but the ceiling ends it. One second before 13 calendar months nothing is
    removed; one second after, the grant, the publication row and the R2 object
    all go, and a repeat pass is a no-op;
  * the horizon is CALENDAR arithmetic, and the Worker's vectors match the
    Python registry's exactly;
  * the shorter access TTLs are unchanged — weekly 10 days, monthly 60;
  * a live grant, and any state younger than the ceiling, is never touched;
  * foreign keys decide the order: a grant a publication still references is
    deferred and reported, never orphaned;
  * an R2 outage does not undo committed D1 work and is reported as its own
    outcome;
  * an object whose upload time cannot be established is never deleted;
  * the sweep is bounded, pages its listing, and is idempotent;
  * enumeration and deletion remain behind the publisher credential.

It drives `ops/tests_manual/eco_hard_retention_harness.mjs`, which executes the
REAL Worker against in-memory D1/R2/ASSETS bindings. No wrangler, no
credentials, no remote Cloudflare resource, no production data.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import retention_registry as rr  # noqa: E402

DELIVERY = REPO_ROOT / "delivery" / "driver_eco_dashboard"
HARNESS = REPO_ROOT / "ops" / "tests_manual" / "eco_hard_retention_harness.mjs"

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


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}" if detail else label)


def test_the_worker_and_the_registry_agree_on_the_ceiling() -> None:
    case = scenarios()["ceiling_is_thirteen_calendar_months"]
    check("the Worker uses 13 months", case["months"] == rr.HARD_RETENTION_MONTHS)
    check("and names the same policy", case["policy_id"] == rr.GLOBAL_POLICY_ID)
    for iso, produced in case["vectors"].items():
        expected = rr.hard_retention_cutoff(
            datetime.fromisoformat(iso.replace("Z", "+00:00"))
        ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        check(f"the two runtimes agree at {iso}", produced == expected,
              f"worker={produced} python={expected}")
    check("the calendar span is not a fixed day count",
          case["days_between_t0_and_deadline"] == 396,
          "13 months from 2027-01-15 is 396 days; from other dates it is not")
    PASSED.append("the_worker_and_the_registry_agree_on_the_ceiling")


def test_the_worker_deletes_ahead_of_the_deadline() -> None:
    """Maintenance is periodic, so the Worker must lead the deadline too.

    Without a lead, a grant minted one minute after a sweep would outlive its
    13-month deadline by a whole maintenance interval — the same defect the
    platform sweep had, in a second runtime.
    """
    case = scenarios()["ceiling_is_thirteen_calendar_months"]
    cycle = rr.MAINTENANCE_CYCLES["eco-dashboard-maintenance"].guaranteed_interval
    check("the Worker's lead is the declared maintenance cycle",
          case["enforcement_lead_seconds"] == int(cycle.total_seconds()),
          f"{case['enforcement_lead_seconds']}s vs {int(cycle.total_seconds())}s")

    for policy_id in ("cloudflare_d1.eco_capability",
                      "cloudflare_d1.eco_publication_operation",
                      "cloudflare_r2.driver_eco_snapshots"):
        policy = rr.get(policy_id)
        check(f"{policy_id} leads by the same interval",
              policy.enforcement_lead() == cycle, str(policy.enforcement_lead()))
        check(f"{policy_id} carries no backup shadow",
              policy.in_backup_set is False,
              "D1 and R2 are in no repository-controlled backup set")

    check("the enforcement cutoff reaches past the deadline cutoff",
          case["enforcement_is_ahead_of_deadline"] is True)
    check("so a grant published just after a sweep is collected on the next one",
          case["published_just_after_a_sweep_is_collected_next_time"] is True,
          "this is the periodic-sweep gap, closed")

    tombstone = scenarios()["tombstone_survives_expiry_and_ends_at_the_ceiling"]
    check("the response reports both horizons",
          tombstone["reports_both_horizons"] is True,
          "an operator must be able to see the policy and the lead")
    check("and the lead it used", tombstone["reported_lead_seconds"]
          == int(cycle.total_seconds()))
    PASSED.append("the_worker_deletes_ahead_of_the_deadline")


def test_d1_and_r2_retention_does_not_depend_on_eco_business_traffic() -> None:
    """THE independence requirement.

    The Worker's D1/R2 retention previously ran only from the Eco mailing run's
    maintenance boundary. Four of five production clients have every Eco
    schedule disabled, so that made retention conditional on somebody sending
    e-mail — business activity is not a retention scheduler. The platform
    hard-retention sweep now calls the same publisher-authenticated route
    itself. This is executable call-path evidence, not a reading of the code.
    """
    import os
    from ops import hard_retention as hr
    from ops import schedule_catalog as sc
    from jobs.ecodriving_dashboard import secure_delivery_client as sdc

    posted: list[tuple] = []

    class RecordingTransport:
        def __init__(self, base_url: str, **kwargs) -> None:
            self.base_url = base_url

        def post(self, path, headers, body):
            posted.append((path, dict(headers), body))
            return 200, {
                "status": "COMPACTED",
                "expired_sessions_removed": 4,
                "hard_retention": {
                    "capabilities_removed": 3,
                    "publications_removed": 2,
                    "snapshots_examined": 9,
                    "snapshots_removed": 5,
                    "snapshot_error": None,
                },
            }

    original_transport = sdc.HttpSecureDeliveryTransport
    previous = {name: os.environ.get(name)
                for name in (hr.ENV_PUBLISHER_ENDPOINT, hr.ENV_PUBLISHER_TOKEN)}
    try:
        sdc.HttpSecureDeliveryTransport = RecordingTransport
        os.environ[hr.ENV_PUBLISHER_ENDPOINT] = "https://dashboard.example.invalid"
        os.environ[hr.ENV_PUBLISHER_TOKEN] = "synthetic-machine-token"

        outcomes = hr.sweep_eco_dashboard_maintenance(
            dry_run=False, now=datetime(2026, 8, 29, 5, 0, tzinfo=timezone.utc))

        check("the platform sweep contacted the Worker exactly once",
              len(posted) == 1, str(len(posted)))
        path, headers, body = posted[0]
        check("on the publisher maintenance route",
              path == "/api/publish/maintenance", path)
        check("with the publisher credential",
              headers.get("Authorization", "").startswith("Publisher "),
              "publisher authentication is preserved, not bypassed")
        check("and no body — it is not a publication",
              body is None, repr(body))

        by_policy = {item.policy_id: item for item in outcomes}
        check("every Cloudflare policy got an outcome",
              set(by_policy) == set(hr.ECO_MAINTENANCE_POLICIES), str(sorted(by_policy)))
        check("expired sessions are attributed to the session policy",
              by_policy["cloudflare_d1.eco_session"].deleted == 4)
        check("grants to the capability policy",
              by_policy["cloudflare_d1.eco_capability"].deleted == 3)
        check("ledger rows to the publication policy",
              by_policy["cloudflare_d1.eco_publication_operation"].deleted == 2)
        check("and R2 objects to the snapshot policy",
              by_policy["cloudflare_r2.driver_eco_snapshots"].deleted == 5
              and by_policy["cloudflare_r2.driver_eco_snapshots"].examined == 9)
        check("all four succeeded",
              all(item.classification == "RETENTION_EXECUTION_SUCCEEDED"
                  for item in outcomes), str([i.classification for i in outcomes]))

        # No publication, no capability, no mailing run, no enabled Eco schedule
        # and no driver was involved in any of the above.
        check("nothing on the call path published anything",
              all("publish" not in path or path.endswith("/maintenance")
                  for path, _, _ in posted))

        # A dry run must contact nothing at all: the route has no plan-only mode.
        posted.clear()
        dry = hr.sweep_eco_dashboard_maintenance(
            dry_run=True, now=datetime(2026, 8, 29, 5, 0, tzinfo=timezone.utc))
        check("a dry run contacts nothing", not posted)
        check("and says so rather than reporting deletions",
              all(item.deleted == 0
                  and item.classification == "RETENTION_DRY_RUN_SUCCEEDED"
                  for item in dry))

        # Missing credential: visibly ungoverned, never silently skipped.
        os.environ.pop(hr.ENV_PUBLISHER_TOKEN)
        unavailable = hr.sweep_eco_dashboard_maintenance(
            dry_run=False, now=datetime(2026, 8, 29, 5, 0, tzinfo=timezone.utc))
        check("without the credential the stores are reported as ungoverned",
              all(item.defect_code == "ECO_PUBLISHER_UNAVAILABLE"
                  and item.failed == 1 for item in unavailable),
              str([item.defect_code for item in unavailable]))
    finally:
        sdc.HttpSecureDeliveryTransport = original_transport
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # And it is represented as such in the central schedule catalogue.
    entry = sc.BY_ID["eco-dashboard-maintenance"]
    check("the catalogue names its driver",
          entry.driven_by == "platform-hard-retention", str(entry.driven_by))
    check("its source of truth is the platform sweep, not the mailing run",
          "ops/hard_retention.py" in entry.source_of_truth, entry.source_of_truth)
    check("its declared cadence is the platform sweep's",
          "weekly" in (entry.declared_cadence or ""), str(entry.declared_cadence))
    check("and the registry cycle records why business activity cannot be the "
          "scheduler",
          "business activity cannot be a retention scheduler"
          in rr.MAINTENANCE_CYCLES["eco-dashboard-maintenance"].rationale,
          rr.MAINTENANCE_CYCLES["eco-dashboard-maintenance"].rationale[:120])
    check("the catalogue reports no drift", not sc.validate())
    PASSED.append("d1_and_r2_retention_does_not_depend_on_eco_business_traffic")


def test_shorter_access_ttls_are_unchanged() -> None:
    case = scenarios()["ceiling_is_thirteen_calendar_months"]
    check("weekly capabilities still live 10 days", case["weekly_ttl_days"] == 10)
    check("monthly capabilities still live 60 days", case["monthly_ttl_days"] == 60)
    check("and both are far shorter than the ceiling",
          case["monthly_ttl_days"] * 6 < case["days_between_t0_and_deadline"])
    PASSED.append("shorter_access_ttls_are_unchanged")


def test_the_report_outlives_the_link_but_not_the_ceiling() -> None:
    case = scenarios()["tombstone_survives_expiry_and_ends_at_the_ceiling"]
    check("the maintenance call succeeds throughout",
          case["early_status"] == 200 and case["just_before_status"] == 200
          and case["just_after_status"] == 200)

    check("40 days after issue the grant has expired",
          case["early_classification"] == "EXPIRED")
    check("but its row, its ledger entry and its snapshot are all retained",
          case["after_early"] == {"capabilities": 1, "sessions": 0,
                                  "publications": 1, "objects": 1},
          str(case["after_early"]))
    check("and the sweep removed nothing at that point",
          case["early_removed"]["capabilities_removed"] == 0
          and case["early_removed"]["snapshots_removed"] == 0)

    check("one second BEFORE the ceiling, still nothing is removed",
          case["after_just_before"] == case["after_early"],
          str(case["after_just_before"]))

    check("one second AFTER, the grant is gone", case["grant_row_gone"] is True)
    check("so is the publication ledger row and the snapshot object",
          case["after_just_after"] == {"capabilities": 0, "sessions": 0,
                                       "publications": 0, "objects": 0},
          str(case["after_just_after"]))
    check("and the sweep reports exactly what it did",
          case["just_after_removed"]["capabilities_removed"] == 1
          and case["just_after_removed"]["publications_removed"] == 1
          and case["just_after_removed"]["snapshots_removed"] == 1,
          str(case["just_after_removed"]))
    check("nothing eligible is left behind",
          case["just_after_removed"]["oldest_retained_issue"] is None)
    check("a repeat pass is a no-op",
          case["repeat_status"] == 200
          and case["repeat_removed"]["capabilities_removed"] == 0
          and case["repeat_removed"]["snapshots_removed"] == 0)
    PASSED.append("the_report_outlives_the_link_but_not_the_ceiling")


def test_younger_state_is_never_collected() -> None:
    case = scenarios()["younger_state_is_untouched"]
    check("the old publication is swept",
          case["removed"]["capabilities_removed"] == 1
          and case["removed"]["snapshots_removed"] == 1, str(case["removed"]))
    check("exactly one grant, ledger row and object remain",
          case["remaining"] == {"capabilities": 1, "sessions": 0,
                                "publications": 1, "objects": 1},
          str(case["remaining"]))
    check("the surviving grant is the fresh one", case["fresh_grant_retained"])
    check("and it is still ACTIVE — retention never revokes",
          case["fresh_grant_classification"] == "ACTIVE",
          case["fresh_grant_classification"])
    PASSED.append("younger_state_is_never_collected")


def test_a_referenced_grant_is_deferred_not_orphaned() -> None:
    case = scenarios()["a_referenced_grant_is_never_orphaned"]
    check("a grant its ledger row still names is not removed",
          case["grant_protected_by_reference"] is True)
    check("and the first pass removed no capability",
          case["first_removed"]["capabilities_removed"] == 0,
          str(case["first_removed"]))
    check("the sweep reports that something eligible survived",
          case["oldest_retained_issue_reported"] is True,
          "a silent skip would be indistinguishable from compliance")
    check("once the ledger row ages out too, both go",
          case["second_removed"]["publications_removed"] == 1
          and case["second_removed"]["capabilities_removed"] == 1,
          str(case["second_removed"]))
    check("and the grant is finally gone", case["grant_removed_once_unreferenced"])
    PASSED.append("a_referenced_grant_is_deferred_not_orphaned")


def test_a_bucket_outage_does_not_undo_committed_work() -> None:
    case = scenarios()["bucket_failure_is_reported_not_swallowed"]
    check("the call still succeeds", case["status"] == 200)
    check("the D1 deletions are committed", case["d1_work_committed"] is True)
    check("the R2 failure is reported rather than swallowed",
          case["snapshot_error_reported"] is True)
    check("the object survives the failed pass", case["objects_after_failure"] == 1)
    check("and the next pass collects it by age",
          case["recovered_removed"]["snapshots_removed"] == 1
          and case["objects_after_recovery"] == 0)
    PASSED.append("a_bucket_outage_does_not_undo_committed_work")


def test_an_object_with_no_upload_time_is_never_deleted() -> None:
    case = scenarios()["an_unageable_object_is_never_deleted"]
    check("the object is examined", case["removed"]["snapshots_examined"] == 1)
    check("but never deleted", case["removed"]["snapshots_removed"] == 0
          and case["objects_retained"] == 1,
          "guessing an age is the one thing a retention sweep must not do")
    check("while the D1 rows, which DO have anchors, are collected",
          case["removed"]["capabilities_removed"] == 1)
    PASSED.append("an_object_with_no_upload_time_is_never_deleted")


def test_the_sweep_is_bounded_and_idempotent() -> None:
    case = scenarios()["the_sweep_is_bounded_and_pages"]
    check("all seven publications are collected",
          case["after"] == {"capabilities": 0, "sessions": 0,
                            "publications": 0, "objects": 0}, str(case["after"]))
    check("the R2 listing asked for a bounded page, not everything",
          case["list_page_limits"] and all(limit <= 1000 for limit in case["list_page_limits"]),
          str(case["list_page_limits"]))
    check("and a repeat pass does nothing", case["repeat_is_a_no_op"] is True)
    PASSED.append("the_sweep_is_bounded_and_idempotent")


def test_retention_is_publisher_only() -> None:
    case = scenarios()["retention_requires_the_publisher_credential"]
    check("an anonymous caller gets the same 404 every publisher route gives",
          case["anonymous_status"] == 404)
    check("and changes nothing", case["anonymous_changed_nothing"] is True,
          "a cleanup route reachable without the credential is a deletion lever")
    check("the credentialed call works", case["authorised_status"] == 200)
    check("and does the work", case["authorised_removed"]["capabilities_removed"] == 1)
    PASSED.append("retention_is_publisher_only")


def test_the_schema_records_the_new_lifecycle() -> None:
    schema = (DELIVERY / "schema" / "001_authorization.sql").read_text(encoding="utf-8")
    check("the schema still explains why an expired grant is kept",
          "LINK_EXPIRED" in schema)
    check("and now says where that ends",
          "SURVIVING EXPIRY IS NOT SURVIVING FOREVER" in schema)
    check("naming the ceiling", "13 CALENDAR MONTHS" in schema)
    check("and the anchor decision",
          "The anchor is `issued_at` and not" in schema)
    check("R2 retention is a separate concern with a horizon, not without one",
          "RETENTION IN R2 IS A SEPARATE CONCERN" in schema
          and "not an absent one" in schema)
    PASSED.append("the_schema_records_the_new_lifecycle")


def main() -> int:
    test_the_worker_and_the_registry_agree_on_the_ceiling()
    test_the_worker_deletes_ahead_of_the_deadline()
    test_d1_and_r2_retention_does_not_depend_on_eco_business_traffic()
    test_shorter_access_ttls_are_unchanged()
    test_the_report_outlives_the_link_but_not_the_ceiling()
    test_younger_state_is_never_collected()
    test_a_referenced_grant_is_deferred_not_orphaned()
    test_a_bucket_outage_does_not_undo_committed_work()
    test_an_object_with_no_upload_time_is_never_deleted()
    test_the_sweep_is_bounded_and_idempotent()
    test_retention_is_publisher_only()
    test_the_schema_records_the_new_lifecycle()
    for name in PASSED:
        print(f"PASS {name}")
    print(f"\n{len(PASSED)} checks passed — Eco Dashboard hard retention (D1 + R2)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
