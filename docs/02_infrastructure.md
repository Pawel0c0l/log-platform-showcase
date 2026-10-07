# Infrastructure

> **RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION.** Command lines of the
> form `PYTHONPATH="$PWD" python3 ops/runner.py …` are development / local /
> debug only — they execute the mutable working tree. The supported production
> entrypoint is the installed wrapper
> `/usr/local/bin/log-job-runner.sh <module> '<json>'`. See
> `docs/07_operations.md` -> *Release boundary*.


## Lokalizacja projektu

`/opt/log-platform` — repozytorium deweloperskie.

`/opt/log-platform-release` — release root (pinned,
read-only drzewa per commit + wskaźniki `current` / `previous`). Mechanizm jest
**aktywny od 2026-08-11**: zainstalowany `/usr/local/bin/log-job-runner.sh` to
wariant `release`, a fence ma `STATE=VERIFIED`. Aktualne wskaźniki `current` /
`previous` odczytuj **wyłącznie** przez `ops/manage_release.py status` — nie
ufaj identyfikatorom wpisanym w dokumentację (stan 2026-08-28 po aktywacji o 19:08:55 UTC: `current =
d49c7c718c59`, `previous = adb9750ca5d9`). Boundary obejmuje trzy powierzchnie
jobowe idące przez ten wrapper (dispatcher Workflow A, Workflow B, retention
purge), a od 2026-08-20 również `log-platform-api.service`,
`database-export-worker.service` i `database-export-cleanup.service`, które
wykonują aktywny release przez `/usr/local/bin/log-ops-runner.sh`
(`docs/07_operations.md` → *Release boundary*). Drzewo deweloperskie pozostaje wymagane
również po cutoverze — `.env` i `.venv` są do niego dowiązane, więc `pip
install` i edycja `.env` nadal sięgają produkcji natychmiast. Rozdział dev /
release / active runtime, prowenancja, promocja, rollback i procedura cutoveru:
`docs/07_operations.md` → *Release boundary*.

## Docker Compose (`docker-compose.yml`)

Pliki Compose:

- `docker-compose.yml` — definicja bazowa i **jedyna** definicja produkcyjna;
- `docker-compose.dev.yml` — nakładka wyłącznie deweloperska, ładowana **tylko** jawnie.

Compose automatycznie ładuje jedynie `docker-compose.override.yml` / `compose.override.yaml`. Repozytorium celowo nie używa tych nazw, więc zwykłe `docker compose ...` zawsze rozwiązuje się do bezpiecznej definicji produkcyjnej i nie może po cichu przejąć deweloperskiego bind-mounta. Pilnuje tego `ops/tests_manual/test_docker_api_execution_boundary.py`.

Serwisy:

- `postgres`
- `minio`
- `api`

Kontener `api` w definicji bazowej uruchamia kod aplikacji **wyłącznie z obrazu** `log-platform-api:latest` (budowanego z kontekstu `./api`): nie ma bind-mounta `./api:/app`, proces działa jako `user: "1000:1000"`, z `cap_drop: [ALL]` i `security_opt: [no-new-privileges:true]`. Dzięki temu digest obrazu identyfikuje kod API, który faktycznie się wykonuje, a odtworzenie kontenera przez uprzywilejowany proces nie uruchamia zapisywalnego dla operatora checkoutu. Modele Pythona są w obrazie własnością `root` i nie są zapisywalne dla użytkownika runtime; `api/Dockerfile` normalizuje uprawnienia, bo cztery moduły `api/eco_driving_explorer/admin_*.py` mają w checkoucie tryb `0600`.

Komendy:

```bash
# produkcja (odtworzenie tylko API)
docker compose -f docker-compose.yml up -d --no-deps --force-recreate api

# development (przywraca podgląd źródeł na żywo przez ./api:/app)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

> **Stan wdrożenia (2026-08-09): wdrożone i zweryfikowane na produkcji.** Poprzedni runtime (UID 0, zapisywalny bind `./api:/app`, pełne domyślne capabilities) został zastąpiony. Działający kontener produkcyjny wykonuje kod z obrazu, bez bind-mounta źródeł i bez żadnego montowania hosta, jako `1000:1000`, z wyzerowanymi wszystkimi zestawami capabilities, `NoNewPrivs=1` i `Privileged=false`, nadal na `127.0.0.1:8000`. Wdrożenie odtworzyło **wyłącznie** serwis `api` — PostgreSQL, MinIO i natywny `log-platform-api.service` pozostały nietknięte.
>
> Atestacją runtime jest **niezmienne ID obrazu** `sha256:c718529f89ad2a822f5f014de6f8cdafa6db2f5227637c8fa4176757f7a6df46`, a nie tag `log-platform-api:latest` — tag jest zmiennym wskaźnikiem i sam w sobie niczego nie dowodzi. Sprawdzaj ID na działającym kontenerze. Materiał rollbackowy (poprzedni obraz **oraz** poprzednia definicja bind-owa) jest celowo zachowany do osobnej decyzji operatora. Szczegóły: `docs/17_production_hardening_roadmap.md` § 5.6.

Artefakty są przechowywane fizycznie w MinIO/S3 (`MINIO_BUCKET`, domyślnie `artifacts`), a ich indeks metadanych znajduje się w tabeli platformowej `artifacts` w Postgresie. Od migracji `022_artifact_layout_metadata.sql` nowe uploady używają layoutu `layout_version=2`; migracja `023_artifacts_client_code.sql` dodaje opcjonalne `artifacts.client_code` pod filtrowanie per klient. Migracja `024_artifact_annotations.sql` dodaje `artifact_metadata_overrides` i `artifact_tags` na manualne opisy, manualne metadata JSON i tagi. Migracja `026_artifact_virtual_folders.sql` dodaje `artifact_virtual_folders` i `artifact_virtual_folder_items` jako metadata nawigacji; migracja `027_artifact_smart_folders.sql` dodaje kolumny `folder_type` (`manual` \| `smart`) oraz `search_query_json` (zapisane kryteria jak w Artifact Browser) dla smart folderów — nadal bez zmiany fizycznych kluczy MinIO. `layout_version=1` oznacza historyczny fizyczny układ obiektu; `layout_version=2` oznacza standardowy klucz budowany przez API. Historyczne obiekty mogą pozostać w starym układzie i z `client_code=NULL`.

Artifact Browser API (`/artifact-browser/*`) działa nad tym samym duetem Postgres metadata + MinIO object storage oraz tabelami manualnych adnotacji. Obsługuje filtry wielowartościowe i endpoint facets dla pól o skończonych wartościach, w tym `client_code` i tagów. Nie wymaga nowych zmiennych ENV ani osobnego storage backendu.

`ops/backfill_artifact_metadata.py` jest hostowym narzędziem operatorskim do bezpiecznego uzupełniania metadanych starszych wierszy `artifacts`. Aktualizuje tylko kolumny metadata w Postgresie; nie przenosi, nie kopiuje i nie usuwa obiektów MinIO.

Artifact Preview API (`/artifact-browser/artifacts/{artifact_id}/preview`) używa tego samego backendu. Dla preview arkuszy API image instaluje `openpyxl` i `xlrd`; limity preview są stałymi w kodzie, nie zmiennymi ENV.

Artifact Explorer UI (`/artifact-explorer`) jest server-rendered w tym samym procesie FastAPI. Nie dodaje osobnego frontendu, katalogu statycznego ani build pipeline. Od Phase 6 używa lokalnych użytkowników/RBAC i podpisanego cookie sesyjnego; dla stabilnych sesji ustaw `ARTIFACT_EXPLORER_SESSION_SECRET`.

Porty hosta:

- `127.0.0.1:5432` -> postgres
- `127.0.0.1:8000` -> api
- MinIO: w `docker-compose.yml` porty `9000:9000` i `9001:9001` (bez prefiksu `127.0.0.1:`), więc nasłuch na wszystkich interfejsach.

## ENV matrix (aktualny kod i docelowy Workflow A)

### API container

Wymagane przez `docker-compose.yml` + `api/main.py`:

- `POSTGRES_HOST` (container default from Compose: `postgres`; host/systemd example: `127.0.0.1`)
- `POSTGRES_PORT`
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `MINIO_ENDPOINT`
- `MINIO_BUCKET`
- `MINIO_SECURE`
- `MINIO_ROOT_USER`
- `MINIO_ROOT_PASSWORD`
- `API_READ_TOKEN`
- `API_WRITE_TOKEN`
- `BUSINESS_TIMEZONE` (optional; default `Europe/Warsaw`; must be an IANA timezone name)
- `ARTIFACT_EXPLORER_SESSION_SECRET` (**required by the production Compose API service**; also derives Database Explorer opaque row references. If missing, the API uses a process-local development secret: sessions *and* copied `row=` URLs are invalidated on restart. Local development may keep that fallback; production deployment may not)
- `LOG_PLATFORM_TARGET_ENVIRONMENT` and `LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID`
- `LOG_PLATFORM_EXPECTED_POSTGRES_PORT`, `LOG_PLATFORM_EXPECTED_POSTGRES_DB`, and `LOG_PLATFORM_EXPECTED_POSTGRES_USER`
- `LOG_PLATFORM_COMPOSE_EXPECTED_POSTGRES_HOST` (optional Compose-scoped expected host; default `postgres`)

Uwaga:

- `POSTGRES_HOST` wewnątrz kontenera API jest ustawione na stałe jako `postgres`.
- Local Docker Compose API storage keeps the Docker-DNS MinIO endpoint. Host prune selects `MINIO_HOST_ENDPOINT` first, otherwise preserves a host-compatible `MINIO_ENDPOINT`, and maps only the exact Compose default to the established host loopback endpoint. It never rewrites arbitrary hostnames. Host-level async export worker/cleanup systemd services retain their protected host-endpoint override. Do not replace the Docker API value with host loopback, because loopback inside the API container is the container itself, not MinIO.
- API-created Postgres sessions execute `SET TIME ZONE` via `set_config('TimeZone', BUSINESS_TIMEZONE, false)`. This changes timestamptz display/session interpretation only; it does not rewrite stored timestamptz values.
- API deployment mode still has no generic `APP_ENV` or debug variable. Guarded operational D105.2 commands use the separate fail-closed identity contract below; it does not change API routing or authentication.

Minimum production portal/API environment for a host systemd service should contain real values for:

```text
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=logdb
POSTGRES_USER=<platform_db_user>
POSTGRES_PASSWORD=<platform_db_password>
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_BUCKET=artifacts
MINIO_SECURE=0
MINIO_ROOT_USER=<minio_user>
MINIO_ROOT_PASSWORD=<minio_password>
API_READ_TOKEN=<long random token>
API_WRITE_TOKEN=<long random token>
ARTIFACT_EXPLORER_SESSION_SECRET=<long random secret>
BUSINESS_TIMEZONE=Europe/Warsaw
LOG_API_URL=http://127.0.0.1:8000
```

`LOG_API_URL` is used by host-side checks/jobs and should point at the local API bind address unless the host intentionally routes through a reverse proxy. Do not put plaintext one-time portal bootstrap passwords in this file; use `PORTAL_ADMIN_PASSWORD` only as a transient shell variable with `scripts/bootstrap_portal_admin.py --password-env`.

Portal row-browse, export, preview and audit limits are code constants in `api/main.py`, not ENV variables. Current notable database limits include rows page max `500`, direct database export cap `20000`, background database export cap `1000000`, and audit page max `500`.

Artifact preview limits include object max `25 MB`, preview rows default `1000`, and preview rows max `5000`.

### Portal operations scripts

The local UI portals (`/artifact-explorer`, `/user`, `/admin`) do not add persistent ENV variables beyond the shared API/session configuration above. First-admin bootstrap can use a one-time host shell variable, for example:

```bash
export PORTAL_ADMIN_PASSWORD='<temporary strong password>'
PYTHONPATH="$PWD" python3 scripts/bootstrap_portal_admin.py --username admin --password-env PORTAL_ADMIN_PASSWORD
unset PORTAL_ADMIN_PASSWORD
```

`PORTAL_ADMIN_PASSWORD` is not an API/container setting and should not be committed to `.env`; it is only a transient input to the operator script. Portal readiness checks use the existing platform `POSTGRES_*` variables and print only table/admin status summaries, not passwords, tokens, DSNs, or row values.

The Client Database Explorer connects to per-client business databases mapped by `portal_clients.database_name` (a bare allowlisted PostgreSQL database name, not a DSN). It introduces **no new ENV variables**: the client-DB connection reuses the same platform `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD` and only swaps the database name (it does not use `POSTGRES_DB=logdb` for this path), opening a read-only session. The portal role must have `CONNECT` on each mapped client database, `USAGE` on approved schemas, and `SELECT` on approved tables/views; the portal performs no auto-grants or writes.

### Runner / joby na hoście — wspólne (rdzeń platformy)

Wymagane przez `ops/runner.py` i `api/client.py` dla każdego joba korzystającego z platformy:

- `LOG_API_URL`
- `API_READ_TOKEN`
- `API_WRITE_TOKEN`
- `BUSINESS_TIMEZONE` (optional; default `Europe/Warsaw`; used by log context, artifact metadata, report timestamp parsing, and app-created Postgres sessions)

Business-facing timestamps use `Europe/Warsaw` by default and DST is handled by IANA timezone rules. Internal UTC/timestamptz storage remains timezone-aware; code must not emulate local time by adding fixed `+1h` / `+2h` offsets.

### suspected_bug alerts (platform-wide)

Read by `api/suspected_bug.py` (reporting and enqueue policy) and `ops/suspected_bug_email_worker.py` (delivery). All are optional; defaults in brackets. Reporting and incident persistence never depend on them — only email delivery does.

| Variable | Default | Meaning |
|---|---|---|
| `SUSPECTED_BUG_ALERT_TO` | *(unset)* | Comma/semicolon-separated recipient list. **No fallback**: unset means email is suppressed as `recipients_not_configured` while the log and incident are still written. |
| `SUSPECTED_BUG_ALERTS_ENABLED` | `true` | Master switch; `false` suppresses enqueue as `alerts_disabled`. |
| `SUSPECTED_BUG_ALERT_FROM` | *(inherits)* | Optional sender override; otherwise `AUTOMATION_SMTP_FROM` from the shared SMTP transport is used. |
| `SUSPECTED_BUG_ALERT_COOLDOWN_MINUTES` | `120` | Minimum silence between alerts for one unchanged incident. |
| `SUSPECTED_BUG_ALERT_REMINDER_HOURS` | `24` | Reminder interval for a still-recurring incident; `0` disables reminders. |
| `SUSPECTED_BUG_ALERT_SCOPE_GROWTH_FACTOR` | `2` | Affected-record growth factor that counts as a material change (default: scope must at least double). |
| `SUSPECTED_BUG_EMAIL_MAX_ATTEMPTS` | `6` | Delivery attempts before an outbox row is dead-lettered. |
| `SUSPECTED_BUG_EMAIL_INITIAL_RETRY_SECONDS` | `60` | First retry delay; doubles per attempt. |
| `SUSPECTED_BUG_EMAIL_MAX_RETRY_SECONDS` | `3600` | Upper bound of the exponential backoff. |
| `SUSPECTED_BUG_EMAIL_WORKER_BATCH_SIZE` | `10` | Rows claimed per worker run. |
| `SUSPECTED_BUG_EMAIL_STALE_CLAIM_SECONDS` | `900` | Claim lease; expired `sending` rows return to the retry queue. |

SMTP transport itself reuses the existing `AUTOMATION_SMTP_*` variables. `LOG_PLATFORM_TARGET_ENVIRONMENT` supplies the `environment` shown in the alert subject, and `ARTIFACT_EXPLORER_BASE_URL` (when set) turns artifact ids into Artifact Explorer links. No SMTP credential is ever stored in the incident or outbox tables.

### Workflow B — Stage 1 (`jobs.mail.fetch_reports`) i powiązane

Poniższe zmienne są używane przez **aktualny** job pocztowy i ingest (`ingest.*`). Nie definiują Workflow A; stanowią konfigurację **ścieżki backupowej B**.

Wymagane/obsługiwane m.in. przez `jobs/mail/fetch_reports.py` (dokładna lista w kodzie):

- `IMAP_HOST` (required)
- `IMAP_PASSWORD` (required)
- `IMAP_PORT` (default `993`)
- `IMAP_USER` (default `automations.scheduled@example.invalid`)
- `IMAP_MAILBOX` (default `INBOX`)
- `IMAP_SENDER_FILTERS` (opcjonalnie; comma-separated lista nadawców dla ręcznie zawężonego fetchu; domyślnie puste, czyli bez filtrowania nadawcy)
- `IMAP_SENDER_FILTER` (legacy pojedynczy nadawca; używany tylko, gdy `IMAP_SENDER_FILTERS` nie jest ustawione; domyślnie puste)
- `REPORTS_DATA_DIR` (default `/home/logplatform/data/reports`)
- `POSTGRES_HOST` (default `127.0.0.1`)
- `POSTGRES_PORT` (default `5432`)
- `POSTGRES_DB` (default `logdb`)
- `POSTGRES_USER` (default `loguser`)
- `POSTGRES_PASSWORD` (default empty)
- `CONTENT_DEDUP_MODE` (`off|tables_70_90`, default `tables_70_90`)
- `CONTENT_DEDUP_RANGE_START` (default `0.70`)
- `CONTENT_DEDUP_RANGE_END` (default `0.90`)
- `REPORT_LINK_DOMAINS_ALLOWLIST`
- `REPORT_LINK_MAX_BYTES`
- `REPORT_LINK_TIMEOUT_SECS`
- `REPORT_LINK_VERIFY_TLS`
- `REPORT_LINK_PREFLIGHT` (default enabled)
- `REPORT_LINK_RANGE_SAMPLE_BYTES` (default `65536`)

Stage 1 obsługuje załączniki `.csv`, `.xls`, `.xlsx` i `.xlsm`. Dla `.xlsm` nie wykonuje makr; normalizuje workbook przez `openpyxl` w trybie read-only/data-only i preferuje arkusz `LOG`. To wspiera emailowy przepływ Alpha GPS z załącznikiem wysyłanym przez VBA z `owner@example.invalid`.

Domyślna kwerenda IMAP jest folder-scoped i szeroka: po `SELECT <IMAP_MAILBOX>` używa tylko `SINCE <date>`. Nie filtruje po `To`, `Cc`, `Delivered-To`, `From`, temacie ani adresie skrzynki. Folder/label pozostaje ograniczeniem wejścia.

### Workflow B — Stage 2 job (`jobs.reports.stage2.job_stage2`)

- `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (jak wyżej)
- `REPORTS_DATA_DIR` (opcjonalnie; domyślnie katalog nad `DEFAULT_CANONICAL_DIR` z kodu, np. `.../data/reports`) — Stage 1 zapisuje tu `raw/` i `normalized/`; Stage 2 pobiera ścieżki kwalifikujących się plików z `ingest.raw_file`
- `AUTOMATION_SMTP_HOST`, `AUTOMATION_SMTP_PORT`, `AUTOMATION_SMTP_USERNAME`, `AUTOMATION_SMTP_PASSWORD`, `AUTOMATION_SMTP_USE_TLS`, `AUTOMATION_SMTP_FROM` — konfiguracja SMTP dla powiadomień Stage 2 o raportach wymagających manual review po niskiej pewności detekcji; brak lub błąd SMTP jest logowany jako ostrzeżenie i nie cofa wyników Stage 2
- `STAGE2_PENDING_REVIEW_NOTIFY_TO` (default `owner@example.invalid`) — adresaci powiadomienia zbiorczego Stage 2
- `ARTIFACT_EXPLORER_BASE_URL` (default `http://localhost:8000`) — publiczny/operacyjny base URL używany do linków Artifact Explorer w wiadomości

Stage 2 runtime zapisuje wyniki do `ingest.raw_file` i artefaktów. Dodatkowo platformowa baza po migracjach `019_workflow_b_report_type_registry.sql`, `020_workflow_b_report_registry_detection_contract.sql`, `021_workflow_b_report_207_registry.sql` i `041_workflow_b_d105_2_ecodriving_registry.sql` zawiera schemat `workflow_b_control` z tabelą `report_type_registry`, czyli read model aktualnych typów raportów, cleanerów i maszynowo czytelnego kontraktu detekcji (`detection_rules_schema_version`, `column_types`, `multi_table`, `cleaner_entrypoint`). Ta tabela nie wymaga nowych zmiennych ENV i nie jest jeszcze źródłem prawdy runtime dla detektora Stage 2.

Stage 2 uploaduje nowe artefakty przez standardowe API z metadanymi `workflow_b/stage_2_clean`; cleaned CSV dostają `artifact_role=cleaned`, a pliki pomocnicze dla `PENDING_REVIEW` dostają `artifact_role=debug_sample`. Przy `low_detection_confidence` wiersz `ingest.raw_file.stage2_report_type` nadal przechowuje najlepszy kandydat diagnostyczny, ale artefakt pomocniczy dostaje `artifacts.report_type=PENDING_REVIEW`, żeby lista Artifact Explorer pokazywała status bez otwierania szczegółów.

Od migracji `028_workflow_b_stage2_client_code_record_id.sql` Stage 2 czyta z `workflow_b_control.report_type_registry` dwa operatorskie pola finalizacji: `id_sync_column_name` oraz `record_id_ingredients`. Gdy `id_sync_column_name` jest skonfigurowane, proces Stage 2 musi mieć dostęp do tych samych sekretów `client_db_password_secret_ref`, które są zapisane w `workflow_a_control.client_account`, ponieważ sprawdza wartości w bazach klientów Workflow A. Nie dodaje to nowych nazw ENV, ale hostowy proces Stage 2 musi widzieć odpowiednie env-var secret refs albo pliki `file:` używane przez onboardowane konta klientów.

### Workflow B — Stage 3 job (`jobs.reports.stage3.job_stage3`)

Stage 3 używa standardowego runnera i tych samych zmiennych platformowych:

- `LOG_API_URL`, `API_READ_TOKEN`, `API_WRITE_TOKEN` — do logów oraz pobierania/uploadu artefaktów przez API platformy,
- `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` — do odczytu `ingest.raw_file`, `artifacts`, `workflow_b_control.report_type_client_load_policy` i `workflow_a_control.client_account`.

Nie dodaje nowych nazw ENV. Docelowe bazy klientów są rozwiązywane z `workflow_a_control.client_account` po `client_code`, a hasło bazy klienta jest pobierane przez istniejący `client_db_password_secret_ref` (`ENV_VAR` albo `file:/abs/path`). Hostowy proces Stage 3 musi więc mieć dostęp do tych samych sekretów baz klientów co joby Workflow A i finalizacja Stage 2.

Stage 3 wymaga przygotowanych w bazach klientów schematu `telematics_reports`, tabel nazwanych kanonicznym `stage2_report_type`, kolumn technicznych i indeksów `record_id`. Przygotowanie wykonuje adminowa migracja `db/client_business/042_workflow_b_stage3_runtime_schema.sql`; recurring runtime nie wykonuje DDL. Wyjątek: `Alpha_GPS_Baza_LOG` ładuje do istniejącego, migrowanego schematu/tabeli `telematics_reports."Alpha_GPS_Baza_LOG"` w bazie Alpha. Polityka overwrite jest zapisana w platformowej tabeli `workflow_b_control.report_type_client_load_policy`; brak wiersza oznacza `data_overwrite=false`.

`ops/grant_workflow_b_stage3_permissions.py --apply` pozostaje kontrolowanym, pozajobowym helperem membership/`CONNECT` i DML na istniejącym schemacie; nie nadaje `CREATE`. `auto_grant_permissions=true` jest odrzucane przez runtime. Import nadal używa `client_db_user` i `client_db_password_secret_ref`.

### Workflow B — `report_207` post-processing job

`jobs.reports.postprocess.job_report_207_speeding_migration` używa standardowego runnera i tych samych zmiennych/sekretów co Stage 3: platformowe `POSTGRES_*` do odczytu `workflow_a_control.client_account` oraz `client_db_password_secret_ref` do połączeń z bazami klientów jako `client_db_user`. Job nie dodaje własnych nazw ENV; respektuje wspólne `BUSINESS_TIMEZONE`. Przed połączeniem do bazy klienta wymaga `client_account.trip_metrics_population_source='report_207_migration'`; przy innym źródle raportuje skip z `skip_reason=trip_metrics_population_source_mismatch`.

`telematics_reports.report_207."Data i czas"` jest timestampem lokalnym FleetWeb bez timezone. Job interpretuje go jako `Europe/Warsaw` przez `AT TIME ZONE 'Europe/Warsaw'` przed porównaniem z `public.client_trips.start_timestamp/end_timestamp` (`timestamptz`).

Job nie dodaje kolumn w runtime. Przed realnym runem migracja 042 musi zapewnić w bazie klienta:

- `telematics_reports.report_207`: `migrated_to_client_db`, `migrated_to_client_db_at`, `migrated_to_client_trip_id`, `migrated_to_client_db_error`
- `public.client_trips`: `speeding_140_160_count`, `speeding_160_170_count`, `speeding_170_plus_count`

Brak wymaganej tabeli, kolumny albo indeksu jest non-retryable/operator-action-required i wskazuje migrację 042. Dry-run jest read-only wobec baz klientów i raportuje brakujące obiekty oraz planowane liczniki.

### Workflow B — D105.2 EcoDriving trip metrics post-processing job

`jobs.reports.postprocess.job_d105_2_ecodriving_trip_metrics_migration` używa standardowego runnera i tych samych zmiennych/sekretów co Stage 3 oraz `report_207`: platformowe `POSTGRES_*` do odczytu `workflow_a_control.client_account` oraz `client_db_password_secret_ref` do połączeń z bazami klientów jako `client_db_user`. Job nie dodaje własnych nazw ENV; respektuje wspólne `BUSINESS_TIMEZONE`. Przed jakimkolwiek permission bootstrapem albo połączeniem do bazy klienta wymaga `client_account.trip_metrics_population_source='d105_2_ecodriving_migration'`; przy innym źródle raportuje skip z `skip_reason=trip_metrics_population_source_mismatch`.

Tabela docelowa Stage 3 to `telematics_reports.report_d105_2_ecodriving`. W realnym runie job może dodać kolumny trackingowe do tej tabeli (`migrated_to_client_db`, `migrated_to_client_db_at`, `migrated_to_client_trip_id`, `migrated_to_client_db_error`) oraz brakujące kolumny metryk w `public.client_trips`: `speeding_140_160_count`, `speeding_160_170_count`, `speeding_170_plus_count`, `overrev_events_count`. Nie dodaje i nie pisze `high_rpm_events_count`.

Dry-run i `ops/checks/check_d105_2_ecodriving_trip_metrics.py` są read-only wobec baz klientów i tylko raportują source selector, wymagane kolumny, kandydatów, parse/match categories i planowane przyrosty.

### Workflow A — Telematics control-plane i sekrety

Aktualne joby Workflow A (`jobs.api.telematics.*`) czytają konfigurację klienta z platformowej bazy `workflow_a_control.client_account`.

- `provider_basic_auth_username`, `provider_base_url`, `client_db_*`, `client_code` są wartościami konfiguracyjnymi w DB.
- `trip_metrics_population_source` wybiera jedno źródło prawdy dla event-derived metryk `public.client_trips` (speeding buckety oraz HIGH_RPM/OVERREV): `api_migration`, `report_207_migration`, `d105_2_ecodriving_migration` albo `disabled`. Default migracji `040_workflow_a_trip_metrics_population_source.sql` to `api_migration`; to pole jest control-plane DB, nie ENV.
- `provider_basic_auth_password_secret_ref` i `client_db_password_secret_ref` są referencjami do sekretów, nie sekretami. Obsługiwane formy:
  - nazwa zmiennej środowiskowej, np. `DELTA_API_KEY`,
  - `file:/abs/path/to/secret.txt`.
- Hostowy proces uruchamiający runnera/dispatcher musi mieć dostęp do tych env-varów albo plików.
- `scripts/onboard_workflow_a_client.py` używa konwencji `<CLIENT_NAME>_API_USERNAME`, `<CLIENT_NAME>_API_KEY`, `<CLIENT_NAME>_DB_USERNAME`, `<CLIENT_NAME>_DB_KEY` i zapisuje do control-plane referencje do sekretów (`*_API_KEY`, `*_DB_KEY`), nigdy wartości.

### Workflow A — Eco Driving weekly email notifications

`jobs.ecodriving.job_eco_driving_weekly_email_notifications` uses the standard runner ENV plus the client DB secret referenced by `workflow_a_control.client_account.client_db_password_secret_ref`. It does not call Telematics; it reads already calculated weekly stats from the client business DB and sends HTML email through SMTP.

Required for non-dry-run sends:

- `ECO_WEEKLY_EMAIL_SMTP_HOST`
- `ECO_WEEKLY_EMAIL_SMTP_PORT`
- `ECO_WEEKLY_EMAIL_SMTP_USERNAME`
- `ECO_WEEKLY_EMAIL_SMTP_PASSWORD`
- `ECO_WEEKLY_EMAIL_SMTP_USE_TLS` (default `true`)
- `ECO_WEEKLY_EMAIL_SMTP_USE_SSL` (default `false`; cannot be true together with STARTTLS)
- `ECO_WEEKLY_EMAIL_FROM_EMAIL` (default `no-reply.eco-alpha@example.invalid`)
- `ECO_WEEKLY_EMAIL_FROM_NAME` (default `Program Ecodriving`)
- `ECO_WEEKLY_EMAIL_REPLY_TO` (optional)
- `ECO_WEEKLY_EMAIL_TIMEOUT_SECONDS` (default `30`)

For `dry_run=true`, SMTP host/auth variables, including `ECO_WEEKLY_EMAIL_SMTP_PASSWORD`, are not required and the job does not connect to SMTP.

### Workflow A — Eco Driving Person email notifications

`jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications` and `jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications` use the standard runner ENV plus the client DB secret referenced by `workflow_a_control.client_account.client_db_password_secret_ref`. They read already calculated `eco_person_*` stats from the client business DB and send through a separate SMTP ENV namespace so BRAVO00016/person-workflow settings do not alter ALPHA00001 sends.

Required for non-dry-run sends:

- `ECO_PERSON_EMAIL_SMTP_HOST`
- `ECO_PERSON_EMAIL_SMTP_PORT`
- `ECO_PERSON_EMAIL_SMTP_USERNAME`
- `ECO_PERSON_EMAIL_SMTP_PASSWORD`
- `ECO_PERSON_EMAIL_SMTP_USE_TLS` (default `true`)
- `ECO_PERSON_EMAIL_SMTP_USE_SSL` (default `false`; cannot be true together with STARTTLS)
- `ECO_PERSON_EMAIL_FROM_EMAIL` (default `no-reply.ecodriving@example.invalid`)
- `ECO_PERSON_EMAIL_FROM_NAME` (default `Program Ecodriving`)
- `ECO_PERSON_EMAIL_REPLY_TO` (optional)
- `ECO_PERSON_EMAIL_TIMEOUT_SECONDS` (default `30`)

For `dry_run=true`, SMTP host/auth variables, including `ECO_PERSON_EMAIL_SMTP_PASSWORD`, are not required and the jobs do not connect to SMTP.

BRAVO00016 sends are further isolated from the generic person namespace. When `client_code='BRAVO00016'`, **both** `jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications` and `jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications` resolve the dedicated `BRAVO_ECO_WEEKLY_EMAIL_*` namespace directly. Neither relies on temporary shell aliases to `ECO_PERSON_EMAIL_*`.

The sender identity follows the **client**, not the reporting period: `BRAVO_ECO_WEEKLY_EMAIL_*` names the BRAVO00016 sender mailbox (the `WEEKLY` in the variable names is historical), and one account, one password and one Sent folder serve the weekly and the monthly report alike. Every other person client keeps `ECO_PERSON_EMAIL_*`.

Required BRAVO00016 SMTP values for non-dry-run sends (weekly and monthly):

- `BRAVO_ECO_WEEKLY_EMAIL_SMTP_HOST`
- `BRAVO_ECO_WEEKLY_EMAIL_SMTP_PORT`
- `BRAVO_ECO_WEEKLY_EMAIL_SMTP_USERNAME`
- `BRAVO_ECO_WEEKLY_EMAIL_SMTP_PASSWORD`
- `BRAVO_ECO_WEEKLY_EMAIL_SMTP_USE_TLS`
- `BRAVO_ECO_WEEKLY_EMAIL_SMTP_USE_SSL`
- `BRAVO_ECO_WEEKLY_EMAIL_FROM_EMAIL`
- `BRAVO_ECO_WEEKLY_EMAIL_FROM_NAME`
- `BRAVO_ECO_WEEKLY_EMAIL_REPLY_TO` (optional; blank means no `Reply-To` header)
- `BRAVO_ECO_WEEKLY_EMAIL_TIMEOUT_SECONDS`

Required BRAVO00016 Sent-folder copy values (weekly and monthly):

- `BRAVO_ECO_WEEKLY_EMAIL_IMAP_HOST`
- `BRAVO_ECO_WEEKLY_EMAIL_IMAP_PORT`
- `BRAVO_ECO_WEEKLY_EMAIL_IMAP_USE_SSL`
- `BRAVO_ECO_WEEKLY_EMAIL_IMAP_TIMEOUT_SECONDS` (optional; default `30`)
- `BRAVO_ECO_WEEKLY_EMAIL_IMAP_USERNAME` (optional override; see below)
- `BRAVO_ECO_WEEKLY_EMAIL_IMAP_PASSWORD` (optional override; see below)
- `BRAVO_ECO_WEEKLY_EMAIL_IMAP_SENT_MAILBOX` (optional; omit when the mailbox advertises exactly one IMAP `\Sent` special-use folder)

**One mailbox is configured once.** The Sent copy is filed in the mailbox the message was sent *from*, so IMAP authenticates as the account that authenticated to SMTP: `{PREFIX}_IMAP_USERNAME` and `{PREFIX}_IMAP_PASSWORD` are **optional overrides**, and when they are absent `{PREFIX}_SMTP_USERNAME` and `{PREFIX}_SMTP_PASSWORD` of the same prefix are used. A password is never duplicated between the two namespaces, and no credential is copied into a new variable. The IMAP **endpoint** is not inherited this way: host, port and SSL mode describe a different service, must be stated explicitly, and are never derived from the SMTP ones. Missing credentials from *both* sources, or an IMAP host with no port or SSL mode, remain a fail-closed configuration error.

For ALPHA weekly and monthly (`ECO_WEEKLY_EMAIL_*`) this means Sent archiving needs only `ECO_WEEKLY_EMAIL_IMAP_HOST`, `_IMAP_PORT` and `_IMAP_USE_SSL` on top of the SMTP configuration that is already deployed.

The BRAVO00016 sender identity is:

```text
Header From: "Ecodriving Telematics" <automations@example.invalid>
SMTP envelope sender: automations@example.invalid
```

After SMTP accepts a message, the job appends the exact RFC 5322 MIME bytes to the sender mailbox Sent folder and verifies the copy by the same `Message-ID`. The Sent folder is discovered from IMAP `\Sent` special-use metadata unless an explicitly verified `BRAVO_ECO_WEEKLY_EMAIL_IMAP_SENT_MAILBOX` is configured. SMTP delivery and Sent archiving are tracked separately in the `eco_person_weekly_email_send_log` archive columns added by `db/client_business/041_eco_person_sent_archive_state.sql`.

### Deprecated direct ALPHA00001 Alpha GPS XLSM import

`jobs.alpha.import_gps_baza_log_xlsm` jest zachowany tylko jako awaryjna/manualna kompatybilność starej ścieżki `source_path`; operacyjnie Alpha GPS używa teraz Workflow B email attachment. Stary job nie dodaje nowych nazw ENV. Używa
standardowego runnera (`LOG_API_URL`, `API_READ_TOKEN`, `API_WRITE_TOKEN`),
platformowych `POSTGRES_*` do odczytu `workflow_a_control.client_account` oraz
istniejącego `client_db_password_secret_ref` dla `ALPHA00001`, żeby połączyć się
z bazą `alpha_main` jako `client_db_user`.

Ścieżka do zsynchronizowanego workbooka jest parametrem joba `source_path`, a
nie zmienną ENV. Docelowy DDL jest w `db/client_business/022_alpha_gps_baza_log.sql`.

### Notes for Workflow A Phase 2 (`jobs.api.telematics.sync_trips_and_speeding`)

- Baza biznesowa klienta musi mieć finalny schemat `client_trips` z
  `db/client_business/020_client_trips_final_schema.sql` oraz
  `db/client_business/021_add_trip_mode_to_client_trips.sql`. Nowy onboarding
  stosuje te DDL bez tworzenia starej tabeli `client_trips`; istniejące bazy
  aktualizuje się przez `scripts/apply_client_business_migrations.py`.
- Eco Driving support tables are client-business DDL in `db/client_business/027_eco_driving_schema.sql` through `db/client_business/035_eco_driving_monthly_email_notifications.sql`. `032_*` adds the weekly email send log table; `033_*` adds send-log audit columns and grants standard client-business `SELECT, INSERT, UPDATE` permissions; `034_*` adds the rating-type group share reporting field; `035_*` adds the monthly email send log table and matching grants. Existing clients receive them through `scripts/apply_client_business_migrations.py`, and new clients receive them during onboarding.
- `provider_basic_auth_password_secret_ref` oraz `client_db_password_secret_ref` są resolved na hoście przez:
  - env-var o tej samej nazwie, albo
  - `file:/path/to/secret.txt` (odczyt treści pliku).
- opcjonalne parametry requestów do Telematics:
  - `TELEMATICS_PROVIDER_TIMEOUT_S` (default `60`) — timeout HTTP na pojedyncze żądanie GET
  - `TELEMATICS_PROVIDER_PAGE_LIMIT` (default `1000`) — `limit` w standardowej paginacji, m.in. `/trips` i batch-safe inventory `GET /vehicles`
  - `TELEMATICS_PROVIDER_VEHICLE_EVENTS_LIMIT` (default `1000`, capped at `1000`) — `limit` dla fleet-wide `/vehicles/events` używanego do liczników speeding i HIGH_RPM/OVERREV
  - `TELEMATICS_PROVIDER_VEHICLE_EVENTS_MAX_PAGES_PER_DAY` (default `500`) — twardy cap stron dla jednego adaptive chunka `/vehicles/events`
  - `TELEMATICS_EVENTS_CHUNK_HOURS` (default `4`) — początkowy rozmiar chunka czasowego dla `/vehicles/events`
  - `TELEMATICS_EVENTS_MIN_CHUNK_MINUTES` (default `30`) — minimalny rozmiar chunka po redukcji na timeout/provider failure
  - `TELEMATICS_EVENTS_TIMEOUT_S` (default jak `TELEMATICS_PROVIDER_TIMEOUT_S`) — timeout dla pojedynczego requestu `/vehicles/events`
  - `TELEMATICS_EVENTS_RATE_LIMIT_RPS` (default `2.5`) — globalny limit tempa żądań Telematics w tym jobie
  - `TELEMATICS_EVENTS_ENRICHMENT_MODE` (default effective mode `enabled` ze strategią fetch `strict`) — tryb event enrichment: `enabled` albo `disabled`; legacy strategie `strict` i `audited_best_effort` są nadal akceptowane, a parametr joba `event_enrichment_mode` ma pierwszeństwo nad ENV
  - `TELEMATICS_EVENTS_ENABLE_REGISTRATION_FALLBACK` (default `false`) — włącza awaryjny fallback per rejestracja dla terminalnie nieudanego fleet-wide chunka `/vehicles/events`
  - `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_RPS` (default `1.0`) — throttling sekwencyjnego fallbacku per rejestracja
  - `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REGISTRATIONS` (default `1500`) — twardy limit liczby rejestracji w fallbacku
  - `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_CHUNKS` (default `1`) — maks. liczba fleet chunków, które mogą przejść przez fallback w jednym runie
  - `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REQUESTS_PER_RUN` (default jak `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_RUN`, musi być ustawiony jawnie, jeśli fallback ma przekroczyć globalny budżet)
  - `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES` (default `5`) — minimalny subchunk dla jednej rejestracji po nieudanym dużym fallback window
  - `TELEMATICS_EVENTS_BEST_EFFORT_MIN_FLEET_CHUNK_MINUTES` (default `5`) — minimalne fleet-wide okno, które w trybie `audited_best_effort` może zostać zapisane jako unresolved gap
  - `TELEMATICS_EVENTS_BEST_EFFORT_MIN_REGISTRATION_CHUNK_MINUTES` (default `5`) — minimalne per-registration okno dla gapów w trybie `audited_best_effort`
  - `TELEMATICS_EVENTS_BEST_EFFORT_MAX_GAPS_PER_RUN` (default `1000`) — maks. liczba gap records w jednym runie; przekroczenie kończy run błędem
  - `TELEMATICS_EVENTS_BEST_EFFORT_MAX_SPLIT_DEPTH` (default `8`) — maks. głębokość rekursywnego dzielenia; przekroczenie kończy run błędem
  - `LOG_PLATFORM_ARTIFACT_TMP_DIR` (default `/tmp`) — katalog bazowy dla tymczasowego artefaktu `vehicle_events_gap_audit.json` w trybie `audited_best_effort`

**Phase 2 — twarde limity bezpieczeństwa (ochrona przed pętlą / runaway pagination):**

Cel: **zatrzymać lokalnie** podejrzane lub nieograniczone zachowanie HTTP wobec API dostawcy, zanim zużyje token / quota klienta. Domyślne wartości są **konserwatywne**; nawet przy dużym oknie wykonania job nie powinien „wisić” w nieskończonej paginacji.

| Zmienna | Domyślna wartość | Znaczenie |
|--------|-------------------|-----------|
| `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_RUN` | `500` | Maks. liczba żądań HTTP GET do Telematics w **jednym** runie joba (suma `/trips` + batch-safe `/vehicles` inventory + fleet-wide `/vehicles/events`; legacy/diagnostic endpoints such as `/alerts/notifications` also consume the same budget when used). |
| `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_ENDPOINT` | `300` | Maks. żądań na **jeden** endpoint (`/trips`, `/vehicles`, `/vehicles/events` lub endpoint diagnostyczny/legacy, np. `/alerts/notifications`) w jednym runie. |
| `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_SUBWINDOW` | `80` | Maks. żądań na endpoint **w jednym** sub-oknie (≤31 dni). |
| `TELEMATICS_PROVIDER_MAX_PAGES_PER_SUBWINDOW` | `50` | Maks. **udanych** stron paginacji (po odpowiedzi HTTP) na endpoint w jednym sub-oknie. |
| `TELEMATICS_PROVIDER_MAX_RETRIES` | `2` | Dodatkowe próby na **jedno** żądanie (po pierwszej próbie) wyłącznie przy timeout / connection error; łącznie max `1 + MAX_RETRIES` prób. |
| `TELEMATICS_PROVIDER_TIMEOUT_S` | `60` | Timeout na pojedyncze żądanie GET. |
| `TELEMATICS_PROVIDER_VEHICLE_EVENTS_LIMIT` | `1000` | `limit` wysyłany do fleet-wide `GET /vehicles/events`; wartości powyżej `1000` są capped do `1000`. |
| `TELEMATICS_PROVIDER_VEHICLE_EVENTS_MAX_PAGES_PER_DAY` | `500` | Twardy cap stron dla jednego adaptive chunka fleet-wide `/vehicles/events`; w strict all-or-nothing flow osiągnięcie capu jest traktowane jako niekompletny chunk i kończy run błędem przed DB upsert. |
| `TELEMATICS_EVENTS_CHUNK_HOURS` | `4` | Początkowy rozmiar chunka czasowego dla `/vehicles/events`. |
| `TELEMATICS_EVENTS_MIN_CHUNK_MINUTES` | `30` | Minimalny rozmiar chunka po redukcji (`4h → 2h → 1h → 30m` przy domyślnych wartościach). |
| `TELEMATICS_EVENTS_TIMEOUT_S` | `TELEMATICS_PROVIDER_TIMEOUT_S` | Timeout pojedynczego requestu `/vehicles/events`; nie zmienia timeoutu `/trips` ani `/vehicles`. |
| `TELEMATICS_EVENTS_RATE_LIMIT_RPS` | `2.5` | Globalny rate limit requestów Telematics w `sync_trips_and_speeding`; provider client śpi między requestami, żeby uniknąć burstów. |
| `TELEMATICS_EVENTS_ENRICHMENT_MODE` | `enabled` | `enabled` wykonuje standardowy enrichment przez `/vehicles/events` ze strategią `strict`. `disabled` pomija wszystkie requesty `/vehicles/events`, nadal wykonuje `/trips` + `/vehicles` i zapisuje zera dla event-derived liczników. Legacy `strict` i `audited_best_effort` pozostają akceptowane; `audited_best_effort` zachowuje udane subokna, zapisuje unresolved gaps w logach i artefakcie, a następnie upsertuje trips z `complete_event_enrichment=false`. Param joba `event_enrichment_mode` ma pierwszeństwo nad ENV. |
| `TELEMATICS_EVENTS_ENABLE_REGISTRATION_FALLBACK` | `false` | Gdy `true`, terminalny fleet-wide chunk `/vehicles/events` po HTTP 500 albo retry exhaustion może zostać pobrany sekwencyjnie per rejestracja przez ten sam endpoint z query param `registration=<registration>`. |
| `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_RPS` | `1.0` | Tempo fallbacku per rejestracja. Fallback jest sekwencyjny; bez concurrency. |
| `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REGISTRATIONS` | `1500` | Maks. liczba znormalizowanych, niepustych rejestracji użytych w fallbacku. Źródło: `/vehicles` inventory union rejestracje z `/trips`. |
| `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_CHUNKS` | `1` | Maks. liczba fleet chunków, które mogą przejść przez fallback w jednym runie. |
| `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REQUESTS_PER_RUN` | `TELEMATICS_PROVIDER_MAX_REQUESTS_PER_RUN` | Limit requestów fallbacku w runie. Jeśli zmienna nie jest ustawiona jawnie, fallback nie może przekroczyć pozostałego globalnego budżetu provider safety. |
| `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES` | `5` | Jeśli pojedyncza rejestracja failuje dla dużego fallback window, job dzieli tylko tę rejestrację/window na mniejsze subchunki aż do tej wartości. |
| `TELEMATICS_EVENTS_BEST_EFFORT_MIN_FLEET_CHUNK_MINUTES` | `5` | Minimalny fleet subchunk w `audited_best_effort`; jeśli dalej failuje, job zapisuje gap `scope=fleet`, o ile recovery per rejestracja nie jest uruchomione. |
| `TELEMATICS_EVENTS_BEST_EFFORT_MIN_REGISTRATION_CHUNK_MINUTES` | `5` | Minimalny subchunk per rejestracja w `audited_best_effort`; jeśli dalej failuje, job zapisuje gap `scope=registration`. |
| `TELEMATICS_EVENTS_BEST_EFFORT_MAX_GAPS_PER_RUN` | `1000` | Twardy limit liczby gap records; przekroczenie oznacza niekontrolowaną degradację i kończy run przed DB upsert. |
| `TELEMATICS_EVENTS_BEST_EFFORT_MAX_SPLIT_DEPTH` | `8` | Twardy limit głębokości rekursji dla fleet i registration splitów. |

Przy przekroczeniu limitu lub wykryciu podejrzanej paginacji job rzuca `TelematicsProviderSafetyError`, loguje **ERROR** (`abort_code`, `phase`), a run kończy się **FAILED** — **bez** dalszych żądań do Telematics.

**Budżety compatibility paginacji `/trips` (`trips_pagination_mode = 'data_invariants_v1'`, C7):**

Te trzy zmienne obowiązują **wyłącznie** w compatibility state machine `/trips` (`docs/12_…` §5.5, `docs/16_…` §7.4). Ścieżka `strict_meta` oraz wszystkie pozostałe endpointy ich nie czytają. Nowe budżety mogą wyłącznie **zawężać** to, co dopuszczają istniejące limity Phase 2 — nigdy ich nie rozszerzają. Konfiguracja jest walidowana **raz, przed pierwszym requestem** sub-okna; wartość niecałkowita, `<= 0` albo powyżej pułapu kończy run fail-closed kodem `PAGINATION_COMPAT_CONFIG_INVALID` **bez** wykonania żądania do providera (w odróżnieniu od `TELEMATICS_PROVIDER_*`, gdzie błędna wartość cicho wraca do domyślnej).

| Zmienna | Domyślna wartość | Znaczenie |
|--------|-------------------|-----------|
| `TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW` | `TELEMATICS_PROVIDER_PAGE_LIMIT × TELEMATICS_PROVIDER_MAX_PAGES_PER_SUBWINDOW` (domyślnie `50000`) | Maks. liczba wierszy `/trips` zakumulowanych w pamięci dla jednego sub-okna compatibility. Przekroczenie: `PAGINATION_COMPAT_ROW_BUDGET_EXCEEDED`. |
| `TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES` | `33554432` (32 MiB), twardy pułap `67108864` (64 MiB) | Maks. znormalizowany rozmiar **jednej** odpowiedzi `/trips` w trybie compatibility. Budżet skumulowany dla całego sub-okna jest **pochodną**, nie osobną zmienną: `TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES × TELEMATICS_PROVIDER_MAX_PAGES_PER_SUBWINDOW`. Przekroczenie któregokolwiek: `PAGINATION_COMPAT_RESPONSE_BYTES_EXCEEDED` (kontekst `scope=response` / `scope=sub_window`). Rozmiar liczony jest z kanonicznej reserializacji sparsowanego payloadu — ścieżka transportowa `_request_json` pozostaje nietknięta. |
| `TELEMATICS_PROVIDER_COMPAT_MAX_ELAPSED_S` | `900` | Maks. czas zegarowy jednego sub-okna compatibility, sprawdzany **przed** wysłaniem kolejnego requestu. Przekroczenie: `PAGINATION_COMPAT_ELAPSED_BUDGET_EXCEEDED`. |

Implementacja: `jobs/api/telematics/provider_safety.py` (`CompatibilitySafetyLimits`), `jobs/api/telematics/provider_client.py` (`_fetch_paginated_data_invariants_v1`). Status: C7 zaimplementowane w repozytorium, **niewdrożone produkcyjnie**.

Implementacja: `jobs/api/telematics/provider_safety.py`, `jobs/api/telematics/provider_client.py`.

`/vehicles/events` dla liczników speeding oraz provider-labeled HIGH_RPM / OVERREV jest pobierane strict all-or-nothing. Job startuje od `TELEMATICS_EVENTS_CHUNK_HOURS`, paginuje każdy chunk z `limit<=1000`, a po timeout/provider failure zmniejsza chunk do połowy aż do `TELEMATICS_EVENTS_MIN_CHUNK_MINUTES`. Po udanym mniejszym chunku utrzymuje ten rozmiar do końca runu. Retry HTTP mają backoff (`5s`, potem `15s` przy domyślnym `TELEMATICS_PROVIDER_MAX_RETRIES=2`), a `TELEMATICS_EVENTS_RATE_LIMIT_RPS` ogranicza tempo requestów globalnie. Dla non-final chunków koniec requestu jest cofany o 1 sekundę, żeby nie podwajać eventów na inkluzywnych granicach.

Jeśli fleet-wide chunk nadal failuje na minimalnym rozmiarze i `TELEMATICS_EVENTS_ENABLE_REGISTRATION_FALLBACK=true`, job pobiera **ten sam chunk** per rejestracja przez `GET /vehicles/events` z query param `registration=<registration>`. W trybie `strict` fallback jest all-or-nothing: wyniki są trzymane w pamięci i zwracane dopiero po sukcesie wszystkich rejestracji. Jeśli pojedyncza rejestracja failuje dla dużego fallback window, dzielone jest tylko to registration/window do `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES`. Jakikolwiek nierozwiązany gap kończy run jako `FAILED` przed DB upsert.

W trybie `audited_best_effort` job zachowuje udane fleet subchunki, a terminalne failure zapisuje jako structured gap albo próbuje recovery per rejestracja, jeśli fallback jest włączony. Udane registration subwindows są zachowywane; terminalne registration failures są zapisywane jako gap records. Run może zakończyć się `SUCCESS` i wykonać DB upsert z dostępnymi countami, ale logi oraz artefakt `vehicle_events_gap_audit.json` mają `event_enrichment_status=partial`, `complete_event_enrichment=false`, liczbę gapów, affected registrations i total failed duration. Fail-fast nadal obowiązuje dla `401/403`, persistent `429`, malformed response/pagination, request budget, max split depth i max gaps.

Szacunek runtime fallbacku: minimalnie `registrations_count / TELEMATICS_EVENTS_REGISTRATION_FALLBACK_RPS`. Dla ALPHA (`1349` rejestracji): ok. `22.5 min` przy `1 req/s`, ok. `11.25 min` przy `2 req/s`, plus czas retry/subchunków i paginacji. Rekomendowany start ALPHA: fallback włączony tylko dla tego klienta/run context, `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_RPS=1`, `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REGISTRATIONS=1500`, `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_CHUNKS=1`, `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REQUESTS_PER_RUN=2000`, `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES=5`.

### Workflow A — dispatcher i retention worker

`jobs.api.telematics.dispatcher`:

- używa `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` do odczytu `workflow_a_control.client_dataset_schedule`, `client_account`, `dataset_registry`, `client_schedule_run_history`,
- jeżeli schedule nie ma własnej timezone, defaultem runtime jest `Europe/Warsaw`,
- dla `trips_sync` przekazuje z `client_dataset_schedule.event_enrichment_mode` parametr joba `event_enrichment_mode` (`enabled` albo `disabled`); dla innych datasetów tego parametru nie przekazuje,
- opcjonalnie używa `WORKFLOW_A_DISPATCHER_STALE_RUNNING_TIMEOUT_MINUTES` (default `720`) do auto-fail starych `RUNNING` rows; parametr joba `stale_running_timeout_minutes` ma pierwszeństwo,
- sam nie ma wymaganych parametrów poza standardowym `params` runnera; proponowany unit przekazuje `{}`,
- uruchamia właściwy job datasetu jako subprocess `ops/runner.py`, więc środowisko procesu dispatchera musi zawierać też `LOG_API_URL`, `API_WRITE_TOKEN`, `API_READ_TOKEN` oraz sekrety wymagane przez docelowy job.

`jobs.api.telematics.retention_purge`:

- używa tych samych `POSTGRES_*` do odczytu polityk `workflow_a_control.client_table_retention`,
- do połączeń z bazami klientów używa `client_db_*` z control-plane oraz `client_db_password_secret_ref` resolved przez env-var albo `file:`,
- parametry runnera: `dry_run` (default `true`), `client_id`, `table_name`, `batch_size` (default `5000`), `max_batches`.

Proponowane unity hostowe:

- `ops/systemd/proposed/log-job@dispatcher.{service,timer}` — tick co 5 minut,
- `ops/systemd/proposed/log-job@retention-purge.{service,timer}` — niedziela 03:30 UTC; parametry z `/etc/log-platform/retention-purge.params.json`.

## Maintenance / backup

`ops/db_migrate.sh`:

- `POSTGRES_USER` (default `loguser`)
- `POSTGRES_DB` (default `logdb`)

`ops/backup.sh`:

- `POSTGRES_USER`
- `POSTGRES_DB`
- `BACKUP_RETENTION_DAYS` (optional)

`POSTGRES_USER`/`POSTGRES_DB` mogą być też wykryte przez `docker compose config`.

`ops/platform_prune.sh`:

- uses the same `POSTGRES_*`, `MINIO_*` and fail-closed `LOG_PLATFORM_TARGET_ENVIRONMENT` / `LOG_PLATFORM_EXPECTED_*` declarations;
- `MINIO_HOST_ENDPOINT` is an optional non-secret host override and takes precedence over `MINIO_ENDPOINT` for the direct prune CLI;
- systemd loads the protected host coordinates from `/etc/log-platform-host.env` before dropping to `logplatform`;
- `--dry-run` and `--execute` are mutually exclusive and one is required; `--days` has no implicit CLI default.
Backup MinIO:

- jeśli istnieje katalog `miniodata/` w repo, skrypt archiwizuje ten katalog,
- przy standardowym `docker-compose.yml` MinIO używa nazwanego wolumenu
  Dockera; skrypt kopiuje wtedy `/data` z działającego kontenera `minio` i
  tworzy `backups/minio_YYYYmmdd_HHMMSS.tar.gz`.

## Pliki env

Repo:

- `docs/env.stage1.example` — przykładowy szablon ENV dla **Workflow B** (IMAP + Postgres używany przez Stage 1); nazwa historyczna „stage1” odnosi się do ścieżki backupowej.

Host (poza repo):

- faktyczne pliki `.env`/`*.env` z sekretami i tokenami,
- (docelowo) osobne pliki lub fragmenty konfiguracji per klient dla Workflow A.

## Fail-closed identity for guarded operational DB commands

The first guarded scope is the D105.2 EcoDriving trip-metrics migration and local/dev single-file replay `--load-stage3`. These commands require all non-secret variables below; none has a default:

- `LOG_PLATFORM_TARGET_ENVIRONMENT`: exactly `local_dev`, `staging`, or `production`;
- `LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID`: canonical UUID of the logical platform DB;
- `LOG_PLATFORM_EXPECTED_POSTGRES_HOST`;
- `LOG_PLATFORM_EXPECTED_POSTGRES_PORT`;
- `LOG_PLATFORM_EXPECTED_POSTGRES_DB`;
- `LOG_PLATFORM_EXPECTED_POSTGRES_USER`.

Expected coordinates must exactly match effective `POSTGRES_*`. The connected platform DB and selected client DB must also contain matching singleton rows in `ops_control.environment_identity`. Expected client environment/UUID are stored in `workflow_a_control.client_account.client_db_environment` and `client_db_identity_id`. Migration `042_platform_environment_identity.sql` creates platform structure and those columns; client migration `038_environment_identity.sql` creates the same marker table in client DBs. Neither migration inserts an identity or assumes production.

Identity labels and UUIDs are non-secret. DB passwords, API tokens, and client password references remain secret. Missing/invalid declarations, missing markers, or any mismatch abort guarded commands. Production is never inferred from host, database name, user, Compose state, or loopback topology.

## Official environment identity promotion

The canonical environment values are case-sensitive: `local_dev`, `staging`, and `production`. `production` is the only production spelling; `prod`, `prd`, `live`, `development`, and `test` are not environment identity values. Python validates the same allow-list as the SQL `CHECK` constraints in platform migration `042_platform_environment_identity.sql` and client-business migration `038_environment_identity.sql`.

`database_identity_id` identifies one physical/logical database instance and is independent of its deployment classification. Promotion changes only `environment` in each selected `ops_control.environment_identity` marker and `client_db_environment` in selected control-plane rows. It never changes platform/client database UUIDs, `client_id`, `client_code`, database name, or connection coordinates. A restored database continuing as the same reviewed physical production instance retains its UUID. A clone made as a new local/staging/production database instance must receive a new UUID through the separate provisioning contract; never copy a production UUID into a clone.

The repository-supported entry point is `ops/promote_environment_identity.py`. It requires explicit source/target, platform UUID, selected client codes, a UUID for every selected client, the exact runtime environment-file path, and verified checkpoint evidence covering the platform and every selected client. It never discovers and promotes every enabled client. Default mode is read-only and emits the immutable plan, SHA-256, affected rows/file, readiness report, and exact execute attestation. Execute additionally requires `--execute --attestation '<exact dry-run output>'`, migration `053_environment_identity_promotion_journal.sql`, and the same checkpoint path.

Promotion plan contract v5 additionally requires explicit `--recovery-root`, `--preserved-recovery-backup`, and `--recovery-evidence`. Its canonical compact hashed JSON binds the explicit v5 schema, host/repository identity, the normalized effective helper-specific sudo policy and fingerprint, deterministic repository-relative security-critical implementation asset hashes, sorted rolled-back journal/evidence history, the complete sorted excluded-action vocabulary, canonical file value/hash/metadata, database markers/control-plane/UUID/capabilities and zero-journal baseline, systemd PID/InvocationID/start/drop-in hashes/health, Docker container/image/creation/Compose fingerprint/health, per-invocation consumers, checkpoint hash/metadata, restricted recovery metadata and provisioning-plan association, helper/consumer assets, exact ordered mutations, explicit exclusions, and separate post-promotion restart/recreation gates. Any ephemeral process/container change intentionally invalidates the approval. A source-file, historical evidence, exclusion-set, or effective sudo-policy change likewise changes the canonical bytes and requires a new dry-run and attestation. New forward execution accepts only contract v5; v3/v4 are superseded for forward use, while historical v4 journals remain readable by recovery-v2.

The only authoritative runtime source for `LOG_PLATFORM_TARGET_ENVIRONMENT` on a provisioned host is `/etc/log-platform/environment-identity.env`. It is an identity-only, non-secret but operationally sensitive file containing exactly one unquoted assignment. The shared parser accepts only `local_dev`, `staging`, or `production`; it rejects comments, blank extra lines, unknown/duplicate keys, quotes, interpolation, command substitution, malformed UTF-8, symlinks, and non-canonical case. The reviewed ownership model is `root:logplatform` mode `0640`: required services and the host job user can read it, but only the narrow privileged helper can write it.

Provisioning and promotion are separate operations. `ops/provision_runtime_environment_identity.py` converges the host while the value is still `local_dev`: it creates the canonical file, removes active identity declarations from `.env`, `/etc/log-platform-host.env`, `/etc/log-platform/runtime.env`, and `/etc/log-platform/backup.env`, installs the reviewed wrapper/helper/systemd drop-ins, and leaves restart/recreate actions explicit. It is dry-run by default and execute requires exact host, repository HEAD, environment, verified backup reference and generated attestation. Do not promote before provisioning readiness is complete.

Root-only legacy files are inspected before provisioning through the independently installed, root-owned `/usr/local/sbin/log-platform-runtime-identity-inspector`. Its only operation is `inspect-runtime-identity-sources`, with a compiled-in allow-list of `/etc/log-platform-host.env`, `/etc/log-platform/runtime.env`, and `/etc/log-platform/backup.env`. Deterministic JSON contains only fixed source names/paths, file metadata and SHA-256, and parsed state for `LOG_PLATFORM_TARGET_ENVIRONMENT`; it never returns other variable names or values. The repository `.env` stays on the unprivileged strict-parser path. The service user receives no general read permission for the secret-bearing files.

The installed wrapper loads secret-bearing repository `.env` configuration, rejects any remaining active identity declaration, then applies the canonical parser. systemd API, prune, backup and proposed workers load their existing file first and the identity-only file last. Compose injects the canonical file through the API `env_file` and no longer interpolates the value from repository `.env`. Direct manual commands use `ops/run_with_environment_identity.py`; direct process values that conflict with the canonical value fail closed. Oneshots reread on each invocation. The systemd API and Docker API cache the value at process/container creation and must be restarted/recreated after a value change.

Promotion invokes `/usr/local/sbin/log-platform-environment-identity-helper` through the narrowly scoped sudo rule; the main CLI does not run as root. Provisioning also installs the parser as root-owned `/usr/local/lib/log-platform/environment_identity_file.py`; the root helper runs Python in isolated mode and never imports executable code from the user-writable checkout. The helper accepts only the canonical path and one canonical value, checks expected old value/current checksum/attestation, rejects symlinks and wrong metadata, creates a root-owned mode-`0600` checksum-bound sibling backup, fsyncs, and atomically replaces the file. The immutable plan binds the helper/version/source+installed+parser hashes, intended before/after file hashes, consumer inventory, installed wrapper/drop-in hashes, exact reload actions, and verification commands. After the file write the journal remains `in_progress` at `runtime_reload_required`; completion is impossible until running-process inspection proves systemd and Docker API convergence.

A promotion checkpoint is a protected JSON document such as:

```json
{
  "kind": "environment_identity_promotion_checkpoint_v1",
  "environment": "local_dev",
  "platform_uuid": "<existing-platform-uuid>",
  "verified": true,
  "clients": {
    "CLIENT00001": "<existing-client-database-uuid>"
  }
}
```

It is an evidence reference, not a backup implementation. Operators must retain independently verified platform and client-database recovery media. The checkpoint contains no passwords, DSNs, tokens, or raw environment-file content.

Client-business migration `045_environment_identity_promotion_primitive.sql` installs `ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text)`. Existing-client rollout discovers the intended runtime role from its direct marker `SELECT` grant; new onboarding grants the known configured client DB user explicitly. Both paths revoke direct marker-table `UPDATE`, revoke function execution from `PUBLIC`, and grant only function `EXECUTE`. Production promotion never silently falls back to direct client-marker UPDATE. The SECURITY DEFINER function fixes `search_path`, locks and verifies the primary marker/database/role/UUID/source, changes only `environment`, rereads the row, and returns the preserved UUID plus old/new environment, database name, and changed-row count.

### Runtime identity recovery and systemd precedence

Provisioning recovery artifacts live outside the Git worktree under the repository-adjacent `log-platform-runtime-identity-recovery` root. Recovery directories are `0700`; backup and evidence files are `0600`, checksum-bound, atomically written, and discoverable by operation, repository HEAD, execution ID, plan SHA-256, checkpoint, and logical source. A recovery root inside the checkout, a symlinked path, permissive metadata, or unverifiable copy fails before the corresponding source is modified.

The API canonical drop-in is `zz-environment-identity.conf`, which sorts after the host-managed `override.conf`. This is required because an empty `EnvironmentFile=` resets every earlier environment-file entry. The later canonical directive adds `/etc/log-platform/environment-identity.env` after that reset while `/etc/log-platform-host.env` remains available for unrelated settings. Prune and backup are inactive oneshot consumers; after `daemon-reload`, their next invocation reads the canonical source and their timers need no identity-only restart.
