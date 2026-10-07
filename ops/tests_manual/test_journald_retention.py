#!/usr/bin/env python3
"""journald time-retention: the configuration contract, not the live host.

Run:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_journald_retention.py

Pure: reads repository files only. It never inspects, edits or restarts the
running journald — installing the drop-in and enabling the timer are separately
authorized host mutations.

WHAT THIS PROVES

  * the repository owns a deployable journald retention configuration at all,
    which this host previously did not have in any form;
  * its numbers are DERIVED from the registry, not typed, and the file and the
    derivation are pinned to each other;
  * the duration can never exceed 13 calendar months for ANY calendar start
    date — it is built from the SHORTEST span 13 months can have, not an
    average, and the usual approximations are shown to fail that test;
  * rotation is bounded, so an active journal file cannot hold over-age entries
    indefinitely — the failure mode a bare `MaxRetentionSec` leaves open;
  * enforcement is scheduled, so a host quiet enough never to rotate still
    complies;
  * the vacuum unit's duration equals the configured one;
  * the whole thing is visible in retention and schedule governance.
"""
from __future__ import annotations

import re
import sys
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import retention_registry as rr  # noqa: E402
from ops import schedule_catalog as sc  # noqa: E402

PROPOSED = REPO_ROOT / "ops" / "systemd" / "proposed"
CONF = PROPOSED / "journald-retention.conf"
SERVICE = PROPOSED / "journald-retention-vacuum.service"
TIMER = PROPOSED / "journald-retention-vacuum.timer"

PASSED: list[str] = []

_SPAN = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "month": 2629800}


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}" if detail else label)


def parse_span(text: str) -> int:
    total = 0
    for amount, unit in re.findall(r"(\d+)\s*([a-zA-Z]+)", text.strip()):
        factor = _SPAN.get(unit.lower())
        if factor is None:
            raise AssertionError(f"unrecognised systemd time unit: {unit!r}")
        total += int(amount) * factor
    if total == 0:
        raise AssertionError(f"no time span found in {text!r}")
    return total


def conf_value(key: str) -> str:
    for line in CONF.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip()
    raise AssertionError(f"{key} is not set in {CONF.name}")


def test_the_repository_owns_a_deployable_configuration() -> None:
    for path in (CONF, SERVICE, TIMER):
        check(f"{path.name} exists", path.is_file())
    text = CONF.read_text(encoding="utf-8")
    check("it declares the [Journal] section", "[Journal]" in text)
    check("it says where it is installed",
          "/etc/systemd/journald.conf.d/" in text,
          "a config nobody can place is not deployable")
    check("and it says explicitly that installing it is a separate step",
          "NOT performed by the change that added this file" in text)
    check("it does not touch storage or size limits",
          "Storage=" not in text and "SystemMaxUse=" not in text,
          "this file owns TIME retention and nothing else")
    PASSED.append("the_repository_owns_a_deployable_configuration")


def test_the_numbers_are_derived_and_pinned() -> None:
    derived = rr.journald_max_retention()
    configured = parse_span(conf_value("MaxRetentionSec"))
    check("MaxRetentionSec equals the derived duration",
          configured == int(derived.total_seconds()),
          f"{configured}s configured vs {int(derived.total_seconds())}s derived")

    max_file = parse_span(conf_value("MaxFileSec"))
    check("MaxFileSec equals the declared rotation granularity",
          max_file == int(rr.JOURNALD_MAX_FILE_SEC.total_seconds()),
          str(max_file))

    vacuum = re.search(r"--vacuum-time=(\S+)", SERVICE.read_text(encoding="utf-8"))
    check("the vacuum unit states a duration", vacuum is not None)
    check("and it is the SAME duration the drop-in configures",
          parse_span(vacuum.group(1)) == configured,
          f"{vacuum.group(1)} vs {conf_value('MaxRetentionSec')}")
    PASSED.append("the_numbers_are_derived_and_pinned")


def test_the_duration_can_never_exceed_thirteen_calendar_months() -> None:
    """journald takes a fixed duration; 13 calendar months is not one.

    The only safe fixed duration is one that is short enough for the SHORTEST
    13-month span in the calendar, because a longer one is a breach for at
    least one start date. This is what "do not use an approximate fixed
    duration that can exceed 13 calendar months" means concretely.
    """
    shortest = timedelta(days=rr.minimum_span_days())
    configured = timedelta(seconds=parse_span(conf_value("MaxRetentionSec")))
    max_file = timedelta(seconds=parse_span(conf_value("MaxFileSec")))
    vacuum_cycle = rr.MAINTENANCE_CYCLES["journald-retention-vacuum"].guaranteed_interval

    worst_case_entry_age = configured + max_file + vacuum_cycle
    check("the worst-case entry age never exceeds the shortest 13 months",
          worst_case_entry_age <= shortest,
          f"{worst_case_entry_age} vs {shortest}")

    # And it holds for every calendar start date, not just on average.
    from datetime import datetime, timezone
    import calendar as _calendar
    for year in (2024, 2025, 2026, 2027):
        for month in range(1, 13):
            for day in (1, 15, _calendar.monthrange(year, month)[1]):
                born = datetime(year, month, day, 12, tzinfo=timezone.utc)
                deadline = rr.add_calendar_months(born, rr.HARD_RETENTION_MONTHS)
                check(f"an entry written {born.date()} is vacuumed by its deadline",
                      born + worst_case_entry_age <= deadline,
                      f"{born + worst_case_entry_age} vs {deadline}")

    # The approximations the owner ruled out really would fail.
    for approximation in (timedelta(days=395), timedelta(days=396),
                          timedelta(days=13 * 30 + 30)):
        check(f"{approximation.days}d would not be safe",
              approximation + max_file + vacuum_cycle > shortest,
              "an approximation that fits inside the ceiling would make this "
              "test vacuous")
    PASSED.append("the_duration_can_never_exceed_thirteen_calendar_months")


def test_rotation_is_bounded_so_the_active_file_cannot_hide_old_entries() -> None:
    """A bare `MaxRetentionSec` leaves the ACTIVE journal file untouched.

    Vacuuming is file-granular. Without `MaxFileSec`, a low-traffic host can
    keep one file writable for months and every entry in it survives regardless
    of the retention setting — the exact "still allows an active journal file
    containing over-age entries to survive indefinitely" failure.
    """
    max_file = timedelta(seconds=parse_span(conf_value("MaxFileSec")))
    check("MaxFileSec is set at all", max_file > timedelta(0))
    check("and it is short relative to the retention window",
          max_file * 20 < timedelta(seconds=parse_span(conf_value("MaxRetentionSec"))),
          str(max_file))
    check("the file explains why it is there",
          "the active journal file is never vacuumed" in CONF.read_text(encoding="utf-8"))
    PASSED.append("rotation_is_bounded_so_the_active_file_cannot_hide_old_entries")


def test_enforcement_is_scheduled_not_traffic_dependent() -> None:
    """journald applies its own retention on rotation, which log traffic drives.

    A quiet host would therefore stop enforcing. The vacuum timer is what turns
    "the ceiling holds while the machine is busy" into a guarantee.
    """
    timer = TIMER.read_text(encoding="utf-8")
    check("the vacuum runs on a calendar timer", "OnCalendar=" in timer)
    check("and survives a missed fire", "Persistent=true" in timer)
    entry = sc.BY_ID["journald-retention-vacuum"]
    derived = sc.derive_guaranteed_interval(entry)
    declared = rr.MAINTENANCE_CYCLES["journald-retention-vacuum"].guaranteed_interval
    check("the declared cycle matches the timer's real cadence",
          derived == declared, f"{derived} vs {declared}")
    check("it is daily", declared == timedelta(days=1), str(declared))
    check("the service does nothing but vacuum",
          SERVICE.read_text(encoding="utf-8").count("ExecStart=") == 1)
    PASSED.append("enforcement_is_scheduled_not_traffic_dependent")


def test_it_is_represented_in_governance() -> None:
    policy = rr.get("host.journald")
    check("journald is an ACTIVE governed category",
          policy.status is rr.Status.ACTIVE, policy.status.value)
    check("it is marked self-enforcing", policy.self_enforcing is True)
    check("its enforcement granularity is declared",
          policy.self_enforcing_lead
          == rr.JOURNALD_MAX_FILE_SEC + rr.JOURNALD_VACUUM_CYCLE)
    check("its cleanup names both halves",
          "MaxRetentionSec" in (policy.cleanup_job or "")
          and "journald-retention-vacuum" in (policy.cleanup_job or ""))
    check("the schedule catalogue carries the vacuum",
          "journald-retention-vacuum" in sc.BY_ID)
    check("flagged as retention work",
          sc.BY_ID["journald-retention-vacuum"].retention_related is True)
    check("and it claims the journald policy",
          "host.journald" in sc.BY_ID["journald-retention-vacuum"].retention_policies)
    check("the catalogue reports no drift", not sc.validate())
    PASSED.append("it_is_represented_in_governance")


def main() -> int:
    test_the_repository_owns_a_deployable_configuration()
    test_the_numbers_are_derived_and_pinned()
    test_the_duration_can_never_exceed_thirteen_calendar_months()
    test_rotation_is_bounded_so_the_active_file_cannot_hide_old_entries()
    test_enforcement_is_scheduled_not_traffic_dependent()
    test_it_is_represented_in_governance()
    for name in PASSED:
        print(f"PASS {name}")
    print(f"\n{len(PASSED)} checks passed — journald retention contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
