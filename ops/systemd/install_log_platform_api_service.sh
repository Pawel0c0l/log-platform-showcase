#!/usr/bin/env bash
# PRE-RELEASE-BOUNDARY BOOTSTRAP INSTALLER — does NOT produce the production unit.
#
# The unit this generates names a repository checkout as WorkingDirectory and
# runs that checkout's `.venv/bin/uvicorn`, so editing the checkout changes what
# the service executes at its next restart. That is the provenance defect
# ops/release_boundary.py exists to remove, and it is why this helper must not be
# used to (re)install the service on a host that has a release root.
#
# On such a host the authoritative unit is
# ops/systemd/proposed/log-platform-api.service, installed as-is: it resolves the
# active release per process start through /usr/local/bin/log-ops-runner.sh. The
# guard below refuses rather than relying on the reader noticing this comment.
#
# See docs/07_operations.md § "Release boundary".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SOURCE_EXAMPLE="${REPO_ROOT}/ops/systemd/log-platform-api.service.example"

service_name="log-platform-api"
service_user="$(id -un)"
service_group="$(id -gn)"
workdir="${REPO_ROOT}"
env_file=""
venv_path="${REPO_ROOT}/.venv"
host="127.0.0.1"
port="8000"
enable_service=false
restart_service=false
assume_yes=false
dry_run=false
allow_dev_tree_binding=false
release_root="/opt/log-platform-release"

usage() {
  cat <<'EOF'
Usage: ops/systemd/install_log_platform_api_service.sh [options]

Install or preview the systemd unit for the Log Platform API and Portal.

Options:
  --service-name NAME   systemd service name without or with .service suffix (default: log-platform-api)
  --user USER           service user (default: current user)
  --group GROUP         service group (default: current primary group)
  --workdir PATH        repository working directory (default: detected repo root)
  --env-file PATH       systemd EnvironmentFile (default: repo .env when present, else /etc/log-platform/api.env)
  --venv PATH           Python virtualenv directory containing bin/uvicorn (default: <repo>/.venv)
  --host HOST           uvicorn bind host (default: 127.0.0.1)
  --port PORT           uvicorn bind port (default: 8000)
  --enable              run systemctl enable after install
  --restart             run systemctl restart after install
  --yes                 do not prompt before writing /etc/systemd/system
  --dry-run             print the generated unit and commands without writing or running systemctl
  --allow-development-tree-binding
                        install even though this host has a release root; the
                        service will execute the mutable checkout instead of the
                        active release (see docs/07_operations.md)
  -h, --help            show this help

This helper never reads or prints .env contents. Run migrations and readiness
checks separately before/after restarting the service:

  ./ops/db_migrate.sh
  PYTHONPATH="$PWD" python3 ops/checks/check_portal_ready.py
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service-name)
      service_name="${2:-}"
      shift 2
      ;;
    --user)
      service_user="${2:-}"
      shift 2
      ;;
    --group)
      service_group="${2:-}"
      shift 2
      ;;
    --workdir)
      workdir="${2:-}"
      shift 2
      ;;
    --env-file)
      env_file="${2:-}"
      shift 2
      ;;
    --venv)
      venv_path="${2:-}"
      shift 2
      ;;
    --host)
      host="${2:-}"
      shift 2
      ;;
    --port)
      port="${2:-}"
      shift 2
      ;;
    --enable)
      enable_service=true
      shift
      ;;
    --restart)
      restart_service=true
      shift
      ;;
    --yes)
      assume_yes=true
      shift
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    --allow-development-tree-binding)
      allow_dev_tree_binding=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "${env_file}" ]]; then
  if [[ -f "${REPO_ROOT}/.env" ]]; then
    env_file="${REPO_ROOT}/.env"
  else
    env_file="/etc/log-platform/api.env"
  fi
fi

service_name="${service_name%.service}"
target_unit="/etc/systemd/system/${service_name}.service"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_nonempty() {
  local name="$1"
  local value="$2"
  [[ -n "${value}" ]] || fail "${name} must not be empty."
}

require_no_newline() {
  local name="$1"
  local value="$2"
  if [[ "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
    fail "${name} must not contain newlines."
  fi
}

require_nonempty "service-name" "${service_name}"
require_nonempty "user" "${service_user}"
require_nonempty "group" "${service_group}"
require_nonempty "workdir" "${workdir}"
require_nonempty "env-file" "${env_file}"
require_nonempty "venv" "${venv_path}"
require_nonempty "host" "${host}"
require_nonempty "port" "${port}"

for pair in \
  "service-name:${service_name}" \
  "user:${service_user}" \
  "group:${service_group}" \
  "workdir:${workdir}" \
  "env-file:${env_file}" \
  "venv:${venv_path}" \
  "host:${host}" \
  "port:${port}"; do
  require_no_newline "${pair%%:*}" "${pair#*:}"
done

[[ "${service_name}" != */* ]] || fail "service-name must not contain '/'."
[[ "${port}" =~ ^[0-9]+$ ]] || fail "port must be numeric."
[[ -f "${SOURCE_EXAMPLE}" ]] || fail "Missing source service example: ${SOURCE_EXAMPLE}"
[[ -d "${workdir}" ]] || fail "WorkingDirectory does not exist: ${workdir}"
[[ -f "${env_file}" ]] || fail "EnvironmentFile does not exist: ${env_file}"

uvicorn_bin="${venv_path}/bin/uvicorn"
if [[ "${dry_run}" == "false" && ! -x "${uvicorn_bin}" ]]; then
  fail "uvicorn executable does not exist or is not executable: ${uvicorn_bin}"
fi

unit_content="$(cat <<EOF
[Unit]
Description=Log Platform API and Portal
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${service_user}
Group=${service_group}
WorkingDirectory=${workdir}
EnvironmentFile=${env_file}
EnvironmentFile=/etc/log-platform/environment-identity.env
ExecStart=${uvicorn_bin} api.main:app --host ${host} --port ${port}
Restart=on-failure
RestartSec=5
TimeoutStartSec=30
TimeoutStopSec=30
UMask=0077

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=false
ReadWritePaths=${workdir}

[Install]
WantedBy=multi-user.target
EOF
)"

echo "INFO: Source example: ${SOURCE_EXAMPLE}"
echo "INFO: Target unit: ${target_unit}"
echo "INFO: Service will bind to ${host}:${port}"
echo "INFO: EnvironmentFile path: ${env_file} (contents are not read or printed)"

if [[ "${dry_run}" == "true" ]]; then
  echo
  echo "===== DRY RUN: generated ${service_name}.service ====="
  printf '%s\n' "${unit_content}"
  echo "===== DRY RUN: commands that would run ====="
  echo "sudo install -m 0644 <generated-unit> ${target_unit}"
  echo "sudo systemctl daemon-reload"
  if [[ "${enable_service}" == "true" ]]; then
    echo "sudo systemctl enable ${service_name}.service"
  fi
  if [[ "${restart_service}" == "true" ]]; then
    echo "sudo systemctl restart ${service_name}.service"
  fi
  echo
  echo "Follow-up checks:"
  echo "sudo systemctl status ${service_name} --no-pager"
  echo "journalctl -u ${service_name} -n 100 --no-pager"
  exit 0
fi

# Fail closed on a release-boundary host. Reaching this point means the operator
# is about to overwrite a live unit with one whose code source is a mutable
# checkout; on a host that maintains an active release that is a regression, not
# a configuration choice. `--dry-run` is deliberately above this line so the
# generated unit can still be inspected.
if [[ "${allow_dev_tree_binding}" != "true" && -L "${release_root}/current" ]]; then
  cat >&2 <<EOF
ERROR: RELEASE_BOUNDARY_PRESENT — refusing to install a development-tree-bound unit.

  ${release_root}/current -> $(readlink "${release_root}/current" 2>/dev/null || echo '?')

This helper generates a unit whose WorkingDirectory and interpreter live in
${workdir}, so an edit there changes what the service runs at its next restart.
On this host the authoritative unit is release-bound:

  sudo install -m 0644 \
    "${REPO_ROOT}/ops/systemd/proposed/log-platform-api.service" \
    ${target_unit}

Pass --allow-development-tree-binding to override deliberately.
EOF
  exit 1
fi

if [[ "${assume_yes}" != "true" ]]; then
  echo
  echo "The following unit will be installed to ${target_unit}:"
  printf '%s\n' "${unit_content}"
  echo
  read -r -p "Install or overwrite ${target_unit}? [y/N] " answer
  case "${answer}" in
    y|Y|yes|YES)
      ;;
    *)
      echo "Aborted; no files were changed."
      exit 1
      ;;
  esac
fi

tmp_unit="$(mktemp)"
trap 'rm -f "${tmp_unit}"' EXIT
printf '%s\n' "${unit_content}" > "${tmp_unit}"

sudo install -m 0644 "${tmp_unit}" "${target_unit}"
sudo systemctl daemon-reload

if [[ "${enable_service}" == "true" ]]; then
  sudo systemctl enable "${service_name}.service"
fi

if [[ "${restart_service}" == "true" ]]; then
  sudo systemctl restart "${service_name}.service"
fi

echo "Installed ${target_unit}."
echo
echo "Recommended follow-up:"
echo "  sudo systemctl status ${service_name} --no-pager"
echo "  journalctl -u ${service_name} -n 100 --no-pager"
echo
echo "Run migrations/readiness separately when needed:"
echo "  ./ops/db_migrate.sh"
echo "  PYTHONPATH=\"\$PWD\" python3 ops/checks/check_portal_ready.py"
