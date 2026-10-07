"""Focused regressions for deferred dispatcher run persistence."""
from __future__ import annotations

import sys
import types
from datetime import datetime, time, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import dispatcher
from ops import runner


class Persistence:
    def __init__(self):
        self.runs = []
        self.logs = []
        self.schedule_history = []
        self.child_runs = []
        self.deletes = 0


class FakeClient:
    def __init__(self, store: Persistence):
        self.store = store

    def start_run(self, trigger, source, actor=None, params=None):
        run_id = f"run-{len(self.store.runs) + 1}"
        self.store.runs.append({
            "run_id": run_id, "trigger": trigger, "source": source,
            "status": "RUNNING", "params": params or {},
        })
        return run_id

    def finish_run(self, run_id, status):
        next(row for row in self.store.runs if row["run_id"] == run_id)["status"] = status

    def log(self, level, type_, source, message, run_id=None, context=None, error=None):
        self.store.logs.append({
            "level": level, "source": source, "message": message,
            "run_id": run_id, "context": context or {}, "error": error,
        })
        return len(self.store.logs)


def run_module(module, store, name="test.job"):
    client = FakeClient(store)
    with patch.object(runner.importlib, "import_module", return_value=module), \
         patch.object(runner.LogPlatformClient, "from_env", return_value=client), \
         patch.object(sys, "argv", ["ops/runner.py", name, "{}"]):
        return runner.main()


def deferred_module(prepare, execute):
    return types.SimpleNamespace(
        DEFERRED_RUN_CREATION=True,
        prepare_run=prepare,
        run_prepared=execute,
        discard_prepared=lambda prepared: None,
        run=lambda client, run_id, params: None,
    )


def assert_silent(store):
    assert store.runs == []
    assert store.logs == []
    assert store.schedule_history == []
    assert store.child_runs == []
    assert store.deletes == 0


def test_runner_protocol():
    # No due schedules and a competing claim loser have the same typed no-op.
    for label in ("no_due", "competing_claim"):
        store = Persistence()
        rc = run_module(deferred_module(lambda params: None, lambda **kwargs: None), store)
        assert rc == 0, label
        assert_silent(store)

    # Claimed work enters the normal lifecycle exactly once.
    store = Persistence()
    prepared = object()
    def execute_success(**kwargs):
        assert kwargs["prepared"] is prepared
        store.schedule_history.append("claimed-once")
        store.child_runs.append("success")
        kwargs["client"].log(
            "INFO", "SCRIPT", "test.job", "claim executed",
            run_id=kwargs["run_id"],
            context={"client_code": "TEST", "dataset_name": "trips_sync",
                     "scheduled_fire_ts": "2026-01-01T00:00:00Z"},
        )
    assert run_module(deferred_module(lambda params: prepared, execute_success), store) == 0
    assert len(store.runs) == 1 and store.runs[0]["status"] == "SUCCESS"
    assert store.runs[0]["trigger"] == "SCHEDULED"
    assert len(store.schedule_history) == 1
    assert len(store.child_runs) == 1
    messages = [row["message"] for row in store.logs]
    assert "Run started" in messages and "Job dispatch" in messages
    assert "Run finished: SUCCESS" in messages and "claim executed" in messages

    # A planning failure is persisted even though preparation precedes run creation.
    store = Persistence()
    def fail_prepare(params):
        raise RuntimeError("schedule query failed")
    try:
        run_module(deferred_module(fail_prepare, lambda **kwargs: None), store)
    except RuntimeError:
        pass
    else:
        raise AssertionError("planning failure must propagate")
    assert len(store.runs) == 1 and store.runs[0]["status"] == "FAILED"
    assert any(row["level"] == "ERROR" for row in store.logs)
    assert store.schedule_history == [] and store.child_runs == []

    # A child failure after claim remains visible and fails the technical run.
    store = Persistence()
    def fail_child(**kwargs):
        store.schedule_history.append("claimed-once")
        store.child_runs.append("failed")
        raise RuntimeError("child failed")
    try:
        run_module(deferred_module(lambda params: object(), fail_child), store)
    except RuntimeError:
        pass
    else:
        raise AssertionError("child failure must propagate")
    assert len(store.runs) == 1 and store.runs[0]["status"] == "FAILED"
    assert len(store.schedule_history) == 1 and len(store.child_runs) == 1
    assert any(row["level"] == "ERROR" for row in store.logs)

    # Generic jobs retain the pre-existing eager lifecycle.
    store = Persistence()
    generic = types.SimpleNamespace(
        run=lambda client, run_id, params: client.log(
            "INFO", "SCRIPT", "test.generic", "generic work", run_id=run_id
        )
    )
    assert run_module(generic, store, "test.generic") == 0
    assert len(store.runs) == 1 and store.runs[0]["status"] == "SUCCESS"
    generic_messages = [row["message"] for row in store.logs]
    assert generic_messages == [
        "Run started", "Job dispatch", "generic work", "Run finished: SUCCESS",
    ]


def schedule_row():
    return dispatcher.ScheduleRow(
        schedule_id="schedule-1", client_id="client-1", client_code="TEST",
        client_name="Test", dataset_name="trips_sync",
        job_module="jobs.api.telematics.sync_trips_and_speeding", enabled=True,
        frequency="daily", day_of_week=None, day_of_month=None,
        day_of_month_last=False, run_time=time(0, 0), timezone_name="UTC",
        lookback_days=1, overwrite_existing=False,
        event_enrichment_mode="enabled",
    )


class FakeConn:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True


def test_dispatcher_atomic_prepare():
    # No schedules due: no claim call and resources are released.
    conn = FakeConn()
    with patch.multiple(
        dispatcher, _platform_pg_conn=lambda: conn,
        _try_acquire_dispatcher_lock=lambda value: True,
        _mark_stale_running=lambda *args, **kwargs: [],
        _count_running=lambda value: 0,
        _load_enabled_schedules=lambda value: [],
        _release_dispatcher_lock=lambda value: None,
    ), patch.object(dispatcher, "_claim_fire", side_effect=AssertionError("claim not expected")):
        assert dispatcher.prepare_run({}) is None
    assert conn.closed

    # All eligible fires already finalized / competing winner: claim returns None.
    conn = FakeConn(); sched = schedule_row(); now = datetime.now(timezone.utc)
    with patch.multiple(
        dispatcher, _platform_pg_conn=lambda: conn,
        _try_acquire_dispatcher_lock=lambda value: True,
        _mark_stale_running=lambda *args, **kwargs: [],
        _count_running=lambda value: 0,
        _load_enabled_schedules=lambda value: [sched],
        select_next_due=lambda **kwargs: [(sched, now, now, now)],
        _claim_fire=lambda *args, **kwargs: None,
        _release_dispatcher_lock=lambda value: None,
    ):
        assert dispatcher.prepare_run({}) is None
    assert conn.closed

    # Successful claim returns the same live connection; no second precheck exists.
    conn = FakeConn()
    with patch.multiple(
        dispatcher, _platform_pg_conn=lambda: conn,
        _try_acquire_dispatcher_lock=lambda value: True,
        _mark_stale_running=lambda *args, **kwargs: [],
        _count_running=lambda value: 0,
        _load_enabled_schedules=lambda value: [sched],
        select_next_due=lambda **kwargs: [(sched, now, now, now)],
        _claim_fire=lambda *args, **kwargs: "history-1",
        _release_dispatcher_lock=lambda value: None,
    ):
        prepared = dispatcher.prepare_run({})
    assert prepared is not None and prepared.conn is conn
    assert prepared.run_history_id == "history-1" and not conn.closed

    # Connection, schedule-query, malformed-row, and selection failures propagate.
    for failing_patch in (
        {"_platform_pg_conn": lambda: (_ for _ in ()).throw(ConnectionError("db"))},
        {"_load_enabled_schedules": lambda value: (_ for _ in ()).throw(RuntimeError("query"))},
        {"select_next_due": lambda **kwargs: (_ for _ in ()).throw(RuntimeError("select"))},
    ):
        conn = FakeConn()
        base = dict(
            _platform_pg_conn=lambda: conn,
            _try_acquire_dispatcher_lock=lambda value: True,
            _mark_stale_running=lambda *args, **kwargs: [],
            _count_running=lambda value: 0,
            _load_enabled_schedules=lambda value: [sched],
            select_next_due=lambda **kwargs: [],
            _release_dispatcher_lock=lambda value: None,
        )
        base.update(failing_patch)
        try:
            with patch.multiple(dispatcher, **base):
                dispatcher.prepare_run({})
        except (ConnectionError, RuntimeError):
            pass
        else:
            raise AssertionError("pre-claim failure must propagate")

    malformed = schedule_row()
    malformed = dispatcher.ScheduleRow(**{
        **malformed.__dict__, "dataset_name": "unknown_dataset",
        "job_module": "jobs.bad",
    })
    conn = FakeConn()
    with patch.multiple(
        dispatcher, _platform_pg_conn=lambda: conn,
        _try_acquire_dispatcher_lock=lambda value: True,
        _mark_stale_running=lambda *args, **kwargs: [],
        _count_running=lambda value: 0,
        _load_enabled_schedules=lambda value: [malformed],
        _release_dispatcher_lock=lambda value: None,
    ):
        try:
            dispatcher.prepare_run({})
        except ValueError:
            pass
        else:
            raise AssertionError("malformed enabled schedule must fail")



def test_real_prepared_execution():
    sched = schedule_row()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for rc, expected_status in ((0, "SUCCESS"), (7, "FAILED")):
        store = Persistence()
        client = FakeClient(store)
        conn = FakeConn()
        prepared = dispatcher.PreparedDispatcherRun(
            conn=conn, schedule=sched, fire_utc=now,
            window_start=now, window_end=now, run_history_id="history-1",
            stale_rows=[], stale_after_minutes=720,
        )
        finalized = []
        def launch(**kwargs):
            store.child_runs.append("invoked")
            # `strict_meta` schedule: no gate, so no terminal record is
            # requested and the M3 outcome gate never runs on this branch.
            return (
                rc, "", "child error" if rc else "", "child-run-id",
                dispatcher.ScheduledExecutionOutcome(requested=False),
            )
        with patch.multiple(
            dispatcher,
            _launch_job=launch,
            _set_platform_run_id=lambda *args, **kwargs: None,
            _finalize_run=lambda *args, **kwargs: finalized.append(kwargs),
            _release_dispatcher_lock=lambda value: None,
        ):
            try:
                dispatcher.run_prepared(client, "dispatcher-run", {}, prepared)
            except RuntimeError:
                assert rc != 0
            else:
                assert rc == 0
        assert len(store.child_runs) == 1
        assert len(finalized) == 1 and finalized[0]["status"] == expected_status
        assert conn.closed and prepared.released
        messages = [row["message"] for row in store.logs]
        assert "Dispatcher tick starting" in messages
        assert any("Dispatching trips_sync" in message for message in messages)
        if rc == 0:
            assert any("Job finished SUCCESS" in message for message in messages)
        else:
            assert any("Job finished FAILED" in message for message in messages)

    # Launch exceptions finalize the claim and remain visible to run_context.
    store = Persistence(); client = FakeClient(store); conn = FakeConn()
    prepared = dispatcher.PreparedDispatcherRun(
        conn=conn, schedule=sched, fire_utc=now, window_start=now,
        window_end=now, run_history_id="history-2", stale_rows=[],
        stale_after_minutes=720,
    )
    finalized = []
    with patch.multiple(
        dispatcher,
        _launch_job=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("launch")),
        _finalize_run=lambda *args, **kwargs: finalized.append(kwargs),
        _release_dispatcher_lock=lambda value: None,
    ):
        try:
            dispatcher.run_prepared(client, "dispatcher-run", {}, prepared)
        except RuntimeError:
            pass
        else:
            raise AssertionError("launch failure must propagate")
    assert len(finalized) == 1 and finalized[0]["status"] == "FAILED"
    assert any(row["level"] == "ERROR" for row in store.logs)


if __name__ == "__main__":
    test_runner_protocol()
    test_dispatcher_atomic_prepare()
    test_real_prepared_execution()
    print("OK - dispatcher silent no-op persistence regressions passed")
