# Copy and Terminology

Canonical Polish UI vocabulary. **The implementation MUST NOT invent alternative names.** Per `D-010`, every string ships through a translation key even though only the `pl` locale exists.

Suggested key convention: `<module>.<surface>.<element>`, e.g. `data.toolbar.filters`.

---

## 1. Navigation and modules

| Concept | Approved Polish | Notes |
|---|---|---|
| Product name | **Log Platform** | Not translated |
| Report Explorer | **Raporty** | Nav label |
| Database Explorer | **Dane** | Nav label |
| Functional Analytics | **Analizy** | Nav group label |
| Eco Driving module | **Eco Driving** | Not translated — established product name |
| Artifact Explorer | **Artefakty** | Tools group |
| Administration | **Administracja** | Tools group |
| Nav group: data modes | **Praca** | Only visible in the collapsed nav drawer |
| Nav group: tools | **Narzędzia** | Only visible in the collapsed nav drawer |
| Global search | **Szukaj wszędzie** | Placeholder |

> The three data-access modes are named for what they are **to the user**, not for what they are in the database. `Reports`/`Data`/`Eco Driving` → `Raporty`/`Dane`/`Analizy`, with Eco Driving as the first module inside `Analizy`.

## 2. Client and dataset

| Concept | Approved Polish |
|---|---|
| Client | **Klient** |
| Client code | shown as a bare mono chip, no label |
| Dataset | **Zbiór danych** (plural **Zbiory danych**) |
| Dataset catalogue page title | **Zbiory danych klientów** |
| Rail heading | **Zbiory danych** |
| Physical table name | shown bare in mono, no label |
| Approved columns | **zatwierdzone kolumny** |
| Read-only | **TYLKO ODCZYT** |
| Row | **wiersz** (plural **wiersze**) |
| Column | **kolumna** (plural **kolumny**) |
| Access rules | **Zasady dostępu** |
| Permission: filtering | **Filtrowanie** / **Bez filtrów** |
| Permission: export | **Eksport** / **Tylko podgląd** |
| Saved view | **Zapisany widok** (plural **Zapisane widoki**) |
| Column set | **Zestaw kolumn**; control label **Zestawy ▾** |

## 3. Table controls

| Concept | Approved Polish |
|---|---|
| Filters button | **Filtry** + count badge |
| Columns button | **Kolumny** + `n/m` |
| Density | **Zwarta** / **Wygodna** |
| Local search placeholder | **Szukaj w `n` kolumnach tekstowych** |
| Add filter | **Dodaj filtr** |
| Find a column | **Znajdź kolumnę** / **Znajdź kolumnę spośród 42** |
| Active filters heading | **Aktywne — `n`** |
| Clear all | **Wyczyść wszystkie** |
| Apply | **Zastosuj** |
| Clear (one) | **Wyczyść** |
| Save as view | **Zapisz jako widok** |
| Save changes | **Zapisz zmiany** |
| Save as column set | **Zapisz jako zestaw** |
| Default columns | **Domyślne kolumny** |
| Rows per page | **wierszy na stronie** |
| Result counter | **`n` z `m` wierszy** |
| Sort summary | **sortowanie `<kolumna>` ↓** |
| Copy hint | **zaznacz zakres i ⌘C, aby skopiować do arkusza** |
| Tabs in the column panel | **Wszystkie** / **Widoczne** / **Ukryte** |

### 3.1 Column menu

| Concept | Approved Polish |
|---|---|
| Sort ascending (text) | **Sortuj A → Z** |
| Sort descending (text) | **Sortuj Z → A** |
| Sort ascending (other) | **Sortuj rosnąco** |
| Sort descending (other) | **Sortuj malejąco** |
| Text operators | **zawiera** · **=** · **≠** · **puste** |
| Numeric operators | **=** · **≠** · **>** · **≥** · **<** · **≤** · **od–do** · **puste** |
| Date operators | **przed** · **po** · **między** · **puste** |
| Boolean options | **wszystko** · **tak** · **nie** · **puste** |
| Distinct values heading | **Wartości w kolumnie** |
| Distinct count | **`n` unikalnych** |
| Distribution heading | **Rozkład wartości** |
| Pin left | **Przypnij kolumnę po lewej** |
| Hide column | **Ukryj kolumnę** |
| Autofit | **Dopasuj szerokość do treści** |
| Keyboard hint | **↵ zastosuj · esc** |
| Non-null count | **`n` niepustych** |

### 3.2 Value markers

| Concept | Approved Polish | Style |
|---|---|---|
| SQL `NULL` | **brak wartości** | italic, `text/faint` |
| Empty string | **pusty tekst** | italic, `text/faint` |
| Boolean true | **TAK** | badge |
| Boolean false | **NIE** | badge |
| Collapsed object | **{ `n` pola }** | mono |
| Collapsed array | **[ `n` elementów ]** | mono |
| Not applicable | **—** | em dash |

## 4. Date presets

| Concept | Approved Polish |
|---|---|
| Today | **Dziś** |
| Yesterday | **Wczoraj** |
| Last 7 days | **7 dni** |
| Last 30 days | **30 dni** |
| Named current month | **Lipiec 2026** (month name capitalised) |
| Previous month | **Poprzedni miesiąc** |
| From / To | **Od** / **Do** |
| Date range section | **Zakres daty — `<kolumna>`** |

## 5. Export

| Concept | Approved Polish |
|---|---|
| Export action | **Eksport** |
| Export panel title | **Eksport** |
| Scope heading | **Zakres** |
| Scope: current view | **Bieżący widok** |
| Scope: whole dataset | **Cały zbiór danych** |
| Scope: selected rows | **Zaznaczone wiersze** |
| Columns heading | **Kolumny** |
| Columns: as on screen | **Jak na ekranie** |
| Columns: all approved | **Wszystkie zatwierdzone** |
| Format heading | **Format** |
| Formats | **XLSX** / **CSV** |
| Immediate path | **Pobranie natychmiastowe** |
| Download button | **Pobierz XLSX** / **Pobierz** |
| Cancel | **Anuluj** |
| Background exports page | **Eksporty danych** |
| Link to that page | **Moje eksporty danych** |
| Recent exports | **Ostatnie eksporty** |
| Request again | **Zleć ponownie** |
| Copy reference | **Kopiuj ref** / **Skopiuj referencję** |
| Available until | **Dostępny do** |
| Requested by/at | **zlecono `<data>` · `<osoba>`** |
| App-bar indicator | **`n` eksport w toku** |

### 5.1 Export states

| State | Approved Polish |
|---|---|
| In progress | **W toku** |
| Ready | **Gotowy** |
| Files expired | **Pliki wygasły** |
| Failed | **Błąd** |

## 6. Report Explorer

| Concept | Approved Polish |
|---|---|
| Report | **Raport** |
| Report type | **Typ raportu**; rail heading **Typy raportów** |
| Filter the rail | **Filtruj typy** |
| Report instance search | **Nazwa raportu lub okres** |
| Reporting period | **Okres raportowania** |
| Period ordinal | **Numer okresu** |
| Cycle | **Cykl** |
| Cycles | **tygodniowy** · **miesięczny** · **kwartalny** · **na żądanie** |
| Generated at | **Wygenerowano** |
| Files | **Pliki**; panel heading **Pliki w tej pozycji** |
| Rows in report | **Wiersze w raporcie** |
| File retention | **Retencja plików** |
| Open report | **Otwórz raport** |
| Download | **Pobierz** |
| Download all | **Pobierz wszystkie (`n`)** |
| Preview | **Podgląd** |
| Full screen | **Pełny ekran** |
| Source data | **Dane źródłowe** |
| History | **Historia tego raportu** |
| Show all periods | **Pokaż wszystkie `n` okresów** |
| Back to library | **‹ Wróć do biblioteki** |
| Filters preserved note | **filtry biblioteki zachowane** |
| Group heading | **Wygenerowane w `<miesiącu>` `<rok>`** |
| Grouping rule note | **grupowanie po miesiącu wygenerowania, najnowsze u góry** |
| Unseen marker | **NOWY** |
| Report a problem | **Zgłoś problem** |
| Subscriptions | **Subskrypcje** |
| File semantics | **dokument główny** · **dane szczegółowe** · **dane surowe** |
| Assignment note heading | **Przypisanie** |

### 6.1 Report states

| State | Approved Polish |
|---|---|
| Ready | **Gotowy** |
| Generating | **W generowaniu** |
| Generation failed | **Błąd generowania** |
| Files expired | **Pliki wygasły** |
| No files yet | **pliki pojawią się po zakończeniu** |
| No files after failure | **brak plików — generowanie nie ukończyło się** |

## 7. Eco Driving

| Concept | Approved Polish |
|---|---|
| Month | **Miesiąc** |
| Weeks in the ranking | **Tygodnie w rankingu** |
| Whole month | **Cały miesiąc** |
| Clear weeks | **Wyczyść** |
| Week label | **W1** … **W5** |
| Partial week | **częściowy** |
| Ranking basis | **Podstawa rankingu** |
| Basis sentence | **W1 + W2 zsumowane · 01–14.07.2026 · 14 dni** |
| Qualification summary | **`n` z `m` kierowców spełnia próg kwalifikacji** |
| Fleet distribution | **Rozkład wyników floty** |
| Median / mean | **mediana** / **średnia** |
| Ranking family badge | **RANKING STANDARDOWY** |
| Data lineage | **Linia danych: zrekonstruowana ze stanu bieżącego** |
| Position | **Poz.** (column) / **Pozycja** (label) |
| Position change | **Δ** |
| Driver | **Kierowca** |
| Driver tag | **Driver tag** |
| Dispatcher | **Dysponent** |
| Score | **Wynik Eco** (column) / **Wynik** (label) |
| Rating | **Ocena** |
| Rating bands | **bardzo dobra** · **dobra** · **przeciętna** · **wymaga uwagi** |
| Qualification | **Kwalifikacja**; values **spełniona** / **niespełniona** |
| Distance | **Dystans (km)** |
| Trips | **Przejazdy** |
| Group | **Grupa** |
| Group values | **w rankingu** · **wykluczeni** · **nieznany kierowca** |
| Driver search | **Kierowca, tag ID lub nr rej.** |
| Jump to driver | **skocz do kierowcy** |
| Details action | **Szczegóły** |
| Recalculated at | **ranking przeliczony `<timestamp>`** |
| Unit toggle label | **Wykroczenia jako** |
| Unit: rate | **/ 100 km** |
| Unit: sum | **Σ suma** |

### 7.1 Violation metrics

| Metric | Approved Polish (long) | Column (short) |
|---|---|---|
| Harsh braking | **Ostre hamowania** | **Ostre ham.** |
| Harsh acceleration | **Przyspieszenia** | **Przysp.** |
| Harsh cornering | **Ostre skręty** | **Skręty** |
| Long idling | **Długi czas postoju** | **Długi postój** |
| High RPM | **Wysokie obroty** | **Wys. obroty** |
| Over 140 km/h | **Przekroczenie 140 km/h** | **> 140** |
| Over 160 km/h | **Przekroczenie 160 km/h** | **> 160** |
| Over 170 km/h | **Przekroczenie 170 km/h** | **> 170** |

### 7.2 Driver detail

| Concept | Approved Polish |
|---|---|
| Back to ranking | **‹ Wróć do rankingu** |
| Context preserved note | **kontekst okresu zachowany** |
| Percentile | **górne `n`% floty** |
| Fleet position | **Gdzie jest w rozkładzie floty** |
| Trend | **Trend wyniku** |
| Trend footnotes | **Zmiana vs poprzedni okres** · **Najlepszy okres** · **Najsłabszy** |
| Compare with fleet | **Porównaj z flotą** |
| Identity section | **Tożsamość wpisu i okres** |
| Persisted-values note | **wartości utrwalone w rankingu** |
| Composition section | **Z czego składa się wynik `n`** |
| Composition arithmetic | **100 pkt bazowo · utracone `n` pkt · sortowanie po utraconych** |
| Metric | **Metryka** |
| Event sum | **Σ zdarzeń** |
| Threshold | **Próg** |
| Points | **Punkty** |
| Maximum | **Maks** |
| Points lost | **Utracone** |
| Share of loss | **Udział w utraconych punktach** |
| Week contribution | **Wkład tygodni** |
| Trips section | **Przejazdy w podstawie rankingu** |
| Trip contribution | **Wkład w wynik** |
| Trip start / end | **Start podróży** / **Koniec podróży** |
| Driving time | **Czas jazdy** |
| Show all trips | **Pokaż wszystkie `n` przejazdy** |
| Export trips | **Eksport przejazdów** |
| Open in Database Explorer | **Otwórz w Przeglądarce danych** |
| Header hint | **każda kolumna sortowalna i filtrowalna z nagłówka** |

### 7.3 Identity field labels

`Klient` · `Dostawca / rodzina rankingu` · `Typ okresu` · `Utrwalona etykieta okresu` · `Początek okresu` · `Koniec okresu (wyłączny)` · `Stan częściowości` · `Numer okresu w miesiącu` · `Przypisane ID (nieprzejrzyste)` · `Grupa rankingowa` · `Status kwalifikacji` · `Status obliczeń` · `Źródło metadanych kierowcy` · `Uczestnicy rankingu` · `Udział pasma oceny` · `Dystans łącznie`

Partiality values: **pełny** / **częściowy**. Calculation status: **zakończone** / **w toku** / **błąd**.

## 8. Artifact Explorer

| Concept | Approved Polish |
|---|---|
| Page title | **Artefakty** |
| Subtitle | **inspekcja artefaktów systemu · nie jest trybem dostępu do danych klienta** |
| Operator mode | **TRYB OPERATORA** |
| Kinds rail | **Rodzaje artefaktów** |
| Search | **Nazwa, hash lub referencja** |
| Refresh | **Odśwież katalog** |
| Columns | **Utworzono** · **Rodzaj** · **Nazwa** · **Klient** · **Hash** · **Rozmiar** · **Stan** |
| Action | **Inspekcja** |
| States | **ZWERYFIKOWANY** · **W TRAKCIE** · **USUNIĘTY** |

## 9. Empty, error and advisory messages

Approved verbatim strings. Placeholders in `⟨⟩`.

### 9.1 Database Explorer

| Situation | Copy |
|---|---|
| Zero rows after filters — title | **Brak wierszy dla aktywnych filtrów** |
| Zero rows after filters — body | **Zbiór ma ⟨48 213⟩ wierszy. Filtr `⟨Depot = KRK-02⟩` zawęża wynik do zera — bez niego zobaczysz ⟨1 274⟩ wiersze.** |
| Zero rows — actions | **Usuń ⟨Depot = KRK-02⟩** · **Wyczyść wszystkie** |
| Data-source error — badge | **BŁĄD ŹRÓDŁA DANYCH** |
| Data-source error — title | **Nie udało się odczytać zbioru** |
| Data-source error — body | **Zapytanie przekroczyło limit ⟨15 s⟩ po stronie bazy klienta. Uprawnienia i konfiguracja zbioru są poprawne — problem jest po stronie połączenia.** |
| Data-source error — actions | **Ponów** · **Zawęź filtrami** · **Skopiuj referencję** |
| Export threshold note | **⟨1 274⟩ wiersze mieszczą się w limicie 20 000. Powyżej — plik przygotowuje się w tle i trafia do Raportów jako Eksport danych (retencja 3 dni).** |
| Export column note | **Eksport zawsze obejmuje wszystkie ⟨42⟩ zatwierdzone kolumny niezależnie od widoku.** |
| All columns hidden | **Co najmniej jedna kolumna musi pozostać widoczna.** |

### 9.2 Report Explorer

| Situation | Copy |
|---|---|
| Empty after filters — title | **Brak raportów dla wybranych filtrów** |
| Empty after filters — body | **Ten klient ma ⟨26⟩ pozycji w bibliotece, ale żadna nie pasuje do filtra ⟨Rok = 2024⟩. Najstarsza dostępna pozycja pochodzi z ⟨maja 2025⟩.** |
| No access — badge | **BRAK DOSTĘPU** |
| No access — title | **Nie masz dostępu do raportów tego klienta** |
| No access — body | **Twoje konto ma dostęp do danych tego klienta w Przeglądarce danych, ale nie do jego folderu raportów. Dostęp nadaje administrator.** |
| No access — actions | **Wróć do ⟨Acme Logistics⟩** · **Poproś o dostęp** |
| File-store error — badge | **BŁĄD MAGAZYNU PLIKÓW** |
| File-store error — title | **Nie udało się odczytać biblioteki** |
| File-store error — body | **Lista pozycji jest dostępna, ale magazyn plików nie odpowiada. Podgląd i pobieranie są chwilowo niemożliwe. Twoje uprawnienia są poprawne.** |
| Retention footer | **retencja plików: 24 miesiące · starsze na żądanie** |
| Assignment note | **Widzisz raporty przypisane do Twojego konta w kontekście tego klienta. Raporty powstają automatycznie z predefiniowanych zapytań — nie tworzysz ich tutaj.** |

### 9.3 Eco Driving

| Situation | Copy |
|---|---|
| Non-contiguous weeks | **Wybrane tygodnie nie są ciągłe. Podstawa rankingu ma przerwę.** |
| Zero weeks | **Wybierz co najmniej jeden tydzień, aby zobaczyć ranking.** |
| Week breakdown note | **Ranking liczony jest na zsumowanych danych ⟨W1+W2⟩. Rozbicie na tygodnie służy diagnozie, nie jest osobnym rankingiem.** |
| Persisted vs live note | **Grupa rankingowa, pozycja i wynik są utrwalone. Nazwa kierowcy pochodzi z bieżącej karty kierowcy i nie jest niezmiennym zapisem historycznym — przypisane ID jest wartością nieprzejrzystą i to ono identyfikuje wpis.** |
| Driver absent after context switch | **⟨Kowalski Marek⟩ nie występuje w wybranym okresie ⟨Sierpień 2026 · W1⟩. Pokazujemy ranking dla nowego okresu.** |
| Trips read-only footer | **te same wiersze są dostępne w Przeglądarce danych — tylko do odczytu** |

### 9.4 Responsive advisory

| Element | Copy |
|---|---|
| Eyebrow | **Poniżej 768 px** |
| Title | **Przeglądarka danych wymaga szerszego ekranu** |
| Body | **Tabela z ⟨42⟩ kolumnami nie da się rzetelnie obsłużyć na tej szerokości. Nie zamieniamy jej na karty, bo porównywanie wierszy jest tu całym sensem pracy.** |
| Actions | **Przejdź do Raportów** · **Otwórz mimo to** |
| Footnote | **Raporty i Eco Driving działają na tej szerokości w pełni.** |

## 10. Tone rules

1. **Name the cause, not the symptom.** "Filtr `Depot = KRK-02` zawęża wynik do zera", never "Brak danych".
2. **Say what the user's next move is**, and put it in the button label: `Usuń Depot = KRK-02`, not `OK`.
3. **Separate "you lack permission" from "the system failed."** Never let one read as the other.
4. **State numbers.** Counts, thresholds and periods are always concrete, never "wiele" or "kilka".
5. **No exclamation marks, no apologies, no personality.** This is an operational tool.
6. **No emoji anywhere.**
7. **Second person singular** (`Twoje konto`, `zobaczysz`), consistent with the existing product voice.
8. Technical identifiers stay in English and in mono; never translate a column or table name.

## 11. Unresolved terminology

None. Two items were resolved during the handoff and are recorded here so they are not reopened:

- **`Przeglądarka danych` vs `Dane`** — `Dane` is the nav label; `Przeglądarka danych` is the prose name used when referring to the module from another module (`Otwórz w Przeglądarce danych`). Both are correct in their place.
- **`Driver tag`** — kept in English because it is the physical identifier printed on the hardware tag; translating it would break the match with the object drivers hold.
