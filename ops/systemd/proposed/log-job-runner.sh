#!/usr/bin/env bash
set -euo pipefail

# Identity of this wrapper, compared against the on-disk copy after the
# barrier is acquired so a replacement mid-wait is detected.
WRAPPER_VARIANT="development"

# --- execution-quiescence barrier -------------------------------------------
# Take the shared side of the cutover barrier BEFORE resolving which tree to
# run, and hold it for this job's whole lifetime. Stopping the timers does not
# stop a manual `systemctl start` or a direct invocation of this script, so
# without this a job could resolve the old wrapper after the cutover's final
# gate and keep running the old mutable tree through the replacement.
#
# Shared locks stack, so ordinary production is unaffected: jobs never contend
# with each other, only with a cutover holding the exclusive side. fd 9 is an
# explicit descriptor, which bash does NOT mark close-on-exec, so the lock
# survives the exec below and is released by the kernel when the job exits.
RELEASE_ROOT_FOR_FENCE="/opt/log-platform-release"
EXEC_BARRIER="/run/lock/log-platform-execution.lock"
if ! { exec 9<>"${EXEC_BARRIER}"; } 2>/dev/null; then
  # A privileged process may own the file; flock needs only an open descriptor.
  { exec 9<"${EXEC_BARRIER}"; } 2>/dev/null || {
    echo "EXECUTION_BARRIER_UNAVAILABLE: cannot open ${EXEC_BARRIER}" >&2; exit 4; }
fi
# Wait rather than fail: during a cutover the right behaviour is to start after
# it finishes, against the new tree. The bound turns a stuck cutover into a
# unit failure (routed to the operator alert path) instead of a hung job.
# Distinguish "busy" (flock exit 1) from every other failure, so a missing
# binary or a broken descriptor does not page the operator about a cutover that
# is not happening.
# Capture the status before anything else can clobber $?. Inside `if ! cmd;`
# the shell has already replaced $? by the time the body runs, which made the
# BUSY branch unreachable and reported every failure as the same thing.
# `|| __flock_rc=$?` is required, not stylistic: under `set -e` a bare failing
# `flock` terminates the shell before the next line runs, so the status capture
# never happened and BOTH diagnostic branches were dead — a blocked job exited 1
# with an empty journal instead of exit 4 with a reason.
__flock_rc=0
flock --shared --wait 900 9 || __flock_rc=$?
if (( __flock_rc != 0 )); then
  if (( __flock_rc == 1 )); then
    echo "EXECUTION_BARRIER_BUSY: a cutover holds ${EXEC_BARRIER}; not starting" >&2
  else
    echo "EXECUTION_BARRIER_UNAVAILABLE: flock failed on ${EXEC_BARRIER} (rc=${__flock_rc})" >&2
  fi
  exit 4
fi

# The barrier says "no cutover is running right now"; the fence says whether
# production execution is permitted at all. They are different questions: a
# cutover that replaced the wrapper and then failed releases the barrier during
# its unwind, and without this check every job queued behind it would start
# against a wrapper nobody verified. Checked here — after the barrier, before
# any tree is resolved and before any project code runs.
CUTOVER_FENCE="${RELEASE_ROOT_FOR_FENCE}/state/cutover-state.txt"
if [[ -e "${CUTOVER_FENCE}" ]]; then
  __fence_state="$(sed -n 's/^STATE=\([A-Z_]*\)$/\1/p' "${CUTOVER_FENCE}" 2>/dev/null | head -n 1)"
  case "${__fence_state}" in
    ALLOWED|VERIFIED) : ;;
    "") echo "CUTOVER_FENCE_UNREADABLE: ${CUTOVER_FENCE} has no STATE; refusing" >&2; exit 5 ;;
    *)  echo "CUTOVER_FENCE_BLOCKS_EXECUTION: state=${__fence_state}; refusing to start" >&2
        exit 5 ;;
  esac
fi

# Re-exec if this script was replaced while we waited. `mv -f` is a rename, so a
# shell already executing the old file keeps reading the OLD inode: without this
# a job that blocked on the barrier during a cutover would resume and run the
# pre-cutover script text — resolving the old tree *after* the cutover reported
# success, which is worse than the race the barrier closes, because it is
# invisible. The declared variant above is compared with the one now on disk;
# /proc/self/fd/255 is not usable here because bash does not allocate it for a
# non-interactive script. fd 9 is not close-on-exec, so the barrier is retained
# across the re-exec, and the guard bounds it to one attempt.
__ondisk_variant="$(sed -n 's/^WRAPPER_VARIANT="\([a-z_]*\)"$/\1/p' "$0" 2>/dev/null | head -n 1)"
if [[ -n "${__ondisk_variant}" && "${__ondisk_variant}" != "${WRAPPER_VARIANT}" ]]; then
  # No environment marker guards this. A caller-supplied variable was able to
  # suppress the refresh, which is exactly the bypass an attacker or a stale
  # unit would use to keep the old inode running the mutable tree. Termination
  # comes from the comparison itself: after re-exec the executing wrapper IS the
  # file on disk, so the variants match and the branch is not taken again.
  echo "log-job-runner: wrapper replaced while waiting on the barrier"\
       " (${WRAPPER_VARIANT} -> ${__ondisk_variant}); re-executing" >&2
  exec "$0" "$@"
fi

BASE_DIR="/opt/log-platform"
VENV_PY="${BASE_DIR}/.venv/bin/python"
CONFIG_DIR="${BASE_DIR}/jobs/config"

USAGE="Usage: log-job-runner.sh <python.module.path> [json_params] [--runner-option ...]"
[[ $# -ge 1 ]] || { echo "${USAGE}" >&2; exit 2; }
JOB_MODULE="$1"
shift

# Separate the optional JSON parameter from explicit runner options.
#
# The final exec used to end at "${PARAMS_JSON}", so every argument after the
# JSON was discarded: an operator running
#   log-job-runner.sh <module> '<json>' <runner-option>
# got a silent legacy run that looked like it had opted in. Dropping an explicit
# operator instruction is worse than refusing it, because nothing says so.
#
# The literal option names are deliberately NOT written anywhere in this file.
# They live in `ops/runner.py`, and a repository-wide scan asserts that no unit,
# timer or script names the dashboard opt-in -- an invariant that protects
# scheduled execution and must not be broken by a comment.
#
# `ops/runner.py` owns the option contract -- which options exist, which modules
# they apply to, and which are refused -- so this only CLASSIFIES tokens and
# forwards them verbatim. No allowlist is kept here: a second one would drift
# from the runner's, and an unknown flag must still be refused by the runner
# rather than by the wrapper.
#
# Exactly one non-option argument is accepted, the JSON parameters. A second one
# is refused rather than ignored, because silent argument loss is precisely the
# defect this handling exists to remove.
CLI_ARG_RAW=""
__have_params=0
RUNNER_OPTIONS=()
for __arg in "$@"; do
  if [[ "${__arg}" == --* ]]; then
    RUNNER_OPTIONS+=("${__arg}")
  elif (( __have_params == 0 )); then
    CLI_ARG_RAW="${__arg}"
    __have_params=1
  else
    echo "${USAGE}" >&2
    echo "unexpected extra positional argument: ${__arg}" >&2
    exit 2
  fi
done
CFG_PATH="${CONFIG_DIR}/${JOB_MODULE}.json"
if [[ -f "${CFG_PATH}" ]]; then
  JOB_PARAMS_RAW="$(<"${CFG_PATH}")"
else
  if [[ -z "${CLI_ARG_RAW:-}" ]]; then
    JOB_PARAMS_RAW='{}'
  else
    JOB_PARAMS_RAW="${CLI_ARG_RAW}"
  fi
fi
PARAMS_JSON="$("${VENV_PY}" -c 'import json,sys; print(json.dumps(json.loads(sys.argv[1]), ensure_ascii=False))' "${JOB_PARAMS_RAW}")"
cd "${BASE_DIR}"
exec "${VENV_PY}" ops/run_with_environment_identity.py -- \
  "${VENV_PY}" ops/runner.py "${JOB_MODULE}" "${PARAMS_JSON}" \
  ${RUNNER_OPTIONS[@]+"${RUNNER_OPTIONS[@]}"}
