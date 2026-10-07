# API Specification

Źródło prawdy: `api/main.py`.

Uwaga: `docs/openapi.yaml` w tym repo jest referencją zewnętrznego **Telematics Fleet API** dla Workflow A. Nie jest wygenerowaną specyfikacją tego wewnętrznego API platformy.

## Rola API w platformie

Wewnętrzne HTTP API platformy służy **obserwowalności i trwałości runów, logów oraz artefaktów** jobów uruchamianych przez runner. To **generyczna warstwa „plumbing”**, wspólna dla różnych typów jobów:

- **Workflow A (docelowy)**: joby synchronizacji API → baza klienta mogą i powinny logować przebieg i ewentualnie wgrywać artefakty przez to API, tak jak inne joby.
- **Workflow B (backup)**: ingest pocztowy i Stage 2 korzystają z tego samego API; pole opcjonalne `raw_file_id` przy uploadzie artefaktu **wiąże** artefakt z wierszem `ingest.raw_file` — to udogodnienie dla ścieżki raportowej, a **nie** ograniczenie API wyłącznie do Stage 1.

Nie należy interpretować dokumentacji endpointów jako „API wyłącznie pod raporty z maila”. Nie opisano tutaj żadnych endpointów publicznego dostawcy używanego w Workflow A — integracja zewnętrzna odbywa się po stronie jobów (poza tą specyfikacją).

## Autoryzacja

Bearer token:

- odczyt: `API_READ_TOKEN`
- zapis: `API_WRITE_TOKEN`

Artifact Explorer UI (`/artifact-explorer*`) oraz portal shells (`/user*`, `/admin*`) uzywaja tego samego lokalnego session-cookie auth dla ludzi, opartego o `artifact_users`. JSON `/artifact-browser/*` pozostaje tokenowe i machine-level; Phase 6 RBAC jest egzekwowane na server-rendered Artifact Explorer UI routes. `/admin*` wymaga `artifact_users.is_admin=true`. Phase 2A dodaje graficzne zarzadzanie lokalnymi UI users pod `/admin/users`. Phase 2B dodaje portal-level client registry i user-client assignments pod `/admin/client-access`. Phase 2C dodaje portal report folders pod `/admin/report-folders` i customer-facing Reports Explorer pod `/user/reports`. Phase 3A dodaje katalog zatwierdzonych datasetow baz klientow pod `/admin/client-access/database` i pokazuje przypisane dataset cards w `/user/database`. Phase 3B dodaje read-only row browsing pod `/user/database/datasets/{dataset_id}` dla jawnie przypisanych datasetow. Phase 3C dodaje bezpieczny bezposredni eksport CSV/XLSX pod `/user/database/datasets/{dataset_id}/export` oraz `portal_audit_events`. Async Database Explorer exports dodaja osobny background flow: `POST /user/database/datasets/{dataset_id}/exports`, durable `database_export_jobs`, requester-owned artifacts, per-owner system folder `Database Exports` pod `/user/reports/database-exports`, i kompatybilny redirect/listing przez `/user/database/exports`. Phase 4A rozszerza audit coverage na loginy, denied access, admin user/client/report/database changes, report preview/download, database row browsing i export oraz dodaje filtry/paginacje w `/admin/audit`. Phase 4B dodaje portal groups pod `/admin/groups` oraz additive direct+group effective access dla klientow, report folders i database datasets. Guided configuration dodaje admin-only wybor klienta, schematu, tabeli i fizycznych kolumn datasetu z `information_schema`, z fallbackiem manualnym dla bezpiecznych identyfikatorow; nadal nie dodaje write operations, mutacji schematu, row-value preview ani arbitralnego SQL. Stage 2 Eco Driving Explorer dodaje **read-only JSON API** pod `/user/eco-driving/api/*`, korzystajace z tego samego session-cookie auth (JSON `401`/`403`, bez nowego cookie i bez Bearer), z dedykowanym RBAC (`can_view_eco_ranking`, `can_view_eco_trip_details`, `can_view_eco_trip_routes`) i sanitowanym auditem; szczegoly nizej. Stage 3 dodaje strony landing i ranking browsing, a Stage 4 ranking-entry summary z warunkowa rekoncyliacja; warstwy reuzywaja `EcoDrivingApiService`, bez trip rows ani route/location.


### Database Explorer async export artifacts

Background Database Explorer exports are indexed as normal Artifact Explorer rows with `workflow_name=database_explorer`, `stage_name=async_export`, `artifact_role=database_export`, `report_type=database_export`, `kind=REPORT`, `owner_user_id=<requesting artifact_users.user_id>`, and `expires_at=completed_at + 3 calendar days`. Migration `044_database_export_system_folders.sql` adds stable per-owner `database_export_system_folders` rows and a DB-enforced owner-coupled `database_export_jobs.system_folder_id` relation, so async job lifecycle is shown in the owner's system-managed `Database Exports` folder under Reports. Pending queued/running jobs are virtual rows only; no placeholder artifact is created. Completed jobs link to the real generated artifact when it is unexpired and not deleted; failed rows show only the generic portal-safe failure text while `database_export_jobs.safe_error_code` retains a static administrator-facing failure category; expired/deleted rows do not expose a stale download action. Non-admin users can view/download their own unexpired generated exports through Artifact Explorer and the Reports folder; ordinary users cannot list or download another user's generated export. Admin access remains the existing technical Artifact Explorer admin behavior, except download/preview is blocked globally after artifact expiry.

## Endpointy

| Method | Path | Auth | Wejście | Zachowanie |
|---|---|---|---|---|
| GET | `/health` | brak | - | `{"ok": true}` |
| GET | `/secure/ping` | read | header `Authorization` | test tokena read |
| POST | `/runs` | write | form: `trigger`, `source`, `actor?`, `params_json` | tworzy run `RUNNING` |
| PATCH | `/runs/{run_id}` | write | JSON body: `{"status": "..."}` | waliduje status w `RUNNING/SUCCESS/FAILED/CANCELED`; dla statusu finalnego ustawia `ended_at`; terminalizacja jest jednokierunkowa (P1-I) — powtórzenie tego samego statusu finalnego jest idempotentne, zmiana jednego statusu finalnego na inny zwraca `409` |
| GET | `/runs` | read | query: `source?`, `status?`, `trigger?`, `actor?`, `limit` (default 100, max 500) | lista runów po `started_at DESC` |
| POST | `/logs` | write | form: `ts?`, `level`, `type`, `source`, `message`, `run_id?`, `context_json?`, `error?` | zapis jednego logu |
| GET | `/logs` | read | query: `from?`, `to?`, `type?`, `level?`, `source?`, `run_id?`, `q?`, `limit` (default 200, max 1000) | filtrowanie logów |
| POST | `/suspected-bugs` | write | form: `event_json` (obiekt JSON zgodny z `api.suspected_bug.SuspectedBugEvent`) | atomowo zapisuje log `ERROR` z `context.classification="suspected_bug"`, upsertuje incident po fingerprincie, dopisuje occurrence i opcjonalnie enqueue'uje jeden mail w outboxie; zwraca `SuspectedBugReportResult` |
| POST | `/artifacts/upload` | write | multipart: `file`, `kind?`, `run_id?`, `raw_file_id?`, `workflow_name?`, `stage_name?`, `artifact_role?`, `report_type?`, `client_code?`, `original_filename?`, `display_filename?`, `metadata_json?`, `idempotency_scope?`, `idempotency_key?` | upload pliku do MinIO i metadanych do DB; API buduje fizyczny `storage_key` layout_version=2 dla nowych artefaktów; opcjonalne `client_code` zapisuje operator-friendly kod klienta, a opcjonalny `raw_file_id` (UUID) wiąże artifact z `ingest.raw_file(id)` — typowe dla **Workflow B** |

`idempotency_scope` (max 128 characters) and `idempotency_key` (max 256) are optional but must be supplied together and must not be blank. They are opaque, non-secret values in the current shared machine `API_WRITE_TOKEN` namespace; they do not provide tenant isolation. A keyed first upload returns `idempotency_status: "created"`; a compatible replay returns the same canonical `artifact_id` with `idempotency_status: "reused"`. Incompatible reuse returns HTTP 409. The immutable compatibility fingerprint is SHA-256, `kind`, content type, workflow, stage, artifact role, report type, client code, raw-file lineage, and artifact layout version. Filenames, display names, and free-form metadata are not fingerprint fields.

Keyed objects use `idempotent/v1/<digest-prefix>/<SHA-256(scope NUL key)>.<ext>`; raw identities are never placed in the object key. A PostgreSQL transaction advisory lock covers lookup, compatibility check, object upload, and row insert. A retry after an uploaded object but failed insert overwrites/adopts the same canonical key and converges. Migration `047_artifact_upload_idempotency.sql` must be applied before deploying this API version.
| GET | `/artifacts` | read | query: `run_id?`, `kind?`, `limit` (default 100, max 500) | lista metadanych artefaktów |
| GET | `/artifacts/{artifact_id}/download` | read | path: `artifact_id` | stream z MinIO; artifacts with `expires_at <= now()` or `expired_at` are rejected even if the object still exists |
| GET | `/artifact-browser/artifacts` | read | query: exact filters `workflow_name?`, `stage_name?`, `artifact_role?`, `report_type?`, `original_filename?`, `display_filename?`, `client_code?`, `tag?`, `run_id?`, `raw_file_id?`, `layout_version?`, `file_ext?`, `kind?`, `content_type?`; contains filters `<field>_search?`; plus `date_from?`, `date_to?`, `search?`, `limit?`, `offset?`, `sort?` | paginowana lista artefaktów dla Artifact Explorer; finite filters obsługują exact multi-value, a `<field>_search` daje case-insensitive contains gdy exact values nie są podane |
| GET | `/artifact-browser/artifacts/facets` | read | - | distinct, non-NULL/non-blank wartości dla dropdownów: workflow/stage/role/report_type/original_filename/display_filename/file_ext/client_code/kind/layout_version/tags |
| GET | `/artifact-browser/artifacts/{artifact_id}` | read | path: `artifact_id` | szczegóły artefaktu z lekkim lineage: run summary i raw_file summary, jeśli dostępne |
| GET | `/artifact-browser/artifacts/{artifact_id}/virtual-folders` | read | path: `artifact_id` | memberships: manual folders from `artifact_virtual_folder_items` plus optional `membership_type` (`manual` / `smart_dynamic`) when a smart folder's saved search matches the artifact |
| PATCH | `/artifact-browser/artifacts/{artifact_id}/metadata` | write | JSON: `description?`, `metadata_json?` | upsert manualnej adnotacji artefaktu; nie zmienia pliku ani `artifacts.metadata_json` |
| POST | `/artifact-browser/artifacts/{artifact_id}/tags` | write | JSON: `tag` | dodaje znormalizowany tag; idempotentne dla istniejącego tagu |
| DELETE | `/artifact-browser/artifacts/{artifact_id}/tags/{tag}` | write | path: `tag` | usuwa relację tagu z artefaktem; nie usuwa artefaktu |
| GET | `/artifact-browser/artifacts/{artifact_id}/preview` | read | path: `artifact_id`; query: `rows_limit?`, `sheet_name?`, `sheet_index?`, `text_chars_limit?` | read-only preview dla `csv`, `xls`, `xlsx`, `txt`, `log`, `json`, `pdf` |
| GET | `/artifact-browser/artifacts/{artifact_id}/download` | read | path: `artifact_id`; query: `disposition=attachment|inline` | read-only alias downloadu z MinIO; używa `display_filename`/`filename` w `Content-Disposition`; expired artifacts are not downloadable |
| GET | `/artifact-browser/runs/{run_id}/artifacts` | read | path: `run_id`; query: `limit?`, `offset?`, `sort?` | wygodny alias listy artefaktów dla runa |
| GET | `/artifact-browser/raw-files/{raw_file_id}/artifacts` | read | path: `raw_file_id`; query: `limit?`, `offset?`, `sort?` | wygodny alias listy artefaktów dla `ingest.raw_file` |
| GET | `/artifact-browser/virtual-folders` | read | query: `parent_folder_id?` | lista root lub child folderów; zwraca `folder_type`, `search_query_json`; `artifacts_count` dla manual, dla smart może być `null` |
| POST | `/artifact-browser/virtual-folders` | write | JSON: `parent_folder_id?`, `folder_name`, `description?`, `folder_type?` (`manual` \| `smart`, domyślnie `manual`), `search_query_json?` (tylko smart) | tworzy folder metadata-only; smart zapisuje allowlistowany JSON filtrów (jak lista Artifact Browser); manual odrzuca `search_query_json` w body |
| GET | `/artifact-browser/virtual-folders/{folder_id}` | read | query: `limit?`, `offset?`, `sort?` | `folder`, `breadcrumbs`, `child_folders`, `artifacts`; smart: artefakty z zapisanego `search_query_json`; manual: z `artifact_virtual_folder_items` |
| PATCH | `/artifact-browser/virtual-folders/{folder_id}` | write | JSON: `folder_name?`, `description?`, `search_query_json?` (tylko smart) | aktualizacja; slug stabilny |
| DELETE | `/artifact-browser/virtual-folders/{folder_id}` | write | path: `folder_id` | usuwa folder, child folders i relacje folder-item przez cascade; nie usuwa artefaktów ani obiektów MinIO |
| POST | `/artifact-browser/virtual-folders/{folder_id}/artifacts` | write | JSON: `artifact_id` | tylko manual; idempotentne dodanie relacji; smart → `400` (edytuj kryteria) |
| DELETE | `/artifact-browser/virtual-folders/{folder_id}/artifacts/{artifact_id}` | write | path: `folder_id`, `artifact_id` | tylko manual; usuwa relację; smart → `400` |
| GET | `/login` | UI session | query: `next?` | kanoniczny formularz logowania platformy (Phase 1); deleguje do handlera Artifact Explorer |
| POST | `/login` | UI session | form: `username`, `password`, `next?` | kanoniczne logowanie platformy; ta sama logika i to samo cookie sesji co `/artifact-explorer/login` |
| GET/POST | `/logout` | UI session | - | kanoniczne wylogowanie platformy; czyści to samo cookie sesji |
| GET | `/artifact-explorer/login` | UI session | query: `next?` | alias zgodności – formularz logowania (zachowany) |
| POST | `/artifact-explorer/login` | UI session | form: `username`, `password`, `next?` | alias zgodności – weryfikuje lokalne hasło i ustawia podpisane cookie sesji |
| GET/POST | `/artifact-explorer/logout` | UI session | - | alias zgodności – czyści cookie sesji |
| GET | `/user` | UI session | - | redirect do `/user/reports` |
| GET | `/user/reports` | UI session | - | customer-facing Reports Explorer: pokazuje tylko aktywne `portal_report_folders` przypisane uzytkownikowi bezposrednio albo przez aktywna grupe i tylko dla aktywnych klientow z effective `can_view_reports=true` |
| GET | `/user/reports/database-exports` | UI session | - | canonical Reports entry point for the signed-in user's system-managed Database Exports folder after migration 044. If no folder exists yet, shows a controlled empty/unavailable state; after migration 043 but before 044, falls back to the compatible legacy background-export listing |
| GET | `/user/reports/database-exports/{folder_id}` | UI session | path: `folder_id` | owner-scoped Database Exports folder view. The route only resolves folders where `owner_user_id` is the signed-in user and `system_key='database_exports'`; non-owners receive the same unavailable response as inaccessible report folders |
| GET | `/user/reports/database-exports/{folder_id}/artifacts/{artifact_id}/download` | UI session | path: `folder_id`, `artifact_id`; query: `disposition?` | owner-scoped download wrapper for completed async export jobs in the requested system folder. It denies missing, failed, pending, expired, deleted, or non-owner artifacts and never exposes storage keys |
| GET | `/user/reports/folders/{folder_id}` | UI session | path: `folder_id` | lista raportow pasujacych do folderu; wymaga aktywnego folderu, bezposredniego albo grupowego przypisania folderu, aktywnego klienta i effective `can_view_reports=true` |
| GET | `/user/reports/artifacts/{artifact_id}/preview` | UI session | query: `folder_id`; path: `artifact_id` | portal-specific preview wrapper; wymaga dostepu do folderu, `can_preview=true`, dopasowania artefaktu do filtrow folderu i tego samego `client_code` |
| GET | `/user/reports/artifacts/{artifact_id}/download` | UI session | query: `folder_id`, `disposition?`; path: `artifact_id` | portal-specific download wrapper; wymaga dostepu do folderu, `can_download=true`, dopasowania artefaktu do filtrow folderu i tego samego `client_code`; expired artifacts remain blocked by the common artifact download path |
| GET | `/user/database` | UI session | - | User Portal dashboard pokazujacy tylko aktywne dataset cards z `portal_database_datasets`, gdy user ma effective `can_view_database=true` dla klienta oraz bezposrednie albo grupowe dataset access z `can_view_rows=true`; linkuje do read-only row browser i pokazuje export badge tylko dla effective `can_export_rows=true` |
| GET | `/user/database/datasets/{dataset_id}` | UI session | query: `page?`, `limit?`, `sort?`, `direction?`, `filter__{column}?`, `op__{column}?`, `dateop__{date_column}?`, `date__{date_column}?`, `date_from__{date_column}?`, `date_to__{date_column}?`, legacy `date_from?`, `date_to?`, `search?`, `density?`, `cols?`, `colorder?`, `colw?`, `colpin?`, `colw__{column}?`, `colsel?`, `colpanel?`, `row?`, `rowfields?` | read-only row browser dla przypisanego datasetu; wybiera tylko widoczne kolumny z katalogu, sortuje/filtruje tylko po allowlistowanych kolumnach i operatorach, limit domyslny 100 i max 500; nie wykonuje arbitralnego SQL. Phase 2A: opcjonalny `search` to global search (parametryzowany ILIKE) po widocznych, filterable text columns; `density` (`comfortable\|compact`) jest tylko prezentacyjne; date presets ustawiaja legacy `date_from`/`date_to` na `default_date_column`. Date-like columns maja operator `dateop__{column}=older|newer|range`; `older` uzywa strict `< date__{column}`, `newer` strict `> date__{column}`, a `range` uzywa inclusive `>= date_from__{column}` i `<= date_to__{column}`. Pusty jeden bok range oznacza open-ended inclusive range; nieprawidlowe daty i `from > to` zwracaja walidacyjny blad UI, nie 500. Phase 2B: opcjonalny `cols` (comma-separated lub powtarzany) wybiera ktore **dozwolone** kolumny sa renderowane w tabeli; nieznane/ukryte/niedozwolone nazwy sa ignorowane, a gdy wszystkie sa nieprawidlowe nastepuje fallback do pelnego zestawu; `cols` wplywa tylko na SELECT renderowanej tabeli, a filtry/sort/search nadal dzialaja na pelnym dozwolonym zestawie; "Reset columns" usuwa `cols` (zachowujac filtry/search/sort/limit/density), zmiana kolumn resetuje `page`. **S5 (kolumny i stan URL, `docs/26`):** `colorder` (comma-separated) ustala kolejnosc kolumn — nieznane/niezatwierdzone nazwy sa odrzucane, duplikaty redukowane, a kolumny nienazwane sa dopisywane w kolejnosci katalogowej; `colw` (`kolumna:px`) ustala szerokosci ograniczone do 64–480 px, wartosci ujemne/zerowe/niecalkowite/niepoprawne sa odrzucane, a spoza zakresu przycinane; `colpin` (comma-separated) ustala przypiete kolumny w kolejnosci przypiecia, obecny-ale-pusty oznacza brak przypiec, a liczba i laczna szerokosc sa ograniczone (max 4, max 576 px); `colw__{column}` to pole jawnej szerokosci z menu kolumny, scalane do `colw` i konsumowane; `colsel` oznacza rzeczywisty submit panelu `DB-008` (pozwala odrzucic pusty wybor), `colpanel` utrzymuje panel otwarty bez JS. Wszystkie parametry S5 sa wylacznie prezentacyjne: nie trafiaja do SQL, nie zmieniaja zakresu eksportu i nie moga poszerzyc zestawu zatwierdzonych kolumn. **S6 (ukryta tozsamosc wiersza i panel szczegolow, `docs/29`):** `row` to **nieprzezroczysta referencja** (AES-256-GCM, zwiazana z `dataset_id`, `client_code` i kolumna identyfikatora) — nigdy surowy `record_id`; serwer renderuje panel `DB-006` po jej rozwiazaniu, a kazde zadanie ponownie wykonuje pelna autoryzacje (sesja, grant klienta, grant datasetu, `can_view_rows`), wiec sama referencja nie jest uprawnieniem. Referencja nieprawidlowa, zmodyfikowana, wystawiona dla innego datasetu/klienta albo wskazujaca wiersz usuniety lub niejednoznaczny daje jeden ogolny stan „niedostepny" bez ujawniania powodu. `rowfields=all` przelacza panel na wszystkie zatwierdzone kolumny. Skonfigurowany techniczny identyfikator wiersza jest wykluczony z `_get_portal_database_visible_columns`, wiec nie pojawia sie w tabeli, `cols`, stanie ukladu, sortowaniu, filtrach, rozkladach wartosci ani eksportach — nawet gdy katalog nadal ma dla niego `is_visible = true`. Phase 2C: gdy dataset ma admin-skonfigurowany row identifier (dokladnie jedna widoczna kolumna `is_row_identifier=true`), kazdy wiersz dostaje link **Details** do row detail route; identyfikator jest dolaczany do SELECT wewnetrznie tylko do zbudowania URL (nie jest renderowany, jesli nie wybrany w `cols`) |
| GET | `/user/database/datasets/{dataset_id}/rows/{row_id}` | UI session | path: `dataset_id`, `row_id` | **Wycofane w S6.** Trasa przyjmowala surowy techniczny identyfikator wiersza w sciezce, co go ujawnialo i dawalo rownolegla droge obejscia nieprzezroczystej referencji `?row=`. Po uwierzytelnieniu zwraca 404 (nie przekierowanie, ktore potwierdzaloby istnienie identyfikatora). Szczegoly wiersza sa dostepne wylacznie przez `?row=<referencja>` na trasie row browsera (`docs/29`) |
| GET | `/user/database/datasets/{dataset_id}/export` | UI session | query: `format=csv\|xlsx`, `limit?`, `sort?`, `direction?`, `filter__{column}?`, `op__{column}?`, `dateop__{date_column}?`, `date__{date_column}?`, `date_from__{date_column}?`, `date_to__{date_column}?`, legacy `date_from?`, `date_to?`, `search?`; legacy `page?` is ignored for row selection | Direct Database Explorer export for filtered rows; wymaga tego samego dostepu co row browser plus effective `can_export_rows=true`; eksportuje tylko widoczne kolumny i ma fixed cap 20,000 matching data rows. Export zawsze pobiera wszystkie rows pasujace do filtrow od offset 0; jesli liczba pasujacych rows przekracza 20,000, route zwraca walidacyjny blad zamiast cichego uciecia lub implicit queue. `search` i filtry dat uzywaja tej samej sciezki co row browser |
| POST | `/user/database/datasets/{dataset_id}/exports` | UI session | form: `format_name=csv\|xlsx`; query: same allowlisted filter/sort/search params as direct export, excluding page/limit/density/cols | Unified export action. For 0 through 20,000 matching data rows it returns the selected CSV/XLSX directly and does not create a persistent artifact. For 20,001 through 1,000,000 matching rows, after migration 043, it queues a background Database Explorer export without generating rows in the API process, writes `database_export_job_queued`, provisions the owner's `Database Exports` system folder when migration 044 is present, and redirects PRG to `/user/database/exports`. Before migration 043, the same large request stays on the dataset page with an inline unavailable notice and no queued job. Over-cap requests are rejected before queueing; permission-revoked jobs fail safely before generation |
| GET | `/user/database/exports` | UI session | - | Compatibility route. After migration 044, redirects to the canonical `/user/reports/database-exports/{folder_id}` when the signed-in user's system folder exists, or renders the same controlled system-folder page before first async queue. After migration 043 but before 044, lists only the signed-in user's `database_export_jobs`: Queued, Generating, Ready, Failed, Expired. Ready rows link to the requester-owned artifact; failed rows show only safe messages. Before migration 043, direct navigation redirects to `/user/database?background_exports_unavailable=1` |
| GET | `/admin` | UI admin | - | redirect do `/admin/users` |
| GET | `/admin/users` | UI admin | - | lista lokalnych UI users z admin flag, statusem, timestampami i przypisanymi rolami |
| GET | `/admin/users/new` | UI admin | - | formularz tworzenia lokalnego UI usera |
| POST | `/admin/users/new` | UI admin | form: `username`, `password`, `confirm_password`, `display_name?`, `is_admin?`, `role_ids?` | tworzy lokalnego UI usera; haslo jest hashowane helperem PBKDF2; waliduje duplicate username i zgodnosc hasel |
| GET | `/admin/users/{user_id}` | UI admin | path: `user_id` | ekran szczegolow lokalnego UI usera: status, admin access, reset hasla, assigned/available roles |
| POST | `/admin/users/{user_id}` | UI admin | form: `display_name?`, `is_active?`, `is_admin?` | aktualizuje bezpieczne pola konta; blokuje usuniecie/dezaktywacje ostatniego aktywnego admina |
| POST | `/admin/users/{user_id}/password` | UI admin | form: `new_password`, `confirm_password` | admin-driven password reset; nie ma email reset flow |
| POST | `/admin/users/{user_id}/roles` | UI admin | form: `role_id` | przypisuje istniejaca role z `artifact_roles` przez `artifact_user_roles`; idempotentne dla duplikatu |
| POST | `/admin/users/{user_id}/roles/{role_id}/remove` | UI admin | path: `user_id`, `role_id` | usuwa przypisanie roli; nie zmienia definicji roli ani permission modelu |
| GET | `/admin/groups` | UI admin | - | dashboard reusable portal groups; group access jest additive i nie zastępuje direct user assignments ani Artifact Explorer RBAC |
| GET | `/admin/groups/new` | UI admin | - | formularz tworzenia `portal_groups` |
| POST | `/admin/groups/new` | UI admin | form: `group_name`, `description?`, `is_active?` | tworzy portal group; waliduje wymagana i unikalna nazwe |
| GET | `/admin/groups/{group_id}` | UI admin | path: `group_id` | formularz edycji grupy, czlonkow, client access i podglad przypisanych folderow/datasetow |
| POST | `/admin/groups/{group_id}` | UI admin | form: `group_name`, `description?`, `is_active?` | aktualizuje metadata grupy; nieaktywny group nie grantuje effective access |
| POST | `/admin/groups/{group_id}/users` | UI admin | form: `user_id` | dodaje aktywnego local UI usera do grupy |
| POST | `/admin/groups/{group_id}/users/{user_id}/remove` | UI admin | path: `group_id`, `user_id` | usuwa usera z grupy; usuwa group-derived access bez zmiany direct assignments |
| POST | `/admin/groups/{group_id}/clients` | UI admin | form: `client_code` | przypisuje aktywnego klienta do aktywnej grupy z domyslnymi flagami database/reports true i export false |
| POST | `/admin/groups/{group_id}/clients/{client_code}/permissions` | UI admin | form: `can_view_database?`, `can_view_reports?`, `can_export_database?` | aktualizuje group-client flags uzywane w effective access |
| POST | `/admin/groups/{group_id}/clients/{client_code}/remove` | UI admin | path: `group_id`, `client_code` | usuwa client assignment z grupy |
| GET | `/admin/client-access` | UI admin | - | dashboard portal client registry i user-client assignments; nie zastepuje Artifact Explorer RBAC i nie grantuje SQL access |
| GET | `/admin/client-access/clients/new` | UI admin | - | formularz tworzenia `portal_clients` |
| POST | `/admin/client-access/clients/new` | UI admin | form: `client_code`, `display_name`, `description?`, `is_active?` | tworzy klienta portalu; normalizuje `client_code` do uppercase i waliduje `^[A-Z0-9_:-]{2,64}$` |
| GET | `/admin/client-access/clients/{client_code}` | UI admin | path: `client_code` | formularz edycji klienta portalu; `client_code` jest stale i nie jest zmieniane przez UI |
| POST | `/admin/client-access/clients/{client_code}` | UI admin | form: `display_name`, `description?`, `is_active?` | aktualizuje display name, opis i aktywnosc klienta |
| GET | `/admin/client-access/users/{user_id}` | UI admin | path: `user_id` | zarzadzanie przypisaniami klientow dla lokalnego UI usera |
| POST | `/admin/client-access/users/{user_id}/clients` | UI admin | form: `client_code` | przypisuje aktywnego klienta do usera z domyslnymi flagami `can_view_database=true`, `can_view_reports=true`, `can_export_database=false`; idempotentne dla duplikatu |
| POST | `/admin/client-access/users/{user_id}/clients/{client_code}/permissions` | UI admin | form: `can_view_database?`, `can_view_reports?`, `can_export_database?` | aktualizuje flagi przypisania user-client |
| POST | `/admin/client-access/users/{user_id}/clients/{client_code}/remove` | UI admin | path: `user_id`, `client_code` | usuwa przypisanie klienta do usera |
| GET | `/admin/client-access/database` | UI admin | - | dashboard katalogu `portal_database_datasets`; rejestruje tylko allowlistowane dataset references dla przyszlego Client Database Explorer |
| GET | `/admin/client-access/database/new` | UI admin | query opcjonalnie: `client_code?`, `schema_name?`, `table_name?` | formularz tworzenia datasetu bazy klienta z server-side guided selectors: aktywni klienci, widoczne non-system schemas, tabele/views dla schematu i date/time columns dla wybranej tabeli |
| POST | `/admin/client-access/database/new` | UI admin | form: `client_code`, `dataset_name`, `slug`, `description?`, `schema_name`, `schema_name_manual?`, `table_name`, `table_name_manual?`, `default_date_column?`, `is_active?` | tworzy wpis `portal_database_datasets`; waliduje `client_code`, slug i bezpieczne identyfikatory `schema_name`/`table_name`/`default_date_column`; manual fallback nadal musi byc pojedynczym bezpiecznym identyfikatorem |
| GET | `/admin/client-access/database/{dataset_id}` | UI admin | path: `dataset_id`; query opcjonalnie: `client_code?`, `schema_name?`, `table_name?` | formularz edycji datasetu, kolumn katalogu oraz bezposrednich i grupowych przypisan; pokazuje guided metadata diagnostics, gdy fizyczne kolumny nie sa widoczne |
| POST | `/admin/client-access/database/{dataset_id}` | UI admin | form: pola datasetu | aktualizuje metadata datasetu i allowlistowana physical table reference; schema/table change jest blokowana, gdy istnieja catalog columns, aby uniknac stalej metadata; nie odpytuje row values i nie modyfikuje fizycznej tabeli |
| POST | `/admin/client-access/database/{dataset_id}/deactivate` | UI admin | path: `dataset_id` | soft-remove datasetu z portalu przez `portal_database_datasets.is_active=false`; nie usuwa assignments, audit history, fizycznej tabeli ani danych klienta |
| POST | `/admin/client-access/database/{dataset_id}/columns` | UI admin | form: `column_name`, `display_name`, `data_type?`, `is_visible?`, `is_filterable?`, `is_sortable?`, `is_default_date_column?`, `display_order?` | dodaje albo aktualizuje kolumne katalogu w `portal_database_dataset_columns`; tylko metadata portalu |
| POST | `/admin/client-access/database/{dataset_id}/columns/discovered` | UI admin | form: `column_names`, `is_visible?`, `is_filterable?`, `is_sortable?`, `default_date_column?` | guided add: odczytuje tylko metadata kolumn z `information_schema.columns` dla zarejestrowanego datasetu, dodaje wybrane kolumny do katalogu z wygenerowana etykieta i znormalizowanym typem; nie czyta row values |
| POST | `/admin/client-access/database/{dataset_id}/columns/{column_name}/remove` | UI admin | path: `dataset_id`, `column_name` | usuwa kolumne tylko z katalogu portalu, nie z fizycznej bazy |
| POST | `/admin/client-access/database/{dataset_id}/users` | UI admin | form: `user_id` | przypisuje dataset userowi tylko jesli ma effective `can_view_database=true` dla klienta datasetu |
| POST | `/admin/client-access/database/{dataset_id}/users/{user_id}/permissions` | UI admin | form: `can_view_rows?`, `can_filter_rows?`, `can_export_rows?` | aktualizuje per-user dataset flags; `can_export_rows=true` wlacza Phase 3C CSV/XLSX export dla tego dataset-user assignment |
| POST | `/admin/client-access/database/{dataset_id}/users/{user_id}/remove` | UI admin | path: `dataset_id`, `user_id` | usuwa przypisanie dataset-user |
| POST | `/admin/client-access/database/{dataset_id}/groups` | UI admin | form: `group_id` | przypisuje aktywna grupe tylko jesli grupa ma `can_view_database=true` dla klienta datasetu |
| POST | `/admin/client-access/database/{dataset_id}/groups/{group_id}/permissions` | UI admin | form: `can_view_rows?`, `can_filter_rows?`, `can_export_rows?` | aktualizuje per-group dataset flags; lacza sie z direct flags przez OR semantics |
| POST | `/admin/client-access/database/{dataset_id}/groups/{group_id}/remove` | UI admin | path: `dataset_id`, `group_id` | usuwa przypisanie dataset-group bez zmiany direct user assignments |
| GET | `/admin/report-folders` | UI admin | - | dashboard customer-facing `portal_report_folders` |
| GET | `/admin/report-folders/new` | UI admin | - | formularz tworzenia report folder |
| POST | `/admin/report-folders/new` | UI admin | form: `client_code`, `folder_name`, `slug`, `description?`, `is_active?`, `can_preview?`, `can_download?`, allowlistowane pola filtrow | tworzy folder raportow dla aktywnego `portal_clients.client_code`; filtr JSON nie przyjmuje `client_code` |
| GET | `/admin/report-folders/{folder_id}` | UI admin | path: `folder_id` | formularz edycji folderu oraz bezposrednich i grupowych przypisan |
| POST | `/admin/report-folders/{folder_id}` | UI admin | form: pola folderu i allowlistowane pola filtrow | aktualizuje folder, flagi preview/download i `search_query_json` |
| POST | `/admin/report-folders/{folder_id}/users` | UI admin | form: `user_id` | przypisuje usera tylko jesli ma effective `can_view_reports=true` dla klienta folderu |
| POST | `/admin/report-folders/{folder_id}/users/{user_id}/remove` | UI admin | path: `folder_id`, `user_id` | usuwa przypisanie user-folder |
| POST | `/admin/report-folders/{folder_id}/groups` | UI admin | form: `group_id` | przypisuje aktywna grupe tylko jesli grupa ma `can_view_reports=true` dla klienta folderu |
| POST | `/admin/report-folders/{folder_id}/groups/{group_id}/remove` | UI admin | path: `folder_id`, `group_id` | usuwa przypisanie folder-group bez zmiany direct user assignments |
| GET | `/admin/report-folders/{folder_id}/preview` | UI admin | path: `folder_id` | pokazuje do 50 pasujacych artefaktow po efektywnych filtrach folderu, bez storage keys i technical lineage |
| GET | `/admin/audit` | UI admin | query: `event_type?`, `actor?`, `client_code?`, `dataset_id?`, `report_folder_id?`, `date_from?`, `date_to?`, `page?`, `limit?` | read-only lista `portal_audit_events` z filtrami i paginacja; max `limit=500`; metadata jest sanityzowane i nie pokazuje raw SQL, sekretow, hasel, tokenow ani row values |
| GET | `/artifact-explorer` | UI session + `can_view` | query: exact filters `workflow_name?`, `stage_name?`, `artifact_role?`, `report_type?`, `original_filename?`, `display_filename?`, `file_ext?`, `client_code?`, `kind?`, `tag?`, `layout_version?`, `run_id?`, `raw_file_id?`; contains filters `<field>_search?`; plus `search?`, `date_from?`, `date_to?`, `limit?`, `offset?`, `sort?` | server-rendered UI: lista ograniczona RBAC, searchable multi-select/exact filtry z typed contains fallback, sortowanie nagłówkami tabeli, paginacja |
| GET | `/artifact-explorer/artifacts/{artifact_id}` | UI session + `can_view`; preview/download/edit gated separately | path: `artifact_id` | server-rendered detail + lineage + annotation forms + preview section zależne od permission |
| POST | `/artifact-explorer/artifacts/{artifact_id}/metadata` | UI session + `can_edit_annotations` | form: `description`, `metadata_json` | zapis manualnej adnotacji przez UI |
| POST | `/artifact-explorer/artifacts/{artifact_id}/tags` | UI session + `can_edit_annotations` | form: `tag` | dodaje tag przez UI |
| POST | `/artifact-explorer/artifacts/{artifact_id}/tags/{tag}/delete` | UI session + `can_edit_annotations` | path: `tag` | usuwa tag przez UI |
| GET | `/artifact-explorer/folders` | UI session | - | root virtual folder navigation; logged-in users can browse folder names |
| GET | `/artifact-explorer/export` | UI session + `can_view` | query: the same filters and `sort` as `/artifact-explorer`; plus `format=xlsx\|csv` | XLSX/CSV of the **whole current filtration**, not the visible page: `limit`/`offset` are dropped, the columns are exactly the table's columns, and rows pass the same `action="view"` predicate. Above `ARTIFACT_EXPLORER_MAX_EXPORT_ROWS` (50 000) returns `413` naming the count and the limit; an unsupported `format` returns `400`. |
| GET | `/artifact-explorer/folders/{folder_id}/export` | UI session + `can_view` applied to artifact rows | query: `sort?`, `format=xlsx\|csv` | the same export for a virtual folder, taking the branch the folder's page takes — a smart folder exports what its saved criteria currently resolve to. |
| GET | `/artifact-explorer/folders/{folder_id}` | UI session + `can_view` applied to artifact rows | query: `limit?`, `offset?`, `sort?` | breadcrumbs, child folders and artifacts in the folder, with artifacts filtered by local RBAC |
| POST | `/artifact-explorer/folders` | UI admin | form: `parent_folder_id?`, `folder_name`, `description?`, `folder_type`, opcjonalne pola kryteriów smart (CSV) | tworzy root lub child folder; smart: kryteria → `search_query_json` |
| POST | `/artifact-explorer/folders/{folder_id}/metadata` | UI admin | form: `folder_name`, `description?`; dla smart także pola kryteriów | aktualizuje folder i kryteria smart |
| POST | `/artifact-explorer/folders/{folder_id}/delete` | UI admin | form: `redirect_to?` | usuwa metadata folderu i relacje; nie usuwa artefaktów |
| POST | `/artifact-explorer/artifacts/{artifact_id}/folders` | UI admin | form: `folder_id` | dodaje artefakt tylko do **manual** folderu (dropdown z pełnymi ścieżkami; smart wykluczone) |
| POST | `/artifact-explorer/artifacts/{artifact_id}/folders/{folder_id}/remove` | UI admin | path params | usuwa tylko relację artefakt-folder |
| GET | `/artifact-explorer/artifacts/{artifact_id}/download` | UI session + `can_download` | path: `artifact_id`; query: `disposition=attachment|inline` | same-origin download dla UI, oparty o zapisany `artifact_id` i `storage_key` |
| POST | `/maintenance/prune` | write | query: `days` (default `60`), `dry_run` (default `true`) | read-only platform-retention plan; `dry_run=false` is rejected with 409 |


## `POST /suspected-bugs` — durable defect reporting

`suspected_bug` is an **error classification carried in `logs.context.classification`**, never a run status and never a `logs.level` value. The log level is always `ERROR`; the run that detected the anomaly keeps whatever status its own business contract dictates (`SUCCESS`, `FAILED`, partial, blocked).

The endpoint is a transport over `api.suspected_bug.report_suspected_bug(conn, event)`. Host-side jobs and operator scripts that already hold a platform-DB connection call the same function directly; both paths execute the identical SQL, so there is exactly one contract with two transports.

`event_json` accepts the fields of `SuspectedBugEvent`. Required: `incident_code` (UPPER_SNAKE_CASE), `title`, `summary`, `occurred_at` (timezone-aware ISO 8601), `environment`, `component`. `classification` must be exactly `suspected_bug` and `severity` one of `warning` / `error` / `critical`. Everything else is optional provenance (`workflow_name`, `stage_name`, `job_name`, `client_id`, `client_code`, `run_id`, `report_type`, `dataset_name`, `database_name`, `schema_name`, `table_name`, `raw_file_id`, `file_id`, `raw_artifact_id`, `normalized_artifact_id`, `cleaned_artifact_id`, `stage3_result_artifact_id`, `subject_type`, `subject_key`, `subject_value`, `affected_record_count`, `affected_period_start`, `affected_period_end`, `processing_outcome`, `rows_modified`, `details`, `evidence`, `suggested_action`, `fingerprint_fields`, `exception_type`, `stack_trace`). Missing optional provenance is represented explicitly as `null` and never makes the call fail. Invalid payloads return `400`; unknown fields are rejected.

One transaction writes: the `ERROR` log row, the incident upsert (`suspected_bug_incidents`, unique per fingerprint), the occurrence (`suspected_bug_occurrences`, linked to the log row) and — only when the alert policy allows it — one `suspected_bug_email_outbox` row. Delivery happens later in `ops/suspected_bug_email_worker.py`; the API never sends email.

Response (`SuspectedBugReportResult`): `reported`, `log_id`, `incident_id`, `fingerprint`, `occurrence_id`, `occurrence_count`, `incident_created`, `email_enqueued`, `email_suppressed`, `suppression_reason` (`alerts_disabled` / `recipients_not_configured` / `cooldown` / `duplicate_notification_key`), `notification_reason` (`new` / `material_change` / `reminder`), `outbox_id`, `error`.

Payloads are sanitized server-side before storage: secret-shaped keys and values are redacted, strings/lists/nesting are bounded, and truncation is marked with `truncated=true`. Recipients come from this process' own configuration (`SUSPECTED_BUG_ALERT_TO`); there is no recipient fallback. With no recipient configured the log and incident are still persisted and only the email is suppressed.

## Portal database catalog, row browsing and export (Phase 3A / 3B / 3C)

`portal_database_datasets`, `portal_database_dataset_columns`, `portal_database_dataset_users` i `portal_database_dataset_groups` tworza katalog Client Database Explorer. Katalog jest allowlista datasetow: zawiera `client_code`, customer-friendly nazwe, opis, slug, bezpieczna referencje `schema_name.table_name`, opcjonalny `default_date_column`, widoczne kolumny i per-user flags `can_view_rows`, `can_filter_rows`, `can_export_rows`. Admin edit page moze podpowiadac kolumny przez `information_schema.columns` tylko dla tej zapisanej referencji datasetu; schema/table sa walidowane jako pojedyncze identyfikatory i przekazywane jako bind params. Kolumny dodawane z guided physical-column picker sa walidowane przeciwko odkrytej allowliscie `information_schema.columns` dla wybranego datasetu, wiec `portal_database_dataset_columns.column_name` moze przechowywac realne fizyczne nazwy wymagajace quoted identifiers (np. spacje, myslniki, wielkie litery lub znaki spoza ASCII). Ten odczyt metadata nie pokazuje wartosci rows, storage keys, tokenow, DSN ani raw SQL.

Multi-database data access: `logdb` jest control-plane/system DB; per-client business data zyje w osobnych bazach. `portal_clients.database_name` mapuje portal client na nazwe jego business database (allowlistowana nazwa bazy PostgreSQL, nie DSN i bez credentials). Zarowno admin metadata discovery (schemas/tables/columns) jak i user-facing row browsing/export lacza sie z ta zmapowana client database, nie z `logdb`: connection helper uzywa platform Postgres host/port/user/password i podmienia tylko nazwe bazy, w read-only session. Gdy klient nie ma skonfigurowanego `database_name`, dataset create selector zwraca konkretny diagnostic ("This client does not have a database name configured...") zamiast generycznego "schemas could not be loaded". Wymagane uprawnienia roli portalu w client database: `CONNECT` na bazie, `USAGE` na approved schemas, `SELECT` na approved tables/views oraz `information_schema` metadata visibility. Portal nie wykonuje auto-grantow, DDL, schema mutation ani client DB writes.

Phase 3B udostepnia read-only row browsing przez `/user/database/datasets/{dataset_id}`. Po Phase 4B dostep wymaga aktywnego usera, aktywnego klienta, effective client `can_view_database=true`, aktywnego datasetu oraz direct albo active-group dataset assignment z effective `can_view_rows=true`. SELECT list zawiera wylacznie kolumny z `portal_database_dataset_columns.is_visible=true`; nie ma `SELECT *`.

Phase 3C udostepnia bezposredni `/user/database/datasets/{dataset_id}/export` dla tego samego modelu query, ale wymaga dodatkowo `can_export_rows=true`. CSV i XLSX sa generowane read-only z tych samych widocznych kolumn i tej samej allowlistowanej sciezki sort/filter/date co row browser. Direct export ma fixed cap 20,000 matching data rows; query `limit` nie moze obnizyc ani podniesc tego capu i nie jest liczba rows do cichego uciecia. Jesli pelny filtrowany wynik przekracza cap, direct route zwraca walidacyjny blad i nie kolejkuje joba. Unified UI export uzywa explicit `POST /user/database/datasets/{dataset_id}/exports`: 0-20,000 rows zwraca wybrany plik bezposrednio bez trwalego artefaktu, a po migracji 043 zakres 20,001 through 1,000,000 data rows trafia do durable queue i worker poza Uvicorn z 3-dniowa retencja oraz PRG do `/user/database/exports`. Po migracji 044 pierwsze takie queue action tworzy albo odswieza jeden system folder `Database Exports` dla wlasciciela (`database_export_system_folders`, unique `(owner_user_id, system_key)`) i zapisuje `database_export_jobs.system_folder_id`; `/user/database/exports` pozostaje kompatybilny, ale kanoniczny widok jest w Reports pod `/user/reports/database-exports/{folder_id}`. Przed migracja 043 normalne linki do `/user/database/exports` sa ukryte, duzy POST zostaje na stronie datasetu z inline unavailable notice, a direct navigation do `/user/database/exports` redirectuje do `/user/database?background_exports_unavailable=1`. Po migracji 043, ale przed 044, kolejka i legacy `/user/database/exports` pozostaja bezpieczne; aplikacja nie odwoluje sie do brakujacego `system_folder_id`. Filename jest sanityzowany jako `client__dataset__timestamp__export.{csv,xlsx}`.

Phase 2A (UI-only) ulepsza row browser: global `search` po widocznych filterable text columns (parametryzowany), active filter chips z precyzyjnym usuwaniem pojedynczego filtra, "Reset filters" zachowujacy sort/direction/page size/density, date presets mapujace na legacy `date_from`/`date_to` dla `default_date_column` (dla kolumn timestamp koniec zakresu jest inkluzywny do `T23:59:59`), sticky table header, density toggle i page-size selector. Date-like column filters uzywaja `dateop__{column}=older|newer|range` z odpowiednio `date__{column}` albo `date_from__{column}`/`date_to__{column}`; range jest inclusive i moze byc jednostronnie pusty jako open-ended. Formularz eksportu w UI celowo nie przekazuje on-screen page size, wiec direct export cap pozostaje 20,000 niezaleznie od `limit` w widoku; audit metadata dodaje tylko flage `search_present` (bez tresci zapytania).

Phase 2B (UI-only) dodaje URL-based column visibility (`cols`). `display_columns` to wybrany podzbior, a `allowed_columns` (wszystkie `is_visible=true`) nadal rzadzi filtrami, sortowaniem, search i guardem widocznosci; `cols` jest zawsze przecinany z `allowed_columns`, wiec nie moze poszerzyc dostepu ani ujawnic ukrytych kolumn (SELECT, WHERE, ORDER BY, export, chips, metadata pozostaja na dozwolonym zestawie). Sort/filtr po dozwolonej kolumnie spoza widoku nadal dziala. **Export celowo ignoruje `cols`** i zawsze zawiera wszystkie zatwierdzone kolumny (UI to komunikuje), aby nie zmieniac semantyki eksportu. Audit metadata dodaje tylko flage `cols_selected` (bez nazw kolumn). Phase 2C wprowadza **admin-konfigurowany row identifier**: migracja `039_portal_database_row_identifier.sql` dodaje `portal_database_dataset_columns.is_row_identifier boolean not null default false` oraz partial unique index na `(dataset_id) WHERE is_row_identifier IS TRUE` (najwyzej jeden identyfikator na dataset, idempotentnie). Aplikacja wymaga, by identyfikator byl kolumna `is_visible=true` (walidacja przy zapisie + czyszczenie flagi, gdy kolumna staje sie ukryta); resolver bierze pod uwage tylko widoczne kolumny. Admin UI datasetu ma kontrolke "Row identifier" (select widocznych kolumn + clear) oraz badge "Row ID" w tabeli kolumn. Row detail route `GET /user/database/datasets/{dataset_id}/rows/{row_id}` pokazuje wszystkie dozwolone widoczne pola pojedynczego wiersza, **nigdy** nie ujawnia ukrytych kolumn, uzywa parametryzowanego SQL i bezpiecznie obsluguje brak identyfikatora (404), brak wiersza (404) i nie-unikalny identyfikator (409). Identyfikatory nie sa fabrykowane z numeru wiersza, `ctid` ani dopasowania wszystkich kolumn. Eksport nadal ignoruje `cols` i nie zalezy od row detail.

Identyfikatory `schema_name` i `table_name` musza pasowac do `^[A-Za-z_][A-Za-z0-9_]{0,62}$`. Catalog `column_name`, `sort`, `filter` i `default_date_column` sa walidowane allowlistowo: request moze uzyc tylko kolumn juz zatwierdzonych dla datasetu, a guided admin save moze dodac tylko kolumny obecne w odkrytym `information_schema.columns` dla wybranej tabeli. SQL renderer podwojnie cytuje zatwierdzone nazwy kolumn jako PostgreSQL identifiers. Wartosc filtrow jest przekazywana jako parametry DB, a sort/filter columns musza byc katalogowo `is_sortable` / `is_filterable`. Obslugiwane operatory legacy: text/unknown `contains|eq|neq`; numeric/date `eq|neq|gt|gte|lt|lte`. Nowe date-like filters: `older` (`<`), `newer` (`>`), `range` (`>=`/`<=`, inclusive, jednostronnie pusty jako open-ended). Legacy `date_from`/`date_to` nadal filtruje widoczna/filterable `default_date_column`. Pagination row browsera ma default `limit=100`, minimum 10 i hard max 500; export ignoruje `page` dla SELECT offsetu.

UI nie przyjmuje SQL fragments, kropek wewnatrz pojedynczego identyfikatora, cudzyslowow, komentarzy, funkcji, operatorow ani JSON path expressions. Phase 3B/3C nie mutuje schematow klientow i nie wykonuje write operations.

`portal_audit_events` zapisuje Phase 3C export attempts: `database_export_success`, `database_export_denied`, `database_export_validation_failed`, `database_export_failed`. Phase 4A rozszerza zdarzenia o `auth_login_success`, `auth_login_failed`, `auth_logout`, `portal_access_denied`, `admin_access_denied`, `report_folder_access_denied`, `database_dataset_access_denied`, admin user/client/report-folder/database-catalog changes, `report_folder_viewed`, report artifact preview/download success/denied oraz `database_rows_viewed`, `database_rows_validation_failed`, `database_rows_failed`. Phase 4B dodaje audit events dla create/update grup, czlonkostwa, group-client permissions oraz group assignments do report folders i database datasets. Metadata audytu zawiera tylko bezpieczne pola, takie jak changed fields, target user/client/dataset/folder ids, format, row limit/count, sort, direction, filter keys i date range; nie zapisuje raw SQL, connection strings, sekretow, hasel, tokenow ani wartosci rows. Audit writes sa non-blocking: blad insertu jest logowany na serwerze i nie powinien przerywac glownej akcji. `/admin/audit` pokazuje read-only liste z filtrami `event_type`, `actor`, `client_code`, `dataset_id`, `report_folder_id`, `date_from`, `date_to` oraz paginacja `page`/`limit`.

## Artefakty: layout fizyczny i metadata

MinIO pozostaje fizycznym backendem plików, a tabela `artifacts` w Postgresie jest indeksem metadanych. Nowe uploady używają `layout_version=2`; stare wiersze i stare obiekty mogą pozostać w dotychczasowym układzie (`layout_version=1` albo brak nowych pól w historycznym snapshotcie).

API przyjmuje semantyczne pola multipart i centralnie buduje `storage_key`:

```text
{workflow_name}/{stage_name}/yyyy={YYYY}/mm={MM}/dd={DD}/run_id={run_id}/{optional_report_type_segment}/{artifact_role}/{display_filename}
```

`optional_report_type_segment` ma postać `report_type=report_207`, gdy typ raportu jest znany; dla `unknown` segment jest pomijany. Minimalny, stary upload bez pól semantycznych nadal działa, ale API przypisuje domyślne wartości (`workflow_unknown`, `stage_unknown`, `artifact`) i zapisuje artefakt jako layout v2.

Standardowy `display_filename`:

```text
{report_type_or_unknown}__{timestamp_utc}__{raw_file_short_id_or_na}__{artifact_role}.{ext}
```

Przykład:

```text
report_207__20260512T221144Z__ac3866ec__cleaned.csv
```

`client_code` jest opcjonalnym, operator-friendly kodem klienta używanym m.in. przez filtrowanie i Artifact Explorer UI RBAC. Pole może być `NULL` dla starych artefaktów i dla obecnych jobów, które nie mają bezpiecznego źródła kodu klienta.

Portal report folders (Phase 2C/4B) używają osobnych tabel `portal_report_folders`, `portal_report_folder_users` i `portal_report_folder_groups`. Folder ma własny `client_code`, `slug`, `search_query_json`, flagi `is_active`, `can_preview`, `can_download` oraz audyt create/update. `search_query_json` ma ścisły allowlist: `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `file_ext`, `tag`, `search`, `date_from`, `date_to`. Admin form moze pokazywac istniejace facet values jako sugestie dla tych allowlistowanych pol, ale zapisuje ten sam walidowany JSON. `client_code` nie jest przyjmowany z JSON, tylko wymuszany z folderu. Foldery portalu nie zastępują `artifact_virtual_folders` ani Artifact Explorer RBAC.

Database Exports system folders are separate from both `portal_report_folders` and `artifact_virtual_folders`. They live in `database_export_system_folders`, have fixed `system_key='database_exports'`, are owned by one `artifact_users.user_id`, and are provisioned only by async export queueing. The portal exposes no create/rename/delete/move controls for these folders; queueing uses an idempotent upsert that restores the project-defined folder name, slug, and description for the owner's stable system key.

`original_filename` przechowuje nazwę źródłową oddzielnie od nazwy wyświetlanej. `artifacts.metadata_json` jest systemowym metadata zapisanym przy uploadzie. Manualne adnotacje operatorów są przechowywane oddzielnie i nie nadpisują systemowego metadata.

### Manualne adnotacje artefaktów

Phase 5 dodaje dwie tabele:

- `artifact_metadata_overrides` — jeden wiersz na artefakt: `description`, manualne `metadata_json`, audyt `created_by/updated_by`.
- `artifact_tags` — wiele tagów na artefakt; `PRIMARY KEY (artifact_id, tag)`.
- `artifact_virtual_folders` — drzewo folderów: `folder_name`, stabilny `slug`, parent, `folder_type` (`manual` \| `smart`), `search_query_json` (filtry dla smart), opis i audyt.
- `artifact_virtual_folder_items` — przypisania **manual**; smart foldery nie wymagają wierszy (zawartość dynamiczna).

Tagi są normalizowane do lowercase, trimowane, mają maksymalnie 64 znaki i muszą pasować do prostego alfabetu: `a-z`, `0-9`, `_`, `.`, `:`, `-`, zaczynając od litery lub cyfry. Przykład: `reviewed`, `speeding`, `client:a`.

`created_by` / `updated_by`: dla tokenowego API zapisywana jest wartość w rodzaju `api_write_token`; dla lokalnych formularzy UI po zalogowaniu `artifact_explorer:<username>`.

## Artifact Browser API

Artifact Browser API jest warstwą nad tabelą `artifacts`, manualnymi adnotacjami, folderami wirtualnymi i MinIO. Nie przenosi obiektów, nie usuwa artefaktów i nie stosuje per-user UI RBAC; tokeny `API_READ_TOKEN` / `API_WRITE_TOKEN` są traktowane jako machine-level access. Stare artefakty z `layout_version=1` albo `NULL` w polach layoutu nadal są listowane. Endpointy edycji manualnych adnotacji i zarządzania folderami wirtualnymi wymagają tokena write.

### `GET /artifact-browser/artifacts`

Zwraca:

```json
{
  "data": [
    {
      "artifact_id": "...",
      "kind": "REPORT",
      "workflow_name": "workflow_b",
      "stage_name": "stage_2_clean",
      "artifact_role": "cleaned",
      "report_type": "report_207",
      "client_code": "CLIENT_A",
      "layout_version": 2,
      "run_id": "...",
      "raw_file_id": "...",
      "filename": "...",
      "display_filename": "...",
      "original_filename": "...",
      "storage_key": "...",
      "bucket_name": "artifacts",
      "content_type": "text/csv",
      "file_ext": "csv",
      "size_bytes": 12345,
      "sha256": "...",
      "metadata_json": {},
      "description": "Reviewed report 207 output",
      "manual_metadata_json": {"review_status": "ok"},
      "tags": ["reviewed", "speeding"],
      "created_at": "..."
    }
  ],
  "meta": {"limit": 50, "offset": 0, "count": 50, "total": 123}
}
```

Obsługiwane filtry exact: `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `original_filename`, `display_filename`, `client_code`, `tag`, `run_id`, `raw_file_id`, `layout_version`, `file_ext`, `kind`, `content_type`, plus `date_from`, `date_to` and global `search`. Field-specific contains filters use `<field>_search`, e.g. `original_filename_search=207` or `report_type_search=207`.

Canonical multi-value syntax uses repeated query params for finite filters:

```text
/artifact-browser/artifacts?workflow_name=workflow_b&workflow_name=workflow_a&report_type=report_207
```

The implementation also accepts comma-separated values for convenience:

```text
/artifact-browser/artifacts?workflow_name=workflow_b,workflow_a
```

Multi-value exact filtering is supported for `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `original_filename`, `display_filename`, `file_ext`, `client_code`, `layout_version`, `kind`, and `tag`; API exact filters also accept `content_type`. Tag filter semantics are OR: `?tag=reviewed&tag=speeding` returns artifacts with either tag.

For field-specific contains search, use `<field>_search` for text-like filter fields: `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `original_filename`, `display_filename`, `client_code`, `run_id`, `raw_file_id`, `layout_version`, `file_ext`, `kind`, `content_type`, and `tag`. Matching is case-insensitive SQL `ILIKE`. Exact selected values take precedence for the same field; when `report_type=report_207&report_type_search=250` is sent, only exact `report_type=report_207` is applied and `_search` only represents UI option narrowing.

`search` remains the global search across safe text fields: `filename`, `display_filename`, `original_filename`, `storage_key`, `workflow_name`, `stage_name`, `artifact_role`, `report_type`.

Paginacja: `limit` default `50`, max `500`; `offset` default `0`.

Sortowanie jest allowlistowane; inne wartości są odrzucane `400`, bez interpolowania do SQL. Obsługiwane wartości:

- legacy/summary: `created_at_desc` (default), `created_at_asc`, `workflow_stage`, `report_type`,
- per-column asc/desc: `workflow_name_asc`, `workflow_name_desc`, `stage_name_asc`, `stage_name_desc`, `artifact_role_asc`, `artifact_role_desc`, `report_type_asc`, `report_type_desc`, `client_code_asc`, `client_code_desc`, `file_ext_asc`, `file_ext_desc`, `size_bytes_asc`, `size_bytes_desc`, `layout_version_asc`, `layout_version_desc`, `filename_asc`, `filename_desc`.

### `GET /artifact-browser/artifacts/facets`

Zwraca distinct, non-NULL wartości dostępne dla dropdownów Artifact Explorer:

```json
{
  "workflow_name": ["workflow_a", "workflow_b"],
  "stage_name": ["stage_1_fetch", "stage_2_clean"],
  "artifact_role": ["cleaned", "debug_sample", "raw"],
  "report_type": ["report_207"],
  "file_ext": ["csv", "txt", "xlsx"],
  "client_code": ["CLIENT_A", "CLIENT_B"],
  "layout_version": [1, 2],
  "tags": ["reviewed", "speeding"]
}
```

### `GET /artifact-browser/artifacts/{artifact_id}`

Zwraca obiekt:

- `artifact` — pełne metadata artefaktu w kształcie listy,
- `run` — `run_id`, `source`, `status`, `started_at`, `ended_at`, `params` z redakcją pól wyglądających na sekrety,
- `raw_file` — jeśli `raw_file_id` istnieje: `id`, `original_filename`, `report_key`, `status`, `normalized_path`, `stage2_status`, `stage2_report_type`, `stage2_scores`, `stage2_pending_reason`.
- `virtual_folders` — manualne + ewentualnie smart (`membership_type`: `manual` \| `smart_dynamic`); `path`, `folder_type`, `search_query_json`.
- manualne pola w `artifact`: `description`, `manual_metadata_json`, `tags`.

Endpoint nie zwraca zawartości pliku.

### Virtual folders

Virtual folders są wyłącznie warstwą metadata/navigation. Nie zmieniają fizycznego `storage_key`, nie przenoszą ani nie kopiują obiektów MinIO, nie usuwają artefaktów i nie zastępują widoku filtrów/search.

**Manual (`folder_type=manual`)** — artefakty są przypisywane jawnie przez `artifact_virtual_folder_items`. Jeden artefakt może należeć do wielu manual folderów.

**Smart (`folder_type=smart`)** — zawartość jest liczona dynamicznie z `search_query_json` (allowlista tych samych filtrów co `GET /artifact-browser/artifacts`). Nie wstawia się dopasowanych artefaktów do `artifact_virtual_folder_items`. Ręczne `POST/DELETE .../artifacts` dla smart folderu zwraca `400`.

`folder_name` jest nazwą display. `slug` jest generowany przy tworzeniu z `folder_name`, jest URL-safe i pozostaje stabilny przy rename. Duplikaty nazw albo slugów wśród sibling folders są odrzucane `409`.

Pełna ścieżka w UI (np. dropdown „Add to manual folder”) używa segmentów oddzielonych ` / `, aby rozróżnić identyczne nazwy w różnych gałęziach.

Przykładowe odpowiedzi:

```json
{
  "data": [
    {
      "folder_id": "...",
      "parent_folder_id": null,
      "folder_name": "Raporty FleetWeb",
      "slug": "raporty-fleetweb",
      "description": "FleetWeb report artifacts",
      "folder_type": "manual",
      "search_query_json": {},
      "children_count": 3,
      "artifacts_count": 12,
      "created_at": "...",
      "updated_at": "..."
    }
  ]
}
```

Folder detail zawiera `folder`, `breadcrumbs`, `child_folders` oraz `artifacts` w tym samym kształcie `data/meta` co `GET /artifact-browser/artifacts`. Query `sort` używa tej samej allowlisty co lista artefaktów; invalid sort jest odrzucany `400`.

Przykłady:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $API_WRITE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"folder_name":"Reviewed report 207","folder_type":"smart","search_query_json":{"workflow_name":["workflow_b"],"stage_name":["stage_2_clean"],"report_type":["report_207"],"tag":["reviewed"]}}' \
  "http://127.0.0.1:8000/artifact-browser/virtual-folders" \
  | python3 -m json.tool

curl -sS -X POST \
  -H "Authorization: Bearer $API_WRITE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"folder_name":"Raporty FleetWeb","folder_type":"manual","description":"FleetWeb report artifacts"}' \
  "http://127.0.0.1:8000/artifact-browser/virtual-folders" \
  | python3 -m json.tool

curl -sS -X POST \
  -H "Authorization: Bearer $API_WRITE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"artifact_id":"<artifact_id>"}' \
  "http://127.0.0.1:8000/artifact-browser/virtual-folders/<folder_id>/artifacts" \
  | python3 -m json.tool

curl -sS \
  -H "Authorization: Bearer $API_READ_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/virtual-folders/<folder_id>" \
  | python3 -m json.tool

curl -sS -X DELETE \
  -H "Authorization: Bearer $API_WRITE_TOKEN" \
  "http://127.0.0.1:8000/artifact-browser/virtual-folders/<folder_id>/artifacts/<artifact_id>"
```

### `PATCH /artifact-browser/artifacts/{artifact_id}/metadata`

Wymaga `API_WRITE_TOKEN`. Upsertuje manualną adnotację bez zmiany pliku w MinIO i bez zmiany `artifacts.metadata_json`.

Request:

```json
{
  "description": "Reviewed report 207 output",
  "metadata_json": {"review_status": "ok"}
}
```

`metadata_json` musi być obiektem JSON. Endpoint zwraca aktualny detail payload artefaktu.

### Tags

`POST /artifact-browser/artifacts/{artifact_id}/tags` wymaga `API_WRITE_TOKEN` i przyjmuje:

```json
{"tag": "reviewed"}
```

Tag jest normalizowany do lowercase. Ponowne dodanie tego samego tagu jest idempotentne. `DELETE /artifact-browser/artifacts/{artifact_id}/tags/{tag}` usuwa wyłącznie relację tagu i jest idempotentne względem braku tagu; brak artefaktu nadal zwraca `404`.

### Download

`GET /artifact-browser/artifacts/{artifact_id}/download` pobiera obiekt z MinIO po zapisanym `storage_key`. `Content-Disposition` używa `display_filename`, a jeśli go brak — `filename`. Query `disposition=inline` pozwala przyszłemu UI osadzić PDF/tekst w przeglądarce; default to `attachment`. Brak rekordu artefaktu zwraca `404`; brak obiektu w storage zwraca czytelne `404`, a inny błąd storage `502`.

### `GET /artifact-browser/artifacts/{artifact_id}/preview`

Read-only endpoint preview. Pobiera metadata artefaktu z Postgresa i obiekt z MinIO, nie zapisuje do DB i nie loguje zawartości pliku.

Obsługiwane typy:

- `csv` — tabela z automatycznym wykryciem separatora (`;`, `,`, tab, `|`) i dekodowaniem m.in. `utf-8-sig`; wartości są zwracane jako stringi.
- `xls` / `xlsx` — pierwsza karta domyślnie; opcjonalnie `sheet_name` albo `sheet_index`; czytane są wartości komórek, bez wykonywania zawartości.
- `txt` / `log` — preview tekstowe.
- `json` — parsed JSON; przy niepoprawnym JSON endpoint zwraca tekst i `parse_error`.
- `pdf` — metadata `pdf_inline` z URL do downloadu/inline; endpoint nie renderuje stron PDF do obrazów.

Limity:

- `ARTIFACT_PREVIEW_MAX_BYTES` stała w kodzie: `25 MB`
- `rows_limit` default `1000`, max `5000`
- `text_chars_limit` default `20000`, max `100000`

Jeśli artefakt przekracza limit bajtów preview, endpoint zwraca structured response:

```json
{
  "artifact_id": "...",
  "preview_type": "unavailable",
  "reason": "file_too_large",
  "size_bytes": 123456789,
  "max_preview_bytes": 26214400
}
```

Unsupported type zwraca `415`. Brak rekordu artefaktu zwraca `404`; brak obiektu w MinIO zwraca `404`; inny błąd storage zwraca `502`.

Table preview:

```json
{
  "artifact_id": "...",
  "preview_type": "table",
  "file_ext": "csv",
  "content_type": "text/csv",
  "columns": ["col1", "col2"],
  "rows": [{"col1": "value", "col2": "value"}],
  "row_count_previewed": 1000,
  "truncated": true,
  "encoding": "utf-8-sig",
  "delimiter": ";",
  "sheet_names": null,
  "selected_sheet": null
}
```

Text preview:

```json
{
  "artifact_id": "...",
  "preview_type": "text",
  "file_ext": "txt",
  "text": "...",
  "truncated": false,
  "encoding": "utf-8"
}
```

JSON preview:

```json
{
  "artifact_id": "...",
  "preview_type": "json",
  "file_ext": "json",
  "json": {"key": "value"},
  "truncated": false
}
```

PDF preview:

```json
{
  "artifact_id": "...",
  "preview_type": "pdf_inline",
  "file_ext": "pdf",
  "content_type": "application/pdf",
  "download_url": "/artifact-browser/artifacts/<artifact_id>/download",
  "inline_url": "/artifact-browser/artifacts/<artifact_id>/download?disposition=inline"
}
```

## Artifact Explorer UI

`GET /artifact-explorer` udostępnia lekki, server-rendered UI dla operatorów lokalnej platformy. UI wymaga zalogowanego lokalnego użytkownika i podpisanego cookie sesji. Używa tych samych wewnętrznych helperów co Artifact Browser API i Artifact Preview API, więc przeglądarka nie musi znać `API_READ_TOKEN`. Publiczne endpointy JSON pod `/artifact-browser/*` nadal wymagają tokena read i nie stosują per-user RBAC.

Funkcje:

- lista artefaktów z filtrami: `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `original_filename`, `display_filename`, `file_ext`, `client_code`, `kind`, `tag`, `search`, `date_from`, `date_to`, `layout_version`, `run_id`, `raw_file_id`,
- RBAC list/detail visibility through local roles and allow-only permissions on `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `client_code`, `file_ext`, `layout_version`, and `tag`,
- searchable multi-select inputs for `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `original_filename`, `display_filename`, `file_ext`, `client_code`, `kind`, `layout_version`, `tag`, z wartościami z `/artifact-browser/artifacts/facets`; wybrane wartości są pokazywane jako comma-separated text, a submit zachowuje istniejący exact multi-value query format; typed text with no selected value submits `<field>_search` for case-insensitive contains filtering,
- table-header sorting for `created_at`, `workflow_name`, `stage_name`, `artifact_role`, `report_type`, `client_code`, `file_ext`, `size_bytes`, `layout_version`, and `filename` / `display_filename`; header links preserve active filters and update `sort`,
- visible `Extension` column backed by the stored `file_ext` value; missing `file_ext` is displayed as `-`,
- paginacja `limit` / `offset`,
- detail page: pełne metadata, `storage_key`, `bucket_name`, lineage run/raw_file, pretty-printed systemowe i manualne metadata, jeśli użytkownik ma `can_view`,
- preview section jeśli użytkownik ma `can_preview`,
- download po `artifact_id` przez `/artifact-explorer/artifacts/{artifact_id}/download`, jeśli użytkownik ma `can_download`,
- formularz edycji `description`, manualnego `metadata_json` i tagów, jeśli użytkownik ma `can_edit_annotations`.
- folder navigation under `/artifact-explorer/folders`: root/child folders, breadcrumbs and folder artifact tables. Folder names are visible to logged-in users; artifact rows inside folders are filtered by existing artifact RBAC.
- artifact detail shows manual and matching smart virtual folder memberships; admins add/remove **manual** memberships only (full-path folder picker; smart folders excluded).

Folder management in Phase 7 is admin-only (`artifact_users.is_admin=true`) in the UI. Non-admin users can browse folders and visible artifacts but cannot create, rename, delete, add or remove folder memberships. Folder delete removes folder metadata, child folders and memberships only; it does not delete artifacts or object storage.

UI nie pozwala na bezpośredni dostęp po arbitralnym `storage_key`, nie usuwa artefaktów i nie zmienia plików. Edytowalne są tylko manualne adnotacje i metadata folderów. Brak zewnętrznego SSO, deny rules i per-folder permissions.

## Eco Driving Explorer read API (Stage 2)

Read-only JSON API exposing the internal Eco Driving Explorer provider foundation (`api/eco_driving_explorer`). It reuses the portal session cookie `artifact_explorer_session` (no new cookie, no Bearer auth for these user endpoints) and is registered on the shared `app` from `api/eco_driving_explorer/http.py`; all business logic lives in the package, not in `api/main.py`. Server-rendered HTML, navigation, exports, and route/location data are intentionally **not** part of this stage.

All responses use the envelope `{ "data": ..., "meta": {...}, "error": null }` (on error, `data` is `null` and `error` is `{ "code", "message" }`). Collections are always lists, never `null`. Dates/timestamps are ISO 8601; period end is **exclusive**; enums are stable strings; `Decimal` scoring values are serialized as decimal strings (e.g. `"eco_driving_score_total": "100.00"`) to avoid float drift. The opaque `assigned_id` is passed as a **query parameter** (never a path segment) and is never cast, normalized, lowercased, or stripped of leading zeroes (`007` ≠ `7`).

| Method | Path | Permissions | Query params | Purpose |
|---|---|---|---|---|
| GET | `/user/eco-driving/api/providers` | `can_view_eco_ranking` (per client) | — | Accessible provider/client combos with effective capabilities and supported period types; unauthorized clients are **omitted** (never returned as `allowed=false`), no infra metadata. |
| GET | `/user/eco-driving/api/periods` | `can_view_eco_ranking` | `client_code`, `ranking_family`, `period_type` (`weekly`/`monthly`); optional `year`, `month` | Persisted periods only; weekly are cumulative month-to-date snapshots and preserve `W5`; monthly zero-row state returns HTTP 200 with an empty list. `entry_counts_by_group` counts ranking populations only; non-qualified rows are reported separately as `not_ranked_count`. Caveat: `not_ranked_count` is `0` for every period computed before the QUALIFIED-only ranking contract — not because no driver was below the threshold, but because such rows were then filed under `INCLUDED`/`EXCLUDED`. |
| GET | `/user/eco-driving/api/ranking-entries` | `can_view_eco_ranking` | `client_code`, `ranking_family`, `period_key`, `ranking_group` (`INCLUDED`/`EXCLUDED`/`UNKNOWN_DRIVER`); optional `page`, `limit` (max **500**), `sort`, `direction` | Persisted ranking entries with deterministic pagination; current-chart metadata is kept in a separate `current_chart` object and never overwrites persisted `ranking_group`/`ranking_included`. Only `qualification_status='QUALIFIED'` rows carry a ranking group, so a group-filtered listing never returns `LOW_DISTANCE`/`NO_DISTANCE` rows; omitting `ranking_group` lists every persisted row, non-qualified ones with `ranking_group=null`, `ranking_position=null` and `ranking_total_participants=null`. |
| GET | `/user/eco-driving/api/ranking-entry` | `can_view_eco_ranking` | `client_code`, `ranking_family`, `period_key`, `assigned_id` | One entry by exact opaque-text equality; persisted score/metrics/group/position/qualification/calculation status plus separate current-chart fields. No trip reconstruction. |
| GET | `/user/eco-driving/api/ranking-entry/trips` | `can_view_eco_ranking` **and** `can_view_eco_trip_details` | `client_code`, `ranking_family`, `period_key`, `assigned_id`; optional `page`, `limit` (max **500**), `sort`, `direction`, `trip_start_from`, `trip_start_to`, `provider_trip_id`, `min_distance_meters`, `max_distance_meters`, `has_scoring_events` | Contributing trips (safe scoring fields only). No coordinates, addresses, emails, driver names, or route polylines. Lineage is always `RECONSTRUCTED_CURRENT_STATE`. |
| GET | `/user/eco-driving/api/ranking-entry/reconciliation` | `can_view_eco_ranking` **and** `can_view_eco_trip_details` | `client_code`, `ranking_family`, `period_key`, `assigned_id` | Persisted vs reconstructed values, `reconciliation_status`, mismatch field names, non-sensitive diagnostics, and provider score definition. `MATCH` is **not** immutable historical proof. |

`period_key` is the Stage 1 stable period token (`RankingPeriodKey.token`), decoded and validated by `RankingPeriodKey.from_token`. `EXACT_SNAPSHOT` lineage is intentionally **not** available; only `RECONSTRUCTED_CURRENT_STATE` is returned.

**Client/provider resolution is server-side and trusted.** The caller supplies only `client_code`, `ranking_family`, and filter/pagination params — never `client_id`, database name/host, schema, table, provider class, or SQL. The server resolves `client_code` → active `portal_clients` → enabled `workflow_a_control.client_account` (verified UUID `client_id` + `client_db_name`) → registered provider factory, cross-checking the portal database mapping against the control-plane config and failing closed on mismatch. Every client-business query runs read-only, with a bounded statement timeout, scoped by the trusted server-side `client_id`.

**Error codes** (stable, sanitized bodies — never SQL, table internals, connection strings, stack traces, or another client's existence): `401 UNAUTHENTICATED`, `403 FORBIDDEN`, `404 PROVIDER_NOT_FOUND` / `PERIOD_NOT_FOUND` / `RANKING_ENTRY_NOT_FOUND`, `422 UNSUPPORTED_PERIOD_TYPE` / `INVALID_RANKING_GROUP` / `INVALID_SORT_FIELD` / `INVALID_PAGINATION` / `MALFORMED_PERIOD_TOKEN` / `INVALID_PARAMETER`, `409 RECONSTRUCTION_UNAVAILABLE`, `503 ENVIRONMENT_CLIENT_MISMATCH`, `500 INTERNAL_ERROR`.

**RBAC** is defined by migration `051_portal_eco_driving_permissions.sql` (see `docs/06_security.md`): additive `can_view_eco_ranking`, `can_view_eco_trip_details`, `can_view_eco_trip_routes` on both `portal_user_clients` and `portal_group_clients`. Effective per-client access is the additive union `direct OR any active-group` flag; administrators do not bypass client grants. `can_view_eco_trip_routes` is reserved for a later stage and exposes no data now.

**Audit** reuses `portal_audit_events` via the existing sanitizer. Successful access emits one event per request (not per row): `eco_driving_providers_viewed`, `eco_driving_periods_viewed`, `eco_driving_ranking_viewed`, `eco_driving_ranking_entry_viewed`, `eco_driving_trip_details_viewed`, `eco_driving_reconciliation_viewed`. Metadata is limited to safe fields (client code, ranking family, period type/dates, ranking group, page/limit, result/total counts, lineage quality, reconciliation status); the raw `assigned_id` is stored only as a short non-reversible SHA-256 `assigned_id_digest`. Failed authorization is not audited with request values.

## Eco Driving Explorer server-rendered pages (Stage 3)

Server-rendered portal pages that let an authorized user **browse** persisted Eco Driving ranking periods and ranking rows. They reuse the same portal session (`artifact_explorer_session`), the shared `_portal_layout` chrome, and — crucially — the Stage 2 `EcoDrivingApiService` for **all** data, RBAC, trusted client/provider resolution, period parsing, sorting/pagination validation, serialization, and audit. The page controllers (`api/eco_driving_explorer/pages.py`, pure HTML in `html.py`, FastAPI adapter in `page_routes.py`) make **no** server-side HTTP calls to the JSON API and duplicate none of its logic; `api/main.py` only registers the routes and injects the existing user/session and layout dependencies.

| Method | Path | Required params | Permission | Purpose / status behavior |
|---|---|---|---|---|
| GET | `/user/eco-driving` | — (optional `client_code`, `ranking_family`, `period_type`, `year`, `month`) | `can_view_eco_ranking` on ≥1 provider | Landing: authorized provider/client selector, weekly/monthly ranking-type tabs, and the persisted period list with `Open ranking` links. No authorized provider → normal 200 page with a no-access state (no client disclosure). |
| GET | `/user/eco-driving/rankings` | `client_code`, `ranking_family`, `period_key` (optional `ranking_group`, `page`, `limit`, `sort`, `direction`) | `can_view_eco_ranking` for the selected client | Ranking page with `INCLUDED` / `EXCLUDED` / `UNKNOWN_DRIVER` tabs (default `INCLUDED`), the persisted ranking table, safe sorting (Stage 1 allowlist), and pagination. Tabs show ranking populations only; non-qualified rows appear in the period list's `Not ranked` count, not inside a tab. |
| GET | `/user/eco-driving/periods/export` | optional `client_code`, `ranking_family`, `period_type`, `year`, `month`; `format=xlsx\|csv` | `can_view_eco_ranking` on ≥1 provider | The landing page's period index as a file. The provider is resolved through the same `_pick_provider` the page uses, so naming an unreachable client yields that account's provider rather than a refusal that discloses one. |
| GET | `/user/eco-driving/rankings/export` | `client_code`, `ranking_family`, and either `period_key` or `month` (+ canonical `weeks`); optional `ranking_group`, `sort`, `direction`, `unit`, `search`; `format=xlsx\|csv` | `can_view_eco_ranking` for the selected client | The ranking table as a file, at the current filtration and the current `unit` — the metric heading carries the unit caption, because a file has no toggle. `month` wins over `period_key`, mirroring the page. Neither → `422`; above 50 000 rows → `413`. |
| GET | `/user/eco-driving/ranking-entry/export` | `table=composition\|progression`, `client_code`, `ranking_family`, `assigned_id`, and either `period_key` or `month` (+ `weeks`); `format=xlsx\|csv` | as the detail page (`can_view_eco_ranking`) | One driver-detail table as a file, in the panel's own row order. An unrecognised `table` → `400`; a non-qualified period has no composition table and returns `409` rather than the metrics its state withholds. |

Semantics mirror the read API: weekly periods are **cumulative month-to-date** snapshots (explicitly stated in the UI), the persisted `period_label` and `W5` render normally, the period end is **exclusive**, and monthly with zero persisted periods shows a normal empty state ("No monthly Eco Driving ranking has been calculated for this client yet.") rather than an error or synthesized periods. Every period and row carries a `Reconstructed from current state` lineage badge (`RECONSTRUCTED_CURRENT_STATE`); current driver name is labeled as current-chart metadata, never as immutable history, and never overrides the persisted ranking group/position. The selected client, family, period, ranking group, sort, direction, and page size are preserved across tab/page navigation, and changing group/sort resets to page 1; the ranking group is carried in the URL (bookmarkable), while the opaque `assigned_id` never appears in a path segment.

**Navigation.** An `Eco Driving` workspace link appears in the shared sidebar only when the signed-in user has effective `can_view_eco_ranking = true` on at least one provider (direct OR active-group). Administrators do **not** bypass this grant, `can_view_database`/admin status do not reveal it, and the check is platform-side (permissions + provider registry) with no client-database query. Before migration `051` is applied the check fails closed to hidden.

**Errors** are mapped to portal-styled pages that reuse the read-API status codes and sanitized messages: unauthenticated → the existing login redirect; `403` for unauthorized client/provider (no disclosure); `404` for unknown provider/period; `422` for malformed period token / invalid ranking group / invalid sort / oversize limit; `503` for environment/client mismatch (generic, no DB name/IDs/SQL). Stage 4 adds the ranking-entry detail and conditional aggregate reconciliation described below. The contributing-trip page is available in Stage 5 under the stricter trip-details permission; route/location data remains unavailable. CSV/XLSX export is available — for the contributing-trip table and, since `docs/45`, for the period, ranking and driver-detail tables — and never widens the row or column set the corresponding page shows.

**Audit** reuses the Stage 2 service-level events (one logical read = one event: landing emits `eco_driving_providers_viewed` + `eco_driving_periods_viewed`; the ranking page emits `eco_driving_ranking_viewed`); the pages add no separate event type and never double-audit. Raw `assigned_id` values from rows are never logged.

## Eco Driving Explorer ranking-entry summary and reconciliation (Stage 4)

GET /user/eco-driving/ranking-entry is a server-rendered detail page. Required query parameters are client_code, ranking_family, period_key and opaque-text assigned_id. Optional ranking_group, page, limit, sort and direction are validated and used only for the safe Back to ranking URL; they never alter entry lookup. Ranking rows expose semantic View details anchors preserving this list state. assigned_id remains query data and is never a path segment.

The persisted summary requires can_view_eco_ranking. It shows client/provider metadata, persisted period label and exclusive boundaries, partial/sequence state, persisted group/inclusion/position/participant count, qualification/calculation status, trip/distance totals, score, rating/share, eight persisted event counters/rates/points, and lineage quality. Current driver-chart metadata is explicitly current, not an immutable historical snapshot. EXCLUDED stays scored and UNKNOWN_DRIVER remains separate.

Score rules come from the provider ScoreDefinition, not an HTML threshold copy: minimum qualifying distance, eight metrics, rounded-event-per-100-km semantics, points, maximum score, buckets and rating thresholds. Decimal values use deterministic decimal-string serialization.

The reconciliation panel additionally requires can_view_eco_trip_details; can_view_eco_trip_routes has no effect. Without trip-detail permission the page still returns the summary, does not invoke reconciliation or query assignments, and shows a non-disclosing message. With permission it displays MATCH, MISMATCH or UNAVAILABLE, comparison fields for trips, distance, eight counters, eight rates, eight point values and total score, plus aggregate diagnostics. UNAVAILABLE does not destroy the persisted summary.

Lineage is always Reconstructed from current state (RECONSTRUCTED_CURRENT_STATE). MATCH does not prove immutable historical membership, fully audited lineage or EXACT_SNAPSHOT. No contributing-trip table/link, provider trip ID, trip timestamp, registration, driver tag, email, address, coordinate, route, map or export is exposed.

Authentication follows the portal login redirect. Missing ranking permission returns non-disclosing 403; stale entry returns safe 404; malformed period/parameters return 422; environment mismatch returns sanitized 503. Administrators do not bypass client grants. Summary emits eco_driving_ranking_entry_viewed; authorized reconciliation additionally emits eco_driving_reconciliation_viewed. Audit uses assigned_id_digest, never raw assigned ID or sensitive/request internals.

## Planned / future (wymagania poza obecnym API)

Jeśli w przyszności zechce się centralnie rejestrować **konfiguracje per klient** (sekrety API, markery synchronizacji) w tej samej instancji co platforma, potrzebne byłyby **nowe endpointy lub inny mechanizm** — **obecnie nie są częścią** `api/main.py` i nie należy ich zakładać w integracjach bez zmiany kodu.

## Uwaga o zduplikowanych definicjach tras

`api/main.py` zawiera podwójne definicje dla `PATCH /runs/{run_id}` i `GET /runs`. W FastAPI pierwsza zarejestrowana trasa jest używana; aktywne są pierwsze definicje (update_run, list_runs). Drugie definicje to martwy kod.


### Terminalizacja runów a rekoncyliacja historyczna

`PATCH /runs/{run_id}` jest ścieżką **żywej** finalizacji: zawsze stempluje `ended_at = now()`,
co jest poprawne dla joba domykającego samego siebie. Nie jest to ścieżka porządkowania
porzuconych wierszy historycznych — użyta miesiące po fakcie zapisałaby egzekucję, która nigdy
nie trwała tak długo.

Retrospektywne domknięcie jednego imiennie wskazanego wiersza `RUNNING` ma osobną powierzchnię
operatorską: `ops/reconcile_historical_run.py` (dry-run domyślnie, jawny status terminalny, jawny
historyczny `ended_at`, trwała proweniencja w `ops_control.run_reconciliation`). Kontrakt i
granica autoryzacji: `docs/07_operations.md` §5.8. Endpoint API pozostaje nietknięty i nie wolno
go rozszerzać o tę rolę.

## Schemat odpowiedzi prune

`POST /maintenance/prune?days=60&dry_run=true` is a write-token-protected, read-only planner. It returns `schema=log-platform-prune-result/v1`, `classification=PRUNE_DRY_RUN_SUCCEEDED`, `operator_action_required`, a safe aggregate `plan` (scope, UTC cutoff, candidate counts and exclusion-reason counts), and a `mutations` object whose values are all zero. It never returns object keys, filenames, customer identifiers or row identities.

The API deliberately rejects `dry_run=false` with HTTP 409 and `PRUNE_API_EXECUTION_DISABLED_USE_COORDINATED_HOST_COMMAND`. Destructive execution is available only through the reviewed host command, which adds backup coordination and environment identity attestation. Compose passes only the six required `LOG_PLATFORM_*` identity declarations to the API; its expected Postgres host is scoped separately from host processes and defaults to Docker DNS. Missing required declarations fail Compose interpolation or the prune request closed.


## Eco Driving Explorer contributing-trip evidence (Stage 5)

GET /user/eco-driving/ranking-entry/trips is the server-rendered evidence page for one persisted ranking entry. Required query parameters are client_code, ranking_family, period_key, and exact opaque assigned_id. It requires both can_view_eco_ranking and can_view_eco_trip_details; can_view_eco_trip_routes has no effect. The Stage 4 detail page renders its semantic link only for users with trip-detail permission. Safe ranking_group, ranking_page, ranking_limit, ranking_sort, and ranking_direction values preserve return navigation without changing entry lookup.

The page calls the existing Python service/provider directly. It shows persisted score/trip count beside the unfiltered current reconstructed count and filtered count, current chart metadata, a RECONSTRUCTED_CURRENT_STATE warning, accessible filters, a semantic table, deterministic pagination, and separate unfiltered/filtered empty states. It exposes provider trip ID, ISO timestamp-with-offset start/end, assignment source, source-row status, distance in metres and deterministic decimal kilometres, all eight scoring-event counters, and their integer total. It does not expose vehicle registration, driver tags, email, location/address, coordinates, route geometry, arbitrary source fields, or exports.

Membership comes from public.eco_trip_assignments scoped by trusted client ID, exact assigned_id, inclusive period start, exclusive period end, aggregation_included = TRUE, and is_private_trip = FALSE. Current Driver_Restrictions/Dysponent_ID values do not redefine membership. A LEFT JOIN to client_trips supplies only source-row availability; a missing source row remains listed with source_trip_present=false and a textual unavailable-source status.

The existing JSON endpoint GET /user/eco-driving/api/ranking-entry/trips accepts the same optional filters: trip_start_from (inclusive ISO date/datetime), trip_start_to (exclusive ISO date/datetime), exact integer provider_trip_id, inclusive non-negative min_distance_meters/max_distance_meters, and has_scoring_events=true|false. Date bounds use the business timezone and intersect with the ranking period; they cannot widen it. Empty effective ranges, min > max, malformed values, unsupported sort/direction, page < 1, limit < 1, or limit > 500 return sanitized 422.

Trip sorting is allowlisted to trip_start_ts, trip_end_ts, trip_distance_meters, provider_trip_id, and total_scoring_events. Default ordering is trip_start_ts ASC, provider_trip_id ASC; alternatives retain a unique provider-trip tie-breaker. HTML defaults to page 1 / limit 50 and offers 25, 50, 100, and 200; provider/API maximum remains 500. An out-of-range page is a normal HTTP 200 empty page with totals and previous navigation. Filter controls and sort/page links preserve identity and safe ranking context; applying/clearing filters or changing sort/page size resets to page 1.

Trip reads emit one eco_driving_trip_details_viewed event per service call. Audit metadata may contain period bounds, page/limit, allowlisted sort/direction, counts, lineage, distance/date/event filter summaries, filters_active, provider_trip_filter_used, and assigned-ID digest. It never contains raw assigned ID, raw provider-trip filter, individual row identifiers/timestamps, personal/location data, SQL, cookies, or full query strings. There is no CSV/XLSX export, route/map output, or immutable EXACT_SNAPSHOT lineage.


## Eco Driving Explorer permission administration (Stage 6)

The server-rendered administrative surface uses the existing portal session and requires an active `artifact_users.is_admin=true` account on every route:

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/client-access/eco-driving` | Bounded Users, Groups and Configured Eco Driving clients sections, including direct/inherited/effective summaries and registry-only provider status. |
| GET | `/admin/client-access/eco-driving/users/{user_id}` | Edit direct Eco flags for every enabled portal client; inherited active-group grants are read-only. |
| POST | `/admin/client-access/eco-driving/users/{user_id}` | Atomically replace the selected user's direct Eco flags. |
| GET | `/admin/client-access/eco-driving/groups/{group_id}` | Edit one group's Eco client flags and show affected active-member count. |
| POST | `/admin/client-access/eco-driving/groups/{group_id}` | Atomically replace the selected group's Eco flags without materializing user rows. |

Administrative authorization permits configuration management; it does **not** grant user-facing Eco Explorer access. The Explorer continues to require the per-client effective union `direct OR any active-group`, including for administrators. A non-admin with Eco grants cannot access these admin routes. Unauthenticated requests receive the canonical `303` login redirect; authenticated non-admins receive the existing non-disclosing `403`.

Forms are `application/x-www-form-urlencoded`. The trusted subject ID comes only from the route. Allowed fields are exactly one `csrf_token`, exactly one `version_token`, one repeated `client_code` per enabled client, and optional `clients[<client_code>][can_view_eco_ranking|can_view_eco_trip_details|can_view_eco_trip_routes]=true` checkbox fields. The request body, field count, client count, field-name length and value length are bounded. Duplicate clients/permissions, unknown clients/fields, malformed booleans, subject IDs, database/environment identifiers and non-Eco permission names are rejected. Body/field limit violations return sanitized `413`; other malformed or unexpected input returns `422`.

Only `000`, `100`, `110` and `111` are persisted (ranking, trip details, routes). New child grants promote prerequisites. Normalization is state-aware: if an existing ranking grant is unchecked, submitted children cannot restore it and the result is `000`; if existing trip-detail access is unchecked while ranking remains selected, route access is cleared and the result is `100`. The same transition function is used for users and groups.

Each POST requires a short-lived HMAC CSRF token bound to the administrator, action, subject type and subject ID. A deterministic version digest of every enabled client code and its three current direct Eco flags is recomputed after locking the subject/current rows. A stale form returns `409` with the current editor and performs no writes. A changed submission and a no-op both use Post/Redirect/Get (`303`) to the editor with only `result=updated` or `result=no-change`; refresh cannot repeat the write. Unexpected failures return a sanitized `500` page (and never database exception details).

All submitted clients are validated before one statement-timeout-bounded platform-DB transaction updates only the three Eco columns. Existing rows remain when Eco flags become false, preserving `can_view_database`, `can_view_reports`, `can_export_database` and other fields. A new Eco-only row explicitly stores those three unrelated flags as false, avoiding their historical true defaults. There are no client-business DB writes.

One `eco_driving_user_permissions_updated` or `eco_driving_group_permissions_updated` event is inserted in the same transaction as a changed submission. Bounded sanitized metadata contains actor/subject IDs, counts, changed client codes, `before`, raw `submitted`, `normalized_after`, dependency-normalization and revoke-cascade flags, plus affected active-member count for groups. CSRF/version tokens, complete forms, email, credentials, SQL and non-Eco permissions are excluded. No-op submissions create no misleading update event. Migration `051_portal_eco_driving_permissions.sql` must be deployed before using these screens; the application applies no migration and creates no automatic grants.
