#!/usr/bin/env python3
"""Deterministic tests for the disk-space guard (P0-4). Mocked filesystem values.

The classification tests read no real filesystem and create no incidents:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_disk_space_monitor.py

Deduplication and recovery are properties of persisted state, so they need a
throwaway database. Never point this at logdb:

    DISK_MONITOR_TEST_DSN='postgresql://user:pw@127.0.0.1:5432/disposable' \
        PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_disk_space_monitor.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ops.disk_space_monitor as dm  # noqa: E402

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
GB = 1024 ** 3

THRESHOLDS = dm.Thresholds(
    warning_percent=20.0, critical_percent=10.0,
    warning_free_bytes=60 * GB, critical_free_bytes=25 * GB,
)


def usage(total_gb: float, free_gb: float, path: str = "/") -> dm.Usage:
    total = int(total_gb * GB)
    free = int(free_gb * GB)
    return dm.Usage(path=path, total_bytes=total, used_bytes=total - free, free_bytes=free)


def test_thresholds_classify_warning_critical_and_ok() -> None:
    # Production shape at audit time: 466 GB total, 118 GB free (25.3%).
    # Above the percentage floor, but already under the 60 GB byte floor?  No:
    # 118 GB > 60 GB, so this is healthy and must not alert.
    assert dm.classify(usage(466, 118), THRESHOLDS) == dm.SEVERITY_OK
    # Two more weeks of ~7 GB/night backup growth.
    assert dm.classify(usage(466, 55), THRESHOLDS) == dm.SEVERITY_WARNING
    assert dm.classify(usage(466, 20), THRESHOLDS) == dm.SEVERITY_CRITICAL
    print("PASS: OK / warning / critical classification matches the configured floors")


def test_either_floor_can_trigger_independently() -> None:
    # Percentage healthy (25%), bytes critical: a small disk.
    assert dm.classify(usage(80, 20), THRESHOLDS) == dm.SEVERITY_CRITICAL
    # Bytes healthy (200 GB), percentage critical: a very large disk.
    assert dm.classify(usage(4000, 200), THRESHOLDS) == dm.SEVERITY_CRITICAL
    # Percentage healthy, bytes in the warning band.
    assert dm.classify(usage(400, 58), THRESHOLDS) == dm.SEVERITY_WARNING
    print("PASS: the percentage floor and the byte floor each trigger on their own")


def test_critical_wins_over_warning_and_boundaries_are_inclusive() -> None:
    assert dm.classify(usage(1000, 100), THRESHOLDS) == dm.SEVERITY_CRITICAL  # exactly 10%
    assert dm.classify(usage(1000, 200), THRESHOLDS) == dm.SEVERITY_WARNING   # exactly 20%
    assert dm.classify(usage(1000, 201), THRESHOLDS) == dm.SEVERITY_OK
    assert dm.classify(usage(0, 0), THRESHOLDS) == dm.SEVERITY_CRITICAL       # no division blow-up
    print("PASS: critical dominates warning and thresholds are inclusive")


def test_thresholds_come_from_environment_with_safe_fallbacks() -> None:
    configured = dm.Thresholds.from_env(
        {
            "DISK_MONITOR_WARNING_PERCENT": "30",
            "DISK_MONITOR_CRITICAL_PERCENT": "15",
            "DISK_MONITOR_WARNING_FREE_GB": "80",
            "DISK_MONITOR_CRITICAL_FREE_GB": "40",
        }
    )
    assert configured.warning_percent == 30.0 and configured.critical_percent == 15.0
    assert configured.warning_free_bytes == 80 * GB

    # Malformed and negative values fall back to defaults rather than disabling the guard.
    fallback = dm.Thresholds.from_env(
        {"DISK_MONITOR_WARNING_PERCENT": "not-a-number",
         "DISK_MONITOR_CRITICAL_PERCENT": "-5"}
    )
    assert fallback.warning_percent == dm.DEFAULT_WARNING_PERCENT
    assert fallback.critical_percent == dm.DEFAULT_CRITICAL_PERCENT
    print("PASS: thresholds are configurable and malformed values never disable the guard")


def test_dry_run_scan_reports_without_touching_the_database() -> None:
    """`alert=False` must not open a connection: the report is pure measurement."""
    measured = {"/": usage(466, 20, "/"), "/var/lib/docker": usage(466, 20, "/var/lib/docker")}
    report = dm.scan(
        paths=[Path("/"), Path("/var/lib/docker")], thresholds=THRESHOLDS, now=NOW,
        alert=False, usage_reader=lambda path: measured[str(path)],
    )
    assert report["worst_severity"] == dm.SEVERITY_CRITICAL
    assert report["operator_action_required"] is True
    assert report["alerts"] == [] and report["recovered"] == []
    assert len(report["filesystems"]) == 2

    evidence = report["filesystems"][0]
    for key in ("mountpoint", "free_bytes", "free_percent", "total_bytes",
                "warning_percent", "critical_free_bytes", "severity"):
        assert key in evidence, key
    assert evidence["free_percent"] == 4.29
    print("PASS: a dry-run scan measures and carries full evidence without alerting")


def test_healthy_scan_requires_no_operator_action() -> None:
    report = dm.scan(
        paths=[Path("/")], thresholds=THRESHOLDS, now=NOW, alert=False,
        usage_reader=lambda path: usage(466, 300),
    )
    assert report["worst_severity"] == dm.SEVERITY_OK
    assert report["operator_action_required"] is False
    print("PASS: a healthy filesystem produces no operator action")


def test_default_paths_deduplicate_by_filesystem() -> None:
    """The repo, its backups directory and the docker root usually share one device."""
    paths = dm.default_paths()
    assert paths, "at least one path must always be watched"
    devices = {Path(path).stat().st_dev for path in paths}
    assert len(devices) == len(paths), "one entry per distinct filesystem"
    print("PASS: watched paths are deduplicated to one entry per filesystem")


# ------------------------------------------------------ persistence / dedup

# Mirrors the runs/logs bootstrap in api/main.py SCHEMA_SQL; `logs` is required
# because every incident also writes a durable ERROR row.
PLATFORM_BOOTSTRAP = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS runs (
  run_id UUID PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
  status TEXT NOT NULL, trigger TEXT NOT NULL, source TEXT NOT NULL, actor TEXT,
  params JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS logs (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, level TEXT NOT NULL, type TEXT NOT NULL,
  source TEXT NOT NULL, run_id UUID REFERENCES runs(run_id) ON DELETE SET NULL,
  message TEXT NOT NULL, context JSONB NOT NULL DEFAULT '{}'::jsonb, error TEXT
);
CREATE SCHEMA IF NOT EXISTS ops_control;
"""


def _bootstrap(conn) -> None:
    os.environ.update(
        {
            "SUSPECTED_BUG_ALERT_TO": "ops@example.com",
            "SUSPECTED_BUG_ALERT_FROM": "alerts@example.com",
            "SUSPECTED_BUG_ALERTS_ENABLED": "true",
            "LOG_PLATFORM_TARGET_ENVIRONMENT": "test",
        }
    )
    for sql in (
        PLATFORM_BOOTSTRAP,
        (REPO_ROOT / "db/migrations/052_suspected_bug_incidents_and_email_outbox.sql").read_text(),
        (REPO_ROOT / "db/migrations/059_operational_watchdog_state.sql").read_text(),
    ):
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE suspected_bug_email_outbox, suspected_bug_occurrences, "
            "suspected_bug_incidents RESTART IDENTITY CASCADE"
        )
        cur.execute("DELETE FROM ops_control.watchdog_observation")
        cur.execute("DELETE FROM logs")
    conn.commit()


def _counts(conn) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*)::int AS n FROM suspected_bug_incidents")
        incidents = cur.fetchone()["n"]
        cur.execute("SELECT count(*)::int AS n FROM suspected_bug_email_outbox")
        emails = cur.fetchone()["n"]
    return incidents, emails


def _observation(conn, subject_key: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT verdict, watchdog_name, observation_count, last_alerted_at "
            "FROM ops_control.watchdog_observation WHERE subject_key = %s",
            (subject_key,),
        )
        return cur.fetchone()


def _open_incidents(conn) -> dict[str, tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fingerprint, incident_code, state FROM suspected_bug_incidents"
        )
        return {row["fingerprint"]: (row["incident_code"], row["state"]) for row in cur.fetchall()}


def _states_for(conn, code: str) -> list[str]:
    return [state for incident_code, state in _open_incidents(conn).values()
            if incident_code == code]


def test_recovery_is_scoped_to_one_filesystem(dsn: str) -> None:
    """Codex HIGH 3: `/var/lib/docker` recovering must not close `/`'s incident.

    Both mountpoints share `component = ops.disk_space_monitor`, so resolving by
    component closed every filesystem at once.
    """
    from datetime import timedelta

    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        _bootstrap(conn)
        root, docker = Path("/"), Path("/var/lib/docker")
        critical = {"/": usage(466, 20, "/"), "/var/lib/docker": usage(466, 18, "/var/lib/docker")}

        dm.scan(paths=[root, docker], thresholds=THRESHOLDS, now=NOW, conn=conn,
                usage_reader=lambda path: critical[str(path)])
        assert len(_states_for(conn, dm.INCIDENT_DISK_SPACE_CRITICAL)) == 2
        assert all(state == "open" for state in _states_for(conn, dm.INCIDENT_DISK_SPACE_CRITICAL))

        # Only /var/lib/docker recovers.
        healthy = {**critical, "/var/lib/docker": usage(466, 300, "/var/lib/docker")}
        report = dm.scan(paths=[root, docker], thresholds=THRESHOLDS,
                         now=NOW + timedelta(hours=1), conn=conn,
                         usage_reader=lambda path: healthy[str(path)])
        assert report["recovered"] == ["disk:/var/lib/docker"], report["recovered"]

        states = _open_incidents(conn)
        by_subject = {}
        for fingerprint, (code, state) in states.items():
            by_subject.setdefault(state, []).append(code)
        assert sorted(by_subject.get("open", [])) == [dm.INCIDENT_DISK_SPACE_CRITICAL], (
            f"exactly one incident must remain open, got {by_subject}"
        )
        assert by_subject.get("resolved") == [dm.INCIDENT_DISK_SPACE_CRITICAL]
        assert _observation(conn, "disk:/")["verdict"] == dm.SEVERITY_CRITICAL
        assert _observation(conn, "disk:/var/lib/docker")["verdict"] == "OK"
        print("PASS: one filesystem recovering never resolves another's incident")
    finally:
        conn.close()


def test_warning_critical_lifecycle_is_coherent_per_filesystem(dsn: str) -> None:
    """HEALTHY → WARNING → CRITICAL → WARNING → HEALTHY on one mountpoint."""
    from datetime import timedelta

    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        _bootstrap(conn)
        subject, root = "disk:/", Path("/")

        def step(free_gb: float, hours: int):
            return dm.scan(paths=[root], thresholds=THRESHOLDS,
                           now=NOW + timedelta(hours=hours), conn=conn,
                           usage_reader=lambda path: usage(466, free_gb))

        step(55, 0)   # WARNING
        assert _states_for(conn, dm.INCIDENT_DISK_SPACE_WARNING) == ["open"]
        assert _observation(conn, subject)["verdict"] == dm.SEVERITY_WARNING

        escalated = step(20, 1)   # CRITICAL supersedes the warning
        assert escalated["alerts"][0]["severity"] == dm.SEVERITY_CRITICAL
        assert escalated["alerts"][0]["superseded_incidents"] == 1, escalated["alerts"]
        assert _states_for(conn, dm.INCIDENT_DISK_SPACE_WARNING) == ["resolved"], (
            "an open warning alongside an open critical for one filesystem is incoherent"
        )
        assert _states_for(conn, dm.INCIDENT_DISK_SPACE_CRITICAL) == ["open"]

        deescalated = step(55, 2)  # back to WARNING
        assert deescalated["alerts"][0]["severity"] == dm.SEVERITY_WARNING
        assert _states_for(conn, dm.INCIDENT_DISK_SPACE_CRITICAL) == ["resolved"]
        assert _states_for(conn, dm.INCIDENT_DISK_SPACE_WARNING) == ["open"], (
            "the reopened warning must be open again, not left resolved"
        )

        recovered = step(300, 3)   # HEALTHY
        assert recovered["recovered"] == [subject]
        assert set(_states_for(conn, dm.INCIDENT_DISK_SPACE_WARNING)) == {"resolved"}
        assert set(_states_for(conn, dm.INCIDENT_DISK_SPACE_CRITICAL)) == {"resolved"}
        from ops.execution_watchdog import load_open_fingerprints
        assert load_open_fingerprints(conn, subject) == []
        print("PASS: warning/critical escalation and de-escalation stay coherent per filesystem")
    finally:
        conn.close()


def test_dedup_and_recovery(dsn: str) -> None:
    """A full disk stays full. The operator must hear about it once, not hourly."""
    from datetime import timedelta

    import psycopg
    from psycopg.rows import dict_row

    os.environ.update(
        {
            "SUSPECTED_BUG_ALERT_TO": "ops@example.com",
            "SUSPECTED_BUG_ALERT_FROM": "alerts@example.com",
            "SUSPECTED_BUG_ALERTS_ENABLED": "true",
            "LOG_PLATFORM_TARGET_ENVIRONMENT": "test",
        }
    )

    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        _bootstrap(conn)
        subject = "disk:/"
        critical = lambda path: usage(466, 20)  # noqa: E731 - 4.3% free
        scan_at = lambda when, reader: dm.scan(  # noqa: E731
            paths=[Path("/")], thresholds=THRESHOLDS, now=when, conn=conn, usage_reader=reader,
        )

        first = scan_at(NOW, critical)
        assert first["worst_severity"] == dm.SEVERITY_CRITICAL
        assert first["alerts"] and first["alerts"][0]["email_enqueued"] is True
        assert _counts(conn) == (1, 1)
        row = _observation(conn, subject)
        assert row["verdict"] == dm.SEVERITY_CRITICAL
        # The producing watchdog owns the row, not whichever module holds the
        # shared upsert helper.
        assert row["watchdog_name"] == dm.WATCHDOG_NAME
        assert row["last_alerted_at"] is not None
        print("PASS: a critical filesystem raises one incident and enqueues one email")

        # The hourly timer keeps firing while the disk stays full.
        for hour in range(1, 4):
            repeat = scan_at(NOW + timedelta(hours=hour), critical)
            assert repeat["worst_severity"] == dm.SEVERITY_CRITICAL
            assert repeat["alerts"][0]["email_enqueued"] is False, (
                f"hour {hour} re-emailed inside the cooldown"
            )
        incidents, emails = _counts(conn)
        assert (incidents, emails) == (1, 1), (
            f"3 further critical scans must not storm: {incidents} incidents, {emails} emails"
        )
        assert _observation(conn, subject)["observation_count"] == 4
        print("PASS: repeated critical scans re-observe one subject and never re-email")

        # A warning on a second filesystem is a different subject and its own incident.
        dm.scan(
            paths=[Path("/var/lib/docker")], thresholds=THRESHOLDS,
            now=NOW + timedelta(hours=4), conn=conn,
            usage_reader=lambda path: usage(466, 55, "/var/lib/docker"),
        )
        assert _observation(conn, "disk:/var/lib/docker")["verdict"] == dm.SEVERITY_WARNING
        assert _counts(conn) == (2, 2)
        print("PASS: a second filesystem is tracked independently, not folded into the first")

        # Space is freed: the incident closes and the subject returns to OK.
        recovered = scan_at(NOW + timedelta(hours=5), lambda path: usage(466, 300))
        assert recovered["worst_severity"] == dm.SEVERITY_OK
        assert recovered["alerts"] == []
        assert recovered["recovered"] == [subject]
        assert _observation(conn, subject)["verdict"] == "OK"
        from ops.execution_watchdog import load_open_fingerprints

        assert load_open_fingerprints(conn, subject) == [], "this subject has nothing open"
        # `/var/lib/docker` is still in warning and shares the component. Its
        # incident must survive `/` recovering — resolving by component closed it.
        assert load_open_fingerprints(conn, "disk:/var/lib/docker"), (
            "recovery of / wrongly closed another filesystem's incident"
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int AS n FROM suspected_bug_incidents "
                "WHERE state = 'open' AND component = %s",
                (dm.COMPONENT,),
            )
            assert cur.fetchone()["n"] == 1, "exactly the other filesystem stays open"
        print("PASS: recovery resolves only the recovered filesystem's incident")

        # A relapse after recovery is new news, even inside the original cooldown.
        relapse = scan_at(NOW + timedelta(hours=6), critical)
        assert relapse["alerts"][0]["email_enqueued"] is True, (
            "a resolved incident that reopens must alert again"
        )
        print("PASS: a relapse after recovery alerts again rather than staying silent")
    finally:
        conn.close()


def main() -> None:
    test_thresholds_classify_warning_critical_and_ok()
    test_either_floor_can_trigger_independently()
    test_critical_wins_over_warning_and_boundaries_are_inclusive()
    test_thresholds_come_from_environment_with_safe_fallbacks()
    test_dry_run_scan_reports_without_touching_the_database()
    test_healthy_scan_requires_no_operator_action()
    test_default_paths_deduplicate_by_filesystem()

    dsn = os.environ.get("DISK_MONITOR_TEST_DSN")
    if dsn:
        if "logdb" in dsn:
            raise SystemExit("refusing to run against logdb; use a disposable database")
        test_dedup_and_recovery(dsn)
        test_recovery_is_scoped_to_one_filesystem(dsn)
        test_warning_critical_lifecycle_is_coherent_per_filesystem(dsn)
    else:
        print("SKIP: DISK_MONITOR_TEST_DSN unset - dedup and recovery not run")
    print("OK - disk space monitor tests passed")


if __name__ == "__main__":
    main()
