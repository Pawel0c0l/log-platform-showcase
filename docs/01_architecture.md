# Architecture

## Pozycja strategiczna

Platforma jest **wielokrotnego użytku**: ten sam runner, wewnętrzne API, Postgres (runy/logi/artefakty) i MinIO mogą obsługiwać zarówno **Workflow A** (główny, docelowy: API zewnętrzne → synchronizacja z bazą klienta), jak i **Workflow B** (backup: poczta / pliki / `ingest`). **Workflow A jest główną ścieżką biznesową w dokumentacji strategicznej; Workflow B jest podrzędny** (fallback, rozwój wstrzymany).

## Model wykonania (rdzeń platformy)

Przepływ (zgodny z kodem): runner ładuje `.env` z katalogu repo, tworzy klienta `LogPlatformClient.from_env()`, importuje moduł joba (`importlib`), uruchamia job wewnątrz `run_context` (api/client.py). W ramach `run_context`: `POST /runs` → run w statusie `RUNNING` → wykonanie `job.run(client, run_id, params)` → job wywołuje `client.log()` i `client.upload_artifact()` → przy wyjściu z kontekstu: `PATCH /runs/{id}` (`SUCCESS` lub `FAILED`). API zapisuje dane do Postgres (tabele runów/logów/artefaktów) i MinIO.

Ten model **nie jest** zarezerwowany wyłącznie dla ingestu pocztowego — dowolny job (w tym docelowe joby Workflow A) używa tej samej mechaniki, o ile implementacja to respektuje.

## Workflow A — docelowy przepływ (A1 / A2)

Workflow A jest częściowo zaimplementowany dla Telematics. Repo zawiera control-plane, onboarding, dwa produkcyjne joby danych (`sync_trips_and_speeding`, `aggregate_trip_fuel_daily`), dispatcher oraz worker retencji. Poniższy model A1/A2 pozostaje ogólnym modelem docelowym; szczegóły aktualnych jobów są w `docs/05_jobs.md`.

| Etap | Nazwa | Opis |
|------|--------|------|
| **A1** | Pozyskanie danych z API | Żądania HTTP do zewnętrznego API dostawcy/usługi, z uwierzytelnieniem i parametrami **specyficznymi dla konta klienta** (osobne sekrety, ewentualnie inne bazy URL / timeouty / limity). |
| **A2** | Synchronizacja z bazą klienta | Zapis do **głównej bazy danych danego klienta** w modelu **inkrementalnym / upsert**: utrzymywany jest identyfikator postępu lub znacznik „ostatniej udanej synchronizacji” oparty o semantykę aktualizacji po stronie źródła (np. `updated_at`, `last_modified`, wersja rekordu). |

### Wymagania operacyjne Workflow A

- **Per klient**: osobne konteksty uwierzytelniania i ewentualnie odrębne ustawienia połączenia do API (patrz `docs/02_infrastructure.md`, `docs/06_security.md`).
- **Wyzwalanie**:
  1. **Na żądanie** — uruchomienie z terminala (`ops/runner.py` + parametry).
  2. **Harmonogram hostowy** — np. systemd timer. Repo zawiera proponowane unity dla `jobs.api.telematics.dispatcher`, który czyta `workflow_a_control.client_dataset_schedule` i uruchamia najwyżej jeden due dataset na tick.
  3. **Na żądanie przez e‑mail** — mechanizm e‑mail → job jest **planowany / host-side**, nie jest kontraktem `api/main.py`.
- **Rozstrzyganie konfliktów (idempotentna synchronizacja po świeżości)**:
  - jeśli rekord w bazie klienta jest **nowszy** niż dane przychodzące z API → **zachować** rekord z bazy;
  - jeśli dane z API są **nowsze** → **nadpisać / zaktualizować** rekord w bazie;
  - jeśli znacznik czasu / wersji / „ostatniej aktualizacji” jest **taki sam** → uznać za już zsynchronizowane i **pominąć** aktualizację (chyba że osobna specyfikacja biznesowa dopuszcza bezpieczny wyjątek).

Formuła ta jest ogólnym oczekiwaniem dla nowych synchronizacji. Aktualne joby Telematics mają własne, udokumentowane zasady konfliktu: `client_dataset_schedule.overwrite_existing` przełącza `ON CONFLICT DO UPDATE` vs `DO NOTHING`; szczegóły są w `docs/05_jobs.md` i `docs/10_scheduler_design.md`.

## Workflow B — backup / legacy (Stage 1 → Stage 2 → Stage 3)

Workflow B to **ścieżka zapasowa** oparta o pocztę i pliki raportów oraz schemat `ingest`. **Rozwój jest wstrzymany**; dokumentacja zachowuje szczegóły dla operatorów i odtworzenia historycznego kontekstu.

- **Stage 1** (`jobs.mail.fetch_reports`): IMAP, normalizacja, deduplikacja — zapis do `ingest.*`.
- **Stage 2** (`jobs.reports.stage2.job_stage2`): detekcja typu, cleaning, walidacja, scoring — pola `stage2_*` w `ingest.raw_file`.
- **Stage 3**: uzupełnianie / aktualizacja bazy z przetworzonej ścieżki raportowej — w obecnym zestawie dokumentów **nie ma osobnego, kanonicznego opisu joba Stage 3**; traktuj jako **zamierzony etap łańcucha B**, niezaimplementowany lub poza tym repo, dopóki nie pojawi się jawna implementacja.

### Architektura Stage 1 ingest (Workflow B)

Dla maili raportowych (aktualny kod, ścieżka B):

- deduplikacja wiadomości IMAP po `(account, mailbox, uidvalidity, uid)`,
- kandydaci z załączników MIME,
- kandydaci-linki jako stuby (bez payloadu),
- preflight linku (`HEAD`, `Accept-Ranges`, `GET Range`) i `http_range_fp`,
- pominięcie downloadu, gdy istnieje canonical po `http_range_fp`,
- dla nowych payloadów: `sha256`, opcjonalny `content_fingerprint`, zapis RAW i normalizacja.

## Granice odpowiedzialności

API (wewnętrzne):

- trwałość danych **platformy** (`runs`, `logs`, `artifacts`),
- upload/download artefaktów,
- retencja (`/maintenance/prune`).

API **nie zastępuje** publicznego API dostawcy używanego w Workflow A ani nie jest „API integracyjnym” klienta końcowego — to warstwa observability i artefaktów dla jobów.

Runner:

- import joba i uruchomienie,
- lifecycle run (`run_context`),
- obsługa wyjątków i finalizacja statusu.

Job:

- logika biznesowa (Workflow A lub B),
- użycie klienta API przekazanego przez runner (logi, artefakty).

## Lifecycle run

Statusy dopuszczone przez API:

- `RUNNING`
- `SUCCESS`
- `FAILED`
- `CANCELED`

Praktyka przy uruchomieniu przez `ops/runner.py`:

- start: `RUNNING`
- brak wyjątku: `SUCCESS`
- wyjątek: `FAILED`

`ended_at` jest ustawiane przy statusach finalnych.
