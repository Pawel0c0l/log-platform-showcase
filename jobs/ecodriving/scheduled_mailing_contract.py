"""THE canonical production execution contract for scheduled Eco mailing fires.

WHAT THIS DECIDES

Exactly one question, for exactly one kind of fire: **when the Workflow A
dispatcher fires an Eco Driving mailing dataset for a client, what invocation
does that fire become?**

It answers with a typed `ScheduledMailingInvocation` carrying two things and
nothing else:

  * the ``execution_mode`` the scheduled run must use — the same explicit
    parameter an operator types by hand, resolved by the same
    `jobs.ecodriving.email_safety.resolve_execution_contract`;
  * the runner **options** the fire must carry — today only the Driver Eco
    Dashboard opt-in, and passed as the literal ``--with-dashboard`` argument
    that `ops/runner.py` already owns and validates.

WHY THE OPTION AND NOT A PARAMETER

`ops/runner.py` is the single authority on which options exist and which job
modules may receive them: it refuses an unknown option, and it refuses
``--with-dashboard`` for any module outside the four Eco mailing commands.
Injecting ``with_dashboard=true`` straight into the parameter JSON would take
the dashboard opt-in around that authority and create a second, unvalidated
propagation path. A scheduled fire therefore carries the SAME argument an
operator types, so scheduled and manual execution are the same command with the
same validation.

THE TWO CONDITIONS SURVIVE, AND STAY INDEPENDENT

Dashboard-enabled sending has always required both an explicit opt-in on the
invocation and explicit client-level rollout permission. A scheduled fire keeps
both, and neither is derived from the other:

  1. **intent** — this declaration (`ops/eco_mailing_production_schedule.json`)
     must name that client and that dataset and give it a sending
     ``execution_mode``. An undeclared pair is not scheduled production;
  2. **permission** — `ops/eco_dashboard_mailing_rollout.json` must enable that
     client. That file remains the ONLY place a dashboard rollout is declared:
     no second boolean is introduced here, and this module cannot enable a
     client the rollout does not.

The job re-checks the permission itself, before it publishes anything or opens
SMTP, so the dispatcher's resolution is a *propagation*, never a substitute for
the gate.

FAIL CLOSED, THE SAME WAY THE ROLLOUT DECLARATION DOES

  * a (client, dataset) pair that is not declared resolves to **nothing**: the
    fire carries no ``execution_mode`` and the job's own default — render only,
    no mail, no dashboard — applies. New clients and new datasets therefore
    never inherit a production schedule;
  * a wildcard client code or dataset name is MALFORMED, not "everyone";
  * a duplicate pair is MALFORMED rather than last-one-wins;
  * only ``render_only`` and ``normal_send`` are schedulable. ``test_send``
    needs a human-chosen recipient and ``force_resend`` deliberately overrides
    an established send: neither is a thing a timer may decide, so both are
    refused here as well as being impossible to express;
  * an unknown key, a wrong contract identity, a non-string value or
    unparseable JSON is MALFORMED. There is no permissive coercion;
  * a declaration that cannot be read at all is UNREADABLE, kept distinct so an
    operator knows whether to look at the filesystem or at the declaration.

WHAT THIS IS NOT

It is not a scheduler. Whether a fire happens at all — cadence, weekday,
wall-clock time, business timezone and the enabled/disabled switch — remains
`workflow_a_control.client_dataset_schedule`, unchanged. This module is only
consulted once a fire has already been claimed, and it can never cause one.

It is also not mailing business logic. Candidate selection, period selection,
rendering, publication, SMTP, the send log and the archive are the job's, and
are identical whichever trigger produced the invocation. A future
email-command trigger reuses this module to build the same invocation and needs
to add nothing else.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from jobs.ecodriving_dashboard.dashboard_rollout import (
    DashboardMailingRollout,
    load_rollout,
    normalize_client_code,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Contract identity, in the repository's established "<slug>/<n>" form.
SCHEDULE_CONTRACT = "eco-mailing-production-schedule/1"

#: THE production declaration. One file, in the repository, under review.
DEFAULT_SCHEDULE_PATH = REPO_ROOT / "ops" / "eco_mailing_production_schedule.json"

#: Test/staging seam, mirroring the rollout declaration's. It cannot widen
#: anything: an alternative declaration is parsed by the same fail-closed rules
#: and still cannot enable a dashboard the rollout declaration has not.
ENV_SCHEDULE_FILE = "ECO_MAILING_PRODUCTION_SCHEDULE_FILE"

#: The dispatcher datasets that ARE Eco mailing, mapped to the job module the
#: registry runs for them and the reporting period they send. Any other dataset
#: is not mailing and can never resolve an invocation here. Exactly the mailing
#: datasets `jobs.api.telematics.registry` declares — a name the dispatcher cannot
#: fire is not schedulable and is refused in a declaration rather than accepted
#: as a decision about nothing.
MAILING_DATASETS: dict[str, dict[str, str]] = {
    "eco_person_driving_weekly_email_notifications": {
        "job_module": (
            "jobs.ecodriving_person."
            "job_eco_driving_person_weekly_email_notifications"
        ),
        "report_type": "weekly",
    },
    "eco_person_driving_monthly_email_notifications": {
        "job_module": (
            "jobs.ecodriving_person."
            "job_eco_driving_person_monthly_email_notifications"
        ),
        "report_type": "monthly",
    },
}

#: The execution modes a timer may decide. See the module docstring.
SCHEDULABLE_EXECUTION_MODES = frozenset({"render_only", "normal_send"})

#: The modes that actually put mail on the wire.
SENDING_EXECUTION_MODES = frozenset({"normal_send"})

#: The Driver Eco Dashboard opt-in, spelled as `ops/runner.py` spells it.
#: Duplicated as a literal rather than imported so this module needs no
#: `ops.runner` import (which would pull in the platform API client); a
#: deterministic test asserts the two literals are the same string.
RUNNER_DASHBOARD_OPTION = "--with-dashboard"

#: Deny by default. `_launch_job` refuses any option outside this set, so a
#: future edit here cannot silently widen what a scheduled fire may carry.
ALLOWED_SCHEDULED_RUNNER_OPTIONS = frozenset({RUNNER_DASHBOARD_OPTION})

_ALLOWED_TOP_LEVEL_KEYS = frozenset({"$comment", "contract", "schedules"})
_ALLOWED_ENTRY_KEYS = frozenset(
    {"client_code", "dataset_name", "execution_mode", "note"}
)

#: Shapes that would mean "everything". Refused as malformed declarations.
_WILDCARD_TOKENS = frozenset({"*", "ALL", "ANY", "DEFAULT", "%"})

UNREADABLE = "ECO_MAILING_PRODUCTION_SCHEDULE_UNREADABLE"
MALFORMED = "ECO_MAILING_PRODUCTION_SCHEDULE_MALFORMED"


class ScheduledMailingDeclarationError(RuntimeError):
    """The scheduled-production declaration could not be read or trusted."""

    def __init__(self, code: str, message: str, source: str = "") -> None:
        self.code = code
        self.source = source
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ScheduledMailingInvocation:
    """One scheduled fire, fully resolved. Evidence, not a decision to fire."""

    client_code: str
    dataset_name: str
    job_module: str
    report_type: str
    execution_mode: str
    with_dashboard: bool
    dashboard_rollout_source: str

    @property
    def sends_email(self) -> bool:
        return self.execution_mode in SENDING_EXECUTION_MODES

    @property
    def runner_options(self) -> tuple[str, ...]:
        """The exact argv options this fire adds after the parameters JSON."""
        return (RUNNER_DASHBOARD_OPTION,) if self.with_dashboard else ()

    def params_overrides(self) -> dict[str, Any]:
        """The parameters this contract contributes, and only those."""
        return {"execution_mode": self.execution_mode}

    def audit(self) -> dict[str, Any]:
        """Everything an operator needs to read a scheduled fire back."""
        return {
            "scheduled_execution_contract": SCHEDULE_CONTRACT,
            "client_code": self.client_code,
            "dataset_name": self.dataset_name,
            "job_module": self.job_module,
            "report_type": self.report_type,
            "execution_mode": self.execution_mode,
            "sends_email": self.sends_email,
            "dashboard_enabled": self.with_dashboard,
            "dashboard_rollout_source": self.dashboard_rollout_source,
            "runner_options": list(self.runner_options),
        }


@dataclass(frozen=True)
class ScheduledMailingDeclaration:
    """The parsed declaration. Immutable, and enables nothing by itself."""

    source: str
    entries: Mapping[tuple[str, str], str]

    def execution_mode_for(
        self, *, dataset_name: Any, client_code: Any
    ) -> Optional[str]:
        return self.entries.get(
            (_normalize_dataset(dataset_name), normalize_client_code(client_code))
        )

    def summary(self) -> dict[str, Any]:
        return {
            "contract": SCHEDULE_CONTRACT,
            "source": self.source,
            "declared": [
                {
                    "dataset_name": dataset_name,
                    "client_code": client_code,
                    "execution_mode": mode,
                }
                for (dataset_name, client_code), mode in sorted(self.entries.items())
            ],
        }


def _normalize_dataset(dataset_name: Any) -> str:
    if dataset_name is None:
        return ""
    return str(dataset_name).strip()


def _malformed(message: str, source: str) -> ScheduledMailingDeclarationError:
    return ScheduledMailingDeclarationError(MALFORMED, message, source)


def _parse_entries(raw: Any, source: str) -> dict[tuple[str, str], str]:
    if not isinstance(raw, list):
        raise _malformed("`schedules` must be a list of entries", source)
    entries: dict[tuple[str, str], str] = {}
    for index, item in enumerate(raw):
        where = f"schedules[{index}]"
        if not isinstance(item, dict):
            raise _malformed(f"{where} must be an object", source)
        unknown = sorted(set(item) - _ALLOWED_ENTRY_KEYS)
        if unknown:
            raise _malformed(
                f"{where} has unknown key(s): {', '.join(unknown)}", source
            )
        client_code = normalize_client_code(item.get("client_code"))
        dataset_name = _normalize_dataset(item.get("dataset_name"))
        if not client_code:
            raise _malformed(f"{where} has no client_code", source)
        if not dataset_name:
            raise _malformed(f"{where} has no dataset_name", source)
        if client_code in _WILDCARD_TOKENS or dataset_name.upper() in _WILDCARD_TOKENS:
            raise _malformed(
                f"{where} uses a wildcard; every client and dataset must be named",
                source,
            )
        if dataset_name not in MAILING_DATASETS:
            raise _malformed(
                f"{where} names {dataset_name!r}, which is not an Eco mailing dataset",
                source,
            )
        mode = item.get("execution_mode")
        if not isinstance(mode, str) or mode.strip() != mode or not mode:
            raise _malformed(f"{where} execution_mode must be a bare string", source)
        if mode not in SCHEDULABLE_EXECUTION_MODES:
            raise _malformed(
                f"{where} execution_mode {mode!r} is not schedulable; allowed: "
                + ", ".join(sorted(SCHEDULABLE_EXECUTION_MODES)),
                source,
            )
        key = (dataset_name, client_code)
        if key in entries:
            raise _malformed(
                f"{where} repeats {client_code}/{dataset_name}; a file that states "
                "a pair twice does not state a decision",
                source,
            )
        entries[key] = mode
    return entries


def parse_schedule_declaration(
    document: Any, *, source: str
) -> ScheduledMailingDeclaration:
    if not isinstance(document, dict):
        raise _malformed("the declaration must be a JSON object", source)
    unknown = sorted(set(document) - _ALLOWED_TOP_LEVEL_KEYS)
    if unknown:
        raise _malformed(f"unknown top-level key(s): {', '.join(unknown)}", source)
    if document.get("contract") != SCHEDULE_CONTRACT:
        raise _malformed(
            f"contract must be {SCHEDULE_CONTRACT!r}", source
        )
    if "schedules" not in document:
        raise _malformed("the declaration states no `schedules`", source)
    return ScheduledMailingDeclaration(
        source=source, entries=_parse_entries(document["schedules"], source)
    )


def schedule_path(path: Any = None) -> Path:
    if path is not None:
        return Path(path)
    override = os.getenv(ENV_SCHEDULE_FILE)
    if override:
        return Path(override)
    return DEFAULT_SCHEDULE_PATH


def load_schedule_declaration(path: Any = None) -> ScheduledMailingDeclaration:
    resolved = schedule_path(path)
    source = str(resolved)
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as error:
        raise ScheduledMailingDeclarationError(
            UNREADABLE, f"cannot read {source}: {error}", source
        ) from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise _malformed(f"invalid JSON: {error}", source) from None
    return parse_schedule_declaration(document, source=source)


def is_mailing_dataset(dataset_name: Any) -> bool:
    return _normalize_dataset(dataset_name) in MAILING_DATASETS


def resolve_scheduled_invocation(
    *,
    dataset_name: Any,
    client_code: Any,
    declaration: Optional[ScheduledMailingDeclaration] = None,
    rollout: Optional[DashboardMailingRollout] = None,
) -> Optional[ScheduledMailingInvocation]:
    """Resolve one claimed fire into its canonical invocation, or `None`.

    `None` means "this pair is not declared scheduled production": the caller
    adds no parameter and no option, and the job falls back to its own
    default — render only, no mail, no dashboard. That is the fail-closed
    branch, and it is the branch every undeclared client and dataset takes.

    A declaration is consulted only for a dataset that IS Eco mailing, and the
    rollout declaration only for a pair that is declared. Nothing else in the
    dispatcher's dataset space can reach either file.
    """
    dataset = _normalize_dataset(dataset_name)
    spec = MAILING_DATASETS.get(dataset)
    if spec is None:
        return None
    code = normalize_client_code(client_code)
    if not code:
        return None
    if declaration is None:
        declaration = load_schedule_declaration()
    mode = declaration.execution_mode_for(dataset_name=dataset, client_code=code)
    if mode is None:
        return None
    if rollout is None:
        rollout = load_rollout()
    return ScheduledMailingInvocation(
        client_code=code,
        dataset_name=dataset,
        job_module=spec["job_module"],
        report_type=spec["report_type"],
        execution_mode=mode,
        # The rollout declaration is the ONLY source of this boolean. This
        # module holds no dashboard switch of its own and cannot enable a
        # client the rollout has not.
        with_dashboard=rollout.is_enabled(code),
        dashboard_rollout_source=str(getattr(rollout, "source", "") or ""),
    )
