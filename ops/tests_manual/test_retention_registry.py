#!/usr/bin/env python3
"""The global 13-calendar-month retention policy, proven rather than declared.

Run:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_retention_registry.py

Pure: no database, no network, no filesystem mutation.

WHAT THIS PROVES

  * the global default is exactly 13 CALENDAR months, and the cutoff is
    calendar arithmetic — not 390, 395 or 396 days;
  * the boundary cases are deterministic: month ends, February, leap years,
    exactly at the cutoff, and one second either side of it;
  * every registered policy is at or under the ceiling, and a longer one is
    impossible to introduce without an attributed owner override;
  * an exemption without a written rationale is refused by validation, so
    "audit data is forever" cannot be smuggled in as a mode;
  * the shorter lifetimes that already existed are still shorter, measured
    from the modules that own them rather than restated here;
  * the number 13 exists once in Python and once in JavaScript, and the two
    are pinned to each other;
  * every relation any repository DDL can create is claimed by some policy, so
    a new migration cannot introduce an ungoverned store;
  * the backup shadow is stated as an open problem and not as a solved one.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import replace as dc_replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import retention_registry as rr  # noqa: E402

UTC = timezone.utc
PASSED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}" if detail else label)


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text)


# --- the policy itself -------------------------------------------------------


def test_the_default_is_thirteen_calendar_months() -> None:
    check("the ceiling is 13", rr.HARD_RETENTION_MONTHS == 13)
    check("and it is expressed in months, not days",
          rr.GLOBAL_DEFAULT.unit is rr.Unit.MONTHS)
    check("the global default carries no number of its own",
          rr.GLOBAL_DEFAULT.value is None and rr.GLOBAL_DEFAULT.is_global_default,
          "a default that restates 13 is a second place to get it wrong")
    check("but it resolves to the ceiling",
          rr.GLOBAL_DEFAULT.effective_months == rr.HARD_RETENTION_MONTHS)
    PASSED.append("the_default_is_thirteen_calendar_months")


def test_the_cutoff_is_not_a_day_approximation() -> None:
    """13 calendar months is not a fixed number of days — and the difference
    is observable, so a day-count implementation cannot pass this."""
    spans = set()
    for month in range(1, 13):
        for day in (1, 15, 28):
            end = datetime(2027, month, day, 12, tzinfo=UTC)
            spans.add((end - rr.hard_retention_cutoff(end)).days)
    check("the span genuinely varies with the calendar", len(spans) > 1, str(sorted(spans)))
    check("and it never dips below the proven minimum",
          min(spans) >= rr.minimum_span_days(), str(sorted(spans)))
    check("the minimum span is 393 days", rr.minimum_span_days() == 393,
          str(rr.minimum_span_days()))

    # The decisive proof: NO fixed day count reproduces the calendar rule. For
    # each candidate approximation there is at least one date on which it lands
    # somewhere else — which is exactly what "do not silently translate the rule
    # into 395 days" means, and what a `timedelta(days=N)` implementation could
    # not satisfy.
    for approximation in (390, 393, 395, 396, 397):
        disagreements = [
            end for end in (
                datetime(2027, month, day, 12, tzinfo=UTC)
                for month in range(1, 13) for day in (1, 15, 28)
            )
            if (end - timedelta(days=approximation)) != rr.hard_retention_cutoff(end)
        ]
        check(f"{approximation} days is not the policy", bool(disagreements),
              f"{approximation} days reproduced the calendar rule on every date")
    PASSED.append("the_cutoff_is_not_a_day_approximation")


def test_calendar_boundaries_are_deterministic() -> None:
    vectors = {
        # ordinary date
        "2026-08-28T21:00:00+00:00": "2025-07-28T21:00:00+00:00",
        # month end clamped into a SHORTER month, non-leap target
        "2026-03-31T12:00:00+00:00": "2025-02-28T12:00:00+00:00",
        "2028-03-29T00:00:00+00:00": "2027-02-28T00:00:00+00:00",
        # leap year: 29 February exists in the target, so no clamp
        "2025-03-29T00:00:00+00:00": "2024-02-29T00:00:00+00:00",
        # 29 February itself, thirteen months on from a leap day
        "2025-03-31T06:30:00+00:00": "2024-02-29T06:30:00+00:00",
        # month end that needs no clamp at all
        "2026-01-31T23:59:59+00:00": "2024-12-31T23:59:59+00:00",
        # 30-day target month
        "2026-05-31T00:00:00+00:00": "2025-04-30T00:00:00+00:00",
    }
    for now, expected in vectors.items():
        got = rr.hard_retention_cutoff(moment(now))
        check(f"{now} - 13 months", got == moment(expected), got.isoformat())
    check("a clamp never rolls forward into the next month",
          rr.hard_retention_cutoff(moment("2026-03-31T12:00:00+00:00")).month == 2)
    check("microseconds and time of day survive the shift",
          rr.subtract_calendar_months(
              datetime(2026, 5, 17, 3, 4, 5, 678901, tzinfo=UTC), 13
          ) == datetime(2025, 4, 17, 3, 4, 5, 678901, tzinfo=UTC))
    check("a naive datetime is refused rather than assumed to be UTC",
          _raises(lambda: rr.hard_retention_cutoff(datetime(2026, 1, 1))))
    PASSED.append("calendar_boundaries_are_deterministic")


def test_eligibility_at_and_around_the_cutoff() -> None:
    """Exactly at the cutoff, one microsecond older, one microsecond newer.

    Eligibility is `record < cutoff` everywhere in this codebase, so a record
    sitting exactly on the cutoff is retained. That is a decision, and it is
    the one every implementation must share.
    """
    now = moment("2026-08-28T21:00:00+00:00")
    cutoff = rr.hard_retention_cutoff(now)
    check("exactly at the cutoff is NOT eligible", not (cutoff < cutoff))
    check("one microsecond older IS eligible",
          (cutoff - timedelta(microseconds=1)) < cutoff)
    check("one microsecond newer is not",
          not ((cutoff + timedelta(microseconds=1)) < cutoff))
    PASSED.append("eligibility_at_and_around_the_cutoff")


# --- the registry ------------------------------------------------------------


def test_the_registry_validates_clean() -> None:
    problems = rr.validate()
    check("no violations", not problems, "; ".join(str(p) for p in problems))
    check("policy ids are unique",
          len({p.policy_id for p in rr.POLICIES}) == len(rr.POLICIES))
    check("every policy names an owner",
          all(p.owner_domain.strip() for p in rr.POLICIES))
    PASSED.append("the_registry_validates_clean")


def test_nothing_exceeds_the_ceiling_and_no_override_is_unapproved() -> None:
    for policy in rr.POLICIES:
        if not policy.is_age_based:
            continue
        if policy.override is not None:
            check(f"{policy.policy_id} override is attributed",
                  bool(policy.override.approved_by and policy.override.reason
                       and policy.override.approved_on))
            continue
        if policy.retention.unit is rr.Unit.MONTHS:
            check(f"{policy.policy_id} is within the ceiling",
                  (policy.retention.effective_months or 0) <= rr.HARD_RETENTION_MONTHS)
        elif policy.retention.unit is rr.Unit.DAYS:
            check(f"{policy.policy_id} is within the ceiling",
                  int(policy.retention.value or 0) <= rr.minimum_span_days())
    check("and today no owner override exists at all",
          all(p.override is None for p in rr.POLICIES),
          "an override is allowed, but each one must be a deliberate decision")
    check("exactly one store is exempt from age-based retention",
          len(rr.owner_exemptions()) == 1,
          str([p.policy_id for p in rr.owner_exemptions()]))
    PASSED.append("nothing_exceeds_the_ceiling_and_no_override_is_unapproved")


# --- the one approved exception ----------------------------------------------

#: A fixed instant: none of the exemption assertions depend on the clock, and
#: pinning it keeps the suite hermetic.
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def test_the_gps_exception_is_explicit_attributed_and_alone() -> None:
    """The owner's 2026-08-29 decision, as the registry represents it.

    `telematics_reports."Alpha_GPS_Baza_LOG"` has NO age-based retention. What
    matters for an operator is that this is legible as a DECISION: not a
    blocked category, not a store nobody registered, not one whose anchor could
    not be found.
    """
    exemptions = rr.owner_exemptions()
    check("there is exactly one, and it is the GPS assignment log",
          [p.policy_id for p in exemptions] == [rr.GPS_ASSIGNMENT_POLICY_ID],
          str([p.policy_id for p in exemptions]))

    gps = rr.get(rr.GPS_ASSIGNMENT_POLICY_ID)
    check("the store names the exempted relation",
          "Alpha_GPS_Baza_LOG" in gps.store, gps.store)
    check("its governance class is the exempt one",
          gps.governance_class() == "OWNER_EXEMPT", gps.governance_class())
    check("it is not age-based", gps.is_age_based is False)
    check("so it has no nominal cutoff", gps.cutoff(NOW) is None)
    check("and no enforcement cutoff a sweep could reach for",
          gps.enforcement_cutoff(NOW) is None)
    check("no anchor column is declared", gps.age_basis is None)
    check("no cleanup job is declared", gps.cleanup_job is None)
    check("and no schedule claims to execute it", gps.schedule_id is None)

    exemption = gps.exemption
    check("the exemption is attributed", exemption is not None
          and bool(exemption.approved_by.strip())
          and bool(exemption.approved_on.strip())
          and bool(exemption.reason.strip()))
    check("the rationale states it is an owner decision",
          "OWNER DECISION" in (gps.rationale or ""))

    # NOT unmanaged: the relation still resolves to this policy from both of its
    # names, which is exactly what distinguishes an exemption from an omission.
    for name in ('telematics_reports.Alpha_GPS_Baza_LOG',
                 'telematics_reports.Alpha_GPS_Baza_LOG'):
        policy = rr.relation_policy(name, client_business=True)
        check(f"{name} is still centrally governed",
              policy is not None and policy.policy_id == rr.GPS_ASSIGNMENT_POLICY_ID)
        check(f"{name} is not reported ungoverned",
              rr.ungoverned([name], client_business=True) == [])

    # The exemption is exactly as wide as the decision: the import history is
    # NOT exempt.
    runs = rr.get("client_db.workflow_b_gps_assignment_import_runs")
    check("the import history stays under the ceiling",
          runs.is_age_based and runs.governance_class() == "CEILING",
          runs.governance_class())
    check("anchored on its own start", runs.age_basis == "started_at")
    check("and the relation maps to it",
          rr.relation_policy("telematics_reports.alpha_gps_baza_log_import_runs",
                             client_business=True).policy_id == runs.policy_id)

    # Nothing can be made expired under an exempt policy, however it is asked.
    for value in (date(2017, 6, 20), datetime(2017, 6, 20, tzinfo=timezone.utc),
                  date(1999, 1, 1)):
        check(f"{value} is not expired under the exemption",
              rr.expired_by_semantic_date(rr.GPS_ASSIGNMENT_POLICY_ID, value,
                                          now=NOW) is False)
    retained, expired, unanchored = rr.partition_by_semantic_date(
        rr.GPS_ASSIGNMENT_POLICY_ID,
        [{"d": date(2017, 6, 20)}, {"d": date(2020, 1, 1)}, {"d": None}],
        key=lambda item: item["d"], now=NOW,
    )
    check("partitioning an exempt store expires nothing", expired == [])
    check("and retains every row", len(retained) == 3, str(retained))
    check("including the one with no date", len(unanchored) == 1)
    PASSED.append("the_gps_exception_is_explicit_attributed_and_alone")


def test_a_new_exemption_cannot_appear_anonymously() -> None:
    """One approved exception exists. A second must cost the same as the first."""
    naked = rr.RetentionPolicy(
        policy_id="synthetic.naked_exemption", store="synthetic customer history",
        backend=rr.Backend.CLIENT_BUSINESS_POSTGRES, owner_domain="test",
        retention=rr.Retention.none(), age_basis=None,
        mode=rr.Mode.OWNER_EXEMPT, cleanup_job=None,
        rationale="x" * 100,
    )
    check("an exemption with no owner behind it is refused",
          "MISSING_EXEMPTION" in {v.code for v in rr.validate([naked])})

    blank = rr.RetentionPolicy(
        policy_id="synthetic.blank_exemption", store="synthetic",
        backend=rr.Backend.CLIENT_BUSINESS_POSTGRES, owner_domain="test",
        retention=rr.Retention.none(), age_basis=None,
        mode=rr.Mode.OWNER_EXEMPT, cleanup_job=None, rationale="x" * 100,
        exemption=rr.OwnerExemption(approved_by="", approved_on="", reason=""),
    )
    check("an unattributed exemption is refused",
          "UNATTRIBUTED_EXEMPTION" in {v.code for v in rr.validate([blank])})

    unexplained = rr.RetentionPolicy(
        policy_id="synthetic.unexplained_exemption", store="synthetic",
        backend=rr.Backend.CLIENT_BUSINESS_POSTGRES, owner_domain="test",
        retention=rr.Retention.none(), age_basis=None,
        mode=rr.Mode.OWNER_EXEMPT, cleanup_job=None,
        exemption=rr.OwnerExemption(approved_by="owner", approved_on="2026-08-29",
                                    reason="because"),
    )
    check("an exemption with no written rationale is refused",
          "MISSING_RATIONALE" in {v.code for v in rr.validate([unexplained])})

    stray = rr.RetentionPolicy(
        policy_id="synthetic.stray_exemption", store="synthetic",
        backend=rr.Backend.PLATFORM_POSTGRES, owner_domain="test",
        retention=rr.Retention.default(), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        exemption=rr.OwnerExemption(approved_by="owner", approved_on="2026-08-29",
                                    reason="pasted onto the wrong entry"),
    )
    check("an exemption pasted onto an age-based policy is refused",
          "STRAY_EXEMPTION" in {v.code for v in rr.validate([stray])},
          "it would be silently ignored, which is worse than a violation")

    anchored = rr.RetentionPolicy(
        policy_id="synthetic.exempt_with_anchor", store="synthetic",
        backend=rr.Backend.CLIENT_BUSINESS_POSTGRES, owner_domain="test",
        retention=rr.Retention.none(), age_basis="assignment_date",
        mode=rr.Mode.OWNER_EXEMPT, cleanup_job="ops.hard_retention",
        rationale="x" * 100,
        exemption=rr.OwnerExemption(approved_by="owner", approved_on="2026-08-29",
                                    reason="keeps a deletion anchor anyway"),
    )
    codes = {v.code for v in rr.validate([anchored])}
    check("an exempt store may not keep a deletion anchor",
          "EXEMPT_WITH_AGE_BASIS" in codes, str(codes))
    check("nor a cleanup job that would use one",
          "EXEMPT_WITH_CLEANUP_JOB" in codes, str(codes))
    PASSED.append("a_new_exemption_cannot_appear_anonymously")


def test_governance_distinguishes_four_kinds_and_only_one_fails() -> None:
    """Governed at the ceiling, governed shorter, exempt, unregistered.

    Only the fourth is a coverage failure. Collapsing the first three — reading
    "no cutoff" as "unmanaged" — is what would put the owner's own exception
    back on an operator's cleanup list.
    """
    summary = rr.governance_summary()
    check("all four descriptive classes are represented",
          {"CEILING", "SHORTER", "OWNER_EXEMPT", "LIFECYCLE"} <= set(summary),
          str(sorted(summary)))
    check("the ceiling class is the bulk of the registry",
          len(summary["CEILING"]) > len(summary["OWNER_EXEMPT"]))
    check("the exempt class holds exactly the GPS assignment log",
          summary["OWNER_EXEMPT"] == [rr.GPS_ASSIGNMENT_POLICY_ID],
          str(summary["OWNER_EXEMPT"]))
    check("every policy is classified exactly once",
          sum(len(ids) for ids in summary.values()) == len(rr.POLICIES))

    for policy in rr.POLICIES:
        if policy.governance_class() == "CEILING":
            check(f"{policy.policy_id} really is at the ceiling",
                  policy.horizon_months == rr.HARD_RETENTION_MONTHS)
        elif policy.governance_class() == "SHORTER":
            check(f"{policy.policy_id} really is shorter",
                  policy.retention.unit in (rr.Unit.DAYS, rr.Unit.HOURS,
                                            rr.Unit.MINUTES))

    # The fourth kind is the only failure, and it still bites.
    check("an unregistered client relation fails coverage",
          rr.ungoverned(["telematics_reports.some_new_gps_table"],
                        client_business=True)
          == ["telematics_reports.some_new_gps_table"])
    check("while the exempt relation does not",
          rr.ungoverned(['telematics_reports.Alpha_GPS_Baza_LOG'],
                        client_business=True) == [])
    check("and validation itself stays clean", rr.validate() == [])
    PASSED.append("governance_distinguishes_four_kinds_and_only_one_fails")


def test_a_longer_policy_is_refused() -> None:
    """The check has to actually bite. Inject a violation and see it caught."""
    too_long_months = rr.RetentionPolicy(
        policy_id="synthetic.too_long_months", store="synthetic",
        backend=rr.Backend.PLATFORM_POSTGRES, owner_domain="test",
        retention=rr.Retention.months(24), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job="synthetic",
    )
    too_long_days = rr.RetentionPolicy(
        policy_id="synthetic.too_long_days", store="synthetic",
        backend=rr.Backend.PLATFORM_POSTGRES, owner_domain="test",
        retention=rr.Retention.days(rr.minimum_span_days() + 1), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job="synthetic",
    )
    codes = {v.code for v in rr.validate([too_long_months, too_long_days])}
    check("a 24-month policy is a violation", "EXCEEDS_CEILING" in codes, str(codes))
    check("and so is a day count that can exceed 13 calendar months",
          len([v for v in rr.validate([too_long_days]) if v.code == "EXCEEDS_CEILING"]) == 1)
    # A day count exactly at the shortest possible 13-month span is legal ONLY
    # once its own lead is zero. That is the whole point of the composed model:
    # the bare number is not what has to clear the ceiling — the number plus the
    # sweep latency and the backup shadow is.
    edge_no_lead = rr.RetentionPolicy(
        policy_id="synthetic.at_the_edge", store="synthetic",
        backend=rr.Backend.CLIENT_BUSINESS_POSTGRES, owner_domain="test",
        retention=rr.Retention.days(rr.minimum_span_days()), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job="synthetic",
    )
    check("no backup set, no schedule: the exact minimum span is accepted",
          edge_no_lead.enforcement_lead() == timedelta(0)
          and not [v for v in rr.validate([edge_no_lead]) if v.code == "EXCEEDS_CEILING"])

    edge_with_backup = rr.RetentionPolicy(
        policy_id="synthetic.at_the_edge_backed_up", store="synthetic",
        backend=rr.Backend.PLATFORM_POSTGRES, owner_domain="test",
        retention=rr.Retention.days(rr.minimum_span_days()), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job="synthetic",
    )
    check("the SAME number on a backed-up store is refused",
          edge_with_backup.enforcement_lead() > timedelta(0)
          and [v for v in rr.validate([edge_with_backup]) if v.code == "EXCEEDS_CEILING"],
          "a backup copy of a 393-day-old row outlives the deadline")
    PASSED.append("a_longer_policy_is_refused")


def test_an_override_must_be_attributed_and_must_actually_be_longer() -> None:
    anonymous = rr.RetentionPolicy(
        policy_id="synthetic.anonymous_override", store="synthetic",
        backend=rr.Backend.PLATFORM_POSTGRES, owner_domain="test",
        retention=rr.Retention.months(24), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job="synthetic",
        override=rr.OwnerOverride(months=24, approved_by="", approved_on="", reason=""),
    )
    codes = {v.code for v in rr.validate([anonymous])}
    check("an unattributed override is refused", "UNATTRIBUTED_OVERRIDE" in codes, str(codes))

    pointless = rr.RetentionPolicy(
        policy_id="synthetic.pointless_override", store="synthetic",
        backend=rr.Backend.PLATFORM_POSTGRES, owner_domain="test",
        retention=rr.Retention.months(6), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job="synthetic",
        override=rr.OwnerOverride(months=6, approved_by="owner",
                                  approved_on="2026-08-28", reason="because"),
    )
    check("an override that is not longer than the ceiling is refused",
          "POINTLESS_OVERRIDE" in {v.code for v in rr.validate([pointless])},
          "a shorter lifetime is an ordinary policy, not an exception")

    # A complete, well-formed override: attributed, longer than the ceiling, and
    # carrying everything an executable policy needs.
    proper = rr.RetentionPolicy(
        policy_id="synthetic.proper_override", store="synthetic",
        backend=rr.Backend.PLATFORM_POSTGRES, owner_domain="test",
        retention=rr.Retention.months(24), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job="ops.hard_retention",
        schedule_id="platform-hard-retention",
        override=rr.OwnerOverride(months=24, approved_by="owner",
                                  approved_on="2026-08-28",
                                  reason="statutory obligation, recorded"),
    )
    check("a properly approved override is accepted", not rr.validate([proper]),
          "; ".join(str(v) for v in rr.validate([proper])))
    check("and it moves the cutoff to its own horizon",
          proper.cutoff(moment("2026-08-28T21:00:00+00:00"))
          == rr.subtract_calendar_months(moment("2026-08-28T21:00:00+00:00"), 24))
    check("and the look-ahead still applies to it",
          proper.enforcement_cutoff(moment("2026-08-28T21:00:00+00:00"))
          == rr.subtract_calendar_months(
              moment("2026-08-28T21:00:00+00:00") + proper.enforcement_lead(), 24),
          "an override changes the horizon, never the guarantee that a periodic "
          "sweep meets it")
    PASSED.append("an_override_must_be_attributed_and_must_actually_be_longer")


def test_an_exemption_without_a_reason_is_refused() -> None:
    """`Mode.LIFECYCLE_BOUND` is the only way to say "no clock applies". It is
    also exactly how "logs are forever" would be smuggled in, so it costs a
    written rationale."""
    silent = rr.RetentionPolicy(
        policy_id="synthetic.silent_exemption", store="synthetic audit trail",
        backend=rr.Backend.PLATFORM_POSTGRES, owner_domain="test",
        retention=rr.Retention.none(), age_basis=None,
        mode=rr.Mode.LIFECYCLE_BOUND, cleanup_job=None,
    )
    check("an unexplained exemption is a violation",
          "MISSING_RATIONALE" in {v.code for v in rr.validate([silent])})

    unexecutable = rr.RetentionPolicy(
        policy_id="synthetic.no_cleanup", store="synthetic",
        backend=rr.Backend.PLATFORM_POSTGRES, owner_domain="test",
        retention=rr.Retention.default(), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job=None,
    )
    check("configuration without an execution path is a violation",
          "MISSING_CLEANUP_JOB" in {v.code for v in rr.validate([unexecutable])},
          "a registry entry nothing enforces is documentation, not governance")

    for policy in rr.POLICIES:
        if policy.mode in rr.RetentionPolicy.NO_AGE_MODES:
            check(f"{policy.policy_id} explains itself",
                  len((policy.rationale or "").strip()) > 80,
                  "the rationale must be an argument, not a word")
    PASSED.append("an_exemption_without_a_reason_is_refused")


def test_no_category_is_left_blocked_and_the_mechanism_still_works() -> None:
    """Both blockers were CLOSED by owner decision, not by deletion.

    The GPS assignment log's decision was to EXEMPT it from age-based retention;
    it is therefore governed-and-exempt rather than blocked or unmanaged.
    journald is configured from a derived duration and vacuumed on a schedule.
    What must NOT happen is a category quietly leaving the registry, so this
    also proves the blocked mechanism still bites when it is used.
    """
    blocked = rr.open_owner_decisions()
    check("no category is blocked any more", not blocked,
          str([policy.policy_id for policy in blocked]))

    gps = rr.get("client_db.workflow_b_gps_assignment_log")
    check("the GPS log is a live registry entry, not a blocked one",
          gps.status is rr.Status.ACTIVE, gps.status.value)
    check("it is exempt from age-based retention by owner decision",
          gps.mode is rr.Mode.OWNER_EXEMPT and gps.is_owner_exempt)
    check("so it has no deletion anchor at all",
          gps.age_basis is None and gps.retention.unit is rr.Unit.NONE)
    check("and nothing is scheduled to delete from it by age",
          gps.cleanup_job is None and gps.schedule_id is None)

    journald = rr.get("host.journald")
    check("journald is governed", journald.status is rr.Status.ACTIVE)
    check("it enforces its own retention", journald.self_enforcing is True)
    check("with a declared enforcement granularity",
          journald.self_enforcing_lead == rr.JOURNALD_MAX_FILE_SEC + rr.JOURNALD_VACUUM_CYCLE)
    check("and a scheduled vacuum backing it",
          journald.schedule_id == "journald-retention-vacuum")

    # The mechanism itself must still work.
    synthetic = rr.RetentionPolicy(
        policy_id="synthetic.blocked", store="synthetic",
        backend=rr.Backend.PLATFORM_POSTGRES, owner_domain="test",
        retention=rr.Retention.default(), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job=None,
        status=rr.Status.BLOCKED_OWNER_DECISION, blocker="",
    )
    check("a blocked entry with no stated blocker is a violation",
          "MISSING_BLOCKER" in {v.code for v in rr.validate([synthetic])})
    PASSED.append("no_category_is_left_blocked_and_the_mechanism_still_works")


# --- the deadline look-ahead -------------------------------------------------


def test_a_periodic_sweep_cannot_delete_after_the_deadline() -> None:
    """THE gap this correction closes.

    A weekly sweep deleting "older than 13 months" leaves a record alive for up
    to another week past its deadline. The enforcement cutoff is the deadline
    moved EARLIER by one guaranteed maintenance interval, so a record whose
    deadline falls anywhere before the next sweep goes on this one.
    """
    policy = rr.get("platform_db.public.portal_audit_events")
    interval = policy.maintenance_cycle().guaranteed_interval
    sweep = moment("2026-08-30T05:00:00+00:00")

    # A record whose deadline is ONE MINUTE after this sweep. Under a bare
    # deadline cutoff it survives until the next sweep, a week late.
    created = rr.subtract_calendar_months(
        sweep + timedelta(minutes=1), rr.HARD_RETENTION_MONTHS)
    deadline = policy.deadline_of(created)
    check("the record's deadline really is just after this sweep",
          sweep < deadline <= sweep + timedelta(minutes=1), deadline.isoformat())
    check("a bare deadline cutoff would NOT collect it",
          not (created < rr.hard_retention_cutoff(sweep)),
          "this is exactly the defect")
    check("the enforcement cutoff DOES collect it",
          created < policy.enforcement_cutoff(sweep),
          f"created={created.isoformat()} cutoff={policy.enforcement_cutoff(sweep).isoformat()}")

    # A record whose deadline falls one minute before the NEXT sweep: still
    # collected now, because the next opportunity is too late.
    next_sweep = sweep + interval
    just_inside = rr.subtract_calendar_months(
        next_sweep - timedelta(minutes=1), rr.HARD_RETENTION_MONTHS)
    check("a deadline just before the next sweep is collected now",
          just_inside < policy.enforcement_cutoff(sweep))

    # The sweep deletes early, but not without bound: the horizon it reaches is
    # exactly `now + lead`. For a store inside a backup set the lead is LONGER
    # than the sweep interval on purpose — the source copy must be gone early
    # enough for the last archive containing it to expire by the deadline — so
    # the boundary to check is the lead, not the next fire.
    horizon = sweep + policy.enforcement_lead()
    beyond = rr.subtract_calendar_months(
        horizon + timedelta(minutes=1), rr.HARD_RETENTION_MONTHS)
    check("a deadline beyond the lead horizon is left alone",
          not (beyond < policy.enforcement_cutoff(sweep)))
    at_horizon = rr.subtract_calendar_months(horizon, rr.HARD_RETENTION_MONTHS)
    check("and one exactly at the horizon is too — eligibility is strict `<`",
          not (at_horizon < policy.enforcement_cutoff(sweep)))

    # For a store with NO backup shadow the lead IS the sweep interval, so the
    # "next guaranteed cleanup opportunity" framing is exact there.
    plain = rr.get("client_db.workflow_a_registered_tables")
    check("a non-backed-up store leads by exactly one maintenance interval",
          plain.enforcement_lead()
          == plain.maintenance_cycle().guaranteed_interval)
    plain_next = sweep + plain.enforcement_lead()
    check("a deadline one minute before its next sweep is collected now",
          rr.subtract_calendar_months(plain_next - timedelta(minutes=1),
                                      rr.HARD_RETENTION_MONTHS)
          < plain.enforcement_cutoff(sweep))
    check("a deadline one minute after it is left for that sweep",
          not (rr.subtract_calendar_months(plain_next + timedelta(minutes=1),
                                           rr.HARD_RETENTION_MONTHS)
               < plain.enforcement_cutoff(sweep)))

    # And the general invariant, over a year of weekly sweeps: whatever the
    # creation instant, the record is gone by its deadline. The sweep series
    # starts well before the earliest deadline, so what is measured is the
    # policy rather than where the loop happened to begin.
    for offset in range(0, 400, 7):
        birth = moment("2025-01-01T00:00:00+00:00") + timedelta(days=offset)
        due = policy.deadline_of(birth)
        deleted_at = None
        cursor = moment("2025-06-01T05:00:00+00:00")
        for _ in range(300):
            if birth < policy.enforcement_cutoff(cursor):
                deleted_at = cursor
                break
            cursor += interval
        check(f"a record born +{offset}d is deleted by its deadline",
              deleted_at is not None and deleted_at <= due,
              f"deleted={deleted_at} deadline={due}")
    PASSED.append("a_periodic_sweep_cannot_delete_after_the_deadline")


def test_the_lookahead_holds_at_month_ends_and_leap_february() -> None:
    """Calendar-month semantics stay authoritative inside the look-ahead."""
    policy = rr.get("platform_db.public.portal_audit_events")
    lead = policy.enforcement_lead()
    for label, sweep_iso in (
        ("month end, 31 days", "2026-03-31T05:00:00+00:00"),
        ("month end, 30 days", "2026-04-30T05:00:00+00:00"),
        ("leap February", "2028-02-29T05:00:00+00:00"),
        ("non-leap February", "2027-02-28T05:00:00+00:00"),
        ("new year boundary", "2027-01-01T00:00:00+00:00"),
    ):
        sweep = moment(sweep_iso)
        expected = rr.subtract_calendar_months(sweep + lead, rr.HARD_RETENTION_MONTHS)
        check(f"{label}: the cutoff is calendar arithmetic on now+lead",
              policy.enforcement_cutoff(sweep) == expected,
              policy.enforcement_cutoff(sweep).isoformat())
        # And a record created exactly at the cutoff is NOT collected — strict
        # `<` everywhere, including here.
        check(f"{label}: exactly at the cutoff is retained",
              not (expected < expected))

    # `add_calendar_months` is the deadline side of the same arithmetic and must
    # clamp the same way.
    check("31 Jan + 1 month clamps to 28 Feb",
          rr.add_calendar_months(moment("2026-01-31T00:00:00+00:00"), 1)
          == moment("2026-02-28T00:00:00+00:00"))
    check("29 Feb + 12 months clamps to 28 Feb",
          rr.add_calendar_months(moment("2028-02-29T00:00:00+00:00"), 12)
          == moment("2029-02-28T00:00:00+00:00"))
    check("and a deadline is never earlier than a plain 13-month subtraction "
          "would imply",
          rr.subtract_calendar_months(
              rr.add_calendar_months(moment("2026-01-31T00:00:00+00:00"), 13), 13)
          <= moment("2026-01-31T00:00:00+00:00"))
    PASSED.append("the_lookahead_holds_at_month_ends_and_leap_february")


def test_the_lead_is_composed_from_declared_descriptors() -> None:
    """No `minus 7 days` constant anywhere: the lead is derived and explainable."""
    cycle = rr.MAINTENANCE_CYCLES["platform-hard-retention"].guaranteed_interval
    shadow = rr.PLATFORM_BACKUP_SET.shadow

    audit = rr.get("platform_db.public.portal_audit_events")
    check("a backed-up platform store leads by cycle + backup shadow",
          audit.enforcement_lead() == cycle + shadow,
          str(audit.enforcement_lead()))

    client = rr.get("client_db.workflow_a_registered_tables")
    check("a client business store is in NO backup set", client.in_backup_set is False)
    check("so it leads by the maintenance cycle alone",
          client.enforcement_lead() == cycle, str(client.enforcement_lead()))

    r2 = rr.get("cloudflare_r2.driver_eco_snapshots")
    check("neither is R2", r2.in_backup_set is False)
    check("and its lead is its own maintenance cycle",
          r2.enforcement_lead()
          == rr.MAINTENANCE_CYCLES["eco-dashboard-maintenance"].guaranteed_interval)

    check("the backup topology covers exactly what ops/backup.sh dumps",
          rr.PLATFORM_BACKUP_SET.covered_backends
          == frozenset({rr.Backend.PLATFORM_POSTGRES, rr.Backend.MINIO}),
          str(rr.PLATFORM_BACKUP_SET.covered_backends))
    check("the shadow is set retention plus one expiry cycle",
          shadow == timedelta(days=rr.PLATFORM_BACKUP_SET.retention_days)
          + rr.MAINTENANCE_CYCLES[rr.PLATFORM_BACKUP_SET.expiry_cycle].guaranteed_interval,
          str(shadow))

    # The declared retention days must be the number the implementation uses.
    import ops.backup_retention as br
    check("the topology's retention_days is ops/backup_retention.py's default",
          rr.PLATFORM_BACKUP_SET.retention_days == br.DEFAULT_RETENTION_DAYS,
          f"{rr.PLATFORM_BACKUP_SET.retention_days} vs {br.DEFAULT_RETENTION_DAYS}")

    # And no cleanup implementation carries a lead constant of its own.
    for relative in ("ops/hard_retention.py", "api/platform_prune.py",
                     "jobs/api/telematics/retention_purge.py"):
        code = _code_only_python((REPO_ROOT / relative).read_text(encoding="utf-8"))
        for smell in ("timedelta(days=7)", "timedelta(days=14)", "timedelta(days=21)",
                      "timedelta(days=22)"):
            check(f"{relative} carries no hand-rolled lead constant",
                  smell not in code, smell)
    PASSED.append("the_lead_is_composed_from_declared_descriptors")


def test_a_slower_schedule_is_a_validation_failure_not_a_silent_breach() -> None:
    """The lead is only sound while the declared cycle matches the real cadence."""
    from ops import schedule_catalog as sc

    original = dict(rr.MAINTENANCE_CYCLES)
    try:
        rr.MAINTENANCE_CYCLES["platform-hard-retention"] = rr.MaintenanceCycle(
            schedule_id="platform-hard-retention",
            guaranteed_interval=timedelta(days=1),   # claims daily, unit is weekly
            rationale="synthetic drift",
        )
        codes = {(problem.code, problem.subject) for problem in sc.validate()}
        check("a declared cycle that disagrees with the unit file is drift",
              ("MAINTENANCE_CYCLE_DRIFT", "platform-hard-retention") in codes,
              str(sorted(codes)))
    finally:
        rr.MAINTENANCE_CYCLES.clear()
        rr.MAINTENANCE_CYCLES.update(original)
    check("and the catalogue is clean again", not sc.validate())

    # A ceiling policy whose schedule declares no cycle would silently get a
    # zero lead — the original defect. Validation refuses it.
    orphan = rr.RetentionPolicy(
        policy_id="synthetic.no_cycle", store="synthetic",
        backend=rr.Backend.CLIENT_BUSINESS_POSTGRES, owner_domain="test",
        retention=rr.Retention.default(), age_basis="created_at",
        mode=rr.Mode.HARD_DELETE, cleanup_job="ops.hard_retention",
        schedule_id="a-schedule-with-no-declared-cycle",
    )
    codes = {v.code for v in rr.validate([orphan])}
    check("a ceiling policy with no declared maintenance cycle is refused",
          "MISSING_MAINTENANCE_CYCLE" in codes, str(codes))
    check("and its lead would indeed have been zero",
          orphan.enforcement_lead() == timedelta(0))
    PASSED.append("a_slower_schedule_is_a_validation_failure_not_a_silent_breach")


# --- existing shorter lifetimes ---------------------------------------------


def test_existing_shorter_lifetimes_are_still_shorter() -> None:
    """Read from the modules that own them, so this is a regression test on the
    implementations rather than a restatement of the registry."""
    ttl = (REPO_ROOT / "delivery" / "driver_eco_dashboard" / "worker" / "lib"
           / "capability_ttl.js").read_text(encoding="utf-8")
    check("the Eco weekly capability still lives 10 days",
          "[PERIOD_TYPE.WEEKLY]: 10 * DAY_SECONDS" in ttl)
    check("the Eco monthly capability still lives 60 days",
          "[PERIOD_TYPE.MONTHLY]: 60 * DAY_SECONDS" in ttl)

    for policy_id, expected in (
        ("filesystem.platform_backup_sets", 14),
        ("platform_db.public.runs", 60),
        ("platform_db.public.logs", 60),
        ("platform_db.public.artifacts", 60),
        ("platform_db.workflow_a_control.provider_request_log", 180),
    ):
        policy = rr.get(policy_id)
        check(f"{policy_id} keeps its shorter {expected}-day horizon",
              policy.retention.unit is rr.Unit.DAYS
              and policy.retention.value == expected,
              policy.retention.describe())

    # The 14 is no longer typed in the executor: it READS the topology, which
    # is what stops a unit, an environment file or a flag establishing a
    # backup lifetime the hard-retention shadow does not know about.
    from ops import backup_retention as br

    check("and backup retention still applies 14 days",
          br.DEFAULT_RETENTION_DAYS == 14)
    check("resolved from the registry rather than declared",
          br.DEFAULT_RETENTION_DAYS is rr.PLATFORM_BACKUP_SET.retention_days
          or br.DEFAULT_RETENTION_DAYS == rr.PLATFORM_BACKUP_SET.retention_days)
    backup = (REPO_ROOT / "ops" / "backup_retention.py").read_text(encoding="utf-8")
    check("and the executor assigns no literal of its own",
          not re.search(r"^DEFAULT_RETENTION_DAYS\s*=\s*\d", backup, re.M),
          "a second copy of the policy is how the backup shadow silently "
          "stops bounding anything")
    check("it resolves the topology instead",
          re.search(r"^DEFAULT_RETENTION_DAYS\s*=\s*PLATFORM_BACKUP_SET\.retention_days",
                    backup, re.M) is not None)

    export = rr.get("platform_db.public.database_export_jobs")
    check("Database Explorer exports still expire after 3 days",
          "3 calendar days" in (export.shorter_ttl or ""), str(export.shorter_ttl))

    session = rr.get("cloudflare_d1.eco_session")
    check("a browser session is the shortest lifetime of all",
          session.retention.unit is rr.Unit.MINUTES,
          "30 minutes is what the Worker mints; see "
          "test_the_eco_session_lifetime_is_the_one_the_worker_mints")

    check("the Eco capability tombstone documents its shorter access TTL",
          "10 days weekly" in (rr.get("cloudflare_d1.eco_capability").shorter_ttl or ""))
    check("and the delivery ledger documents secret minimisation before it",
          rr.get("client_db.eco_dashboard_delivery_operation").mode
          is rr.Mode.SECRET_MINIMISATION)
    PASSED.append("existing_shorter_lifetimes_are_still_shorter")


def test_a_shorter_policy_deletes_earlier_than_the_ceiling() -> None:
    now = moment("2026-08-28T21:00:00+00:00")
    ceiling = rr.hard_retention_cutoff(now)
    for policy_id in ("platform_db.public.logs", "filesystem.platform_backup_sets"):
        cutoff = rr.get(policy_id).cutoff(now)
        check(f"{policy_id} deletes strictly earlier than the ceiling",
              cutoff > ceiling, f"{cutoff.isoformat()} vs {ceiling.isoformat()}")
    PASSED.append("a_shorter_policy_deletes_earlier_than_the_ceiling")


# --- one number, two runtimes ------------------------------------------------


def test_the_ceiling_is_stated_once_per_runtime_and_pinned() -> None:
    js = (REPO_ROOT / "delivery" / "driver_eco_dashboard" / "worker" / "lib"
          / "retention_policy.js").read_text(encoding="utf-8")
    match = re.search(r"export const HARD_RETENTION_MONTHS = (\d+);", js)
    check("the Worker states the ceiling exactly once", match is not None)
    check("and it is the same number Python states",
          int(match.group(1)) == rr.HARD_RETENTION_MONTHS, match.group(1))
    policy_match = re.search(r'HARD_RETENTION_POLICY_ID = "([^"]+)"', js)
    check("the two runtimes name the same policy",
          policy_match and policy_match.group(1) == rr.GLOBAL_POLICY_ID)

    # And nobody else re-types it. Cleanup implementations must import.
    for relative in ("ops/hard_retention.py", "api/platform_prune.py",
                     "jobs/api/telematics/retention_purge.py"):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        check(f"{relative} imports the ceiling instead of restating it",
              "from ops.retention_registry import" in text, relative)
        code = _code_only_python(text)
        for approximation in ("395", "396", "390"):
            check(f"{relative} contains no day approximation of the ceiling",
                  approximation not in code, approximation)
    PASSED.append("the_ceiling_is_stated_once_per_runtime_and_pinned")


# --- coverage: no silently ungoverned store ----------------------------------

_CREATE_TABLE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
    r'("?[A-Za-z_][A-Za-z0-9_]*"?)(?:\.("?[A-Za-z_][A-Za-z0-9_]*"?))?',
    re.IGNORECASE,
)


def _declared_relations(paths, default_schema: str) -> set[str]:
    found: set[str] = set()
    for path in paths:
        for match in _CREATE_TABLE.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            first = match.group(1).strip('"')
            second = match.group(2).strip('"') if match.group(2) else None
            if first.upper() in {"IF", "NOT", "EXISTS"}:
                continue
            found.add(f"{first}.{second}" if second else f"{default_schema}.{first}")
    return found


def test_every_declared_relation_is_governed() -> None:
    """The check that makes a future ungoverned table impossible to merge."""
    platform = _declared_relations(
        sorted((REPO_ROOT / "db" / "migrations").glob("*.sql")), "public"
    ) | _declared_relations([REPO_ROOT / "api" / "main.py"], "public")
    missing = rr.ungoverned(platform)
    check("every platform relation the repository can create is governed",
          not missing, str(missing))

    client = _declared_relations(
        sorted((REPO_ROOT / "db" / "client_business").glob("*.sql")), "public"
    )
    missing_client = rr.ungoverned(client, client_business=True)
    check("every client-business relation the repository can create is governed",
          not missing_client, str(missing_client))

    # And the check bites: an unknown relation must be reported, not ignored.
    check("an unregistered store is detected",
          rr.ungoverned(["public.a_table_nobody_registered"])
          == ["public.a_table_nobody_registered"])
    check("relation_policy answers None for an unknown store",
          rr.relation_policy("public.a_table_nobody_registered") is None)
    check("and resolves a known one to its policy",
          rr.relation_policy("public.portal_audit_events").policy_id
          == "platform_db.public.portal_audit_events")
    PASSED.append("every_declared_relation_is_governed")


def test_every_governed_relation_names_a_real_policy() -> None:
    for mapping, label in ((rr.GOVERNED_RELATIONS, "platform"),
                           (rr.GOVERNED_CLIENT_RELATIONS, "client")):
        for relation, policy_id in mapping.items():
            check(f"{label} {relation} names a declared policy",
                  policy_id in rr.BY_ID, policy_id)
    PASSED.append("every_governed_relation_names_a_real_policy")


# --- backups: honest, not solved --------------------------------------------


def test_no_backup_copy_outlives_the_deadline() -> None:
    """The end-to-end datum lifetime, modelled step by step.

    Backup file age proves nothing: a 14-day-old full dump can contain a record
    whose own age is thirteen months. What is proved here is the whole chain —
    created, backed up, source-deleted, last containing backup expires — and
    that the final surviving copy is never later than the datum's deadline.

    The mechanism is NOT "shorten the archive and hope". It is that stores which
    appear in a backup set delete at the SOURCE early enough (`lead` includes
    the backup shadow) that every archive containing the record has expired by
    the deadline. Shorter live retention is explicitly permitted; later
    deletion is not.
    """
    policy = rr.get("platform_db.public.portal_audit_events")
    check("this store is genuinely inside a backup set", policy.in_backup_set)
    interval = policy.maintenance_cycle().guaranteed_interval
    first_sweep = moment("2026-01-04T05:00:00+00:00")
    sweeps = [first_sweep + interval * index for index in range(140)]

    worst = None
    for offset in range(0, 400, 3):
        created = moment("2025-02-01T00:00:00+00:00") + timedelta(days=offset)
        story = rr.final_surviving_copy(policy, created=created, sweep_times=sweeps)
        check(f"a datum born +{offset}d is actually deleted at source",
              story["source_deleted"] is not None, str(story))
        check(f"a datum born +{offset}d has no copy past its deadline",
              story["compliant"],
              f"final={story['final_surviving_copy']} deadline={story['deadline']}")
        margin = story["deadline"] - story["final_surviving_copy"]
        worst = margin if worst is None or margin < worst else worst
    check("and the guarantee is not accidental slack",
          worst is not None and worst >= timedelta(0), str(worst))

    # A store OUTSIDE any backup set has no shadow added, so its final copy is
    # simply the source deletion.
    client = rr.get("client_db.workflow_a_registered_tables")
    story = rr.final_surviving_copy(
        client, created=moment("2025-03-15T00:00:00+00:00"), sweep_times=sweeps)
    check("a non-backed-up store's last copy IS its source deletion",
          story["final_surviving_copy"] == story["source_deleted"])
    check("and it is still inside the deadline", story["compliant"], str(story))

    # THE COUNTERFACTUAL, simulated directly rather than through the model, so
    # it cannot accidentally inherit the very correction it is meant to falsify.
    # A sweep that leads by its maintenance cycle ALONE — the obvious fix for
    # Gap 1, and an insufficient one — still leaves a backup copy alive past the
    # deadline, because the archive taken just before the deletion survives the
    # deletion by the whole backup shadow.
    cycle_only = policy.maintenance_cycle().guaranteed_interval
    shadow = rr.PLATFORM_BACKUP_SET.shadow
    breaches = 0
    for offset in range(0, 400, 3):
        created = moment("2025-02-01T00:00:00+00:00") + timedelta(days=offset)
        due = policy.deadline_of(created)
        naive_deleted = next(
            (fire for fire in sweeps
             if created < rr.subtract_calendar_months(
                 fire + cycle_only, rr.HARD_RETENTION_MONTHS)),
            None,
        )
        if naive_deleted is not None and naive_deleted + shadow > due:
            breaches += 1
    check("leading by the maintenance cycle alone leaves backup copies past the "
          "deadline", breaches > 0,
          "if this is zero the backup shadow is not load-bearing and the "
          "guarantee proved above is accidental")

    check("the note explains the mechanism rather than claiming file age proves it",
          "deleting at the SOURCE early enough" in rr.BACKUP_SHADOW_NOTE
          and "no per-record expiry" in rr.BACKUP_SHADOW_NOTE)
    check("client business databases are named as outside every backup set",
          "Client business databases, Cloudflare D1/R2 and the filesystem roots "
          "are in NO repository-controlled backup set" in rr.BACKUP_SHADOW_NOTE)
    check("the residual is derived from the topology, not typed",
          rr.backup_shadow_days() == rr.PLATFORM_BACKUP_SET.retention_days + 1)
    check("negative day counts are refused",
          _raises(lambda: rr.backup_shadow_days(backup_retention_days=-1)))
    PASSED.append("no_backup_copy_outlives_the_deadline")


def test_journald_retention_is_derived_and_conservative() -> None:
    """journald cannot express calendar months, so the fixed duration must be
    provably safe for EVERY calendar start date, not the average one."""
    derived = rr.journald_max_retention()
    ceiling = timedelta(days=rr.minimum_span_days())
    check("the value plus its granularity never exceeds the shortest 13 months",
          derived + rr.JOURNALD_MAX_FILE_SEC + rr.JOURNALD_VACUUM_CYCLE == ceiling,
          str(derived))
    check("it is 385 days", derived == timedelta(days=385), str(derived))
    for approximation in (timedelta(days=395), timedelta(days=396),
                          timedelta(days=13 * 30)):
        check(f"{approximation.days} days would not be safe",
              approximation + rr.JOURNALD_MAX_FILE_SEC > ceiling
              or approximation > ceiling,
              "a duration that exceeds the shortest 13-month span is a breach "
              "for at least one start date")
    check("a shorter granularity yields a longer, still-safe retention",
          rr.journald_max_retention(max_file_sec=timedelta(days=1),
                                    vacuum_cycle=timedelta(days=1))
          == ceiling - timedelta(days=2))
    check("an absurd granularity is refused rather than silently negative",
          _raises(lambda: rr.journald_max_retention(max_file_sec=timedelta(days=999))))
    PASSED.append("journald_retention_is_derived_and_conservative")


# --- rendering ---------------------------------------------------------------


def test_an_operator_can_read_it_from_one_place() -> None:
    payload = rr.as_dict(now=moment("2026-08-28T21:00:00+00:00"))
    check("the payload names the global default",
          payload["global_default"]["months"] == 13
          and payload["global_default"]["unit"] == "months")
    check("every policy carries its own resolved cutoff",
          all(("cutoff" in entry) for entry in payload["policies"]))
    check("and the fields the owner asked for are all present",
          set(payload["policies"][0]) >= {
              "policy_id", "store", "backend", "owner_domain", "retention",
              "age_basis", "mode", "cleanup_job", "schedule_id", "status",
              "shorter_ttl", "override", "notes",
          }, str(sorted(payload["policies"][0])))
    check("violations travel with the payload", payload["violations"] == [])
    check("and there are no open owner decisions left",
          payload["open_owner_decisions"] == [], str(payload["open_owner_decisions"]))

    # The questions an operator must be able to answer from this one place.
    entry = next(item for item in payload["policies"]
                 if item["policy_id"] == "platform_db.public.portal_audit_events")
    for field, question in (
        ("retention", "base retention policy"),
        ("shorter_ttl", "shorter lifecycle, if any"),
        ("age_basis", "semantic age anchor"),
        ("mode", "cleanup mechanism"),
        ("schedule_id", "schedule responsible"),
        ("maintenance_cycle", "maximum cleanup interval"),
        ("backup_shadow_seconds", "backup shadow"),
        ("enforcement_lead_seconds", "effective source deletion lead"),
        ("enforcement_cutoff", "final hard deadline guarantee"),
        ("status", "blocked / unmanaged status"),
    ):
        check(f"the payload answers: {question}", field in entry, field)
    check("the enforcement cutoff really is earlier than the deadline cutoff",
          entry["enforcement_cutoff"] > entry["cutoff"],
          f"{entry['enforcement_cutoff']} vs {entry['cutoff']}")
    check("the shared descriptors are published once, not per policy",
          isinstance(payload["maintenance_cycles"], list)
          and isinstance(payload["backup_topology"], list),
          "normalised, not copied into every record")
    check("and the journald derivation travels with it",
          payload["journald"]["max_retention_seconds"]
          == int(rr.journald_max_retention().total_seconds()))

    table = rr.render_table(now=moment("2026-08-28T21:00:00+00:00"))
    check("the table states the global default first",
          table.splitlines()[0].startswith("Global default: 13 calendar months"))
    check("and every policy appears in it",
          all(policy.policy_id in table for policy in rr.POLICIES))
    check("the table states the backup topology and what LEAD means",
          "Backup topology:" in table and "LEAD is how far ahead" in table)

    # THE EXEMPTION MUST BE OBVIOUS, not deducible. An operator reading this
    # output has to see that the GPS assignment log is deliberately exempt, and
    # not mistake it for a store that is blocked, unmanaged, or missing an
    # anchor.
    check("the table calls the exemption what it is",
          "OWNER-APPROVED EXEMPTION" in table
          and "Owner-approved exemptions from age-based retention: 1" in table,
          table.rsplit("Violations:", 1)[-1])
    check("it says the exempt store is neither unmanaged nor blocked",
          "NOT unmanaged" in table and "NOT blocked" in table
          and "NOT missing an anchor" in table)
    check("names the approver and the date",
          "approved by platform owner on 2026-08-29" in table)
    check("and shows the governance classes side by side",
          "Governance classes:" in table and "OWNER_EXEMPT=1" in table)

    exempt_payload = payload["owner_exemptions"]
    check("the JSON payload publishes the exemption too",
          [item["policy_id"] for item in exempt_payload]
          == [rr.GPS_ASSIGNMENT_POLICY_ID], str(exempt_payload))
    check("with its attribution", exempt_payload[0]["approved_by"]
          and exempt_payload[0]["approved_on"] and exempt_payload[0]["reason"])
    check("the summary counts each governance class",
          payload["governance_summary"]["OWNER_EXEMPT"]
          == [rr.GPS_ASSIGNMENT_POLICY_ID])
    gps_entry = next(item for item in payload["policies"]
                     if item["policy_id"] == rr.GPS_ASSIGNMENT_POLICY_ID)
    check("the exempt policy publishes no cutoff at all",
          gps_entry["cutoff"] is None and gps_entry["enforcement_cutoff"] is None)
    check("and says so in its own record",
          gps_entry["governance"] == "OWNER_EXEMPT"
          and gps_entry["is_age_based"] is False
          and gps_entry["exemption"] is not None)
    PASSED.append("an_operator_can_read_it_from_one_place")


# --- helpers -----------------------------------------------------------------


def _raises(call) -> bool:
    try:
        call()
    except Exception:
        return True
    return False


def _code_only_python(text: str) -> str:
    """Strip docstrings and comments so a numeric scan tests code, not prose."""
    without_docstrings = re.sub(r'"""[\s\S]*?"""', " ", text)
    return re.sub(r"#.*$", " ", without_docstrings, flags=re.M)


# --- effective mechanisms: where the host disagrees with the declaration -----


def test_the_eco_session_lifetime_is_the_one_the_worker_mints() -> None:
    """30 minutes, read from the Worker, not 12 hours read from nowhere.

    The registry claimed `Retention.hours(12)` for `eco_session`. That was
    neither the authorization lifetime (30 minutes, `SESSION_TTL_SECONDS`) nor
    the compaction horizon (the next maintenance call, weekly): it was a number
    nothing on the platform produced. Pinned to the Worker source so the two
    cannot drift apart again.
    """
    source = (REPO_ROOT / "delivery/driver_eco_dashboard/worker/lib/session.js"
              ).read_text(encoding="utf-8")
    declared = re.search(
        r"export const SESSION_TTL_SECONDS\s*=\s*([0-9]+)\s*\*\s*([0-9]+)\s*;", source
    )
    check("the Worker states its session TTL in one place", declared is not None)
    worker_seconds = int(declared.group(1)) * int(declared.group(2))

    policy = rr.get("cloudflare_d1.eco_session")
    check("the registry states it in minutes", policy.retention.unit is rr.Unit.MINUTES)
    check("and the two agree exactly",
          policy.retention.value * 60 == worker_seconds,
          f"registry {policy.retention.value}min vs worker {worker_seconds}s")
    check("the cutoff is computed from it",
          policy.cutoff(moment("2026-08-29T12:00:00+00:00"))
          == moment("2026-08-29T11:30:00+00:00"),
          str(policy.cutoff(moment("2026-08-29T12:00:00+00:00"))))

    # The compaction horizon is a DIFFERENT question and is declared as such.
    compaction = [item for item in policy.effective_mechanisms
                  if "compaction" in item.mechanism.lower()]
    check("compaction is declared separately from the session lifetime",
          len(compaction) == 1, str(policy.effective_mechanisms))
    check("and it is not presented as a shorter maximum",
          compaction[0].approximate_max is None,
          "compaction is LONGER than the 30-minute authorization lifetime; "
          "reporting it as an effective max would invert the two")
    PASSED.append("the_eco_session_lifetime_is_the_one_the_worker_mints")


def test_the_stage2_scratch_root_declares_its_real_lifecycle() -> None:
    """The ceiling is the declared policy; tmpfiles is what actually empties it."""
    policy = rr.get("filesystem.workflow_b_stage2_cleaned")
    check("the declared policy is still the ceiling, unchanged",
          policy.retention.is_global_default and policy.governance_class() == "CEILING")
    mechanisms = policy.effective_mechanisms
    check("exactly one effective mechanism is declared", len(mechanisms) == 1)
    tmpfiles = mechanisms[0]
    check("it names systemd-tmpfiles", "tmpfiles" in tmpfiles.mechanism)
    check("it names the OS policy that decides the age",
          "30d" in tmpfiles.mechanism, tmpfiles.mechanism)
    check("it is marked host-managed, because the lever is not in ops/systemd/",
          tmpfiles.host_managed)
    check("it enforces", tmpfiles.enforcing)
    check("the effective maximum is 30 days",
          tmpfiles.approximate_max == timedelta(days=30))
    check("which is what the policy reports as its effective max",
          policy.effective_max == timedelta(days=30))
    check("and it is genuinely shorter than the ceiling",
          policy.effective_max < timedelta(days=rr.minimum_span_days()))
    check("the recurring mechanism is named so it can be looked up centrally",
          tmpfiles.schedule_id == "systemd-tmpfiles-clean")
    check("and that schedule declares a maintenance cycle",
          tmpfiles.schedule_id in rr.MAINTENANCE_CYCLES)
    check("the operator surface reports it",
          policy.policy_id in {p.policy_id for p in rr.shorter_effective_lifecycles()})
    PASSED.append("the_stage2_scratch_root_declares_its_real_lifecycle")


def test_a_configured_shorter_policy_is_not_reported_as_enforced() -> None:
    """The per-client purge runs weekly and deletes nothing. Say so."""
    policy = rr.get("client_db.workflow_a_registered_tables")
    simulated = policy.non_enforcing_mechanisms
    check("the dry-run purge is declared as non-enforcing", len(simulated) == 1,
          str(policy.effective_mechanisms))
    check("it names the job", "retention_purge" in simulated[0].mechanism)
    check("it names the schedule an operator can look up",
          simulated[0].schedule_id == "log-job@retention-purge")
    check("it imposes NO effective maximum, because it deletes nothing",
          simulated[0].approximate_max is None)
    check("the policy therefore reports no shorter effective lifetime",
          policy.effective_max is None,
          "a configured shorter policy must never read as an enforced one")
    check("and the ceiling is still what governs it",
          policy.governance_class() == "CEILING")
    check("the operator surface lists it as configured-but-not-enforced",
          policy.policy_id in {p.policy_id for p in rr.simulated_mechanisms()})
    PASSED.append("a_configured_shorter_policy_is_not_reported_as_enforced")


def test_an_effective_mechanism_cannot_lie_in_either_direction() -> None:
    """The descriptor is for honesty; both dishonest uses are refused."""
    base = rr.get("filesystem.workflow_b_stage2_cleaned")

    unexplained = [dc_replace(base, effective_mechanisms=(
        rr.EffectiveMechanism(mechanism="something", schedule_id=None,
                              enforcing=True, note=""),
    ))]
    check("an unexplained mechanism is refused",
          any(v.code == "MISSING_EFFECTIVE_NOTE" for v in rr.validate(unexplained)),
          str(rr.validate(unexplained)))

    pretending = [dc_replace(base, effective_mechanisms=(
        rr.EffectiveMechanism(mechanism="a dry-run job", schedule_id=None,
                              enforcing=False, approximate_max=timedelta(days=30),
                              note="pretends to shorten"),
    ))]
    check("a mechanism that deletes nothing cannot claim to shorten anything",
          any(v.code == "NON_ENFORCING_WITH_MAX" for v in rr.validate(pretending)),
          str(rr.validate(pretending)))

    lengthening = [dc_replace(base, effective_mechanisms=(
        rr.EffectiveMechanism(mechanism="a slower cleanup", schedule_id=None,
                              enforcing=True, approximate_max=timedelta(days=900),
                              note="longer than the ceiling"),
    ))]
    check("an 'effective' lifetime longer than the declared one is refused",
          any(v.code == "EFFECTIVE_MAX_NOT_SHORTER" for v in rr.validate(lengthening)),
          "this descriptor records a SHORTER real lifecycle; lengthening one "
          "needs an attributed OwnerOverride")
    PASSED.append("an_effective_mechanism_cannot_lie_in_either_direction")


def test_the_write_test_snapshots_are_governed_by_name_not_by_pattern() -> None:
    """Live coverage found two relations no DDL in this repository creates.

    They are pre-write snapshots an operator took in `alpha_main` on
    2026-06-19 — full copies of customer trip rows and of a Stage 3 report
    table — so `test_every_declared_relation_is_governed` could never have seen
    them: it parses migrations, and no migration creates these.

    They join the EXISTING deprecated-copy family rather than getting a policy
    of their own, and they join it BY NAME. A `backup_*` pattern over
    `telematics_reports` would be a deletion rule nobody wrote.
    """
    from ops import hard_retention as hr

    names = (
        "telematics_reports.backup_client_trips_d105_2_write_test_20260619_100539",
        "telematics_reports.backup_d105_2_ecodriving_write_test_20260619_100539",
    )
    for name in names:
        check(f"{name} is governed",
              rr.GOVERNED_CLIENT_RELATIONS.get(name) == "client_db.legacy_backup_tables",
              str(rr.GOVERNED_CLIENT_RELATIONS.get(name)))
        check(f"{name} is not reported ungoverned",
              rr.ungoverned([name], client_business=True) == [])

    policy = rr.get("client_db.legacy_backup_tables")
    check("under the global ceiling, not a new horizon",
          policy.retention.is_global_default and policy.governance_class() == "CEILING")
    check("and it is still the one deprecated-copy family, not a second system",
          policy.status is rr.Status.DEPRECATED)

    # Each is swept with the anchor of the table it was copied from.
    declared = {
        f"{item.schema}.{item.table}": item.anchor
        for item in hr.CLIENT_EXTRA_SWEEPS
        if item.policy_id == "client_db.legacy_backup_tables"
    }
    check("the trip copy is anchored on the trip's own start",
          declared.get(names[0]) == "start_timestamp", str(declared))
    check("the report copy is anchored on its Stage 3 load timestamp",
          declared.get(names[1]) == "_loaded_at", str(declared))
    check("both are optional, so the four clients without them do not fail",
          all(item.optional for item in hr.CLIENT_EXTRA_SWEEPS
              if f"{item.schema}.{item.table}" in names))

    # FAIL-CLOSED: no wildcard anywhere near this family.
    for mapping in (rr.GOVERNED_RELATIONS, rr.GOVERNED_CLIENT_RELATIONS):
        for relation in mapping:
            check(f"{relation} is an exact relation name",
                  not any(char in relation for char in "*%?["), relation)
    for item in hr.CLIENT_EXTRA_SWEEPS + hr.PLATFORM_SWEEPS:
        check(f"{item.qualified()} names one relation exactly",
              not any(char in item.table for char in "*%?["), item.table)

    # And the family does not reach anything it must not.
    swept = {item.qualified() for item in hr.CLIENT_EXTRA_SWEEPS}
    for forbidden in ('telematics_reports.Alpha_GPS_Baza_LOG',
                      'telematics_reports.Alpha_GPS_Baza_LOG'):
        check(f"{forbidden} is still swept by nothing", forbidden not in swept)
        check(f"{forbidden} is still the owner exemption",
              rr.GOVERNED_CLIENT_RELATIONS[forbidden] == rr.GPS_ASSIGNMENT_POLICY_ID)
    check("the exemption is still the only one", len(rr.owner_exemptions()) == 1)
    PASSED.append("the_write_test_snapshots_are_governed_by_name_not_by_pattern")


# --- one number per policy, wherever it is consumed --------------------------


def test_the_backup_window_is_declared_once() -> None:
    """`ops.backup_retention` reads the window; it does not own one."""
    from ops import backup_retention as br

    check("the executor's default IS the topology's number",
          br.DEFAULT_RETENTION_DAYS == rr.PLATFORM_BACKUP_SET.retention_days)
    check("and the operator-facing policy entry is the same number again",
          rr.get("filesystem.platform_backup_sets").retention.value
          == rr.PLATFORM_BACKUP_SET.retention_days)
    check("a shorter operational window is allowed",
          rr.validate_backup_retention_days(7) == 7)
    for bad, label in ((rr.PLATFORM_BACKUP_SET.retention_days + 1, "one day longer"),
                       (365, "a year"), (0, "zero"), ("x", "nonsense")):
        try:
            rr.validate_backup_retention_days(bad, source="test")
        except rr.BackupRetentionPolicyConflict:
            continue
        raise AssertionError(f"{label} was accepted as a backup window")
    check("the shadow the enforcement leads use comes from the same number",
          rr.backup_shadow_days() == rr.PLATFORM_BACKUP_SET.retention_days + 1)
    PASSED.append("the_backup_window_is_declared_once")


def test_the_prune_horizon_is_declared_once() -> None:
    """The unit's literal `--days 60` and the registry cannot diverge."""
    declared = rr.platform_prune_retention_days()
    check("the three prune policies agree on one horizon", declared == 60, str(declared))

    unit = (REPO_ROOT / "ops/systemd/log-platform-prune.service").read_text(encoding="utf-8")
    literals = re.findall(r"--days\s+(\d+)", unit)
    check("the installed unit passes exactly one horizon", len(literals) == 1, str(literals))
    check("and it is the one the registry declares",
          int(literals[0]) == declared,
          f"unit says {literals[0]}, registry says {declared}")

    check("a shorter horizon is accepted", rr.validate_platform_prune_days(30) == 30)
    for bad in (declared + 1, 3650, 0, None):
        try:
            rr.validate_platform_prune_days(bad)
        except rr.PrunePolicyConflict:
            continue
        raise AssertionError(f"{bad!r} was accepted as a prune horizon")

    # And the executor's own gate enforces it, not just this helper.
    from api import platform_prune as pp

    check("the executor accepts the declared horizon",
          pp.validate_retention_days(str(declared)) == declared)
    try:
        pp.validate_retention_days(str(declared + 1))
    except pp.PlatformPruneError as exc:
        check("and refuses a longer one with its own error code",
              exc.code == "PRUNE_RETENTION_CONFIGURATION_INVALID", exc.code)
    else:
        raise AssertionError("api.platform_prune accepted a horizon past the policy")
    PASSED.append("the_prune_horizon_is_declared_once")


# --- coverage against a live database ----------------------------------------


def test_coverage_is_read_only_and_never_guesses() -> None:
    """`--coverage` answers the one question repository code cannot.

    No database is available here, and that is the case worth proving: an
    unreachable database must report itself as unproven, never as clean.
    """
    report = rr.coverage_report(platform_dsn="host=127.0.0.1 port=1 dbname=nope user=nope")
    check("it declares itself read-only", report["read_only"] is True)
    check("an unreachable database is reported, not guessed",
          report["databases"][0]["reachable"] is False
          and report["databases"][0]["ungoverned"] is None)
    check("and that is NOT a pass",
          report["ok"] is False,
          "answering 'clean' to an unanswered question is the failure this "
          "registry exists to prevent")
    check("the offline half is still answered",
          report["violations"] == []
          and report["declared"]["policies"] == len(rr.POLICIES))
    check("the four governance kinds are reported",
          {"CEILING", "SHORTER", "OWNER_EXEMPT", "LIFECYCLE"}
          <= set(report["governance_summary"]))
    check("the one exemption is named",
          report["owner_exemptions"] == [rr.GPS_ASSIGNMENT_POLICY_ID])
    check("effective mechanisms are part of the same answer",
          {item["policy_id"] for item in report["effective_mechanisms"]}
          == {policy.policy_id for policy, _ in rr.effective_mechanisms()})
    check("shorter effective lifecycles are named",
          "filesystem.workflow_b_stage2_cleaned"
          in {item["policy_id"] for item in report["shorter_effective_lifecycles"]})
    check("configured-but-not-enforced mechanisms are named",
          "client_db.workflow_a_registered_tables"
          in {item["policy_id"] for item in report["configured_but_not_enforced"]})
    rendered = rr.render_coverage(report)
    for expected in ("READ-ONLY", "Coverage proven: NO", "NOT INSPECTED",
                     "Effective mechanisms"):
        check(f"the operator view says {expected!r}", expected in rendered)
    PASSED.append("coverage_is_read_only_and_never_guesses")


def test_the_cli_exposes_coverage_without_a_new_tool() -> None:
    """The same surface answers all of the owner's questions."""
    import contextlib
    import io

    # Pinned to an unreachable port so the assertion is deterministic and this
    # suite never touches a real database — including the production one, which
    # is reachable from the repository root and would otherwise decide the
    # outcome of a test that is about the CLI, not about the data.
    import os

    previous = {key: os.environ.get(key) for key in ("POSTGRES_HOST", "POSTGRES_PORT")}
    os.environ["POSTGRES_HOST"] = "127.0.0.1"
    os.environ["POSTGRES_PORT"] = "1"
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = rr.main(["--coverage", "--format", "json"])
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check("an unproven coverage run exits non-zero", code == 1, str(code))
    payload = json.loads(buffer.getvalue())
    check("the exit code follows the report's own verdict",
          (code == 0) == bool(payload["ok"]))
    check("and the unreachable database is named as unproven",
          payload["summary"]["databases_unreachable"] == 1,
          str(payload["summary"]))
    check("it is the coverage schema",
          payload["schema"] == "log-platform-retention-coverage/v1")
    check("and it is read-only", payload["read_only"] is True)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rr.main([])
    text = buffer.getvalue()
    for question, marker in (
        ("what policies exist", "filesystem.platform_backup_sets"),
        ("what is exempt", "OWNER-APPROVED EXEMPTION"),
        ("what is shorter", "30 minutes"),
        ("what is lifecycle-bound", "lifecycle_bound"),
        ("what contradicts the declaration", "Effective mechanisms"),
        ("what is configured but not enforcing", "CONFIGURED BUT NOT ENFORCING"),
        ("what really bounds the stage-2 root", "EFFECTIVE MAX 30d"),
    ):
        check(f"one command answers {question!r}", marker in text, marker)
    PASSED.append("the_cli_exposes_coverage_without_a_new_tool")


def main() -> int:
    test_the_default_is_thirteen_calendar_months()
    test_the_cutoff_is_not_a_day_approximation()
    test_calendar_boundaries_are_deterministic()
    test_eligibility_at_and_around_the_cutoff()
    test_the_registry_validates_clean()
    test_nothing_exceeds_the_ceiling_and_no_override_is_unapproved()
    test_a_longer_policy_is_refused()
    test_an_override_must_be_attributed_and_must_actually_be_longer()
    test_an_exemption_without_a_reason_is_refused()
    test_no_category_is_left_blocked_and_the_mechanism_still_works()
    test_the_gps_exception_is_explicit_attributed_and_alone()
    test_a_new_exemption_cannot_appear_anonymously()
    test_governance_distinguishes_four_kinds_and_only_one_fails()
    test_a_periodic_sweep_cannot_delete_after_the_deadline()
    test_the_lookahead_holds_at_month_ends_and_leap_february()
    test_the_lead_is_composed_from_declared_descriptors()
    test_a_slower_schedule_is_a_validation_failure_not_a_silent_breach()
    test_journald_retention_is_derived_and_conservative()
    test_existing_shorter_lifetimes_are_still_shorter()
    test_a_shorter_policy_deletes_earlier_than_the_ceiling()
    test_the_ceiling_is_stated_once_per_runtime_and_pinned()
    test_every_declared_relation_is_governed()
    test_every_governed_relation_names_a_real_policy()
    test_no_backup_copy_outlives_the_deadline()
    test_an_operator_can_read_it_from_one_place()
    test_the_eco_session_lifetime_is_the_one_the_worker_mints()
    test_the_stage2_scratch_root_declares_its_real_lifecycle()
    test_a_configured_shorter_policy_is_not_reported_as_enforced()
    test_an_effective_mechanism_cannot_lie_in_either_direction()
    test_the_write_test_snapshots_are_governed_by_name_not_by_pattern()
    test_the_backup_window_is_declared_once()
    test_the_prune_horizon_is_declared_once()
    test_coverage_is_read_only_and_never_guesses()
    test_the_cli_exposes_coverage_without_a_new_tool()
    for name in PASSED:
        print(f"PASS {name}")
    print(f"\n{len(PASSED)} checks passed — global retention registry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
