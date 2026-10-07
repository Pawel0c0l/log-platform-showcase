# Overview

Log Platform to **single-hostowy** system do wykonywania jobów, rejestrowania ich przebiegu (runy, logi, artefakty) oraz — w docelowym modelu — integracji z zewnętrznymi API i bazami klientów.

## Pozycjonowanie

| Warstwa | Opis |
|--------|------|
| **Rdzeń platformy (aktualny kod)** | Runner, wewnętrzne API, Postgres (runy/logi/artefakty), MinIO, retencja przez `/maintenance/prune`. Ten sam rdzeń może obsługiwać wiele typów jobów. |
| **Workflow A (główny kierunek strategiczny)** | A1: pobieranie danych z zewnętrznego API na konto klienta. A2: synchronizacja z **główną bazą danych tego klienta** w modelu inkrementalnym / upsert. Aktualnie repo zawiera joby Telematics (`sync_trips_and_speeding`, `aggregate_trip_fuel_daily`), control-plane, onboarding, DB-driven dispatcher oraz worker retencji. Komendy e‑mail i V2 joby pozostają **planned / not implemented** — patrz `docs/05_jobs.md`, `docs/10_scheduler_design.md`. |
| **Workflow B (backup / fallback)** | Legacy pipeline: Stage 1 (IMAP + normalizacja raportów), Stage 2 (detekcja typu, walidacja), przewidywany Stage 3 (uzupełnienie bazy z przetworzonych raportów). **Rozwój Workflow B jest wstrzymany**; ścieżka pozostaje jako zapasowa i jest nadal opisana oraz częściowo zaimplementowana w repo (`ingest`, joby mail/stage2). |

## Komponenty runtime

- Runner: `ops/runner.py`
- API: `api/main.py`
- DB runtime API: tabele `runs`, `logs`, `artifacts`
- DB Workflow B (ingest): schemat `ingest` (`ingest.imap_message`, `ingest.raw_file`) oraz kolumny Stage 2 — używane przez ścieżkę mail/raportową, nie definiują same w sobie Workflow A
- Storage artefaktów: MinIO/S3 (`artifacts` bucket)
- Scheduler hostowy: systemd (część unitów jest poza repo)

## Co robi system (rdzeń)

- tworzy i finalizuje runy (`RUNNING`, `SUCCESS`, `FAILED`, `CANCELED`)
- zapisuje logi strukturalne
- przechowuje artefakty binarne i metadane
- realizuje retencję przez endpoint `/maintenance/prune`

## Co robią workflow (koncepcja)

- **Workflow A (aktualnie dla Telematics + target dalej)**: joby powiązane z runnerem wykonują ręczne lub dispatcher-driven synchronizacje API → baza klienta; konfiguracja uwierzytelniania i endpointów jest per konto klienta w `workflow_a_control.client_account` (patrz `docs/02_infrastructure.md`, `docs/06_security.md`). V2 staging DDL istnieje jako declared-only, ale V2 joby i aktywne V2 registry rows nie są obecnie zaimplementowane.
- **Workflow B (aktualna implementacja części ścieżki)**: Stage 1 pobiera i normalizuje raporty z IMAP; Stage 2 wykonuje detekcję typu raportu, walidację i scoring; zapis pól `stage2_*` do `ingest.raw_file`. Stage 3 (wypełnianie bazy z tej ścieżki) w repo **nie jest w pełni opisany jako osobny job** — traktuj jako część docelowej, wstrzymanej ścieżki B, o ile nie pojawi się dedykowana dokumentacja implementacji.

## Zakres dokumentacji

Kod jest źródłem prawdy dla **zachowania już zaimplementowanego**. Dokumentacja opisuje:

- aktualne endpointy i ich faktyczne zachowanie,
- aktualne migracje i model danych związany z **Workflow B** (`ingest`, stage2),
- wymagane ENV dla API, runnera i maintenance (w tym zmienne specyficzne dla jobów B),
- podział: repo vs host,
- elementy istniejące vs **planned / target-state / not yet implemented** — zwłaszcza dla Workflow A i per-klientowej izolacji.
