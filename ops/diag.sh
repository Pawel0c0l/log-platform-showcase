#!/usr/bin/env bash
set -euo pipefail

date
hostname
pwd

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git rev-parse --short HEAD
fi

docker compose ps
docker compose logs --tail=200 api

systemctl list-timers --all | grep -E "log-platform|log-job|log-backup" || true
systemctl --no-pager status \
  log-platform-prune.timer log-platform-prune.service \
  log-backup.timer log-backup.service \
  log-job@dispatcher.timer log-job@dispatcher.service \
  log-job@retention-purge.timer log-job@retention-purge.service || true
journalctl -u log-platform-prune.service -n 80 --no-pager || true
journalctl -u log-backup.service -n 80 --no-pager || true
journalctl -u log-job@dispatcher.service -n 80 --no-pager || true
journalctl -u log-job@retention-purge.service -n 80 --no-pager || true
