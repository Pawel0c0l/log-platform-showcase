# CLAUDE_CODE_IMPLEMENTATION_BRIEF
# Driver Eco Driving Dashboard V1 — pełny pakiet wdrożeniowy

**Klasyfikacja pakietu:** `DRIVER_ECO_DASHBOARD_V1_DESIGN_HANDOFF_IMPLEMENTATION_READY`
**Wersja pakietu:** `v3.1 · 2026-08-18` (jedna obowiązująca wersja; wcześniejsze wersje 1.0 i 2.0 są wycofane)
**Dla:** Claude Code CLI, repozytorium `log-platform` (+ nowa ścieżka dostawy Cloudflare)
**Nie zawiera implementacji produkcyjnej.** Na etapie projektu nie zmieniono kodu, bazy, szablonów, harmonogramów ani zasobów Cloudflare.

---

## 0. Jak tego użyć

```
log-platform/                          # istniejące repo (Python 3.12, Postgres 16, FastAPI, brak ORM)
eco-driving-as-is-audit-handoff/       # AUDYT AS-IS — prawda o obecnym systemie
design-handoff/
  FINAL_HANDOFF_MANIFEST.md              # ← przeczytaj pierwszy: kto jest autorytetem w czym
  CLAUDE_CODE_IMPLEMENTATION_BRIEF.md    # ← TEN PLIK: kontrakt wdrożenia
  DESIGN_HANDOFF_INDEX.md                # log decyzji + zależności
  PRODUCT_UX_CONTRACT.md                 # IA i zachowanie 4 ekranów
  VISUAL_SYSTEM.md                       # tokeny i język wykresów
  COMPONENT_SPECIFICATIONS.md            # C-01…C-17
  SCREEN_SPECIFICATIONS.md               # ekrany blok po bloku
  RESPONSIVE_AND_INTERACTION_SPEC.md     # breakpointy, interakcje
  STATES_AND_EDGE_CASES.md               # stany danych, uprawnień i dostępu
  ACCESSIBILITY_SPEC.md                  # kontrast, klawiatura, niezależność od koloru
  DATA_TO_UI_MAPPING.md                  # DISPLAY vs SEMANTIC + kontrakt snapshotu
  IMPLEMENTATION_HANDOFF.md              # co niezmienne, co elastyczne, co zależność
  VISUAL_REFERENCES.md                   # jak czytać wzorce
Eco Driving Dashboard.dc.html          # KANONICZNY wzorzec produktu (4 ekrany, 9 stanów, mobile, stany dostępu)
Eco Driving Design System.dc.html      # KANONICZNY system wizualny (tokeny, anatomia komponentów)
archive-non-authoritative/             # wycofane szkice — NIE są autorytetem, nie implementuj z nich
```

**Kolejność autorytetu przy konflikcie:**
1. §1 (decyzje właściciela) i §2 (niezmienniki) tego pliku,
2. `eco-driving-as-is-audit-handoff/` — każdy fakt o obecnym systemie (formuły, progi, semantyka okresów, ranking, prywatność),
3. `Eco Driving Dashboard.dc.html` + `Eco Driving Design System.dc.html` — wygląd, układ, tokeny, copy,
4. pozostałe pliki `design-handoff/` — rozwinięcia,
5. własny sąd inżynierski — tylko tam, gdzie nic z powyższych nie rozstrzyga.

Jeśli semantyka danych i wizualia są w sprzeczności: **wygrywa semantyka** (2 przed 3).

**Czego nie zmieniać:** modelu punktowego, progów, wag, skali 100 pkt, klasyfikacji 85/40, bramki 100 km **wyłącznie na poziomie całego okresu raportowego**, semantyki okresów, reguł rankingu, reguł ingestii per klient, wyłączeń prywatności.

**Zasada robocza:** liczby liczy Python (`Decimal` + `ROUND_HALF_UP`) i wysyła gotowe; frontend formatuje i układa, nigdy nie przelicza.

---

## 1. Decyzje właściciela (obowiązujące)

| # | Decyzja | Konsekwencja w kodzie |
|---|---|---|
| 1 | **Prywatne przejazdy** — ALPHA00001 wyklucza, BRAVO00016 wlicza wszystkie przejazdy kierowcy; różnica jest celowa | zachowanie ingestii/punktacji, **bez osobnego traktowania w UI**: brak elementu, brak przypisu, brak copy zależnego od klienta |
| 2 | **Kierowca `EXCLUDED`** dostaje pełny użyteczny dashboard: wynik, klasyfikację, punktację obszarów, wskaźniki, trendy, szczegóły dzienne, coaching | bez pozycji rankingowej, bez delty pozycji, bez udziału grupy; `ranking_position` **nie trafia** do snapshotu; słowo `EXCLUDED` nie jest pokazywane |
| 3 | **Tydzień = zamknięty okres narastający od 1. dnia miesiąca** | porównanie: bieżący zamknięty okres MTD vs poprzedni zamknięty okres MTD tego samego miesiąca; brak tygodni izolowanych w V1 |
| 4 | **Dzień** = liczba zdarzeń + dystans + **status per obszar** z wskaźnika; bez punktów dziennych | brak zbiorczego statusu dnia; brak własnego progu dystansu (patrz §2.4) |
| 5 | **Oś wyniku całkowitego liniowa 0–100** | znacznik = `score` % szerokości; wynik ujemny przypięty do lewej + podany liczbowo |
| 6 | **Nadmierne obroty zostają** w modelu 100 pkt, wizualnie wyciszone, neutralny podpis „pełne 15 pkt w obecnym okresie" | wykluczone z coachingu; **zakaz** twierdzeń o przyczynie technicznej („brak pomiaru", „measurement unavailable"); pełne punkty to wynik, nie brak danych |
| 7 | **Brak nazwiska** | nagłówek „Twoje Eco Driving"; imię nie opuszcza hosta |
| 8 | **Tylko zamknięte okresy** | brak stanu „w toku", brak odpytywania, brak wskaźników czasu rzeczywistego |
| 9 | **Świeżość zawsze widoczna** | `snapshot_updated_at` + data zamknięcia okresu |
| 10 | **Etykiety grup bez zmian**: `bezpieczny` / `akceptowalny` / `niebezpieczny`, zawsze z zakresem punktowym; UI po polsku | — |
| 11 | **Pomarańcz Telematics `#FF7300`** jako motyw programu | akcent marki, nawigacja, „potencjał"; **nigdy** nie oznacza statusu |
| 12 | **Zakaz słowa „migawka"** w interfejsie | dozwolone: „zamknięty okres", „poprzedni okres", „najnowszy tydzień okresu", „Dane zaktualizowane" |
| 13 | **Szczegóły dzienne = układ excelowy** | wiersze = dni, kolumny = obszary, w komórce suma zdarzeń dnia |
| 14 | **W komórkach same liczby** | bez glifów w komórkach, bez kolumny statusu dnia; próg niesie tło komórki |
| 15 | **Tylko 2 obszary oznaczone „blisko progu"** | znacznik ◇, plakietka i karty tylko dla tych dwóch; gaśnie razem z coachingiem |
| 16 | Etykiety obszarów: `AREA_SCORE_METRICS` jest zbiorem kanonicznym | `top_1_validation` / `top_2_validation` mapować, nigdy renderować surowo |
| 17 | Jedna reguła kolorów statusu, derywowana z tabeli punktowej | `LOST_POINTS_COLOR_RULES` z maila nie jest używane |

---

## 2. Niezmienniki — twarde granice implementacji

### 2.1 Kolor pochodzi ze wskaźnika, nigdy z liczby zdarzeń (najważniejszy niezmiennik)

```
surowe zdarzenia + kwalifikujący dystans/ekspozycja
    → wskaźnik naruszeń Eco (całkowity, ROUND_HALF_UP, liczony na hoście)
    → istniejący próg / bucket punktacji Eco
    → punkty vs points_max
    → zielony / żółty / czerwony (lub neutralny)
```

- `green` = `points == points_max`; `yellow` = `0 ≤ points < points_max`; `red` = `points < 0`; `neutral` = wskaźnika nie da się wyliczyć w istniejącym kontrakcie punktacji.
- UI może pokazać „7 zdarzeń", ale kolor pochodzi ze wskaźnika dla tych 7 zdarzeń przy ekspozycji tego dnia/okresu.
- **Dwie identyczne liczby zdarzeń mogą mieć różny kolor.** To jest cel, nie błąd.
- Jedna reguła w całym produkcie — okresowo i dziennie. Zakaz progów lokalnych dla dashboardu.
- `DISPLAY VALUE` (liczba zdarzeń, dystans, liczba przejazdów) nigdy nie steruje kolorem; `SEMANTIC VALUE` (wskaźnik → próg → punkty) steruje wyłącznie kolorem. Rozdział opisany w `DATA_TO_UI_MAPPING.md` §0.

### 2.2 Brak zbiorczego statusu dnia

Nie istnieje `day_status` ani żaden dzienny kolor Eco. Żadna zatwierdzona reguła nie zwija niezależnych obszarów w jedną dzienną ocenę — nie wolno wymyślać „najgorszy obszar wygrywa", średniej, liczby czerwonych obszarów ani wagi. Dzień ma **wyłącznie statusy per obszar**. Wiersz/karta może podsumować **dane** dnia (dystans, przejazdy, sumy zdarzeń), ale nie przypisuje dniu jednego koloru Eco.

### 2.3 Fail closed — niekompletna punktacja to nie dashboard

Okres, którego snapshot nie spełnia kompletnego kontraktu punktacji Eco, **nie jest publikowany jako dashboard kierowcy**. Serwowany jest stan `REPORT_NOT_READY`. Zakaz renderowania ekranu, który wygląda kompletnie, pomijając obszar wymagany do wyliczenia bieżącego wyniku, i zakaz pokazywania częściowego wyniku. Obszar, którego istniejący kontrakt punktacji zwraca poprawną wartość, to normalne dane — **nadmierne obroty na pełnych punktach nie są brakiem danych**.

### 2.4 Brak progu dystansu dziennego — twarda decyzja właściciela

Nie ma stałej typu `MIN_DAILY_EVALUATION_KM` i nie wolno jej wprowadzać. **Nie istnieje dzienna bramka 50 km ani 100 km.** Po zakwalifikowaniu całego okresu raportowego każdy dzień z `dystans > 0` jest normalnie pokazywany, nawet jeśli ma 1–99 km: wyświetl dystans i surowe sumy wykroczeń, a współczynnik/status każdej kategorii policz z faktycznego dziennego dystansu zgodnie z istniejącą formułą. `dystans == 0` / brak jazdy kwalifikującej → neutralny stan „brak jazdy". Nie wyprowadzaj z repozytorium żadnego dodatkowego progu dziennego — autorytatywna bramka 100 km dotyczy wyłącznie sumy dystansu całego okresu raportowego.

### 2.5 Semantyka okresów

- **Tydzień jest narastający.** `period_start_date` = 1. dzień miesiąca; rośnie tylko `period_end_date`. Porównanie: bieżący zamknięty okres MTD vs poprzedni zamknięty okres MTD (np. `01–14 sie` wobec `01–07 sie`, **nie** `08–14 sie` wobec `01–07 sie`). Zawsze pełne daty obu okresów. Ciągi „ostatni tydzień", „w tym tygodniu", „last week" są zabronione w copy i w kodzie.
- **Zamknięcie miesiąca zamyka serię.** Narastanie nigdy nie przechodzi przez granicę miesiąca; nowy miesiąc rozpoczyna nową serię. Przy różnych długościach porównywanych okresów nie sugeruj równoważności — etykietuj daty.
- **Miesiąc** porównuje się z poprzednim zamkniętym miesiącem; przebieg wewnątrz miesiąca odtwarzany z jego zamkniętych okresów narastających i tak podpisany.

### 2.6 Uprawnienia rankingu

`ranking_position`, `ranking_total_participants` i `rating_group_share_percent` istnieją w payloadzie **wyłącznie** dla `ranking_state == RANKED`. Nigdy nie fabrykuj delty z nieistniejącej pozycji. Przejścia: ranked→ranked (pozycja + populacja + poprzednia + ruch), nie-ranked→ranked (pozycja + „Nowo w rankingu", bez delty), ranked→nie-ranked (bez delty, bez pozycji; poprzednia pozycja jako kontekst z datami), nie-ranked→nie-ranked (brak elementu rankingu; dashboard nadal użyteczny).

### 2.7 Coaching deterministyczny

Cztery wnioski, liczone na hoście, bez modelu i losowości (dokładne reguły w §5.4). `LARGEST_LOSS` na utraconych punktach; `MOST_IMPROVED` i `MOST_DETERIORATED` na **ruchu wskaźnika** (bez wymogu przejścia progu); `BEST_OPPORTUNITY` na wskaźniku wobec najbliższego lepszego istniejącego progu + deterministyczny zysk punktowy. Nigdy o nadmiernych obrotach; przy okresie poniżej 100 km coaching w ogóle nie jest renderowany, nigdy prognoz ani prawdopodobieństw.

### 2.8 Zakaz języka „budżetu wykroczeń"

Nie prezentuj wartości pochodnych jako dopuszczalnej liczby zdarzeń („nie więcej niż N zdarzeń", „możesz mieć jeszcze N"). Kanoniczna metryka poprawy to **wskaźnik i próg punktowy**. Surowe sumy zdarzeń pozostają informacją opisową.

### 2.9 Minimalizacja payloadu i prywatność

Snapshot jest **prezentacyjny**: zawiera tylko to, co dashboard renderuje. Brak `driver_key`, brak `client_code` (Worker rozwiązuje tożsamość wewnętrznie, wybierając obiekt w R2 — to nie staje się polem payloadu). Brak nazwiska, e-maila, telefonu, identyfikatora pracownika, rejestracji, lokalizacji, trasy, GPS, wyników innych kierowców. Jeden snapshot jednego kierowcy na odpowiedź; nigdy paczka klienta filtrowana w przeglądarce.

### 2.10 Brak punktów dziennych

Punkty dzienne nie sumują się do wyniku okresu, więc nie są pokazywane w żadnym widoku.

### 2.11 Brak przewijania w poziomie

Ani strona, ani komponent. Siatka dzienna transformuje się wraz z dostępną szerokością (§6.3); przy dużym powiększeniu obowiązuje ta sama transformacja, nie kontener przewijany.

### 2.12 Kolor nigdy nie jest jedynym nośnikiem znaczenia

Każdy stan semantyczny ma kolor **i** znak **i** słowo. Kierunek zmiany zawsze z wartością i jednostką, nie samą strzałką ani kolorem.

### 2.13 Sumy muszą się zgadzać

Sumy dzienne (dystans i każdy obszar) równe wartościom okresu z Podsumowania. Nieznana wartość to `brak`, nigdy `0`.

---

## 3. Zakres

| id | `period_type` | `view` | Pytanie kierowcy |
|---|---|---|---|
| `W-SUM` | weekly | summary | „Jak stoję w tym miesiącu i co zmienić?" |
| `W-DET` | weekly | detailed | „Które dni miesiąca dały ten wynik?" |
| `M-SUM` | monthly | summary | „Jak zamknął się miesiąc wobec poprzedniego?" |
| `M-DET` | monthly | detailed | „Które dni zamkniętego miesiąca dały ten wynik?" |

Komponenty współdzielone przez wszystkie cztery: `C-01` PeriodHeader, `C-02` ScoreRing/ScoreCard, `C-03` TotalScoreTrack, `C-04` DeltaChip, `C-05`/`C-06` Ranking / RankingNotice, `C-07` GroupShare, `C-07b` LossDistributionRing, `C-08` DistanceKpi, `C-09` CategoryRow, `C-10` ScoringAxis, `C-11` PeriodTrend, `C-12` CategoryChangeRows, `C-13` Coaching, `C-14` DayGrid, `C-15` StatusMarker, `C-16` PeriodViewNav, `C-17` Access/Unavailable states.

Poza zakresem V1: widoki operatora i flotowe, dane na żywo, logowanie, eksport, zmiana szablonów e-mail, tygodnie izolowane, tryb ciemny, przełącznik języka.

---

## 4. Kontrakt snapshotu (to, czego oczekuje frontend)

### 4.0 Bramka 100 km — tylko cały okres raportowy

To twardy kontrakt biznesowy. `100 km` odnosi się wyłącznie do **łącznego kwalifikującego dystansu całego zamkniętego okresu raportowego**.

- Gdy cały okres ma `< 100 km`: nie renderuj i nie wysyłaj do normalnego dashboardu żadnych danych Eco Driving. Zwróć minimalny stan `INSUFFICIENT_DISTANCE` z identyfikacją zakresu dat/freshness i informacją, że okres nie osiągnął progu 100 km. Nie pokazuj nawet rzeczywistego dystansu, wyników, rankingu, klasyfikacji, kategorii, sum wykroczeń, trendów, coachingu ani dni.
- Gdy cały okres ma `>= 100 km`: dashboard może zostać opublikowany, o ile scoring jest kompletny.
- Dla dni wewnątrz zakwalifikowanego okresu nie ma ani progu 50 km, ani 100 km. Dzień z 1–99 km jest pokazywany normalnie: dystans + surowe sumy wykroczeń. Przy `day.kilometers > 0` współczynniki/statusy kategorii liczy się z faktycznego dziennego dystansu. `0 km` = neutralny „brak jazdy”.



Jeden dokument JSON na kierowcę i typ okresu, prezentacyjny. Przeglądarka dostaje dokładnie jedną snapshot.

```jsonc
{
  "schema_version": 1,
  "generated_at_utc": "2026-07-20T04:40:11Z",
  "timezone": "Europe/Warsaw",
  "locale": "pl-PL",
  "constants": {
    "min_qualifying_distance_km": 100,        // istniejąca bramka kwalifikacji Eco
    "rating_thresholds": { "safe": 85, "acceptable": 40 },
    "score_max": 100
  },
  "periods": {
    "weekly":  { "current": <PeriodBlock>, "previous": <PeriodBlock|null>, "series": [<SeriesPoint>] },
    "monthly": { "current": <PeriodBlock>, "previous": <PeriodBlock|null>, "series": [<SeriesPoint>] }
  }
}

// PeriodBlock
{
  "period_type": "weekly" | "monthly",
  "period_label": "2026-07-W3",
  "period_start_date": "2026-07-01",          // weekly: ZAWSZE 1. dzień miesiąca
  "period_end_date_exclusive": "2026-07-20",
  "period_end_date_display": "2026-07-19",
  "period_sequence_in_month": 3,
  "closed_periods_in_month": 5,
  "snapshot_updated_at_utc": "2026-07-20T04:40:11Z",

  "qualification_status": "QUALIFIED" | "LOW_DISTANCE" | "NO_DISTANCE",
  "scoring_complete": true,                   // false ⇒ NIE publikuj jako dashboard (REPORT_NOT_READY)
  "ranking_state": "RANKED" | "NOT_RANKED_BY_CONFIGURATION" | "NOT_ON_ROSTER"
                 | "LEFT_RANKING",

  "eco_score_total": 71,
  "rating_type": "safe" | "acceptable" | "dangerous" | null,
  "ranking_position": 18,                     // TYLKO gdy ranking_state == RANKED
  "ranking_total_participants": 158,          // ta sama reguła
  "rating_group_share_percent": 52.5,         // ta sama reguła
  "rating_group_distribution": { "safe": 39.2, "acceptable": 52.5, "dangerous": 8.3 },

  "total_kilometers": 3418,                   // dystans kwalifikujący
  "trips_count": 214,

  "comparison": {
    "kind": "PREVIOUS_CUMULATIVE_PERIOD" | "PREVIOUS_CLOSED_MONTH" | null,
    "basis_start_date": "2026-07-01",
    "basis_end_date_display": "2026-07-12",
    "comparable": true,                       // false przez granicę przeliczenia rankingu (DEP-04)
    "previous_eco_score_total": 67,
    "previous_ranking_position": 24,          // null, gdy wtedy nie był w rankingu
    "previous_total_kilometers": 2106
  },

  "categories": [ <Category> ],               // zawsze 8, w kolejności REQUIRED_METRICS
  "coaching": [ <Insight> ],                  // 0–4
  "near_threshold": [ <Near> ],               // 0–2, tylko gdy scoring_complete
  "days": [ <Day> ]                            // materializowane przy generowaniu
}

// Category
{
  "key": "idle",
  "label": "Postój na biegu jałowym",
  "short_label": "Postój",
  "deemphasize": false,                        // true dla overrev (wyciszenie wizualne, NIE brak danych)
  "count": 168,                                // DISPLAY VALUE
  "coefficient_per_100km": 5,                  // SEMANTIC VALUE — steruje progiem i kolorem
  "points": 0,
  "points_max": 10,
  "points_lost": -10,
  "status": "green" | "yellow" | "red" | "neutral",
  "bands": [ { "label": "0", "upper_bound": 0, "points_lost": 0, "status": "green" }, … ],
  "marker_band_index": 3,                      // null → nie rysuj znacznika
  "previous_count": 190,
  "previous_coefficient_per_100km": 5,         // WYMAGANE: skalar, nigdy etykieta przedziału
  "previous_points_lost": -10
}

// Near  (max 2 — decyzja 15; tylko gdy scoring_complete)
{
  "category_key": "harsh_acceleration",
  "rank": 1,                                   // 1 = najbliżej progu
  "coefficient_now": 1,
  "target_upper_bound": 0,
  "target_band_label": "0",
  "points_gain": 5,
  "coefficient_distance": 1                    // brak jakiegokolwiek pola "budżetu zdarzeń"
}

// Day  (bez punktów, bez day_status!)
{
  "date": "2026-07-14",
  "weekday_short": "Wt",
  "kilometers": 283,                            // 0 ⇒ neutralny stan „brak jazdy"
  "trips_count": 11,
  "categories": [
    { "key": "idle", "count": 15, "coefficient_per_100km": 5,
      "band_label": "5", "status": "yellow" }   // status per obszar, niezależnie
  ]
}

// SeriesPoint — jeden zamknięty okres, etykietowany pełnym zakresem
{ "period_label": "2026-07-W2", "start_date": "2026-07-01",
  "end_date_display": "2026-07-12", "eco_score_total": 67, "is_current": false }

// Insight
{ "code": "LARGEST_LOSS" | "MOST_IMPROVED" | "MOST_DETERIORATED" | "BEST_OPPORTUNITY",
  "category_key": "idle",
  "selected_by": "points_lost" | "coefficient_delta" | "threshold_gain",
  "value": { "points": -10 } | { "coefficient_delta": -3 } | { "points_gain": 5 },
  "inputs": { "coefficient": 5, "previous_coefficient": 8, "band_label": "5",
              "points_max": 10, "points_lost": -10, "previous_points_lost": -10,
              "kilometers": 3418, "previous_kilometers": 2106,
              "target_band_label": "3–4", "target_upper_bound": 4 } }
```

### 4.1 Asercje przed publikacją snapshotu (fail closed)

```
A1  eco_score_total == 100 + Σ categories[].points_lost        (wszystkie 8)
A2  categories[].points == points_max + points_lost
A3  status: points == points_max → green
            0 <= points < points_max → yellow
            points < 0 → red
            coefficient_per_100km == null → neutral
A4  marker_band_index = pierwszy próg, którego points_lost == points_lost kategorii; brak → null
A5  ranking_position / ranking_total_participants / rating_group_share_percent
       obecne ⟺ ranking_state == "RANKED"
A6  period_end_date_display == period_end_date_exclusive − 1 dzień
A7  weekly: period_start_date == pierwszy dzień miesiąca z period_label
A8  Σ days[].kilometers == total_kilometers
A9  dla każdej kategorii: Σ days[].categories[key].count == categories[key].count
       (rozdział metodą największych reszt)
A10 scoring_complete == false ⟹ snapshot NIE jest publikowany jako dashboard
A11 qualification_status != QUALIFIED ⟹ NIE publikuj normalnego PeriodBlock; Worker zwraca minimalny
       `INSUFFICIENT_DISTANCE` bez actual distance, score, rating, rankingu, kategorii, trendów, coachingu i dni
A12 brak pól: driver_key, client_code, day_status, min_daily_evaluation_km,
       jakiekolwiek pole „budżetu zdarzeń", oraz cokolwiek z §7
A13 near_threshold.length <= 2 i niepuste tylko gdy scoring_complete
```
Niespełniona asercja = **nie publikuj**, zwróć błąd runu (tak działa dziś reszta pipeline'u, `SCORING_RANKING_CONTRACT.md` §7).

---

## 5. Derywacje do napisania

### 5.1 `ranking_state`
```python
# qualification_status != QUALIFIED jest obsłużone wcześniej przez INSUFFICIENT_DISTANCE
if ranking_group == "INCLUDED":             -> "RANKED"
elif ranking_group == "EXCLUDED":           -> "NOT_RANKED_BY_CONFIGURATION"
elif ranking_group == "UNKNOWN_DRIVER":     -> "NOT_ON_ROSTER"
# poprzedni okres RANKED, obecny nie -> "LEFT_RANKING"
```
Kierowca nie może rozróżnić `NOT_RANKED_BY_CONFIGURATION` od `NOT_ON_ROSTER` — ten sam komunikat. Surowe `ranking_group` / `ranking_included` nie opuszczają hosta.

### 5.2 Status obszaru i próg docelowy
Reguła statusu: §2.1. Próg docelowy = `marker_band_index − 1`; `points_gain = bands[i−1].points_lost − bands[i].points_lost`; wymagany wskaźnik = `upper_bound(bands[i−1])`. Komunikuj wskaźnik i próg — **nigdy liczby dopuszczalnych zdarzeń** (§2.8).

### 5.3 Dwa obszary „blisko progu"
```
warunek wstępny: scoring_complete == true            # inaczej near = []
kandydaci = kategorie gdzie deemphasize == false
            and points_lost < 0 and marker_band_index > 0
sort po (coefficient − target_upper) rosnąco, potem points_gain malejąco,
        potem kolejność deklaracji REQUIRED_METRICS
near = pierwsze 2      # tylko one dostają znacznik ◇, plakietkę i kartę
```

### 5.4 Coaching — dokładne reguły
```
kandydaci = kategorie gdzie deemphasize == false and points_lost != null

LARGEST_LOSS       = min(points_lost)                    # najbardziej ujemny → największy koszt punktowy
                     remis → kolejność REQUIRED_METRICS

# poprawa i pogorszenie to RUCH WSKAŹNIKA, nie zmiana progu
porownywalne = kandydaci gdzie previous_coefficient_per_100km != null
                              and coefficient_per_100km != null
MOST_IMPROVED      = max(previous_coefficient − coefficient) > 0
MOST_DETERIORATED  = max(coefficient − previous_coefficient) > 0
# konsekwencję punktową RAPORTUJEMY, gdy istnieje — nigdy nie służy do wyboru
# kierowca może poprawić się istotnie zostając w tym samym progu punktowym

BEST_OPPORTUNITY   = jak §5.3, pierwszy kandydat
```
Zwracaj 0–4 wnioski; **nie dopełniaj** do czterech. Wybór jest deterministyczny i dzieje się na hoście; zdania buduje UI z `inputs` (copy poprawialne bez regeneracji snapshotów). Każda karta musi pozwolić kierowcy zobaczyć: która metryka wywołała wniosek, co się zmieniło lub co jest teraz istotne, i jaka jest konsekwencja punktowa, jeśli istnieje. To wymóg **semantyczny** — nie osobne pole „Dlaczego" (zostało usunięte).

### 5.5 Dzienne wiersze
1. Grupuj `eco_*_trip_assignments` po `(driver, (trip_start_ts AT TIME ZONE 'Europe/Warsaw')::date)`; przejazd należy w całości do dnia **rozpoczęcia**.
2. Wskaźnik dnia per obszar = `count / km × 100`, HALF_UP, ten sam zbiór istniejących progów co okres.
3. `km == 0` → neutralny stan „brak jazdy": komórki `0`, brak oceny, wiersz zostaje (kalendarz okresu kompletny).
4. **Nie licz zbiorczego statusu dnia** (§2.2) i **nie wprowadzaj progu dystansu** (§2.4).
5. Nieznana liczba okresowa obszaru → `null` w dniach i `brak` w sumach (nigdy `0`).
6. **Materializuj** dni w snapshocie (retencja `eco_*_trip_assignments` to `DEP-07`).
7. Sumy muszą spełniać A8/A9.

### 5.6 Trend
Weekly: kolejne zamknięte okresy narastające bieżącego miesiąca. Monthly: te same punkty jako kształt zamkniętego miesiąca + wynik poprzedniego miesiąca jako linia odniesienia (nie jako słupek serii). Każdy punkt to odrębny zamknięty okres z pełnym zakresem dat i własną wartością; linia łączy wierzchołki, ale **nic między słupkami nie jest danymi** — brak wygładzania, interpolacji, wypełnienia obszaru i metafor strumieniowych. Brakującego okresu nie interpoluj (AS-IS `G-08`: `2026-08-W1` nie powstał).

---

## 6. Ekrany i komponenty

### 6.1 Powłoka (wszystkie ekrany)
Nagłówek: znak programu (pomarańczowy gradient) + „PROGRAM ECODRIVING" + „Twoje Eco Driving"; po prawej „Dane zaktualizowane: DD.MM.RRRR, HH:MM" i pigułka okresu (Tygodniowy / Miesięczny). Karta okresu: rodzaj okresu, zakres dat, chip narastania/zamknięcia, chip kolejności („3. z 5 zamkniętych okresów lipca · zamknięty 20.07.2026"), po prawej baza porównania z pełnymi datami i zdaniem wyjaśniającym różnicę długości. Zakładki: „Podsumowanie" / „Szczegóły dzienne". Bez nazwiska. Wysokość powłoki stała między widokami.

### 6.2 Podsumowanie — kolejność bloków (niezmienna)
1. **Kafle KPI**: Wynik Eco (`71 / 100` + chip zmiany), Pozycja w rankingu (`18 / 158` + ruch) **albo** stan bez rankingu, Twoja grupa (`52,5 %`, tylko gdy `RANKED` i jest wynik), Dystans kwalifikujący.
2. **Wynik okresu** — `C-02` pierścień (zewnętrzny = obecny okres w kolorze progu, wewnętrzny = poprzedni okres), liczba w wariancie tekstowym progu, plakietka klasyfikacji z zakresem, lista (zmiana vs baza / do progu 85 / utracone punkty łącznie / klasyfikacja), `C-03` pasek 0–100 z liniami 40 i 85.
3. **Udział strat według obszarów** — `C-07b` pierścień z odczytem w środku; legenda to przyciski (klik rozwija oś obszaru); paleta §3.5 `VISUAL_SYSTEM`; segmenty `aria-hidden`.
4. **Co teraz najbardziej wpływa na Twój wynik** — 1–4 kafle `C-13`; kolejność: największa strata, największa poprawa, największe pogorszenie, największy potencjał; wartość w jednostce, która wybrała wniosek (pkt lub „na 100 km").
5. **Kolejne zamknięte okresy** — `C-11` słupki w kolorach progów, ciągła linia przez wierzchołki wszystkich słupków, kropka na każdym, animacja progresywna, linie odniesienia 85 i 40, legenda progów, pełne zakresy dat pod słupkami.
6. **Co się zmieniło w każdym obszarze** — `C-12` pełna szerokość: chipy podsumowania (X poprawa / Y pogorszenie / Z bez zmian; ukryte gdy nie ma bazy), wskaźnik „poprzednio → teraz" jako skalary, para słupków utraconych punktów z wartościami, chip zmiany.
7. **Tu niewielka poprawa daje punkty** — dokładnie 2 karty-przyciski (wskaźnik teraz → próg, brakujący wskaźnik, zysk punktowy, „razem +N pkt"); gaśnie razem z coachingiem.
8. **Punkty według obszarów** — `C-09` 8 wierszy: obszar (+ plakietka „blisko progu" dla dwóch), liczba zdarzeń, chip wskaźnika ze statusem, mini-drabina progów ze znacznikiem ▲ (◇ tylko dla dwóch), punkty `4 / 10` + strata, rozwinięcie → `C-10` pełna oś z trzema panelami (stan obecny / potencjał / zmiana).

### 6.3 Szczegóły dzienne — układ excelowy (decyzje 13–14)
```
Dzień │ Dystans │ Hamowania │ Przyspieszenia │ Skręty │ Postój │ 140–160 │ 160–170 │ >170 │ ⌄
▸ 01.07 – 05.07 │  948 km │ 20 │  9 │ 58 │ 47 │ 11 │ 3 │ 0 │
▾ 13.07 – 19.07 │ 1365 km │ 30 │ 15 │ 82 │ 66 │ 16 │ 2 │ 0 │   ← „najnowszy tydzień okresu"
    13.07 Pn    │  161 km │  3 │  2 │  9 │  7 │  2 │ 0 │ 0 │ +
    …
══ Suma okresu │ 3 418 km │ 74 │ 34 │ 202 │ 168 │ 41 │ 8 │ 0 ══   ← ciemny wiersz
```
- w komórce **sama liczba**; tło = próg wskaźnika tego dnia w tym obszarze; bez glifów, bez kolumny statusu, bez zbiorczego koloru dnia;
- każda komórka ma `title` **i** `aria-label`: „Postój na biegu jałowym: 7 zdarzeń · 4 na 100 km · próg 3–4" (lub „brak danych" / „brak przejazdów kwalifikujących" / „brak oceny dla tego dnia");
- nagłówki tygodni to `<button aria-expanded>` z `padding: 0` (inaczej kolumny się rozjeżdżają);
- wąska kolumna końcowa: `<button aria-expanded>` rozwijający wskaźniki dnia;
- 4 kafle KPI: dystans w okresie, dni z jazdą, dni z wyliczonym wskaźnikiem, wskaźnik okresu („liczone raz dla całego okresu");
- legenda: cztery próbki tintów ze słowami + zdanie, że dzienne punkty się nie sumują;
- **bez przewijania w poziomie**: ≥ 1024 px pełna siatka; 700–1023 px ta sama siatka ze skróconymi etykietami i mniejszym tekstem komórek; < 700 px karty dni (data + dzień tygodnia, neutralny chip dystans + przejazdy, chipy obszarów, „Pokaż wskaźniki dnia"); przy dużym powiększeniu ta sama transformacja;
- miesięcznie 28–31 wierszy: tygodnie zwinięte poza tym z największą liczbą dni ze stratą punktów.

### 6.4 Mobile (≤ 700 px)
Kafle KPI 2×2, pierścień mniejszy, oś punktacji **pionowa** (jeden wiersz na próg, kolorowa szyna po lewej), siatka dzienna → karty dni, kolejność coachingu: potencjał przed historią. Cele dotykowe ≥ 44 px. Brak przewijania w poziomie w jakimkolwiek widoku.

### 6.5 Stany dostępu i braku danych
`INVALID_LINK` · `LINK_EXPIRED` · `INSUFFICIENT_DISTANCE` · `REPORT_NOT_READY` · `SNAPSHOT_UNAVAILABLE` · `SERVICE_UNAVAILABLE` — copy w §9.4 i `STATES_AND_EDGE_CASES.md` §5. Bez formularza logowania, bez pola e-mail, bez informacji, czy link istniał, bez tożsamości kierowcy, bez echa tokenu.

---

## 7. Prywatność — lista zakazana

Nigdy w snapshocie, w DOM, w logach frontendu ani w URL-u:
`driver_key` · `client_code` · `driver_name` · `person_name` · `driver_surname` · `person_name_group_key` · `assigned_id` · `source_person_id` · e-mail · telefon · identyfikator pracownika · `ranking_included` · `ranking_group` · `ranking_position` dla nie-`INCLUDED` · `client_id` · `provider_trip_id` · `record_id` · `trip_start_ts`/`trip_end_ts` per przejazd · `driver_tag_description` · `trip_mode` · rejestracja i dane pojazdu · współrzędne, lokalizacje, geofence, licznik · hosty/hasła/sekrety · **jakikolwiek wynik lub pozycja innego kierowcy**.

Dwie pułapki: (1) widoki trendów dołączają roster i wystawiają `driver_name` oraz `email` — projektuj zapytanie tak, by je odrzucić; (2) `ranking_position` dla `EXCLUDED` to realna liczba bez znaczenia (druga liga 927 kierowców w ALPHA00001) — filtruj w **generatorze**, nie w UI.

---

## 8. Dostępność (kontrakt)

- **Kolor + znak + słowo** dla każdego stanu semantycznego. Wyjątek dla komórek siatki dziennej (same liczby) ma pięć kompensacji (`ACCESSIBILITY_SPEC.md` §1.1): liczba jako tekst, dostępna nazwa komórki z wartościami słownie, legenda tintów, chipy obszarów na mobile, rozwinięty panel dnia. Nie przywracaj glifów w komórkach i nie usuwaj kompensacji.
- **Kierunek zmiany** nigdy samą strzałką ani kolorem: zawsze wartość + jednostka („▲ 4 pkt", „▼ 3 na 100 km", „6 miejsc w górę").
- **Kontrast:** tekst < 24 px ≥ 4,5:1, ≥ 24 px ≥ 3:1. Tokeny wypełnień (`#1F8A4C`, `#F5B700`, `#DC2626`) **tylko** na pierścieniach, słupkach, progach i obwódkach; tokeny tekstowe (`#14653B`, `#7C3A05`, `#912018`, neutralne `#101828`/`#344054`/`#475467`/`#667085`) dla liczb i etykiet.
- **Klawiatura:** zakładki jako `role="tablist"` ze strzałkami; każde rozwinięcie to `<button aria-expanded>` (obszary, dni, tygodnie, karty „blisko progu", wiersze legendy pierścienia); widoczny focus 2 px; kolejność DOM = kolejność wizualna; skip link.
- **Bez informacji tylko w tooltipie.** Tooltip zawsze duplikuje tekst dostępny inaczej; hover ma odpowiednik na `focus`; ujawnianie treści działa na dotyku i klawiaturze.
- **Wykresy:** `role="img"` + `aria-label` dla pierścienia wyniku i paska 0–100; `role="list"` dla słupków okresów (każdy nazwany pełnym zakresem i wartością, więc każdy zamknięty okres jest identyfikowalny bez linii); segmenty pierścienia strat `aria-hidden`, kontrolką są wiersze legendy.
- **Cele dotykowe** ≥ 44 × 44 px; `prefers-reduced-motion` → 0 ms; użyteczność przy 320 px i 400 % powiększenia bez przewijania w poziomie.

---

## 9. Copy (polski)

### 9.1 Zabronione
„migawka" (słowo zabronione w UI; w tym dokumencie technicznym używamy „snapshot" dla artefaktu danych) · „ostatni tydzień" / „w tym tygodniu" · `EXCLUDED` / `INCLUDED` / `UNKNOWN_DRIVER` / `ranking_included` / `LOW_DISTANCE` · nazwy tabel i kolumn · treści motywacyjne · prognozy wyniku · „wszystko, co przejechałeś" · dopuszczalna liczba wykroczeń · twierdzenia o przyczynie technicznej pełnych punktów nadmiernych obrotów.

### 9.2 Dozwolone odpowiedniki
„zamknięty okres", „poprzedni okres", „okres liczony od 1. dnia miesiąca", „najnowszy tydzień okresu", „Dane zaktualizowane", „dystans kwalifikujący", „ten okres jest bez rankingu", „pełne 15 pkt w obecnym okresie".

### 9.3 Reguły
Wskaźnik zawsze z jednostką „na 100 km". Klasyfikacja zawsze z zakresem („akceptowalny · próg 40–84 pkt"). Zmiana zawsze ze znakiem, strzałką i jednostką. Liczby: `pl-PL`, przecinek dziesiętny, wąska spacja nierozdzielająca w tysiącach (`3 418`), minus typograficzny (`−5`). Daty `DD.MM.RRRR`, koniec okresu = `period_end_date − 1 dzień`.

### 9.4 Stany dostępu — dosłownie
| Kod | Tytuł | Treść |
|---|---|---|
| `INVALID_LINK` | Ten link nie jest prawidłowy | Otwórz panel z najnowszej wiadomości Programu Ecodriving. Linki są indywidualne i nie da się ich odtworzyć ręcznie. |
| `LINK_EXPIRED` | Link stracił ważność | Każde podsumowanie ma własny link. Najnowszy znajdziesz w ostatniej wiadomości e-mail z podsumowaniem okresu. |
| `INSUFFICIENT_DISTANCE` | Za mały dystans do wyliczenia wyniku | Dla tego zamkniętego okresu łączny dystans kwalifikujący nie osiągnął 100 km. Zgodnie z zasadami Eco Driving nie pokazujemy danych ani wyniku dla tego okresu. |
| `REPORT_NOT_READY` | Raport tego okresu nie jest jeszcze gotowy | Wynik powstaje ze wszystkich obszarów punktacji razem. Gdy komplet danych będzie dostępny, zobaczysz pełne podsumowanie tego okresu. |
| `SNAPSHOT_UNAVAILABLE` | Ten okres nie jest jeszcze dostępny | Panel pokazuje tylko zamknięte okresy sprawozdawcze. Gdy okres zostanie zamknięty i przeliczony, pojawi się tutaj. |
| `SERVICE_UNAVAILABLE` | Panel jest chwilowo niedostępny | Spróbuj ponownie za kilka minut. Twoje dane nie zostały utracone — nic nie jest liczone w tej przeglądarce. |

---

## 10. Architektura dostawy

```
Lenovo T14s (host obliczeń, bez zmian)
  Postgres 16 ─► eco_* stats + eco_*_trip_assignments
      ├─ NOWE: job generatora snapshotów   run(client, run_id, params), dry-run first, 1 transakcja
      ├─ NOWE: publisher ─► Cloudflare R2 (prywatny bucket, jurysdykcja EU)
      ▼
Cloudflare Pages (statyczny frontend) ──fetch──► Cloudflare Worker (brama dostępu)
                                                  │ waliduje token zdolnościowy
                                                  │ token → DOKŁADNIE JEDEN klucz obiektu
                                                  │ (mapowanie wewnętrzne; nie trafia do payloadu)
                                                  ▼
                                                R2 ──► prezentacyjny snapshot jednego kierowcy
```
Nienegocjowalne: przeglądarka dostaje jeden prezentacyjny snapshot; klucz w R2 nie jest tożsamością kierowcy (nie `assigned_id`, nie `person_name_group_key`, nie hash z nich); tokeny mają unieważnianie, wygaszanie i rotację przez mutowalne mapowanie (KV/D1), nie przez przekształcenie stringa; generator to zarejestrowany job zgodny z `run(client, run_id, params)`; adapter per rodzina pipeline'u (`eco_driver_*` dla ALPHA00001, `eco_person_*` dla BRAVO00016) wypełnia **jeden** kontrakt snapshotu — nie buduj dwóch dashboardów.

Stack frontendu: dowolny, byle statyczny i bez przeliczania wyników w przeglądarce; zwykły HTML + CSS + niewielki JS wystarcza i jest preferowany. Payload ≤ 60 kB dla miesiąca z 31 dniami. Czcionka: Manrope 400–800, cyfry tabularne, subset latin-ext, `display=swap` + fallback systemowy. Brak rodziny monospace.

---

## 11. Kolejność budowy

1. **Generator snapshotów** jako job: jeden kierowca, jeden okres, asercje A1–A13, dry-run, fail closed.
2. **Derywacje**: `ranking_state`, status obszaru, progi/znacznik/cel, wartości poprzedniego okresu per obszar (`DEP-03`), dni + rekoncyliacja sum, wybór coachingu, `near_threshold`.
3. **Frontend — powłoka + wynik okresu + oś**: to jest produkt; dopiero gdy reguła statusu i oś są poprawne, buduj resztę.
4. **Podsumowanie**: kafle, stany rankingu, pierścień strat, coaching, trend okresów, sekcja zmian, tabela obszarów z osiami.
5. **Szczegóły dzienne**: siatka excelowa, grupy tygodni, sumy, rozwinięcia, transformacja < 700 px.
6. **Stany**: fixtures poniżej + test zrzutu ekranu każdego.
7. **Ścieżka dostępu**: Worker, mapowanie tokenów, unieważnianie, 5 stanów dostępu.
8. **Przebieg dostępności** wg §8 (greyscale, klawiatura, czytnik ekranu, 320 px / 400 %, axe-core = 0 naruszeń AA).

### Fixtures (po jednym snapshocie, dane wyłącznie syntetyczne)
`ranked_safe` · `ranked_acceptable` · `ranked_dangerous` · `not_ranked_by_configuration` · `not_on_roster` · `newly_ranked` · `left_ranking` · `insufficient_period_distance` · `first_closed_period_of_month` · `end_of_month_period` · `no_comparison` · `report_not_ready` · `zero_event_category` · `no_driving_day` · `skipped_period_in_series` · `monthly_31_days`.

---

## 12. Lista kontrolna odbioru

- [ ] Każdy zielony/żółty/czerwony piksel wynika z łańcucha zdarzenia + ekspozycja → wskaźnik → istniejący próg → punkty; brak progów lokalnych; dwie równe liczby zdarzeń mogą mieć różny kolor.
- [ ] Nigdzie nie istnieje zbiorczy status dnia.
- [ ] Brak stałej progu dystansu dziennego; `km == 0` to neutralny stan „brak jazdy".
- [ ] Niekompletna punktacja → `REPORT_NOT_READY`, nigdy częściowy wynik. Nadmierne obroty na pełnych punktach to poprawne dane.
- [ ] Payload bez `driver_key`, `client_code` i całej listy §7.
- [ ] `MOST_IMPROVED` / `MOST_DETERIORATED` wybrane ruchem wskaźnika, nie zmianą progu.
- [ ] Nigdzie nie ma języka dopuszczalnej liczby wykroczeń.
- [ ] Copy nie zawiera słowa „snapshot" ani „ostatni tydzień"; tydzień czyta się jako narastający MTD; miesiąc porównuje się z poprzednim zamkniętym miesiącem.
- [ ] Stany `RANKED` / `NEWLY_RANKED` / `LEFT_RANKING` / `NOT_RANKED_*` / `INSUFFICIENT_DISTANCE` oraz stany rankingu renderują swoje właściwe warianty; nigdzie pustego slotu.
- [ ] Oś punktacji: progi, utracone punkty, znacznik ▲, cel ◇ tylko w dwóch obszarach.
- [ ] Każdy punkt trendu to identyfikowalny zamknięty okres z pełnym zakresem dat; linia nie sugeruje danych między okresami.
- [ ] Sumy dzienne = wartości okresu; nieznane = `brak`.
- [ ] W zakwalifikowanym okresie dni 1–99 km są normalnie widoczne; nie istnieje dzienny próg 50/100 km. Kolor per kategoria pochodzi z dziennego współczynnika.
- [ ] Brak przewijania w poziomie na każdej szerokości i przy 400 % powiększeniu; 31 wierszy używalne na telefonie.
- [ ] 0 elementów tekstowych poniżej progu kontrastu; pełna obsługa klawiaturą; kierunek zmiany nie zależy od strzałki ani koloru.
- [ ] Brak odpytywania, brak stanu „w toku", świeżość zawsze widoczna.

---

## 13. Zależności inżynierskie (nie decyzje projektowe)

| id | Co jest potrzebne | Skutek braku |
|---|---|---|
| `DEP-01` | Czy istniejący kontrakt punktacji ma autorytatywny warunek nieoceniania (np. minimalna ekspozycja), który widok dzienny ma odwzorować | tylko stan neutralny poza „brak jazdy"; **nie wymyślaj progu** — użyj wskaźnika wszędzie, gdzie istniejący kontrakt go zwraca |
| `DEP-02` | Kadencja odświeżania / włączenie 10 wyłączonych harmonogramów Eco | tylko copy: bez obietnicy „co tydzień" |
| `DEP-03` | Per-obszar `LAG()` po `*_events_per_100km` **i** `*_maxpoints_subtract` | ruch wskaźnika w sekcji zmian oraz `MOST_IMPROVED`/`MOST_DETERIORATED` → degradacja do „brak bazy" |
| `DEP-04` | Które okresy poprzedzają przeliczenie kontraktu rankingu (`G-07`) | `comparison.comparable = false` dla rankingu przez tę granicę |
| `DEP-05` | Generator + publisher + układ R2 + mapowanie token→klucz z unieważnianiem | cała ścieżka dostawy i stany dostępu |
| `DEP-06` | `rating_group_distribution` jako zapytanie na poziomie okresu, kopiowane do każdego snapshotu | pasek rozkładu grup ukryty, własny udział zostaje |
| `DEP-07` | Materializacja `days[]` (retencja `eco_*_trip_assignments`, 365 dni, dziś wyłączona) | Szczegóły dla starszych okresów |
| `DEP-08` | Adapter per rodzina pipeline'u wypełniający jeden kontrakt | jeden dashboard zamiast dwóch |

---

## 14. Poza zakresem V1

Tygodnie izolowane · okres „w toku" · widoki operatora i flotowe · powiadomienia z dashboardu · eksport/druk · przełącznik języka · tryb ciemny · zmiany w `eco_scoring.py` · zmiany szablonów e-mail (osobne zadanie: wycofać `LOST_POINTS_COLOR_RULES` i przestać używać nadmiernych obrotów jako przykładu w legendzie).

---

## 15. Potwierdzenie zakresu

Ten pakiet to design + kontrakt wdrożenia. Nie zmieniono kodu produkcyjnego, bazy, szablonów, harmonogramów, wydań ani zasobów Cloudflare; nie uruchomiono przetwarzania Eco; nie wysłano e-maili; nie utworzono migracji ani commitów; we wszystkich wzorcach użyto wyłącznie danych syntetycznych.
