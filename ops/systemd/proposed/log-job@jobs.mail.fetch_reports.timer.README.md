# Proposed systemd timer: jobs.mail.fetch_reports

Proponowany plik docelowy:
- `/etc/systemd/system/log-job@jobs.mail.fetch_reports.timer`

Kroki wdrożenia:

```bash
sudo cp ops/systemd/proposed/log-job@jobs.mail.fetch_reports.timer /etc/systemd/system/log-job@jobs.mail.fetch_reports.timer
sudo systemctl daemon-reload
sudo systemctl enable --now log-job@jobs.mail.fetch_reports.timer
systemctl status log-job@jobs.mail.fetch_reports.timer
```
