# Runner

> **RELEASE-BOUNDARY-DEVELOPMENT-ONLY-INVOCATION.** Command lines of the
> form `PYTHONPATH="$PWD" python3 ops/runner.py …` are development / local /
> debug only — they execute the mutable working tree. The supported production
> entrypoint is the installed wrapper
> `/usr/local/bin/log-job-runner.sh <module> '<json>'`. See
> `docs/07_operations.md` -> *Release boundary*.


Plik: `ops/runner.py`

## Wejście

```bash
python3 ops/runner.py <job_module> [params_json_or_file]
```

- `job_module`: np. `jobs.mail.fetch_reports` (**Workflow B**) albo `jobs.api.telematics.sync_trips_and_speeding`, `jobs.api.telematics.aggregate_trip_fuel_daily`, `jobs.ecodriving.job_eco_driving_aggregate`, `jobs.api.telematics.dispatcher`, `jobs.api.telematics.retention_purge` (**Workflow A**)
- drugi argument:
  - JSON inline, albo
  - ścieżka do pliku JSON,
  - brak argumentu -> `{}`

## Zachowanie

- ładuje `.env` z katalogu repo dla sekretów i pozostałej konfiguracji, bez nadpisywania istniejącego process ENV,
- jeśli istnieje kanoniczny `/etc/log-platform/environment-identity.env`, parsuje go bez shell evaluation i stosuje jako jedyne źródło `LOG_PLATFORM_TARGET_ENVIRONMENT`,
- odrzuca konflikt pomiędzy process ENV / starym `.env` i plikiem kanonicznym; wrapper zainstalowany przez provisioning wymaga pliku kanonicznego i failuje, gdy go brakuje,
- ręczne wywołania produkcyjnego hosta przechodzą przez `ops/run_with_environment_identity.py`, więc operator nie eksportuje identity ręcznie,
- tworzy klienta API przez `LogPlatformClient.from_env()`,
- importuje dynamicznie wskazany moduł,
- wymaga funkcji `run(client, run_id, params)` w module joba,
- uruchamia job w `run_context`.

Kontrakt `run(client, run_id, params)` jest **wspólny** dla workflow A i B: różnią się implementacje modułów jobów, nie mechanika runnera.

## Lifecycle

`run_context` (`api/client.py`):

1. `POST /runs` -> run `RUNNING`
2. log `Run started`
3. wykonanie joba
4. sukces: `PATCH /runs/{id}` -> `SUCCESS` + log `Run finished: SUCCESS`
5. wyjątek: log `ERROR` z traceback + `PATCH /runs/{id}` -> `FAILED`, potem re-raise

Runner dodatkowo wysyła log dispatchu: `"Job dispatch"`.

### Finalizacja i stany nieterminalne (P1-I)

`public.runs` jest rekordem, któremu ufa watchdog i narzędzia operatorskie, więc finalizacja ma trzy **rozłączne** wyniki. Wcześniej `finish_run(SUCCESS)` był wywoływany wewnątrz tego samego `try`, co ciało joba, przez co awaria finalizacji trafiała do handlera błędów aplikacji: run, który wykonał całą swoją pracę, był logowany jako „Run failed” i zapisywany jako `FAILED`. Rekord trwały aktywnie zaprzeczał temu, co się stało.

| sytuacja | `public.runs` | wyjątek do runnera | dowód |
|---|---|---|---|
| job OK, finalizacja OK | `SUCCESS` | brak | log `Run finished: SUCCESS` |
| job rzucił wyjątek | `FAILED` | oryginalny wyjątek | log `ERROR` z traceback |
| job OK, finalizacja padła | pozostaje nieterminalny | `RunFinalizationError` (`application_succeeded=True`) | log `ERROR`, `classification=RUN_FINALIZATION_FAILED`, `intended_status=SUCCESS` |
| job padł i finalizacja padła | pozostaje nieterminalny | oryginalny wyjątek aplikacji | log `ERROR`, `classification=RUN_FINALIZATION_FAILED`, `intended_status=FAILED`, `primary_exception` |

Zasady, które z tego wynikają:

- **Awaria finalizacji nigdy nie degraduje sukcesu do `FAILED`.** Sukces jest finalizowany poza `try`, więc jego błąd nie może zostać wzięty za błąd aplikacji.
- **Awaria finalizacji nigdy nie maskuje pierwotnego wyjątku.** Na ścieżce błędu wygrywa wyjątek aplikacji — ale nie jest to już ciche `except: pass`: wiersz zostawiony w `RUNNING` ma odtąd zapisaną przyczynę.
- **Logowanie na ścieżce awaryjnej jest best-effort.** To API właśnie zawiodło, więc log, który sam rzuca, zastąpiłby precyzyjną diagnozę błędem połączenia z sąsiedniej ramki.
- **`run_context` nie naprawia i nie postarza wierszy.** Postarzanie należy do `ops/execution_watchdog.py` (`stale_grace_minutes`, domyślnie 240 min, werdykt `STALE` + incydent `SCHEDULED_RUN_STALE`). Proces, który właśnie udowodnił, że nie umie dosięgnąć API, jest najgorszym możliwym kandydatem na wykonawcę recovery.
- **Terminalizacja jest jednokierunkowa.** `PATCH /runs/{id}` używa compare-and-set w samym `UPDATE`: powtórzenie tego samego statusu jest idempotentne, a zmiana jednego stanu terminalnego na inny to `409` i wiersz zostaje nietknięty. Spóźniona lub zduplikowana finalizacja nie przepisze rozstrzygniętego wyniku.

Kontrakt plikowy Stage 3 (`docs/05_jobs.md`, „Stage 3 — odzyskiwanie po przerwaniu”) używa **innej, wyższej** wartości progu (480 min wobec 240 min tutaj) i jest to celowe: próg runu odpowiada na pytanie „czy ten wiersz przestał się ruszać?”, a próg pliku musi przekroczyć każde dopuszczalne życie wykonania (`TimeoutStartSec=6h`), zanim wolno odebrać plik możliwemu właścicielowi. Zrównanie ich dało wcześniej 240-minutowy próg pliku, który mógł przejąć żywy, pięciogodzinny load. Wymagane jest wyłącznie uporządkowanie: plik nie staje się odzyskiwalny wcześniej, niż watchdog zgłosiłby jego run jako `STALE`, więc operator zawsze widzi najpierw incydent runu.

Uwaga obserwacyjna, poza zakresem tej zmiany: skoro `log-workflow-b.service` dopuszcza 6 h, a próg watchdoga to 240 min, legalne wielogodzinne wykonanie mogłoby zostać zgłoszone jako `STALE`. W praktyce cykl trwa sekundy (produkcja: 3 s), więc nie jest to obserwowane; strojenie `stale_grace_minutes` per subject należy do osobnej decyzji operacyjnej i nie zostało tu wykonane.

Wyjątek opt-in dotyczy `jobs.api.telematics.dispatcher`. Runner wywołuje jego atomową fazę przygotowania przed `POST /runs`. Gdy faza zwraca oczekiwany brak claimable fire, runner kończy się kodem `0` bez tworzenia wiersza `runs` i bez logów aplikacyjnych. Po claimie stosowany jest pełny lifecycle powyżej. Wyjątek podczas przygotowania jest ponownie zgłaszany wewnątrz `run_context`, więc tworzy widoczny run `FAILED` i log `ERROR`. Pozostałe joby zachowują eager lifecycle bez zmian.

## Trigger i actor

- `trigger = params.get("trigger", "MANUAL")`
- `actor = params.get("actor")`

`source` runa jest równe nazwie modułu joba.

## Run ID handoff dla subprocessów

Runner zachowuje dotychczasowe wyjście CLI. Dodatkowo, jeśli proces ma ustawione `LOG_PLATFORM_RUN_ID_FILE`, po utworzeniu runa zapisuje `run_id` do wskazanego pliku tekstowego. Używa tego `jobs.api.telematics.dispatcher`, żeby powiązać `workflow_a_control.client_schedule_run_history.platform_run_id` z platformowym runem uruchomionego dataset joba.

## Idempotent artifact client uploads

`LogPlatformClient.upload_artifact()` remains backward compatible and returns the artifact ID string by default. Callers may pass `idempotency_scope` and `idempotency_key` together; keyed timeout/connection retry reopens the file and resends the same identity. `structured_response=True` returns `ArtifactUploadResult(artifact_id, idempotency_status)` where status is `created` or `reused`.

Workflow B Stage 1, Stage 2 and Stage 3 return typed batch results to direct Python callers. The generic runner intentionally ignores successful return values, so existing terminal/systemd invocations retain the same lifecycle. A typed batch exception still propagates through `run_context`, marks the run `FAILED`, and exposes `result` / `partial_result` to an in-process caller. Jobs log bounded aggregate summaries rather than requiring the runner to serialize potentially large per-item results.

`jobs.reports.workflow_b.orchestrator` composes those contracts inside the runner's single parent `run_id`. It calls stage batch functions and bounded configured postprocessors directly (never a nested runner), returns `WorkflowBBatchResult`, and raises `WorkflowBOrchestrationError` with `result` / `partial_result` after safe independent work finishes. A healthy no-work mailbox/backlog check remains an auditable successful parent run; it does not use Workflow A dispatcher's silent no-op deletion protocol.

## Tryby uruchomienia (operacyjnie)

1. **Na żądanie (terminal / ręcznie)** — wywołanie `ops/runner.py` z odpowiednim modułem i `params_json`; `trigger` można przekazać w parametrach (np. dla rozróżnienia w logach).
2. **Cyklicznie (harmonogram)** — zwykle systemd timer na hoście wywołujący tę samą komendę; dla Workflow A repo zawiera zaimplementowany `jobs.api.telematics.dispatcher` i proponowane unity systemd, a dla Workflow B przykładowy timer `jobs.mail.fetch_reports`.
3. **Na żądanie przez e‑mail (docelowo)** — poza repo wymaga mechanizmu na hoście (np. skrypt parsujący skrzynkę, webhook), który w efekcie wywoła runnera lub równoważną automatyzację; **nie jest to** część kontraktu `api/main.py`.

Eco Driving uses the same runner contract. Manual runs call the module path
directly, while scheduled runs are dispatcher dataset rows registered in
`jobs.api.telematics.registry` / `workflow_a_control.dataset_registry`:

```bash
PYTHONPATH="$PWD" python3 ops/runner.py jobs.ecodriving.job_eco_driving_aggregate \
  '{"client_id":"<CLIENT_ID>","month":"2026-05","include_weekly":true,"include_monthly":true}'
```
