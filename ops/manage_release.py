#!/usr/bin/env python3
"""Operator CLI for the production release boundary.

Subcommands:

    prepare   materialize an immutable release from an explicit commit
    verify    recompute that a release is byte-identical to its commit
    activate  point `current` at a verified release (mutating: needs --execute)
    rollback  return `current` to the recorded `previous` (needs --execute)
    remove    delete a release no pointer references (needs --execute)
    status    which release the pointers name, and whether production reads them
    list      every prepared release

`prepare` and `verify` are read-only with respect to production and safe to run
at any time. `activate` and `rollback` move pointers and therefore default to a
dry run; they only act with `--execute`.

Whether any of this is *live* is a separate fact: production executes whatever
`BASE_DIR` the installed wrapper names. `status` reports that path next to the
pointer state so the two are never conflated — see `docs/07_operations.md`
§ "Release boundary".

    ops/manage_release.py prepare --commit <sha>
    ops/manage_release.py verify --release <id>
    ops/manage_release.py status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.release_boundary import (  # noqa: E402
    DEFAULT_RUNTIME_LINK_NAMES,
    assess_release_bootability,
    FAULT_RELEASE_SPECIFIC,
    FAULT_SHARED,
    diagnose_runtime_fault,
    diagnose_shared_runtime_fault,
    repoint_previous,
    unknown_bootability,
    WRAPPER_DEVELOPMENT,
    WRAPPER_RELEASE,
    WRAPPER_RELEASE_HISTORICAL,
    installed_wrapper_variant,
    observe_running_release,
    pointer_matches_running,
    RELEASE_BOUND_SERVICES,
    RELEASES_DIRNAME,
    RUNTIME_LINK_ENV,
    RUNTIME_LINK_VENV,
    ReleaseBoundaryError,
    activate_release,
    list_releases,
    prepare_release,
    release_status,
    remove_release,
    resolve_commit,
    rollback_release,
    verify_release,
)

DEFAULT_RELEASE_ROOT = Path("/opt/log-platform-release")
INSTALLED_WRAPPER = Path("/usr/local/bin/log-job-runner.sh")
_WRAPPER_BASE_DIR_RE = re.compile(r'^BASE_DIR="([^"]+)"', re.MULTILINE)


def installed_wrapper_base_dir() -> dict:
    """Read-only: which wrapper production has installed, and therefore what runs.

    A prepared or even activated release is inert until the installed wrapper
    reads it, so identity comes from `installed_wrapper_variant` (SHA-256
    against the two maintained wrappers). The parsed `BASE_DIR` is reported too,
    explicitly labelled as the literal it is: the release wrapper assigns
    `BASE_DIR="${RELEASE_ROOT}/current"`, so its textual value is an unexpanded
    shell variable and must never be used to decide anything.
    """
    if not INSTALLED_WRAPPER.is_file():
        return {"wrapper": str(INSTALLED_WRAPPER), "present": False,
                "variant": "absent", "base_dir_literal": None, "sha256": None}
    variant = installed_wrapper_variant(repo_root=REPO_ROOT, installed_wrapper=INSTALLED_WRAPPER)
    try:
        text = INSTALLED_WRAPPER.read_text()
        digest = hashlib.sha256(INSTALLED_WRAPPER.read_bytes()).hexdigest()
    except OSError as exc:
        return {"wrapper": str(INSTALLED_WRAPPER), "present": True, "variant": variant,
                "base_dir_literal": None, "sha256": None, "error": str(exc)}
    match = _WRAPPER_BASE_DIR_RE.search(text)
    return {"wrapper": str(INSTALLED_WRAPPER), "present": True, "variant": variant,
            "base_dir_literal": match.group(1) if match else None, "sha256": digest}


def _runtime_links(args) -> dict:
    """Runtime links for a CLI-prepared release, defaulted rather than omitted.

    Omitting both flags used to produce a link-less release that verified
    cleanly and then could not start — the 2026-08-20 outage. An operator
    following the documented command passes both; an operator who forgets got a
    broken release and no warning until production crash-looped. Defaulting to
    the source repository's own `.env` and `.venv` makes the documented case the
    unavoidable one, and the paths are derived from `--source-repo` rather than
    hardcoded, so a non-standard checkout still gets its own.

    `--no-runtime-links` remains for a deliberately link-less release; it has to
    be asked for now, and such a release is reported unbootable rather than
    silently passing.
    """

    if getattr(args, "no_runtime_links", False):
        return {}
    source_repo = Path(args.source_repo)
    return {
        RUNTIME_LINK_ENV: Path(args.env_file) if args.env_file else source_repo / RUNTIME_LINK_ENV,
        RUNTIME_LINK_VENV: Path(args.venv) if args.venv else source_repo / RUNTIME_LINK_VENV,
    }


def _assess(release_root, release_id) -> dict:
    """Bootability, or an explicit UNKNOWN verdict if the checker itself failed.

    THE ASYMMETRY THIS EXISTS FOR — read before changing how any caller uses it.

    `activate` and `rollback` must fail in OPPOSITE directions, and making them
    consistent would silently re-break recovery:

    * `activate` is not an emergency. Nothing is broken while you retry, so
      refusing on an unknown verdict costs a re-run and protects the pointer.
      It fails CLOSED.
    * `rollback` runs when production is already down. An unknown verdict is not
      evidence of a bad fallback, and a checker that cannot run must never be the
      reason recovery does not happen. It fails OPEN.

    This was learned the hard way on 2026-08-20. The bootability gate was added
    that morning to prevent an outage; by that evening it had made the emergency
    path more fragile than it found it, because a raising checker blocked
    `rollback` entirely and surfaced as a bare traceback with no guidance. Before
    the gate existed, a transient filesystem fault could not stop a rollback at
    all. The gate must not be the reason recovery fails.

    It is the same reasoning that kept the application import out of the
    assessment: a transient environment fault must not produce a false refusal at
    the worst possible moment. That argument was applied one level too high the
    first time — to what the checker inspects, but not to the checker failing.

    The catch is deliberately broad. Enumerating the faults we thought of
    (ReleaseBoundaryError, OSError from a readlink race, permission errors while
    traversing) and hoping the list is complete is precisely the assumption that
    produced this defect; recovery has to survive the faults nobody anticipated.
    """

    try:
        return assess_release_bootability(release_root=release_root, release_id=release_id)
    except Exception as exc:  # noqa: BLE001 - see above; breadth is the point
        return unknown_bootability(release_id, exc)


def _diagnose(release_root, release_id, compare_with):
    """Shared-resource diagnosis, or None. NEVER raises.

    This is advisory: it refines the WORDING of a warning or a refusal that has
    already been decided elsewhere. It must never be the reason a command fails,
    least of all on the recovery path — which is exactly what the first,
    unguarded version of this call did: sabotaging the checker turned a
    proceed-with-warning rollback into a hard failure, reintroducing through a
    new door the defect `_assess` was written to close.

    The standing question for anything that runs during an incident is not only
    what it reports, but what happens when it cannot run. For an advisory helper
    the answer is: say nothing, and get out of the way.
    """

    try:
        return diagnose_runtime_fault(
            release_root=Path(release_root), release_id=release_id, compare_with=compare_with)
    except Exception:  # noqa: BLE001 - advisory only; a failure means "no diagnosis"
        return None


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _emit_err(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), file=sys.stderr)


def cmd_prepare(args) -> int:
    commit = resolve_commit(Path(args.source_repo), args.commit)
    result = prepare_release(
        source_repo=Path(args.source_repo),
        release_root=Path(args.release_root),
        committish=args.commit,
        runtime_links=_runtime_links(args),
    )
    report = verify_release(release_root=Path(args.release_root), release_id=result["release_id"],
                            source_repo=Path(args.source_repo))
    _emit({"action": "prepare", "resolved_commit": commit, **result,
           "verified": True, "file_count": report["file_count"],
           "runtime_links": report["runtime_links"],
           "bootable": report["bootable"],
           "bootability": report["bootability"],
           # `is False`, not falsiness: an UNKNOWN verdict means the checker could
           # not find out, which is not a reason to call a freshly materialized
           # release unusable. `activate` is the gate that fails closed on
           # unknown, so nothing can reach production on the strength of this.
           "activation": ("NOT ACTIVATABLE: this release cannot start; see bootability above"
                          if report["bootable"] is False else
                          "not activated by prepare; run `activate --execute`")})
    return 1 if report["bootable"] is False else 0


def cmd_verify(args) -> int:
    report = verify_release(release_root=Path(args.release_root), release_id=args.release,
                            source_repo=Path(args.source_repo))
    report.pop("metadata", None)
    _emit({"action": "verify", "verified": True, **report})
    return 0


def cmd_activate(args) -> int:
    report = verify_release(release_root=Path(args.release_root), release_id=args.release,
                            source_repo=Path(args.source_repo))
    status = release_status(Path(args.release_root))

    # The gate the 2026-08-20 outage was missing. `activate` is the step that
    # puts a release in front of the launcher, so this is the last moment a
    # release that cannot start can be stopped cheaply. It runs identically in
    # the dry run and the execute path, so a rehearsal cannot report a clean
    # activation the real one would refuse.
    #
    # Scope, deliberately: this is the `activate` COMMAND only, not
    # `activate_release()` in the library, because `rollback` reaches the same
    # library function and gating it there would silently change what rollback
    # does when its target cannot start. That is a decision about the semantics
    # of the escape hatch, not a bug fix, and it is deliberately left open.
    # FAILS CLOSED, including on an unknown verdict: see `_assess`. Retrying an
    # activation is free, so refusing without proof is the cheap side here.
    boot = _assess(Path(args.release_root), args.release)
    if boot["bootable"] is not True:
        _emit({"action": "activate", "executed": False, "would_activate": args.release,
               "current_release_id": status["current_release_id"],
               "classification": "RELEASE_NOT_BOOTABLE",
               # The VERDICT, not a hardcoded False. `activate` refuses on both
               # False and unknown, but a consumer reading this field must be able
               # to tell which — the nested `bootability` block always could, and
               # the two disagreeing is worse than either alone.
               "bootable": boot["bootable"], "bootability": boot,
               # Renamed from `shared_runtime_fault`: since OPS-11 the diagnosis
               # carries four conclusions, so the old name asserted one of them.
               "runtime_fault": _diagnose(
                   args.release_root, args.release, status["current_release_id"]),
               "next": ("activation REFUSED: the bootability check could not be completed, so "
                        "this release is not proven able to start. Nothing has changed; "
                        "investigate the error above and re-run."
                        if boot.get("verdict") == "unknown" else
                        "activation REFUSED: this release cannot start and would crash-loop the "
                        "service. Re-prepare it with its runtime links "
                        "(`prepare --commit <sha>` now defaults them) and activate that.")})
        return 1

    if not args.execute:
        # The dry run reports the same schema prerequisite gate the execute path
        # enforces, so an operator learns about a missing client migration
        # before committing to the activation rather than from a refusal. It is
        # read-only: the preflight issues only SELECTs.
        from ops.release_schema_preflight import (
            SchemaPreflightError, verify_schema_prerequisites,
        )
        layout_root = Path(args.release_root)
        try:
            schema_report = verify_schema_prerequisites(
                release_tree=layout_root / "releases" / args.release,
                release_id=args.release,
            ).as_dict()
            schema_ok, schema_refusal = True, None
        except SchemaPreflightError as exc:
            schema_report, schema_ok = None, False
            schema_refusal = {"code": exc.code, "detail": exc.detail, **exc.context}
        _emit({"action": "activate", "executed": False, "would_activate": args.release,
               "commit": report["commit"], "current_release_id": status["current_release_id"],
               "runtime": installed_wrapper_base_dir(),
               "schema_preflight_ok": schema_ok,
               "schema_preflight": schema_report,
               "schema_preflight_refusal": schema_refusal,
               "next": ("re-run the identical command with --execute" if schema_ok
                        else "activation would be REFUSED: resolve the schema "
                             "prerequisite above before re-running")})
        return 0 if schema_ok else 1
    result = activate_release(release_root=Path(args.release_root), release_id=args.release,
                              source_repo=Path(args.source_repo))

    # ACTIVATION IS NOT DEPLOYMENT, and this is where that stops being implicit.
    #
    # Moving the pointer does not change what is executing: the release-bound
    # services resolve `current` at start and hold it until restarted. Until
    # 2026-08-27 this command reported a successful activation and exited 0 into
    # exactly that state, and production served the previous release for two
    # days with every tool reporting health.
    #
    # WHY THIS COMMAND STILL DOES NOT RESTART ANYTHING. Restarting was the
    # obvious fix and it is the wrong one. `activate` today can only rewrite two
    # symlinks inside a directory it owns; a failed activation leaves production
    # serving what it was already serving. Teaching it to restart would hand a
    # pointer-management tool the ability to take production down, require it to
    # hold privilege it does not have, and -- worst -- give it a NEW silent
    # failure mode: a restart with no health gate turns today's "stale but
    # healthy" into "activated and now down". Restart, health check and smoke
    # are one operator procedure with judgement in it (`docs/07_operations.md`
    # § "Release boundary"), not a side effect of moving a symlink.
    #
    # So the command keeps its blast radius and loses its silence. It observes
    # what is running, reports the release as NOT LIVE, names the exact command,
    # and exits non-zero. An operator or a script cannot mistake this for done.
    running = observe_running_release(release_root=Path(args.release_root))
    live = pointer_matches_running(args.release, running)
    restart_command = "sudo systemctl restart " + " ".join(RELEASE_BOUND_SERVICES)

    if live is True:
        payload_extra = {
            "classification": "ACTIVE_AND_LIVE",
            "next": "no restart required: the release-bound services are already executing "
                    f"{args.release}. Confirm health and run the production smoke.",
        }
        exit_code = 0
    elif live is False:
        payload_extra = {
            "classification": "ACTIVATED_NOT_YET_LIVE",
            "next": f"THE POINTER MOVED; PRODUCTION HAS NOT. `current` now names {args.release} "
                    f"but the release-bound services are still executing "
                    f"{running.get('release_id') or running.get('release_ids')}. This activation "
                    f"is NOT deployed until you run: {restart_command} -- then verify with "
                    "`ops/manage_release.py status` that running_release_id matches, and run the "
                    "health check and production smoke. To abandon it instead, "
                    "`ops/manage_release.py rollback --execute`.",
        }
        exit_code = 2
    else:
        payload_extra = {
            "classification": "ACTIVATION_LIVENESS_UNKNOWN",
            "next": f"`current` now names {args.release}, but which release the services are "
                    "executing COULD NOT BE OBSERVED, so this activation is not proven live. "
                    f"Do not assume it is. Restart the release-bound services ({restart_command}) "
                    "and verify with `ops/manage_release.py status`.",
        }
        exit_code = 2

    _emit({"action": "activate", "executed": True, **result,
           "runtime": installed_wrapper_base_dir(),
           # `live` is the same three-state answer `status` reports, and for the
           # same reason: unknown is not success.
           "live": live,
           "running_release_id": running.get("release_id"),
           "running_release": running,
           "restart_command": restart_command,
           **payload_extra})
    return exit_code


def cmd_repair_previous(args) -> int:
    """Aim `previous` somewhere useful again. See `repoint_previous`.

    Dry run by default, like `activate` and `rollback`, and it prints the full
    before/after before it will act: this exists to be reached for during an
    incident, and a command that changes a pointer without first saying which
    pointer and from what to what is not one you want in that moment.
    """
    release_root = Path(args.release_root)
    status = release_status(release_root)
    # Part of the recovery path, so an unknown verdict reports and proceeds.
    target_boot = _assess(release_root, args.release)

    if not args.execute:
        _emit({
            "action": "repair-previous", "executed": False,
            "current_release_id": status["current_release_id"],
            "previous_release_id_now": status["previous_release_id"],
            "previous_release_id_after": args.release,
            "target_bootable": target_boot["bootable"],
            "target_bootability": target_boot,
            "current_is_never_touched": True,
            # Three states, three messages. Telling an operator the target "does
            # not look bootable" when the checker merely failed is advisory text
            # that is simply false, delivered on the escape hatch at the moment
            # they are deciding whether to use it.
            "warning": (
                None if target_boot["bootable"] is True else
                ("the release you are aiming `previous` at does not look bootable either; "
                 "rolling back to it would not recover the service"
                 if target_boot["bootable"] is False else
                 "the bootability of the release you are aiming `previous` at could not be "
                 f"determined ({target_boot.get('assessment_error')}). Proceeding is "
                 "reasonable — this command never touches `current` — but verify the "
                 "service after any rollback onto it.")),
            "next": "re-run the identical command with --execute",
        })
        return 0

    result = repoint_previous(release_root=release_root, release_id=args.release,
                              source_repo=Path(args.source_repo))
    _emit({"action": "repair-previous", "executed": True,
           "current_is_never_touched": True, **result})
    return 0


def cmd_rollback(args) -> int:
    status = release_status(Path(args.release_root))
    target = status["previous_release_id"]
    if target is None:
        raise ReleaseBoundaryError("RELEASE_POINTER_INVALID", {"reason": "no_previous_release_recorded"})

    # ROLLBACK WARNS. IT DOES NOT REFUSE. Reverted 2026-08-20 after an
    # independent review; re-enabling the refusal is a separate decision.
    #
    # The refusal was added to stop a rollback ONTO a release that cannot start.
    # It produced at least three ways to block recovery instead:
    #
    #  * the verdict is a function of `meta/<id>.json`, but the launcher reads
    #    only the filesystem — `[[ -x .venv/bin/python ]]` and `[[ -e .env ]]`,
    #    and it never opens the metadata. A release whose links are present and
    #    working but undeclared assesses False, so a fallback the wrapper would
    #    happily start was refused;
    #  * every release on this host declares the SAME two link targets, both in
    #    the mutable development tree, so one venv rebuild makes every release
    #    assess False at once. The refusal then diagnosed a per-release fault and
    #    printed a per-release remedy whose every step returned to the same
    #    refusal — a loop, with the real fix (restore the venv) never mentioned;
    #  * `Path.exists()` on an unreadable link target raises EACCES on 3.12, so
    #    the operator got a bare traceback anyway.
    #
    # Rolling back cannot make a shared-resource fault worse: the pointer moves
    # in milliseconds and the service starts as soon as the resource is back. A
    # gate that blocks the emergency path is worse than the silent wrong recovery
    # it replaced, so the warning carries the diagnosis and the operator decides.
    #
    # `activate` and `cutover` keep their refusals. They are planned operations
    # with production healthy, where refusing without proof costs only a re-run.
    # See `_assess`: the direction a check fails in follows whether production is
    # already down.
    target_boot = _assess(Path(args.release_root), target)
    warning = None
    if target_boot["bootable"] is None:
        warning = (
            "the bootability check could not be completed: "
            f"{target_boot.get('assessment_error')}. Rollback is PROCEEDING anyway, "
            "deliberately: a checker that cannot run is not evidence that the fallback "
            "is bad, and recovery must never depend on the checker being healthy. "
            "Verify the service yourself after the rollback."
        )
    elif target_boot["bootable"] is False:
        reasons = sorted({d.get("reason") for d in (target_boot.get("defects") or []) if d.get("reason")})
        diagnosis = _diagnose(args.release_root, target, status["current_release_id"])
        conclusion = (diagnosis or {}).get("conclusion")

        # THE NARROW REFUSAL. Reinstated 2026-08-20 after F2 gave us something we
        # did not have when the first version of this gate was written: the
        # ability to tell a SHARED-resource fault from a RELEASE-SPECIFIC one.
        # Those deserve opposite treatment, and conflating them is exactly why
        # the original refusal was net-harmful and had to be reverted.
        #
        #   SHARED           refusing protects nothing. Nothing starts whichever
        #                    release `current` names, the operator must repair the
        #                    shared resource either way, and the pointer move is
        #                    harmless. Refusing only adds friction — and, before
        #                    F2, printed a per-release remedy that looped forever.
        #   RELEASE_SPECIFIC the target is broken on its OWN resources while the
        #                    serving release is fine. Rolling back here trades a
        #                    working service for a dead one. This is the case the
        #                    gate was built for, it is now identifiable, and
        #                    `repair-previous` is now documented as the way out.
        #
        # INDETERMINATE PROCEEDS. Refusing requires POSITIVE evidence that the
        # fault is this release's own; the absence of a shared verdict is not
        # that evidence. `_diagnose` also returns None on any internal fault, and
        # a checker that could not run must never be the reason recovery does not
        # happen — the same rule as the UNKNOWN verdict above.
        if conclusion == FAULT_RELEASE_SPECIFIC:
            _emit({
                "action": "rollback", "executed": False, "would_have_activated": target,
                "current_release_id": status["current_release_id"],
                "classification": "RELEASE_NOT_BOOTABLE",
                "target_bootable": False, "target_bootability": target_boot,
                "runtime_fault": diagnosis,
                "why": (f"the rollback target fails on {', '.join(reasons)}, and the release "
                        "currently serving does NOT fail on those same paths — so the fault "
                        "is this release's own, not the shared runtime resources. Rolling "
                        "back would trade a working service for one that cannot start."),
                "next": ("aim the fallback at a release that works, then roll back:\n"
                         "  1. ops/manage_release.py list                      "
                         "# choose a known-good release\n"
                         "  2. ops/manage_release.py repair-previous --release <id>            "
                         "# dry run\n"
                         "  3. ops/manage_release.py repair-previous --release <id> --execute\n"
                         "  4. ops/manage_release.py rollback --execute\n"
                         "`repair-previous` never touches `current`, so it cannot affect what "
                         "is serving now. See docs/07_operations.md, "
                         "`repair-previous` — the way out of a poisoned fallback."),
            })
            return 1

        if conclusion == FAULT_SHARED:
            warning = (
                f"the rollback target reports {', '.join(reasons)}, and so does the release "
                f"currently serving — on the SAME resolved path(s): "
                f"{', '.join(diagnosis.get('shared_paths') or [])}. The fault is therefore in "
                "the shared runtime resource, NOT in either release, and NO POINTER MOVE WILL "
                "FIX IT. Rollback is PROCEEDING anyway, because moving the pointer cannot make "
                "a shared fault worse and the service will start as soon as the resource is "
                "repaired. Repair the path above — a venv rebuild or an interrupted "
                "`pip install` is the usual cause."
            )
        else:
            warning = (
                f"the rollback target does not look bootable ({', '.join(reasons) or 'see bootability'}), "
                "but it could NOT be established whether the fault is its own or lies in the "
                "runtime resources every release shares. Rollback is PROCEEDING: refusing "
                "requires positive evidence that the fault is this release's own, and an "
                "inconclusive diagnosis is not that evidence. Verify the service afterwards."
            )

    if not args.execute:
        # The dry run must tolerate exactly what the execute path tolerates, or
        # the rehearsal refuses something the real command would do — the same
        # divergence `activate`'s dry run was written to avoid.
        tolerated_dry = None
        try:
            report = verify_release(release_root=Path(args.release_root), release_id=target,
                                    source_repo=Path(args.source_repo))
        except ReleaseBoundaryError as exc:
            if exc.classification != "RELEASE_RUNTIME_RESOURCE_MISSING":
                raise
            tolerated_dry = {"classification": exc.classification, "details": exc.details}
            report = {"commit": None}
        _emit({"action": "rollback", "executed": False, "would_activate": target,
               "commit": report["commit"], "current_release_id": status["current_release_id"],
               "would_tolerate_verification_failure": tolerated_dry,
               "runtime": installed_wrapper_base_dir(),
               "target_bootable": target_boot["bootable"],
               "target_bootability": target_boot,
               "runtime_fault": _diagnose(
                   args.release_root, target, status["current_release_id"]),
               "warning": warning,
               "next": "re-run the identical command with --execute"})
        return 0

    # Only `rollback` asks for the narrow tolerance; `activate` and `cutover`
    # keep verifying strictly, because they are promotions and retrying is free.
    result = rollback_release(release_root=Path(args.release_root),
                              source_repo=Path(args.source_repo),
                              tolerate_missing_runtime_resource=True)
    tolerated = result.get("tolerated_verification_failure")
    _emit({"action": "rollback", "executed": True, **result,
           "target_bootable": target_boot["bootable"],
           "warning": ((warning + " " if warning else "")
                       + "VERIFICATION WAS TOLERATED: the runtime-link targets this release "
                         "points at are missing, which every release shares, so refusing would "
                         "have left no command able to move the pointer. The pointer HAS moved; "
                         "the service will not start until those resources are restored."
                       if tolerated else warning),
           "runtime": installed_wrapper_base_dir()})
    return 0


def cmd_status(args) -> int:
    status = release_status(Path(args.release_root))
    runtime = installed_wrapper_base_dir()
    variant = runtime.get("variant")
    # A release wrapper from an earlier commit still executes the release root.
    # Reporting it as "not the release root" would hand an operator the exact
    # conclusion the boundary guards exist to prevent — "the boundary is gone,
    # reinstall the wrapper" — during an incident. The remedy for a stale
    # wrapper is to promote a release, which `wrapper_up_to_date` says plainly.
    production_uses_release_root = variant in (WRAPPER_RELEASE, WRAPPER_RELEASE_HISTORICAL)
    if production_uses_release_root:
        production_source_path = f"{Path(args.release_root)}/current -> {status['current_release_id']}"
    elif variant == WRAPPER_DEVELOPMENT:
        production_source_path = str(REPO_ROOT)
    else:
        production_source_path = None
    # WHAT IS RUNNING, next to what the pointers intend. Everything above this
    # line describes files and symlinks; none of it can tell an operator that
    # the service is still serving the previous release because nobody
    # restarted it after the activation. That is not hypothetical -- it is the
    # state this host was in for two days, while `status` reported a clean
    # `remedy: null`.
    running = observe_running_release(release_root=Path(args.release_root))
    matches = pointer_matches_running(status["current_release_id"], running)

    remedy = None
    if matches is False:
        remedy = (
            f"RELEASE DRIFT: `current` points at {status['current_release_id']} but the "
            f"release-bound services are executing "
            f"{running.get('release_id') or running.get('release_ids')}. Activation moves the "
            "pointer; it does not restart anything. Restart the release-bound services to "
            "adopt the active release: `sudo systemctl restart "
            + " ".join(RELEASE_BOUND_SERVICES)
            + "`, then re-run `status` and confirm running_release_id."
        )
    elif variant == WRAPPER_RELEASE_HISTORICAL:
        remedy = ("installed wrapper is from an earlier commit; promote a release "
                  "(never re-provision identity, which would remove the boundary)")

    _emit({"action": "status", **status, "runtime": runtime,
           # RENAMED from `production_executes_release_root`. The old name was
           # read as "production is executing the active release" -- a statement
           # about runtime -- when it only ever measured the installed wrapper's
           # configuration. It now says which it is, and the observed fact sits
           # beside it so the two cannot be confused for one another.
           "wrapper_executes_release_root": production_uses_release_root,
           "production_wrapper_variant": variant,
           "wrapper_up_to_date": None if not production_uses_release_root else variant == WRAPPER_RELEASE,
           "running_release_id": running.get("release_id"),
           "running_release": running,
           # True / False / None. None means NOT OBSERVED, and is never rendered
           # as agreement.
           "pointer_matches_running_release": matches,
           "remedy": remedy,
           # Renamed for the same reason: this is the path production is
           # CONFIGURED to read, which is not evidence about what it is running.
           "configured_source_path": production_source_path})
    return 0


def cmd_remove(args) -> int:
    status = release_status(Path(args.release_root))
    if args.release in (status["current_release_id"], status["previous_release_id"]):
        raise ReleaseBoundaryError("RELEASE_POINTER_INVALID", {
            "release_id": args.release, "reason": "release_is_referenced_by_a_pointer",
        })
    if not args.execute:
        _emit({"action": "remove", "executed": False, "would_remove": args.release,
               "current_release_id": status["current_release_id"],
               "previous_release_id": status["previous_release_id"],
               "next": "re-run the identical command with --execute"})
        return 0
    _emit({"action": "remove", "executed": True,
           **remove_release(release_root=Path(args.release_root), release_id=args.release)})
    return 0


def cmd_list(args) -> int:
    _emit({"action": "list", "releases": list_releases(Path(args.release_root))})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-repo", default=str(REPO_ROOT),
                        help="development repository the release is cut from")
    parser.add_argument("--release-root", default=str(DEFAULT_RELEASE_ROOT),
                        help="release root holding releases/, meta/, current and previous")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="materialize an immutable release from a commit")
    prepare.add_argument("--commit", required=True, help="explicit commit SHA (or any committish that resolves to one)")
    prepare.add_argument("--env-file", default=None,
                         help=f"absolute path linked into the release as {RUNTIME_LINK_ENV}")
    prepare.add_argument("--venv", default=None,
                         help=f"absolute path linked into the release as {RUNTIME_LINK_VENV}")
    prepare.add_argument("--no-runtime-links", action="store_true",
                         help="deliberately prepare a release with no runtime links; it will not "
                              "be activatable, because it cannot start")
    prepare.set_defaults(func=cmd_prepare)

    verify = sub.add_parser("verify", help="recompute release content against its commit")
    verify.add_argument("--release", required=True)
    verify.set_defaults(func=cmd_verify)

    activate = sub.add_parser("activate", help="point current at a verified release")
    activate.add_argument("--release", required=True)
    activate.add_argument("--execute", action="store_true", help="actually move the pointer")
    activate.set_defaults(func=cmd_activate)

    repair = sub.add_parser("repair-previous",
                            help="aim `previous` at a different release (never touches `current`)")
    repair.add_argument("--release", required=True,
                        help="release id `previous` should name after the repair")
    repair.add_argument("--execute", action="store_true", help="actually move the `previous` pointer")
    repair.set_defaults(func=cmd_repair_previous)

    rollback = sub.add_parser("rollback", help="return current to the recorded previous release")
    rollback.add_argument("--execute", action="store_true", help="actually move the pointer")
    rollback.set_defaults(func=cmd_rollback)

    status = sub.add_parser("status", help="pointer state plus the path production actually executes")
    status.set_defaults(func=cmd_status)

    remove = sub.add_parser("remove", help="delete a release no pointer references")
    remove.add_argument("--release", required=True)
    remove.add_argument("--execute", action="store_true", help="actually delete the release")
    remove.set_defaults(func=cmd_remove)

    listing = sub.add_parser("list", help="every prepared release")
    listing.set_defaults(func=cmd_list)
    return parser


#: Exit code for a fault this CLI did not anticipate. Distinct from 2, which
#: means "the release boundary refused you for a stated reason".
EXIT_UNEXPECTED = 3


def main(argv: list[str] | None = None) -> int:
    """Parse and dispatch, and never let a command die as a bare traceback.

    WHY THIS IS HERE, and what it deliberately does NOT do.

    On 2026-08-20 a `chmod 000` on a runtime-link target directory made
    `rollback` exit 1 with EMPTY STDOUT and a `PermissionError` stack trace — on
    BOTH the dry run and the execute path, because the execute path reaches
    verification through `rollback_release` -> `_activate_release_locked` ->
    `verify_release`. No race, no readlink, an ordinary persistent permission
    condition, on the emergency path, with production down. `Path.exists()`
    raises EACCES on 3.12, and only `ReleaseBoundaryError` was being caught.

    This catches EVERY command's unanticipated faults and reports them as JSON.
    It is deliberately at the dispatch boundary rather than at the three call
    sites that happened to be found: the faults that matter are the ones nobody
    enumerated, and a guard placed per-site can only ever cover the list someone
    thought of.

    IT CHANGES NO SEMANTICS. The command still fails and still exits non-zero.
    Whether verification may PROCEED when it cannot complete is a separate
    question that remains open and is not answered here — this only decides how
    a failure is reported, never whether it is one.
    """

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ReleaseBoundaryError:
        raise
    except Exception as exc:  # noqa: BLE001 - breadth is the point; see above
        command = getattr(args, "command", None) or "?"
        recovery = command in ("rollback", "repair-previous")
        _emit_err({
            "action": command,
            "executed": False,
            "classification": "UNEXPECTED_ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "why": ("this command failed for a reason the release boundary does not "
                    "classify. Nothing was reported as done that was not done; the "
                    "state on disk is whatever it was before the failure."),
            "next": ("RECOVERY DID NOT HAPPEN. Read the error above — a permission or "
                     "filesystem fault on the runtime-link targets (.env / .venv in the "
                     "development tree) is the common cause and is shared by every "
                     "release, so no pointer move will fix it. Check `status` before "
                     "retrying."
                     if recovery else
                     "read the error above; nothing was activated or removed."),
        })
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseBoundaryError as exc:
        print(json.dumps({"classification": exc.classification, "details": exc.details},
                         indent=2, sort_keys=True, default=str), file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:  # noqa: BLE001 - backstop for anything outside main()
        print(json.dumps({"classification": "UNEXPECTED_ERROR",
                          "error_type": type(exc).__name__, "error": str(exc)},
                         indent=2, sort_keys=True, default=str), file=sys.stderr)
        raise SystemExit(EXIT_UNEXPECTED)
