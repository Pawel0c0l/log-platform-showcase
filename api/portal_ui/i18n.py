"""Lightweight UI translation keys for the Log Platform portal.

Decision `D-010` of the approved design requires every user-facing string to go
through a translation key even though only the Polish locale ships. This module
is the whole mechanism: a frozen catalogue plus ``t()``. It is deliberately not
a localisation framework — no plural rules, no runtime catalogue loading, no
extraction tooling — because the product ships exactly one locale and adding
machinery for a second one would be speculative.

Scope of this slice: the shared shell. Module surfaces migrate their own
terminology in their own redesign stages; the rule is only that *new* shared-UI
strings arrive as keys rather than as literals.

Naming convention: ``<module>.<surface>.<element>``.
"""
from __future__ import annotations

from typing import Mapping

DEFAULT_LOCALE = "pl"

# Canonical Polish vocabulary. Values are verbatim from the approved
# COPY_AND_TERMINOLOGY.md where that document names the concept; alternatives
# must not be invented.
_CATALOG: dict[str, dict[str, str]] = {
    "pl": {
        # -- brand ---------------------------------------------------------
        "shell.brand.name": "Log Platform",
        "shell.brand.home_label": "Log Platform — strona główna",
        # -- primary navigation --------------------------------------------
        "shell.nav.aria_label": "Nawigacja główna",
        "shell.nav.reports": "Raporty",
        "shell.nav.data": "Dane",
        "shell.nav.analytics": "Analizy",
        "shell.nav.artifacts": "Artefakty",
        "shell.nav.administration": "Administracja",
        "shell.nav.group_work": "Praca",
        "shell.nav.group_tools": "Narzędzia",
        "shell.nav.open": "Otwórz nawigację",
        "shell.nav.close": "Zamknij nawigację",
        "shell.nav.drawer_title": "Nawigacja",
        # -- section (secondary) navigation ---------------------------------
        "shell.subnav.aria_label": "Nawigacja sekcji",
        "shell.subnav.admin.overview": "Przegląd",
        "shell.subnav.admin.users": "Użytkownicy",
        "shell.subnav.admin.groups": "Grupy",
        "shell.subnav.admin.client_access": "Dostęp klientów",
        "shell.subnav.admin.database_access": "Zbiory danych",
        "shell.subnav.admin.eco_driving_permissions": "Uprawnienia Eco Driving",
        "shell.subnav.admin.report_folders": "Foldery raportów",
        "shell.subnav.admin.audit": "Audyt",
        "shell.subnav.artifacts.all": "Wszystkie artefakty",
        "shell.subnav.artifacts.folders": "Foldery wirtualne",
        "shell.subnav.data.datasets": "Zbiory danych",
        "shell.subnav.data.exports": "Moje eksporty",
        # -- theme switcher --------------------------------------------------
        "shell.theme.group": "Motyw",
        "shell.theme.auto": "AUTO",
        "shell.theme.auto_name": "Motyw automatyczny — zgodny z systemem",
        "shell.theme.light_name": "Motyw jasny",
        "shell.theme.dark_name": "Motyw ciemny",
        # Approved stage S13: the theme override is an ACCOUNT preference, so a
        # write that did not reach the server must say so rather than let the
        # UI imply the choice will follow the account to another device.
        "shell.theme.status_aria": "Stan zapisu motywu",
        "shell.theme.save_failed": "Nie uda\u0142o si\u0119 zapisa\u0107 motywu na koncie. Zmiana dzia\u0142a tylko w tej przegl\u0105darce.",
        # -- account ---------------------------------------------------------
        "shell.account.aria_label": "Konto",
        "shell.account.logout": "Wyloguj",
        # -- context bar -----------------------------------------------------
        "shell.context.client": "Klient",
        "shell.context.aria_label": "Kontekst klienta",
        # A surface with no client in scope (artifact triage, administration)
        # must not label its context region "client context": the shell may not
        # claim a business scope the page does not have.
        "shell.context.aria_label_module": "Kontekst modułu",
        "shell.context.operator_mode": "TRYB OPERATORA",
        # -- working area ----------------------------------------------------
        "shell.main.skip_link": "Przejdź do treści",
        "shell.main.aria_label": "Obszar roboczy",
        # -- Database Explorer data sheet (DB-003 / DB-004) -------------------
        # Verbatim from the approved COPY_AND_TERMINOLOGY.md; alternatives must
        # not be invented.
        "db.readonly": "TYLKO ODCZYT",
        "db.filters": "Filtry",
        "db.columns": "Kolumny",
        "db.density.group": "Gęstość wierszy",
        "db.density.compact": "Zwarta",
        "db.density.comfortable": "Wygodna",
        "db.search.placeholder": "Szukaj w {count} kolumnach tekstowych",
        "db.search.submit": "Szukaj",
        "db.rows_per_page": "wierszy na stronie",
        "db.counter.rows": "wierszy",
        "db.counter.of": "z",
        # Shown in place of the dataset total when the secondary count fails.
        # Substituting the filtered count there would claim the filters matched
        # every row, which is false; an explicit unknown is the honest degradation.
        "db.counter.unknown": "?",
        "db.counter.total_unavailable": "Nie udało się ustalić liczby wszystkich wierszy w zbiorze.",
        "db.sort.summary": "sortowanie",
        "db.clear_all": "Wyczyść wszystkie",
        "db.apply": "Zastosuj",
        "db.approved_columns": "zatwierdzone kolumny",
        "db.page.previous": "Poprzednia strona",
        "db.page.next": "Następna strona",
        "db.page.indicator": "Strona {page} z {pages}",
        "db.table.aria_label": "{dataset} — {client}",
        "db.toolbar.aria_label": "Narzędzia tabeli",
        "db.footer.aria_label": "Stronicowanie",
        # -- value markers -----------------------------------------------------
        "db.value.null": "brak wartości",
        "db.value.blank": "pusty tekst",
        "db.value.true": "TAK",
        "db.value.false": "NIE",
        # -- states ------------------------------------------------------------
        # Approved stage S9. `DB-010`, `DB-011` and the catalogue/permission
        # states. Verbatim from COPY_AND_TERMINOLOGY.md §9.1 where that document
        # names the string; the situations it does not name (dataset-empty,
        # ambiguous culprit, permission-denied, malformed view) follow its tone
        # rules — name the cause, state the number, keep permission separate
        # from failure — and invent nothing else.
        "db.empty.filtered.title": "Brak wierszy dla aktywnych filtrów",
        # The approved single-culprit sentence. `{filter}` is the chip text the
        # toolbar already renders, so the empty state and the chip name the same
        # constraint in the same words.
        "db.empty.filtered.body": "Zbiór ma {total} wierszy. Filtr {filter} zawęża wynik do zera — bez niego zobaczysz {restored} wierszy.",
        "db.empty.filtered.body_search": "Zbiór ma {total} wierszy. Wyszukiwanie {filter} zawęża wynik do zera — bez niego zobaczysz {restored} wierszy.",
        # More than one filter independently restores rows: naming one of them
        # as THE cause would be false, so the state states what the evidence
        # actually supports.
        "db.empty.filtered.body_multiple": "Zbiór ma {total} wierszy. Wynik zerują niezależnie różne filtry — usunięcie dowolnego z nich przywraca wiersze.",
        # No single removal restores rows: the constraint is the combination.
        "db.empty.filtered.body_joint": "Zbiór ma {total} wierszy. Żaden pojedynczy filtr nie odpowiada za pusty wynik — dopiero ich połączenie zawęża go do zera.",
        # Culprit analysis was not run (too many active constraints, or the
        # diagnostic count itself failed). Claiming anything about individual
        # filters here would be a guess.
        "db.empty.filtered.body_generic": "Zbiór ma {total} wierszy. Aktywne filtry zawężają wynik do zera.",
        "db.empty.filtered.body_unknown": "Aktywne filtry zawężają wynik do zera.",
        "db.empty.filtered.remove": "Usuń {filter}",
        "db.empty.filtered.remove_search": "Usuń wyszukiwanie",
        "db.empty.search_label": "Wyszukiwanie",
        "db.empty.dataset.title": "Ten zbiór nie ma wierszy",
        # `DB-58`: a genuinely empty dataset must not read like a filtered
        # result, so it states the absence of filtering explicitly.
        "db.empty.dataset.body": "Zbiór danych nie zawiera żadnych wierszy. Żaden filtr nie zawęża tego wyniku.",
        "db.empty.back": "Wróć do zbiorów danych",
        # -- data-source error (DB-011) -----------------------------------------
        "db.error.badge": "BŁĄD ŹRÓDŁA DANYCH",
        "db.error.title": "Nie udało się odczytać zbioru",
        "db.error.body.timeout": "Zapytanie przekroczyło limit {timeout} po stronie bazy klienta. Uprawnienia i konfiguracja zbioru są poprawne — problem jest po stronie połączenia.",
        "db.error.body.unavailable": "Baza klienta nie odpowiada. Uprawnienia i konfiguracja zbioru są poprawne — problem jest po stronie połączenia.",
        "db.error.body.source": "Zapytanie do bazy klienta nie powiodło się. Uprawnienia i konfiguracja zbioru są poprawne — problem jest po stronie źródła danych.",
        "db.error.retry": "Ponów",
        "db.error.narrow": "Zawęź filtrami",
        "db.error.copy_reference": "Skopiuj referencję",
        "db.error.diagnostics_aria": "Dane diagnostyczne",
        "db.error.reference_label": "ref",
        "db.error.code_label": "kod",
        # -- malformed view state ------------------------------------------------
        # Kept semantically separate from both the permission state and the
        # source error: the account may open this dataset and the source is
        # fine — the requested view is not a view this dataset has.
        "db.view.badge": "NIEPRAWIDŁOWY WIDOK",
        "db.view.title": "Nie można otworzyć tego widoku",
        "db.view.body": "Adres tego widoku zawiera ustawienia filtrów, sortowania lub stronicowania, których ten zbiór nie obsługuje. Sam zbiór jest dostępny.",
        "db.view.reset": "Otwórz widok domyślny",
        # A dataset whose catalog has no approved column is not a bad view and not
        # a failure — there is genuinely nothing approved to show yet.
        "db.view.columns_title": "Ten zbiór nie ma zatwierdzonych kolumn",
        "db.view.columns_body": "Administrator nie zatwierdził jeszcze żadnej kolumny tego zbioru, więc nie ma czego pokazać.",
        # -- access-disabled state (DB-61, PBC 2.17) -----------------------------
        # Deliberately says nothing about the requested dataset: an unauthorized
        # id and an unknown id must stay indistinguishable.
        "db.access.badge": "BRAK DOSTĘPU",
        "db.access.title": "Nie masz dostępu do tego zbioru danych",
        "db.access.body": "Twoje konto nie ma dostępu do tego zbioru danych. Dostęp nadaje administrator.",
        # -- loading / pending (DB-59) -------------------------------------------
        "db.loading.aria": "Wczytywanie wierszy",
        "db.loading.counter": "…",
        # -- dataset catalogue (DB-001 / DB-002) ---------------------------------
        "db.catalog.title": "Zbiory danych klientów",
        "db.catalog.rail_heading": "Zbiory danych",
        "db.catalog.rail_aria": "Zbiory danych klientów",
        "db.catalog.table_aria": "Porównanie zbiorów danych",
        "db.catalog.col.dataset": "Zbiór",
        "db.catalog.col.client": "Klient",
        "db.catalog.col.rows": "Wiersze",
        "db.catalog.col.columns": "Kolumny",
        "db.catalog.col.permissions": "Uprawnienia",
        "db.catalog.col.saved_views": "Zapisane widoki",
        "db.catalog.open": "Otwórz arkusz",
        "db.catalog.open_aria": "Otwórz arkusz: {dataset}",
        "db.catalog.exports": "Moje eksporty",
        "db.catalog.back": "Zbiory danych",
        "db.catalog.empty.title": "Nie masz przypisanych zbiorów danych",
        "db.catalog.empty.body": "Twoje konto nie ma obecnie dostępu do żadnego zbioru danych. Dostęp nadaje administrator.",
        # `PBC` 2.2 puts the access-rules prose in the catalogue — this is where
        # it belongs, and it is exactly why the row sheet carries none.
        "db.catalog.rules_heading": "Zasady dostępu",
        "db.catalog.rules_body": "Widzisz wyłącznie zbiory danych przypisane do Twojego konta. Przeglądanie wierszy i eksport obejmują tylko zatwierdzone kolumny i uprawnienia nadane Twojemu kontu. Dostęp nadaje administrator.",
        # Permission badges are configuration facts, never errors (PBC 2.1).
        "db.perm.filter": "Filtrowanie",
        "db.perm.no_filter": "Bez filtrów",
        "db.perm.export": "Eksport",
        "db.perm.view_only": "Tylko podgląd",
        # `DB-54` on the row sheet: the export action is absent, and this states
        # why in the approved permission vocabulary rather than in English.
        "db.export.absent": "Ten zbiór jest dla Twojego konta w trybie „Tylko podgląd”. Eksport nadaje administrator.",
        # -- column menu (DB-005) ---------------------------------------------
        "db.menu.aria": "{column}, menu kolumny",
        "db.menu.aria_filtered": "{column}, filtr aktywny: {filter}, menu kolumny",
        "db.menu.aria_sorted_asc": "{column}, sortowanie rosnąco, menu kolumny",
        "db.menu.aria_sorted_desc": "{column}, sortowanie malejąco, menu kolumny",
        "db.menu.sort_heading": "Sortowanie",
        "db.menu.sort_asc_text": "Sortuj A \u2192 Z",
        "db.menu.sort_desc_text": "Sortuj Z \u2192 A",
        "db.menu.sort_asc": "Sortuj rosnąco",
        "db.menu.sort_desc": "Sortuj malejąco",
        "db.menu.nulls_last": "Wartości puste zawsze na końcu.",
        "db.menu.filter_heading": "Filtr",
        "db.menu.operator_label": "Operator dla {column}",
        "db.menu.value_label": "Wartość dla {column}",
        "db.menu.value_from": "Od",
        "db.menu.value_to": "Do",
        "db.menu.values_label": "Wartości dla {column} — po jednej w wierszu",
        "db.menu.keyboard_hint": "\u21b5 zastosuj \u00b7 esc",
        "db.menu.not_filterable": "Ta kolumna nie jest filtrowalna.",
        "db.menu.blank_text_note": "Dopasowuje brak wartości i pusty tekst.",
        "db.menu.blank_note": "Dopasowuje brak wartości.",
        # -- typed dates and the range calendar (UI-20260831-01) --------------
        # The native date control already accepts typing, but only in the
        # browser's own locale shape and with no range affordance at all. These
        # name the added text fields and the two-click calendar that writes into
        # the same two inputs.
        # UI-20260831-02: one field per endpoint, written the way the user
        # writes a date. The separate "wpisz datę" pair is gone — the visible
        # field IS the entry field, and the native picker's own control is the
        # hidden carrier of the wire value behind it.
        "db.date.format_hint": "dd.mm.rrrr",
        "db.date.format_hint_time": "dd.mm.rrrr gg:mm",
        "db.date.single_label": "Data dla {column}",
        "db.date.open_calendar": "Pokaż kalendarz",
        "db.date.close_calendar": "Ukryj kalendarz",
        "db.date.invalid": "Nieprawidłowa data — nie zastosowano.",
        "db.date.calendar_label": "Kalendarz zakresu dat",
        "db.date.prev_month": "Poprzedni miesiąc",
        "db.date.next_month": "Następny miesiąc",
        "db.date.pick_start": "Wybierz datę początkową.",
        "db.date.pick_end": "Wybierz datę końcową.",
        "db.date.range_selected": "Zakres: {from} — {to}",
        "db.date.in_range": "w zakresie",
        "db.date.range_start": "początek zakresu",
        "db.date.range_end": "koniec zakresu",
        "db.date.clear_range": "Wyczyść zakres",
        "db.date.months": "styczeń,luty,marzec,kwiecień,maj,czerwiec,lipiec,sierpień,wrzesień,październik,listopad,grudzień",
        "db.date.weekdays": "pon,wt,śr,czw,pt,sob,niedz",
        # -- value distributions (DB-005 section 4, stage S4) -------------------
        "db.dist.values_heading": "Wartości w kolumnie",
        "db.dist.distribution_heading": "Rozkład wartości",
        "db.dist.distinct": "{count} unikalnych",
        "db.dist.non_null": "{count} niepustych",
        "db.dist.loading": "Wczytywanie…",
        "db.dist.error": "Nie udało się wczytać rozkładu wartości. Filtrowanie ręczne działa bez zmian.",
        "db.dist.empty": "Brak wierszy w bieżącym wyniku.",
        "db.dist.all_null": "Wszystkie wartości w bieżącym wyniku są puste.",
        "db.dist.single_value": "Wszystkie wartości w bieżącym wyniku są takie same: {value}.",
        "db.dist.truncated": "Pokazano {shown} z {total} wartości — lista jest niepełna.",
        # The counts describe the result WITHOUT this column's own filter, so the
        # scope is stated in words; otherwise the numbers would look like they
        # describe the visible rows and quietly contradict the row counter.
        "db.dist.scope": "Liczby uwzględniają pozostałe filtry, ale nie filtr tej kolumny.",
        "db.dist.range": "min {min} · max {max}",
        "db.dist.select_value": "Zaznacz wartość {value} ({count})",
        "db.dist.select_blank": "Filtruj puste ({count})",
        "db.dist.bucket": "od {min} do {max}: {count}",
        "db.dist.search": "Szukaj wśród wartości",
        "db.dist.selected": "Zaznaczono {count}",
        "db.dist.off_list": "Zaznaczono też {count} spoza tej listy.",
        "db.dist.limit_reached": "Można wybrać najwyżej {limit} wartości.",
        "db.dist.search_hint": "Szuka tylko wśród pobranych wartości.",
        "db.dist.no_matches": "Brak pasujących wartości na tej liście.",
        # -- filter panel -------------------------------------------------------
        "db.panel.aria": "Filtry",
        "db.panel.active": "Aktywne \u2014 {count}",
        "db.panel.none_active": "Brak aktywnych filtrów.",
        "db.panel.add": "Dodaj filtr",
        "db.panel.find_column": "Znajdź kolumnę",
        "db.panel.presets": "Zakresy dat",
        "db.panel.close": "Zwiń filtry",
        "db.chip.remove": "Usuń filtr: {filter}",
        # -- RSP-003 below-minimum-width advisory (COPY_AND_TERMINOLOGY 9) ------
        # Verbatim from the approved handoff. The column count is the dataset's
        # own approved-column total, which is what the placeholder stands for.
        "db.narrow.eyebrow": "Poniżej 768 px",
        "db.narrow.title": "Przeglądarka danych wymaga szerszego ekranu",
        "db.narrow.body": (
            "Tabela z {count} kolumnami nie da się rzetelnie obsłużyć na tej "
            "szerokości. Nie zamieniamy jej na karty, bo porównywanie wierszy "
            "jest tu całym sensem pracy."
        ),
        "db.narrow.to_reports": "Przejdź do Raportów",
        "db.narrow.open_anyway": "Otwórz mimo to",
        "db.narrow.footnote": "Raporty i Eco Driving działają na tej szerokości w pełni.",
        "db.clear_one": "Wyczyść",
        # -- operator vocabulary (COPY_AND_TERMINOLOGY 3.1) ---------------------
        "db.op.contains": "zawiera",
        "db.op.eq": "=",
        "db.op.neq": "\u2260",
        "db.op.gt": ">",
        "db.op.gte": "\u2265",
        "db.op.lt": "<",
        "db.op.lte": "\u2264",
        "db.op.between": "od\u2013do",
        "db.op.blank": "puste",
        "db.op.in": "in",
        "db.op.older": "przed",
        "db.op.newer": "po",
        "db.op.range": "między",
        "db.op.is_true": "tak",
        "db.op.is_false": "nie",
        "db.op.all": "wszystko",
        "db.op.search": "szukaj",
        # -- demoted secondary controls ---------------------------------------
        # Column management (DB-008, DB-26..DB-29)
        "db.col.actions_heading": "Kolumna",
        "db.col.pin": "Przypnij kolumnę po lewej",
        "db.col.unpin": "Odepnij kolumnę",
        "db.col.hide": "Ukryj kolumnę",
        "db.col.autofit": "Dopasuj szerokość do treści",
        "db.col.width_label": "Szerokość kolumny {column} w pikselach",
        "db.col.width_apply": "Ustaw szerokość",
        "db.col.resize_aria": "Szerokość kolumny {column}: {width} pikseli. Przeciągnij lub użyj strzałek.",
        "db.col.pin_refused": "Przypięte kolumny nie mogą zajmować więcej niż 40% szerokości tabeli.",
        "db.cols.aria": "Kolumny",
        "db.cols.help": "Wybierz widoczne kolumny, ustaw ich kolejność i przypnij te, które mają zostać na widoku.",
        "db.cols.search": "Znajdź kolumnę",
        "db.cols.tabs_aria": "Filtr listy kolumn",
        "db.cols.tab_all": "Wszystkie",
        "db.cols.tab_visible": "Widoczne",
        "db.cols.tab_hidden": "Ukryte",
        "db.cols.reorder": "Zmień kolejność kolumny {column}",
        "db.cols.move_up": "Przenieś kolumnę {column} w górę",
        "db.cols.move_down": "Przenieś kolumnę {column} w dół",
        "db.cols.moved": "Przeniesiono kolumnę {column} na pozycję {position} z {total}.",
        "db.cols.pin_short": "Przypnij",
        "db.cols.min_one": "Co najmniej jedna kolumna musi pozostać widoczna.",
        "db.cols.no_matches": "Brak pasujących kolumn.",
        "db.cols.reset": "Domyślne kolumny",

        # Saved views and named column sets (approved stage S13, `D-007`,
        # `DB-30`). The concept nouns and every control label that
        # COPY_AND_TERMINOLOGY.md §2/§3 names are verbatim; the field labels,
        # the empty states and the refusal messages are not defined there and
        # are ordinary Polish derived from those nouns.
        "db.saved.views": "Zapisane widoki",
        "db.saved.view": "Zapisany widok",
        "db.saved.save_as_view": "Zapisz jako widok",
        "db.saved.save_changes": "Zapisz zmiany",
        "db.saved.view_name": "Nazwa widoku",
        "db.saved.views_none": "Brak zapisanych widoków.",
        "db.saved.views_aria": "Zapisane widoki tego zbioru",
        "db.saved.open_view_aria": "Otwórz zapisany widok {name}",
        "db.saved.delete_view_aria": "Usuń zapisany widok {name}",
        "db.saved.active_view": "Aktywny widok: {name}",
        "db.saved.modified": "zmieniony",
        "db.saved.delete": "Usuń",
        "db.saved.sets": "Zestawy",
        "db.saved.set": "Zestaw kolumn",
        "db.saved.save_as_set": "Zapisz jako zestaw",
        "db.saved.set_name": "Nazwa zestawu",
        "db.saved.sets_none": "Brak zapisanych zestawów kolumn.",
        "db.saved.sets_aria": "Zestawy kolumn tego zbioru",
        "db.saved.apply_set_aria": "Zastosuj zestaw kolumn {name}",
        "db.saved.delete_set_aria": "Usuń zestaw kolumn {name}",
        "db.saved.name_required": "Podaj nazwę.",
        "db.saved.name_too_long": "Nazwa może mieć najwyżej 80 znaków.",
        "db.saved.name_invalid": "Nazwa nie może zawierać znaków sterujących.",
        "db.saved.state_invalid": "Nie można zapisać tego widoku.",
        "db.saved.state_too_large": "Ten widok jest zbyt złożony, aby go zapisać.",
        "db.saved.duplicate": "Masz już zapisany element o tej nazwie w tym zbiorze.",
        "db.saved.limit": "Osiągnięto limit zapisanych elementów dla tego zbioru.",
        "db.saved.failed": "Nie udało się zapisać. Spróbuj ponownie.",
        "db.saved.not_found": "Ten zapisany element nie jest już dostępny.",
        "db.saved.stale": "Ten zapisany widok nie odpowiada już bieżącej konfiguracji zbioru.",
        "db.saved.stale_column": "Ten zapisany widok odwołuje się do kolumny, która nie jest już dostępna.",
        "db.saved.filtering_revoked": "Ten zapisany widok korzysta z filtrów, a filtrowanie tego zbioru nie jest już dla Ciebie dostępne.",
        "db.saved.view_created": "Zapisano widok.",
        "db.saved.view_updated": "Zaktualizowano zapisany widok.",
        "db.saved.view_deleted": "Usunięto zapisany widok.",
        "db.saved.set_created": "Zapisano zestaw kolumn.",
        "db.saved.set_updated": "Zaktualizowano zestaw kolumn.",
        "db.saved.set_deleted": "Usunięto zestaw kolumn.",
        "db.saved.set_narrowed": "Część kolumn z tego zestawu nie jest już dostępna i została pominięta.",
        "db.saved.unavailable": "Zapisane widoki i zestawy kolumn będą dostępne po zastosowaniu migracji bazy danych.",

        # Row detail (DB-006, DB-36..DB-39) — approved stage S6
        "db.row.detail_column": "Szczegóły",
        "db.row.open": "Szczegóły",
        "db.row.detail_heading": "Szczegóły wiersza",
        "db.row.close": "Zamknij panel szczegółów",
        "db.row.previous": "Poprzedni wiersz",
        "db.row.next": "Następny wiersz",
        "db.row.fields_scope": "Zakres pól",
        "db.row.fields_visible": "Widoczne",
        "db.row.fields_all": "Wszystkie",
        "db.row.unavailable": "Ten wiersz jest niedostępny. Mógł zostać usunięty lub odnośnik stracił ważność.",
        "db.row.section.identity": "Tożsamość",
        "db.row.section.time": "Czas i trasa",
        "db.row.section.metrics": "Metryki",
        "db.row.section.classification": "Klasyfikacja i pochodzenie",

        "db.section.export": "Eksport",

        # Export panel (DB-009) and background export states (DB-007) —
        # approved stage S8. Verbatim from COPY_AND_TERMINOLOGY.md §5.
        "db.export.title": "Eksport",
        "db.export.scope_heading": "Zakres",
        "db.export.scope.view": "Bie\u017c\u0105cy widok",
        "db.export.scope.dataset": "Ca\u0142y zbi\u00f3r danych",
        "db.export.scope.selection": "Zaznaczone wiersze",
        "db.export.columns_heading": "Kolumny",
        "db.export.columns.screen": "Jak na ekranie",
        "db.export.columns.approved": "Wszystkie zatwierdzone",
        "db.export.format_heading": "Format",
        "db.export.format.xlsx": "XLSX",
        "db.export.format.csv": "CSV",
        "db.export.submit_direct": "Pobierz {format}",
        "db.export.submit_background": "Przygotuj w tle",
        "db.export.cancel": "Anuluj",
        "db.export.rows": "wierszy",
        "db.export.count_unknown": "?",
        "db.export.count_unavailable": "Nie uda\u0142o si\u0119 ustali\u0107 liczby wierszy dla tego zakresu.",
        # The path notice, stated before the user commits (DB-48).
        "db.export.path.direct": "Pobranie natychmiastowe.",
        "db.export.path.background": "Plik przygotuje si\u0119 w tle i trafi do Raport\u00f3w jako Eksport danych (retencja {days} dni).",
        "db.export.path.threshold": "Do {limit} wierszy plik pobiera si\u0119 od razu. Powy\u017cej \u2014 przygotowuje si\u0119 w tle.",
        "db.export.path.over_ceiling": "Ten zakres ma wi\u0119cej ni\u017c {ceiling} wierszy. Zaw\u0119\u017c filtrami \u2014 eksportu nie da si\u0119 wykona\u0107.",
        "db.export.path.unknown": "Liczba wierszy jest nieznana. \u015acie\u017ck\u0119 ustali serwer po zatwierdzeniu.",
        "db.export.selection.empty": "Zaznacz zakres kom\u00f3rek w tabeli, aby wybra\u0107 wiersze.",
        "db.export.selection.unavailable": "Ten zbi\u00f3r nie ma skonfigurowanej to\u017csamo\u015bci wiersza, wi\u0119c nie da si\u0119 wyeksportowa\u0107 zaznaczonych wierszy.",
        "db.export.selection.too_many": "Mo\u017cna wyeksportowa\u0107 najwy\u017cej {limit} zaznaczonych wierszy.",
        "db.export.selection.invalid": "Zaznaczenie jest nieaktualne. Zaznacz wiersze ponownie.",
        # -- background exports (DB-007) --------------------------------------
        "db.exports.title": "Eksporty danych",
        "db.exports.link": "Moje eksporty danych",
        "db.exports.recent": "Ostatnie eksporty",
        "db.exports.state.running": "W toku",
        "db.exports.state.ready": "Gotowy",
        "db.exports.state.expired": "Pliki wygas\u0142y",
        "db.exports.state.failed": "B\u0142\u0105d",
        "db.exports.action.download": "Pobierz",
        "db.exports.action.cancel": "Anuluj",
        "db.exports.action.requeue": "Zle\u0107 ponownie",
        "db.exports.action.copy_reference": "Kopiuj ref",
        "db.exports.available_until": "Dost\u0119pny do",
        "db.exports.requested": "zlecono {date}",
        "db.exports.expired_note": "plik usuni\u0119ty po {days} dniach",
        "db.exports.progress_aria": "Post\u0119p przygotowania eksportu",
        "db.exports.prepared_rows": "przygotowano {rows}",
        "db.exports.reference": "Referencja",
        "db.exports.reference_copied": "Skopiowano referencj\u0119 do schowka.",
        "db.exports.reference_copy_failed": "Nie uda\u0142o si\u0119 skopiowa\u0107 referencji do schowka.",
        "db.exports.empty.title": "Nie zlecono jeszcze \u017cadnego eksportu w tle",
        "db.exports.empty.body": "Eksporty do {limit} wierszy pobieraj\u0105 si\u0119 od razu. Wi\u0119ksze przygotowuj\u0105 si\u0119 w tle i pojawiaj\u0105 si\u0119 tutaj.",
        "db.exports.queued_notice": "Eksport przygotowywany w tle.",
        "db.exports.cancelled_notice": "Eksport anulowany.",
        "db.exports.cancel_refused": "Tego eksportu nie da si\u0119 ju\u017c anulowa\u0107.",
        "db.exports.requeued_notice": "Eksport zlecony ponownie.",
        "db.exports.requeue_refused": "Tego eksportu nie da si\u0119 zleci\u0107 ponownie.",
        "db.exports.status_aria": "Stan eksport\u00f3w",
        # -- app-bar indicator (DB-049) ----------------------------------------
        "shell.export.indicator": "{count} eksport w toku",
        "shell.export.indicator_aria": "Eksporty w toku",

        # Grid selection and clipboard (DB-44, AC-2, D-012) — approved stage S7
        "db.select.hint": "zaznacz zakres i \u2318C, aby skopiowa\u0107 do arkusza",
        "db.select.status_aria": "Zaznaczenie kom\u00f3rek",
        # `3 wiersze \u00d7 4 kolumny \u00b7 12 kom\u00f3rek` — the row count the approved
        # footer states, plus the column and cell dimensions the rectangle has.
        "db.select.summary": "zaznaczono {rows} \u00d7 {columns} \u00b7 {cells}",
        "db.select.copied": "Skopiowano {cells} do schowka.",
        "db.select.copy_failed": "Nie uda\u0142o si\u0119 skopiowa\u0107 zaznaczenia do schowka.",
        "db.select.row.one": "wiersz",
        "db.select.row.few": "wiersze",
        "db.select.row.many": "wierszy",
        "db.select.column.one": "kolumna",
        "db.select.column.few": "kolumny",
        "db.select.column.many": "kolumn",
        "db.select.cell.one": "kom\u00f3rka",
        "db.select.cell.few": "kom\u00f3rki",
        "db.select.cell.many": "kom\u00f3rek",
        # -- Artifact Explorer (`ART-001`) -------------------------------------
        # Verbatim from COPY_AND_TERMINOLOGY.md \u00a78. `ART-001` is a technical
        # operator surface, not a fourth client-data access mode, and the copy
        # here is what says so on the screen (`AR-2`).
        "art.page.title": "Artefakty",
        "art.page.subtitle": "inspekcja artefakt\u00f3w systemu \u00b7 nie jest trybem dost\u0119pu do danych klienta",
        "art.rail.heading": "Rodzaje artefakt\u00f3w",
        "art.rail.aria": "Rodzaje artefakt\u00f3w",
        # The rail states the operator-permission requirement and the
        # immutability rule (PRODUCT_BEHAVIOR_CONTRACT \u00a75, `AR-5`).
        "art.rail.note": "Widok wymaga uprawnienia operatora. Artefakty s\u0105 niezmienne i tylko do odczytu.",
        "art.search.placeholder": "Nazwa, hash lub referencja",
        "art.action.refresh": "Od\u015bwie\u017c katalog",
        "art.action.inspect": "Inspekcja",
        "art.export.caption": "Pobierz wszystkie wiersze bie\u017c\u0105cej filtracji ({total})",
        "art.export.xlsx": "Pobierz XLSX",
        "art.export.csv": "Pobierz CSV",
        "art.export.too_many.title": "Zbyt wiele wierszy",
        "art.export.too_many.body": "Bie\u017c\u0105ca filtracja obejmuje {total} wierszy, a limit eksportu to {limit}. Zaw\u0119\u017a filtry i spr\u00f3buj ponownie.",
        "art.col.created": "Utworzono",
        "art.col.kind": "Rodzaj",
        "art.col.name": "Nazwa",
        "art.col.client": "Klient",
        "art.col.hash": "Hash",
        "art.col.size": "Rozmiar",
        "art.col.state": "Stan",
        # Only the two states the persisted artifact record can actually carry.
        # The approved vocabulary also names `W TRAKCIE`, but no current artifact
        # field represents an in-progress artifact; inventing one would be an
        # artifact lifecycle state, which S11 must not add.
        "art.state.verified": "ZWERYFIKOWANY",
        "art.state.removed": "USUNI\u0118TY",
        # -- Eco Driving (ECO-001 / ECO-002 / ECO-003) ------------------------
        # Verbatim from COPY_AND_TERMINOLOGY.md §7 wherever that document names
        # the concept. Where the approved vocabulary describes a capability this
        # stage deliberately does not ship (the month + multi-week basis widget),
        # no key is invented here: a key that renders a promise the product does
        # not keep is worse than an absent one.
        "eco.module": "Eco Driving",
        "eco.ranking.title": "Ranking",
        "eco.ranking.family_badge": "RANKING STANDARDOWY",
        "eco.lineage": "Linia danych: zrekonstruowana ze stanu bie\u017c\u0105cego",
        "eco.period.label": "Okres",
        "eco.period.previous": "Poprzedni okres",
        "eco.period.next": "Nast\u0119pny okres",
        "eco.period.type_weekly": "Tygodniowy \u00b7 narastaj\u0105cy w miesi\u0105cu",
        "eco.period.type_monthly": "Miesi\u0119czny",
        "eco.period.partial": "cz\u0119\u015bciowy",
        "eco.period.full": "pe\u0142ny",
        "eco.period.end_exclusive": "Koniec okresu (wy\u0142\u0105czny)",
        "eco.basis.label": "Podstawa rankingu",
        "eco.basis.qualified": "{qualified} z {total} kierowc\u00f3w spe\u0142nia pr\u00f3g kwalifikacji",
        "eco.basis.cumulative": "okres narastaj\u0105cy od pocz\u0105tku miesi\u0105ca \u2014 nie sumuj kolejnych uj\u0119\u0107",
        "eco.distribution.title": "Rozk\u0142ad wynik\u00f3w \u2014 ca\u0142y ranking",
        "eco.distribution.title_group": "Rozk\u0142ad wynik\u00f3w \u2014 {group}",
        "eco.distribution.universe_note": (
            "rozk\u0142ad obejmuje t\u0119 sam\u0105 grup\u0119 i t\u0119 sam\u0105 podstaw\u0119 co ranking obok, "
            "z pomini\u0119ciem kierowc\u00f3w poni\u017cej progu kwalifikacji"
        ),
        "eco.distribution.median": "mediana",
        "eco.distribution.mean": "\u015brednia",
        "eco.distribution.empty": "Brak wynik\u00f3w do rozk\u0142adu w tym okresie.",
        "eco.distribution.summary": "{count} kierowc\u00f3w w rozk\u0142adzie",
        "eco.distribution.bin_aria": "wynik od {low} do {high}: {count} kierowc\u00f3w",
        "eco.unit.label": "Wykroczenia jako",
        "eco.unit.rate": "/ 100 km",
        "eco.unit.sum": "\u03a3 suma",
        "eco.unit.note": (
            "Prze\u0142\u0105cznik zmienia wy\u0142\u0105cznie spos\u00f3b wy\u015bwietlania. "
            "Ranking, wynik, kwalifikacja i ocena pozostaj\u0105 bez zmian, a kolor "
            "wykroczenia zawsze wynika ze wsp\u00f3\u0142czynnika / 100 km."
        ),
        "eco.unit.primary": "jednostka g\u0142\u00f3wna",
        "eco.unit.secondary": "warto\u015b\u0107 pomocnicza",
        "eco.unit.comp_note": (
            "Obie warto\u015bci pozostaj\u0105 widoczne. Prze\u0142\u0105cznik decyduje tylko o tym, "
            "kt\u00f3ra z nich jest g\u0142\u00f3wna; ocena i kolor nadal wynikaj\u0105 ze "
            "wsp\u00f3\u0142czynnika / 100 km."
        ),
        "eco.search.label": "Kierowca lub tag ID",
        "eco.search.submit": "Szukaj",
        "eco.search.chip": "Szukaj = {term}",
        "eco.search.clear": "Usu\u0144 wyszukiwanie",
        "eco.group.label": "Grupa",
        "eco.group.included": "w rankingu",
        "eco.group.excluded": "wykluczeni",
        "eco.group.unknown": "nieznany kierowca",
        "eco.group.aria": "Grupa rankingowa",
        "eco.group.clear": "Usu\u0144 filtr grupy",
        "eco.group.not_ranked": "poza rankingiem (poni\u017cej progu): {count}",
        # The INCLUDED tab is the client's ranking POPULATION, so it also
        # lists permitted drivers who did not reach the qualifying
        # distance. They have no position, and the tab says so rather than
        # letting an empty `Poz.` column be read as a defect.
        "eco.group.included_unranked": (
            "w tym {count} z zezwoleniem na ranking, ale poni\u017cej progu "
            "kwalifikacji \u2014 bez pozycji i bez wyniku"
        ),
        "eco.col.position": "Poz.",
        "eco.col.driver": "Kierowca",
        "eco.col.driver_tag": "Driver tag",
        "eco.col.score": "Wynik Eco",
        "eco.col.rating": "Ocena",
        "eco.col.distance": "Dystans (km)",
        "eco.col.trips": "Przejazdy",
        "eco.col.qualification": "Kwalifikacja",
        "eco.col.details": "Szczeg\u00f3\u0142y",
        "eco.qualification.met": "spe\u0142niona",
        "eco.qualification.not_met": "niespe\u0142niona",
        "eco.recalculated": "ranking przeliczony {timestamp}",
        "eco.metric.harsh_braking": "Ostre hamowania",
        "eco.metric.harsh_braking_short": "Ostre ham.",
        "eco.metric.harsh_acceleration": "Przyspieszenia",
        "eco.metric.harsh_acceleration_short": "Przysp.",
        "eco.metric.harsh_turning": "Ostre skr\u0119ty",
        "eco.metric.harsh_turning_short": "Skr\u0119ty",
        "eco.metric.idle": "D\u0142ugi czas postoju",
        "eco.metric.idle_short": "D\u0142ugi post\u00f3j",
        "eco.metric.overrev": "Wysokie obroty",
        "eco.metric.overrev_short": "Wys. obroty",
        "eco.metric.speeding_140": "Przekroczenie 140 km/h",
        "eco.metric.speeding_140_short": "> 140",
        "eco.metric.speeding_160": "Przekroczenie 160 km/h",
        "eco.metric.speeding_160_short": "> 160",
        "eco.metric.speeding_170": "Przekroczenie 170 km/h",
        "eco.metric.speeding_170_short": "> 170",
        "eco.severity.ok": "bez straty punkt\u00f3w",
        "eco.severity.warn": "strata punkt\u00f3w",
        "eco.severity.bad": "punkty ujemne",
        "eco.severity.none": "brak danych",
        # -- driver detail (ECO-003) -----------------------------------------
        "eco.detail.back": "\u2039 Wr\u00f3\u0107 do rankingu",
        "eco.detail.context_kept": "kontekst okresu zachowany",
        "eco.detail.score_section": "Wynik i pozycja",
        "eco.detail.score": "Wynik",
        "eco.detail.position": "Pozycja",
        "eco.detail.percentile": "g\u00f3rne {percent}% floty",
        "eco.detail.fleet_position": "Gdzie jest w rozk\u0142adzie \u2014 ca\u0142y ranking",
        "eco.detail.fleet_position_group": "Gdzie jest w rozk\u0142adzie \u2014 {group}",
        "eco.detail.trend": "Trend wyniku",
        "eco.detail.trend_window": "ostatnie {count} okres\u00f3w \u00b7 ta sama metodologia",
        "eco.detail.trend_change": "Zmiana vs poprzedni okres",
        "eco.detail.trend_best": "Najlepszy okres",
        "eco.detail.trend_worst": "Najs\u0142abszy",
        "eco.detail.trend_empty": "Brak wcze\u015bniejszych utrwalonych okres\u00f3w dla tego kierowcy.",
        "eco.detail.trend_missing": (
            "Pokazane s\u0105 wy\u0142\u0105cznie okresy, dla kt\u00f3rych istnieje utrwalony wiersz. "
            "Brakuj\u0105ce okresy pozostaj\u0105 puste i nie s\u0105 uzupe\u0142niane zerami."
        ),
        "eco.detail.identity": "To\u017csamo\u015b\u0107 wpisu i okres",
        "eco.detail.identity_note": "warto\u015bci utrwalone w rankingu",
        "eco.detail.persisted_note": (
            "Grupa rankingowa, pozycja i wynik s\u0105 utrwalone. Nazwa kierowcy pochodzi z "
            "bie\u017c\u0105cej karty kierowcy i nie jest niezmiennym zapisem historycznym \u2014 "
            "przypisane ID jest warto\u015bci\u0105 nieprzejrzyst\u0105 i to ono identyfikuje wpis."
        ),
        "eco.detail.composition": "Z czego sk\u0142ada si\u0119 wynik {score}",
        "eco.detail.composition_plain": "Z czego sk\u0142ada si\u0119 wynik",
        "eco.detail.composition_arithmetic": (
            "{base} pkt bazowo \u00b7 utracone {lost} pkt \u00b7 sortowanie po utraconych"
        ),
        "eco.detail.progression": "Przebieg narastaj\u0105cy w miesi\u0105cu",
        "eco.detail.progression_caption": (
            "kolejne uj\u0119cia narastaj\u0105ce od pocz\u0105tku miesi\u0105ca \u2014 nie s\u0105 to odr\u0119bne tygodnie"
        ),
        "eco.detail.progression_note": (
            "Ka\u017cde uj\u0119cie obejmuje ca\u0142y miesi\u0105c do danej granicy, wi\u0119c kolejne uj\u0119cia "
            "zawieraj\u0105 si\u0119 w sobie i nie wolno ich sumowa\u0107. To nie jest wk\u0142ad "
            "poszczeg\u00f3lnych tygodni: izolowane tygodnie wybiera si\u0119 w podstawie rankingu, "
            "a wynik jest wtedy przeliczany od nowa z przejazd\u00f3w \u017ar\u00f3d\u0142owych."
        ),
        "eco.detail.progression_empty": "Brak wcze\u015bniejszych uj\u0119\u0107 w tym miesi\u0105cu.",
        "eco.detail.trips": "Przejazdy w podstawie rankingu",
        "eco.detail.trips_open": "Otw\u00f3rz pe\u0142n\u0105 list\u0119 przejazd\u00f3w",
        "eco.detail.trips_denied_title": "Brak dost\u0119pu do dowod\u00f3w przejazdowych",
        "eco.detail.trips_denied": (
            "Masz dost\u0119p do podsumowania rankingu, ale nie do dowod\u00f3w na poziomie "
            "przejazd\u00f3w. Wymagane jest osobne uprawnienie do szczeg\u00f3\u0142\u00f3w przejazd\u00f3w."
        ),
        "eco.detail.trips_empty": "Brak przejazd\u00f3w w tej podstawie rankingu.",
        "eco.detail.trips_footer": (
            "Te same wiersze s\u0105 dost\u0119pne w Przegl\u0105darce danych \u2014 tylko do odczytu."
        ),
        "eco.comp.metric": "Metryka",
        "eco.comp.event_sum": "\u03a3 zdarze\u0144",
        "eco.comp.rate": "/ 100 km",
        "eco.comp.threshold": "Pr\u00f3g",
        "eco.comp.threshold_ladder": "drabinka",
        "eco.comp.threshold_step": "krok {step}/{total}",
        "eco.comp.threshold_note": (
            "Punktacja ka\u017cdej metryki jest wielostopniowa. Kolumna pokazuje stopie\u0144 "
            "drabinki, kt\u00f3ry obowi\u0105zuje przy bie\u017c\u0105cym wsp\u00f3\u0142czynniku, "
            "wraz z jego pozycj\u0105 w ca\u0142ej drabince \u2014 nie jest to jedyny pr\u00f3g metryki."
        ),
        "eco.comp.points": "Punkty",
        "eco.comp.max": "Maks",
        "eco.comp.lost": "Utracone",
        "eco.comp.share": "Udzia\u0142 w utraconych punktach",
        "eco.comp.no_loss": "Ten kierowca nie utraci\u0142 \u017cadnych punkt\u00f3w w tym okresie.",
        "eco.trip.start": "Start podr\u00f3\u017cy",
        "eco.trip.end": "Koniec podr\u00f3\u017cy",
        "eco.trip.distance": "Dystans",
        "eco.trip.duration": "Czas jazdy",
        "eco.trip.id": "ID przejazdu",
        "eco.trip.registration": "Nr rejestracyjny",
        "eco.trip.source": "\u0179r\u00f3d\u0142o przypisania",
        "eco.trip.sum_caption": "\u03a3",
        "eco.trip.sum_note": (
            "Na poziomie przejazdu warto\u015bci wykrocze\u0144 s\u0105 zawsze sumami \u03a3 dla tej trasy."
        ),
        # -- states -----------------------------------------------------------
        "eco.state.insufficient_title": "Za ma\u0142y dystans w okresie sprawozdawczym",
        "eco.state.insufficient": (
            "W ca\u0142ym wybranym okresie kierowca przejecha\u0142 {km} km, a pr\u00f3g kwalifikacji "
            "wynosi {threshold} km. Dla tego okresu nie prezentujemy wyniku ani metryk Eco. "
            "Pr\u00f3g dotyczy sumy dystansu w ca\u0142ym okresie, nie pojedynczych dni."
        ),
        "eco.state.no_distance_title": "Brak dystansu w okresie sprawozdawczym",
        "eco.state.no_distance": (
            "W wybranym okresie nie zarejestrowano dystansu dla tego wpisu, wi\u0119c wynik Eco "
            "nie zosta\u0142 policzony."
        ),
        "eco.state.no_access_title": "Brak dost\u0119pu do Eco Driving",
        "eco.state.no_access": (
            "Twoje konto nie ma dost\u0119pu do \u017cadnego rankingu Eco Driving albo \u017caden "
            "dostawca nie jest skonfigurowany. Popro\u015b administratora o nadanie dost\u0119pu."
        ),
        "eco.state.no_periods_title": "Brak okres\u00f3w rankingowych",
        "eco.state.no_entries": "Brak wierszy w tej grupie dla wybranego okresu.",
        # -- identity field labels (COPY_AND_TERMINOLOGY §7.3) ----------------
        "eco.id.client": "Klient",
        "eco.id.provider": "Dostawca / rodzina rankingu",
        "eco.id.period_type": "Typ okresu",
        "eco.id.period_label": "Utrwalona etykieta okresu",
        "eco.id.period_start": "Pocz\u0105tek okresu",
        "eco.id.period_end": "Koniec okresu (wy\u0142\u0105czny)",
        "eco.id.partiality": "Stan cz\u0119\u015bciowo\u015bci",
        "eco.id.sequence": "Numer okresu w miesi\u0105cu",
        "eco.id.assigned_id": "Przypisane ID (nieprzejrzyste)",
        "eco.id.group": "Grupa rankingowa",
        "eco.id.qualification": "Status kwalifikacji",
        "eco.id.calculation": "Status oblicze\u0144",
        "eco.id.metadata_source": "\u0179r\u00f3d\u0142o metadanych kierowcy",
        "eco.id.participants": "Uczestnicy rankingu",
        "eco.id.band_share": "Udzia\u0142 pasma oceny",
        "eco.id.total_distance": "Dystans \u0142\u0105cznie",
        "eco.value.qualified": "spe\u0142niona",
        "eco.value.low_distance": "niespe\u0142niona \u2014 poni\u017cej progu dystansu",
        "eco.value.no_distance": "niespe\u0142niona \u2014 brak dystansu",
        "eco.value.calc_ok": "zako\u0144czone",
        "eco.value.chart_current": "bie\u017c\u0105ca karta kierowcy",
        "eco.value.chart_none": "brak wpisu w karcie kierowcy",
        "eco.value.not_ranked": "poza rankingiem",
        "eco.basis.month": "Miesi\u0105c",
        "eco.basis.weeks": "Tygodnie w rankingu",
        "eco.basis.whole_month": "Ca\u0142y miesi\u0105c",
        "eco.basis.clear": "Wyczy\u015b\u0107",
        "eco.basis.week_aria": "Tygodnie w podstawie rankingu",
        "eco.basis.card_select": "Dodaj {label} do podstawy rankingu",
        "eco.basis.card_deselect": "Usu\u0144 {label} z podstawy rankingu",
        "eco.basis.sentence_month": "{label} \u00b7 {range} \u00b7 {days} dni",
        "eco.basis.sentence_weeks": "{label} zsumowane \u00b7 {range} \u00b7 {days} dni",
        "eco.basis.whole_month_source": (
            "ca\u0142y miesi\u0105c \u2014 wynik z utrwalonego miesi\u0119cznego uj\u0119cia sprawozdawczego"
        ),
        "eco.basis.whole_month_dynamic_source": (
            "ca\u0142y miesi\u0105c \u2014 brak utrwalonego uj\u0119cia miesi\u0119cznego, wynik przeliczony "
            "od nowa z przejazd\u00f3w \u017ar\u00f3d\u0142owych dla pe\u0142nego zakresu miesi\u0105ca"
        ),
        "eco.basis.no_data_title": "Brak danych dla wybranego zakresu",
        "eco.basis.no_data": (
            "W wybranym zakresie nie ma \u017cadnych przejazd\u00f3w \u017ar\u00f3d\u0142owych dla tego klienta. "
            "Wybierz inny miesi\u0105c lub inne tygodnie."
        ),
        "eco.basis.dynamic_source": (
            "wyb\u00f3r tygodni \u2014 wynik przeliczony od nowa z przejazd\u00f3w \u017ar\u00f3d\u0142owych dla "
            "wybranych przedzia\u0142\u00f3w; utrwalone uj\u0119cia narastaj\u0105ce nie s\u0105 sumowane"
        ),
        "eco.basis.gap": (
            "Wybrane tygodnie nie s\u0105 ci\u0105g\u0142e. Podstawa rankingu ma przerw\u0119."
        ),
        "eco.basis.empty_title": "Nie wybrano \u017cadnego tygodnia",
        "eco.basis.empty": "Wybierz co najmniej jeden tydzie\u0144, aby zobaczy\u0107 ranking.",
        "eco.basis.trips_volume": "{trips} przejazd\u00f3w \u00b7 {km} km",
        "eco.basis.no_months_title": "Brak danych \u017ar\u00f3d\u0142owych",
        "eco.basis.no_months": (
            "Dla tego klienta nie ma jeszcze \u017cadnych przypisa\u0144 przejazd\u00f3w Eco Driving."
        ),
        "eco.basis.month_previous": "Poprzedni miesi\u0105c",
        "eco.basis.month_next": "Nast\u0119pny miesi\u0105c",
        "eco.basis.detail_context": "Podstawa: {month} \u00b7 {label}",
        "eco.state.driver_absent": (
            "Kierowca {driver} nie wyst\u0119puje w wybranym kontek\u015bcie ({period}). "
            "Pokazujemy ranking dla tego kontekstu."
        ),
    }
}


def available_keys(locale: str = DEFAULT_LOCALE) -> frozenset[str]:
    """Every key defined for ``locale``. Used by the shell tests."""
    return frozenset(_CATALOG.get(locale, {}))


def catalog(locale: str = DEFAULT_LOCALE) -> Mapping[str, str]:
    return dict(_CATALOG.get(locale, {}))


def t(key: str, *, locale: str = DEFAULT_LOCALE, **params: object) -> str:
    """Resolve a translation key.

    An unknown key returns the key itself: a missing translation must degrade to
    a visible, greppable marker rather than break a page render. Tests assert
    that no key the shell uses is missing, so an unresolved key never reaches
    production silently.
    """
    text = _CATALOG.get(locale, {}).get(key)
    if text is None:
        return key
    if params:
        return text.format(**params)
    return text
