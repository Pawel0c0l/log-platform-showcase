#!/usr/bin/env bash
# Release-bound launcher for every non-job process: the operational sidecars
# (alert worker, watchdog) and the long-running services (API/Portal, Database
# Explorer export worker).
#
# Counterpart to log-job-runner.sh, for the processes that OBSERVE, SERVE and
# DELIVER rather than the jobs that produce. It answers exactly one question —
# "which tree supplies the Python modules this process imports?" — and answers
# it with the same immutable release pointer production jobs already resolve.
#
# Why a second launcher instead of reusing log-job-runner.sh: that wrapper is a
# *job* entry point. It reads jobs/config/<module>.json, parses a params JSON
# object, and execs `ops/runner.py`, which opens a `public.runs` lifecycle via
# run_context. None of the units routed here are jobs: they must not create run
# rows, must not take job params, and must not appear in the run history the
# watchdog is supposed to observe. Routing them through the job runner to obtain
# release resolution would buy the correct import path at the cost of corrupting
# the very dataset the watchdog reads.
#
# Deliberately NOT taken here, and both omissions are load-bearing:
#
#   * the execution-quiescence barrier (/run/lock/log-platform-execution.lock).
#     It serializes *job* execution against a wrapper cutover. Making the
#     watchdog and the alert worker wait on it would couple the observability
#     plane's availability to the data plane's maintenance window — and with
#     TimeoutStartSec=600 on the watchdog against a 900s barrier wait, a slow
#     cutover would turn into a failed watchdog and a false operator alert.
#     For the Type=simple services the omission is not a trade-off but a
#     requirement: the barrier is held for the launched process's whole
#     lifetime, so a daemon that took it would hold the shared side forever and
#     no cutover could ever acquire the exclusive side again.
#
#   * the cutover fence (state/cutover-state.txt). It answers "may production
#     jobs run at all?". When the answer is no, the alert worker still has to
#     drain queued incidents, the watchdog still has to observe that nothing is
#     running, and the Portal is the surface an operator uses to watch the
#     cutover. An observer that is silenced by the same switch that silences
#     what it observes is not an observer.
#
# The property that *is* needed — a process cannot have its code changed out
# from under it by a mid-flight release activation — comes from resolving the
# pointer once, below, and running from the resolved path. For a long-running
# service that property is what makes the contract "activate the pointer, then
# restart the unit": the running process keeps the release it loaded until an
# authorized restart, and the restart picks up whatever `current` names then.
set -euo pipefail

RELEASE_ROOT="/opt/log-platform-release"

# Exit codes are deliberately outside the range the launched modules use.
# ops/suspected_bug_email_worker.py exits 3 to mean "an alert reached
# dead_letter"; if this script also exited 3 for a broken release pointer, an
# operator reading ExecMainStatus=3 on suspected-bug-email-worker.service would
# diagnose a dead mail channel when the actual fault is a missing release.
EXIT_USAGE=2
EXIT_RELEASE_POINTER_INVALID=90
EXIT_RELEASE_RUNTIME_RESOURCE_MISSING=91
EXIT_RELEASE_ENTRYPOINT_MISSING=92

[[ $# -ge 1 ]] || {
  echo "Usage: log-ops-runner.sh <python.module.path> [module args...]" >&2
  exit "${EXIT_USAGE}"; }

MODULE="$1"
shift

# `python -m` takes its module name positionally, so a unit typo that produced a
# leading dash would silently become a python option — `-c`, worst case. The
# module name is unit-authored, not user input, but a launcher that turns a
# typo into arbitrary code execution is not one worth installing.
[[ "${MODULE}" =~ ^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$ ]] || {
  echo "OPS_RUNNER_MODULE_INVALID: '${MODULE}' is not a dotted python module path" >&2
  exit "${EXIT_USAGE}"; }

BASE_DIR="${RELEASE_ROOT}/current"

# Fail closed. `current` is swapped atomically by ops/release_boundary.py, so it
# either names one complete release or does not resolve at all. Refusing here
# turns a missing or half-promoted release into a failed unit — visible in
# `systemctl --failed`, in journald, and (for the watchdog) through its
# OnFailure= route — instead of a silent run against an unexpected tree.
[[ -L "${BASE_DIR}" ]] || {
  echo "RELEASE_POINTER_INVALID: ${BASE_DIR} is not a symlink" >&2
  exit "${EXIT_RELEASE_POINTER_INVALID}"; }

# Resolve once, then run from the resolved path rather than through the symlink,
# so an activation during this process's lifetime cannot change which code it is
# importing, and so the journal records the release identity rather than the
# constant string "current".
RESOLVED_DIR="$(readlink -f "${BASE_DIR}" || true)"
[[ -n "${RESOLVED_DIR}" && -d "${RESOLVED_DIR}" ]] || {
  echo "RELEASE_POINTER_INVALID: ${BASE_DIR} does not resolve" >&2
  exit "${EXIT_RELEASE_POINTER_INVALID}"; }

# Only a real release may be executed. Without this, `ln -sfn <dev tree> current`
# would satisfy every other guard and put the alerting path straight back on the
# mutable development checkout this launcher exists to escape.
[[ "${RESOLVED_DIR}" =~ ^"${RELEASE_ROOT}"/releases/[0-9a-f]{12}$ ]] || {
  echo "RELEASE_POINTER_INVALID: ${RESOLVED_DIR} is not a release under ${RELEASE_ROOT}/releases" >&2
  exit "${EXIT_RELEASE_POINTER_INVALID}"; }

BASE_DIR="${RESOLVED_DIR}"

# The dependency environment, which is a different question from code
# provenance. `.venv` is a plain virtualenv: no .pth file, no editable install,
# so it contributes site-packages and nothing else — the repository never
# appears on sys.path through it. That is why binding code to the release does
# not first require un-linking `.venv` from the development tree (P2-L).
VENV_PY="${BASE_DIR}/.venv/bin/python"
[[ -x "${VENV_PY}" ]] || {
  echo "RELEASE_RUNTIME_RESOURCE_MISSING: ${VENV_PY}" >&2
  exit "${EXIT_RELEASE_RUNTIME_RESOURCE_MISSING}"; }

# Unlike log-job-runner.sh, `.env` is deliberately NOT required unconditionally.
# The alerting sidecars take their configuration and secrets from
# EnvironmentFile=/etc/log-platform/*.env, never from a dotenv file in the tree,
# and no module in their import graph loads one. Demanding the release `.env`
# symlink for them would couple the alerting path to mutable development state
# for no benefit.
#
# Other units launched through this script do have per-unit prerequisites, and
# they are not the same set. The API imports `api.main`; the export worker
# imports `ops.database_export_worker`, which in turn imports `api.main`; and
# `api/platform_prune.py` reads the tree's own `.env` in-process to resolve the
# runtime identity. Each unit therefore declares what the release must actually
# contain, and this is where that declaration is enforced — after the pointer
# has resolved and before a single line of project code runs.
#
# Why check at all, when `python -m` would fail on a missing module anyway:
#   * `Restart=on-failure` on a long-running unit turns a structurally broken
#     release into a silent restart loop whose exit code (1, from an unhandled
#     ModuleNotFoundError) is indistinguishable from an ordinary crash;
#   * an entrypoint that is present but whose *support file* is missing — the
#     `.env` symlink, say — does not fail at import, it fails later, wrongly, or
#     silently degrades. `load_dotenv()` on an absent path is a no-op.
# Exit 92 says "the pointer resolved to a release that cannot serve this unit",
# which is a different operator action from 90 (pointer broken) and from any
# code the launched module owns.
#
# Requirements only ever ADD refusals, so an inherited or injected value cannot
# weaken the boundary — the worst it can do is refuse to start. Relative paths
# only: an absolute path or a `..` component would let the assertion be
# satisfied from outside the release, which is the one thing it must not allow.
if [[ -n "${OPS_RUNNER_REQUIRE_RELEASE_FILE:-}" ]]; then
  IFS=':' read -r -a __required <<< "${OPS_RUNNER_REQUIRE_RELEASE_FILE}"
  for __rel in "${__required[@]}"; do
    [[ -n "${__rel}" ]] || continue
    if [[ "${__rel}" == /* || "${__rel}" == *".."* ]]; then
      echo "OPS_RUNNER_REQUIREMENT_INVALID: '${__rel}' must be a release-relative path" >&2
      exit "${EXIT_USAGE}"
    fi
    # `-e` follows symlinks on purpose: the release `.env` and `.venv` are
    # symlinks by design, and a DANGLING one must fail here rather than
    # half-work later.
    [[ -e "${BASE_DIR}/${__rel}" ]] || {
      echo "RELEASE_ENTRYPOINT_MISSING: ${BASE_DIR}/${__rel}" >&2
      exit "${EXIT_RELEASE_ENTRYPOINT_MISSING}"; }
  done
fi

# The release tree is sealed read-only, so CPython cannot write __pycache__ next
# to the modules it imports. Redirect bytecode to a mutable state directory,
# exactly as log-job-runner.sh does, rather than recompiling on every fire.
PYCACHE_DIR="${RELEASE_ROOT}/state/pycache"
mkdir -p "${PYCACHE_DIR}"
export PYTHONPYCACHEPREFIX="${PYCACHE_DIR}"

# SET, never prepend. `ops` is a namespace package (no __init__.py), so a
# PYTHONPATH spanning both the release and the development checkout would let
# any ops.* module missing from one resolve out of the other — a silent
# mixed-version import. /etc/log-platform/runtime.env exports a PYTHONPATH
# naming the development tree and EnvironmentFile= beats Environment=, so this
# assignment must happen here, in the process, after systemd has applied the
# unit environment. It is the whole reason the units no longer carry
# Environment=PYTHONPATH= themselves.
export PYTHONPATH="${BASE_DIR}"

# The only operator-visible record of which release a fire actually executed.
echo "log-ops-runner release=$(basename "${BASE_DIR}") module=${MODULE} base_dir=${BASE_DIR}" >&2

# cd so that `python -m` puts the release — not an inherited working directory —
# at sys.path[0].
cd "${BASE_DIR}"
exec "${VENV_PY}" -m "${MODULE}" "$@"
