[English](README.md) · **Polski**

# Demo stack

Podnosi cały stack (Postgres 16, MinIO, API z portalem) jako odizolowany projekt Compose, na portach, które nie kolidują z żadną inną instancją na tej samej maszynie, i wprowadza do niego dwóch syntetycznych klientów tym samym skryptem, którym wprowadza się klientów produkcyjnych.

```bash
./demo/up.sh
```

Pierwszy przebieg buduje obraz API (kilka minut), kolejne trwają poniżej minuty. Po zakończeniu:

| Co | Adres |
|---|---|
| Zdrowie API | http://127.0.0.1:8010/health |
| Portal operatora | http://127.0.0.1:8010/login (`admin` / `demo-admin`, hasło można nadpisać zmienną `PORTAL_ADMIN_PASSWORD`) |
| Konsola MinIO | http://127.0.0.1:9011 (dane logowania w `demo/.env.demo`) |

Po zalogowaniu działają od razu: Artefakty, Administracja (użytkownicy, grupy, dostęp klientów, zbiory danych, uprawnienia Eco Driving, foldery raportów, audyt) oraz Dane. Portal pokazuje użytkownikowi wyłącznie zbiory i foldery raportów, które administrator mu przypisał, więc zakładka Dane jest na początku pusta: dostęp do klientów `ALPHA00001` i `BRAVO00016` nadaje się w Administracja → Dostęp klientów i Zbiory danych. To celowe zachowanie produkcyjne, nie brak demo.

Sprzątanie, łącznie z wolumenami i obrazem:

```bash
./demo/down.sh
```

## Co robi `up.sh`

1. Generuje `demo/.env.demo` z `.env.example`: losowe sekrety w miejsce `CHANGE_ME`, świeży UUID tożsamości platformy, porty demo.
2. Buduje obraz API i uruchamia kontenery (`docker-compose.yml` plus nakładka `demo/docker-compose.demo.yml`), czeka na `/health`, bo tabele bazowe tworzy API przy starcie.
3. Nakłada migracje bazy platformy skryptem `ops/db_migrate.sh`. Na pustej bazie przebieg zatrzymuje się trzy razy, zgodnie z projektem migracji, i za każdym razem skrypt uzupełnia brakujący stan tak, jak robi to produkcja:
   - migracja 047 wymaga tożsamości środowiska: skrypt zapisuje marker `local_dev`;
   - migracja 060 przepisuje harmonogram jednego klienta produkcyjnego i na pustej bazie nie ma czego przepisać: jest odnotowana jako wykonana;
   - migracja 072 zmienia ograniczenie (ta część się wykonuje), po czym sprawdza stan dwóch klientów produkcyjnych: skrypt wprowadza dwóch klientów syntetycznych, ustawia im wartość, której migracja wymaga, i odnotowuje ją jako wykonaną.
4. Wprowadza klientów `ALPHA00001` i `BRAVO00016` z `demo/clients/*.yaml` skryptem `scripts/onboard_workflow_a_client.py`: osobna baza biznesowa per klient, DDL z `db/client_business`, uprawnienia, wiersze w control plane. Dostawca telematyki nie jest odpytywany (`--skip-provider-auth-check`). Klienci kończą w stanie `CREATED_DISABLED_STRICT`, pierwszym z dziewięciu stanów maszyny onboardingu; harmonogramy są wyłączone.
5. Tworzy konto administratora portalu.

Narzędzia hostowe (onboarding, bootstrap admina) nie są instalowane na hoście. Działają w jednorazowym kontenerze `tools` z nakładki, w sieci Compose, więc bazy klientów są widoczne pod tą samą nazwą hosta dla skryptów i dla API.

## Czego demo nie zawiera

- **Przejazdów.** Bazy klientów mają pełny schemat, ale są puste. Zasilenie wymaga dostępu do API dostawcy albo syntetycznego generatora przejazdów, którego w tej migawce nie ma.
- **Harmonogramów.** Produkcyjnie joby uruchamia systemd (`ops/systemd`). W demo nic nie działa cyklicznie.
- **Dostarczania dashboardu.** Warstwa `delivery/` to Cloudflare Worker z D1 i R2; demo jej nie wdraża. Sam dashboard kierowcy można obejrzeć bez stacku, zobacz główne README.

## Wymagania i stan weryfikacji

Docker z pluginem Compose w wersji co najmniej 2.24 (nakładka używa `!override` i `!reset`), Python 3 na hoście tylko do wygenerowania pliku env. Pełny przebieg `up.sh` od pustego stanu został wykonany 2026-10-07 na Linuksie z Dockerem 29 i Compose v5: 71 migracji, dwóch klientów, logowanie do portalu.

## Uwaga o obrazie API

Obraz budowany z katalogu `api/` nie zawiera pakietu `ops`, którego od pewnego momentu wymaga `api/platform_prune.py`. Produkcja uruchamia API natywnie z checkoutu repozytorium, gdzie import się rozwiązuje. Nakładka demo montuje `ops/` do kontenera tylko do odczytu.
