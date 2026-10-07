#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SNAPSHOT_DIR="$REPO_ROOT/snapshots"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SNAPSHOT_FILE="$SNAPSHOT_DIR/snapshot_${TIMESTAMP}.txt"

mkdir -p "$SNAPSHOT_DIR"

append_header() {
  local title="$1"
  {
    echo
    echo "===== ${title} ====="
  } >> "$SNAPSHOT_FILE"
}

append_block() {
  local content="$1"
  printf '%s\n' "$content" >> "$SNAPSHOT_FILE"
}

append_cmd() {
  local label="$1"
  shift

  {
    echo
    echo "--- ${label} ---"
  } >> "$SNAPSHOT_FILE"

  if output="$("$@" 2>&1)"; then
    printf '%s\n' "$output" >> "$SNAPSHOT_FILE"
  else
    printf '%s\n' "$output" >> "$SNAPSHOT_FILE"
  fi
}

is_forbidden_path() {
  local path="$1"

  case "$path" in
    *.env|.env|*.pem|id_rsa|id_rsa.pub|id_rsa_*|authorized_keys|*.key|*.crt|secrets*|/home/*/.ssh/*)
      return 0
      ;;
  esac

  return 1
}

sanitize_line() {
  local line="$1"
  if [[ "$line" == *"="* ]] && printf '%s\n' "$line" | grep -Eiq '(TOKEN|PASSWORD|SECRET|KEY)'; then
    printf '%s\n' "${line%%=*}=***"
  else
    printf '%s\n' "$line"
  fi
}

append_file_contents() {
  local path="$1"

  {
    echo
    echo "--- FILE: ${path} ---"
  } >> "$SNAPSHOT_FILE"

  if is_forbidden_path "$path"; then
    echo "SKIPPED: forbidden path pattern" >> "$SNAPSHOT_FILE"
    return
  fi

  if [[ ! -e "$path" ]]; then
    echo "MISSING" >> "$SNAPSHOT_FILE"
    return
  fi

  if [[ ! -r "$path" ]]; then
    echo "UNREADABLE: brak uprawnień" >> "$SNAPSHOT_FILE"
    if [[ "$path" == /etc/systemd/system/* ]]; then
      echo "Aby odczytać: sudo cat $path" >> "$SNAPSHOT_FILE"
    fi
    return
  fi

  while IFS= read -r line || [[ -n "$line" ]]; do
    sanitize_line "$line"
  done < "$path" >> "$SNAPSHOT_FILE"
}

: > "$SNAPSHOT_FILE"

append_header "Meta"
append_cmd "date" date
append_cmd "hostname" hostname
append_cmd "user" whoami
append_cmd "pwd" pwd
append_cmd "uname -a" uname -a

append_header "Git"
if git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  append_cmd "git remote -v" git -C "$REPO_ROOT" remote -v
  append_cmd "git branch --show-current" git -C "$REPO_ROOT" branch --show-current
  append_cmd "git rev-parse HEAD" git -C "$REPO_ROOT" rev-parse HEAD
  append_cmd "git status -sb" git -C "$REPO_ROOT" status -sb
  append_cmd "git log --oneline -20" git -C "$REPO_ROOT" log --oneline -20
else
  append_block "Not a git repository"
fi

append_header "Project tree"
if command -v tree >/dev/null 2>&1; then
  append_cmd "tree -L 4" tree -L 4 "$REPO_ROOT"
else
  append_cmd "find . -maxdepth 4 -type d -print" find "$REPO_ROOT" -maxdepth 4 -type d -print
fi

append_header "Key files contents"
append_file_contents "$REPO_ROOT/docker-compose.yml"
append_file_contents "$REPO_ROOT/api/main.py"
append_file_contents "$REPO_ROOT/api/client.py"
append_file_contents "$REPO_ROOT/ops/runner.py"
append_file_contents "$REPO_ROOT/ops/diag.sh"
append_file_contents "$REPO_ROOT/ops/smoke.sh"
append_file_contents "$REPO_ROOT/ops/backup.sh"
append_file_contents "$REPO_ROOT/docs/00_overview.md"
append_file_contents "$REPO_ROOT/docs/07_operations.md"
append_file_contents "$REPO_ROOT/docs/09_disaster_recovery.md"
append_file_contents "/etc/systemd/system/log-job@.service"
append_file_contents "/etc/systemd/system/log-platform-prune.timer"
append_file_contents "/etc/systemd/system/log-platform-prune.service"

append_header "Docker status"
if command -v docker >/dev/null 2>&1; then
  if docker compose version >/dev/null 2>&1; then
    docker_ps_output="$(docker compose -f "$REPO_ROOT/docker-compose.yml" ps 2>&1 || true)"
    {
      echo
      echo "--- docker compose ps ---"
      printf '%s\n' "$docker_ps_output"
    } >> "$SNAPSHOT_FILE"

    if printf '%s\n' "$docker_ps_output" | grep -qiE 'permission denied.*docker.sock|docker.sock.*permission denied'; then
      echo "INFO: brak uprawnień do /var/run/docker.sock; dodaj user do grupy docker" >> "$SNAPSHOT_FILE"
    else
      docker_logs_output="$(docker compose -f "$REPO_ROOT/docker-compose.yml" logs --tail=200 api 2>&1 || true)"
      {
        echo
        echo "--- docker compose logs --tail=200 api ---"
        printf '%s\n' "$docker_logs_output"
      } >> "$SNAPSHOT_FILE"

      if printf '%s\n' "$docker_logs_output" | grep -qiE 'permission denied.*docker.sock|docker.sock.*permission denied'; then
        echo "INFO: brak uprawnień do /var/run/docker.sock; dodaj user do grupy docker" >> "$SNAPSHOT_FILE"
      fi
    fi
  else
    append_block "docker compose is not available"
  fi
else
  append_block "docker is not available"
fi

append_header "Systemd timers"
if command -v systemctl >/dev/null 2>&1; then
  append_cmd "systemctl list-timers | grep -E \"log-platform|log-job\" || true" bash -lc 'systemctl list-timers | grep -E "log-platform|log-job" || true'
  append_cmd "systemctl --no-pager status log-platform-prune.timer" systemctl --no-pager status log-platform-prune.timer
else
  append_block "systemctl is not available"
fi

append_header "Last errors"
if command -v journalctl >/dev/null 2>&1; then
  append_cmd "journalctl -u log-platform-prune.service -n 80 --no-pager" journalctl -u log-platform-prune.service -n 80 --no-pager
else
  append_block "journalctl is not available"
fi

echo "Snapshot saved to: $SNAPSHOT_FILE"
