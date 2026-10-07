import importlib
import json
import os
import sys
from typing import Any


from pathlib import Path
from dotenv import load_dotenv
from ops.environment_identity_file import (
    CANONICAL_IDENTITY_FILE,
    IdentityFileError,
    apply_identity_to_environ,
)

# Automatyczne ładowanie .env z katalogu projektu
BASE_DIR = Path(__file__).resolve().parent.parent
env_path = BASE_DIR / ".env"
if env_path.exists():
    load_dotenv(env_path, override=False)

if CANONICAL_IDENTITY_FILE.exists():
    apply_identity_to_environ(path=CANONICAL_IDENTITY_FILE, reject_conflict=True)
elif os.getenv("LOG_PLATFORM_REQUIRE_CANONICAL_IDENTITY") == "1":
    raise IdentityFileError(
        "IDENTITY_FILE_MISSING",
        f"required canonical identity file is missing: {CANONICAL_IDENTITY_FILE}",
    )



# Importujemy klienta z Twojego projektu
from api.client import LogPlatformClient, run_context


# ---------------------------------------------------------------------------
# Command-line options
# ---------------------------------------------------------------------------
#
# The runner's contract has always been positional:
#
#     python3 ops/runner.py <job_module> [params_json_or_file]
#
# and the Workflow A dispatcher builds that argv
# (`jobs/api/telematics/dispatcher.py::_launch_job`), appending an option only
# when `jobs/ecodriving/scheduled_mailing_contract.py` resolved one for a
# reviewed (client, dataset) pair. This module remains the single authority on
# which options exist and which job modules may receive them, so a scheduled
# fire is validated here exactly as an operator's command is — which is the
# point: there is one command surface, not a scheduled variant of it.

#: THE Driver Eco Dashboard mailing opt-in.
#:
#: Absent — which is every invocation that did not ask, including every
#: scheduled fire outside `ops/eco_mailing_production_schedule.json` — the
#: four Eco Driving mailing jobs run their legacy path: no dashboard snapshot,
#: no publication, no R2 write, no D1 capability state, no capability
#: generation or recovery, no dashboard link in the message, no dependency on
#: publisher configuration or availability, and no change to whether the
#: ordinary e-mail is sent.
#:
#: Present, it is still only HALF of the requirement. Dashboard-enabled sending
#: also needs client-level rollout permission
#: (`ops/eco_dashboard_mailing_rollout.json`), which the job checks before it
#: publishes anything or opens SMTP. Neither condition alone is sufficient.
WITH_DASHBOARD_FLAG = "--with-dashboard"

#: The parameter the flag sets. Mirrors
#: `jobs.ecodriving_dashboard.eco_mailing_integration.PARAM_WITH_DASHBOARD`;
#: duplicated as a literal so the runner needs no job import to parse argv.
WITH_DASHBOARD_PARAM = "with_dashboard"

#: The existing person/driver weekly/monthly mailing commands, and only those.
#: The flag means nothing anywhere else, so anywhere else it is a usage error
#: rather than a silently ignored option.
ECO_MAILING_JOB_MODULES = (
    "jobs.ecodriving.job_eco_driving_weekly_email_notifications",
    "jobs.ecodriving.job_eco_driving_monthly_email_notifications",
    "jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications",
    "jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications",
)

KNOWN_FLAGS = (WITH_DASHBOARD_FLAG,)


class RunnerUsageError(RuntimeError):
    """The invocation itself is wrong. Nothing has been executed."""


def _split_options(argv: list[str]) -> tuple[list[str], list[str]]:
    """Separate `--options` from the positional `<job_module> [params]`.

    Options may appear anywhere, so an operator does not have to remember
    whether the flag goes before or after the params JSON. A params argument is
    either a JSON document or a path and never begins with `--`.
    """
    positional: list[str] = []
    options: list[str] = []
    for token in argv:
        (options if token.startswith("--") else positional).append(token)
    return positional, options


def _apply_options(job_module: str, params: dict[str, Any],
                   options: list[str]) -> dict[str, Any]:
    """Fold recognised options into `params`, or refuse the invocation.

    An unrecognised option is refused rather than ignored: an operator who
    mistypes `--with-dashboards` must not get a silent legacy run that looks
    like it did what they asked.
    """
    unknown = [option for option in options if option not in KNOWN_FLAGS]
    if unknown:
        raise RunnerUsageError(
            "unknown option(s): " + ", ".join(unknown)
            + "; supported: " + ", ".join(KNOWN_FLAGS))
    if WITH_DASHBOARD_FLAG in options:
        if job_module not in ECO_MAILING_JOB_MODULES:
            raise RunnerUsageError(
                f"{WITH_DASHBOARD_FLAG} applies only to the Eco Driving mailing "
                "commands: " + ", ".join(ECO_MAILING_JOB_MODULES))
        params = dict(params)
        params[WITH_DASHBOARD_PARAM] = True
    return params


def _parse_params(argv: list[str]) -> dict[str, Any]:
    """
    Params can be:
      - JSON string: '{"a":1}'
      - path to JSON file: /path/params.json
      - empty -> {}
    """
    if len(argv) < 2:
        return {}
    raw = argv[1].strip()
    if not raw:
        return {}

    if raw.startswith("{") or raw.startswith("["):
        return json.loads(raw)

    # file path
    with open(raw, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_run_id_file_if_requested(run_id: str) -> None:
    path = os.getenv("LOG_PLATFORM_RUN_ID_FILE")
    if not path:
        return
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(run_id + "\n", encoding="utf-8")


# Last platform run id opened by this process, so a terminal failure can be
# correlated back to its `public.runs` / `public.logs` rows. Not thread state:
# the runner executes exactly one job per process.
_ACTIVE_RUN: dict[str, Any] = {"run_id": None}


def _note_active_run(run_id: str) -> str:
    _ACTIVE_RUN["run_id"] = run_id
    return run_id


def _report_terminal_failure(job_module: str, params: dict, exc: BaseException) -> None:
    """Turn an escaping job exception into a durable operator incident.

    `ops/runner.py` is the single boundary every scheduled job crosses, so this
    one hook covers the Workflow A dispatcher and sync, the Workflow B
    orchestrator and its stages, retention purge and the Eco aggregate/email
    jobs. It changes no job's own success semantics: the run is already FAILED
    by the time this runs, and reporting can never mask the original exception.
    """
    import traceback

    try:
        from ops.operational_alert import report_job_terminal_failure

        report_job_terminal_failure(
            job_module=job_module,
            exc=exc,
            run_id=_ACTIVE_RUN.get("run_id"),
            params=params if isinstance(params, dict) else {},
            stack_trace=traceback.format_exc(),
        )
    except Exception:  # pragma: no cover - alerting must never mask the failure
        pass


def main() -> int:
    """Execute one job and, on terminal failure, raise an operator incident."""
    positional, options = _split_options(sys.argv[1:])
    job_module = positional[0].strip() if positional else ""
    params: dict[str, Any] = {}
    try:
        params = _apply_options(job_module, _parse_params(positional), options)
    except Exception:
        params = {}
    try:
        return _execute()
    except RunnerUsageError as usage_error:
        # A rejected invocation executed nothing, so there is no job failure to
        # report and no run to correlate. It is a usage error, not an incident.
        print(f"ERROR: {usage_error}")
        return 2
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if job_module:
            _report_terminal_failure(job_module, params, exc)
        raise


def _execute() -> int:
    positional, options = _split_options(sys.argv[1:])
    if len(positional) < 1:
        print("Usage: python3 ops/runner.py <job_module> [params_json_or_file] "
              f"[{WITH_DASHBOARD_FLAG}]")
        print("Example: python3 ops/runner.py jobs.reports.demo '{\"k\":\"v\"}'")
        return 2

    job_module = positional[0].strip()
    params = _apply_options(job_module, _parse_params(positional), options)

    # base_url + tokens czytamy z ENV (u Ciebie ładowane z .env w shellu)
    client = LogPlatformClient.from_env()

    # source w log-platform = nazwa joba (czytelne filtrowanie)
    source = job_module

    trigger = params.get("trigger", "MANUAL")
    actor = params.get("actor")

    # import joba
    mod = importlib.import_module(job_module)
    if getattr(mod, "DEFERRED_RUN_CREATION", False) and "trigger" not in params:
        trigger = "SCHEDULED"

    if not hasattr(mod, "run"):
        print(f"ERROR: Module {job_module} must expose function: run(client, run_id, params)")
        return 3

    job_run = getattr(mod, "run")

    # Opt-in protocol for jobs that can atomically decide whether a persisted
    # technical run is warranted. The dispatcher uses this to keep successful
    # no-work ticks out of runs/logs while preserving failures and real work.
    if getattr(mod, "DEFERRED_RUN_CREATION", False):
        prepare_run = getattr(mod, "prepare_run", None)
        run_prepared = getattr(mod, "run_prepared", None)
        if not callable(prepare_run) or not callable(run_prepared):
            print(
                "ERROR: Deferred jobs must expose prepare_run(params) and "
                "run_prepared(client, run_id, params, prepared)"
            )
            return 3
        try:
            prepared = prepare_run(params)
        except Exception as preparation_error:
            # Re-raise inside run_context so planning/DB failures retain the
            # standard persisted FAILED lifecycle.
            with run_context(
                client, trigger=trigger, source=source, actor=actor, params=params
            ) as run_id:
                _note_active_run(run_id)
                _write_run_id_file_if_requested(run_id)
                client.log(
                    "INFO", "SCRIPT", source, "Job dispatch", run_id=run_id,
                    context={"module": job_module, "phase": "prepare"},
                )
                raise preparation_error
        if prepared is None:
            return 0
        try:
            with run_context(
                client, trigger=trigger, source=source, actor=actor, params=params
            ) as run_id:
                _note_active_run(run_id)
                _write_run_id_file_if_requested(run_id)
                client.log(
                    "INFO", "SCRIPT", source, "Job dispatch", run_id=run_id,
                    context={"module": job_module},
                )
                run_prepared(
                    client=client, run_id=run_id, params=params, prepared=prepared
                )
        except Exception:
            discard_prepared = getattr(mod, "discard_prepared", None)
            if callable(discard_prepared):
                discard_prepared(prepared)
            raise
        return 0

    with run_context(client, trigger=trigger, source=source, actor=actor, params=params) as run_id:
        _note_active_run(run_id)
        _write_run_id_file_if_requested(run_id)
        client.log("INFO", "SCRIPT", source, "Job dispatch", run_id=run_id, context={"module": job_module})
        job_run(client=client, run_id=run_id, params=params)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
