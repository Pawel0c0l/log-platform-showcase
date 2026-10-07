# Architecture Overview

Dokument pomocniczy. Szczegóły implementacyjne są w:
- `docs/01_architecture.md`
- `docs/03_api_spec.md`
- `docs/04_runner.md`
- `docs/05_jobs.md`

## Pozycjonowanie przepływów

- **Workflow A (główny kierunek strategiczny, częściowo docelowy)**: zewnętrzne API → synchronizacja z bazą klienta (joby na runnerze; Telematics sync/aggregation, dispatcher i retencja są w repo; szczegóły w `docs/01_architecture.md`, `docs/05_jobs.md`, `docs/10_scheduler_design.md`).
- **Workflow B (backup / legacy, rozwój wstrzymany)**: IMAP / pliki raportów → `ingest` → Stage 2 (i przewidywany Stage 3). Nadal wspierany przez istniejące migracje i joby w repo, ale **nie** jest traktowany jako główny model operacyjny.

## Topologia

- `ops/runner.py` (host) wywołuje joby `jobs.*`
- job korzysta z `api/client.py` (wewnętrzne API platformy)
- API (`api/main.py`) zapisuje metadane runów/logów/artefaktów do PostgreSQL oraz bloby do MinIO

## Storage

- PostgreSQL:
  - **runtime platformy**: `runs`, `logs`, `artifacts`
  - **Workflow B (ingest)**: `ingest.imap_message`, `ingest.raw_file` (oraz kolumny Stage 2)
  - **Workflow A (docelowo)**: główne bazy danych klientów mogą być **poza** tą samą instancją/schematem co `logdb` — konfiguracja i backup to odpowiedzialność operatora, o ile nie zostanie ujednolicona w repo
- MinIO:
  - payload artefaktów uploadowanych przez `/artifacts/upload` (dowolne joby, w tym B; artefakty związane z raportami często linkowane przez `raw_file_id` do `ingest.raw_file`)

## Dostęp sieciowy (wg `docker-compose.yml`)

- API: `127.0.0.1:8000`, Postgres: `127.0.0.1:5432`
- MinIO: porty `9000:9000`, `9001:9001` (bez `127.0.0.1:` — nasłuch na wszystkich interfejsach)

## Harmonogram

W repo są tylko wybrane unity i proponowane szablony:

- `ops/systemd/log-backup.service`
- `ops/systemd/log-backup.timer`
- `ops/systemd/proposed/log-job@jobs.mail.fetch_reports.timer`
- `ops/systemd/proposed/log-job@dispatcher.{service,timer}`
- `ops/systemd/proposed/log-job@retention-purge.{service,timer}`

**Tryby wykonania (koncepcja operacyjna):**

1. **Cyklicznie** — systemd timer lub inny scheduler na hoście.
2. **Na żądanie** — ręczne `ops/runner.py` (terminal); docelowo także wyzwolenie po komendzie z e‑mail (implementacja po stronie hosta / **planowane**).
3. **DB-driven dispatcher** — `jobs.api.telematics.dispatcher` może być uruchamiany timerem hostowym i odpala najwyżej jeden due dataset na tick.

Finalne unity uruchamiane na hoście (np. `log-job@.service`, prune, docelowe timery dla Workflow A) nie są w pełni wersjonowane tutaj.
