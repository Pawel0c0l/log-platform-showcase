[English](README.md) · **Polski**

# Log Platform

Platforma automatyzacji dla firm, które zarządzają flotą pojazdów. Codziennie pobiera dane telematyczne (przejazdy, zużycie paliwa, przekroczenia prędkości, gwałtowne manewry) z API dostawcy telematyki, utrzymuje je w bazie klienta, liczy ranking Eco Driving dla kierowców i rozsyła im tygodniowe oraz miesięczne podsumowania z osobistym, zabezpieczonym dashboardem. Operator ma do tego portal z raportami, eksploratorem danych i rejestrem artefaktów.

System działa produkcyjnie od 2026 roku w modelu „jeden host, wielu klientów”. Ten katalog to **zanonimizowana migawka** kodu produkcyjnego, zobacz [sekcję o tym repozytorium](#o-tym-repozytorium) na dole.

## Co ten system robi

Dla osoby, która nie czyta kodu:

- **Pobiera dane z telematyki bez udziału człowieka.** Harmonogram (dispatcher) co kilka godzin odpytuje API dostawcy o nowe przejazdy i zdarzenia każdego klienta, a wynik ląduje w osobnej bazie tego klienta. Pobieranie jest odporne na opóźnienia po stronie dostawcy: przejazdy, które „dojechały” z opóźnieniem, są dopisywane przez okna uzgadniania (dobowe, tygodniowe, 32-dniowe).
- **Liczy program Eco Driving.** Z przejazdów i zdarzeń powstaje wynik 0–100 dla każdego kierowcy, ranking w obrębie floty, progi kwalifikacji (minimalny dystans), porównanie z poprzednim okresem i rozbicie utraconych punktów na kategorie.
- **Wysyła kierowcom e-maile i dashboard.** Co tydzień i co miesiąc kierowca dostaje wiadomość dopasowaną do swojego wyniku, z linkiem do osobistego dashboardu. Link jest jednorazowym „kluczem” (capability link), nie wymaga konta ani hasła, a serwer nigdy nie widzi sekretu w adresie.
- **Daje operatorowi portal.** Biblioteka raportów cyklicznych, eksplorator danych klienta (filtry, dystrybucje wartości, eksporty w tle, zapisane widoki), ranking Eco Driving z drążeniem do pojedynczego przejazdu, rejestr artefaktów z weryfikacją integralności.
- **Pilnuje siebie.** Każde uruchomienie joba ma rejestr runów, logów strukturalnych i artefaktów. Retencja usuwa stare dane według polityk per klient. Błędy i naruszone niezmienniki trafiają do rejestru „podejrzanych błędów” z powiadomieniem e-mail. Operacje produkcyjne (promocja środowiska, odtwarzanie z backupu) mają kontrakty fail-closed: odmawiają działania, gdy tożsamość środowiska się nie zgadza.

## Zrzuty ekranu

Wszystkie dane na zrzutach są syntetyczne.

**Dashboard kierowcy** (wersja desktop, 1440 px; renderowany z fixture'ów w tym repozytorium):

| Wynik | Wykroczenia | Dni |
|---|---|---|
| ![Dashboard kierowcy, slajd Wynik](docs/portfolio/screenshots/desktop-1440-wynik.png) | ![Dashboard kierowcy, slajd Wykroczenia](docs/portfolio/screenshots/desktop-1440-wykroczenia.png) | ![Dashboard kierowcy, slajd Dni](docs/portfolio/screenshots/desktop-1440-dni.png) |

**Dashboard kierowcy** (wersja mobilna, 390 px) oraz warianty stanów:

| Mobile: Wynik | Mobile: Wykroczenia | Kierowca „niebezpieczny” | Okres miesięczny |
|---|---|---|---|
| ![Mobile, zakładka Wynik](docs/portfolio/screenshots/mobile-390-wynik.png) | ![Mobile, zakładka Wykroczenia](docs/portfolio/screenshots/mobile-390-wykroczenia.png) | ![Desktop, kierowca niebezpieczny](docs/portfolio/screenshots/desktop-1440-niebezpieczny.png) | ![Desktop, okres miesięczny](docs/portfolio/screenshots/desktop-1440-miesiac.png) |

**Portal operatora** (zatwierdzone projekty ekranów, na podstawie których zbudowano portal; katalog `design-handoffs/log-platform`):

| Ranking Eco Driving | Eksplorator danych | Biblioteka raportów |
|---|---|---|
| ![Ranking Eco Driving](design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff/reference/screens/ECO-001-ranking-light-rate.png) | ![Eksplorator danych z panelem wiersza](design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff/reference/screens/DB-006-row-detail-panel.png) | ![Szczegóły raportu](design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff/reference/screens/REP-003-report-detail.png) |

Pozostałe ekrany (katalog zbiorów, eksporty w tle, tryb ciemny, tablet, artefakty): `design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff/reference/screens/`.

## Jak to działa

```
 API dostawcy telematyki ──► jobs/api/telematics/*  ──► baza biznesowa klienta (Postgres, osobna per klient)
                                   │                             │
                                   │ runy / logi / artefakty      │ przejazdy, zdarzenia, paliwo
                                   ▼                             ▼
                          api/main.py (FastAPI)          jobs/ecodriving/* ──► ranking, e-maile
                          Postgres + MinIO                       │
                                   │                             ▼
                                   ▼                    jobs/ecodriving_dashboard/* ──► snapshot kierowcy
                          portal operatora (HTML/JS)             │
                                                                 ▼
                                               delivery/ (Cloudflare Worker + D1 + R2) ──► dashboard kierowcy
```

Trzy warstwy:

1. **Rdzeń platformy**: wspólny model uruchomień (`runs`), logów strukturalnych, artefaktów binarnych i retencji. Każdy job, niezależnie od rodzaju, raportuje do tego samego API. Harmonogram hostowy to systemd (timery i unity w `ops/systemd`).
2. **Workflow A, integracja z API telematyki**: dispatcher, synchronizacja przejazdów i zdarzeń z kontrolą pokrycia czasowego, uzgadnianie opóźnionych danych, agregacje dzienne, retencja per klient, onboarding nowego klienta ze skryptu i pliku YAML. Program Eco Driving (agregacja, e-maile tygodniowe i miesięczne, snapshot dashboardu, bezpieczna publikacja).
3. **Workflow B, pipeline raportów z poczty** (ścieżka zapasowa, rozwój wstrzymany): pobieranie raportów z IMAP, normalizacja CSV i XLSX, detekcja typu raportu i walidacja, zasilanie bazy klienta z raportów.

Portal operatora i dashboard kierowcy są napisane bez frameworka frontendowego: serwerowo renderowany HTML plus moduły JS bez bundlera. Dashboard kierowcy to jeden statyczny pakiet, którego pięć plików prezentacyjnych jest bajtowo zgodnych z zatwierdzonym projektem (test w repozytorium pilnuje sum SHA-256).

## Stack

| Obszar | Technologia |
|---|---|
| Backend | Python 3.12, FastAPI, psycopg 3 |
| Dane | PostgreSQL 16 (baza platformy plus osobna baza per klient), MinIO / S3 na artefakty |
| Uruchomienie | Docker Compose (API), systemd (joby, timery, watchdog), jeden host Linux |
| Frontend | HTML, CSS, JavaScript bez frameworka; tokeny projektowe, motyw jasny i ciemny, WCAG 2.1 AA |
| Dostarczanie dashboardu | Cloudflare Workers, D1 (uprawnienia), R2 (snapshoty), linki capability, cookie `__Host-` |
| Testy | 275 plików testów (Python i Node), w tym testy na jednorazowych instancjach Postgres i testy przeglądarkowe przez WebDriver |

## Skala

| Miara | Wartość |
|---|---|
| Endpointy HTTP w API | 132 |
| Migracje bazy platformy | 71 |
| Migracje baz klientów | 45 |
| Kod API | ok. 62 tys. linii |
| Kod jobów | ok. 67 tys. linii |
| Testy | ok. 210 tys. linii |
| Dokumentacja projektowa | 45 dokumentów w `docs/`, 3 pakiety handoff projektowego |

## Uruchomienie

### Dashboard kierowcy, bez instalowania czegokolwiek

Potrzebny jest tylko Python 3 (dowolny serwer statyczny też zadziała):

```bash
cd assets/driver_eco_dashboard
python3 -m http.server 8731 --bind 127.0.0.1
```

Następnie otwórz `http://127.0.0.1:8731/preview.html`. Lista rozwijana przełącza 16 syntetycznych stanów (kierowca bezpieczny, niebezpieczny, nowy w rankingu, zbyt mały dystans, raport niegotowy i inne). Adres `preview.html?fixture=<nazwa>#weekly/2` otwiera konkretny stan na konkretnym slajdzie.

### Cały stack: API, Postgres, MinIO, portal

Skrypt w katalogu `demo/` podnosi odizolowany stack na innych portach niż produkcja, nakłada migracje i tworzy konto administratora. Szczegóły i ograniczenia: [demo/README.pl.md](demo/README.pl.md).

```bash
./demo/up.sh
```

## Gdzie zacząć czytać

Dla programisty albo asystenta AI, który ma ocenić ten kod:

1. [ARCHITECTURE.md](ARCHITECTURE.md): mapa systemu, przepływ danych, decyzje projektowe.
2. [CONVENTIONS.md](CONVENTIONS.md): nazewnictwo, obsługa błędów, dyscyplina migracji.
3. [docs/00_overview.md](docs/00_overview.md) i [docs/05_jobs.md](docs/05_jobs.md): katalog jobów z kontraktami.
4. [docs/22_portal_ui_foundation_and_shared_shell.md](docs/22_portal_ui_foundation_and_shared_shell.md): dlaczego portal nie ma frameworka i jak jest zbudowany.
5. [docs/28_driver_eco_dashboard_v1_snapshot_foundation.md](docs/28_driver_eco_dashboard_v1_snapshot_foundation.md) oraz [delivery/driver_eco_dashboard/README.md](delivery/driver_eco_dashboard/README.md): kontrakt snapshotu kierowcy i bezpieczna publikacja.
6. [docs/06_security.md](docs/06_security.md), [docs/08_retention.md](docs/08_retention.md), [docs/09_disaster_recovery.md](docs/09_disaster_recovery.md): bezpieczeństwo, retencja, odtwarzanie.
7. Testy: `ops/tests_manual/test_*.py` i `ops/tests_manual/*_harness.mjs`. Większość testów integracyjnych sama podnosi jednorazowy Postgres (`ops/tests_manual/disposable_postgres.py`).

Dokumenty w `docs/` numerowane od 12 wzwyż to zapis decyzji projektowych w kolejności ich podejmowania, włącznie z audytami i planami naprawczymi. Nie są podręcznikiem, tylko historią inżynierską systemu.

## O tym repozytorium

To jest migawka prywatnego repozytorium produkcyjnego, wygenerowana automatycznie skryptem eksportu i sprawdzona listą zakazanych tokenów przed publikacją.

- **Klienci i dostawca są zanonimizowani.** Kody klientów to pseudonimy (`ALPHA00001`, `BRAVO00016`, `DELTA00001` i podobne), dostawca telematyki występuje jako `telematics`, adresy e-mail i hosty wskazują na domeny `example.invalid`. Przemianowanie jest spójne w kodzie, SQL, testach i dokumentacji, więc projekt nadal się kompiluje i testy przechodzą.
- **Żadne dane produkcyjne nie są dołączone.** Tablice rejestracyjne w testach i dokumentach są deterministycznie sfałszowane, identyfikatory środowisk zhaszowane, a fixture'y dashboardu i prototypów od początku były syntetyczne.
- **Historia commitów nie jest przenoszona.** Każda publikacja to jeden commit „Snapshot”.
- **Czego celowo nie ma**: raportów operacyjnych z incydentów, prawdziwych próbek raportów od dostawcy, specyfikacji OpenAPI dostawcy (dokumenty w `docs/` mogą się do niej odwoływać), plików kontekstu dla asystentów AI używanych przy rozwoju.

Autor: Paweł Dzierzek. Kod udostępniony do wglądu jako próbka pracy.
