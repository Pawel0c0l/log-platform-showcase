# Jobs

## Pozycjonowanie workflow

- **Workflow A (główny kierunek strategiczny)**: joby **pozyskiwania danych z zewnętrznego API (A1)** oraz **synchronizacji z główną bazą klienta (A2)** — per konto klienta, na żądanie przez terminal oraz opcjonalnie przez DB-driven dispatcher uruchamiany timerem hostowym. Aktualnie zaimplementowane są moduły Telematics opisane niżej. Wyzwolenie e‑mail pozostaje planowane po stronie hosta.
- **Workflow B (backup / legacy, rozwój wstrzymany)**: istniejące joby poczty, Stage 2 oraz Stage 3 w łańcuchu Stage 1 → Stage 2 → Stage 3. Nadal dokumentowane jako **ścieżka zapasowa**.

## Canonical production entrypoint vs development invocation

**RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION**

Every `PYTHONPATH="$PWD" python3 ops/runner.py <module> '<json>'` command in this
document is **development / local / debug only**. It executes the mutable
development working tree directly, which is exactly the property the release
boundary exists to remove: after cutover it would run unpromoted code against
production data while the release pointers still looked healthy.

The canonical **production** entrypoint for every job covered by the platform
release boundary is the installed wrapper:

```bash
/usr/local/bin/log-job-runner.sh <module> '<json params>'
```

It is the single place that decides which source tree production runs from, and
it selects the project virtualenv and the canonical environment identity. Use it
for manual production runs of runner jobs — Workflow A datasets, the dispatcher,
retention purge, Workflow B stages and orchestrator, Report 207 postprocessors,
and Eco Driving aggregation and notification jobs.

Direct `ops/runner.py` invocation remains correct and supported for local
development, debugging and non-production environments.

Scope, provenance, promotion and rollback: `docs/07_operations.md` → *Release
boundary*.

## Kontrakt joba (wspólny dla A i B)

Każdy job uruchamiany przez runner musi eksportować:

```python
def run(client, run_id: str, params: dict):
    ...
```


All runner jobs—Workflow A, Workflow B, dispatcher-launched datasets and retention—inherit runtime identity from the canonical `/etc/log-platform/environment-identity.env` through the strict installed wrapper/runner parser. Secret-bearing `.env` remains a separate configuration source and must not declare the identity key after provisioning. The suspected-bug and database-export dedicated worker units likewise load existing SMTP/runtime configuration first and the canonical identity file last; this separation does not install, enable or send through those workers.
Job **nie** tworzy ani nie kończy runa samodzielnie — robi to `run_context` przez wewnętrzne API.

## Dedicated workers outside the runner

`ops/database_export_worker.py` is a dedicated local worker for asynchronous Database Explorer exports. It is **not** a runner job and does not implement `run(client, run_id, params)`: it owns a database-backed queue (`database_export_jobs`) and writes requester-owned Artifact Explorer artifacts after completing a large export.

Behavior:

- direct Database Explorer CSV/XLSX export remains synchronous and capped at 20,000 matching data rows;
- background CSV/XLSX export is queued from the portal for 20,001 through 1,000,000 matching data rows; requests above the cap are rejected without queueing or creating an artifact;
- migration `044_database_export_system_folders.sql` adds one stable, system-managed `Database Exports` folder per owner and stores the durable, DB-enforced owner-coupled relation on `database_export_jobs.system_folder_id`; direct exports at or below 20,000 rows are not added to this folder and do not create persistent artifacts;
- queued/running jobs appear in that folder as virtual rows only, with status and timestamps but no placeholder artifact and no download action;
- the worker re-checks the requester’s current dataset authorization and `can_export_rows` immediately before generating output;
- only one export runs globally at a time, guarded by a Postgres advisory transaction lock, a `running` lease, a persisted `claim_token`, and `FOR UPDATE SKIP LOCKED` queue claiming;
- stale `running` jobs whose lease expires are requeued while `attempt_count < 3`, then failed with a safe message; fenced stale workers cannot refresh, fail, complete, or attach artifacts after a later claim replaces their `claim_token`;
- unpublished attempt object keys are retained in `database_export_attempt_objects`; stale/failure/fence-loss paths mark them `cleanup_pending` and retry deletion idempotently without deleting the ledger row;
- on `SIGTERM` / `SIGINT`, the loop wakes promptly instead of waiting for the full poll interval, stops claiming new jobs, and only exits active jobs at safe checkpoints; if shutdown is requested after an attempt object was uploaded, the object is marked `cleanup_pending` and is not published as a completed artifact;
- completed exports become Artifact Explorer artifacts with `workflow_name=database_explorer`, `stage_name=async_export`, `artifact_role=database_export`, `owner_user_id=<requester>`, and `expires_at=completed_at + 3 days`; publication inserts the artifact and completes the fenced job in the same DB transaction, so the folder row points at a real artifact only after publication succeeds;
- failed jobs keep only generic portal-safe failure text, and expired/deleted artifacts remain visible only as non-downloadable job history;
- the same worker entry point can run cleanup (`--cleanup-only`), retrying pending unpublished attempt-object deletion and marking completed exports expired while deleting completed artifact objects idempotently.

Platform migration `043_database_explorer_async_exports.sql` adds `database_export_jobs`, `database_export_attempt_objects`, and additive artifact owner/expiry metadata. Platform migration `044_database_export_system_folders.sql` adds `database_export_system_folders` plus the optional job relation `database_export_jobs.system_folder_id`; before 044 is applied, the queue and worker stay safe and the portal uses the legacy `/user/database/exports` listing instead of the Reports system-folder view.

## Workflow A — model jobów

Oczekiwane cechy implementacji:

| Etap | Odpowiedzialność |
|------|------------------|
| **A1** | Wywołania zewnętrznego API z uwierzytelnieniem **specyficznym dla klienta** (parametry joba + env / pliki po stronie hosta). |
| **A2** | Zapis do bazy klienta w modelu **inkrementalnym / upsert**, z utrzymaniem znacznika postępu lub „ostatniej znanej synchronizacji” opartego o pole świeżości ze źródła. |
| **Idempotencja / konflikty** | Jeśli rekord w DB jest nowszy niż payload z API — zachować DB. Jeśli API jest nowsze — zaktualizować rekord. Przy identycznym znaczniku świeżości — pominąć zapis (chyba że obowiązuje osobna reguła biznesowa). |

**Wyzwalanie**: ten sam runner obsługuje uruchomienia ręczne i cykliczne. Dla Workflow A istnieje `jobs.api.telematics.dispatcher`, który może być odpalany timerem hostowym i uruchamia due dataset jobs. E‑mail jako trigger wymaga warstwy na hoście (**planned**).

## Aktualne joby w repo

### `jobs.reports.demo`

- loguje kroki 1/3, 2/3, 3/3,
- tworzy plik `/tmp/demo_result.txt`,
- uploaduje artefakt przez `client.upload_artifact(..., kind="REPORT")`.

Platform support for keyed artifact uploads is used by Workflow B Stage 2 cleaned outputs. Debug/review samples keep their existing unkeyed lifecycle because they are not the finalized cleaned-output contract.

### `jobs.mail.fetch_reports` — Workflow B, Stage 1

Funkcjonalność (ścieżka backupowa):

- odbiór raportów z IMAP,
- obsługa załączników `.csv/.xls/.xlsx/.xlsm`,
- obsługa linków raportowych z allowlistą domen,
- preflight linków: `http_range_fp` (`<len>:<sha_first>:<sha_last>`),
- deduplikacja:
  - po `sha256`,
  - po `(report_key, content_fingerprint)`,
  - dla linków także pre-deduplikacja po `http_range_fp`,
- normalizacja do canonical CSV (`.xls/.xlsx/.xlsm`: wszystkie niepuste arkusze w kolejności workbooka, bez kolumn technicznych, z jednym pustym wierszem między arkuszami; dla `.xlsx/.xlsm` Stage 1 ignoruje stale metadane wymiaru arkusza typu `A1` / `A1:A1`, żeby `openpyxl` w trybie `read_only` nie uciął realnych wierszy; `.csv`: detekcja kodowania/dialektu i rewrite do `;` / `utf-8-sig`),
- kanonizacja rozpoznanych kolumn daty/daty-czasu w normalized CSV do stabilnego formatu (patrz sekcja niżej),
- addytywny upload nowych plików RAW i normalized CSV do standardowego systemu artefaktów,
- statusy `ingest.raw_file`: `NEW`, `NORMALIZED`, `FAILED`, `DUPLICATE_CONTENT`.

#### Dane Stage 1 (Workflow B)

Tabele:

- `ingest.imap_message`
- `ingest.raw_file`

Migracje aktualne (związane z ingestem / Stage 2 w B):

- `001_ingest_imap_fetch_reports.sql`
- `002_ingest_raw_file_content_fingerprint.sql`
- `003_ingest_raw_file_persisted_duplicate_of.sql`
- `004_ingest_raw_file_http_range_fp.sql`
- `005_ingest_raw_file_sha256_partial_unique.sql`
- `006_stage2_status.sql`
- `007_artifacts_raw_file_id.sql` (opcjonalna kolumna `raw_file_id` w `artifacts` → `ingest.raw_file(id)`)
- `019_workflow_b_report_type_registry.sql` (schemat `workflow_b_control`, tabela `report_type_registry` jako read model aktualnych typów raportów Stage 2)
- `020_workflow_b_report_registry_detection_contract.sql` (Phase 1: dodaje maszynowo czytelny kontrakt `detection_rules`, `column_types`, `multi_table` i `cleaner_entrypoint`; nadal bez zmiany runtime Stage 2)
- `021_workflow_b_report_207_registry.sql` (dodaje `report_207` do control-plane registry; runtime Stage 2 nadal używa Pythonowego registry)
- `022_artifact_layout_metadata.sql` (dodaje semantyczne metadata artefaktów: workflow/stage/role/report_type/display_filename/original_filename/file_ext/layout_version/metadata_json)
- `023_artifacts_client_code.sql` (dodaje opcjonalne `artifacts.client_code` pod przyszłe filtrowanie per klient; istniejące wiersze i obecne Stage 2 mogą mieć `NULL`)
- `024_artifact_annotations.sql` (dodaje manualne opisy, manualne metadata JSON i tagi artefaktów; nie zmienia runtime Stage 2 ani plików w MinIO)
- `028_workflow_b_stage2_client_code_record_id.sql` (dodaje konfigurację `id_sync_column_name` / `record_id_ingredients` w registry oraz `ingest.raw_file.client_code`)
- `029_workflow_b_stage3_load.sql` (dodaje per-client/report policy `workflow_b_control.report_type_client_load_policy` oraz pola `stage3_*` w `ingest.raw_file`)
- `030_workflow_b_alpha_gps_baza_log_registry.sql` (dodaje `Alpha_GPS_Baza_LOG` do read modelu registry oraz policy `data_overwrite=true` dla `ALPHA00001`)
- `031_workflow_b_ensure_alpha_gps_baza_log_registry.sql` (idempotentnie naprawia/uzupełnia registry i policy `ALPHA00001` / `Alpha_GPS_Baza_LOG`)
- `049_workflow_b_stage1_artifact_reconciliation.sql` (addytywne `ingest.raw_file.stage1_normalized_artifact_metadata` potrzebne do wiernego retry metadanych normalized artifact; bez backfillu)
- `050_workflow_b_report_postprocessor_selector_override.sql` (nullable `report_type_client_load_policy.trip_metrics_population_source_override`; brak defaultu i backfillu; `NULL` dziedziczy selector klienta)

Stage 1 zapisuje RAW i canonical CSV lokalnie oraz w `ingest.raw_file` w **osobnej transakcji dla każdej wiadomości IMAP**. Wiersz `ingest.imap_message` i wszystkie powiązane `ingest.raw_file` danej wiadomości commitują razem dopiero po poprawnym pobraniu, walidacji i normalizacji wszystkich wymaganych kandydatów. Retryable fetch/download/persistence failure wykonuje rollback tylko tej wiadomości; wcześniej zakończone wiadomości pozostają committed, a batch zwraca partial `Stage1BatchResult` z `retryable_work_remains=true`. Lokalnie utworzone przez wycofaną wiadomość pliki są usuwane; wcześniej istniejące content-addressed pliki nie są kasowane.

Download linku jest strumieniowany do tymczasowego pliku `.part`, ma dwa bounded attempts, respektuje limit bajtów i porównuje odebraną liczbę bajtów z `Content-Length`, jeśli nagłówek jest poprawny. Dopiero zwalidowane bajty są atomowo publikowane pod finalną content-addressed ścieżką RAW; niepełny `.part` jest usuwany i nie tworzy committed `imap_message`. Linki odrzucone przez allowlistę pozostają expected skips (`SKIPPED_EXPECTED_LINK`); dotyczy to m.in. `cancelEmail`, które nie jest retryable download failure. Logi nie zapisują query stringów URL.

Dla nowo przetworzonych plików, po per-message commicie danych ingestu, Stage 1 próbuje addytywnie uploadować artefakty layoutu v2:

- `workflow_name=workflow_b`
- `stage_name=stage_1_fetch`
- `artifact_role=raw` dla pobranego pliku źródłowego
- `artifact_role=normalized` dla canonical CSV
- `report_type=unknown`, bo Stage 1 nie wykonuje pewnej detekcji typu raportu
- `raw_file_id=<ingest.raw_file.id>`, jeśli wiersz został utworzony
- `original_filename=<ingest.raw_file.original_filename>`

Upload artefaktów nie zastępuje lokalnej persystencji. Role `raw` i `normalized` mają odrębne kontrakty v1 i scope odpowiednio `workflow_b.stage1.raw.v1` oraz `workflow_b.stage1.normalized.v1`. Klucz to SHA-256 compact canonical JSON `["workflow_b.stage1.<role>","v1",raw_file_id,source_sha256]`; nie zależy od run ID, czasu, hosta, ścieżki, nazwy pliku ani danych maila/klienta. `created` i `reused` wskazują kanoniczny artifact ID, a HTTP 409 jest nie-retryable integrity conflict bez fallbacku do unkeyed uploadu.

Stage 1 zachowuje pliki `raw_path` i `normalized_csv_path`, a migracja 049 zachowuje bezpieczne metadane normalizacji potrzebne przy późniejszym retry. Przed IMAP Stage 1 wykonuje bounded persisted-record reconciliation (parametr `artifact_reconcile_limit`, default 50), więc deduplikacja już zapisanego message nie blokuje naprawy brakującego artefaktu. Synchronizacja jest chroniona session advisory lockiem namespace `workflow_b.stage1.artifact_sync.v1` per `(raw_file_id, role)`; Artifact API idempotency pozostaje finalną granicą correctness. Retryable upload/persistence failure albo integrity/ambiguity powoduje `Stage1ArtifactSyncError` z partial `Stage1ArtifactReconciliationResult`; udane ingest rows nie są usuwane.

Manual-only job `jobs.mail.reconcile_report_artifacts` nie łączy się z IMAP. Domyślnie działa jako dry-run; zapis/upload wymaga `execute=true`. Obsługuje `raw_file_ids`, `limit` i `roles` (`raw`/`normalized`). Dokładnie jeden zgodny legacy artifact jest akceptowany przez istniejące lineage, wiele kandydatów blokuje się jako ambiguous, a brak trwałych bajtów daje `BLOCKED_SOURCE_MISSING`.

Publiczne `jobs.mail.fetch_reports.run(...)` deleguje do reusable `fetch_reports_batch(...)` i zwraca `Stage1BatchResult`. Per-item `Stage1ItemResult` zawiera opaque identities, `raw_file_id`, bezpieczne artifact IDs, outcome/dedup classification, retry/operator flags oraz dla failures: IMAP UID, download host, oryginalny exception type, sanityzowany detail, expected/received bytes, retry attempt, transaction scope i cleanup result; nie serializuje sekretów, query stringów ani pełnych URL. Batch rozróżnia: poprawnie sprawdzoną pustą skrzynkę, wszystkie wiadomości zdeduplikowane, expected allowlist skips, wiadomości bez wspieranych załączników oraz retryable IMAP/download/persistence failure. `downstream_stage2_work_may_exist` wskazuje nowy znormalizowany persisted input. Reconciliation result jest osadzony bez ponownego wyliczania jego outcome.

Atomowość ingestu obowiązuje per wiadomość, nie per pojedynczy załącznik: failure jednego wymaganego kandydata wycofuje metadata i nowo utworzone lokalne pliki tej wiadomości, ale nie innych wiadomości. Po commicie ingestu artifact upload pozostaje osobną durable/reconcilable granicą (API musi widzieć `raw_file_id` przez FK). Keyed upload i persisted-record reconciliation deterministycznie naprawiają przypadek „obiekt zapisany, metadata/response niepotwierdzone”; taki item ma typed retryable artifact outcome, więc nie jest raportowany jako ukończony sukces. Po batchu błąd daje `Stage1BatchError(result)` z aliasem `partial_result`; brak pracy pozostaje typed sukcesem.

Dla `Alpha_GPS_Baza_LOG` Stage 1 przyjmuje załącznik `.xlsm` wysłany z procesu VBA, ale nie wykonuje VBA. Normalizacja używa `openpyxl` w trybie `read_only=True`, `data_only=True`, `keep_vba=False`. Workbook ma trzy istotne arkusze (`GPS_baza_START` — nagłówek detekcji, `LOG` — nagłówek outputu/cleaningu, `Status_Prywatnosci` — nagłówek stop), więc Stage 1 **zapisuje wszystkie arkusze do jednego canonical CSV** w kolejności workbooka, oddzielając każdy arkusz pojedynczym pustym wierszem. Dzięki temu Stage 2 widzi w jednym CSV nagłówek detekcji `ID;Nr rejestracyjny;Data przydziału;RFID;PRYW;EDYS;OPTIMA;OTK`, nagłówek startu cleaningu `ID;Nr rejestracyjny;Data przydziału;Nazwa Pliku csv` oraz nagłówek stop `ID;Data przydziału;PRYW stary;PRYW aktualny`. Surowy XLSM i normalized CSV są śledzone jako artefakty `raw` i `normalized` z `raw_file_id`, `sha256`, `run_id`, nazwą załącznika i metadanymi wiadomości email.

##### Kanonizacja dat w normalized CSV (Stage 1)

Stage 1 kanonizuje **rozpoznane** kolumny daty/daty-czasu przy zapisie normalized CSV, żeby downstream (Stage 2 walidacja, Stage 3 load, migracja `report_207` → `client_trips`) nie dostawał mieszanki formatów (np. Excelowych seriali z `.xls` obok stringów ISO z `.xlsx`). Logika jest współdzielona w `jobs/reports/date_normalization.py` i jest **kolumno-/raporto-świadoma**:

- Format wyjściowy: data → `DD.MM.YYYY`, data+czas → `DD.MM.YYYY HH:MM` (sekundy są zaokrąglane do minuty i pomijane).
- Rozpoznawane wejścia: Excelowe seriale (całe i ułamkowe, baza `1899-12-30`, okno lat 2000–2100, jak w Stage 2/3), obiekty `datetime`/`date` z `openpyxl`, oraz formaty tekstowe `YYYY-MM-DD[ HH:MM[:SS]]`, `DD.MM.YYYY[ HH:MM[:SS]]`, `DD/MM/YYYY[ HH:MM[:SS]]` (z opcjonalnym `T` i prostym sufiksem strefy, który jest odcinany — czas lokalny/biznesowy nie jest konwertowany).
- Detekcja kolumn: najpierw mapowanie per-raport (m.in. `report_207` → `Data i czas` jako datetime), potem zachowawcza heurystyka po nazwie nagłówka (`data`, `date`, `czas`, `godzina`, `datetime`, `timestamp`, `event_ts`). Ciągi liczbowe są traktowane jako Excelowe seriale **wyłącznie** w potwierdzonych kolumnach daty; kolumny czysto liczbowe nie są dotykane. Wartości boolowskie i nieskończone nie są konwertowane.
- Wartości niemożliwe do sparsowania są **zachowywane bez zmian** i zliczane w metadanych (z próbką), zamiast być zamieniane na błędną datę.
- Nagłówki i kolumny niebędące datami zachowują dotychczasowe (legacy) wyjście normalizacji; zmienia się tylko zawartość rozpoznanych kolumn daty.
- Skoroszyty wieloarkuszowe / stronicowane (np. `report_207` w `.xls` dzielony przez limit 65 536 wierszy na arkusz): kolumna daty wykryta na jednym arkuszu jest **dziedziczona** na kolejne arkusze tego samego skoroszytu, więc wiersze-kontynuacje (przed powtórzonym, „głębokim” nagłówkiem albo całkiem bez nagłówka) też są kanonizowane. Dziedziczenie jest stosowane do arkusza tylko po **próbkowym potwierdzeniu**, że wartości w tej kolumnie faktycznie są datami/serialami — inaczej kolumna pozostaje nietknięta (zabezpieczenie przed reklasyfikacją kolumny liczbowej o innym układzie).

Metadane normalizacji dat są dołączane do logu `Raw file normalized` oraz do `metadata` artefaktu `normalized` (per kolumna: `treated_as`, `source`, `input_format_families`, `parsed`, `empty`, `unparseable`, `unparseable_sample`). Stage 2 walidacja (`jobs/reports/stage2/validation.py`) jawnie akceptuje format `DD.MM.YYYY HH:MM` (oraz pozostałe warianty bez sekund) obok wcześniej akceptowanych formatów i Excelowych seriali.

Jeśli istniejący wiersz `ingest.raw_file` został zapisany przez wcześniejszą wersję normalizacji XLSM (tylko arkusz `LOG`), można go odbudować bez ponownego pobierania maila przez `ops/renormalize_raw_file.py`. Skrypt domyślnie działa w trybie dry-run; `--apply --reset-stage2` ponownie zapisuje plik `normalized_csv_path` (używając aktualnej logiki `_convert_to_canonical_csv`) i czyści `stage2_*` w `ingest.raw_file`, żeby kolejny run Stage 2 ponownie zdetektował typ raportu.

Domyślnie Stage 1 nie filtruje wiadomości po odbiorcy, nadawcy ani temacie. Po wybraniu skonfigurowanego folderu/labela (`IMAP_MAILBOX`) kwerenda IMAP używa tylko `SINCE <date>`, a dalej działają normalne reguły załączników/linków i deduplikacji. `IMAP_SENDER_FILTERS` / `IMAP_SENDER_FILTER` mogą zawęzić fetch tylko wtedy, gdy operator jawnie je ustawi.

### `jobs.reports.stage2.job_stage2` — Workflow B, Stage 2

Funkcjonalność:

- detekcja typu raportu na canonical CSV,
- cleaning i walidacja kontraktu (schema drift),
- scoring: `detect_score`, `clean_score`, `schema_score`, `final_score`,
- decyzja importu: `OK` / `PENDING_REVIEW`.

Wejście produkcyjne jest wybierane z trwałych wierszy `ingest.raw_file`, których `normalized_csv_path` wskazuje canonical CSV zapisany przez Stage 1 pod `REPORTS_DATA_DIR/normalized`; nie odbywa się już rekurencyjny skan katalogu. Domyślnie kwalifikują się znormalizowane, nieskończone rekordy oraz jawnie sklasyfikowane retryable failures; zakończone `OK`, walidacyjne/review i niejednoznaczne historyczne powody są wykluczone. Manualne `input_files` / `input_dir` pozostają akceptowane, lecz każda ścieżka musi mapować się dokładnie do jednego `raw_file_id`.

Parametry:

- `limit` (opcjonalnie; limit liczby plików),
- `debug_detection` (opcjonalnie; `true` wymusza dla każdego pliku log INFO `Stage2 detection diagnostics` z pełną listą kandydatów Pythonowych klas detekcji, ich score, podglądem nagłówków canonical CSV i wykrytym typem; niezależnie od parametru ten sam log jest emitowany dla plików kończących się jako `PENDING_REVIEW`).
- `raw_file_ids` (opcjonalnie; jawna lista trwałych identyfikatorów), `retry_technical_failures` (default `true`) i `force_reprocess` (default `false`; wymaga jawnego `raw_file_ids` lub `input_files` i nie omija idempotency).

Każdy plik jest chroniony session advisory lockiem z namespace `workflow_b.stage2.raw_file.v1` i deterministycznym SHA-256 do signed bigint. Po locku stan jest ponownie odczytywany. Worker przegrywający lock zwraca `SKIPPED_LOCKED` bez transformacji/uploadu; lock jest zwalniany w `finally` lub przez zamknięcie sesji.

**Reconciliation nieprzypisanych plików (P0-C).** Discovery Stage 2 ponownie bierze plik `NORMALIZED` tylko gdy stan jest pusty, `stage2_retryable = true` albo ma historyczny kształt `PENDING_REVIEW` / `stage2_exception`. Stage 3 konsumuje plik tylko gdy `stage2_status = 'OK'` **i** jest routowalny (client code, report type, cleaned artifact). Każdy inny trwały stan nie należy do żadnego z nich i po cyklu, który go utworzył, nigdy więcej się nie pojawia.

Na końcu autonomicznego batcha `stage2_unrouted_files()` wykonuje read-only sweep (wyłącznie SELECT, bez locka, bez UPDATE) i dopisuje `review_required` item dla każdego takiego pliku:

| outcome | znaczenie |
|---|---|
| `STRANDED_AWAITING_REVIEW` | `stage2_status` inny niż `OK`, nieretryowalny — czeka na decyzję operatora |
| `STRANDED_UNROUTABLE` | `stage2_status = 'OK'`, ale brakuje `client_code`, `stage2_report_type` albo cleaned artifactu, więc Stage 3 go odfiltrowuje |

Pliki te **nie są reprocesowane** — ciche ponowienie pliku odstawionego do przeglądu to dokładnie sposób na duplikat loadu w Stage 3. Nie liczą się do `attempted_count` i nie są `batch failure`; podnoszą `operator_action_required`, więc cykl kończy się `SUCCEEDED_WITH_REVIEW_ITEMS` i sygnał powtarza się przy każdym uruchomieniu aż operator go rozwiąże. Predykaty `STAGE2_REDISCOVERY_SQL` i `STAGE3_ROUTABLE_SQL` są zapisane raz, a test na jednorazowym PostgreSQL dowodzi, że sweep jest dokładnie ich dopełnieniem — porównany zarówno z żywym `_candidate_rows` Stage 2, jak i z `_select_stage3_candidates` Stage 3. Run adresowany (`raw_file_ids` / `input_files` / `input_dir`) nie reconciluje. Plik z ustawionym `stage3_status` — w tym `'ERROR'` — należy do recovery Stage 3 (`P0-E`), nie do tego sweepa.

**Własność w tym samym cyklu.** Sweep pomija plik obecny już w `result.items` tylko wtedy, gdy istniejący item realnie go posiada (`item_carries_operator_ownership`): ma `review_required`, jest outcome'em reconciliacji albo awarią techniczną (te i tak podnoszą `Stage2BatchError` i docierają wyżej jako `FAILED_*`). Sama obecność `raw_file_id` nie wystarcza. Plik przetworzony w **tym** cyklu do `stage2_status='OK'` bez `client_code` — produkcyjny kształt `report_112` — trafia do `result.items` jako zwykły `SUCCEEDED_CREATED` z `review_required=false`; gdyby sweep pominął go z powodu samego id, cykl, który go osierocił, zakończyłby się zwykłym `SUCCEEDED`, a stan ujawniłby się dopiero w następnym przebiegu 06:00/20:00. Dlatego trwała klasyfikacja **nadpisuje item w miejscu** (ta sama pozycja, bez duplikatu `raw_file_id`) i plik przestaje być liczony jako sukces Stage 2.

Detekcja typu raportu pozostaje sterowana Pythonem (klasy w `jobs/reports/stage2/types/` zarejestrowane w `jobs.reports.stage2.registry.REGISTERED_REPORTS`). Tabela `workflow_b_control.report_type_registry` jest read-modelem: na starcie joba Stage 2 wywołuje `reconcile_registry()` z tej samej registry i loguje `Stage2 registry reconciliation` (lista typów w DB i w Pythonie, status implementacji, `cleaner_entrypoint`). Drift jest sygnalizowany jako `WARNING`: `db_only_report_types` (DB widzi typ, którego Python nie zna — nie wykona się), `python_only_report_types` (kod ma typ, którego brakuje w read-modelu), `cleaner_mismatches` (różne `module:Class.method`). To samo źródło prawdy obowiązuje dla `Alpha_GPS_Baza_LOG`.

Uruchomienie:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage2.job_stage2 '{}'
```

Z limitem liczby plików lub diagnostyką detekcji:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage2.job_stage2 '{"limit":20}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage2.job_stage2 '{"debug_detection":true}'
```

Statusy Stage 2:

- `OK`: raport przeszedł detekcję, cleaning i walidację,
- `PENDING_REVIEW`: raport zablokowany (np. low detection confidence, schema mismatch, cleaning TODO).

Pola zapisywane do `ingest.raw_file`:

- `stage2_status`,
- `stage2_report_type`,
- `stage2_scores` (`JSONB`),
- `stage2_schema_diff` (`JSONB`),
- `stage2_pending_reason`,
- `stage2_updated_at`,
- `client_code` (jeżeli Stage 2 wykryje dokładnie jednego klienta na podstawie finalnego cleaned report i baz Workflow A).
- `stage2_outcome_category`, `stage2_retryable` i `stage2_cleaned_artifact_id` (migracja `048_workflow_b_stage2_batch_contract.sql`; bez backfillu historycznych wierszy).

Cleaned artifact ma scope `workflow_b.stage2.cleaned.v1`. Klucz to SHA-256 kanonicznego JSON bez spacji: `["workflow_b.stage2.cleaned","v1",raw_file_id,source_sha256]`. Zawiera wyłącznie trwały UUID raw-file, niezmienny Stage 1 SHA-256 i stałe/versioned role strings; nie zawiera klienta, nazwy pliku, ścieżki, run ID ani danych raportu. Zmiana semantyki/layoutu, która może zmienić kanoniczne bajty, wymaga świadomego bumpu kontraktu z `v1`.

Upload przekazuje `structured_response=True`. `created` i `reused` zapisują ten sam kanoniczny artifact ID, a sukces jest finalizowany dopiero po zapisie statusu/linku. To naprawia crash window upload-before-final-commit. HTTP 409 nie ma fallbacku do uploadu bez klucza: jest zapisywany jako nie-retryable `FAILED_IDEMPOTENCY_CONFLICT` i powoduje końcowy `Stage2BatchError`.

Publiczne `run(...)` zwraca serializowalny `Stage2BatchResult` z per-item outcome i licznikami created/reused/skips/review/rejections/failures. Brak pracy zwraca wynik zerowy. Review/rejection nie blokują niezależnych plików; techniczne lub integrity failures są akumulowane, po czym rzucany jest `Stage2BatchError` z partial result. Historyczne `PENDING_REVIEW/stage2_exception` jest retryable; inne niejawne historyczne reasons pozostają wykluczone do ręcznej oceny. Pełny orchestrator Workflow B pozostaje follow-upem.

Test runtime-source odzwierciedla obecną architekturę DB-backed: produkcyjna selekcja pochodzi z persisted `ingest.raw_file`, a `workflow_b_control.report_type_registry` może dostarczać konfigurację finalizacji. Pythonowe klasy nadal wykonują detekcję/cleaning. Historyczne rekordy `NORMALIZED`/Stage 2 `OK` z pustym `stage2_cleaned_artifact_id` są obsługiwane wyłącznie przez manualny `ops/reconcile_stage2_cleaned_artifact_links.py`; orchestrator nigdy nie uruchamia tej rekonstrukcji automatycznie.

Reconciliation domyślnie jest read-only. Wybiera persisted completed rows, wymaga dokładnie jednego artefaktu o lineage `workflow_b/stage_2_clean/cleaned`, zgodnego raw-file UUID, report/client metadata, rodzaju, content type, rozmiaru, SHA-256 i dostępnych versioned metadata. Poprawne legacy unkeyed artifacts są dozwolone; timestamp, filename i object key nigdy nie rozstrzygają kandydata. MinIO jest sprawdzane przez stat oraz metadata SHA-256 albo streaming digest bez zapisu kopii. Deterministyczny plan v1 zawiera wyłącznie safe control-plane IDs, ma kanoniczny SHA-256 niezależny od display timestamp i jest zapisywany z mode `0600`.

Future `--execute` wymaga osobno przejrzanego `--plan`, `--expected-digest` i `--expect-count`, zgodnej identity/commit ancestry, braku blocked entries i aktywnego Workflow B, oraz dedykowanego advisory locka. Po pełnej ponownej walidacji blokuje wszystkie raw-file rows `FOR UPDATE`, ustawia wyłącznie `stage2_cleaned_artifact_id` i commituję cały plan atomowo; drift lub affected-count mismatch rollbackuje całość. Status, reason, outcome, retry fields, timestamps, artifacts i MinIO nie są zmieniane.

Stage 2 przy uploadzie artefaktów (cleaned CSV, oryginalny/źródłowy CSV przy `PENDING_REVIEW`) przekazuje opcjonalnie `raw_file_id`: wiersz w `ingest.raw_file` jest wyszukiwany po `normalized_csv_path` lub `sha256` (ścieżka przetwarzanego pliku). Dzięki temu w tabeli `artifacts` kolumna `raw_file_id` (FK do `ingest.raw_file(id)`) wypełnia się i powstaje ślad: raw_file → artifact (traceability / lineage w obrębie **Workflow B**). Jeśli dla ścieżki nie ma wiersza w ingest, job loguje ostrzeżenie i uploaduje artefakt bez `raw_file_id`.

Nowe artefakty Stage 2 używają layoutu v2:

- `workflow_name=workflow_b`
- `stage_name=stage_2_clean`
- `artifact_role=cleaned` dla cleaned CSV
- `artifact_role=debug_sample` dla źródłowego CSV uploadowanego przy `PENDING_REVIEW`
- `report_type=<detected type>` (np. `report_207` albo `report_d105_2_ecodriving`) dla zwykłych artefaktów; dla low detection confidence debug artifact ma `report_type=PENDING_REVIEW`, a najlepszy kandydat zostaje w `ingest.raw_file.stage2_report_type` oraz w `metadata_json.detected_candidate_report_type`
- `original_filename` z `ingest.raw_file.original_filename`, jeśli dostępny
- `client_code=<resolved client_code>` dla cleaned artefaktów i debug artefaktów tworzonych po finalnym cleaned report, jeżeli detekcja klienta dała jednoznaczny wynik; w pozostałych przypadkach pozostaje `NULL`.

Po cleaning/validation, ale przed zapisem cleaned CSV, Stage 2 wykonuje finalizację:

- czyta `workflow_b_control.report_type_registry.id_sync_column_name` dla wykrytego `report_type`; jeśli pole jest puste, loguje ostrzeżenie i pomija detekcję klienta,
- jeśli `id_sync_column_name` wskazuje istniejącą kolumnę cleaned report, zbiera unikalne niepuste wartości i szuka ich we wszystkich włączonych bazach klientów Workflow A (`workflow_a_control.client_account`) w `client_trips.registration`, `client_trips.chassis_number` oraz `client_trips.driver_name`; matching jest dokładny po trimowaniu, bez fuzzy matching,
- dokładnie jeden dopasowany `client_code` jest zapisywany do `ingest.raw_file.client_code` i metadanych artefaktu; wiele dopasowanych kodów kończy przetwarzanie tego pliku błędem `ambiguous client_code detection`; brak dopasowań zostawia `client_code=NULL` i loguje ostrzeżenie,
- czyta `workflow_b_control.report_type_registry.record_id_ingredients` jako przecinkową listę kolumn cleaned report; jeżeli lista jest pusta, dodaje końcową kolumnę `record_id` z pustymi wartościami,
- jeżeli lista składników jest skonfigurowana, wszystkie wskazane kolumny muszą istnieć; dla każdego wiersza Stage 2 tworzy deterministyczny SHA-256 z kanonicznej tablicy wartości w skonfigurowanej kolejności i zapisuje go w końcowej kolumnie `record_id`,
- wyjątek runtime dotyczy wyłącznie `report_207`: Stage 2 ignoruje zbyt wąskie historyczne `record_id_ingredients` i generuje `record_id` z pełnego klucza biznesowego `Data i czas`, `Nr rejestracyjny`, `Prędkość`, `Ograniczenie prędkości drogowej`, `Lokalizacja` oraz deterministycznego `duplicate_ordinal` liczonego w kolejności cleaned report. Dzięki temu wiersze z tym samym timestampem i rejestracją, ale inną prędkością/limitem/lokalizacją, oraz w pełni identyczne powtórzone wiersze z jednego źródła nie są kolapsowane przez Stage 3.

Jeżeli po zakończeniu runu są pliki z `stage2_status=PENDING_REVIEW` i `stage2_pending_reason=low_detection_confidence`, Stage 2 wysyła jedno zbiorcze powiadomienie email z tabelą HTML do operatora. Wiadomość zawiera run id, nazwę/ścieżkę pliku, `original_filename`, najlepszy kandydat typu raportu, score, reason i link do szczegółów artefaktu w Artifact Explorer. SMTP i adresaci są konfigurowani wyłącznie przez ENV (`AUTOMATION_SMTP_*`, `STAGE2_PENDING_REVIEW_NOTIFY_TO`, `ARTIFACT_EXPLORER_BASE_URL`). Błąd wysyłki jest logowany jako `WARNING` i nie zmienia wyniku Stage 2 ani zapisanych danych.

Repo nie ma trwałej tabeli stanu powiadomień dla runów; implementacja zapobiega duplikatom w obrębie jednego procesu joba. Jeżeli operator uruchomi osobny retry z tym samym zestawem plików, wiadomość może zostać wysłana ponownie.

Przykładowy `storage_key`:

```text
workflow_b/stage_2_clean/yyyy=2026/mm=05/dd=12/run_id=<uuid>/report_type=report_207/cleaned/report_207__20260512T221144Z__ac3866ec__cleaned.csv
```

### `jobs.reports.stage3.job_stage3` — Workflow B, Stage 3

Funkcjonalność:

- wybiera z `ingest.raw_file` pliki ze `stage2_status='OK'`, niepustym `client_code`, niepustym `stage2_report_type`, istniejącym cleaned artifactem Stage 2 i statusem Stage 3 kwalifikującym do batcha: `stage3_status` pusty/`NULL`, wcześniejszy `OK`, dla którego `stage2_updated_at > stage3_finished_at`, a od P0-E także `RUNNING` po upływie stale grace oraz `ERROR` — zakwalifikowanie do discovery nie jest decyzją o ładowaniu, patrz „Stage 3 — odzyskiwanie po przerwaniu (P0-E)” niżej,
- znajduje odpowiadający cleaned artifact Stage 2 (`workflow_name=workflow_b`, `stage_name=stage_2_clean`, `artifact_role=cleaned`, zgodny `report_type`, zgodny `raw_file_id`); ponieważ API artefaktów przechowuje layout components w formie zsanityzowanej (`Alpha_GPS_Baza_LOG` → `alpha_gps_baza_log`), Stage 3 akceptuje zarówno literalny `stage2_report_type`, jak i jego wariant zsanityzowany,
- pobiera cleaned CSV przez API platformy (`GET /artifacts/{artifact_id}/download`) i ładuje go do bazy klienta wskazanej przez `workflow_a_control.client_account.client_code`,
- wymaga przygotowanego przez kontrolowaną migrację schematu `telematics_reports`, tabeli o nazwie wykrytego typu raportu (np. `telematics_reports.report_207`) oraz wymaganych kolumn; recurring runtime nie wykonuje DDL,
- przechowuje kolumny raportu jako `TEXT` i zapisuje techniczne metadane `_loaded_at`, `_raw_file_id`, `_source_artifact_id`, `_source_filename`, `_stage3_run_id`; brak tabeli/kolumny/indeksu kończy element typed `FAILED_SCHEMA_NOT_READY` (non-retryable, operator action required) ze wskazaniem `db/client_business/042_workflow_b_stage3_runtime_schema.sql`,
- zapisuje wynik do pól `stage3_*` w `ingest.raw_file` i uploaduje artefakt `load_result`; dla odrzuconych/pominiętych wierszy z `record_id` tworzy także `rejected_rows`.
- obsługuje `dry_run=true`: wykonuje tę samą selekcję, rozwiązuje klienta i artifact, pobiera cleaned CSV, waliduje plan ładowania oraz wypisuje/loguje podsumowanie bez DDL/DML ani zmian `stage3_*`.

Publiczne `run(...)` deleguje do `process_stage3_batch(...)` i zwraca `Stage3BatchResult`. `Stage3ItemResult` klasyfikuje production load, dry-run, completed/ineligible/routing skips, validation, environment identity, permission, infrastructure i database-load failures. Niezależne pliki nadal są commitowane osobno; po akumulacji błędów `Stage3BatchError` udostępnia partial result. Zerowy wybór jest typed sukcesem, natomiast identity/permission/infrastructure failure nie jest raportowany jako no-work.

Każdy udany production load ze statusem `OK` tworzy bezpieczny `Stage3SuccessfulLoadIdentity`: `raw_file_id`, `client_code`, `report_type`, target schema/table, cleaned artifact ID i final status. Dry-run nigdy nie trafia do tej listy. Stage 3 sam nie uruchamia postprocessora; istniejący parent orchestrator konsumuje te identity po zakończeniu Stage 3.

Parametry:

- `limit` (opcjonalnie; limit liczby kandydatów),
- `raw_file_id` (opcjonalnie; przetworzenie jednego pliku),
- `force_reprocess` (opcjonalnie, domyślnie `false`; działa tylko z `raw_file_id` i pozwala ominąć filtr kwalifikowalności `stage3_status`),
- `dry_run` (opcjonalnie, domyślnie `false`; read-only walidacja planu ładowania),
- `persist_dry_run_result` (opcjonalnie, domyślnie `false`; przy `dry_run=true` uploaduje JSON `dry_run_result`, więc zapisuje artifact/audit do platformy),
- `auto_grant_permissions` jest zachowany wyłącznie jako parametr kompatybilności wejścia; wartość `true` jest deterministycznie odrzucana jako non-retryable/operator-action-required, ponieważ recurring runtime nie może nadawać grantów ani wykonywać bootstrapu schematu.

Uruchomienie:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage3.job_stage3 '{"limit":20}'
```

Walidacja przed produkcyjnym loadem:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage3.job_stage3 '{"raw_file_id":"<uuid>","dry_run":true}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.stage3.job_stage3 '{"limit":10,"dry_run":true}'
```

Dry-run jest domyślnie read-only wobec bazy platformowej i baz klientów. Nie tworzy schematu `telematics_reports`, tabel, kolumn ani indeksów; nie insertuje, nie aktualizuje, nie kasuje wierszy i nie zmienia `ingest.raw_file.stage3_status`. Dla każdego kandydata wypisuje na stdout i loguje strukturę z m.in. `source_artifact_id`, `input_rows`, semantyką `record_id`, polityką `data_overwrite`, stanem schematu/tabeli, planowanymi kolumnami, wykrytymi duplikatami istniejących `record_id`, licznikami `would_insert_rows` / `would_update_rows` / `would_skip_rows` / `would_reject_rows` oraz `dry_run_status` (`OK`, `WARNING`, `ERROR`).

`persist_dry_run_result=false` nie tworzy audytowego artifactu; `true` zachowuje dotychczasowy jawny upload `dry_run_result`, ale nadal nie zapisuje danych klienta ani `stage3_*`. Symulowane liczniki pozostają w dry-run item i nie są przedstawiane jako committed successful-load identity.

`force_reprocess=true` działa wyłącznie z jawnym `raw_file_id` i omija tylko filtr zakończonego `stage3_status`; nadal wymaga Stage 2 `OK`, klienta i typu raportu. Nie omija docelowych reguł: z `record_id` nadal działa skip/upsert według `data_overwrite`, bez użytecznego `record_id` overwrite=false nadal skipuje, overwrite=true (oraz specjalny Alpha GPS target) może zastąpić zawartość. Force nie jest uniwersalnie bezpieczny i przyszły orchestrator nie może ustawiać go domyślnie.

Stage 1/2/3 mają bezpiecznie serializowalne typed results konsumowane bez parsowania logów przez `jobs.reports.workflow_b.orchestrator`. Timer `log-workflow-b.timer` (06:00/20:00 Europe/Warsaw) jest zainstalowany i aktywny.

#### Stage 3 — odzyskiwanie po przerwaniu (P0-E)

Przed P0-E `stage3_status` w stanie `RUNNING` lub `ERROR` był **terminalny przez pominięcie**: żadna kwerenda discovery, żadna rekoncyliacja i żaden watchdog nie wybierały takiego wiersza ponownie. Proces zabity milisekundę po commicie `_mark_stage3_started` usuwał plik z systemu autonomicznego na stałe. Kontrakt naprawia to bez zmiany schematu — wszystkie potrzebne dowody już istnieją.

**Źródłem prawdy jest cel, nie etykieta.** `stage3_status='RUNNING'` nie odróżnia „transakcja docelowa nigdy się nie otworzyła” od „commit docelowy przeszedł, a proces zginął przed finalizacją wiersza platformowego”. Pierwszy przypadek trzeba powtórzyć, drugi powtórzony na ślepo duplikuje dane klienta. Dlatego decyzję podejmuje `jobs/reports/stage3/recovery.py` na podstawie dowodów trwałych:

- `_mark_stage3_started` stempluje `stage3_destination_schema` / `stage3_destination_table` **przed** jakąkolwiek pracą docelową, więc nawet wiersz po awarii wskazuje swój własny cel;
- każdy wiersz docelowy niesie identyfikator **cleaned artifactu**, z którego powstał — i to on, a nie `raw_file_id`, jest właściwym dowodem.

**Tożsamość próby to tożsamość treści, nie tożsamość pliku.** Zliczanie wierszy po `raw_file_id` dowodzi tylko, że *kiedyś* jakiś load tego pliku się scommitował. Nie odróżnia sekwencji:

> load się udaje → Stage 2 ponownie czyści plik → nowa próba stempluje `RUNNING` → proces ginie przed commitem nowej transakcji docelowej

Stare wiersze wciąż tam są. Zliczanie po `raw_file_id` uznałoby to za „scommitowane”, sfinalizowało wiersz platformowy jako `OK` i nowsza generacja nigdy nie zostałaby załadowana — cicha utrata danych. Ponowne czyszczenie tworzy **nowy** artifact, więc wiersze poprzedniej generacji noszą poprzedni identyfikator i nigdy nie mogą zostać wzięte za pracę bieżącej próby. Nie wymaga to porównywania zegarów dwóch baz ani nowej kolumny.

**Kolumny provenance różnią się per strategia ładowania — nie ma uniwersalnego `_raw_file_id`.** Zweryfikowane na produkcji:

| strategia | tabela | kolumna pliku | kolumna cleaned artifactu | znacznik czasu |
|---|---|---|---|---|
| `TELEMATICS_TECHNICAL` | `telematics_reports.<report_type>` | `_raw_file_id` | `_source_artifact_id` | `_loaded_at` |
| `ALPHA_GPS_REPLACE_ALL` | `telematics_reports."Alpha_GPS_Baza_LOG"` | `raw_file_id` | `cleaned_artifact_id` | `imported_at` |

Sonda zakodowana na `_raw_file_id` uznałaby każdy przerwany load Alpha GPS za niemożliwy do zbadania — a przez tę ścieżkę przechodzą 44 produkcyjne pliki Stage-3 `OK`. Mapowanie żyje w `DESTINATION_PROVENANCE` i jest wybierane przez `_stage3_load_strategy`, które odwzorowuje gałąź faktycznie wybieraną przez `_load_dataframe_to_destination`.

Sonda jest read-only (`SET TRANSACTION READ ONLY`), działa na osobnym połączeniu i wykonuje się **po** bramce P0-G — plik zablokowany polityką nadal nigdy nie otwiera połączenia do bazy klienta.

**Macierz własności stanów trwałych.** Każdy stan ma deterministycznego następnego właściciela; to jest cały invariant P0-E.

| `stage3_status` | terminalny | retryable | następny właściciel | wybierany przez discovery |
|---|---|---|---|---|
| `NULL` / `''` | nie | tak | zwykły Stage 3 | tak |
| `OK` (nieprzedawniony) | tak | nie | nikt — praca skończona | nie |
| `OK` z nowszym Stage 2 | nie | tak | zwykły Stage 3 (supersede) | tak |
| `RUNNING`, lock zajęty | nie | nie teraz | żywy proces trzymający lock (`AWAIT_LIVE_OWNER`) | tak, ale odkładany |
| `RUNNING`, lock wolny, w oknie grace | nie | nie teraz | następny cykl (`AWAIT_GRACE`) | tak, ale odkładany |
| `RUNNING`, lock wolny, po grace | nie | tak | recovery: replay albo rekoncyliacja wg dowodu | tak |
| `ERROR`, wiersze bieżącej próby scommitowane | nie | — | rekoncyliacja bez ponownego ładowania | tak |
| `ERROR`, znacznik `retry=yes` | nie | tak | recovery: replay (przejściowa infrastruktura) | tak |
| `ERROR`, znacznik `retry=no` | tak (do decyzji) | nie | operator — deterministyczna walidacja writera / konfiguracja | tak, po to by zgłosić |
| `ERROR` bez znacznika | tak (do decyzji) | nie | operator — brak trwałej klasyfikacji | tak, po to by zgłosić |
| `SKIPPED_NO_RECORD_ID` | tak (z założenia) | nie | operator — decyzja o danych/polityce | nie |
| nieznany | — | nie | operator — klasyfikacja | tak, ale tylko po to, by zgłosić |

Retryowalność `ERROR` jest rozstrzygana z **trwałego** znacznika, a nie z kadencji ani z klasy wyjątku Pythona. Wcześniejszy projekt ponawiał każdy `ERROR`, opierając ograniczenie na tym, że Workflow B odpala tylko dwa razy dziennie — co dla awarii deterministycznej oznacza czerwony cykl dwa razy na dobę bez ścieżki wyjścia.

**Macierz okien awarii.** Dla każdego okna wynik jest jednoznaczny i żadne nie kończy się trwałym osieroceniem.

| okno | co się stało | dowód trwały | działanie recovery |
|---|---|---|---|
| A | kandydat wybrany, brak zapisów | `stage3_status` nadal `NULL` | zwykłe przetworzenie |
| B | `_mark_stage3_started` scommitowane | `RUNNING`, 0 wierszy z bieżącym cleaned artifactem | `SAFE_REPLAY` |
| C | transakcja docelowa otwarta, bez commitu | rollback po stronie serwera; 0 wierszy | `SAFE_REPLAY` |
| D | commit docelowy wykonany | >0 wierszy z bieżącym cleaned artifactem | `RECONCILE_COMMITTED` — bez ponownego ładowania |
| E | dowód platformowy nie zdążył powstać | j.w. | `RECONCILE_COMMITTED` |
| F | postprocesor wystartował | j.w. | `RECONCILE_COMMITTED` + ponowne wydanie identity |
| G | efekt uboczny postprocesora scommitowany | dane docelowe postprocesora | `RECONCILE_COMMITTED`; postprocesor sam raportuje `SKIPPED_ALREADY_COMPLETED` |
| H | zapis końcowego `stage3_status` przerwany | `RUNNING`/`ERROR` + >0 wierszy | `RECONCILE_COMMITTED` |

`RECONCILE_COMMITTED` finalizuje wiersz platformowy z licznika wierszy docelowych, **nie pobiera ponownie artefaktu i nie wykonuje żadnego zapisu docelowego**, a wynik dostaje własny outcome `RECOVERED_RECONCILED`, liczony oddzielnie od `LOADED`.

**Duplikacja efektów postprocesora — bezpieczeństwo jest *deklarowane*, nie zakładane.** Plik po rekoncyliacji nadal wystawia `Stage3SuccessfulLoadIdentity` (oznaczony `recovered=True`), bo awaria mogła nastąpić przed postprocesorem (okna F–H). Nie wynika to jednak z żadnego ogólnego założenia, że postprocesory są zbieżne — **`report_207` zbieżny nie jest**. Dwa obecne postprocesory są bezpieczne z **różnych** powodów:

| postprocesor | efekt | dlaczego replay nie duplikuje |
|---|---|---|
| `report_207_speeding_migration` | **inkrementuje** liczniki przekroczeń | **nie jest zbieżny.** Bezpieczny wyłącznie dzięki znacznikowi: kandydaci to `COALESCE(migrated_to_client_db, FALSE) IS NOT TRUE`, a inkrementacja (`updated_trips`) i oznaczenie (`marked_migrated`) są CTE **jednej** instrukcji, więc wiersz jest albo zinkrementowany i oznaczony, albo żadne z tych dwóch. Orkiestrator nigdy nie ustawia `force_retry_errors`, więc na ścieżce autonomicznej zawsze rządzi znacznik. |
| `alpha00001_dysponent_id_enrichment` | **ustawia** `dysponent_id` | zbieżny: `planned_updates` liczy tylko przejazdy z `dysponent_id IS NULL` (lub różnym przy jawnym overwrite), więc drugie uruchomienie dotyka zera wierszy i zwraca `SKIPPED_ALREADY_COMPLETED`. |

Każdy wpis w `POSTPROCESSOR_REGISTRY` deklaruje `recovery_safety` wraz z uzasadnieniem; domyślną wartością jest `UNKNOWN`. Jeśli tożsamość oznaczona `recovered=True` trafi na postprocesor, który nie zadeklarował bezpieczeństwa replayu, orkiestrator podnosi `Stage3RecoveryReofferNotDeclaredSafe` i cykl kończy się widoczną porażką — nowy postprocesor nie dziedziczy statusu „bezpieczny” przez przemilczenie, a ciche pominięcie planu zostawiłoby plik wyglądający na w pełni przetworzony.

**Fail-closed.** Jeśli sondy nie da się wykonać (brak kolumn provenance danej strategii, nieosiągalna baza klienta, nienazwany cel, nierozpoznana strategia), wynik to `TERMINAL_OPERATOR`, a nie replay. „Nie udało się sprawdzić” nigdy nie zamienia się w „można ładować ponownie”.

**Zero wierszy nie dowodzi braku commitu.** Obie obecne strategie mogą legalnie scommitować pusty wynik: Alpha GPS commituje samo `DELETE`, gdy skoroszyt sparsował się do zera wierszy, a ścieżka insertowa `telematics` commituje, gdy wszystkie wiersze odpadły. Dlatego kontrakt nie brzmi już „`rows == 0` ⇒ `SAFE_REPLAY`”, tylko: replay jest wykonywany, **ponieważ writer danej strategii jest zadeklarowany jako idempotentny** (`replay_is_idempotent`), co dopiero czyni powtórzenie pustego commitu nieszkodliwym. Strategia bez tej deklaracji trafia do `TERMINAL_OPERATOR`.

**Trwała klasyfikacja `ERROR` — retryowalność jest jawną, trwałą semantyką.** `_mark_stage3_error` poprzedza `stage3_error` znacznikiem `[stage3 category=<kategoria> retry=yes|no]`, wyliczanym przez `classify_stage3_exception` — tę samą funkcję, z której korzysta raportowany `Stage3ItemResult`, więc obie strony nie mogą się rozjechać (jest na to test parzystości dla każdej rodziny). Bez tego po restarcie każdy `ERROR` wygląda tak samo.

Pytanie rozstrzygające brzmi: **„czy powtórzenie dokładnie tej próby na niezmienionym trwałym wejściu i niezmienionej konfiguracji może się powieść?”** — a nie „czy `RuntimeError` bywa przejściowy”.

| rodzina | przykłady | `retry` | następny właściciel |
|---|---|---|---|
| **deterministyczna walidacja writera** (`Stage3WriterValidationError`) | `empty_result` Alpha GPS; błędna data/wiersz w skoroszycie; brak wymaganych kolumn; niepoprawne kolumny cleaned reportu (pusta nazwa, bajt NUL, kolizja z kolumną techniczną, duplikat); odmowa z powodu duplikatów `record_id` blokujących indeks unikalny; niebezpieczny identyfikator docelowy; pusty/brakujący pobrany artefakt; brak lub niekompletne konto klienta | `no` | **operator** — bez autonomicznego replayu |
| **przejściowa infrastruktura** | `psycopg.OperationalError`, zerwane połączenie, błędy I/O przy pobieraniu artefaktu | `yes` | recovery autonomiczne |
| **konfiguracja / schemat / uprawnienia** | `Stage3SchemaReadinessError`, `EnvironmentIdentityError`, `PermissionError`, `ValueError`/`KeyError`/`TypeError` | `no` | operator |

Cleaned artifact jest niezmienny, kształt tabeli docelowej jest jaki jest, a wiersz konta klienta albo istnieje, albo nie — więc kolejne odpalenie o 06:00 odtwarza identyczną awarię. Wcześniejsza wersja klasyfikowała te przypadki jako `retryable=true` (przez ogólny fallback `RuntimeError`), co zamieniało jedno złe wejście w czerwony cykl dwa razy na dobę bez ścieżki wyjścia.

**Sygnał jest strukturalny, nie tekstowy.** `Stage3WriterValidationError` niesie pole `signal` (np. `alpha_gps_rows_rejected`, `cleaned_report_columns_invalid`, `load_plan_rejected`) i to ono trafia do kategorii. Klasyfikator nigdy nie dopasowuje fragmentów komunikatów w rodzaju `"invalid date"` — przeredagowanie komunikatu nie może po cichu zmienić produkcyjnego zachowania recovery. Typ dziedziczy po `RuntimeError`, więc istniejące `except RuntimeError` (m.in. w `_build_load_plan`) działają bez zmian.

Wiersz bez znacznika — np. sprzed tego kontraktu — dostaje `TERMINAL_OPERATOR`, nigdy zgadywanego retry. **Dowód commitu ma pierwszeństwo przed znacznikiem**: wyjątek podniesiony *po* commicie docelowym również ląduje w `ERROR`, więc najpierw pytana jest baza docelowa, i `RECONCILE_COMMITTED` wygrywa nawet przy `retry=no`.

**Własność wiersza `RUNNING` to lock, nie zegar.** `log-workflow-b.service` dopuszcza `TimeoutStartSec=6h`, `log-job@.service` 4 h, a samodzielne uruchomienie Stage 3 przez `ops/runner.py` jest nieograniczone i nie bierze locka orkiestratora — żaden próg zegarowy nie dowodzi więc, że nie żyje już żaden uprawniony właściciel. Dlatego Stage 3 **jawnie** obejmuje plik sesyjnym lockiem doradczym (`stage3_file_advisory_lock_key`) zanim go dotknie, dla **każdego** kandydata, nie tylko odzyskiwanego — co przy okazji uniemożliwia dwóm równoległym uruchomieniom Stage 3 załadowanie tego samego pliku. Lock sesyjny PostgreSQL jest zwalniany przez serwer wraz z sesją, więc proces po awarii przestaje być właścicielem natychmiast, a żywy pozostaje nim dowolnie długo. Recovery może działać wyłącznie wtedy, gdy zdoła ten lock przejąć.

`DEFAULT_STAGE3_STALE_GRACE_MINUTES = 480` jest już tylko zabezpieczeniem drugiego rzędu, ustawionym **powyżej** każdego wspieranego limitu wykonania (6 h), a nie strojonym: wcześniejsza wartość 240 min była krótsza niż `TimeoutStartSec`, więc mogła odebrać plik żywemu, 5-godzinnemu loadowi. Jednocześnie 480 min pozostaje poniżej 10–14-godzinnej przerwy między odpaleniami, więc osierocony plik wraca w następnym cyklu. Próg pliku nie jest równy progowi runu z `ops/execution_watchdog.py` (240 min) i nie powinien być — odpowiadają na różne pytania; wymagane jest jedynie uporządkowanie: plik nie staje się odzyskiwalny wcześniej, niż watchdog zgłosiłby run jako `STALE`.

### `jobs.reports.workflow_b.orchestrator` — parent Workflow B

#### Kontrakt operacyjny Workflow B (autonomiczny mailbox ingestion)

Workflow B jest **autonomicznym potokiem ingestu i przetwarzania skrzynki pocztowej**. Skrzynka
przyjmuje raporty generowane automatycznie przez systemy zewnętrzne (np. FleetWeb) **oraz** raporty
wysłane ręcznie przez właściciela; oba są równoprawnym wejściem. Stage 1 nie filtruje nadawcy —
`IMAP_SENDER_FILTERS` / `IMAP_SENDER_FILTER` są puste domyślnie i puste w produkcji.

**Za co Workflow B odpowiada:**

1. **Wykonanie cyklu.** Uruchomienie zgodnie z harmonogramem (`log-workflow-b.timer`, 06:00 i 20:00
   Europe/Warsaw). Cykl, który nie wystartował, wywrócił się, nie zdobył zasobów albo nie dosięgnął
   infrastruktury, musi być wykrywalny — patrz *odpowiedzialność watchdoga* niżej.
2. **Inspekcję skrzynki.** Każde udane wykonanie sprawdza skonfigurowany folder IMAP pod kątem
   nowych, wcześniej niewidzianych wiadomości i plików.
3. **Przetworzenie każdego znalezionego raportu.** Rozpoznanie typu, identyfikacja klienta, walidacja
   struktury, normalizacja, wyznaczenie docelowego handlingu i load do właściwej tabeli lub
   destynacji. Typy raportów nie są przetwarzane identycznie; zachowanie specyficzne dla typu jest
   celowe.
4. **Doprowadzenie każdego wejścia do stanu terminalnego lub jawnie nierozwiązanego** — patrz
   *cykl życia znalezionego wejścia*.

**Za co Workflow B NIE odpowiada.** Workflow B **nie przewiduje**, czy konkretny klient/raport
*powinien* przysłać e-mail w poniedziałek, co 7 dni albo w ciągu 10 dni. **Brak nowego raportu nie
jest sam w sobie awarią Workflow B.** W szczególności nie istnieją i nie mają być wprowadzane:
harmonogramy oczekiwanego przyjścia per klient, deadline'y tygodniowe/poniedziałkowe, `max_age_days`
ani source max-age SLA, `SOURCE_REPORT_MISSING` z samego faktu braku maila, kalendarze świąt dla
oczekiwanej dostawy, ani monitoring świeżości źródła oparty wyłącznie na czasie od ostatniego maila.
Wcześniejszy kierunek `P0-B` („expected report arrival / source freshness") jest **wycofany** —
`docs/17` §5.2 i §5.10.

**„Czegoś brakuje" oznacza tu wyłącznie: brakuje czegoś do przetworzenia wejścia, które JUŻ
przyszło** — nie da się ustalić klienta, nie da się ustalić typu raportu, klasyfikacja jest
niejednoznaczna, brak wymaganej konfiguracji/load policy, brak wymaganych kolumn/danych, format
nieobsługiwany, normalizacja się nie kończy, nie da się wyznaczyć destynacji, load się nie udaje,
wymagany jest przegląd operatora. Nigdy nie oznacza „raport, którego oczekiwaliśmy, nie przyszedł".

**Zdrowe `SUCCEEDED_NO_WORK`.** Cykl, który faktycznie się wykonał, poprawnie sprawdził skrzynkę, nie
znalazł nowego wejścia i nie napotkał nierozwiązanego błędu technicznego, jest **zdrowy** i zalicza
harmonogram. Pusta skrzynka nie jest awarią. Odwrotność jest jednak twardo pilnowana:
`_assert_mailbox_was_inspected()` nie pozwala policzyć `SUCCEEDED_NO_WORK` cyklowi, którego Stage 1
zakończył się bez `mailbox_check_completed` — taki cykl jest `FAILED_RETRYABLE` z kategorią
`mailbox_check_not_completed`. Dziś nieosiągalne (każda awaria dostępu do skrzynki — login, SELECT,
SEARCH, FETCH — podnosi `Stage1BatchError`), ale to jedyne twierdzenie w całym workflow, którego
pomyłka jest niewidoczna, więc jest asercją, nie założeniem.

**Cykl życia znalezionego wejścia.** Każdy raport/plik, który wszedł do Workflow B, musi osiągnąć
trwały, wytłumaczalny stan: przetworzony (`stage3_status='OK'`), duplikat
(`ingest.raw_file.status='DUPLICATE_CONTENT'`, dedup po `sha256` / `http_range_fp`), jawnie
zignorowany na podstawie trwałej reguły (wiadomość bez obsługiwanego załącznika lub linku,
rozszerzenie spoza `ALLOWED_EXTENSIONS`), albo nierozwiązany i wymagający działania. Wejście, które
weszło do Workflow B, **nigdy nie może stać się niewidoczne tylko dlatego, że potok nie potrafił go
zrozumieć**.

**Semantyka nierozwiązanego wejścia — sygnał per wejście (`WORKFLOW_B_INPUT_UNRESOLVED`).** Sam
poziom runu nie wystarcza: `SUCCEEDED_WITH_REVIEW_ITEMS` nad trwałym backlogiem jest permanentnie
nasycony i 60. nowy problem nie tworzy w nim żadnego materialnie nowego sygnału. Dlatego
`jobs/reports/workflow_b/unresolved_inputs.py` wystawia **osobny trwały incydent
`suspected_bug` na każde (wejście, powód blokady)**:

- fingerprint zawiera `raw_file_id`, etap i `reason_code`, więc dwa różne zablokowane pliki to dwa
  incydenty, a nowo zablokowany plik jest `REASON_NEW` i dociera do operatora mimo otwartego
  backlogu;
- wejście, którego incydent jest już otwarty, **nie jest ponownie raportowane**, więc powtarzane
  skanowanie tego samego niezmienionego problemu nie generuje duplikatów alertów ani lawiny
  przypomnień;
- incydent jest zamykany **wyłącznie na dowód właściwy dla etapu**. `stage3_status='OK'`,
  `DUPLICATE_CONTENT` i brak wiersza dowodzą zakończenia łańcucha ingest/clean/load i zamykają
  incydent `stage1`/`stage2`/`stage3`. Nie dowodzą **niczego** o pracy należnej po loadzie, więc nie
  zamykają incydentu `postprocess`: ten zamyka wyłącznie zaobserwowane w tym cyklu ukończenie
  postprocesora dla tego wejścia (`SUCCEEDED` / `SKIPPED_ALREADY_COMPLETED` i bez
  `operator_action_required`) albo zniknięcie wiersza. Platforma nie ma trwałego zapisu wykonania
  postprocesora — plany powstają z tożsamości załadowanych przez Stage 3 w tym samym cyklu, więc po
  `stage3_status='OK'` postprocesor nigdy nie jest ponownie oferowany — i nie dodano schematu, żeby
  taki zapis wymyślić: brak lub niejednoznaczność dowodu zostawia incydent otwarty;
- zastąpienie powodu jest **uporządkowane**: incydent starego powodu zamyka się dopiero wtedy, gdy
  nowy jest **otwarty** w ponownym odczycie tabeli incydentów po zapisie. Zastępca odłożony przez
  limit albo taki, którego zapis się nie powiódł, zostawia poprzednika otwartego — stan
  `nierozwiązane wejście → stary incydent zamknięty → brak zastępcy` nie może zaistnieć nawet
  przejściowo. Wejście, którego cykl w ogóle nie oglądał (ucięty sweep, etap, który się nie
  wykonał), zachowuje otwarty incydent;
- pobór nowych incydentów w jednym cyklu jest ograniczony, a limit konsumują **otwarte incydenty,
  nie próby**: chroni outbox przed lawiną, a to własność tego, co faktycznie się zapisało. Nieudana
  próba jest logowana (pojedynczo do `MAX_LOGGED_REPORT_FAILURES`, dalej zbiorczo) i **zwalnia
  slot** kolejnemu kandydatowi w tym samym cyklu, więc trwale niezapisujący się fingerprint nie może
  zagłodzić stojących za nim. Cichego obcięcia nie ma — to, czego limit nie wpuścił, jest **zawsze
  jawnie logowane**;
- limit jest **rozdzielony według odtwarzalności wejścia**, bo „wejdzie w następnym cyklu" nigdy nie
  było prawdą dla wszystkich klas (patrz *Odtwarzalne a jednorazowe* niżej).
  `WORKFLOW_B_MAX_NEW_UNRESOLVED_INCIDENTS_PER_CYCLE` (domyślnie 20) dotyczy **wyłącznie wejść
  odtwarzalnych**; to, co odłoży, następny cykl rzeczywiście odnajdzie w trwałym stanie i podejmie.
  Wejścia **jednorazowe** mają własny, osobny budżet
  `WORKFLOW_B_MAX_NEW_ONE_SHOT_UNRESOLVED_INCIDENTS_PER_CYCLE` (domyślnie 100), więc żadna klasa nie
  może skonsumować pojemności przed zdarzeniem, którego żaden późniejszy cykl już nie wyprodukuje.
  Ponieważ Stage 2 jest dziś w całości jednorazowy, ten budżet spotyka **stałą populację**, a nie
  tylko świeżą pracę jednego cyklu — konsekwencje operacyjne opisuje *Skutek operacyjny
  jednorazowości Stage 2* niżej. Cokolwiek po tym przebiegu nie jest trwale otwarte — odłożone przez budżet **albo**
  takie, którego zapis się nie powiódł, bo dla wejścia jednorazowego to ta sama konsekwencja —
  trafia do **jednego** zbiorczego incydentu `WORKFLOW_B_UNRESOLVED_SIGNAL_TRUNCATED`, nazywającego
  te wejścia. Przynależność do tego zbioru ustala **ten sam ponowny odczyt tabeli incydentów** co
  supersedowanie: dowód, nie intencja.

**Odtwarzalne a jednorazowe wejścia nierozwiązane.** Odtwarzalność jest własnością **konkretnego
zdarzenia**, którą trzeba **udowodnić semantyką potoku**, a nie etykietą etapu. Niezmiennik:

> Zdarzenie może pójść ograniczoną ścieżką odroczenia „odtwarzalne" **wyłącznie** wtedy, gdy
> mechanizm, który miałby je ponownie zaoferować, jest — na podstawie dowodów z tego cyklu —
> udowodniony jako docierający ponownie dokładnie do tego zdarzenia w późniejszym naturalnym cyklu.

Wszystko inne, łącznie z każdym przyszłym źródłem zdarzeń, jest **jednorazowe**.
`UnresolvedInput.replayable` jest polem o domyślnej wartości `False`, ustawianym wyłącznie w
`collect_unresolved_inputs`, więc źródło, które „zapomni" odpowiedzieć na to pytanie, dziedziczy
wariant bezpieczny. Pomyłka w tę stronę kosztuje najwyżej wcześniejszy incydent wyciszany przez
dedup; pomyłka w drugą stronę to trwała utrata sygnału.

| źródło zdarzenia | etap nominalny | klasa | dowód ponownego odnalezienia / zachowanie fail-safe |
|---|---|---|---|
| Stage 2 — **dowolne pochodzenie**: sweep `reconcile_unrouted_stage2_files` (`STRANDED_UNROUTABLE`, `STRANDED_AWAITING_REVIEW`) **oraz** przetwarzanie w tym cyklu (`PENDING_HUMAN_REVIEW`, `UNSUPPORTED_REPORT`, `AMBIGUOUS_DETECTION`, `REJECTED_VALIDATION`, `FAILED_NON_RETRYABLE`, `FAILED_IDEMPOTENCY_CONFLICT`) | `stage2` | **jednorazowe** | brak dowodu, którego nie da się unieważnić wspieraną ścieżką repozytorium. Dowód „pozycji w prefiksie sweepu" obalono: `_candidate_rows(raw_file_ids=[…])` **pomija predykat kwalifikacji** i dopuszcza dowolny wiersz `NORMALIZED` po `id`, a `_persist_stage2` bezwarunkowo zapisuje `stage2_updated_at = NOW()`. Wiersz, który pozostał materialnie nierozwiązany i nieretryowalny, dostaje wtedy **najnowszy** klucz w całym zbiorze i sortuje się **ostatni** — za trwałym prefiksem sięgającym zapory `UNROUTED_SWEEP_MAX_PAGES × UNROUTED_SWEEP_LIMIT` (10 000) żaden późniejszy sweep już do niego nie dociera, a normalne wykrywanie nadal go odrzuca jako nieretryowalny. Drugą taką ścieżką jest `ops/renormalize_raw_file.py --apply --reset-stage2`, kasujący stan i pola porządkowe Stage 2 bez gwarancji wyczerpującego późniejszego odnalezienia. Argument o pozycji jest tak mocny, jak zbiór pisarzy mogących tę pozycję zmienić — więc dla Stage 2 nie jest dowodem w ogóle i nie da się go naprawić dokładniejszym testem pochodzenia |
| Stage 3 z trwałym statusem NULL / `''` / `ERROR` / `RUNNING` | `stage3` | **odtwarzalne** | `_stage3_batch_status_eligible_sql` (P0-E) ponownie dopuszcza dokładnie te statusy, a orchestrator **nie przekazuje `limit`** do `process_stage3_batch`, więc `_select_stage3_candidates` nie emituje klauzuli `LIMIT` i zwraca **cały** kwalifikujący się zbiór. Jedynym predykatem jest trwały `stage3_status` samego wiersza; `ORDER BY stage2_updated_at ASC, id ASC` decyduje o kolejności przetwarzania **wewnątrz** zbioru, którego każdy element i tak został wybrany. Dlatego mutacja przestemplowująca klucz sortowania przesuwa wiersz **wewnątrz zbioru nieograniczonego**, a nie **poza ograniczony prefiks** — to dokładnie ta różnica, której Stage 2 nie ma, i dlatego obalenie Stage 2 nie przenosi się tutaj |
| Stage 3 z jakimkolwiek innym trwałym statusem (także statusem wprowadzonym w przyszłości) | `stage3` | **jednorazowe** | nic go ponownie nie wybiera; decyzja czyta `persisted_status` pozycji, nie nazwę etapu |
| postprocesor | `postprocess` | **jednorazowe** | `_discover_postprocessor_plans` czerpie plany wyłącznie z tożsamości załadowanych przez Stage 3 **w tym cyklu**; po `stage3_status='OK'` plik nie jest już nigdy oferowany i nic trwałego nie zapisuje, że praca wciąż jest należna |
| Stage 1 | `stage1` | **jednorazowe** | zebrana pozycja zawsze nazywa raw file, więc wiersz `ingest.imap_message` jest zacommitowany i każdy kolejny cykl pomija wiadomość jako `REUSED_MESSAGE`; jedyna ścieżka odtwarzająca to batch rekoncyliacji artefaktów ograniczony `limit` — to nie jest gwarancja |
| źródło nieujęte w tabeli | dowolny | **jednorazowe** | domyślna wartość pola |

Produkcja udowodniła, dlaczego ten podział musi istnieć. `2026-08-24 20:00` przyszedł plik
`Alpha_GPS_Baza_LOG` ALPHA00001, przeszedł Stage 2, załadował 8 999 wierszy w Stage 3, a jego
postprocesor zwrócił `FAILED_NON_RETRYABLE / AMBIGUOUS_ENRICHMENT_MATCH` z
`operator_action_required`. Zdarzenie zostało zebrane jako 64. nierozwiązane wejście cyklu i
odłożone przez limit 20 za backlogiem 63 wejść Stage 2 — a w cyklu `2026-08-25 06:00` nie było go już
w populacji w ogóle (`unresolved_input_count` 64 → 63 przy `incidents_resolved: 0`). Widoczność
utrzymywał wyłącznie domenowy incydent `ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT`, a generyczny kontrakt
bezpieczeństwa nie może zależeć od tego, że każdy postprocesor alarmuje sam za siebie:

```text
ARRIVED -> STAGE 3 OK -> POSTPROCESS FAILED -> DEFERRED
        -> NEVER REDISCOVERED -> NO DURABLE ACTIONABLE SIGNAL
```

Trwały niezmienny kontrakt jest więc mocniejszy niż „limit odkłada do następnego cyklu":

```text
ARRIVED INPUT -> MATERIAL WORK REMAINS UNRESOLVED -> DURABLE ACTIONABLE SIGNAL EXISTS
```

Odłożenie przez budżet jest dopuszczalne **tylko** wtedy, gdy wejście jest udowodnienie odtwarzalne
albo gdy równoważny trwały dowód dla dokładnie tego wejścia i warunku już istnieje.

**Skutek operacyjny jednorazowości Stage 2.** „Jednorazowe" znaczy **„musi stać się trwale
aktionowalne w cyklu, który to zaobserwował"** — nie znaczy „wystąpi tylko raz". Trwałe warunki
Stage 2 z reguły **będą** obserwowane ponownie w kolejnych cyklach; poprawność po prostu na tym nie
polega. Bilans dla realnej produkcji:

- **pierwsza aktywacja.** Trwały zbiór nierozroutowanych plików Stage 2 to rząd wielkości 60 pozycji
  rosnący o ~4 tygodniowo. Mieści się poniżej budżetu jednorazowego 100, więc pierwszy cykl otwiera
  ~60 incydentów naraz zamiast drenować po 20 na cykl przez trzy doby. Każdy z nich to jeden wiersz
  outboxu `REASON_NEW`; worker dostarczający chodzi co 5 minut po
  `SUSPECTED_BUG_EMAIL_WORKER_BATCH_SIZE` (10), czyli 120/godz., więc taki impuls rozkłada sam
  outbox znacznie poniżej godziny i nigdy nie powstaje synchroniczna lawina wysyłek;
- **każdy kolejny cykl.** Te same pliki trafiają na otwarty incydent po fingerprincie, wypadają z
  `pending` **zanim** budżet jest w ogóle konsultowany i nie generują żadnej nowej wiadomości. Stała
  populacja kosztuje powiadomienia **raz**, nie raz na cykl — wyciszenie robi dedup, nie limit;
- **backlog powyżej budżetu.** To, czego budżet nie wpuścił, i tak jest trwale reprezentowane —
  z dokładną licznością i digestem pełnego zbioru — przez `WORKFLOW_B_UNRESOLVED_SIGNAL_TRUNCATED`;
  gdy i tego nie da się zapisać, cykl fail-closuje (niżej). Reszta dostaje incydenty indywidualne w
  kolejnych cyklach, w miarę jak już reprezentowane przestają konsumować pojemność. Żadna ścieżka
  nie gubi wejścia po cichu.

**Zbiorczy incydent `WORKFLOW_B_UNRESOLVED_SIGNAL_TRUNCATED`.** Jeden na cykl, `severity=error`.
Trwała tożsamość jest liczona z **kompletnego** zbioru członkowskiego, niezależnie od tego, ile
pozycji incydent pokazuje: `fingerprint_fields` niesie
`one_shot_membership_digest` — strumieniowy SHA-256 po kanonicznie posortowanym, zdeduplikowanym
zbiorze `(raw_file_id, etap, reason_code)` — oraz dokładną liczność. Dwa różne zbiory to dwa
incydenty; ten sam zbiór w dowolnej kolejności i z powtórzeniami to jeden. Część czytelna dla
człowieka pozostaje ograniczona (`MAX_LISTED_UNREPORTED_ONE_SHOT`, dodatkowo obcinana do
`MAX_LIST_ITEMS` przez sanitizer `SuspectedBugEvent`) i jawnie oznaczona jako widok obcięty, a
`affected_record_count` zawsze podaje prawdziwą sumę. Dzięki temu ładunek incydentu jest stały
(~5,7 KB) niezależnie od tego, czy zbiór ma 51 czy 50 000 elementów. Incydent nie należy do cyklu
życia `WORKFLOW_B_INPUT_UNRESOLVED` i nie jest zamykany automatycznie — zamyka go operator, kiedy
obsłuży wymienione wejścia.

**Fail-closed — jedyny warunek sygnalizacji zmieniający werdykt cyklu.** Zapis zbiorczego incydentu
też może się nie powieść. Jeśli dla materialnego wejścia **jednorazowego** nie powstał ani jego
własny trwały incydent, ani zbiorczy, to warunek istnieje, a jego trwałej reprezentacji nie ma
nigdzie — i dowód tego stanu znika razem z cyklem. Obie przesłanki są ustalane **ponownym odczytem
`suspected_bug_incidents`** (indywidualna po fingerprincie `WORKFLOW_B_INPUT_UNRESOLVED`, zbiorcza
przez `truncation_incident_is_durable`), nigdy przez `report.error is None`. Wtedy i tylko wtedy
`signal.safety_contract_violated` jest prawdziwe, orchestrator ustawia
`WorkflowBBatchResult.unresolved_signal_safety_failure` i `_finish_or_raise` kończy cykl jako
`FAILED_NON_RETRYABLE` z przyczyną `one_shot_unresolved_input_without_durable_signal`. Run jest wtedy
`FAILED`, `ops/runner.py` podnosi istniejący incydent `JOB_TERMINAL_FAILURE`, działa `OnFailure` —
żadnego drugiego kanału alertowego nie dodano. Gałąź jest **poniżej** awarii etapów, żeby cykl, który
już padł z powodu etapu, zachował swoją własną przyczynę. Jeśli sama sygnalizacja w ogóle się nie
wykonała (np. nie udało się otworzyć połączenia), pytanie „czy ten cykl obserwował pracę
jednorazową" odpowiada czysta, bezbazowa `one_shot_unresolved_input_count`; brak takiej pracy
oznacza zachowanie dokładnie jak dotąd — awaria ścieżki alertowej nie psuje werdyktu.

Wszystko inne pozostaje kontenerowane: nieudany incydent odtwarzalny, pozycja do przeglądu domenowego
czy niedostępna ścieżka alertowa w cyklu bez wejść jednorazowych **nie** zmieniają wyniku cyklu.

Sweep nierozroutowanych plików (`reconcile_unrouted_stage2_files`) **stronicuje keysetem po całym
kwalifikującym się zbiorze**. Wcześniej czytał jedną stronę `UNROUTED_SWEEP_LIMIT = 200` wierszy
posortowaną `stage2_updated_at ASC` i na tym kończył, więc przy trwałym backlogu ≥ 200 wiersze 201+
**nigdy** nie były oglądane — ani w tym cyklu, ani w żadnym następnym — i nowo przybyłe wejście nie
dostawało ani review itemu, ani incydentu. Kursor idzie po `(COALESCE(stage2_updated_at,'epoch'),
id)`, dokładnie po tym samym wyrażeniu co `ORDER BY`, więc strony ani się nie nakładają, ani nie
gubią wierszy; `COALESCE` jest konieczne, bo `stage2_updated_at` bywa NULL, a kursora nie da się
porównać z kluczem, który czasem jest NULL. `UNROUTED_SWEEP_LIMIT` ogranicza teraz **jedno
zapytanie i jedną stronę pamięci**, a `UNROUTED_SWEEP_MAX_PAGES = 50` jest zaporą zasobową dwa rzędy
wielkości powyżej realnego zbioru. Jej osiągnięcie oznacza nieobejrzane pliki, więc jest logowane
jako WARNING z rozmiarem strony, limitem stron i liczbą obejrzanych wierszy; normalne wyczerpanie
zbioru (także pustego) jest ciche. **Ten WARNING nie jest dowodem późniejszego odnalezienia**: sweep
startuje w każdym cyklu od najstarszego końca, więc trwały prefiks 10 000 wierszy zasłania wszystko
za sobą w każdym cyklu tak samo. Konsekwencja jest ujęta w klasyfikacji odtwarzalności wyżej —
**żadna** pozycja Stage 2 nie jest traktowana jako odtwarzalna, także ta, do której sweep w danym
cyklu dotarł: bycie zmiecionym nie jest gwarancją, bo wspierane ścieżki mutacji potrafią wiersz z
tego prefiksu wyprowadzić. Pozostała znana granica: plik, który ten cykl
przetworzył do kształtu `stage2_status='OK'` bez `client_code`, jest reklasyfikowany dopiero przez
sweep, więc przy backlogu na poziomie zapory nie zostanie w tym cyklu zebrany w ogóle — jest trwałym
wierszem i wraca do widoczności, gdy backlog spadnie poniżej zapory (który to backlog sam jest
raportowany incydentami per wejście). Funkcja zwraca `Stage2UnroutedSweep`
(`rows_inspected`, `pages_read`, `truncated`), bo sama liczba wierszy nie odróżnia już pokrycia od
obcięcia.

Granularność, nie surowość: pojedyncze zablokowane wejście nadal kończy cykl jako
`SUCCEEDED_WITH_REVIEW_ITEMS`, żeby nie zatrzymywać przetwarzania niezależnych, poprawnych wejść.
Awaria samej ścieżki sygnalizacyjnej jest logowana i połykana — raportowanie, że wejście utknęło,
nigdy nie może stać się powodem czerwonego runu. Sygnalizacja działa na **własnym połączeniu**:
`_finish_cycle` otwiera je i zamyka w `finally`, więc awaria na poziomie połączenia nie może zatruć
sesji trzymającej sesyjny advisory lock `workflow_b.orchestrator.v1`. Sprzątanie locka przechodzi
przez `_relinquish_lock_session` (używane też przez `run_alpha_source_refresh_batch`): wyjątek
podniesiony w `finally` **zastąpiłby** zwracany wynik albo oryginalny wyjątek, więc nieudany
`pg_advisory_unlock` jest logowany, połączenie i tak jest zamykane, a nieudane zamknięcie jest
połykane. Ponieważ lock jest sesyjny — `commit`/`rollback` na zdrowym połączeniu go nie zwalnia —
to zamknięcie sesji jest właściwym mechanizmem zwolnienia; jawny unlock jest tanim, czytelnym
zwolnieniem na zdrowej ścieżce, nie gwarancją.

**Odpowiedzialność techniczna / watchdog.** Wykonanie cyklu jest własnością
`ops/execution_watchdog.py`, subject `systemd:workflow_b_orchestrator` (`06:00`/`20:00`
Europe/Warsaw, `completion_grace_minutes=180`, `stale_grace_minutes=240`), który podnosi
`SCHEDULED_RUN_MISSING` / `SCHEDULED_RUN_STALE`. Run zakończony `FAILED` jest klasyfikowany
`EXPECTED_FAILED` i nigdy nie zalicza fire; własny incydent niesie ścieżka
`JOB_TERMINAL_FAILURE`, a śmierć przed rejestracją runu — `SYSTEMD_UNIT_FAILURE` przez
`OnFailure=`. Utrata locka przez cykl `scheduled` jest `BLOCKED_CONCURRENT_EXECUTION` i **FAILED**
(P0-D).

**Polling i odzyskiwanie skrzynki.** Horyzont wyszukiwania IMAP jest **ograniczony i wynosi
`SINCE now() - 30 dni`** (`DEFAULT_SINCE_DAYS`, nadpisywalny per-run `since_days`); nie ma
nieograniczonego crawlera historii. Deduplikacja jest po `(account, mailbox, uidvalidity, uid)`
w `ingest.imap_message`, a wiersz wiadomości powstaje w **transakcji per wiadomość**: nieudane
przetworzenie robi rollback, więc UID nie zostaje oznaczony jako widziany i jest ponawiany w
kolejnym cyklu. Zmiana `UIDVALIDITY` unieważnia klucz dedupu i powoduje ponowne pobranie wiadomości
z horyzontu — chroni przed tym dedup treści po `sha256`. Horyzont jest bezpieczny **przy przyjętym
modelu pracy**: cykl co ~10-14 h, a niewykonany cykl jest wykrywany przez watchdoga w ciągu godzin,
więc 30 dni to rzędy wielkości zapasu. Wejście, które przyszło w oknie przestoju **dłuższego niż 30
dni**, wypadłoby poza horyzont — to jedyny znany warunek utraty i jest tu udokumentowany świadomie.

Publiczne `run(client, run_id, params)` deleguje do `run_workflow_b_batch(...)` i wykonuje w jednym parent runie: Stage 1 → Stage 2 → Stage 3 → postprocessory dopuszczone przez statyczny rejestr `workflow_b/postprocessor_registry.py`. Report 207 nadal jest wybierany przez selector control-plane; ALPHA00001 `Alpha_GPS_Baza_LOG` ma osobny dependency-coupled wpis rejestru. Rejestr deklaruje enum-like execution capabilities: Report 207 wspiera tylko `execute`, a ALPHA enrichment `execute` i `dry_run`; nieznany lub niewspierany mode jest odrzucany. Nie ma dynamicznego importu z wartości DB ani wykonania dowolnej nazwy modułu. Nie tworzy child runs ani nie wywołuje `ops/runner.py`. Session advisory lock `workflow_b.orchestrator.v1` obejmuje całość bez długiej transakcji; lock loser nie wywołuje żadnego etapu.

Semantyka przegranej locka zależy od `mode`, bo to ona decyduje, czy cokolwiek jest nie tak (P0-D). `manual_diagnostic` zwraca typed `SKIPPED_LOCKED`, run kończy się SUCCESS i nie powstaje incydent — ad-hoc uruchomienie ustępujące scheduled ownerowi to zamierzony model własności. `scheduled` ustawia `BLOCKED_CONCURRENT_EXECUTION` i podnosi `WorkflowBOrchestrationError`, więc `run_context` zapisuje run jako **FAILED**: wymagany cykl się nie odbył, a Workflow B startuje tylko o 06:00 i 20:00, więc nic go nie powtórzy przez kolejne 10-14 h. Watchdog czyta ten trwały fakt — FAILED jest klasyfikowany `EXPECTED_FAILED` i nigdy nie zalicza scheduled fire, podczas gdy wcześniejsze SUCCESS zaliczało cykl, który nie wszedł do żadnego etapu.

`WorkflowBBatchResult.cycle_executed` (pochodna `lock_acquired`, nie osobne pole) odróżnia „cykl się wykonał i nie było pracy" — `SUCCEEDED_NO_WORK`, nadal zalicza harmonogram — od „cykl się nie wykonał". Incydent niesie `cycle_executed=false`, `durable_writes_committed=false` i namespace spornego locka, co wprost mówi, że ręczny restart jest bezpieczny.

Typed partial error Stage 1 nie blokuje drenażu persisted Stage 2/3 backlogu, a typed partial error Stage 2 nie blokuje Stage 3. Typed partial Stage 3 dostarcza wyłącznie wiarygodne production `successful_load_identities` dla postprocessingu. Unexpected error zatrzymuje następne etapy; unexpected Stage 3 nie uruchamia postprocessora. Po bezpiecznych krokach techniczne błędy powodują `WorkflowBOrchestrationError(result)`; review-only outcomes dają `SUCCEEDED_WITH_REVIEW_ITEMS`, a zdrowy przebieg bez użytecznej pracy `SUCCEEDED_NO_WORK`.

Domyślny profil `mode=scheduled` używa persisted eligibility i produkcyjnego Stage 3 (`dry_run=false`, `force_reprocess=false`, bez path/raw-file filters i bez selector overrides). Orchestrator odrzuca force, Stage 2 path diagnostics, Stage 3 dry-run, scheduled raw-file filters oraz override postprocessorów. Tryb `manual_diagnostic` może zawężać persisted identities, ale nadal nie rozszerza force ani dry-run.

Workflow A nadal używa wyłącznie non-null `workflow_a_control.client_account.trip_metrics_population_source`. Workflow B parent rozwiązuje selector osobno dla dokładnej policy `(client_code, report_type)`: najpierw nullable `trip_metrics_population_source_override`, potem client default. Wynik zachowuje origin `report_policy_override` albo `client_default`. Migracja 050 dopuszcza `api_migration|report_207_migration|d105_2_ecodriving_migration|disabled`; `NULL` oznacza dziedziczenie, bez backfillu.

Macierz parenta: statyczny rejestr wspiera `report_207_speeding_migration` dla `report_207` oraz `alpha00001_dysponent_id_enrichment` wyłącznie dla `(ALPHA00001, Alpha_GPS_Baza_LOG, telematics_reports.Alpha_GPS_Baza_LOG)`. Scheduled parent zawsze tworzy dependency plan w `execute`; parametry schedule/orchestratora nie mogą przełączyć go na `dry_run`. ALPHA plan powstaje dopiero z committed production `successful_load_identity`, obejmuje zakres od poprzedniej różnej Warsaw-local daty udanego loadu do daty bieżącego loadu (end exclusive) i niesie `raw_file_id` oraz cleaned artifact ID. `report_207_migration` jest wykonywalny wyłącznie dla `report_207` i pozostaje bounded przez dokładny `raw_file_id`; `d105_2_ecodriving_migration` jest semantycznie rozpoznany wyłącznie dla `report_d105_2_ecodriving`, ale fail-closed jako parent-unsupported, bo bounded adapter nie istnieje. `disabled` jest wspieranym wynikiem bez selector-driven planu i nie zmienia Stage 3; nie wyłącza jednak statycznego, dependency-coupled ALPHA enrichmentu po replace-all `Alpha_GPS_Baza_LOG`. `api_migration` zachowuje dotychczasową semantykę: Workflow A jest źródłem metryk, a Workflow B nie tworzy planu i nigdy nie wywołuje Workflow A. Inne niezgodne, nieznane, malformed albo brakujące policy tworzą typed fail-closed outcome wymagający działania operatora. Routing nie używa nazw plików. Dry-run Stage 3 nigdy nie jest kandydatem.

Wszystkie 93 historyczne completed Stage 2 rows mają już zrekoncyliowane bezpośrednie linki cleaned-artifact; narzędzie recovery pozostaje wyłącznie ścieżką DR/audytu.

Stage 3 importuje dane biznesowe zawsze jako `workflow_a_control.client_account.client_db_user` z hasłem rozwiązanym przez `client_db_password_secret_ref`. Nie używa `POSTGRES_USER` do importu ani do runtime bootstrapu. Schemat przygotowuje wcześniej adminowa ścieżka `scripts/apply_client_business_migrations.py`; kontrolowany helper grantów może utworzyć rolę `workflow_b_stage3_loader`, nadać membership i `CONNECT`, ale nie nadaje runtime roli `CREATE`. DML na przygotowanych tabelach `telematics_reports` jest jawnie ograniczony do `SELECT, INSERT, UPDATE` oraz `DELETE` wymaganym przez istniejącą politykę replace-all; postprocessor Report 207 wymaga `SELECT, UPDATE` na `public.client_trips`. Sekwencje nie są używane.

Polityka overwrite jest per klient i typ raportu:

```sql
INSERT INTO workflow_b_control.report_type_client_load_policy
    (client_code, report_type, data_overwrite)
VALUES
    ('DELTA00001', 'report_207', true)
ON CONFLICT (client_code, report_type)
DO UPDATE SET data_overwrite = EXCLUDED.data_overwrite;
```

Brak wiersza policy oznacza `data_overwrite=false`; Stage 3 nie tworzy policy automatycznie. Statyczny registry nadal osobno waliduje exact client/report/destination dependency.

**Pre-write policy gate (P0-G).** Selector-driven postprocessor discovery był fail-closed dopiero *po* udanym production loadzie — czyli po commicie danych klienta. Brakujący wiersz policy dawał zmienione dane biznesowe **i** czerwony run. `_require_downstream_policy_configured()` wykonuje teraz tę samą rezolucję zanim cokolwiek zostanie oznaczone, pobrane lub zapisane. Importuje `load_report_policy_selector_state` i `resolve_workflow_b_trip_metrics_population_source` z `jobs.reports.workflow_b.trip_metrics_selector`, czyli z modułu orchestratora, więc bramka przed zapisem i wymóg po loadzie nie mogą się rozjechać. Jej wejścia są w pełni statyczne (`client_code`, `report_type`), dlatego można je sprawdzić jako pierwsze.

Blokuje oba kształty odmowy: wyjątek resolvera (`missing_report_policy`, malformed, unknown) oraz rezolucję z ustawionym `error_category` (selector niekompatybilny z typem raportu). Rezolucja bez `error_category` — w tym `disabled` i `api_migration`, które nie tworzą planu — przechodzi bez zmian.

Dwa umiejscowienia są nośne: **przed** `_mark_stage3_started` (to wywołanie stempluje `'RUNNING'` i czyści `stage3_finished_at`, a plik zablokowany na konfiguracji nigdy nie wszedł do Stage 3) i **poza** blokiem `try` (jego handler woła `_mark_stage3_error`, który stempluje `'ERROR'`, a `'ERROR'` nie jest stanem kwalifikującym do batcha — blokada konfiguracyjna trwale usunęłaby plik z discovery). Plik zostaje więc ze `stage3_status = NULL` i jest ponawiany automatycznie po skonfigurowaniu policy, bez `force_reprocess`.

Zablokowany element to `BLOCKED_OPERATOR_ACTION`: liczony jako non-retryable, więc cykl nadal nie może zaraportować sukcesu, ale żadne dane klienta nie zostały zapisane i nie powstaje `successful_load_identity`. Sam postprocessor **nie** przenosi się przed commit loadu — ma własną transakcję i efekty zewnętrzne; przeniesione zostało wyłącznie statyczne pytanie konfiguracyjne.

Po wdrożeniu migracji 050 operator może addytywnie ustawić `trip_metrics_population_source_override`; pominięcie kolumny albo jawne `NULL` zachowuje dziedziczenie. Blank, whitespace i wartości poza vocabulary są odrzucane. Nie ma publicznego API do edycji tych policy. Wymagana późniejsza, osobno zatwierdzona korekta konfiguracji to: client default `report_207_migration`, override `report_207=report_207_migration` oraz override `Alpha_GPS_Baza_LOG=disabled`; migracja 050 nie wykonuje tych zmian.

Zachowanie ładowania:

- jeżeli cleaned report ma użyteczny `record_id` (kolumna istnieje i co najmniej jeden wiersz ma niepustą wartość po trimowaniu), Stage 3 wymaga przygotowanego unikalnego indeksu na niepustym `record_id`; indeks tworzy migracja, nie job;
- przy `data_overwrite=false` nowe `record_id` są insertowane, istniejące są pomijane, a istniejące wiersze nie są aktualizowane;
- przy `data_overwrite=true` Stage 3 wykonuje upsert po `record_id`: istniejące wiersze są nadpisywane wartościami z raportu, nowe są dopisywane;
- wiersze z pustym `record_id` w raporcie, który ogólnie używa `record_id`, są pomijane i mogą trafić do artefaktu `rejected_rows`;
- jeżeli `record_id` jest brakujący lub całkowicie pusty i `data_overwrite=false`, plik dostaje `stage3_status='SKIPPED_NO_RECORD_ID'` i żadne dane nie są ładowane;
- jeżeli `record_id` jest brakujący lub całkowicie pusty i `data_overwrite=true`, Stage 3 transakcyjnie czyści tabelę docelową dla danego klienta/typu raportu i wstawia cały cleaned report.

Specjalny target `Alpha_GPS_Baza_LOG` nie używa generycznego schematu `telematics_reports`. Stage 3 ładuje go do bazy klienta `alpha_main`, schemat `telematics_reports`, tabela `telematics_reports."Alpha_GPS_Baza_LOG"` utworzona przez `db/client_business/023_alpha_gps_baza_log_workflow_b.sql`. Load jest zawsze `replace_all`: w jednej transakcji docelowej waliduje cleaned CSV, wykonuje `DELETE FROM telematics_reports."Alpha_GPS_Baza_LOG"` i inserty z mapowaniem `ID -> source_id`, `Nr rejestracyjny -> registration`, `Data przydziału -> assignment_date`, `Nazwa Pliku csv -> csv_filename`, plus audit IDs artefaktów Stage 1/2, `workflow_run_id`, `raw_file_id`, `source_sha256`, `source_row_number`, `raw_row_json`.

Każdy `raw_file` jest ładowany atomowo w docelowej bazie klienta. Po sukcesie transakcji docelowej Stage 3 aktualizuje status w bazie platformowej. Przy błędzie transakcja docelowa jest rollbackowana, a `ingest.raw_file.stage3_status` dostaje `ERROR` z komunikatem w `stage3_error`.

Gdy Stage 3 nie znajdzie cleaned artifactu, komunikat błędu zawiera `raw_file_id`, `report_type`, dokładne filtry lookupu (`workflow_name`, `stage_name`, `artifact_role`, kandydatów `report_type`) oraz podsumowanie wszystkich artifact rows dla tego `raw_file_id` pogrupowane po `stage_name` / `artifact_role` / `kind` / `report_type` / `client_code`. Do diagnostyki bieżącego pliku bez zgadywania UUID użyj:

```bash
PYTHONPATH="$PWD" python3 ops/diagnose_workflow_b_file_lineage.py \
  --filename GPS_baza_START_skrypt.xlsm \
  --report-type Alpha_GPS_Baza_LOG \
  --client-code ALPHA00001
```

Przykładowy `storage_key` artefaktu wyniku zawiera nazwę pliku unikalną dla źródła (`raw_file_id` i `source_artifact_id` w formie skróconej), żeby wiele plików tego samego `report_type` przetwarzanych w jednym runie nie nadpisywało tego samego obiektu MinIO:

```text
workflow_b/stage_3_load/yyyy=2026/mm=05/dd=14/run_id=<uuid>/report_type=report_207/load_result/stage3_load_result__<raw8>__<source_artifact8>.json
```

#### Legacy artifact metadata backfill

`ops/backfill_artifact_metadata.py` uzupełnia wyłącznie metadata w tabeli `artifacts` dla starszych wierszy, gdy źródło jest wysokiej pewności. Skrypt domyślnie działa jako dry-run; zapis wymaga `--apply`. Nie przenosi, nie kopiuje i nie usuwa obiektów MinIO, a dla starych fizycznych kluczy zostawia `layout_version=1`.

Przykłady:

```bash
PYTHONPATH="$PWD" python3 ops/backfill_artifact_metadata.py
PYTHONPATH="$PWD" python3 ops/backfill_artifact_metadata.py --apply --only-layout-version 1 --min-confidence high
```

Backfill może uzupełnić m.in. `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `raw_file_id`, `file_ext`, `display_filename`, `original_filename` oraz marker `metadata_json.backfilled`. Nie zgaduje `client_code` z nazw plików i pomija wiersze, dla których źródła są niejednoznaczne.

#### Workflow B report type registry

Platformowa migracja `019_workflow_b_report_type_registry.sql` dodaje schemat `workflow_b_control` oraz tabelę `workflow_b_control.report_type_registry`. To DB-backed read model / control-plane inspection table opisująca aktualnie zarejestrowane typy raportów Stage 2, ich klasy cleanerów, reguły detekcji oraz wymagane/opcjonalne kolumny z kontraktu walidacji.

Ważne: ta tabela nadal **nie steruje detekcją ani wyborem cleanera**. `jobs.reports.stage2.job_stage2` nadal używa Pythonowego `jobs.reports.stage2.registry.REGISTERED_REPORTS`, `detector.py` i klas w `jobs/reports/stage2/types/` jako źródła prawdy wykonania. Od migracji `028_*` Stage 2 czyta z niej tylko operatorską konfigurację finalizacji: `id_sync_column_name` oraz `record_id_ingredients`.

Migracja `020_workflow_b_report_registry_detection_contract.sql` dodaje Phase 1 kontraktu pod przyszłą DB-driven detekcję, ale nadal nie przełącza wykonania Stage 2 na DB. Nowe pola:

- `detection_rules_schema_version` — obecnie `1`; wersja maszynowo czytelnego kontraktu `detection_rules`
- `column_types` — JSON object z typami kolumn wyjściowych wykrytymi w Pythonowych `COLUMN_TYPES`; wartości normalizowane do `string|date|datetime|integer|numeric|boolean`
- `multi_table` — czy typ raportu oczekuje wejścia z wielu tabel po `split_into_tables`
- `cleaner_entrypoint` — przyszły dynamiczny punkt wejścia cleanera w formacie `module:Class.method`; `cleaner_module` i `cleaner_function` pozostają dla kompatybilności i inspekcji
- `id_sync_column_name` — dokładna nazwa kolumny w finalnym cleaned report używana do wykrycia klienta przez porównanie z `client_trips.registration`, `client_trips.chassis_number`, `client_trips.driver_name`
- `record_id_ingredients` — przecinkowa lista kolumn finalnego cleaned report, w kolejności, z których Stage 2 generuje deterministyczny `record_id`

Kontrakt `detection_rules` w wersji 1 używa obiektu JSON z polami m.in. `required_anywhere_strings`, `required_header_labels`, `optional_header_labels`, `forbidden_anywhere_strings`, `filename_hints`, `score_weights` i `term_groups`. Wartości są seedowane wyłącznie z obecnych Pythonowych klas raportów. Jeżeli obecny detektor nie używa danej kategorii, pole pozostaje puste; np. `filename_hints` jest puste, bo Pythonowy detektor Stage 2 nie czyta nazw plików.

Początkowo seedowane typy raportów:

- `d104_1`, `n104_1`, `report_112`, `report_207`, `report_602`, `report_602_ev`, `d104_7`, `d105_2` — `implementation_status='implemented'`
- `report_d105_2_ecodriving` — `implementation_status='implemented'`; migracja `041_workflow_b_d105_2_ecodriving_registry.sql` dodaje specjalny wariant D105.2 z metrykami EcoDriving/event, wykrywany strukturalnie po kolumnach nagłówka, bez filename/title hints
- `eco_driving_driver`, `eco_driving_vehicle` — `implementation_status='todo'`, ponieważ ich `clean()` rzuca `NotImplementedError`; Stage 2 oznacza takie pliki jako `PENDING_REVIEW` z `cleaning_not_implemented`

`report_207` obsługuje raport „207 Raport przekroczeń limitów prędkości drogowej”. Detekcja i cleaning są zaimplementowane w Pythonie (`jobs.reports.stage2.types.report_207.Report207`) i rejestrowane w `jobs.reports.stage2.registry.REGISTERED_REPORTS`; odpowiadający wiersz DB jest tylko control-plane/read-model. Output Stage 2 dla tego typu ma kolumny: `Data i czas`, `Nr rejestracyjny`, `Prędkość`, `Ograniczenie prędkości drogowej`, `Lokalizacja` oraz końcowe `record_id` dodane przez finalizację Stage 2. Dla `report_207` `record_id` jest liczony przez specjalną logikę `report_207_business_key_v2`: SHA-256 z `report_type`, pełnego kanonicznego klucza biznesowego (`Data i czas`, `Nr rejestracyjny`, `Prędkość`, `Ograniczenie prędkości drogowej`, `Lokalizacja`) oraz `duplicate_ordinal` dla powtórzeń identycznego klucza w kolejności cleaned report. Stage 3 może ładować ten raport do `telematics_reports.report_207`, gdy Stage 2 ustali `client_code`.

`report_d105_2_ecodriving` obsługuje jeden specjalny wariant D105.2 z metrykami EcoDriving/event. Detekcja jest wyłącznie strukturalna: jeden arkusz/tabela z wierszem nagłówka zawierającym wymagane kolumny `Nr Rejestracyjny`, `Czas rozpoczęcia`, `Czas zakończenia`, `przekroczenia obr/min`, `> 140kmh`, `> 160kmh`, `> 170kmh`; FleetWebowe arkusze z rozdzielonymi kolumnami `Data rozpoczęcia`, `Data ukończenia`, `Czas rozpoczęcia`, `Czas zakończenia` są także akceptowane. Detektor nie czyta nazwy pliku ani tytułu raportu; typ jest zarejestrowany przed generycznym `d105_2`, żeby wariant z kolumnami metrycznymi nie został sklasyfikowany jako zwykły D105.2. Cleaner zachowuje wymagane kolumny pod kanonicznymi nazwami, trimuje rejestrację, normalizuje parsowalne daty/czasy do `YYYY-MM-DD HH:MM:SS`, a dla rozdzielonych timestampów składa `Data rozpoczęcia + Czas rozpoczęcia` do kanonicznego `Czas rozpoczęcia` oraz `Data ukończenia + Czas zakończenia` do kanonicznego `Czas zakończenia`. Cleaner normalizuje całkowite nieujemne metryki do tekstowych liczb całkowitych i zachowuje dodatkowe kolumny nagłówka, które Stage 3 może załadować dynamicznie. Puste komórki metryk nie są traktowane jako zero na etapie migracji, tylko jako `INVALID_METRIC_COUNTS`. Finalizacja Stage 2 ma registry config: `id_sync_column_name='Nr Rejestracyjny'` oraz deterministyczny `record_id` z wymaganych kolumn: `Nr Rejestracyjny,Czas rozpoczęcia,Czas zakończenia,przekroczenia obr/min,> 140kmh,> 160kmh,> 170kmh`. Stage 3 target table: `telematics_reports.report_d105_2_ecodriving`.

`Alpha_GPS_Baza_LOG` obsługuje ALPHA00001 / Alpha GPS XLSM `LOG`. Detekcja wymaga w jednym wierszu kolumn: `ID`, `Nr rejestracyjny`, `Data przydziału`, `RFID`, `PRYW`, `EDYS`, `OPTIMA`, `OTK`. Cleaner nie używa pierwszego wykrytego nagłówka jako outputu; szuka wiersza `ID`, `Nr rejestracyjny`, `Data przydziału`, `Nazwa Pliku csv`, bierze tylko wiersze poniżej, zatrzymuje się przed wierszem `ID`, `Data przydziału`, `PRYW stary`, `PRYW aktualny`, usuwa puste wiersze, trimuje wartości i zwraca wyłącznie te cztery kolumny. `Data przydziału` jest parsowana fail-fast jako data (`dd.mm.yyyy`, `yyyy-mm-dd`, warianty z `/`/`-`, albo Excel serial). Jeżeli Stage 2 nie rozwiąże klienta przez ogólną konfigurację, typ ma domyślny `client_code=ALPHA00001`.

## `suspected_bug` — platform-wide defect and invariant alerting

`suspected_bug` is a reusable **error classification**, not a job and not a run status. Any component can report an anomaly that likely indicates a software defect, a data-integrity conflict, a violated invariant or an impossible state. Implementation: `api/suspected_bug.py` (contract, sanitization, fingerprint, atomic store, email rendering), migration `052_suspected_bug_incidents_and_email_outbox.sql`, worker `ops/suspected_bug_email_worker.py`, inspection `ops/inspect_suspected_bugs.py`.

**Log contract.** Level is always `ERROR`; `logs.context.classification = "suspected_bug"`, plus `incident_code`, `incident_id`, `fingerprint`, `occurrence_no` and the sanitized provenance. The detecting run keeps its own status (`SUCCESS`, `FAILED`, partial, blocked) according to its existing business contract — reporting never changes it.

**Reporting is atomic.** One transaction writes the ERROR log, upserts `suspected_bug_incidents` (unique per fingerprint), appends `suspected_bug_occurrences` (linked to the log row) and, when the alert policy allows, inserts one `suspected_bug_email_outbox` row. Nothing is sent inside the business transaction. Two transports, one implementation: `POST /suspected-bugs` for HTTP-only jobs (`LogPlatformClient.report_suspected_bug`) and a direct platform-DB call for host jobs and operator scripts.

**Grouping.** The fingerprint is a SHA-256 over canonical JSON of environment, component, incident code, client, report type, dataset, database/schema/table, subject type/key/normalized subject value and `fingerprint_fields` (the invariant identity, e.g. the sorted conflicting values). It deliberately excludes run id, raw file id, artifact ids, timestamps, affected record ids and occurrence counts — those change per occurrence and live in the occurrence payload and evidence. Reordering the conflicting values does not change the fingerprint; changing them does.

**Email deduplication.** The first occurrence of a new fingerprint alerts immediately. Repeats within `SUSPECTED_BUG_ALERT_COOLDOWN_MINUTES` (default 120) are suppressed while the occurrence count and the ERROR logs keep accumulating. A material change re-alerts: reopened state, or affected-record scope growing by the configured factor (default: doubling). A different conflict set is a different fingerprint, therefore a new incident. A still-recurring incident alerts again after `SUSPECTED_BUG_ALERT_REMINDER_HOURS` (default 24; `0` disables). Idempotent enqueue is enforced by a unique `notification_key`, so concurrent detections produce one incident and at most one immediate email.

**No recipient fallback.** With `SUSPECTED_BUG_ALERT_TO` unset, logging and incident persistence continue and the occurrence records `email_decision='suppressed'`, reason `recipients_not_configured`. The mechanism never falls back to report, customer or Eco Driving recipients.

**Failure containment.** `safe_report_suspected_bug` never raises, never replaces the original business exception, and refuses to recurse: a reporting failure prints a conventional operational error to stderr. Email delivery errors are ordinary operational errors too — they never create another suspected_bug incident.

**Delivery worker.** `python -m ops.suspected_bug_email_worker --once` (or `--loop`) claims due rows with `FOR UPDATE SKIP LOCKED`, marks them `sending` with a claim token and lease before any SMTP call, sends through the shared `jobs.common.emailer.send_html_email` transport, then records `sent` with the provider `Message-ID`. Transient failures go to `retry` with bounded exponential backoff; permanent SMTP rejections and exhausted attempts go to `dead_letter`; expired leases return to the queue. `--show-due` is read-only. Proposed host units: `ops/systemd/proposed/suspected-bug-email-worker.{service,timer}` (not enabled by the repo).

**Sanitization.** Payloads are allow-listed and bounded before storage: secret-shaped keys and inline `password=…` style values are redacted, email addresses are masked, strings/lists/nesting are capped, stack traces are trimmed to the last lines, and any truncation is marked `truncated=true`. See `docs/06_security.md`.

### `jobs.reports.postprocess.job_alpha00001_dysponent_id_enrichment` — Workflow B, ALPHA00001 Dysponent_ID enrichment

Ten ALPHA-only postprocessor jest automatycznie dołączony do udanego, committed Stage 3 loadu `Alpha_GPS_Baza_LOG`. Stage 3 replace-all jest commitowany przed wywołaniem enrichmentu; enrichment używa osobnej transakcji i nie może cofnąć source loadu. Przed każdym batchem ponownie sprawdza, że source table zawiera dokładnie jeden oczekiwany `raw_file_id`. Inny klient, report type, destination albo nieznana nazwa postprocessora są blokowane przez statyczny rejestr.

Dedykowana operacja manualna `ops/refresh_alpha00001_source_for_backfill.py` jest jedynym kontraktem, który żąda ALPHA postprocessora w `dry_run`. Domyślne wywołanie tylko planuje, nie łączy się z IMAP i nie wykonuje DML. `--execute-source-refresh` wymaga dokładnego attestation, host/user/repo/main, clean worktree z `HEAD == origin/main`, dokładnych markerów `logdb`/`alpha_main`, braku aktywnej migracji i globalnego locka Workflow B. Stage 1 może odkryć współdzielony mailbox, ale przed Stage 2 raw IDs są filtrowane statycznym ALPHA `report_key=gps_baza_start_skrypt`; po detekcji Stage 3 dopuszcza wyłącznie dokładne `(ALPHA00001, Alpha_GPS_Baza_LOG, telematics_reports.Alpha_GPS_Baza_LOG)`. Source replace-all commit kończy się przed dry-run enrichmentu. Failure dry-runu nie cofa source; wynik rozróżnia `NO_NEWER_SOURCE_AVAILABLE`, `ALREADY_LOADED`, source-loaded/dry-run-passed i source-loaded/dry-run-failed. Rerun po wcześniejszym failed runie może ponowić tylko dry-run dla już committed source. Każdy wynik z `target_rows_modified != 0` jest hard failure. Normalny `execute` postprocessor pozostaje jawnie outstanding; operacja nie uruchamia backfillu.

Kontrakt dopasowania: rejestracja jest trimowana, upper-case i pozbawiana whitespace; dla tripu wybierana jest największa `assignment_date` nie późniejsza niż Warsaw-local data `start_timestamp`. Zakres tripów ma start inclusive i end exclusive na północach `Europe/Warsaw`, więc działa poprawnie na DST. Jeden source row może pokrywać wiele późniejszych tripów. Jeżeli najnowsza data ma więcej niż jeden różny niepusty `source_id`, trip jest `AMBIGUOUS_ENRICHMENT_MATCH`, nie jest zgadywany ani aktualizowany. Pusty source ID nie nadpisuje celu.

Domyślnie job jest `dry_run=true`, wymaga `date_from` i ogranicza `date_to` do wcześniejszej z jawnego endu i dnia po najnowszym tripie. Świeżość źródła nie jest domyślnie egzekwowana: pełny żądany zakres jest wzbogacany ostatnim zatwierdzonym snapshotem przypisań, więc przypisania zmienione po tym snapshocie mogą zostać zapisane jako nieaktualne. Opcjonalne `require_fresh_source=true` przywraca fail-closed — dokłada dzień source-loadu do minimum okna i egzekwuje `max_source_age_hours`. Niezależnie od tego parametru okno wychodzące poza dzień source-loadu jest raportowane jako `enriched_beyond_source_boundary=true` i logowane na poziomie WARNING. Aktualizuje wyłącznie puste `Dysponent_ID`; istniejące identyczne wartości raportuje jako already-correct, a różne jako konflikt. Jawne `overwrite_existing=true` jest jedynym trybem korekty; legacy `force=true` jest odrzucane.

Readiness zwraca source/target/provenance, planowane update'y, no-match, ambiguity, konflikty, coverage po tripach i dystansie, active driver-chart match oraz unmatched IDs. `max_source_age_hours` to 36 h i obowiązuje wyłącznie przy `require_fresh_source=true`; minimalne predicted coverage to 95% zarówno po tripach, jak i dystansie; default `max_ambiguous_matches=25`. 95% wynika z historycznej zdrowej charakterystyki i pozostaje parametrem, nie hard-coded business invariant. Typed gates: `SOURCE_REPORT_NOT_READY`, `SOURCE_REPORT_EMPTY`, `UNSUPPORTED_CLIENT`, `INVALID_DATE_RANGE`, `AMBIGUOUS_ENRICHMENT_MATCH`, `COVERAGE_BELOW_THRESHOLD`, `ENVIRONMENT_IDENTITY_NOT_VERIFIED`. Dry-run zwraca `NOT_READY` bez DML; realny tryb kończy się błędem przed pierwszym UPDATE.

Każdy execute batch jest osobną transakcją, ponowne uruchomienie jest idempotentne, a błąd późniejszego batcha nie cofa wcześniejszych commitów. Environment identity platformy i bazy klienta jest zawsze atestowana. Istniejące DDL `023`, `024`, `025` oraz indeksy są wystarczające; ta automatyzacja nie wymaga nowej migracji ani schedule row.

Kontrolowany backfill jest opisany w `docs/07_operations.md` i realizowany przez `ops/backfill_alpha00001_dysponent_id.py`; default to read-only dry-run, zapis wymaga `--execute`. Eco Driving generation i email schedules pozostają disabled do osobnego readiness approval.

**Ambiguity alerting (`ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT`).** Każda grupa niejednoznaczności jest zgłaszana jako jeden `suspected_bug` — nie jeden na trip. Grupą jest znormalizowana rejestracja, której najnowsza obowiązująca `assignment_date` zawiera więcej niż jeden różny `source_id`; 37 dotkniętych tripów daje jeden incident. Fingerprint obejmuje incident code, klienta (`ALPHA00001`), report `Alpha_GPS_Baza_LOG`, source table `telematics_reports."Alpha_GPS_Baza_LOG"`, target table `public."client_trips"`, rejestrację, posortowane sporne ID i datę przypisania. Payload zawiera zakres czasu tripów, numery wierszy źródła, nazwy plików CSV, `raw_file_id`, Workflow B `workflow_run_id`, cleaned artifact id, czas załadowania źródła, zakres dat przetwarzania oraz `rows_modified=0`; identyfikatory tripów są tylko ograniczonym dowodem (maks. 20 próbek).

Wykrycie jest wyłącznie obserwacją: matching pozostaje fail-closed, żaden niejednoznaczny wiersz nie jest wybierany ani aktualizowany, próg readiness i `--max-ambiguities` (default `25`) nie zmieniają się. Raportowanie działa tak samo w `dry_run` i w `execute`, także w ścieżce `ops/refresh_alpha00001_source_for_backfill.py`; wspólny fingerprint i cooldown zapobiegają duplikatom maili między controlled source refresh, scheduled postprocessorem i standalone backfillem. `ops/backfill_alpha00001_dysponent_id.py --no-suspected-bug-report` (param joba `report_suspected_bugs=false`) wypisuje wykryte grupy bez trwałego incidentu i bez maila.

### `jobs.reports.postprocess.job_report_207_speeding_migration` — Workflow B, post-processing `report_207`

Ten job jest osobnym krokiem backupowego Workflow B po Stage 3. Skanuje włączone bazy klientów z `workflow_a_control.client_account`, ale migruje zdarzenia przekroczenia prędkości do liczników w `public.client_trips` tylko dla klientów z `trip_metrics_population_source='report_207_migration'`. Klienci z innym źródłem są pomijani przed połączeniem do bazy klienta oraz jakimkolwiek DML. Job nigdy nie wykonuje DDL ani grantów.

Parametry:

- `client_code` (opcjonalnie; przetwarza jednego klienta),
- `limit` (opcjonalnie; limit kandydackich wierszy `report_207` na klienta),
- `dry_run` (opcjonalnie, domyślnie `false`; nie wykonuje DDL/DML w bazach klientów),
- `force_retry_errors` (opcjonalnie, domyślnie `false`; ponownie analizuje wszystkie niemigrowane wiersze z `migrated_to_client_db_error`; bez tej flagi job automatycznie retriuje tylko `NO_MATCHING_TRIP`),
- `auto_grant_permissions` jest parametrem kompatybilności; `true` jest odrzucane jako operator-action-required i nie wykonuje grantów.

Dry-run raportuje także source-gate skipy przez `trip_metrics_population_source`, `required_trip_metrics_population_source` i `skip_reason=trip_metrics_population_source_mismatch`.

Dry-run:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.postprocess.job_report_207_speeding_migration '{"dry_run":true}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.postprocess.job_report_207_speeding_migration '{"client_code":"ALPHA00001","limit":100,"dry_run":true}'
```

Realny run:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.postprocess.job_report_207_speeding_migration '{}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.reports.postprocess.job_report_207_speeding_migration '{"client_code":"ALPHA00001"}'
```

Przed realnym runem migracja `db/client_business/042_workflow_b_stage3_runtime_schema.sql` musi zapewnić kolumny trackingowe w `telematics_reports.report_207`:

- `migrated_to_client_db BOOLEAN NOT NULL DEFAULT FALSE`
- `migrated_to_client_db_at TIMESTAMPTZ NULL`
- `migrated_to_client_trip_id TEXT NULL`
- `migrated_to_client_db_error TEXT NULL`

oraz liczniki w `public.client_trips`:

- `speeding_140_160_count INTEGER NOT NULL DEFAULT 0`
- `speeding_160_170_count INTEGER NOT NULL DEFAULT 0`
- `speeding_170_plus_count INTEGER NOT NULL DEFAULT 0`

Brak tabeli, wymaganej kolumny albo indeksu `report_207__record_id_uidx` zatrzymuje job przed DML przez typed schema-readiness error ze wskazaniem migracji 042. Job nie próbuje naprawić schematu i nie wymaga ownership tabel.

Matching jest dokładny i bez fuzzy logic: trimmed `report_207."Nr rejestracyjny"` musi równać się `client_trips.registration`, a sparsowany `report_207."Data i czas"` musi mieścić się inkluzywnie w `[client_trips.start_timestamp, client_trips.end_timestamp]`. `report_207."Data i czas"` może być tekstowym lokalnym timestampem FleetWeb albo numerem seryjnym daty/czasu Excela w zakresie 2000-01-01–2100-01-01; obie formy są interpretowane jako lokalny czas `Europe/Warsaw` przed porównaniem z `client_trips` (`timestamptz`). Finalny schemat `client_trips` ma klucz złożony `(client_id, provider_trip_id)`; job używa tych kolumn do aktualizacji liczników, a w `migrated_to_client_trip_id` zapisuje tekstowy `record_id` tripu, jeśli istnieje, w przeciwnym razie `provider_trip_id`.

Buckety dla `report_207."Prędkość"`:

- `> 140` i `<= 160` → `speeding_140_160_count`
- `> 160` i `<= 170` → `speeding_160_170_count`
- `> 170` → `speeding_170_plus_count`

Dolna granica jest **wyłączająca** (`> 140`): wiersze z poprawną rejestracją i sparsowaną prędkością `<= 140` (w tym dokładnie `= 140`) są **sub-threshold** — nie należą do żadnego bucketu, nie są migrowane i **z założenia nie dostają błędu**. Pozostają `migrated_to_client_db=false` z `migrated_to_client_db_error IS NULL` także po pełnym rerunie i są re-selekcjonowane jako kandydaci w każdym runie. To oczekiwane zachowanie, nie błąd migracji; aby odróżnić te wiersze od realnych niepowodzeń, podsumowanie (dry-run i realny run) raportuje `sub_threshold_rows`. Zmiana granicy na `>= 140` jest decyzją biznesową i nie jest wprowadzana bez niej.

Wiersze już oznaczone `migrated_to_client_db=TRUE` nie są nigdy liczone ponownie. Brak dopasowania ustawia `migrated_to_client_db_error='NO_MATCHING_TRIP'`, wieloznaczne dopasowanie ustawia `AMBIGUOUS_TRIP_MATCH`, a błędne dane wejściowe ustawiają `INVALID_SPEED`, `INVALID_TIMESTAMP` albo `INVALID_REGISTRATION`. Niemigrowane wiersze z `NO_MATCHING_TRIP` są retriowane automatycznie w normalnych runach, bo mogą stać się dopasowalne po późniejszym zasileniu `client_trips`. Pozostałe wiersze ze stored error są pomijane bez `force_retry_errors=true`.

Po wdrożeniu obsługi nowego formatu timestampu historyczne wiersze wcześniej oznaczone `INVALID_TIMESTAMP` wymagają jednorazowego dry-run i backfillu z `force_retry_errors=true`, np. najpierw `{"client_code":"<CLIENT_CODE>","dry_run":true,"force_retry_errors":true}`, a po weryfikacji liczników `{"client_code":"<CLIENT_CODE>","force_retry_errors":true}`.

Operator może sprawdzić rejestr i bieżący status przetwarzania Stage 2 przez:

```bash
PYTHONPATH="$PWD" python3 ops/workflow_b_report_status.py --limit 10
```

Skrypt jest tylko read-only raportem operatorskim; nie zmienia detekcji, cleaningu ani zapisów `stage2_*`.


### `ops/recover_report_207_speed_violations.py` — manual Report 207 speed recovery

Manual-only recovery tool for bounded cases where `report_207` was loaded before the Report 207 identity fix in commit `ae53716d89e2fdd9d57352a3c8217c30c5ab4ea6`. It is not a runner job and has no scheduler entry. Default mode is dry-run; writes require `--execute`, environment identity attestation, an explicit backup confirmation token, and an explicit acknowledgement that `client_trips` speeding counters are recalculated by client/date scope.

The tool supports explicit scope:

- `--client-code`, `--date-from`, `--date-to` are required;
- `--raw-file-id` and `--source-artifact-id` are optional repeatable filters for Report 207 cleanup/reload;
- `--cleaned-csv RAW_FILE_ID=/path/to/file.csv` can be used when the reviewed cleaned artifact is available locally;
- destination database, schema, and table are resolved through `workflow_a_control.client_account` and Stage 3 metadata; the Report 207 target remains `telematics_reports.report_207`.

Dry-run verifies the fixed commit is present, checks environment identity, checks that the client is gated with `trip_metrics_population_source='report_207_migration'`, validates required columns, reports current `report_207` and `client_trips` counts, and calculates corrected cleaned-row `record_id` counts from available artifacts. It performs no DDL/DML.

Write mode deletes only scoped rows from `telematics_reports.report_207`, regenerates Report 207 `record_id` values with Stage 2 `report_207_business_key_v2`, reloads corrected rows through the existing Stage 3 destination loader, clears only the three Report-207-derived speeding bucket columns in `public.client_trips` for the requested client/date range, and repopulates those counters by assigning aggregate counts from corrected `report_207` rows. It does not delete base trip rows. The counter repopulation sets final bucket values from aggregates rather than incrementing existing values, so rerunning the same recovery converges to the same `client_trips` counts.

Important limitation: `public.client_trips` stores aggregate speeding counters and does not store per-counter `_raw_file_id` or `_source_artifact_id` lineage. Therefore Report 207 table cleanup can be raw/source scoped, but `client_trips` counter cleanup is client/date scoped and requires the separate `CLIENT_TRIPS_DATE_RECALC:<client_code>:<date_from>:<date_to>` acknowledgement in execute mode.

### `jobs.reports.postprocess.job_d105_2_ecodriving_trip_metrics_migration` — Workflow B, post-processing D105.2 EcoDriving

Ten job jest osobnym krokiem backupowego Workflow B po Stage 3 dla tabeli `telematics_reports.report_d105_2_ecodriving`. Pisze do `public.client_trips` tylko dla klientów z `workflow_a_control.client_account.trip_metrics_population_source='d105_2_ecodriving_migration'`. Klienci z innym źródłem są pomijani przed auto-grantem, połączeniem do bazy klienta i jakimkolwiek DDL/DML; dry-run i realny run raportują `client_code`, wybrane źródło, wymagane źródło oraz `skip_reason=trip_metrics_population_source_mismatch`.

Parametry:

- `client_code` (opcjonalnie; przetwarza jednego klienta),
- `limit` (opcjonalnie; limit kandydackich wierszy raportu na klienta),
- `dry_run` (opcjonalnie, domyślnie `false`; nie wykonuje DDL/DML w bazach klientów),
- `force_retry_errors` (opcjonalnie, domyślnie `false`; ponownie analizuje wszystkie niemigrowane wiersze ze stored error; bez tej flagi retry obejmuje tylko `NO_MATCHING_TRIP`),
- `auto_grant_permissions` (opcjonalnie, domyślnie `false`; przy realnym runie uruchamia istniejący bootstrap uprawnień Stage 3 dopiero po weryfikacji tożsamości bazy klienta),
- `production_write_confirmation` (wymagany tylko dla produkcyjnego write-mode; dokładnie `production/<platform-uuid>/<client_code>/d105_2_trip_metrics_migration`).

Dry-run i write-mode wymagają fail-closed runtime/platform/client identity guard. Produkcyjny write-mode dodatkowo wymaga jawnego `client_code`; brak deklaracji, markera lub zgodności kończy run przed analizą/DDL/DML.

W realnym runie job może dodać kolumny trackingowe do `telematics_reports.report_d105_2_ecodriving`: `migrated_to_client_db`, `migrated_to_client_db_at`, `migrated_to_client_trip_id`, `migrated_to_client_db_error`. Może też zapewnić w `public.client_trips` kolumny `speeding_140_160_count`, `speeding_160_170_count`, `speeding_170_plus_count`, `overrev_events_count`. Nie dodaje i nie aktualizuje `high_rpm_events_count`, bo aktualny Workflow A API traktuje HIGH_RPM i OVERREV jako osobne provider-labeled event metrics; raportowe `przekroczenia obr/min` jest migrowane konserwatywnie tylko do `overrev_events_count`.

Mapping metryk jest bezpośredni, bez odejmowania bucketów:

- `> 140kmh` → `client_trips.speeding_140_160_count`,
- `> 160kmh` → `client_trips.speeding_160_170_count`,
- `> 170kmh` → `client_trips.speeding_170_plus_count`,
- `przekroczenia obr/min` → `client_trips.overrev_events_count`.

Matching jest deterministyczny i bez szerokiego fuzzy logic. Najpierw job próbuje dokładnego dopasowania:

```sql
trim(report."Nr Rejestracyjny") = trim(client_trips.registration)
AND parsed(report."Czas rozpoczęcia", Europe/Warsaw) = client_trips.start_timestamp
AND parsed(report."Czas zakończenia", Europe/Warsaw) = client_trips.end_timestamp
```

Jeżeli dokładnych kandydatów jest 0, job próbuje fallbacku do precyzji minuty:

```sql
trim(report."Nr Rejestracyjny") = trim(client_trips.registration)
AND date_trunc('minute', client_trips.start_timestamp) = date_trunc('minute', parsed(report."Czas rozpoczęcia", Europe/Warsaw))
AND date_trunc('minute', client_trips.end_timestamp) = date_trunc('minute', parsed(report."Czas zakończenia", Europe/Warsaw))
```

Jeżeli dokładne i minutowe dopasowanie mają po 0 kandydatów, job próbuje D105.2-specific bounded rounded-timestamp fallbacku dla raportowych timestampów zaokrąglonych do minuty:

```sql
trim(report."Nr Rejestracyjny") = trim(client_trips.registration)
AND abs(extract(epoch FROM (client_trips.start_timestamp - parsed(report."Czas rozpoczęcia", Europe/Warsaw)))) <= 60
AND abs(extract(epoch FROM (client_trips.end_timestamp - parsed(report."Czas zakończenia", Europe/Warsaw)))) <= 60
```

Dokładne dopasowanie ma pierwszeństwo przed minutowym fallbackiem, a minutowy fallback ma pierwszeństwo przed rounded fallbackiem. Jeśli dowolna strategia zwróci więcej niż jednego kandydata, row dostaje `AMBIGUOUS_TRIP_MATCH` i nie inkrementuje liczników. Jeśli żadna strategia nie znajdzie kandydata, row dostaje `NO_MATCHING_TRIP`. To nie jest nearest-trip matching: job nie sortuje kandydatów po odległości i nie wybiera najbliższego z wielu; bounded rounded fallback jest użyty tylko przy dokładnie jednym kandydacie. Nie ma dopasowania `+/-5min`, tylko po dacie ani po jednym endpointcie.

Daty/czasy raportu bez offsetu są interpretowane jako lokalny czas `Europe/Warsaw`; wartości z offsetem są parsowane jako `timestamptz`. Minutowy fallback obsługuje historyczne raporty z precyzją minutową przy `client_trips` przechowującym sekundy, a rounded fallback obsługuje raportowe wartości zaokrąglone do najbliższej minuty.

Dry-run raportuje: `candidate_rows`, `non_zero_metric_rows`, `zero_metric_rows`, `exact_match_rows`, `minute_fallback_match_rows`, `rounded_fallback_match_rows`, `matched_rows`, `unmatched_rows`, `ambiguous_rows`, `invalid_registration_rows`, `invalid_timestamp_rows`, `invalid_metric_rows`, `rows_incrementing_*`, sumy `incremented_*`, brakujące kolumny i source-gate skipy. Realny run inkrementuje liczniki tylko dla dokładnie jednego dopasowanego tripu, oznacza takie rows jako migrated, oznacza w pełni zerowe rows jako migrated bez aktualizacji tripu, zapisuje `NO_MATCHING_TRIP`, `AMBIGUOUS_TRIP_MATCH`, `INVALID_REGISTRATION`, `INVALID_TIMESTAMP` albo `INVALID_METRIC_COUNTS` dla pozostałych przypadków. Wiersze już `migrated_to_client_db=TRUE` nie są liczone ponownie; brak historycznego counter reconciliation.

Read-only walidator operatorski:

```bash
PYTHONPATH="$PWD" python3 ops/checks/check_d105_2_ecodriving_trip_metrics.py --json
PYTHONPATH="$PWD" python3 ops/checks/check_d105_2_ecodriving_trip_metrics.py --client-code ALPHA00001 --strict
```

Walidator sprawdza source selector, istnienie tabeli, wymagane kolumny, parseability/match categories przez ten sam SQL co dry-run oraz wskazuje, czy write-mode byłby blokowany przez błędy/niejednoznaczności. Jest read-only i nie tworzy schematów, kolumn ani tracking flags.

### Client Trips — global ingestion admission rule (2,000 km distance cap)

**Client Trips ingestion MUST discard provider trips whose provider-reported distance is greater than 2,000 km before `client_trips` persistence.**

This is an owner/business decision and the default invariant for **all** Client Trips ingestion jobs, current and future.

```text
trip_distance_meters <= 2_000_000  -> normal Client Trips persistence
trip_distance_meters >  2_000_000  -> discarded before persistence
```

Boundary semantics are intentional and asymmetric:

- `1,999,999 m` → accepted;
- `2,000,000 m` exactly (2,000 km) → **accepted**; the rule is `> 2,000 km`, never `>= 2,000 km`;
- `2,000,001 m` → rejected.

Contract details:

- the threshold is applied to the **provider-reported** distance (`/trips.trip_distance`), used exactly as received; no unit conversion, reconstruction or correction is performed;
- there is **intentionally no additional data-quality inference**. This is a single hard cap on one field, not an anomaly-detection system: no GPS/geodesic validation, no average-speed check, no odometer validation, no statistical outlier detection, no vehicle- or client-specific thresholds, no quality tiers, no confidence scoring and no generic anomaly flags. The platform deliberately does not attempt to decide whether a trip below the cap is correct;
- **missing / NULL distance semantics are unchanged.** A trip whose provider distance is absent, `NULL`, empty or not interpretable as a number carries no comparable distance, so the cap has nothing to compare: the trip follows the existing path and is persisted with `trip_distance_meters` `NULL`. The rule never rejects on absence and never substitutes a value;
- the rule applies to **every** Client Trips ingestion job — the scheduled/incremental/reconciliation `trips_sync` path (including dispatcher fires and `ops/recover_telematics_trips_window.py`, which executes that same job) and the manual `backfill_trips_insert_only` repair path;
- **historical rows are not affected.** This is an ingestion invariant only; it deletes and rewrites nothing. An existing over-cap row stays untouched until an ordinary provider re-ingestion would have re-upserted it, at which point the incoming provider record is discarded and the stored row is simply not refreshed.

Implementation — `jobs/api/telematics/client_trips_admission.py` is the single shared admission boundary. It owns `MAX_TRIP_DISTANCE_METERS = 2_000_000`, the coercion of the provider value, the stable rejection reason `distance_over_2000km`, the bounded per-run counter and — decisively — `execute_client_trips_insert(...)`, **the only sanctioned way this repository writes `client_trips`**. Enforcement is therefore in two places with two different jobs:

1. an early parse gate in each ingestion job, immediately after the provider `trip_id` is parsed, so a discarded trip never enters event matching or row preparation;
2. `execute_client_trips_insert`, which applies the cap to the exact value each row binds to `trip_distance_meters` *inside the call that issues the statement*, drops the over-cap rows, and issues no statement at all if nothing admissible remains.

The second point is what a future job inherits. A new Client Trips writer does not have to remember the rule, or even know it exists: it only has to write rows the way the repository already writes them. It cannot construct rows, skip the gate and still reach the database through the sanctioned path, because the gate lives inside that path rather than beside it.

`ops/tests_manual/test_client_trips_distance_cap.py` enforces this structurally with an AST audit of every non-test module under `jobs/`, `ops/`, `api/`, `scripts/` and `delivery/`: a module whose statically visible SQL contains a `client_trips` INSERT must route it through `execute_client_trips_insert` and must not hand it to `cursor.execute*` directly. **Importing `client_trips_admission` is explicitly not sufficient to pass** — the suite carries two rogue-writer fixtures (one importing the module but calling `cur.executemany` on an inline f-string, one on a module-level SQL constant) and asserts both are rejected, plus a compliant fixture asserted to pass.

Honest limit of the structural guard: it resolves SQL that is statically visible — string literals, f-strings, `a + b` concatenation and names bound to those, which is every statement in this repository's established style. SQL assembled so that no fragment is recognizable would escape the audit, but it would equally escape the writer *detection*, so such a module was never claimed to be covered. The runtime enforcement inside `execute_client_trips_insert` is unconditional for anyone who uses it; the audit is what makes not using it a test failure rather than a silent regression.

The `/trips` request itself supports no server-side maximum-distance filter (only `start_timestamp`, `end_timestamp`, `incl_private` and pagination), so the invariant is enforced locally and unconditionally.

Observability — rejections are aggregated, never logged per trip:

- `trips_sync` exposes `client_trips_rejected_distance_over_2000km` in `TRIP_PARSE_DIAGNOSTIC_COUNTER_KEYS`, so the count appears in the parse-diagnostics log and in the run completion summary alongside `run_id` and `client_id`. A single `WARNING` per run adds `client_trips_rejected_max_distance_meters` and a bounded sample of rejected provider trip ids. The companion counter `client_trips_rejected_distance_over_2000km_at_persistence` reports anything stopped by the second boundary and is expected to stay `0`;
- `backfill_trips_insert_only` reports the same canonical key in its preflight/commit context, plus `distance_over_2000km` inside `rejected_by_reason` and the cap itself as `max_trip_distance_meters`.

### `jobs.api.telematics.sync_trips_and_speeding` — Workflow A, Phase 2

**Onboarding nowego klienta:** przed pierwszym uruchomieniem tego joba dla nowego klienta wymagane jest pełne onboardowanie — patrz `docs/07_operations.md`, sekcja 5.1 „Workflow A — Client Onboarding (Telematics)".

Funkcjonalność:

- `GET /trips` zewnętrznego API (Telematics) dla danego okna `[window_start_ts, window_end_ts]`; job obowiązkowo dzieli każde uruchomienie na chunki (`chunk_days`, default `2`, cap `5`) i dopiero dla każdego chunka osobno wywołuje provider client; request wysyła `incl_private=true`, więc obejmuje także prywatne tripy
- gdy `trip_metrics_population_source='api_migration'`, `GET /vehicles/events` w strict all-or-nothing adaptive chunkach czasowych (default start `4h`, minimum `30m`) z `limit<=1000`; po timeout/provider failure job zmniejsza chunk (`4h → 2h → 1h → 30m`) i po sukcesie kontynuuje mniejszym rozmiarem; fleet-wide surowa telemetria jest filtrowana lokalnie po rejestracjach mających tripy w runie oraz po `speed >= 140` dla liczników speeding
- okno requestu `/vehicles/events` jest serializowane jako lokalny wall-clock `Europe/Warsaw`, nie jako projekcja UTC — kontrakt zmierzony bezpośrednio sondą GET-only na DELTA00001 (2026-08-10) i opisany w `docs/18_telematics_trips_request_time_contract.md` §2; odpowiedzi (`event_ts`) pozostają UTC, a dopasowanie event→trip pozostaje absolutne. Obie ścieżki requestu (fleet oraz fallback per rejestracja) używają wspólnego `vehicle_events_wire_window`; przy przejściach DST okno jest poszerzane wyłącznie na zewnątrz (możliwy over-fetch, nigdy pominięcie interwału), a chunk jest ograniczony do 21 h, tak aby po poszerzeniu nie przekroczyć limitu 24 h endpointu
- opcjonalny, domyślnie wyłączony fallback per rejestracja dla terminalnie nieudanego fleet-wide chunka `/vehicles/events`: ten sam endpoint z query param `registration=<registration>`, rejestracje z `/vehicles` inventory union `/trips`, sekwencyjnie i z throttlingiem; fallback zwraca dane tylko po sukcesie wszystkich rejestracji, inaczej run failuje przed DB upsert
- alternatywny tryb `event_enrichment_mode=audited_best_effort`: job rekursywnie dzieli failing fleet windows i per-registration windows, zachowuje udane subwindows, zapisuje nierozwiązane minimum windows jako structured gaps, uploaduje `vehicle_events_gap_audit.json` i wykonuje DB upsert z dostępnymi countami oraz `complete_event_enrichment=false`
- operator-safe disable mode `event_enrichment_mode=disabled`: job nie wykonuje żadnych calli `GET /vehicles/events`, nadal pobiera `/trips` oraz `/vehicles`, wykonuje DB upsert dla tripów i zapisuje deterministyczne zera dla liczników zależnych od eventów (`high_rpm_events_count`, `overrev_events_count`, `speeding_140_160_count`, `speeding_160_170_count`, `speeding_170_plus_count`); ten tryb nie jest traktowany jako partial failure
- przy `trip_metrics_population_source='api_migration'` ten sam fleet-wide strumień `GET /vehicles/events` jest źródłem HIGH_RPM / OVERREV; job liczy wyłącznie provider-labeled event rows, nie progi `rpm` i nie `GET /alerts/notifications`
- **global Client Trips admission rule:** provider trips with `trip_distance > 2 000 000` m (2 000 km) są odrzucane przed jakąkolwiek persystencją do `client_trips` — patrz sekcja „Client Trips — global ingestion admission rule" wyżej; odrzucony trip nie wchodzi też do event matching, a licznik `client_trips_rejected_distance_over_2000km` trafia do diagnostyki runu
- trip-level fuel enrichment is deprecated and skipped; this job does not call fuel endpoints
- `start_timestamp` / `end_timestamp` pozostają timezone-aware (`timestamptz` w bazie klienta); logi i user-facing konteksty joba pokazują dodatkowe pola lokalne `*_local` w `Europe/Warsaw`
- upsert do bazy biznesowej klienta:
  - `client_trips` — finalny, uporządkowany schemat z kolumnami lokalizacji, odometru, RPM/OVERREV, liczników speeding, tagów kierowcy (`driver_tag_description`, `identification_tag_id`) oraz `trip_mode`; deprecated kolumny trip-level fuel i legacy terminal/speeding-bucket lineage nie są już częścią finalnej tabeli
  - `client_speeding_notifications` pozostaje w schemacie dla historycznych danych notification, ale ten job nie pobiera już `GET /alerts/notifications` do liczników HIGH_RPM / OVERREV
  - zapisywane rekordy `client_trips` niosą równolegle:
    - `client_id` (kanoniczny identyfikator systemowy, UUID),
    - `client_code` (czytelny identyfikator operatorski, np. `DELTA00001`; dodatkowe pole, nie zamiennik `client_id`)
- przy `trip_metrics_population_source='api_migration'` obliczenie i zapis per-trip liczników speeding z surowych próbek `speed >= 140` z fleet-wide `/vehicles/events`
- `driver_tag_description` i `identification_tag_id` są mapowane z payloadu `/trips`, jeśli provider je zwraca
- `trip_mode` jest mapowane z `/trips.is_private`: `true` → `private`, `false` → `business`, brak/nieznana wartość → `NULL`; `trip_type` nie jest używany, bo spec opisuje go jako user-defined klasyfikację odrębną od telematycznego `is_private`
- `vehicle_name` i `vehicle_description` są wzbogacane z batch-safe `GET /vehicles`: job pobiera fleet-wide inventory raz na run, buduje lookup po `vehicle_id` z fallbackiem po znormalizowanej `registration`, mapuje `vehicle_name` oraz `client_vehicle_description`; jeśli pojazd nie pasuje do inventory, pola pozostają `NULL`
- liczenie jest na poziomie wiersza telemetrycznego: każdy wiersz z `/vehicles/events`, który przejdzie lokalne filtry i mieści się w tripie, liczy się jako osobne naruszenie; job nie grupuje po timestampie i nie deduplikuje wierszy z tą samą rejestracją, timestampem ani prędkością
- buckety naruszeń są wyznaczane bez grupowania zdarzeń:
  - `speeding_140_160_count`: `speed >= 140 AND speed < 160`
  - `speeding_160_170_count`: `speed >= 160 AND speed < 170`
  - `speeding_170_plus_count`: `speed >= 170`
- `/trips.max_speed` nie jest używany do dokładnego liczenia bucketów
- safety cap `max_pages=500` per adaptive chunk `/vehicles/events`, `TELEMATICS_PROVIDER_PAGE_LIMIT` dla standardowej paginacji (`/trips`, `/vehicles`), globalny provider safety budget, backoff retry i rate limit zapobiegają nieograniczonej paginacji oraz burstom API; osiągnięcie page capu oznacza niekompletny event chunk i fail przed DB upsert; `TELEMATICS_PROVIDER_VEHICLE_EVENTS_LIMIT`, `TELEMATICS_PROVIDER_VEHICLE_EVENTS_MAX_PAGES_PER_DAY`, `TELEMATICS_EVENTS_CHUNK_HOURS`, `TELEMATICS_EVENTS_MIN_CHUNK_MINUTES`, `TELEMATICS_EVENTS_TIMEOUT_S`, `TELEMATICS_EVENTS_RATE_LIMIT_RPS` oraz `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_*` mogą nadpisać domyślne wartości dla `/vehicles/events`
- matching naruszeń speeding do tripów:
  - rejestracja (`registration`)
  - włączenie granic: `event_ts` w `[trip.start_timestamp, trip.end_timestamp]` (inclusive)
  - tie-break: wąski (najkrótszy) przedział zawierający zdarzenie, potem najwcześniejszy `trip.start_timestamp`
- HIGH_RPM / OVERREV:
  - źródło: provider-labeled fleet-wide `/vehicles/events`
  - rozpoznawane etykiety obejmują m.in. `OVERREV_START`, `OVERREV_END`, `OVERREV`, `OVER_REV`, `OVER REV`, `HIGH_RPM_START`, `HIGH_RPM_END`, `HIGH_RPM`, `HIGH RPM`, `HIGH-RPM`
  - `*_START` i niesufiksowane etykiety liczą jako zdarzenie; `*_END` jest rozpoznawane i ignorowane, żeby nie podwajać jednego provider incidentu
  - przypisanie do tripu używa `vehicle_id` albo znormalizowanej `registration` oraz `event_ts` w `[trip.start_timestamp, trip.end_timestamp]`

Parametry (wymagane):

- `client_id`
- `window_start_ts` (ISO-8601)
- `window_end_ts` (ISO-8601)

Parametry opcjonalne:

- `skip_fuel` (bool, default `false`) — deprecated no-op; akceptowany tymczasowo dla kompatybilności wstecznej i nie zmienia zachowania
- `skip_vehicle_events` (bool, default `false`) — diagnostyczny parametr historyczny; w strict complete-enrichment flow `true` kończy job błędem przed DB upsert, ponieważ finalne trips-only wyniki bez speeding/RPM enrichment nie są akceptowalne
- `chunk_days` (int, default `2`, maximum `5`) — job-level chunk size for `GET /trips`; values `1`–`5` are accepted, `0`/negative and values above `5` fail validation clearly. Manual callers do not need to split large backfills themselves; the job applies the same chunking for manual, dispatcher/scheduled, and any other invocation path.
- `event_enrichment_mode` (`enabled` albo `disabled`, default `enabled`) — `enabled` zachowuje aktualny flow z `/vehicles/events`; `disabled` pomija `/vehicles/events` i zapisuje zera dla event-derived liczników. Dla kompatybilności wstecznej istniejące wartości `strict` i `audited_best_effort` są nadal akceptowane jako strategie trybu enabled; `audited_best_effort` można też ustawić przez `TELEMATICS_EVENTS_ENRICHMENT_MODE`.
- Dla uruchomień przez dispatcher `event_enrichment_mode` jest ustawiany per schedule z `workflow_a_control.client_dataset_schedule.event_enrichment_mode`; migracja `018_workflow_a_schedule_event_enrichment_mode.sql` dodaje kolumnę `NOT NULL DEFAULT 'enabled'` z CHECK `enabled|disabled`. Dispatcher przekazuje tę wartość tylko dla `trips_sync`. `chunk_days` nie musi być przekazywany przez dispatcher, bo `sync_trips_and_speeding` wymusza default `2` wewnątrz joba.

- `schedule_run_type` (`DAILY` | `WEEKLY_RECONCILIATION` | `MONTHLY_RECONCILIATION`) — rola harmonogramu, która zgłosiła ten fire. Dispatcher przekazuje ją **tylko** dla `trips_sync` i **nie rozgałęzia** na niej sterowania; job używa jej wyłącznie do wyboru zakresu event enrichment (niżej). Brak wartości = `DAILY`.
- `vehicle_events_scope` (`window` | `reconciliation_candidates`) — jawne nadpisanie zakresu dla narzędzi recovery/audytu. Gdy nieustawione, wynika z `schedule_run_type`; nieznana rola daje `window`, czyli zachowanie historyczne.

### Zakres event enrichment a szerokość okna tripów

**Szerokość okna tripów nie jest szerokością okna eventów.** To rozdzielenie jest kontraktem, nie optymalizacją — przywrócenie równości `okno tripów == fleet event fetch` jest regresją, nie uproszczeniem.

Okno rekoncyliacji (M6, `L=16`) jest szerokie, bo **tripy** bywają dostarczane z opóźnieniem. Eventy nie. Przy `trip_metrics_population_source='api_migration'` i `overwrite_existing=true` job przeliczał i **nadpisywał** pięć kolumn event-derived (`high_rpm_events_count`, `overrev_events_count`, `speeding_140_160_count`, `speeding_160_170_count`, `speeding_170_plus_count`) dla **każdego** tripa w oknie — mimo że w produkcji ~0,27% tripów w oknie 16-dniowym to realnie nowe trafienia.

| zakres | co kupuje | kiedy |
| --- | --- | --- |
| `window` | wszystkie eventy fleet w całym oknie; metryki przeliczane dla każdego tripa | `DAILY` — krótkie okno jest czytane ponownie właśnie po to, by spóźniony event mógł poprawić wczorajszy licznik |
| `reconciliation_candidates` | eventy tylko dla tripów, których **nie było** w `client_trips` przed tym runem | `WEEKLY_RECONCILIATION`, `MONTHLY_RECONCILIATION` |

**Dlaczego obecność wiersza, a nie wartość metryki.** Wartości nie odróżniają „wzbogacone, zaobserwowano zero" od „nigdy nie wzbogacone": `high_rpm_events_count` wynosi 0 dla wszystkich 220 497 wierszy FOXTROT00001, a liczba NULL-i to 0. Kandydat jest więc definiowany wyłącznie przez brak `(client_id, provider_trip_id)` w `client_trips`, odczytany **przed** jakimkolwiek wydatkiem na eventy, poza transakcją zapisu. Kierunek nieaktualności jest bezpieczny: wiersz, który probe zobaczył, istnieje (job nie usuwa tripów), więc nowy trip nie może zostać uznany za już-przechwycony i zostać z fałszywymi zerami; odwrotny dryf tworzy tylko nadmiarowego kandydata, którego wzbogacamy poprawnie.

**Dlaczego okno kandydata wystarcza.** Reguła dopasowania (`_compute_rpm_vehicle_event_counts` i pass speeding) liczy event dla tripa tylko gdy `trip.start_ts <= event.event_ts <= trip.end_ts`. Event poza interwałem tripa nie może zmienić jego metryk. Okno `[start_ts, end_ts]` dla rejestracji tripa nie jest więc przybliżeniem pełnego skanu — dla tych tripów to **ten sam zbiór rozstrzygających eventów**. Padding nie jest dodawany. Sklejanie sąsiednich interwałów (`TELEMATICS_EVENTS_CANDIDATE_COALESCE_MINUTES`, default 360) jest wyłącznie dźwignią kosztu: pobiera nadzbiór, a każdy dodatkowy event i tak nie przechodzi tego samego testu czasu.

**Zachowanie istniejących metryk.** Trip, którego ten run nie odkrył, jedzie w drugiej partii upsertu, z klauzulą konfliktu pozbawioną pięciu kolumn metrycznych; jego wiersz niesie `NULL` dla pary RPM (własna wartość „nie wzbogacone" kolumny), a kolumny speeding — `NOT NULL`, więc niezdolne wyrazić „nieznane" — są pomijane w passie bucketów zamiast zapisywane zerem. Tożsamość, geometria i czas nadal się rekoncyliują (rekoncyliacja musi móc poprawić `end_timestamp`). `first_seen_*` i `Dysponent_ID` pozostają poza każdą listą `DO UPDATE SET`, bez zmian.

**Fail-closed.** `_fetch_vehicle_events_candidate_window` nie jest ścieżką fallbacku i **nie podnosi** limitów providera (`_ensure_registration_fallback_actual_budget` jest tu celowo nieużywany). Każde przerwanie — page cap, budżet, dno podziału, głębokość podziału — podnosi wyjątek. Częściowo pobrane okno dałoby zaniżony licznik nieodróżnialny od realnego zera, a dla nowo przechwyconego tripa to zero jest **trwałe**: następna rekoncyliacja poprawnie zobaczy wiersz jako już przechwycony.

**Podział okna przy page cap.** Gdy okno kandydata uderzy w page cap, jest dzielone na pół, a obie połowy są **przycinane do własnych granic** (`_clamp_events_to_window`; prawa połowa jest wyłączna na szwie). Przycinanie wyniku, nie tylko żądania, jest konieczne, bo `vehicle_events_wire_window` celowo **poszerza** okno na drucie — a duplikat nie jest tu nieszkodliwy: RPM deduplikuje po `_rpm_vehicle_event_dedupe_key`, ale `_compute_speeding_violation_counts` liczy każdą przekazaną violation, więc ten sam odczyt 165 km/h z dwóch sąsiednich okien to **trwale** zawyżony `speeding_160_170_count` na nowo przechwyconym tripie. Szew jest przypięty do pełnej sekundy (`replace(microsecond=0)`), bo `PROVIDER_WIRE_DT_FORMAT` to `%Y-%m-%d %H:%M:%S`, a `strftime` **obcina**: szew pod-sekundowy trafiłby na drut w obu żądaniach jako ta sama sekunda i wyłączny filtr otworzyłby **dziurę** zamiast zamknąć nakładkę. Okno zbyt krótkie, by unieść szew na pełnej sekundzie, kończy się `CANDIDATE_SEAM_BELOW_WIRE_RESOLUTION`.

**Bezpiecznik gęstości.** Gdy liczba okien kandydatów przekroczy `TELEMATICS_EVENTS_CANDIDATE_MAX_WINDOWS` (default 1200), job wraca do pełnego skanu fleet i wyłącza również zakresowany **zapis** — po pobraniu wszystkich eventów przeliczenie każdego tripa jest realną obserwacją, a nie zerowaniem. Cold start / masowy backfill zostaje więc na ścieżce historycznej: przy „każdy trip nowy" scoping modeluje się na 4 528 requestów wobec ~2 590 dla skanu fleet.

**Zmierzony efekt (FOXTROT00001, realna geometria okna 16-dniowego, 29 456 tripów / 310 rejestracji).** Requesty idą na osobny klucz budżetu `/vehicles/events:registration`, nie na `/vehicles/events`.

| scenariusz | kandydaci | okna | strategia | requesty | % z 3000 |
| --- | --- | --- | --- | --- | --- |
| obserwowana gęstość opóźnień (0,27%) | 80 | 79 | scoped | 79 | 2,6% |
| 10× obserwowanej (2,7%) | 795 | 729 | scoped | 729 | 24,3% |
| cały dzień przychodzi z opóźnieniem | 892 | 210 | scoped | 210 | 7,0% |
| trzy pełne dni z opóźnieniem | 4 762 | 780 | scoped | 780 | 26,0% |
| każdy trip nowy (degeneracja) | 29 456 | 4 527 | fleet-scan | 2 590 | 86,3% |

Batch czterech klientów od 00:30 (latencja kalibrowana produkcyjnie): **~03:39 → ~00:58**.

Tryby event enrichment:

- `enabled` — default; wykonuje obecny flow enrichment przez `/vehicles/events`. Domyślna strategia fetch pozostaje strict; legacy `event_enrichment_mode=strict` jest aliasem zgodności.
- `disabled` — gdy `trip_metrics_population_source='api_migration'`, nie wywołuje `/vehicles/events`, nie uruchamia fleet ani registration fallbacku, nie uploaduje gap audit, loguje `event_enrichment_status=disabled`, `complete_event_enrichment=true`, `speeding_rpm_counts_are_partial=false` i zapisuje zera dla OVERREV/HIGH_RPM oraz bucketów 140/160/170.
- `strict` — legacy strategia trybu enabled; DB upsert nie startuje, dopóki `/trips` nie zostanie sparsowane bez utraty wierszy wymaganych do enrichment oraz dopóki wszystkie wymagane chunki `/vehicles/events` nie zakończą się sukcesem.
- Fleet-wide jest ścieżką podstawową i najszybszą. Fallback per rejestracja dotyczy wyłącznie terminalnie nieudanego fleet chunka, nie całego dnia/runu.
- Jeśli fallback dla jednej rejestracji failuje na dużym oknie, job dzieli tylko tę rejestrację/window na mniejsze subchunki do `TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES`.
- W `strict`, jeśli jakikolwiek fallback subchunk nadal failuje, job loguje szczegóły (`registration`, window, status/body summary, retry context, phase) i kończy run błędem bez częściowych countów.
- `audited_best_effort` — failing fleet chunks są rekursywnie dzielone do `TELEMATICS_EVENTS_BEST_EFFORT_MIN_FLEET_CHUNK_MINUTES`; udane subchunki są używane, a terminalne failures są zapisywane jako `scope=fleet` gaps, chyba że włączone recovery per rejestracja pokryje to okno.
- W `audited_best_effort`, recovery per rejestracja używa tego samego `GET /vehicles/events` z query param `registration=<registration>`, dzieli failing registration windows do `TELEMATICS_EVENTS_BEST_EFFORT_MIN_REGISTRATION_CHUNK_MINUTES`, zachowuje udane subwindows i zapisuje terminalne failures jako `scope=registration` gaps.
- Structured gap ma pola: `scope`, `registration`, `chunk_start_ts`, `chunk_end_ts`, `duration_seconds`, `endpoint`, `mode`, `failure_code`, `status_code`, `response_body_summary`, `attempts`, `split_depth`, `min_chunk_minutes`.
- Jeśli w `audited_best_effort` istnieją gaps, finalne logi mają `event_enrichment_status=partial`, `complete_event_enrichment=false`, `event_gap_count`, `fleet_gap_count`, `registration_gap_count`, `affected_registrations_count`, `affected_registrations_sample`, `total_gap_duration_seconds`, `events_fetched_total`, `events_fetched_from_fleet`, `events_fetched_from_registration_fallback` oraz `speeding_rpm_counts_are_partial=true`.
- Gap audit jest uploadowany jako artefakt runu `VEHICLE_EVENTS_GAP_AUDIT` z pliku `vehicle_events_gap_audit.json`; payload zawiera `summary` i pełną listę `gaps`.
- Jeżeli `client_account.trip_metrics_population_source` nie jest `api_migration`, job nadal synchronizuje tripy/pojazdy/kierowców, ale nie pobiera `/vehicles/events`, nie zapisuje `high_rpm_events_count` / `overrev_events_count`, nie zapisuje speeding bucketów i nie wykonuje osobnego UPDATE bucketów. Logi mają `event_enrichment_status=source_mismatch` oraz `skip_reason=trip_metrics_population_source_mismatch`.
- Fail-fast działa także w `audited_best_effort` dla `401/403`, persistent `429`, malformed response/pagination, provider request budget, max split depth oraz max gaps. Tryb nie używa concurrency.
- Logi postępu obejmują `Phase start/end`, `chunk_index`, `chunk_total_estimated`, `fallback_rps`, `estimated_requests`, `estimated_min_duration_seconds`, `completed`, `total`, `percent`, `estimated_remaining_seconds`, `fallback_requests`, `fallback_events_fetched`, `event_enrichment_status` oraz `complete_event_enrichment`.

Uwaga o HIGH_RPM / OVERREV:

- Liczniki `high_rpm_events_count` i `overrev_events_count` pochodzą z provider-labeled fleet-wide `/vehicles/events`.
- Job nie używa progów liczbowych `rpm`, ponieważ limity RPM mogą różnić się per pojazd.
- `GET /alerts/notifications` może istnieć w provider client i diagnostykach manualnych, ale nie jest już production source dla tych liczników w `sync_trips_and_speeding`.

Konfiguracja (control-plane) ładowana dla `client_id`:

- z platformowej bazy Postgres (`workflow_a_control.client_account`)
- zawiera m.in. `provider_base_url`, BasicAuth username oraz `*_secret_ref` do host-managed sekretów
- może zawierać także `client_code` (human-readable), który jest propagowany do tabel biznesowych klienta obok `client_id`
- `speed_trigger_filter_text` może istnieć w control-plane jako legacy pole, ale nie jest już używany do liczenia speeding; aktualne liczniki pochodzą z raw telemetry `/vehicles/events`, tylko gdy `trip_metrics_population_source='api_migration'`.
- `trip_metrics_population_source` wybiera jedno źródło prawdy dla grupy metryk tripów (`api_migration`, `report_207_migration`, `d105_2_ecodriving_migration`, `disabled`); default dla istniejących rows to `api_migration`. Loader D105.2 EcoDriving wymaga `d105_2_ecodriving_migration` i używa tego samego source gate.
- `workflow_a_control.client_account.trips_pagination_mode` dopuszcza `strict_meta` i `data_invariants_v1`, a domyślne i backfillowane `strict_meta` zachowuje obecny strict pagination path. Pole jest ładowane przez wdrożony runtime C6. Od `2026-08-04` produkcja ma **czterech** klientów `data_invariants_v1` — `BRAVO00016` (canary, `2026-08-03`), `ALPHA00001` (`2026-08-03`), `DELTA00001` i `FOXTROT00001` (oba `2026-08-04`) — a `ECHO00001` pozostaje `strict_meta` i **niezbootstrapowany**. Każde z czterech przejść wykonano osobną, wąską transakcją operatorską dla jednego `client_id`; **żadnego zbiorczego update'u trybu nie wykonano i nie wolno go wykonać**. Każdy kolejny klient wymaga własnego inventory, przedziału, bundla dowodowego i osobnej autoryzacji. Szczegóły włączenia: `docs/07_operations.md` §5.5.
- **`strict_meta` nie jest zatwierdzonym produkcyjnym trybem pracy aktywnego schedule'a Telematics `/trips`.** Może pozostać wyłącznie jako fail-closed stan onboardingowy lub diagnostyczny **przed** bootstrapem coverage. Aktywny produkcyjny schedule `trips_sync` nie może być trwale sparowany ze `strict_meta` — to właśnie ten stan wyprodukował serię `PAGINATION_MISMATCH` z `2026-08-01`…`2026-08-04` dla `DELTA00001` i `FOXTROT00001`. Pełny kontrakt ścieżki dla nowych kont: `docs/07_operations.md` §5.5.
- **Ścieżka cold start dla klienta, który nigdy nie wykonał runu (od `2026-08-04`).** Historyczny bootstrap C10 jest oparty na dowodach z `client_schedule_run_history` i nie obsługuje klienta z wyłączonym schedule'em i zerową historią — odrzuca go poprawnie (`AMBIGUOUS_SCHEDULE`, a przy pustej historii `INSUFFICIENT_HISTORY_EVIDENCE`, której writer C10 nie akceptuje). Dla klienta **dowodliwie pustego** istnieje osobna, wąska ścieżka: `ops/audit_telematics_cold_start.py` (read-only, klasyfikacja `COLD_START_ZERO_STATE_CONFIRMED` — `UNRESOLVED_GAPS_PRESENT` nigdy nie jest reużywane dla klienta bez historii) → `ops/bootstrap_telematics_cold_start_coverage.py` (jeden wiersz coverage o **zerowej szerokości**, `A == W`) → wąska zmiana trybu → `ops/recover_telematics_trips_window.py --allow-disabled-schedule-for-cold-start` → `ops/activate_telematics_trips_schedule.py` (dokładnie jedno pole `enabled false → true`). Ścieżka **nie osłabia** kontraktu C10: klient z jakąkolwiek historią wykonań jest przez każde z tych narzędzi odrzucany. Nie wymaga migracji — migracja `057` dopuszcza `A <= W`. Semantyka, dowód braku dziury granicznej i zachowanie przy awarii: `docs/13_…` §13.6a i `docs/07_operations.md` §5.5. Ścieżka jest zaimplementowana i przetestowana, ale **nigdy nie wykonana produkcyjnie**; `ECHO00001` pozostaje niezmieniony do czasu niezależnej recenzji i osobnego rolloutu.
- **Defekt cold startu z `2026-08-04` — NAPRAWIONY tego samego dnia; `ECHO00001` pozostaje w kwarantannie.** Pierwszy rzeczywisty rollout (`ECHO00001`) wykazał BLOCKER: `jobs.api.telematics.sync_trips_and_speeding` ma własną bramkę `if not schedule.enabled: return`, a ścieżka cold start **wymaga** wyłączonego schedule'a. Job kończył się kodem `0` bez żadnego requestu do providera i bez żadnego zapisu biznesowego, a `ops/recover_telematics_trips_window.py` uznawał `returncode == 0` za sukces biznesowy i przesuwał `covered_through_ts`. `ECHO00001` ma z tego powodu fałszywe roszczenie coverage `[2026-07-01T00:00:00Z, 2026-08-01T00:00:00Z]` przy `0` wierszy w `echogallery_main.public.client_trips`. **Ten wiersz jest jawnie nieważny i nie wolno go używać do raportowania.** `ECHO00001` pozostaje w kwarantannie: schedule `enabled = false`, tryb bez zmian, brak recovery W02, brak aktywacji; jego naprawa nie jest częścią utwardzenia i nie jest kryterium jego ukończenia.
- **Utwardzenie ścieżki onboardingu (`2026-08-04`) — `returncode == 0` nigdy nie wystarcza.** Trzy warstwy, wymagane łącznie:
  - **Jawna autoryzacja manual-recovery** (`jobs/api/telematics/manual_recovery_authority.py`). Bramka wyłączonego schedule'a zachowuje domyślne zachowanie — zwykłe wywołanie przy wyłączonym schedule'u wykonuje zero pracy providera i pomija run. Przejść ją może wyłącznie koniunkcja: parametrów joba (`trigger = MANUAL_RECOVERY`, dokładny `client_id`, `expected_schedule_id`, dataset `trips_sync`, dokładny `manual_recovery_run_id`, dokładne okno, jawna flaga `allow_disabled_schedule_manual_recovery`), **atestacji uruchomienia** przekazanej poza parametrami w `TELEMATICS_MANUAL_RECOVERY_AUTHORITY`, oraz **trwałego wiersza recovery** w stanie `RUNNING` o zgodnym kliencie, schedule'u, datasecie i oknie, przy wciąż wyłączonym schedule'u. Sama flaga logiczna albo bezpośrednie wywołanie joba **nie wystarczają**. Dispatcher nie może jej ani zbudować (`_build_job_params` odmawia z zasady deny-by-default), ani odziedziczyć (`_launch_job` usuwa zmienną ze środowiska każdego podprocesu). Zachowanie recovery przy **włączonym** schedule'u jest niezmienione; żaden wiersz historii schedule'a nie jest syntetyzowany. **Poziom zaufania atestacji — dokładnie:** jest to *atestacja operacyjna i zabezpieczenie przed przypadkowym użyciem*, nie granica bezpieczeństwa i nie uwierzytelnienie. Nie zawiera sekretu, wartości losowej ani wartości znanej wyłącznie launcherowi — każde pole to stała modułu albo identyfikator, który operator i tak podaje — więc proces lokalny działający jako **ten sam użytkownik systemowy**, znający te identyfikatory, może zbudować równoważną atestację. Sprawdzenie nazwy launchera jest kontrolą spójności, **nie** dowodem pochodzenia wywołania; mechanizm nie chroni przed złośliwym procesem tego samego użytkownika, operatorem świadomie budującym atestację, przejęciem konta usługowego ani przejęciem poświadczeń bazy platformowej. Istotne pozostaje to, co realnie działa: **właściwą bramką autoryzacyjną jest trwały wiersz recovery `RUNNING`**, względem którego weryfikowana jest każda tożsamość i granica okna, oraz **izolacja dispatchera**. Wiersz ten nie jest klaimowany w osobnym, wcześniejszym kroku operatora: `ops/recover_telematics_trips_window.py` klaimuje go i uruchamia proces potomny w **tym samym wywołaniu** — jedna transakcja wstawia dokładnie jeden wiersz `RUNNING` po ponownej ewaluacji wszystkich bramek pod blokadami klaimu, następnie budowana jest atestacja uruchomienia, dopiero potem startuje proces potomny, a na końcu odczytywany jest strukturalny wynik terminalny i wykonywana finalizacja recovery/coverage. Operator dostarcza wcześniej **zatwierdzenie** (klient, dataset, okno, oczekiwany watermark, powód, `approval_ref`), a nie ręcznie zapisany wiersz. Łącznie żadne zwykłe przypadkowe wywołanie nie spełnia pełnej koniunkcji — i taki jest przyjęty model zagrożeń.
  - **Strukturalny wynik terminalny** (`jobs/api/telematics/execution_outcome.py`, wersja `telematics-trips-execution-outcome/1`). Job zapisuje dokładnie jeden ściśle parsowany rekord JSON pod ścieżką z `TELEMATICS_TRIPS_EXECUTION_OUTCOME_FILE`: `EXECUTED_COMMITTED` | `EXECUTED_ZERO_ROWS_COMMITTED` | `SKIPPED_DISABLED_SCHEDULE` | `SKIPPED_OTHER` | `FAILED`, wraz z `client_id`/`client_code`, `schedule_id`, datasetem, UUID recovery i runu platformowego, żądanym oknem, informacją czy wejściowo uruchomiono providera i logikę transakcji biznesowej, statusem transakcji, licznikami `prepared`/`upserted`/`malformed`, flagą i powodem pominięcia oraz znacznikiem terminalnym. Dopasowywanie logów **nie jest** kontraktem autorytatywnym.
  - **Bramka finalizacji coverage.** `W` przesuwa się wyłącznie, gdy: `returncode = 0` **i** wynik to `EXECUTED_COMMITTED` albo `EXECUTED_ZERO_ROWS_COMMITTED` **i** `provider_execution_entered = true` **i** `business_transaction_entered = true` **i** status transakcji to `COMMITTED` **i** `skipped = false` **i** zgadzają się tożsamości klienta, schedule'a, datasetu, recovery oraz okna **i** tożsamość runu platformowego jest obecna po obu stronach i dokładnie równa. Dla wyniku mogącego przesunąć coverage brak, pusty lub zniekształcony `platform_run_id` — po stronie launchera albo rekordu — jest odmową (`EXECUTION_OUTCOME_PLATFORM_RUN_ID_MISSING` / `..._MALFORMED`), a inny poprawny UUID jest niezgodnością tożsamości; porównanie nigdy nie jest pomijane dlatego, że jedna ze stron jest pusta. Dla rekordów pominięcia i `FAILED` `platform_run_id` pozostaje opcjonalny. Nie przesuwa się przy pominięciu dowolnego rodzaju, przy braku wyniku, przy wyniku zniekształconym, przy braku commitu ani przy wyniku należącym do innego lub przeterminowanego wykonania. **Wykonanie z zerem wierszy, ale zacommitowane, jest ważne i przesuwa `W`; wykonanie pominięte nigdy nie przesuwa.** Przy niepowodzeniu dowody recovery są zachowane, schedule pozostaje wyłączony, nic nie jest ponawiane automatycznie. **Trzy stwierdzenia o wykonaniu są niezależne** i żadne nie wynika z pozostałych: wejście do providera, wejście do transakcji biznesowej i commit. Rekord twierdzący, że wykonanie zostało zacommitowane, a jednocześnie raportujący `provider_execution_entered = false`, opisuje wiersze, których nie mógł pobrać — jest odrzucany jako wewnętrznie sprzeczny już przy **parsowaniu** (`EXECUTION_OUTCOME_MALFORMED`), a nie tylko klasyfikowany jako niekwalifikujący się. Recorder zachowuje się zgodnie z parserem: dojście do stwierdzenia terminalnego bez wejścia do providera zapisuje `FAILED`, nigdy wyniku wykonanego. Dokładnie zgodny `platform_run_id` nie ratuje takiego rekordu — weryfikacja tożsamości odpowiada na pytanie, *czyjego* wykonania rekord dotyczy, a nie czy jest spójny.
  - **Aktywacja wymaga tego samego dowodu dla każdego okna łańcucha** (`ops/activate_telematics_trips_schedule.py`), więc status `SUCCESS` wiersza recovery przestał być dowodem sam w sobie. Każde akceptowane okno musi dodatkowo nieść poprawny `platform_run_id` dokładnie równy temu, który zapisał jego własny wiersz recovery; brak, wartość pusta, zniekształcona lub inna kończy się `ACTIVATION_REFUSED_EXECUTION_PROOF`. Konsekwencja: wiersze recovery sprzed `2026-08-04` nie niosą dowodu strukturalnego, więc zbudowany z nich łańcuch nie może zostać aktywowany i wymaga ponownego wykonania na poprawionej ścieżce.
- **Recovery cold startu to łańcuch jednego lub więcej okien (od `2026-08-04`).** Zakres cold startu dłuższy niż `trips_max_recovery_span_seconds` klienta nie mieści się w jednym recovery, więc krok recovery jest ciągiem `baseline W → okno 1 SUCCESS → W1 → … → zatwierdzone końcowe W → aktywacja`. **Cold start jednookienny to łańcuch długości jeden i pozostaje wspierany.** Podział na okna jest deterministyczny i czysty (`ops/telematics_cold_start_chain.py`: pierwsze okno startuje w `A`, każde następne dokładnie tam, gdzie skończyło się poprzednie, bez dziur i nakładek, ostatnie kończy dokładnie na zatwierdzonej granicy). Tożsamość łańcucha to `--cold-start-chain-ref` utrwalony strukturalnie w istniejącym `approval_ref` jako `<chain>-W<NN>` — **żadna migracja nie jest dodawana**, a każde okno zachowuje własny unikalny `approval_ref`. Każde okno wymaga osobnego zatwierdzenia i osobnego wykonania; schedule pozostaje `disabled` przez cały łańcuch i nie powstaje żaden syntetyczny wiersz historii. Kontynuacja jest dozwolona wyłącznie, gdy cały stan wykonań celu to udany, stykający się prefiks tego łańcucha z korespondencją jeden-do-jednego z runami platformowymi `SUCCESS`; wiersz nieudany, aktywny, obcy lub nieciągły odmawia. Aktywacja **nie wymaga już dokładnie jednego wiersza recovery** — wymaga `N` udanych stykających się okien, jawnego `--expected-successful-window-count` i jawnej końcowej granicy łańcucha. `ECHO00001` będzie potrzebował **dwóch** okien i pozostaje niezmieniony do czasu rolloutu.
- `workflow_a_control.client_account` ma także addytywny kontrakt stabilizacji C2: `trips_stabilization_delay_seconds=10800` (3 h czasu UTC), `trips_overlap_seconds=3600` (1 h ciągłego overlapu) oraz `trips_max_recovery_span_seconds=2678400` (31 dni). Migracja `056_workflow_a_trips_stabilization_config.sql` dodaje wartości domyślne, `NOT NULL` i ograniczenia zakresów/relacji, a loader waliduje dokładne typy i wartości bez fallbacku dla brakujących, `NULL` lub błędnych danych.
- Wartości stabilizacji są używane wyłącznie przez compatibility branch; przy `strict_meta` pozostają runtime-inert. Od canary enablementu `2026-08-03` obowiązują dla `BRAVO00016` (`D = 10800 s`, `O = 3600 s`, `R = 2678400 s` — wartości domyślne, niezmienione), natomiast czterej pozostali klienci nadal wykonują dotychczasowy strict runtime path. Zmiana tych wartości przez operatora nie jest autoryzowana; enablement `BRAVO00016` ich nie dotknął.
- Platformowe migracje control-plane `055_workflow_a_trips_pagination_mode.sql`, `056_workflow_a_trips_stabilization_config.sql`, `057_workflow_a_trips_coverage_state.sql` i `058_telematics_trips_manual_recovery.sql` są zastosowane produkcyjnie (`058` — `2026-08-03`).
- **Migracja `060_workflow_a_daily_trips_lookback_l3.sql` (M2) jest zastosowana produkcyjnie** — jeden wiersz w `public.schema_migrations`. Zmienia zachowanie: jeden strzeżony `UPDATE` na `workflow_a_control.client_dataset_schedule` przesuwa `lookback_days` `1 → 3` dla `ALPHA00001` / `trips_sync` (`daily`, `02:00 UTC`), więc okno nominalne dziennego fire'a to `[F − 3·86400 s, F]`, a efektywne `73 h`. Bez DDL, bez zmiany kodu, bez wpływu na innych klientów; `sync_trips_and_speeding` nie czyta `lookback_days` — czyta je wyłącznie `dispatcher.evaluate_schedule`. Pierwszy naturalny run `L = 3` (`2026-08-13 02:00:00Z`) zakończył się `SUCCESS` w `client_schedule_run_history` i w `public.runs`. Rollback `ops/sql/m2_rollback_alpha00001_daily_trips_lookback_to_1.sql` pozostaje uśpiony i **nie** został wykonany. Poprawność pokrycia historycznego pozostaje poza zakresem M2 (`COVERAGE_CORRECTNESS_DEFERRED_TO_M3`). Kontrakt i dowody: `docs/20_telematics_ingestion_permanent_repair_plan.md` §19, §19.12.
- **Status C7 (compatibility pagination state machine) — ZAIMPLEMENTOWANE, WDROŻONE PRODUKCYJNIE I ZRECENZOWANE (G-SM APPROVED).** Niezależną recenzję code-first G-SM wykonała `2026-08-03` **świeża, niezależna sesja `Opus 5`**. Niezależność recenzji jest **procesowa** (`docs/14_…` §9): świeża sesja, recenzent nie jest autorem, runtime recenzowany niezależnie i niemodyfikowany w trakcie recenzji, model raportowany prawdomównie. Wymóg **innego modelu recenzenta został wycofany decyzją operatorską** i jest opcjonalny — recenzja tym samym modelem jest ważnym zatwierdzeniem, a G-SM nie podlega ponownemu uruchomieniu z powodu tożsamości modelu. Szczegóły i pełna lista zweryfikowanych własności: `docs/07_operations.md` §5.5. Runtime produkcyjny na HEAD `2290acc3fce9fbe27ff9c8b5abbf19593bcb79e9` ładuje pliki **bajtowo identyczne** z zrecenzowanym commitem C7 `22db25e9cbc7ada60bf62c9a691957653d0c6cdd`; jedyne zmiany po C7 są dokumentacyjne. Od `2026-08-03` `provider_client.py` zna tryb paginacji (`TelematicsFleetProviderClient(trips_pagination_mode=…)`), `fetch_trips` rozgałęzia się na `_fetch_paginated_data_invariants_v1` albo na niezmienione `_fetch_paginated`, a `sync_trips_and_speeding.py` propaguje znormalizowany tryb z parametrów joba do klienta providera. **Runtime produkcyjny ładuje ten kod z commitu `22db25e9cbc7ada60bf62c9a691957653d0c6cdd`** (deployment direct-checkout — `docs/07_operations.md` §5.5). Ścieżka compatibility została **uruchomiona produkcyjnie po raz pierwszy `2026-08-03`** — nie jako zaplanowany fire, lecz jako jednorazowe ręczne recovery C11 (`recovery_run_id = 0e86fafa-ded3-4be4-8ec6-01dce344a0d1`, platform run `4088ac2e-2206-451f-8ac9-798084bcc856`, approval `TELEMATICS-C11-BRAVO00016-2026-08-03-CANARY-1`), które zamknęło zaległy interwał `2026-07-27T00:00:00Z` – `2026-08-03T00:00:00Z` i przesunęło `W` do `2026-08-03T00:00:00Z` ze źródłem `manual_recovery`. Pierwszy **zaplanowany** compatibility fire `BRAVO00016` nadal nie nastąpił. Klienci `strict_meta` zachowują dotychczasowe zachowanie `/trips` i nadal mogą skończyć się `PAGINATION_MISMATCH`.
  - Zaimplementowana decyzja **D5 Option B** (`docs/16_telematics_d5_total_policy_decision.md`): brak `meta.total` jest dozwolonym stanem compatibility (bez kodu błędu), terminacja wyłącznie regułą short-page `len(data) < requested_limit` (łącznie z pustą stroną), obecny `total` musi być prawdziwym nieujemnym integerem JSON (bool, float, numeric string, `null`, obiekt i tablica są odrzucane), identycznym na każdej stronie, nigdy nieprzekroczonym przez zakumulowane unikalne wiersze i uzgodnionym do równości przy terminacji. Taksonomia `total` to dokładnie cztery kody `docs/16_…` §5.3; `PAGINATION_COMPAT_TOTAL_ABSENT` nie istnieje.
  - Inwarianty danych: wymagana stabilna tożsamość `provider_trip_id = int(row["trip_id"])` dla każdego wiersza (bez jakiegokolwiek fallbacku z timestampów, współrzędnych, adresów, rejestracji czy kierowcy), brak duplikatu w stronie, brak nakładania z **dowolną** wcześniejszą stroną sub-okna (pełna historia), brak powtórzonego uporządkowanego fingerprintu i nieuporządkowanego zbioru tożsamości, `len(data) <= limit`, lokalna progresja stron `1, 2, 3, …`, zachowana kolejność providera. Metadane `current_page`, `per_page`, `last_page`, `from`, `to` są wyłącznie diagnostyczne.
  - Budżety compatibility (`TELEMATICS_PROVIDER_COMPAT_*`, `docs/02_infrastructure.md`) są walidowane raz przed pierwszym requestem i sprawdzane przed każdym kolejnym; błędna konfiguracja kończy run fail-closed kodem `PAGINATION_COMPAT_CONFIG_INVALID` bez żadnego żądania do providera.
  - **Parametr joba `trips_pagination_mode`** (`strict_meta` | `data_invariants_v1`) jest teraz konsumowany przez `sync_trips_and_speeding`: brak parametru albo `null` → `strict_meta`, każda inna wartość spoza kontraktu → run `FAILED` przed utworzeniem klienta providera. Dispatcher emituje go tylko dla compatibility fire, a runner C11 zawsze; obie ścieżki weryfikują wcześniej tryb w control-plane, więc control-plane pozostaje właściwym przełącznikiem. Tryb trafia **wyłącznie** do ścieżki `/trips`; `/vehicles`, `/drivers`, `/vehicles/events`, `/alerts/notifications` i `/fuel/*` pozostają strict.
  - C7 **nie** mutuje coverage — `W` przesuwają wyłącznie zrecenzowane finalizery C6 i C11 — nie zmienia granicy zapisu (fetch-before-connect, jeden commit), `ON CONFLICT` ani semantyki `overwrite_existing`. `strict_meta` pozostaje bez zmian i nie istnieje dynamiczny fallback w żadną stronę.
  - Testy: `ops/tests_manual/test_telematics_trips_pagination_compat.py` (regresje strict, sukcesy i awarie compatibility, propagacja trybu, generowane sekwencje T35; brak sieci, brak DB, brak sekretów). Wartownik eligibility okna (`PAGINATION_COMPAT_WINDOW_INELIGIBLE`), pre-commit assertion duplikatów i szersza integracja pozostają w C8. Szczegóły zakresu i własności: `docs/14_…` §3/C7 i §3/C8.

**Schemat bazy klienta (Workflow A) — DDL stosowany przez onboarding i migracje istniejących klientów:**

- `db/client_business/020_client_trips_final_schema.sql` — bazowy ordered schema `public.client_trips` oraz compatibility table `public.client_speeding_notifications`; dla istniejących baz przebudowuje `client_trips` przez `client_trips_rebuilt_020`, waliduje row count, robi swap w transakcji i zostawia `client_trips_legacy_backup_020` do rollbacku
- `db/client_business/021_add_trip_mode_to_client_trips.sql` — finalny rebuild dodający `trip_mode` przed `start_timestamp`; zostawia `client_trips_legacy_backup_021` do rollbacku
- `db/client_business/023_alpha_gps_baza_log_workflow_b.sql` — Workflow B target `telematics_reports."Alpha_GPS_Baza_LOG"` dla emailowego Alpha GPS XLSM LOG importu
- `db/client_business/024_alpha00001_client_trips_dysponent_id.sql` — nullable `public.client_trips."Dysponent_ID"` oraz indeks częściowy wartości uzupełnionych; migracja jest bezpieczna globalnie, job używa wyłącznie `client_code=ALPHA00001`
- `db/client_business/025_alpha00001_dysponent_id_batch_indexes.sql` — częściowy indeks `(start_timestamp, client_id, provider_trip_id)` dla pustych `"Dysponent_ID"`, używany przez batch loop ALPHA00001 enrichment
- `db/client_business/026_add_driver_restrictions_to_client_trips.sql` — nullable `public.client_trips."Driver_Restrictions"`; PostgreSQL fizycznie dopisuje kolumnę na końcu, a wymagany porządek jest utrzymywany logicznie w job output/INSERT (`identification_tag_id`, `"Driver_Restrictions"`, `trip_mode`)
- `db/client_business/027_eco_driving_schema.sql` — tabele wsparcia Eco Driving: `public.eco_trip_assignments`, `public.eco_driver_weekly_stats`, `public.eco_driver_monthly_stats`
- `db/client_business/028_eco_driving_periods_and_driver_chart.sql` — korekta Eco Driving: `public.eco_drivers_id_chart`, business-facing view `public."Eco_Drivers_ID_Chart"`, audit wykluczeń prywatnych tripów oraz metadata dla cumulative month-to-date weekly ranking snapshots
- `db/client_business/029_eco_driving_nullable_scores.sql` — pozwala zapisywać `NULL` w punktach i `eco_driving_score_total`, żeby okresy z zerowym dystansem nie dostawały fałszywego wyniku `0`
- `db/client_business/030_eco_driving_trend_views.sql` — query views `public.eco_driver_weekly_trends_view` i `public.eco_driver_monthly_trends_view` dla progress/trend reporting over persisted Eco Driving stats
- `db/client_business/031_eco_driving_validation_fields.sql` — dodaje do weekly/monthly Eco Driving stats pola strat punktowych względem maksimum, `top_1_validation`, `top_2_validation`, `ecodriving_rating_type` oraz odświeża trend views o te pola
- `db/client_business/032_eco_driving_weekly_email_notifications.sql` — dodaje `public.eco_driving_weekly_email_send_log` dla audit/idempotency weekly email notifications
- `db/client_business/033_eco_driving_weekly_email_send_log_grants.sql` — dodaje send-log audit columns dla qualification/ranking template routing i granty `SELECT, INSERT, UPDATE` dla standardowego client DB usera
- `db/client_business/034_eco_driving_rating_type_share_percent.sql` — dodaje `ecodriving_rating_type_share_percent numeric(7,2)` do weekly/monthly stats i appenduje to pole do trend views
- `db/client_business/035_eco_driving_monthly_email_notifications.sql` — dodaje `public.eco_driving_monthly_email_send_log` dla audit/idempotency monthly email notifications oraz granty `SELECT, INSERT, UPDATE` mirrorowane ze standardowej tabeli client-business
- `db/client_business/036_eco_driving_round_per_100km_stats.sql` — backfilluje weekly/monthly Eco Driving `*_events_per_100km` do zaokrąglonych wartości całkowitych; typy kolumn pozostają `NUMERIC`
- `db/client_business/037_eco_driving_score_from_rounded_per_100km.sql` — backfilluje weekly/monthly Eco Driving punkty, `*_maxpoints_subtract`, `eco_driving_score_total`, `top_1_validation` / `top_2_validation`, `ecodriving_rating_type` i `ecodriving_rating_type_share_percent` na podstawie zaokrąglonych `*_events_per_100km`
- `db/client_business/039_eco_person_driving_schema.sql` — isolated Eco Driving Person objects: `eco_person_people`, `eco_person_driver_mappings`, `eco_person_trip_assignments`, weekly/monthly stats, weekly/monthly send logs, and person trend/admin views. It does not alter ALPHA00001 Eco Driving tables.
- `db/client_business/040_eco_person_runtime_privileges.sql` — least-privilege runtime grants for `eco_person_*` jobs, including direct `SELECT` on person views, stats-table `DELETE` for recalculation, and function execution grants mirrored from standard client-business grantees.
- `db/client_business/041_eco_person_sent_archive_state.sql` — additive MIME preservation and Sent-folder archive status columns for `eco_person_*_email_send_log`; enables archive-only retry without SMTP resend after a successful delivery.
- `db/client_business/043_eco_person_physical_person_identity.sql` — fail-closed textual source-identity and physical-person grouping redesign for intentionally empty Eco Person tables.
- `db/client_business/044_eco_email_fail_closed_idempotency.sql` — schema-aware, conflict-gated normal-send identity without template/rating, ALPHA reserve-before-SMTP columns, separate test/forced scopes, and preserved BRAVO pending reservations. Driver/person weekly/monthly tables are migrated independently when present; missing model or monthly tables are `not_applicable`, an all-absent Eco schema is a noticed no-op, and partial/application reruns converge safely. Conflict diagnostics identify model, table, stable subject key, period, statuses, scopes, template types, and row IDs without recipient content; any conflict aborts the transaction before stable-key/index replacement.
- `db/client_business/045_environment_identity_promotion_primitive.sql` — installs the versioned SECURITY DEFINER client-marker transition used only by the environment-promotion CLI, revokes direct marker UPDATE from discovered/explicit runtime roles, and preserves every UUID and non-environment marker field. It creates no schedule and touches no business rows.
- `db/client_business/012_client_vehicle_daily_fuel.sql` — `public.client_vehicle_daily_fuel`
- `db/client_business/013_client_vehicle_driver_daily_fuel.sql` — `public.client_vehicle_driver_daily_fuel`
- `db/client_business/014_add_record_id_and_synced_at.sql` — nadal dodaje `record_id` / `synced_at` / `sync_run_id` do tabel agregacji dziennych; dla nowego finalnego `client_trips` jest no-op
- `db/client_business/022_alpha_gps_baza_log.sql` — deprecated/emergency direct-source import target `telematics_reports."Alpha_GPS_Baza_LOG"` oraz historia `telematics_reports.alpha_gps_baza_log_import_runs` dla starego joba `jobs.alpha.import_gps_baza_log_xlsm`

Finalna logiczna kolejność kolumn `client_trips` w outputach joba:

1. `client_id`
2. `client_code`
3. `provider_trip_id`
4. `vehicle_id`
5. `registration`
6. `vehicle_name`
7. `vehicle_description`
8. `chassis_number`
9. `driver_name`
10. `driver_surname`
11. `driver_tag_description`
12. `identification_tag_id`
13. `Driver_Restrictions`
14. `trip_mode`
15. `start_timestamp`
16. `start_location`
17. `start_latitude`
18. `start_longitude`
19. `start_geofence_name`
20. `start_odometer_value`
21. `end_timestamp`
22. `end_location`
23. `end_latitude`
24. `end_longitude`
25. `end_geofence_name`
26. `end_odometer_value`
27. `trip_duration_seconds`
28. `trip_distance_meters`
29. `high_rpm_events_count`
30. `overrev_events_count`
31. `harsh_braking_events`
32. `harsh_acceleration_events`
33. `harsh_turning_events`
34. `idle_events`
35. `idle_time_seconds`
36. `speeding_140_160_count`
37. `speeding_160_170_count`
38. `speeding_170_plus_count`
39. `record_id`
40. `synced_at`
41. `sync_run_id`

`Driver_Restrictions` is populated from the bulk `GET /drivers` field `license_driver_restrictions`. The sync job matches trips to driver inventory by provider `driver_id` first, then by `identification_tag_id` if the driver inventory payload exposes one of the supported tag fields, and finally by exact normalized driver first/last name. Empty, missing, or unmatched values remain `NULL`.

Finalny `client_trips` nie zawiera: `driver_id`, `terminal_id`, `terminal_serial`, legacy `speeding_bucket_*_events`, `speeding_buckets_*`, `fuel_consumed_liters`, `avg_fuel_l_per_100km`.

### `jobs.api.telematics.backfill_trips_insert_only` — Workflow A maintenance

Manual-only repair path for bounded historical gaps in `client_trips`. The job is intentionally absent from the dispatcher registry and does not change scheduled `trips_sync` behavior.

Behavior:

- requires explicit `client_id`, matching `client_code`, `window_start_ts`, `window_end_ts`, and `insert_only=true`;
- accepts only explicit UTC timestamps and intervals up to 31 days;
- defaults to `dry_run=true`;
- fetches `/trips` with the same chunk builder and parsing/mapping helpers as normal trip ingestion, plus `/vehicles` and `/drivers` for metadata;
- pages `/trips` in the client's configured `trips_pagination_mode`, read from the same `client_account` row the scheduled job reads. Until 2026-08-10 the mode was not passed at all, so `normalize_trips_pagination_mode(None)` selected `strict_meta` for every client: a recovery for a client configured as `data_invariants_v1` aborted fail-closed with `PAGINATION_MISMATCH` on the first sub-window, after the whole provider fetch had been paid for. Nothing was ever written by such a run, and the mode is now recorded in the preflight evidence;
- never calls `/vehicles/events`;
- applies the global Client Trips admission rule: a provider trip with `trip_distance > 2 000 000` m is rejected at parse time (reason `distance_over_2000km`, counted in the preflight), and its single `client_trips` INSERT is issued through `client_trips_admission.execute_client_trips_insert`, which re-applies the cap to the bound `trip_distance_meters` and suppresses the statement entirely if nothing admissible remains;
- deduplicates fetched provider trip IDs, reports rejected rows, date/registration distribution, exact existing keys, and temporal overlap with existing trips;
- optionally restricts candidates to an explicit `provider_trip_ids` allowlist after provider parsing, while retaining the fleet-window provider fetch;
- inserts with `ON CONFLICT (client_id, provider_trip_id) DO NOTHING`; existing rows and all established counters/assignment/private-trip fields remain unchanged;
- initializes event-derived counters to zero only for newly inserted rows, pending their dedicated event/report migration;
- requires `expected_insert_count` from a reviewed dry-run when `dry_run=false`; a changed preflight count aborts the real run.

Parameters:

- required: `client_id`, `client_code`, `window_start_ts`, `window_end_ts`, `insert_only=true`;
- optional: `dry_run` (default `true`), `chunk_days` (default `2`, maximum `5`), `provider_trip_ids` (non-empty unique list of positive provider trip IDs);
- real run only: `expected_insert_count` (non-negative integer equal to the current preflight `rows_would_insert`).

The dry-run opens a read-only client DB transaction and performs no `INSERT` or commit. Its preflight includes `candidate_provider_trip_ids` and the count removed by an optional allowlist. A real run remains idempotent because conflicts are skipped rather than updated.

### `jobs.reports.postprocess.job_alpha00001_driver_chart_exact_import` — manual exact driver chart import

This manual-only ALPHA00001 repair job imports authoritative CSV roster rows into `public.eco_drivers_id_chart`. It does not infer identity from assignments, registrations, trips, or provider data; it does not update trips, counters, stats, or scoring. The job is hard-limited to `client_code=ALPHA00001` and defaults to `dry_run=true`.

Required CSV columns are `driver_id`, `driver_name`, `email`, `ranking_included`, and `is_active`. Optional `notes`, `source`, `effective_from`, and `effective_to` values are merged into `metadata_json`. Boolean values must be explicit `true` or `false`; dates use `YYYY-MM-DD`. Blank email is accepted but reported because weekly/monthly notification jobs will skip that driver. The job stores `is_active` exactly as supplied; current Eco Driving aggregation and notification selection do not filter chart rows by `is_active`.

Parameters:

- required: `client_code`, `input_path`, and non-empty unique `driver_ids`;
- optional: `dry_run` (default `true`), `allow_updates` (default `false`), `allow_extra_ids` (default `false`), and `allow_identical_duplicates` (default `false`);
- real run only: `expected_insert_count` and `expected_update_count`, both equal to the reviewed current dry-run counts.

Rows outside `driver_ids` are reported and skipped unless `allow_extra_ids=true`. Existing rows are left unchanged unless `allow_updates=true`; protected rows report the fields that differ. Conflicting or unapproved duplicate IDs are rejected. Real runs reject any roster containing invalid rows, lock existing scoped chart rows, use `ON CONFLICT DO NOTHING` for inserts, and roll back if actual counts differ from the preflight. A dry-run after a successful import predicts zero changes when the roster and chart are unchanged.

Template: `scripts/templates/alpha00001_eco_driver_chart_roster.template.csv`.

### `jobs.ecodriving.job_eco_driving_aggregate` — Workflow A Eco Driving aggregation

This job implements the Eco Driving postprocess layer over already imported `public.client_trips` rows. It writes assignment audit rows, cumulative month-to-date weekly stats, full calendar-month stats, and ranking metadata into the existing Eco Driving tables.

Runner and scheduler registration:

- manual runner module: `jobs.ecodriving.job_eco_driving_aggregate`;
- stable job/logical name in the job payload: `eco_driving_aggregate`;
- dispatcher dataset names registered in `jobs.api.telematics.registry` and platform migration `032_workflow_a_eco_driving_registry.sql`:
  - `eco_driving_weekly_snapshot` -> mode `weekly_cumulative_snapshot`, default `include_weekly=true`, `include_monthly=false`;
  - `eco_driving_month_end_weekly_snapshot` -> mode `final_month_weekly_snapshot`, default `include_weekly=true`, `include_monthly=false`;
  - `eco_driving_monthly_aggregation` -> mode `monthly_full_aggregation`, default `include_weekly=false`, `include_monthly=true`.

The dispatcher still uses the existing `workflow_a_control.client_dataset_schedule` mechanism. No second scheduler is introduced. New onboarding and migration `032_*` create disabled schedule rows; operators enable the desired rows per client.

Source table and columns:

- source table: `public.client_trips`
- identity/window columns: `client_id`, `client_code`, `provider_trip_id`, `record_id`, `start_timestamp`, `end_timestamp`
- assignment/private columns: `"Driver_Restrictions"`, `"Dysponent_ID"`, `driver_tag_description`
- metrics: `trip_distance_meters`, `overrev_events_count`, `harsh_braking_events`, `harsh_acceleration_events`, `harsh_turning_events`, `idle_events`, `speeding_140_160_count`, `speeding_160_170_count`, `speeding_170_plus_count`

Assignment priority:

1. If `client_trips."Driver_Restrictions"` is non-empty, assign the trip to that value with `assignment_source='DRIVER_RESTRICTIONS'`.
2. Else if `client_trips."Dysponent_ID"` is non-empty, assign the trip to that value with `assignment_source='DYSPONENT_ID'`.
3. Else keep an audit row with `assignment_source='SKIPPED_NO_ID'`, `assigned_id=NULL`, `aggregation_included=false`, and `exclusion_reason=NULL`; skipped trips are excluded from assigned-driver aggregation. `exclusion_reason` stays `NULL` for this case because the schema constraint currently reserves it for private-trip exclusion values.

Private-trip exclusion:

- if `client_trips.driver_tag_description` contains `pryw` case-insensitively, the postprocess still keeps the assignment audit row but sets `is_private_trip=true`, `aggregation_included=false`, and `exclusion_reason='PRIVATE_DRIVER_TAG'`;
- private rows do not contribute to weekly/monthly stats or rankings.

Driver chart naming:

- the physical table is `public.eco_drivers_id_chart`, following the repository's snake_case table convention;
- `public."Eco_Drivers_ID_Chart"` is a compatibility view for the business-facing name;
- rows are keyed by `(client_id, driver_id)`, where `driver_id` matches `eco_trip_assignments.assigned_id` and stats `assigned_id`;
- `ranking_included=true` participates in the main ranking; `false` participates in the non-ranking/internal group.
- the chart is a hard aggregation prerequisite: zero usable rows for the selected client fails with `DRIVER_CHART_NOT_LOADED` before assignment or snapshot persistence;
- after source aggregation, a non-empty source population with zero chart matches fails with `DRIVER_CHART_NO_SOURCE_MATCHES` before any existing snapshot is deleted or replaced;
- partially matched populations remain supported. The result reports `driver_chart_rows`, `source_driver_rows`, `matched_driver_rows`, `unmatched_driver_rows`, and `unmatched_driver_percentage`; unmatched rows remain `UNKNOWN_DRIVER`.

Tables:

- `public.eco_trip_assignments` — one audit row per source trip, keyed by `(client_id, provider_trip_id)` to match `client_trips`; stores raw `Driver_Restrictions` / `Dysponent_ID`, `driver_tag_description`, assignment/exclusion source, Warsaw period dates, trip distance, and event count snapshots.
- `public.eco_driver_weekly_stats` — kept as the weekly stats table for compatibility, but `028_*` adds `period_start_date`, `period_end_date`, `month_start_date`, `period_sequence_in_month`, `period_label`, `is_partial_period`, and ranking metadata. Weekly rows are cumulative month-to-date snapshots: `period_start_date` is always the first day of the month, while `period_end_date` is the exclusive SQL/reporting boundary. Business-facing display subtracts one day from `period_end_date` (for example, `2026-04-01` to `2026-05-01` displays as `01 - 30.04.2026`).
- `public.eco_driver_monthly_stats` — monthly materialized stats table rather than a view, matching the repository pattern of persisted aggregate tables such as `client_vehicle_daily_fuel`; monthly periods are full calendar months (`month_start_date` inclusive, `month_end_date` exclusive) and include ranking metadata.

> **Warning — do not sum weekly snapshots into monthly totals.** `eco_driver_weekly_stats` rows are cumulative month-to-date snapshots, not isolated week-only slices. W2 already contains W1, W3 already contains W1+W2, and the final weekly snapshot already contains every eligible month-to-date trip. The final weekly snapshot's raw totals (`total_distance_meters`, `total_kilometers`, event counts) match the corresponding `eco_driver_monthly_stats` row over the same eligible trip set, while a naive sum of all weekly snapshots would multi-count the same trips. Use `eco_driver_monthly_stats` (or the `eco_driver_monthly_trends_view`) whenever monthly totals are required.

Period and aggregation rules:

- all business period bounds use `Europe/Warsaw` through the shared timezone helper; intervals are half-open (`period_start` inclusive, `period_end` exclusive);
- weekly ranking reports are cumulative month-to-date snapshots: `W1 = month_start` to the first weekly boundary, `W2 = month_start` to the second weekly boundary, `Wn = month_start` to the nth weekly boundary, and final `W = month_start` to `next_month_start`;
- `is_partial_period` describes the incremental reporting segment that ends at that boundary: first/final segments can be partial when the calendar month does not align with Monday-to-Monday cycles;
- monthly stats cover the full calendar month (`month_start_date` inclusive, `month_end_date` exclusive) and are calculated independently from source assignments, not by summing weekly rows; business-facing display uses `month_end_date - 1 day`;
- a trip contributes to stats only when `assigned_id IS NOT NULL`, `aggregation_included=true`, and `is_private_trip=false`;
- `NULL` event counts are treated as zero;
- `total_kilometers = total_distance_meters / 1000`;
- each computed rate starts as `event_sum / total_kilometers * 100`; the raw coefficient is rounded to a whole-number value with `ROUND_HALF_UP` semantics, that rounded value is persisted in weekly/monthly `*_events_per_100km`, and the same rounded value is used for scoring bucket selection; when `total_kilometers=0`, rates, points, max-points subtraction fields, validation labels, and `eco_driving_score_total` / `ecodriving_rating_type` are `NULL`;
- `qualification_status`: `QUALIFIED` for `total_kilometers >= 100`, `LOW_DISTANCE` for `0 < total_kilometers < 100`, `NO_DISTANCE` for zero distance;
- `calculation_status`: `OK` for distance > 0, `NO_DISTANCE` for distance = 0, `ERROR` is reserved for unexpected failures.

Eco Driving scoring rules are implemented in `jobs.ecodriving.eco_scoring` as deterministic Python configuration, not DB-managed operator configuration. The helper scores **rounded per-100km event coefficients**, not raw event counts or precise fractional rates; those coefficients can come from either cumulative month-to-date weekly snapshots or full calendar-month periods. Missing or `NULL` required rates do not produce a total score, and negative rates are rejected as invalid aggregate inputs. The total score range is `100` maximum and `-100` minimum. Migration `029_eco_driving_nullable_scores.sql` relaxes the stats point/score columns so zero-distance periods can persist `NULL` scores. Migration `031_eco_driving_validation_fields.sql` adds snake_case reporting fields for point loss versus per-metric maximum (`*_maxpoints_subtract = LEAST(metric_points - metric_max_points, 0)` in Python-equivalent logic), top two Polish validation labels by largest negative loss, and `ecodriving_rating_type` (`bezpieczny` for score `>=85`, `akceptowalny` for `>=40`, `niebezpieczny` below `40`, `NULL` for `NULL` score). The mixed-case requested spelling `EcoDriving_rating_type` is intentionally not used because repository DB convention is unquoted snake_case identifiers. Migration `034_eco_driving_rating_type_share_percent.sql` adds `ecodriving_rating_type_share_percent numeric(7,2)`: for each weekly/monthly period, it is `count(rows with the same ecodriving_rating_type, ranking_included=true, qualification_status='QUALIFIED') * 100.0 / count(rows with ranking_included=true, qualification_status='QUALIFIED')`. Rows with `ranking_included=false`, `qualification_status!='QUALIFIED'`, missing `ecodriving_rating_type`, or a zero denominator store `NULL`. Migration `036_eco_driving_round_per_100km_stats.sql` backfills existing persisted weekly/monthly `*_events_per_100km` values with `ROUND(column)` and intentionally leaves column types as `NUMERIC`; migration `037_eco_driving_score_from_rounded_per_100km.sql` recalculates existing points, max-points subtraction fields, score totals, validation labels, rating type, and rating-type share percent from those rounded values.

Ranking:

- **qualification is a hard prerequisite for ranking eligibility**: only `qualification_status='QUALIFIED'` rows belong to a ranking population. `LOW_DISTANCE` and `NO_DISTANCE` rows get `ranking_group=NULL`, `ranking_position=NULL` and `ranking_total_participants=NULL`, and are excluded from every INCLUDED/EXCLUDED/UNKNOWN_DRIVER population count. `ranking_group=NULL` means "outside every ranking population" and is deliberately distinct from `UNKNOWN_DRIVER`;
- qualification takes precedence over chart membership: a `LOW_DISTANCE` row whose chart entry says `ranking_included=true` still receives no ranking group. The flag keeps reflecting chart configuration and is preserved on the row, but has no ranking effect until the row becomes `QUALIFIED`. Because weekly snapshots are cumulative month-to-date, the same driver/person legitimately transitions from unranked in `W1` to normally ranked in a later snapshot once distance crosses 100 km;
- stats rows join `eco_drivers_id_chart` by `(client_id, assigned_id = driver_id)`;
- for `QUALIFIED` rows: chart row with `ranking_included=true` gives `ranking_group='INCLUDED'`;
- for `QUALIFIED` rows: chart row with `ranking_included=false` gives `ranking_group='EXCLUDED'`;
- `QUALIFIED` with a missing chart row gives `ranking_group='UNKNOWN_DRIVER'`, `ranking_position=NULL`, `ranking_total_participants=NULL`;
- INCLUDED and EXCLUDED are ranked separately, and each `ranking_total_participants` counts only the `QUALIFIED` rows of that group in that period;
- migration `046_eco_ranking_qualified_only.sql` makes `ranking_group` nullable on all four weekly/monthly stats tables and adds a `NOT VALID` coherence constraint (`ranking_group IS NULL` implies both ranking coordinates are `NULL`). It performs **no data backfill**: rows written before this contract keep their previous ranking values until an operator re-runs the aggregation job with `recalculate=true` for the affected periods;
- the aggregation job result reports non-qualified rows separately as `not_ranked_rows`, next to `included_ranking_rows` / `excluded_ranking_rows` / `unknown_driver_rows`;
- rank order is deterministic row-number semantics: higher `eco_driving_score_total` first (`NULLS LAST`), then higher `total_kilometers`, then `assigned_id ASC`;
- `top_1_validation` / `top_2_validation` consider only negative `*_maxpoints_subtract` values; ties use this metric order: overrev, harsh braking, harsh acceleration, harsh turning, idle, speeding 140-160, speeding 160-170, speeding 170+.

Trend views:

- `public.eco_driver_weekly_trends_view` is based on `eco_driver_weekly_stats` and compares cumulative month-to-date weekly snapshots for the same `(client_id, assigned_id)`, ordered by `month_start_date, period_end_date`;
- weekly `previous_snapshot_score` is the previous cumulative snapshot score, not an isolated previous-week slice;
- weekly `score_delta_abs` is the change in cumulative score between snapshots; `score_delta_pct` is `score_delta_abs / abs(previous_snapshot_score) * 100` and is `NULL` when the previous score is `NULL` or zero;
- weekly `kilometers_delta_abs` is the distance added between two cumulative snapshots. At a new month, `W1` resets to the new month-to-date window, so the delta versus the previous month's final snapshot can be negative; this reset is expected;
- weekly rolling averages (`rolling_4_snapshot_avg_score`, `rolling_8_snapshot_avg_score`) average the current and previous snapshots for the same assigned ID and ignore `NULL` scores according to SQL `AVG`;
- `public.eco_driver_monthly_trends_view` is based on independently calculated `eco_driver_monthly_stats` rows and exposes previous-month score/kilometer deltas plus rolling 3-month and 6-month score averages;
- `ranking_position_delta = previous_ranking_position - ranking_position`; positive means the driver moved up in the ranking, negative means the driver moved down;
- `UNKNOWN_DRIVER` rows remain visible in both views. Ranking fields and ranking deltas stay `NULL` when the source stats row has no ranking position;
- the views join `eco_drivers_id_chart` for current display fields (`driver_name`, `email`) but use the historical `ranking_included` and `ranking_group` stored on each stats row;
- after `031_eco_driving_validation_fields.sql`, both trend views also expose the persisted max-points subtraction fields, `top_1_validation`, `top_2_validation`, and `ecodriving_rating_type`; after `034_eco_driving_rating_type_share_percent.sql`, `ecodriving_rating_type_share_percent` is appended at the end of both view outputs.

Parameters:

- `client_id` — required
- `month` — optional `YYYY-MM`; computes all cumulative month-to-date weekly reporting points in that month and, by default, the monthly stats row set
- `period_start_date` + `period_end_date` — optional `YYYY-MM-DD` pair; computes exactly one matching cumulative weekly report; `period_start_date` must be the first day of the month, and monthly aggregation is skipped unless `include_monthly=true` is explicitly passed
- `mode` / `resolver` — optional automatic resolver; accepted values:
  - `previous_completed_weekly_snapshot` or `weekly_cumulative_snapshot` resolves the latest completed cumulative weekly snapshot in Europe/Warsaw business time;
  - `final_month_weekly_snapshot` resolves previous-month start to current-month start for the final weekly snapshot after month end;
  - `previous_completed_monthly_aggregation` or `monthly_full_aggregation` resolves the previous completed full calendar month;
- `assigned_id` — optional filter that recomputes/upserts only one Eco Driving ID
- `include_weekly` — bool, default `true` for selected-month and weekly resolver modes; default `false` for monthly resolver mode
- `include_monthly` — bool, default `true` for selected-month and monthly resolver modes; default `false` for explicit-period and weekly resolver modes
- `recalculate` — bool, default `false`; when true, scoped existing stats rows are deleted before recomputed rows are upserted, which removes stale stats for drivers that no longer have included trips
- `dry_run` — bool, default `false`; executes the same SQL in a transaction and rolls it back
- `batch_size` — int, default `5000`, used for assignment upsert batches
- `max_batches` — optional safety cap for assignment batches

Automatic resolver behavior:

- `weekly_cumulative_snapshot`: uses Europe/Warsaw local time, picks the last completed reporting boundary, and stores `period_start_date=month_start`, `period_end_date=<boundary>`. If the boundary is the first day of a month, the resolved period is the final snapshot for the previous month.
- `final_month_weekly_snapshot`: resolves `previous_month_start -> current_month_start`.
- `monthly_full_aggregation`: resolves `previous_month_start -> current_month_start` and calculates monthly stats independently from source assignments, not from weekly rows.
- resolver modes never select a future period; dispatcher history prevents repeated execution of the same scheduled fire, while the job remains idempotent on reruns.

Manual examples:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate \
  '{"client_id":"<CLIENT_ID>","month":"2026-05"}'

PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate \
  '{"client_id":"<CLIENT_ID>","month":"2026-05","include_weekly":true,"include_monthly":true,"recalculate":true}'

PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate \
  '{"client_id":"<CLIENT_ID>","month":"2026-05","include_weekly":false,"include_monthly":true,"recalculate":true}'

PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate \
  '{"client_id":"<CLIENT_ID>","period_start_date":"2026-05-01","period_end_date":"2026-05-11","include_monthly":false}'

PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate \
  '{"client_id":"<CLIENT_ID>","mode":"previous_completed_weekly_snapshot","include_weekly":true,"include_monthly":false}'

PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate \
  '{"client_id":"<CLIENT_ID>","month":"2026-05","assigned_id":"DRIVER123","recalculate":true}'

PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate \
  '{"client_id":"<CLIENT_ID>","month":"2026-05","dry_run":true}'
```

### `jobs.ecodriving.job_eco_driving_weekly_email_notifications` — Workflow A Eco Driving weekly email notifications

This job sends weekly HTML notification emails to individual drivers from already persisted `public.eco_driver_weekly_stats` rows. It does not calculate Eco Driving scores and does not read monthly stats. The job joins `public.eco_driver_weekly_stats` to `public.eco_drivers_id_chart` on `(client_id, assigned_id = driver_id)` and uses the first supported email column found in the chart, currently `email`. If none of `email`, `driver_email`, `notification_email`, or `email_address` exists, the run fails with a clear schema error.

Templates live in `assets/email_templates/ecodriving/weekly/`. Startup/dry-run validates the full required inventory before reading the database:

- ranked templates: `Tygodniowe - Bezpieczni.html`, `Tygodniowe - Akceptowalni.html`, `Tygodniowe - Niebezpieczni.html`;
- non-qualified template: `Tygodniowe - Niezakwalifikowani.html`;
- no-ranking templates: `Tygodniowe - Bezpieczni - norank.html`, `Tygodniowe - Akceptowalni - norank.html`, `Tygodniowe - Niebezpieczni - norank.html`.

Template selection priority:

1. `qualification_status IN ('LOW_DISTANCE', 'NO_DISTANCE')` always uses `Tygodniowe - Niezakwalifikowani.html`;
2. otherwise the persisted weekly snapshot value `eco_driver_weekly_stats.ranking_included=false` uses the matching `- norank.html` template for `ecodriving_rating_type`;
3. persisted snapshot `ranking_included=true` may use the normal ranked template;
4. persisted snapshot `ranking_included=NULL` is classified as `INVALID_RANKING_SNAPSHOT` and is never rendered or sent as ranked. The current chart value is selected only as `current_ranking_included` diagnostic metadata.

No-ranking templates do not require `ranking_position`; if a no-ranking template includes ranking placeholders and the selected row has no safe ranking values, dry-run/rendering fails clearly instead of sending unresolved content. A ranked candidate requires positive-integer `ranking_position` and `ranking_total_participants`, a non-empty `ecodriving_rating_type`, and numeric `ecodriving_rating_type_share_percent` in the inclusive `0..100` range. Zero percent is valid. Invalid ranked candidates write an existing `status='failed'` per-candidate audit row with `metadata_json.classification='INVALID_RANKING_SNAPSHOT'`, field issues, and no SMTP call. `LOW_DISTANCE` and `NO_DISTANCE` always select the non-qualified template and are never validated as ranked candidates. Those rows carry `ranking_group=NULL` and no ranking coordinates (see *Ranking* above), so the ranking position and participant total shown to a `QUALIFIED` recipient describe a QUALIFIED-only population. The aggregation job still does not rewrite `ecodriving_rating_type`: a non-qualified row keeps its calculated score and rating, it simply does not compete.

The renderer escapes placeholder values, formats visible dates in Polish format, keeps missing validation labels empty instead of `None`, and keeps internal period storage half-open: `week_start_date` renders as `DD`, `week_end_date` renders as `period_end_date - 1 day` in `DD.MM.YYYY`, and `period_start_date_display` / `period_end_date_display` render as `DD.MM.YYYY`. The legacy template expression `{{ (ranking_position / ranking_total_participants) * 100 }}%` is still recognized for backward compatibility, but it now renders `ecodriving_rating_type_share_percent` from weekly stats, not a ranking percentile. Percent display uses a Polish decimal comma when needed, for example `73,5%`, and `NULL` renders empty.

The weekly templates keep generated visual components behind high-level placeholders: `{eco_score_bar_html}` renders a fixed-width Outlook-safe red/yellow/green segmented score bar with an arrow derived from `eco_driving_score_total`, and `{lost_points_tiles_html}` renders the two-column lost-points tile table with per-metric colors based on the persisted `*_maxpoints_subtract` values. These fragments are generated by `jobs.ecodriving.email_visuals`; the email job still only renders already-calculated values and does not own scoring rules.

Client-business send-log and safety contract:

- migrations `032_*`, `033_*`, and additive `044_eco_email_fail_closed_idempotency.sql` define the weekly log;
- `status='pending'` is committed before SMTP for normal sends and blocks concurrent or later normal attempts; a stale `pending` is not reclaimed and returns `STALE_PENDING_REQUIRES_RECONCILIATION` for manual investigation;
- `status='sent'` means **accepted by example.invalid SMTP for relay**, not confirmed mailbox delivery. The evidence kept for it is `smtp_message_id`, `sent_at`, `provider_response` (the relay's verbatim final reply where the library exposes it, otherwise the historical literal) and `metadata_json.smtp_acceptance` with the reply code, its text, the relay's queue identifier if it emits one — never a synthesised one — and `final_reply_observed`, which states whether the row quotes example.invalid or only records that `smtplib` returned without raising;
- after acceptance the EXACT transmitted MIME message is appended to the sender mailbox's Sent folder and verified by `Message-ID`, in all four mailers, when that sender's `{PREFIX}_IMAP_*` namespace is configured. The namespace belongs to the sender **mailbox**, not the period: BRAVO person weekly and monthly both resolve `BRAVO_ECO_WEEKLY_EMAIL_*`, both ALPHA mailers resolve `ECO_WEEKLY_EMAIL_*`, and other person clients resolve `ECO_PERSON_EMAIL_*`. Configuring that namespace means the IMAP **endpoint** only — `_IMAP_HOST`, `_IMAP_PORT`, `_IMAP_USE_SSL` — because the copy authenticates as the account that sent the mail: `_IMAP_USERNAME` / `_IMAP_PASSWORD` are optional overrides and default to the same prefix's `_SMTP_USERNAME` / `_SMTP_PASSWORD`, so one mailbox never holds two copies of its own password. Credentials resolvable from neither source, and a partially stated endpoint, are still fail-closed errors. The copy runs outside the SMTP failure path: a failed copy leaves `status='sent'` untouched, is counted and recorded (`sent_archive_status` on the person logs, `metadata_json.sent_folder_copy` on the driver logs) and NEVER authorizes a retransmission;
- an **ambiguous** SMTP result — the submission was attempted and remote acceptance cannot be excluded — keeps the row `pending` and marks it `metadata_json.smtp_submission_result='AMBIGUOUS'` with the submission phase and `requires_operator_reconciliation=true`. `status='failed'` is reserved for a submission the protocol PROVED was not accepted (connection/STARTTLS/authentication failure, `SMTPSenderRefused`, `SMTPRecipientsRefused`, or an `SMTPDataError` carrying a real 4xx/5xx reply code), which stays automatically retryable. An `SMTPDataError` whose `smtp_code` is outside 400-599 — `-1` for a reply `smtplib` could not parse, an unexpected 2xx/3xx, anything unrepresentable — proves nothing, because it can be raised after the whole message was transmitted, so it is AMBIGUOUS. Classification lives in `jobs.common.eco_smtp_submission`, the durable state in `jobs.common.eco_email_reconciliation`, and both are shared by all four Eco mailers;
- all four mailers submit on **one authenticated SMTP session per run** (`jobs.common.eco_smtp_submission.ReusableSmtpSession`, opened by `eco_email_transport.open_run_session` on the first message, never in `render_only`). The reason is measured, not assumed: example.invalid delays the `220` greeting of every new connection from this host by 12–25 s (a reverse-DNS wait on the relay side; the host's PTR delegation is unreachable), so one session per message cost 5–7 h per ~1000 messages while the message itself takes under a second. Nothing about the evidence changes: each message is still one `sendmail` with its own captured final reply, the DEFINITE/AMBIGUOUS rules above are unchanged, and the run summary adds `smtp_sessions_opened`, `smtp_session_messages`, `smtp_session_liveness_failures`, `smtp_session_envelope_retries` and `smtp_session_dropped_after_failure`. A reused session is proven alive with `NOOP` first; a dead one is replaced silently. The single retry a kept session may perform is narrow by construction: a *connection loss* (never a relay reply) while negotiating the envelope of a *reused* session, proven pre-`DATA` because the client's `data()` step was never entered (`SmtpSubmissionError.phase='ENVELOPE'`), is resubmitted once on a fresh session — the same failure on a fresh session, any refusal, and anything after `DATA` are raised exactly as before, and after any failure the session is discarded. `ECO_EMAIL_SMTP_SESSION_REUSE=false` restores one session per message without a release; `ECO_EMAIL_SMTP_SESSION_MAX_MESSAGES=N` reconnects after `N` messages (unset/`0` = until the relay ends the session). Proof: `ops/tests_manual/test_eco_email_smtp_session_reuse.py`;
- an unresolved ambiguous row is refused by `reserve_send()` with `AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION` under both `normal` and `forced` scopes — `force_resend` overrides an established `sent`, never an unknown outcome — and all four mailers check it in BOTH production modes (`normal_send` and `force_resend`) BEFORE the dashboard capability step, so a message that may not be sent causes no publication, no capability rotation, no expiry recovery and no dashboard ledger mutation. The reservation-time refusal stays as the concurrency backstop. `test` scope is exempt because it goes to the configured test mailbox, never the driver. The only exit is `ops/reconcile_eco_email_ambiguous_send.py`, where an operator attests `delivered` (the row becomes `sent`) or `not-delivered` (the row becomes `failed`, so the next normal run mails it exactly once);
- normal identity is `(client_id, assigned_id, report_type, period_start_date, period_end_date)` for `send_scope='normal'` and `status IN ('pending','sent')`; template, rating, ranking and rendered content are deliberately excluded;
- test and forced rows are outside normal uniqueness. A forced row stores its required reason, timestamp, and parent normal-send row where one exists.

All four Eco Driving email jobs share `jobs.ecodriving.email_safety`. Boundaries are local-midnight `Europe/Warsaw` values with inclusive start and exclusive end. A period closes exactly when the current aware Warsaw instant is greater than or equal to its exclusive end instant. Automatic selection validates only the requested report type, rejects malformed/open/future candidates, and deterministically chooses the latest eligible closed period; explicit periods pass the same policy. A sender also requires every persisted row in the candidate snapshot to have `updated_at` at or after the exclusive end instant; this proves a final post-boundary aggregation and prevents an early W4-like snapshot becoming eligible merely when midnight passes. Failure code: `SNAPSHOT_NOT_FINALIZED`. Typed period codes are `PERIOD_NOT_CLOSED`, `PERIOD_END_IN_FUTURE`, `INVALID_PERIOD_BOUNDARY`, and `NO_ELIGIBLE_CLOSED_PERIOD`.

`execution_mode` is optional and defaults to `render_only`, which validates, resolves and renders without SMTP, IMAP, or a normal reservation. `test_send` requires exactly one `test_recipient_email` and never consumes normal idempotency. `normal_send` is the only ordinary real-recipient mode. `force_resend` requires non-empty `force_resend_reason`, retains normal evidence, and cannot bypass period closure. `allow_unclosed_period_for_test=true` is accepted only with `render_only` or `test_send`. Legacy `dry_run=true` remains a safe render-only alias; `dry_run=false` without an explicit mode and conflicting legacy combinations fail closed.

Other parameters remain `client_id` (required), an optional exact date pair and its weekly aliases, `assigned_id`, positive `limit`, `template_dir`, `subject`, and `fail_fast`. The stats-selection `mode` parameter is not the email execution mode.

**Driver Eco Dashboard link — opt-in, and additionally gated per client.** All four mailing commands (`ecodriving` weekly/monthly, `ecodriving_person` weekly/monthly) accept one boolean option on the existing entry point:

```
ops/runner.py <eco_mailing_job_module> '<params_json>' --with-dashboard
```

Without it the run is **legacy-only**: no dashboard snapshot build, no publication request, no R2 write, no D1 capability or publication state, no capability generation or recovery, no dashboard link in the message, no dependency on publisher configuration or availability, and no change to whether the ordinary e-mail is sent. `DashboardLinkSettings.from_params` returns before it reads any `ECO_DASHBOARD_*` value, so a missing or broken publisher configuration cannot affect a default run. Configuration alone never enables the integration.

With it, dashboard-enabled sending **additionally** requires client-level rollout permission declared in `ops/eco_dashboard_mailing_rollout.json` (`jobs/ecodriving_dashboard/dashboard_rollout.py`). Neither condition alone is sufficient. Current production state: **BRAVO00016 enabled by explicit owner decision, ALPHA00001 disabled, unknown/new clients disabled**. A flag with a disabled client raises `ECO_DASHBOARD_MAILING_ROLLOUT_NOT_ENABLED` from `authorize_dashboard_mailing()`, which every mailer calls immediately after resolving the client account and before constructing the dashboard service — so before any snapshot build, publication, capability, send-log reservation or SMTP connection. It never falls back to a dashboard-less e-mail.

The option is accepted only on the four mailing modules; anywhere else, and any mistyped variant, is a runner usage error (exit 2) that executes nothing. The Workflow A dispatcher builds `[python, ops/runner.py, <module>, <params_json>]` and appends an option only for a (client, dataset) pair declared in `ops/eco_mailing_production_schedule.json`, resolving the dashboard state from the same rollout declaration a manual run is gated by; every other fire carries no option at all. Contract: `docs/28` §12.11; production execution contract and retry matrix: `docs/07_operations.md`.

Safe examples:

```bash
# Default render-only, latest eligible closed weekly period.
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py   jobs.ecodriving.job_eco_driving_weekly_email_notifications   '{"client_id":"<CLIENT_UUID>"}'

# Test-scope SMTP to one address only.
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py   jobs.ecodriving.job_eco_driving_weekly_email_notifications   '{"client_id":"<CLIENT_UUID>","execution_mode":"test_send","test_recipient_email":"test@example.com","limit":5}'

# Real recipients require explicit normal intent.
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py   jobs.ecodriving.job_eco_driving_weekly_email_notifications   '{"client_id":"<CLIENT_UUID>","execution_mode":"normal_send"}'

# Forced resend requires an operational reason.
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py   jobs.ecodriving.job_eco_driving_weekly_email_notifications   '{"client_id":"<CLIENT_UUID>","execution_mode":"force_resend","force_resend_reason":"<REVIEWED_REASON>","assigned_id":"<DRIVER_ID>"}'

# Dashboard-enabled send — permitted only for a client the rollout declaration enables.
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py   jobs.ecodriving.job_eco_driving_weekly_email_notifications   '{"client_id":"<CLIENT_UUID>","execution_mode":"normal_send"}' --with-dashboard
```

SMTP ENV remains documented in `docs/02_infrastructure.md` and `docs/06_security.md` under `ECO_WEEKLY_EMAIL_*`.

### `jobs.ecodriving.job_eco_driving_monthly_email_notifications` — Workflow A Eco Driving monthly email notifications

This job sends monthly HTML notification emails to individual drivers from already persisted `public.eco_driver_monthly_stats` rows. It is intentionally analogous to the weekly notification job: it does not calculate Eco Driving scores, joins `public.eco_driver_monthly_stats` to `public.eco_drivers_id_chart` on `(client_id, assigned_id = driver_id)`, uses the same supported email columns, renders static HTML templates, sends through the same `ECO_WEEKLY_EMAIL_*` SMTP mailbox/settings, and writes a client-DB send log.

Templates live in `assets/email_templates/ecodriving/monthly/`. The required inventory is seven files: ranked `Miesięczne - Bezpieczni.html`, `Miesięczne - Akceptowalni.html`, `Miesięczne - Niebezpieczni.html`; no-ranking `Miesięczne - Bezpieczni - norank.html`, `Miesięczne - Akceptowalni - norank.html`, `Miesięczne - Niebezpieczni - norank.html`; and `Miesięczne - Niezakwalifikowani.html`. Selection priority matches weekly and uses persisted `eco_driver_monthly_stats.ranking_included`, never the current chart value: `LOW_DISTANCE`/`NO_DISTANCE` uses the non-qualified template, snapshot `false` uses the matching `- norank` template, snapshot `true` may use ranked, and snapshot `NULL` becomes `INVALID_RANKING_SNAPSHOT`. Monthly ranked candidates use the same positive rank/participant, rating-type, and inclusive `0..100` percentage validation and are recorded as `failed` without SMTP when invalid.

The monthly templates use `{month_start_date}` / `{month_end_date}` for the visible half-open period display (`month_end_date - 1 day`). The job also accepts `{week_start_date}` / `{week_end_date}` aliases in custom monthly templates for compatibility, but new repository templates use month placeholders. `{eco_score_bar_html}` remains the shared total-score bar. `{area_score_axes_html}` renders the monthly “Wizualizacja punktacji według obszarów” section: one Outlook-safe table axis per Eco Driving scoring dimension, with threshold-range labels, lost-point labels, colored segments, and a `▲` marker placed in the segment matching the persisted `*_maxpoints_subtract` value. The visible `Ilość wykroczeń` header displays the rounded persisted `*_events_per_100km` value, and the marker is based on the persisted deduction calculated from that same rounded coefficient. Missing or malformed per-100km values render `brak danych na 100 km`; a missing subtract value renders no marker instead of crashing the run.

Client-business send-log behavior is the same fail-closed contract as weekly. Migration `044_eco_email_fail_closed_idempotency.sql` makes normal identity `(client_id, assigned_id, report_type, period_start_date, period_end_date)` for normal `pending`/`sent` rows and preserves separate test/forced scopes.

Parameters are analogous to weekly. The exact monthly boundary must be the first day of one month through the first day of the next month. Without explicit dates the shared policy chooses the latest eligible closed monthly period, never an open or future row. `execution_mode` defaults to `render_only`; real modes, force reason, and test-recipient rules are identical to weekly.

```bash
# Render only.
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py   jobs.ecodriving.job_eco_driving_monthly_email_notifications   '{"client_id":"<CLIENT_UUID>"}'

# One explicit test recipient.
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py   jobs.ecodriving.job_eco_driving_monthly_email_notifications   '{"client_id":"<CLIENT_UUID>","execution_mode":"test_send","test_recipient_email":"test@example.com","limit":5}'

# Explicit normal send of the latest eligible closed month.
PYTHONPATH="$PWD" .venv/bin/python ops/runner.py   jobs.ecodriving.job_eco_driving_monthly_email_notifications   '{"client_id":"<CLIENT_UUID>","execution_mode":"normal_send"}'
```

DDL aplikowane są **przez `scripts/onboard_workflow_a_client.py`** w trakcie onboardingu klienta; nie przez `ops/db_migrate.sh` (ten obsługuje tylko migracje platformowej bazy `logdb`).

### `jobs.ecodriving_person.*` - isolated Eco Driving Person workflow

This job family is separate from the existing ALPHA00001 Eco Driving implementation. For `BRAVO00016`, `eco_person_people.person_id TEXT` is a provider source identity corresponding to `public.client_trips.driver_name`, not an application UUID. `person_name` is the canonical human-readable physical driver. Several source identities may share one `person_name_group_key` and therefore one physical-person result. ALPHA00001 `eco_*` objects and assignment rules remain unchanged.

Implemented runner modules:

- `jobs.ecodriving_person.job_eco_driving_person_aggregate` - assignment audit, cumulative weekly snapshots, final month-end weekly snapshots and independent monthly aggregation;
- `jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications` - weekly person email notifications;
- `jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications` - monthly person email notifications;
- `jobs.ecodriving_person.job_eco_driving_person_mapping_import` - dry-run-first CSV import for person and driver-name mappings.

Dispatcher dataset names are `eco_person_driving_weekly_snapshot`, `eco_person_driving_month_end_weekly_snapshot`, `eco_person_driving_monthly_aggregation`, `eco_person_driving_weekly_email_notifications`, and `eco_person_driving_monthly_email_notifications`. Migration `046_workflow_a_eco_person_registry.sql` creates disabled schedule rows by default. Email datasets are not enabled automatically.

Client-business objects originate in `039_eco_person_driving_schema.sql`; migration `043_eco_person_physical_person_identity.sql` fail-closes unless every isolated Eco Person table is empty, then replaces the UUID-oriented identity contract. `eco_person_people` is authoritative for runtime matching. `eco_person_driver_mappings` remains only as an unused compatibility object and is not read or written by the BRAVO00016 importer or aggregation job.

Driver-name matching uses `normalize_person_source_identity(...)` and SQL `eco_person_normalize_source_identity(...)`: Unicode NFC, case-insensitive comparison, removal of all whitespace/punctuation/symbol/special characters, and retention of Unicode letters plus decimal digits. Polish characters remain distinct and are never transliterated (`Lukasz != Łukasz`, `Zolty != Żółty`). Empty normalized values are rejected. Physical names are NFC-normalized, trimmed and whitespace-collapsed for display; their case-insensitive group key does not remove Polish characters. Assignment outcomes are `PERSON_ID_MATCH`, `SKIPPED_NO_DRIVER_NAME`, `UNMAPPED_DRIVER_NAME`, or fail-closed `INVALID_AMBIGUOUS_MAPPING`.

Trip inclusion intentionally differs from ALPHA00001: once mapped, business, private, null-mode, unknown-mode and `driver_tag_description` containing `pryw` all contribute. Qualification, score calculation, rounded per-100km rates, rating classification, ranking, rating-type share and period semantics are copied from the existing Eco Driving scoring/aggregation helpers.

Aggregate parameters: `client_id` required; `month`, explicit period, or `mode`/`resolver`; optional `include_weekly`, `include_monthly`, `recalculate`, `dry_run`, `batch_size`, `max_batches`, and `person_name_group_key`. Weekly/monthly raw metrics are grouped across every source alias before rates, points, score, qualification, rating, and INCLUDED/EXCLUDED ranking are calculated.

Mapping import columns are exactly `client_id;person_id;person_name;email;ranking_included;is_active;metadata_json;created_at;updated_at`. Defaults are strict `cp1250` decoding and `;` delimiter. The importer validates the complete file before writes, ignores CSV audit timestamps, treats blank metadata as `{}`, and rejects invalid values, client mismatches, normalized source collisions, or group-level email/ranking/activity conflicts. Exact identical input rows are retained once and reported with source row numbers. Writes require `dry_run=false` with `apply=true`.

Weekly/monthly candidates are one row per active physical person. Missing email follows the existing skip audit path; shared addresses do not merge people. Migration `044_eco_email_fail_closed_idempotency.sql` changes normal identity to `(client_id, person_name_group_key, report_type, period_start_date, period_end_date)` for normal `pending` and `sent` rows. Template/rating changes cannot create a second normal delivery. Test and forced scopes remain separate. A stale normal `pending` is never automatically reclaimed; it blocks and reports `STALE_PENDING_REQUIRES_RECONCILIATION` until an operator investigates SMTP acceptance and reconciles the row explicitly.

For BRAVO00016 weekly email sends, the final MIME message is created once, assigned one stable `Message-ID`, sent via SMTP with the configured envelope sender, preserved in `sent_mime_bytes`, and then appended to the mailbox Sent folder through IMAP. The append uses the same MIME bytes and verifies by `Message-ID`; an existing matching Sent copy prevents duplicate append. If SMTP succeeds but Sent archiving fails, the row remains `status='sent'` and `sent_archive_status='failed'`; recovery must use `archive_only=true` so the stored MIME is appended without resending SMTP.

Manual examples:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_mapping_import \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","csv_path":"/path/to/mapping.csv","dry_run":true}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_aggregate \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","month":"2026-06","dry_run":true,"recalculate":true}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","dry_run":true,"limit":5}'
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications \
  '{"client_id":"<BRAVO00016_CLIENT_UUID>","dry_run":true,"month_start_date":"2026-06-01","month_end_date":"2026-07-01"}'
```

Uwaga o V2 staging:

- `db/client_business/017_v2_staging_tables.sql` istnieje i tworzy `source_trips`, `source_notifications`, `source_fuel_observations`.
- Żaden produkcyjny job w repo aktualnie nie zapisuje tych tabel.
- Nowy onboarding nie aplikuje `017_*` bezpośrednio; plik może zostać później zastosowany przez `scripts/apply_client_business_migrations.py`, bo onboarding nie oznacza go jako applied.
- `db/migrations/015_workflow_a_v2_datasets.sql` deklarowało V2 dataset/table rows w platformowej bazie, ale odpowiadające moduły jobów (`ingest_trips`, `ingest_notifications`, `ingest_fuel`, `enrich_trips`) nie istnieją w repo.
- `db/migrations/016_workflow_a_disable_declared_v2_registry.sql` usuwa te V2 rows z aktywnego `dataset_registry` / `table_registry`, więc `jobs/api/telematics/registry.py`, dispatcher i retention worker pozostają implemented-only do czasu realnej implementacji V2 jobów.

**Bezpieczeństwo runtime (Phase 2, ochrona tokena API):**

- Twarde limity liczby żądań HTTP na run, na endpoint i na sub-okno (≤31 dni); twarde limity stron paginacji na sub-okno.
- Brak nieograniczonych retry: ponawianie wyłącznie przy timeout/connection error, z górną granicą (`TELEMATICS_PROVIDER_MAX_RETRIES`).
- Wykrywanie podejrzanej paginacji (m.in. powtarzający się fingerprint strony, brak postępu przy pustych stronach, `current_page` ≠ żądany `page`, częściowe/niespójne `meta`).
- Przy dowolnym „safety stop”: wyjątek `TelematicsProviderSafetyError`, log **ERROR** z `abort_code`, run **FAILED**, **brak** kolejnych żądań do Telematics.
- Szczegóły ENV i domyślnych wartości: `docs/02_infrastructure.md`; operacyjnie: `docs/07_operations.md` (sekcja 5.2).

### `jobs.alpha.import_gps_baza_log_xlsm` — deprecated direct ALPHA00001 XLSM import

Ten job jest zachowany tylko jako awaryjna/manualna kompatybilność ze starszą ścieżką `source_path`. Kanoniczna ścieżka operacyjna dla Alpha GPS to teraz Workflow B email attachment: Stage 1 `.xlsm` → normalized CSV ze wszystkich niepustych arkuszy workbooka → Stage 2 `Alpha_GPS_Baza_LOG` → Stage 3 `telematics_reports."Alpha_GPS_Baza_LOG"`.

Stary job klienta ALPHA00001 / Alpha konsumuje zsynchronizowany plik
`GPS_baza_START_skrypt.xlsm` zapisany przez laptop z Windows/VBA, ale na
serwerze **nie wykonuje makr**. Czyta tylko zapisany skoroszyt przez `openpyxl`
w trybie `read_only=True`, `data_only=True`, `keep_vba=False`.

Funkcjonalność:

- sprawdza stabilność pliku wejściowego przez porównanie rozmiaru i `mtime` przed odczytem,
- liczy `sha256` całego XLSM,
- pomija import, jeśli ten sam `sha256` ma już status `SUCCESS`, chyba że `force=true`,
- czyta arkusz `LOG` i znajduje wiersz nagłówka z dokładnie tymi kolumnami biznesowymi: `ID`, `Nr rejestracyjny`, `Data przydziału`, `Nazwa Pliku csv`,
- mapuje kolumny do `source_id`, `registration`, `assignment_date`, `csv_filename`, zapisuje `source_row_number` oraz `raw_row_json`,
- waliduje datę `Data przydziału` fail-fast; puste wiersze pod nagłówkiem są pomijane i liczone w summary,
- dla realnego importu tworzy `RUNNING` w `telematics_reports.alpha_gps_baza_log_import_runs`, a następnie w jednej transakcji wykonuje `DELETE` z `telematics_reports."Alpha_GPS_Baza_LOG"`, `INSERT` nowych wierszy i oznacza import jako `SUCCESS`,
- przy błędzie po utworzeniu import runa cofa zmiany tabeli docelowej i oznacza import jako `FAILED`,
- uploaduje artifact summary JSON oraz, przy realnym udanym imporcie, kopię źródłowego XLSM do standardowych artefaktów platformy.

Parametry:

- `source_path` (wymagany) — ścieżka do zsynchronizowanego XLSM na serwerze,
- `sheet_name` (opcjonalnie, default `LOG`),
- `dry_run` (opcjonalnie, default `false`; czyta i waliduje workbook bez zmian w tabelach klienta),
- `trigger` (opcjonalnie; przekazywany także do runnera),
- `force` (opcjonalnie, default `false`; wymusza ponowny replace-all dla tego samego `sha256`),
- `client_code` (opcjonalnie, default `ALPHA00001`; job rozwiązuje bazę klienta z `workflow_a_control.client_account`).

Manualnie:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.alpha.import_gps_baza_log_xlsm '{
  "source_path": "/home/logplatform/data/alpha/GPS_baza_START_skrypt.xlsm",
  "sheet_name": "LOG",
  "trigger": "MANUAL"
}'
```

Tabele w bazie klienta `alpha_main`:

- `telematics_reports."Alpha_GPS_Baza_LOG"` — target replace-all; nazwa tabeli jest mixed-case i musi być cytowana w SQL,
- `telematics_reports.alpha_gps_baza_log_import_runs` — historia importów. Indeks unikalny na udanych importach `source_sha256` wymusza co najwyżej jeden aktywny `SUCCESS` dla danego pliku; `force=true` oznacza poprzedni sukces jako `SUPERSEDED`.

Wymagany DDL klienta: `db/client_business/022_alpha_gps_baza_log.sql`, aplikowany przez `scripts/apply_client_business_migrations.py` do bazy klienta.

### `jobs.api.telematics.aggregate_trip_fuel_daily` — Workflow A, Phase 2 (daily aggregation)

Job wtórny względem `sync_trips_and_speeding`: czyta z bazy klienta tabelę `client_trips` dla dystansu, rejestracji i atrybucji kierowcy, pobiera dzienny poziom paliwa przez `GET /fuel/level/{registration}`, liczy zużycie jako `start_period.liters - end_period.liters`, a następnie zapisuje agregaty dzienne. Idempotentny (`ON CONFLICT … DO UPDATE`).

Funkcjonalność:

- czyta `client_trips` w oknie `[window_start_ts, window_end_ts)` (filtr po `start_timestamp`) dla dystansu i atrybucji; agregacja driver-level używa `identification_tag_id` z `client_trips` jako `driver_id` w tabeli dziennej
- pobiera dzienny poziom paliwa przez Telematics `GET /fuel/level/{registration}`; `/fuel/consumed` nie jest używane w tym przepływie klienta
- liczy `fuel_consumed_liters` jako poziom baku na początku dnia minus poziom baku na końcu dnia; wynik ujemny, brak start/end, `calibrated=false` albo nieprecyzyjny start/end zapisuje `NULL`
- agreguje per `(vehicle_id, day)` → `public.client_vehicle_daily_fuel`
- agreguje per `(vehicle_id, driver_id, day)` → `public.client_vehicle_driver_daily_fuel`
- liczy: `distance_m`, `distance_km`, `fuel_consumed_liters`, `avg_fuel_l_per_100km`, `trip_count`, `first_trip_start_ts`, `last_trip_end_ts`
- propaguje `client_id` + `client_code` do każdej zagregowanej linii

Parametry (wymagane):

- `client_id`
- `window_start_ts` (ISO-8601)
- `window_end_ts` (ISO-8601)

Uruchomienie:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.api.telematics.aggregate_trip_fuel_daily '{
  "client_id": "<CLIENT_ID>",
  "window_start_ts": "2026-04-01T00:00:00Z",
  "window_end_ts": "2026-04-08T00:00:00Z"
}'
```

Wymaga, aby tabele `client_vehicle_daily_fuel` i `client_vehicle_driver_daily_fuel` istniały w bazie biznesowej klienta (DDL: `012_*.sql`, `013_*.sql`).

### `jobs.api.telematics.dispatcher` — Workflow A scheduler tick

DB-driven scheduler uruchamiany jako standardowy job runnera. Proponowane unity są w `ops/systemd/proposed/log-job@dispatcher.{service,timer}`; repo ich nie instaluje ani nie włącza automatycznie.

Funkcjonalność:

- czyta enabled rows z `workflow_a_control.client_dataset_schedule` połączone z `client_account` i `dataset_registry`; `client_dataset_schedule.client_code` jest denormalizowanym kodem operatorskim dla audytu/filtrowania i musi odpowiadać `client_id`, gdy jest ustawiony; dla `trips_sync` czyta także `client_dataset_schedule.event_enrichment_mode`,
- próbuje przejąć globalny Postgres advisory lock; jeśli inny dispatcher już go trzyma, tick kończy się czysto bez scheduling loop,
- przed zwykłym `RUNNING` guardem oznacza stare `RUNNING` rows jako `FAILED` po timeoutcie `stale_running_timeout_minutes` / `WORKFLOW_A_DISPATCHER_STALE_RUNNING_TIMEOUT_MINUTES` (default `720` minut),
- sprawdza, czy istnieje dowolny `client_schedule_run_history.status='RUNNING'`; jeśli tak, tick kończy się bez uruchamiania kolejnego joba,
- waliduje `(dataset_name, job_module)` przeciwko `jobs/api/telematics/registry.py` i nie wykonuje arbitralnego `job_module` z DB,
- oblicza najnowszy scheduled fire `<= now` dla `daily`, `weekly`, `monthly` (dzień 1..28 albo ostatni dzień miesiąca) z użyciem `zoneinfo`,
- dla schedule rows bez timezone runtime defaultem jest `Europe/Warsaw`,
- sortuje due rows po `(scheduled_fire_ts, client_code, dataset_name)`,
- claimuje najwyżej jeden fire przez INSERT `RUNNING` do `workflow_a_control.client_schedule_run_history`, zapisując `client_id` i `client_code`,
- uruchamia dataset job przez subprocess `ops/runner.py` z parametrami `client_id`, opcjonalnym `client_code`, `window_start_ts`, `window_end_ts`, `trigger="SCHEDULED"`; dla `trips_sync` dodaje `event_enrichment_mode` ze schedule row (`enabled` albo `disabled`), a obowiązkowe `/trips` chunking `chunk_days=2` jest dziedziczone z defaultu samego joba,
- dla Eco Driving datasetów uruchamia ten sam moduł `jobs.ecodriving.job_eco_driving_aggregate` i przekazuje `mode` oraz `include_weekly` / `include_monthly` zamiast okna API: `eco_driving_weekly_snapshot` liczy latest completed cumulative weekly snapshot, `eco_driving_month_end_weekly_snapshot` liczy final previous-month weekly snapshot, a `eco_driving_monthly_aggregation` liczy previous full calendar month,
- dla Eco Driving Person datasetów przekazuje analogiczne parametry do `jobs.ecodriving_person.job_eco_driving_person_aggregate`: `eco_person_driving_weekly_snapshot`, `eco_person_driving_month_end_weekly_snapshot`, `eco_person_driving_monthly_aggregation`; email datasets `eco_person_driving_weekly_email_notifications` i `eco_person_driving_monthly_email_notifications` dostają `client_id`, `client_code`, `trigger` i `scheduled_fire_ts`, ale pozostają disabled-by-default,
- przekazuje subprocessowi `LOG_PLATFORM_RUN_ID_FILE` i po odczycie run id aktualizuje `client_schedule_run_history.platform_run_id`,
- finalizuje run-history row jako `SUCCESS` albo `FAILED`,
- wykonuje atomową fazę planowania/claimu przed utworzeniem technicznego runa platformy: poprawny tick bez claimable fire kończy się kodem `0` bez nowych wierszy `runs`, `logs` ani `client_schedule_run_history`; nieobecność takich no-op rows jest oczekiwana i nie oznacza zatrzymania timera,
- po skutecznym claimie tworzy normalny run dispatchera z triggerem `SCHEDULED`, zachowuje pełne logi claimu/uruchomienia/wyniku i uruchamia audytowany child run,
- błędy połączenia, zapytania, walidacji enabled schedule, selekcji albo claim/execution nie korzystają z silent-success path: pozostają widoczne jako `FAILED` i `ERROR`; auto-fail stale `RUNNING` jest również audytowanym działaniem, nie cichym no-opem.

Parametry: brak wymaganych; opcjonalnie `stale_running_timeout_minutes`. Manualnie:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.api.telematics.dispatcher '{}'
```

Ograniczenia:

- brak catch-up wielu historycznych fire times; rozważany jest tylko najnowszy fire dla schedule,
- brak równoległego uruchamiania jobów Workflow A,
- brak cron expressions i brak e‑mail triggera.

#### Schema C3 — inert Telematics stabilized coverage state (migracja `057`)

Migracja `057_workflow_a_trips_coverage_state.sql` wprowadza **wyłącznie inertną schemę**. Nie zmienia zachowania dispatchera, joba `trips_sync`, provider clienta ani żadnej konfiguracji klienta.

- Tworzy `workflow_a_control.client_dataset_coverage`, kluczowaną `PRIMARY KEY (schedule_id)` z `FOREIGN KEY → client_dataset_schedule(schedule_id) ON DELETE CASCADE` — dokładnie jeden wiersz coverage na schedule. Wyłączenie schedule (`enabled=false`) wiersza nie usuwa; usunięcie schedule kasuje go kaskadowo, bo odtworzony schedule jest nowym roszczeniem coverage i wymaga ponownego bootstrapu.
- Coverage reprezentuje **wyłącznie domknięty przedział `[coverage_start_ts, covered_through_ts]`**. `coverage_start_ts` jest jawną dolną granicą zweryfikowanego roszczenia i nigdy nie jest wnioskowana; `covered_through_ts` jest monotoniczną górną granicą przyszłego scheduled advancement. Brak dolnej granicy **nie** jest słabszym roszczeniem coverage — jest brakiem roszczenia.
- `bootstrap_status` przyjmuje wyłącznie `UNINITIALIZED`, `READY`, `GAP_DETECTED`, `RESEED_REQUIRED`. Ograniczenia bazodanowe wymuszają porządek granic oraz kompletność `READY` (obie granice, niepusty `bootstrap_evidence_ref`, `seeded_at`, niepusty `seeded_by`), więc wiersz `READY` bez kompletnego, udokumentowanego roszczenia nie może istnieć.
- **Migracja nie tworzy żadnego wiersza coverage.** Nie wnioskuje coverage z historii schedule, z danych klienta ani z timestampów; nie instaluje triggera, funkcji ani procedury; nie zmienia żadnego wiersza `client_account`, `client_dataset_schedule` ani istniejącego wiersza `client_schedule_run_history`. Brak wiersza i wiersz `UNINITIALIZED` są równoważnie bezpieczne — przyszły runtime odrzuca oba.
- Dodaje pięć addytywnych, **nullable i obecnie niezapisywanych** kolumn evidence do `client_schedule_run_history`: `nominal_window_start_ts`, `nominal_window_end_ts`, `stabilization_delay_seconds`, `overlap_seconds`, `trips_pagination_mode`. `window_start_ts` / `window_end_ts` zachowują dotychczasowe znaczenie (okno faktycznie zlecone jobowi), unikalność `(schedule_id, scheduled_fire_ts)` jest nietknięta, semantyka `_finalize_run` niezmieniona, a terminalne wiersze historii pozostają immutable. `NULL` jest poprawną i trwałą wartością dla wierszy historycznych — nic nie backfilluje evidence.
- Sama migracja C3 nie czyta ani nie zapisuje coverage rows. Wdrożony runtime C5/C6 zawiera loader, bootstrap gate i finalizery advancementu; te ścieżki są nieosiągalne dla klienta `strict_meta`, a od canary enablement `2026-08-03` są osiągalne wyłącznie dla `BRAVO00016` — jego pierwszy compatibility fire jeszcze nie nastąpił.
- **Ad hoc operatorski SQL na stanie coverage nadal nie jest autoryzowany.** Istnieje natomiast zrecenzowana procedura: read-only audyt `ops/audit_telematics_coverage_bootstrap.py` (Gate 1) oraz dry-run-first writer `ops/bootstrap_telematics_trips_coverage.py` (Gate 6 krok 5), jedyny autoryzowany `INSERT` coverage — wymaga `--execute` razem z `--confirm-client-code`, hash-weryfikowanego bundla dowodowego i jawnie zatwierdzonych przez operatora `A`/`W`. Writer nie ma `UPDATE`, `DELETE` ani upsertu; reseed i zmiana `bootstrap_status` pozostają poza jego zakresem. **Bootstrap jest operacją per klient — zarówno pierwszy, jak i każdy kolejny.** Bramki writera są zawężone do klienta docelowego (dokładnie jedno konto, dokładnie jeden autorytatywny enabled schedule, cel `strict_meta`, zero wierszy coverage i recovery celu, brak `RUNNING` w historii celu); inni zatwierdzeni klienci compatibility, w tym `BRAVO00016`, są dozwoleni i nie blokują celu. Writer nigdy nie zmienia trybu paginacji żadnego klienta i nie mutuje wiersza innego klienta; stan floty jest wyłącznie raportowany. Każdy klient zachowuje własny inventory, bundel dowodowy, recenzję i autoryzację wykonania. Commit `fix: allow per-client Telematics coverage bootstrap` (`49ba6c02cbebec87ac3c22e9a605fcc249e1f28e`) został niezależnie zrecenzowany i wypchnięty na `origin/main` `2026-08-03`; po recenzji `ALPHA00001` **został zbootstrapowany dokładnie raz** (`A = 2026-07-01T00:00:00Z`, `W = 2026-07-29T01:59:59Z`, źródło `bootstrap`, evidence SHA-256 `80a85e48daeaa9412870a9894b5a4555a25a8809f937c3933e69061744c9dfcc`, approval `TELEMATICS-C10-ALPHA00001-2026-08-PRODUCTION-REPORTING`). Szczegóły i zakazy: `docs/07_operations.md` §5.5.
- Runtime C4–C7 (czyste wyprowadzanie okna, bootstrap gate w dispatcherze, coverage advancement i compatibility pagination) jest wdrożony. Bootstrap, canary enablement `BRAVO00016`, wdrożenie C7 i zastosowanie migracji `058` zostały wykonane `2026-08-03`. Po zatwierdzonym dry-runie wykonano **dokładnie jedno**, osobno autoryzowane recovery C11 dla interwału `2026-07-27T00:00:00Z` – `2026-08-03T00:00:00Z` (approval `TELEMATICS-C11-BRAVO00016-2026-08-03-CANARY-1`) — wynik `SUCCESS`, bez automatycznego retry. **Wykonanie** backfillu oraz każde kolejne recovery pozostaje osobno autoryzowaną bramką. Decyzją operatorską z `2026-08-03` niezależność recenzji jest **procesowa**, a odmienny model recenzenta jest opcjonalny (commit `33e3be855393dc7630007fa824dab2378b9d6bf6`).
- Migracje `055`, `056`, `057` i `058` są zastosowane produkcyjnie dokładnie raz; produkcyjny sufit to `058_telematics_trips_manual_recovery.sql`. Tabela coverage istnieje i od `2026-08-03` ma **dokładnie dwa** wiersze — `BRAVO00016` oraz `ALPHA00001`, oba `trips_sync` i `READY`. Wiersz `ALPHA00001`: `A = 2026-07-01T00:00:00Z`, `W = 2026-08-03T02:00:00Z`, `covered_through_source = 'manual_recovery'`, `last_gap_detected_ts = NULL` (bootstrap zasiał `W = 2026-07-29T01:59:59Z` ze źródłem `bootstrap`, a finalizer sukcesu recovery C11 `f2cdeb56-9c5f-4768-80d2-7fbd4f384f39` przesunął `W` dokładnie raz). Wiersz `BRAVO00016`: `A = 2026-07-01T00:00:00Z`, `W = 2026-08-03T00:00:00Z`, `covered_through_source = 'manual_recovery'`, `last_gap_detected_ts = NULL`. Wstawił go zrecenzowany writer `ops/bootstrap_telematics_trips_coverage.py` (`W = 2026-07-27T00:00:00Z`, źródło `bootstrap`), a jedyną późniejszą zmianą było przesunięcie `W` przez finalizer sukcesu ręcznego recovery C11 z `2026-08-03`. Nie wykonano seedu, reseedu ani backfillu, a żaden z trzech pozostałych klientów (`FOXTROT00001`, `DELTA00001`, `ECHO00001`) nie ma wiersza coverage. Canary enablement `BRAVO00016` z `2026-08-03` **nie zmienił tego wiersza**. Interwał nieudanego strict runu `2026-08-03` (`2026-07-27T00:00:00Z` – `2026-08-03T00:00:00Z`) został **odzyskany** i leży teraz wewnątrz roszczenia `[A, W]`. C11 (ręczne recovery compatibility) jest **zaimplementowane, przetestowane i wykonane produkcyjnie dokładnie raz**: `ops/recover_telematics_trips_window.py` plus wąsko współdzielony CAS `jobs/api/telematics/coverage_finalization.py` i migracja `058_telematics_trips_manual_recovery.sql`, która dodaje tabelę tożsamości/dowodów `workflow_a_control.client_dataset_recovery_run` oraz rozszerza słownik `covered_through_source` o `manual_recovery`. **Migracja `058` jest zastosowana produkcyjnie `2026-08-03`** — sufit produkcyjny to `058`; tabela recovery ma **dokładnie dwa** wiersze, oba `SUCCESS`: `0e86fafa-ded3-4be4-8ec6-01dce344a0d1` (`BRAVO00016`) i `f2cdeb56-9c5f-4768-80d2-7fbd4f384f39` (`ALPHA00001`). `covered_through_source` dopuszcza `bootstrap`, `scheduled_run`, `operator` i `manual_recovery`. Po C11 `W` mogą przesunąć **dokładnie dwie** zrecenzowane powierzchnie: finalizacja sukcesu zaplanowanego fire'a (`covered_through_source = 'scheduled_run'`) i finalizacja sukcesu ręcznego recovery (`'manual_recovery'`); ręczny SQL na coverage pozostaje zabroniony i nie został wykonany. Nieudany fire `2026-08-03` pozostał **bajtowo niezmieniony**, nie utworzono żadnego nowego wiersza historii schedule i nie wykonano automatycznego retry. To samo dotyczy recovery `ALPHA00001`: nieudane fire'y `2026-08-01`, `2026-08-02` i `2026-08-03` pozostały bajtowo niezmienione, brakujące fire'y `2026-07-30` i `2026-07-31` pozostały brakujące, a łączna liczba wierszy historii (`308`) nie zmieniła się. Rollout fleet-wide **nie został wykonany** — klientami compatibility są wyłącznie `BRAVO00016` i `ALPHA00001`; `FOXTROT00001`, `DELTA00001` i `ECHO00001` pozostają `strict_meta`. Szczegóły: `docs/07_operations.md` §5.5. **Stan zaktualizowany `2026-08-04`** — patrz punkt o rollout'cie pozostałej floty poniżej.
- **Pierwszy naturalny scheduled fire `ALPHA00001` compatibility (`2026-08-04T02:00:00Z`) jest terminalny, lecz audyt natural-canary zakończył się `ALPHA_NATURAL_FIRE_BLOCKED_WINDOW`.** Normalny dispatcher utworzył dokładnie jeden wiersz historii `03f6b5a5-38a2-4327-8c5d-ff095857b979` i jeden business run `c36ea500-496e-4aa2-844f-26be3cf8c602`, oba `SUCCESS`, z `trigger = SCHEDULED`, bez recovery UUID. Claim-time mode to `data_invariants_v1`, ale zapisany i wykonany przedział efektywny wynosi `2026-08-02T22:00:00Z` – `2026-08-03T23:00:00Z`, zamiast bramki audytowej `2026-08-03T02:00:00Z` – `2026-08-04T02:00:00Z`; fire został zaclaimowany o nominalnym czasie `02:00Z`, a nie po `05:00Z`. Provider i transakcja biznesowa przeszły bez incydentu, a finalizer atomowo przesunął `W` z `2026-08-03T02:00:00Z` do rzeczywistego `E_end = 2026-08-03T23:00:00Z` ze źródłem `scheduled_run`. Ten zapis nie zatwierdza oczekiwanej granicy raportowania `2026-08-04T02:00:00Z`; rozbieżność kontraktu okna wymaga osobnej decyzji przed kolejnym rolloutem. Pełny sanityzowany dowód i znana pozostałość pięciu stale `runs.status = RUNNING`: `docs/07_operations.md` §5.5.
- **KOREKTA (`2026-08-04`, audyt semantyki `ALPHA_NATURAL_FIRE_SUCCESS_STABILIZED_WINDOW_CONFIRMED`).** Poprzedni wynik `ALPHA_NATURAL_FIRE_BLOCKED_WINDOW` z punktu wyżej **był błędem oczekiwania audytu, a nie awarią produkcji**. Poprzedni audyt zakładał semantykę opóźnionej eligibility (fire claimowalny dopiero od `F + D = 2026-08-04T05:00:00Z`, `E_end = F`), której **nie ma ani w kodzie, ani w dokumentacji**. Obowiązujący kontrakt to przesunięty cutoff: opóźnienie stabilizacji przesuwa **efektywny cutoff danych**, a nie nominalny czas odpalenia schedule'a. Dispatcher odpala, gdy `F <= now_utc` (`jobs/api/telematics/dispatcher.py:428`), a okno wyprowadza `jobs/api/telematics/coverage_windows.py:196-204` wg `docs/13_telematics_trips_stabilization_windows.md` §4.1/§16.1: `E_end = F − D`; `base = (F − L) − D − O`; `candidate_start = min(base, W − O)`; `E_start = max(candidate_start, E_end − R)`; coverage awansuje do `E_end`, nie do `F` (`_finalize_compat_success`, `dispatcher.py:1330-1354`, `new_W = max(W, E_end)`). Naturalny fire `2026-08-04T02:00:00Z` przy `L = 1 d`, `D = 10800 s`, `O = 3600 s` daje dokładnie `E_end = 2026-08-03T23:00:00Z` i `E_start = 2026-08-02T22:00:00Z` — czyli **dokładnie** przedział wykonany produkcyjnie. Provider (7 stron, `advisory_total = 6344`, `total_reconciliation = exact`, `termination_reason = short_page`), jedna zatwierdzona transakcja biznesowa (`6344` wierszy, jeden `synced_at`, zero duplikatów) i atomowa finalizacja coverage (`W` `2026-08-03T02:00:00Z` → `2026-08-03T23:00:00Z`, źródło `scheduled_run`, monotonicznie, bez luki logicznej) są poprawne. Overlap jest wyłącznie overlapem pobrania — ponownie żąda danych sprzed starego `W`, nigdy nie cofa ani nie duplikuje roszczenia coverage. `ALPHA00001` **zaliczył** pierwszy naturalny canary compatibility; recovery, zmiana schedule'a ani zmiana runtime'u **nie są wymagane**. Granica raportowania: coverage do `2026-08-03T23:00:00Z` UTC = `2026-08-04 01:00 CEST`, więc pełna doba lokalna Europe/Warsaw jest zatwierdzona **do `2026-08-03` włącznie**; `2026-08-04` dopiero po kolejnym naturalnym fire. Platforma z założenia **utrzymuje** lag stabilizacji `D` i nie zbiega `W` do nominalnego `F`. Pięć stale `runs.status = RUNNING` pozostaje osobnym długiem obserwowalności. Szczegóły: `docs/07_operations.md` §5.5.
- **Rollout pozostałej floty (`2026-08-04`) — `TELEMATICS_REMAINING_FLEET_ROLLOUT_PARTIAL_SWEE_DISABLED_CONTRACT`.** `DELTA00001` i `FOXTROT00001` przeszły pełną, per-klienta procedurę (inventory C10 → bundel dowodowy → bootstrap → zmiana trybu → recovery C11 → weryfikacja) i są `data_invariants_v1`. Tabela coverage ma teraz **cztery** wiersze `READY`, wszystkie z `A = 2026-07-01T00:00:00Z` i `last_gap_detected_ts = NULL`: `BRAVO00016` `W = 2026-08-03T00:00:00Z` (`manual_recovery`), `ALPHA00001` `W = 2026-08-03T23:00:00Z` (`scheduled_run`), `DELTA00001` `W = 2026-08-03T21:00:00Z` (`manual_recovery`), `FOXTROT00001` `W = 2026-08-03T23:00:00Z` (`manual_recovery`). Tabela recovery ma **cztery** wiersze, wszystkie `SUCCESS`; aktywnych recovery `0`. Bootstrap `DELTA00001` zasiał `W = 2026-07-22T23:59:59Z` (7-dniowy lookback cofa pierwszy nierozwiązany interwał do `2026-07-23T00:00:00Z`), a bootstrap `FOXTROT00001` `W = 2026-07-29T01:59:59Z`; oba `W` przesunął następnie dokładnie raz finalizer sukcesu recovery C11. Łączna liczba wierszy historii schedule pozostała `312` przed i po całym rollout'cie: **zero** mutacji nieudanych fire'ów, **zero** odtworzonych brakujących fire'ów (`2026-07-30`, `2026-07-31` nadal brakują dla obu klientów), **zero** wierszy syntetycznych, **zero** automatycznych retry. `ECHO00001` **pozostaje `strict_meta`, bez wiersza coverage i bez wiersza recovery** — jego jedyny autorytatywny schedule `trips_sync` (`60c80b85-f294-4a00-8e09-b6a3688af443`) jest `enabled = false`, a wszystkie trzy zrecenzowane narzędzia (audyt C10, writer C10-W, recovery C11) wymagają dokładnie jednego **enabled** schedule'a i odmawiają przed jakimkolwiek zapisem. Szczegóły i klasyfikacje terminalne: `docs/07_operations.md` §5.5.

#### C4 — czysty helper wyprowadzania ustabilizowanego okna (`coverage_windows`)

Istnieje moduł `jobs/api/telematics/coverage_windows.py`: **czysta, deterministyczna** funkcja `derive_effective_window(...)` zwracająca niemutowalny `EffectiveWindow`. Specyfikacją jest `docs/13_telematics_trips_stabilization_windows.md` §16.1. Moduł nie wykonuje żadnego I/O — bez zegara, bez ENV, bez timezone database, bez logowania, bez sieci i bez bazy danych; wszystkie wejścia podaje jawnie wywołujący i są walidowane (timezone-aware UTC o pełnosekundowej precyzji, dokładne typy `int` bez `bool`, `O <= R`, `A <= W`).

- **Dispatcher produkcyjny go nie importuje.** Żaden moduł runtime (`dispatcher.py`, `control_plane.py`, `provider_client.py`, `provider_safety.py`, `sync_trips_and_speeding.py`) nie odwołuje się do tego helpera; w C4 importują go wyłącznie skrypty testowe. Zachowanie dispatchera, `_build_job_params`, semantyka claimu oraz obecne okna nominalne pozostają niezmienione.
- **Okno nominalne** (bez zmian względem dzisiejszego dispatchera): `N_end = F`, `N_start = F − L`, gdzie `F` to `scheduled_fire_ts` w UTC, a `L = lookback_days × 86400`.
- **Okno efektywne** (jeszcze nieużywane w runtime): `E_end = F − D`; `base = (F − L) − D − O`; `candidate_start = min(base, W − O)`; `E_start = max(candidate_start, E_end − R)`. `D` i `O` są bezwzględnymi sekundami UTC odejmowanymi **po** konwersji local→UTC, nigdy w lokalnym czasie ściennym.
- **Spójność (`is_connected`)** jest liczona jako `E_start <= W + 1 s` na jednosekundowej siatce przedziałów domkniętych i **nie ma żadnego efektu runtime**: helper jej nie egzekwuje, nie zgłasza wyjątku dla okna rozłącznego, nie loguje i nie dotyka historii schedule. `+1 s` jest krokiem siatki, a nie czasem trwania overlapu (`docs/13_…` §2.4).
- **Żaden stan coverage nie jest czytany.** Helper nie wykonuje `SELECT` na `client_dataset_coverage`; `coverage_start_ts` i `covered_through_ts` są parametrami wejściowymi i są zwracane niezmienione. `E_start` nigdy nie jest przycinany do `coverage_start_ts`: żądanie może zaczynać się wcześniej niż `A`, co jest wyłącznie arytmetyką żądania i **nie** przesuwa zweryfikowanego roszczenia coverage wstecz.
- **Nie istnieje bootstrap gate.** C4 nie ładuje `bootstrap_status`, nie rozstrzyga czy wiersz jest `READY` i nie zgłasza `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`.
- **Brak evidence i brak advancement.** Nic nie zapisuje kolumn evidence w `client_schedule_run_history` ani nie przesuwa `covered_through_ts`; `max(W, E_end)` nie jest przez ten moduł liczone ani udostępniane.
- **Sam commit C4 nie zmienił żadnej konfiguracji klienta ani żadnego schedule.** Stan trybów jest odrębnym faktem operacyjnym: od `2026-08-03` `BRAVO00016` jest `data_invariants_v1`, a pozostali czterej klienci `strict_meta`.
- Integracja C5 (bootstrap gate + wyprowadzanie okna w dispatcherze) i advancement C6 są wdrożone. Ta dokumentacja nie opisuje procedury enablement; bootstrap wykonano `2026-08-03`, canary enablement `BRAVO00016` również (`docs/07_operations.md` §5.5), a właściwe wykonanie canary pozostaje odrębną, osobno autoryzowaną operacją.

Testy: `ops/tests_manual/test_telematics_trips_stabilization_windows.py` (czysty, stdlib-only; `zoneinfo` używany wyłącznie do budowy reprezentatywnych fixture'ów UTC, nie wewnątrz helpera).

#### C5 — coverage bootstrap gate i integracja z dispatcherem

Dispatcher importuje `coverage_windows` i egzekwuje fail-closed bramkę `docs/13_telematics_trips_stabilization_windows.md` §5.2.1 w kolejności `docs/14_…` §7.1. C6 jest zaimplementowane i wdrożone; produkcyjny sufit to `057_workflow_a_trips_coverage_state.sql`, migracje `055`–`057` są zastosowane, kolumny compatibility/history evidence i tabela coverage istnieją. Od `2026-08-03` istnieje dokładnie jeden wiersz coverage (`BRAVO00016` / `trips_sync`, `READY`, `A = 2026-07-01T00:00:00Z`, `W = 2026-07-27T00:00:00Z`) i tego samego dnia — po bootstrapie, w kolejności `docs/13_…` §13.6 — `BRAVO00016` został przełączony na `data_invariants_v1`. Pozostali czterej klienci są `strict_meta` i dla nich dispatcher nadal w ogóle nie czyta `client_dataset_coverage`. Ścieżka compatibility jest zatem **uzbrojona, lecz jeszcze nieuruchomiona**: żaden compatibility fire nie wystąpił, seed, reseed, recovery ani backfill nie zostały wykonane, a writery C6 nie zostały jeszcze osiągnięte.

Pierwszy naturalny tick dispatchera po wdrożeniu, `2026-08-02 23:06:22 CEST`, zakończył się sukcesem i załadował zmigrowany schemat; kolejny naturalny tick o `23:10:00 CEST` również zakończył się sukcesem. Pierwszy naturalny tick po canary enablemencie, `2026-08-03 10:40:00 CEST`, również zakończył się `Result=success` / exit `0` i poprawnie załadował mieszaną flotę (`BRAVO00016` jako `data_invariants_v1`, czterej pozostali jako `strict_meta`) — loader fail-closes na nieznanym trybie lub wartości spoza kontraktu, więc udany tick jest dowodem poprawnego załadowania. Żaden z tych ticków nie claimował schedule, nie utworzył business joba ani requestu do providera i nie wyemitował kodu incydentu C6.

- **Tryb strict pozostaje bez zmian operacyjnych.** Dla `trips_pagination_mode = 'strict_meta'` — oraz dla każdego datasetu innego niż `trips_sync` — dispatcher nie czyta `client_dataset_coverage`, nie ewaluuje bramki, nie wyprowadza okna efektywnego, zapisuje `NULL` we wszystkich pięciu kolumnach evidence i zachowuje dotychczasową arytmetykę `[F-L, F]`, dobór due fire, unikalność claimu, wartości parametrów joba, komendę subprocess, obsługę exit code, finalizację `SUCCESS`/`FAILED`, ciche no-opy oraz obecną klasyfikację `PAGINATION_MISMATCH`. Jedyna addytywna różnica dla strict `trips_sync` to nowy parametr `scheduled_fire_ts` (domykający lukę R6 z `docs/13_…` §14.2); job go dziś ignoruje.
- **Compatibility scheduled fire stosuje ścisłą precedencję.** Brak wiersza i każda niezgodność tożsamości schedule/client/client-code/dataset dają `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`. Następnie `READY` i `GAP_DETECTED` przechodzą ten sam czysty walidator fundamentów: kompletne, aware, pełnosekundowe i uporządkowane `A/W`, `W` nie w przyszłości, niepuste evidence, obecne aware `seeded_at` oraz niepusty `seeded_by`. `UNINITIALIZED`, `RESEED_REQUIRED`, nieznany i `NULL` status zawsze pozostają bootstrap-required.
- **Poprawnie zapisany `GAP_DETECTED`** jest wąskim wyjątkiem: re-emituje `TRIPS_COVERAGE_GAP_DETECTED` na każdym późniejszym due fire, z `requires_gap_persistence=false`, bez okna launch i bez mutacji. **Malformed `GAP_DETECTED`** daje `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED`; sam status string nie jest dowodem wcześniej zweryfikowanego gapu. Poprawny rozłączny `READY` nadal daje gap code z `requires_gap_persistence=true`.
- **Pre-claim C5 pozostaje read-only.** Dispatcher wykonuje jeden `SELECT` coverage po authoritative `schedule_id`, bez `FOR UPDATE`, i zachowuje ten sam niemutowalny dwunastopolowy `CoverageState` przez claim i subprocess. Tylko dwa nazwane finalizery C6 wykonują coverage lock/write.
- **Żaden klient nie może zostać włączony przed osobnymi bramkami migracji, wdrożenia, bootstrapu i enablementu.** Reject jest egzekwowany po claimie i przed parametrami/subprocessem/sekretami/gniazdem, bez fallbacku do strict.
- **Każdy odrzucony fire zostawia trwały dowód i nie uruchamia joba:** wiersz historii jest claimowany (okno nominalne), finalizowany jako `FAILED` z ograniczonym `error_summary` równym kodowi abortu, logowany jako `ERROR` ze stabilnym `abort_code`, oraz raportowany przez istniejący mechanizm `suspected_bug` (fingerprint bez `scheduled_fire_ts`, więc powtarzalne fire'y agregują się jako occurrences jednego incydentu). Żaden inny terminalny wiersz historii nie jest modyfikowany.
- **Evidence claim-time jest zapisywane dokładnie raz** w tym samym `INSERT`, który tworzy wiersz `RUNNING`: `nominal_window_start_ts`, `nominal_window_end_ts`, `stabilization_delay_seconds`, `overlap_seconds`, `trips_pagination_mode`. `_finalize_run` nadal zapisuje wyłącznie `status`, `finished_at` i `error_summary`; nic nie backfilluje historii i nie zapisuje `trips_max_recovery_span_seconds` (migracja `057` nie tworzy takiej kolumny historii). Dla dozwolonego compatibility fire `window_start_ts`/`window_end_ts` niosą okno **efektywne**, a kolumny nominalne `[F-L, F]`; job dostaje okno efektywne dokładnie raz i nigdy go nie przelicza.
- **C6 posiada trwałe mutacje coverage.** Po `rc == 0` atomowo wykonuje monotoniczne `W → E_end` i `RUNNING → SUCCESS`, albo prawdziwy coverage no-op dla `E_end <= W`. Nowy rozłączny `READY` jest przed launch atomowo utrwalany jako `GAP_DETECTED` razem z historią `FAILED`; istniejący lub malformed gap nie jest przepisywany.
- **Nic nie jest inicjalizowane automatycznie.** Coverage nie jest zasiewane ani wnioskowane z historii schedule, z `client_trips`, z najnowszego wiersza `SUCCESS` ani z `max(synced_at)`. Nie wykonano bootstrapu, reseedu, recovery ani backfillu, a ta dokumentacja nie podaje procedury enablement, SQL-a seedującego ani komendy przełączenia trybu — pozostają one osobno autoryzowanymi bramkami.
- Runtime wymaga migracji `055`–`057`; przeciw niekompatybilnej schemie loader zawodzi widocznie zamiast po cichu wracać do strict.

#### C6 — atomowa mutacja coverage (zaimplementowana i wdrożona; canary uzbrojony, nieuruchomiony)

`_finalize_compat_success` i `_finalize_compat_gap` zachowują lock order coverage → history, weryfikują pełny retained claim snapshot oraz historię `RUNNING` przed coverage mutation i stosują zatwierdzony NULL-safe CAS. Claim loss nie mutuje coverage ani nie nadpisuje terminalnej historii. Niepewny `COMMIT` uruchamia świeży read-only repeatable-read reconciliation; nie ma blind `FAILED`, inference z danych klienta ani automatycznego replay.

Kody C6: `TRIPS_COVERAGE_ADVANCE_CONFLICT`, `TRIPS_COVERAGE_GAP_DETECTED_PERSISTENCE_CONFLICT`, `TRIPS_HISTORY_CLAIM_LOST`, `TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED`, `TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE`, `TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE`. `strict_meta` i non-trips nadal używają niezmienionego `_finalize_run` bez coverage SQL.

Migracje `055`–`057` i runtime C6 są wdrożone produkcyjnie. Narzędzia bootstrapu (`ops/audit_telematics_coverage_bootstrap.py`, `ops/bootstrap_telematics_trips_coverage.py`) są zaimplementowane i przetestowane; writer został uruchomiony na produkcji dokładnie dwa razy — po jednym razie na klienta: `2026-08-03` dla `BRAVO00016` / `trips_sync` i `2026-08-03` dla `ALPHA00001` / `trips_sync` (po niezależnej recenzji commita `49ba6c02`). Tego samego dnia wykonano dwa compatibility enablementy (`strict_meta` → `data_invariants_v1`): canary `BRAVO00016` i `ALPHA00001`; żadnemu z nich nie towarzyszyła mutacja coverage ani żaden job. Obecnie klientami compatibility są `BRAVO00016` i `ALPHA00001`, trzej pozostali są strict, a tabela coverage ma dokładnie dwa wiersze. Testy: `test_telematics_coverage_bootstrap_gate.py`, `test_telematics_coverage_finalization_postgres.py`, `test_telematics_coverage_concurrency_postgres.py`, `test_workflow_a_dispatcher.py`, `test_telematics_coverage_bootstrap_audit.py`, `test_telematics_coverage_bootstrap_writer_postgres.py`, `test_telematics_coverage_bootstrap_multi_client_postgres.py` i zawężony `test_telematics_coverage_state_schema_postgres.py`.

### `jobs.api.telematics.retention_purge` — Workflow A client-business retention

Worker retencji danych biznesowych klientów. Działa niezależnie od platformowego prune: API udostępnia tylko bezpieczny dry-run planner, a hostowy `api.platform_prune --execute` może usuwać wyłącznie kwalifikujące się terminalne `runs`, `logs`, niereferencjonowane artefakty platformowe i odpowiadające im obiekty MinIO.

Funkcjonalność:

- czyta enabled policies z `workflow_a_control.client_table_retention` połączone z `table_registry` i `client_account`; `client_table_retention.client_code` jest denormalizowanym kodem operatorskim dla audytu/filtrowania i musi odpowiadać `client_id`, gdy jest ustawiony,
- waliduje każdą tabelę i retention key column przeciwko `jobs/api/telematics/registry.py`,
- liczy `cutoff_ts = now_utc - retention_days` w Pythonie,
- łączy się do bazy biznesowej klienta z `client_db_*` i `client_db_password_secret_ref`,
- w `dry_run=true` liczy wiersze kwalifikujące się do usunięcia,
- w `dry_run=false` usuwa wiersze batchami po `ctid`, commit per batch, i zapisuje audit columns `last_purge_*`.

Parametry:

- `dry_run` (default `true`),
- `client_id` (opcjonalny filtr),
- `table_name` (opcjonalny filtr),
- `batch_size` (default `5000`),
- `max_batches` (opcjonalny limit testowy).

Manualnie:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.api.telematics.retention_purge '{"dry_run":true,"batch_size":5000}'
```

## Workflow B — Stage 3 (łańcuch backupowy)

Stage 3 jest zaimplementowany jako `jobs.reports.stage3.job_stage3` i opisany w sekcji aktualnych jobów Workflow B wyżej. Ładuje finalized cleaned artifacts ze Stage 2 do właściwej bazy klienta, do schematu `telematics_reports`, z polityką overwrite per `(client_code, report_type)`.
