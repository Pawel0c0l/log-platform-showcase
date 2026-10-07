import os
import json
import pathlib
from dataclasses import dataclass
from typing import Optional, Dict, Any, Literal

import requests

try:
    from api.timezone_utils import business_time_log_context
except ImportError:
    from timezone_utils import business_time_log_context


Level = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogType = Literal["SYSTEM", "SCRIPT"]


@dataclass(frozen=True)
class ArtifactUploadResult:
    artifact_id: str
    idempotency_status: Literal["created", "reused"]


class LogPlatformClient:
    """
    Minimal client for Log Platform API.
    Uses WRITE token for writes, READ token for reads.
    """

    def __init__(
        self,
        base_url: str,
        read_token: Optional[str] = None,
        write_token: Optional[str] = None,
        timeout_s: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.read_token = read_token
        self.write_token = write_token
        self.timeout_s = timeout_s

    @staticmethod
    def from_env() -> "LogPlatformClient":
        base_url = os.getenv("LOG_API_URL", "http://127.0.0.1:8000")
        return LogPlatformClient(
            base_url=base_url,
            read_token=os.getenv("API_READ_TOKEN"),
            write_token=os.getenv("API_WRITE_TOKEN"),
        )

    def _headers(self, mode: Literal["read", "write"]) -> Dict[str, str]:
        token = self.read_token if mode == "read" else self.write_token
        if not token:
            raise RuntimeError(f"Missing {mode} token")
        return {"Authorization": f"Bearer {token}"}

    # ----- Runs -----
    def start_run(
        self,
        trigger: str,
        source: str,
        actor: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> str:
        data = {
            "trigger": trigger,
            "source": source,
            "params_json": json.dumps(params or {}),
        }
        if actor:
            data["actor"] = actor

        r = requests.post(
            f"{self.base_url}/runs",
            headers=self._headers("write"),
            data=data,
            timeout=self.timeout_s,
        )
        r.raise_for_status()
        return r.json()["run_id"]

    def finish_run(self, run_id: str, status: str) -> None:
    	r = requests.patch(
        	f"{self.base_url}/runs/{run_id}",
        	headers={**self._headers("write"), "Content-Type": "application/json"},
        	json={"status": status},
        	timeout=self.timeout_s,
    	)
    	r.raise_for_status()


    # ----- Logs -----
    def log(
        self,
        level: Level,
        type_: LogType,
        source: str,
        message: str,
        run_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> int:
        log_context = dict(context or {})
        for key, value in business_time_log_context().items():
            log_context.setdefault(key, value)
        data = {
            "level": level,
            "type": type_,
            "source": source,
            "message": message,
            "context_json": json.dumps(log_context),
        }
        if run_id:
            data["run_id"] = run_id
        if error:
            data["error"] = error

        r = requests.post(
            f"{self.base_url}/logs",
            headers=self._headers("write"),
            data=data,
            timeout=self.timeout_s,
        )
        r.raise_for_status()
        return int(r.json()["id"])

    # ----- Suspected bugs -----
    def report_suspected_bug(self, event) -> Dict[str, Any]:
        """Report a `suspected_bug` through the durable platform boundary.

        `event` is an `api.suspected_bug.SuspectedBugEvent` (or its dict form).
        The API persists the ERROR log, the incident, the occurrence and the
        alert-email outbox row in one transaction and returns the report result.
        """
        payload = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        r = requests.post(
            f"{self.base_url}/suspected-bugs",
            headers=self._headers("write"),
            data={"event_json": json.dumps(payload, default=str)},
            timeout=self.timeout_s,
        )
        r.raise_for_status()
        return r.json()

    # ----- Artifacts -----
    def upload_artifact(
        self,
        filepath: str,
        kind: str = "REPORT",
        run_id: Optional[str] = None,
        raw_file_id: Optional[str] = None,
        workflow_name: Optional[str] = None,
        stage_name: Optional[str] = None,
        artifact_role: Optional[str] = None,
        report_type: Optional[str] = None,
        client_code: Optional[str] = None,
        original_filename: Optional[str] = None,
        display_filename: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        idempotency_scope: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        structured_response: bool = False,
    ) -> str | ArtifactUploadResult:
        p = pathlib.Path(filepath)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(str(p))

        if (idempotency_scope is None) != (idempotency_key is None):
            raise ValueError("idempotency_scope and idempotency_key must be supplied together")
        if idempotency_scope is not None:
            if not idempotency_scope.strip() or not idempotency_key.strip():
                raise ValueError("idempotency_scope and idempotency_key must not be blank")
            if len(idempotency_scope) > 128 or len(idempotency_key) > 256:
                raise ValueError("idempotency_scope or idempotency_key exceeds maximum length")

        data = {"kind": kind}
        if idempotency_scope is not None:
            data["idempotency_scope"] = idempotency_scope
            data["idempotency_key"] = idempotency_key
        if run_id:
            data["run_id"] = run_id
        if raw_file_id is not None:
            data["raw_file_id"] = raw_file_id
        if workflow_name is not None:
            data["workflow_name"] = workflow_name
        if stage_name is not None:
            data["stage_name"] = stage_name
        if artifact_role is not None:
            data["artifact_role"] = artifact_role
        if report_type is not None:
            data["report_type"] = report_type
        if client_code is not None:
            data["client_code"] = client_code
        if original_filename is not None:
            data["original_filename"] = original_filename
        if display_filename is not None:
            data["display_filename"] = display_filename
        if metadata is not None:
            artifact_metadata = dict(metadata)
            for key, value in business_time_log_context().items():
                artifact_metadata.setdefault(key, value)
            data["metadata_json"] = json.dumps(artifact_metadata)

        attempts = 2 if idempotency_scope is not None else 1
        for attempt in range(attempts):
            try:
                with p.open("rb") as f:
                    files = {"file": (p.name, f, "application/octet-stream")}
                    r = requests.post(
                        f"{self.base_url}/artifacts/upload",
                        headers=self._headers("write"),
                        data=data,
                        files=files,
                        timeout=max(self.timeout_s, 300),
                    )
                break
            except (requests.Timeout, requests.ConnectionError):
                if attempt + 1 >= attempts:
                    raise
        r.raise_for_status()
        payload = r.json()
        result = ArtifactUploadResult(
            artifact_id=payload["artifact_id"],
            idempotency_status=payload.get("idempotency_status", "created"),
        )
        return result if structured_response else result.artifact_id

    def download_artifact(self, artifact_id: str, dest_path: str) -> str:
        """
        Download an artifact through the platform API into dest_path.
        """
        p = pathlib.Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)

        with requests.get(
            f"{self.base_url}/artifacts/{artifact_id}/download",
            headers=self._headers("read"),
            params={"disposition": "attachment"},
            stream=True,
            timeout=max(self.timeout_s, 300),
        ) as r:
            r.raise_for_status()
            with p.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return str(p)


from contextlib import contextmanager
import traceback

#: P1-I. Stable code for "the application outcome is known, but `public.runs`
#: could not be moved off RUNNING". It is deliberately its own classification:
#: the watchdog's STALE verdict answers "is this row still moving?", and this
#: answers "why did it stop moving?". Grepping either alone must not hide the
#: other.
RUN_FINALIZATION_FAILED = "RUN_FINALIZATION_FAILED"


class RunFinalizationError(RuntimeError):
    """P1-I. Terminalizing `public.runs` failed; the application outcome did not.

    Raised only on the success path, and only after the work itself completed.
    It carries `application_succeeded=True` so a caller can tell "the job is
    broken" from "the job worked and the bookkeeping did not" — a distinction
    the previous implementation destroyed by letting a `finish_run` failure
    fall into the generic failure handler and be recorded as FAILED.
    """

    application_succeeded = True

    def __init__(self, run_id: str, cause: BaseException):
        self.run_id = run_id
        self.cause = cause
        super().__init__(
            f"{RUN_FINALIZATION_FAILED}: run {run_id} completed its work but could not be "
            f"marked SUCCESS ({type(cause).__name__}: {cause})"
        )


def _log_quietly(client: LogPlatformClient, *args, **kwargs) -> None:
    """Best-effort logging on a path that is already handling a failure.

    The platform API is the thing that just failed in every caller here, so a
    logging attempt that raises would replace a precise diagnosis with a
    connection error from one frame later.
    """
    try:
        client.log(*args, **kwargs)
    except Exception:
        pass


@contextmanager
def run_context(
    client: LogPlatformClient,
    *,
    trigger: str,
    source: str,
    actor: str | None = None,
    params: dict | None = None,
):
    """Own one `public.runs` row for the lifetime of a job.

    Usage:
      with run_context(c, trigger="SCHEDULED", source="some-job", params={...}) as run_id:
          c.log(...)

    P1-I. Three outcomes, kept strictly separate because conflating them is
    what made the durable record untrustworthy:

      * the body succeeds and the run is marked SUCCESS — the ordinary case;

      * the body raises — an ERROR log carries the traceback and the run is
        marked FAILED. A failure to write FAILED never masks the original
        exception, but it is no longer silent either: it is logged under
        `RUN_FINALIZATION_FAILED`, so a row left RUNNING has a stated cause
        instead of looking like a process that vanished;

      * the body succeeds and *finalization* fails — previously this fell into
        the failure handler, which logged "Run failed", attempted
        FAILED, and re-raised. A run that did all its work was therefore
        durably recorded as a failure, and the watchdog and operator tooling
        had no way to see otherwise. It now raises `RunFinalizationError`,
        which states that the application succeeded, and the row is left
        non-terminal for the watchdog's existing STALE contract to surface
        rather than being overwritten with a false FAILED.

    What this deliberately does *not* do is repair or age rows itself. Aging is
    the watchdog's job (`ops/execution_watchdog.py`, `stale_grace_minutes`), and
    a job process that has just proven it cannot reach the API is the worst
    possible candidate for performing recovery.
    """
    run_id = client.start_run(trigger=trigger, source=source, actor=actor, params=params)
    try:
        client.log("INFO", "SCRIPT", source, "Run started", run_id=run_id, context=params or {})
        yield run_id
    except Exception as e:
        tb = traceback.format_exc()
        _log_quietly(
            client,
            "ERROR",
            "SCRIPT",
            source,
            f"Run failed: {type(e).__name__}: {e}",
            run_id=run_id,
            error=tb,
        )
        try:
            client.finish_run(run_id, "FAILED")
        except Exception as finish_exc:
            # The primary exception still wins — it is the operator's actual
            # problem — but the fact that the row is stranded RUNNING is now
            # itself durable evidence rather than a silent `pass`.
            _log_quietly(
                client,
                "ERROR",
                "SCRIPT",
                source,
                f"{RUN_FINALIZATION_FAILED}: could not mark run FAILED after an "
                f"application failure ({type(finish_exc).__name__}: {finish_exc})",
                run_id=run_id,
                context={
                    "run_id": run_id,
                    "classification": RUN_FINALIZATION_FAILED,
                    "intended_status": "FAILED",
                    "application_succeeded": False,
                    "primary_exception": f"{type(e).__name__}: {e}",
                },
                error=traceback.format_exc(),
            )
        raise

    # Outside the try on purpose. Anything raised from here is a bookkeeping
    # failure of an already-successful run, and must never be re-entered into
    # the handler that reports application failures.
    try:
        client.finish_run(run_id, "SUCCESS")
    except Exception as finish_exc:
        _log_quietly(
            client,
            "ERROR",
            "SCRIPT",
            source,
            f"{RUN_FINALIZATION_FAILED}: run completed successfully but could not be "
            f"marked SUCCESS ({type(finish_exc).__name__}: {finish_exc})",
            run_id=run_id,
            context={
                "run_id": run_id,
                "classification": RUN_FINALIZATION_FAILED,
                "intended_status": "SUCCESS",
                "application_succeeded": True,
            },
            error=traceback.format_exc(),
        )
        raise RunFinalizationError(run_id, finish_exc) from finish_exc
    client.log("INFO", "SCRIPT", source, "Run finished: SUCCESS", run_id=run_id)
