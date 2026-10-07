#!/usr/bin/env python3
"""Read-only: what would a scheduled Eco mailing fire actually do?

Answers, without a database, a network call or a single side effect, the
question an operator has before enabling — or after reading — a scheduled Eco
Driving mailing fire:

    for this client and this dataset, what invocation does the scheduler build,
    is the dashboard on, and where did each half of that decision come from?

It resolves exactly what `jobs/api/telematics/dispatcher.py` resolves, through the
same module, so its output is the contract and not a description of it. It reads
two declarations and nothing else. It never reads the schedule table, so it can
say what a fire WOULD be, never whether one is due: cadence, weekday, clock time
and the enabled/disabled switch live in
`workflow_a_control.client_dataset_schedule` and are printed by the ordinary
schedule inspection paths.

    PYTHONPATH="$PWD" .venv/bin/python ops/inspect_eco_mailing_schedule_contract.py
    PYTHONPATH="$PWD" .venv/bin/python ops/inspect_eco_mailing_schedule_contract.py \
        --client-code BRAVO00016 --client-code ALPHA00001
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving import scheduled_mailing_contract as smc
from jobs.ecodriving_dashboard import dashboard_rollout as roll


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--client-code", action="append", dest="client_codes",
        help="Client to resolve. Repeatable. Default: every client either "
             "declaration mentions.")
    parser.add_argument(
        "--schedule-file", default=None,
        help="Alternative scheduled-production declaration (test/staging).")
    parser.add_argument(
        "--rollout-file", default=None,
        help="Alternative dashboard rollout declaration (test/staging).")
    args = parser.parse_args()

    declaration = smc.load_schedule_declaration(args.schedule_file)
    rollout = roll.load_rollout(args.rollout_file)

    codes = sorted({
        roll.normalize_client_code(code) for code in (args.client_codes or [])
    } or (
        {client for _dataset, client in declaration.entries}
        | set(rollout.declared_clients)
    ))

    resolved = []
    for dataset_name in sorted(smc.MAILING_DATASETS):
        for client_code in codes:
            invocation = smc.resolve_scheduled_invocation(
                dataset_name=dataset_name, client_code=client_code,
                declaration=declaration, rollout=rollout,
            )
            if invocation is None:
                resolved.append({
                    "client_code": client_code,
                    "dataset_name": dataset_name,
                    "scheduled_production": False,
                    "execution_mode": None,
                    "effective_behaviour": "render_only (job default)",
                    "dashboard_enabled": False,
                    "runner_options": [],
                    "reason": "not declared in the scheduled-production declaration",
                })
                continue
            record = invocation.audit()
            record["scheduled_production"] = True
            record["effective_behaviour"] = invocation.execution_mode
            resolved.append(record)

    print(json.dumps({
        "inspection_only": True,
        "sends_nothing": True,
        "scheduled_production_declaration": declaration.summary(),
        "dashboard_rollout": rollout.summary(),
        "resolved": resolved,
        "note": (
            "Whether a fire happens at all — cadence, weekday, run_time, "
            "timezone and enabled — is workflow_a_control.client_dataset_schedule "
            "and is NOT read here."
        ),
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
