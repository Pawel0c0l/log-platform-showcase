#!/usr/bin/env python3
"""Deterministic checks for the portal UI foundation and the approved shared shell.

Covers the first redesign slice: the static asset foundation, the approved design
token system, self-hosted typography, the AUTO/light/dark theme engine, the
horizontal shared shell, the Polish translation-key foundation, the shared-shell
accessibility foundation, and the navigation authorization model.

Approved design reference (read-only, not part of the repository):
``design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff``
— screens ``SHL-001``/``SHL-002``/``SHL-003``, criteria ``SH-1``..``SH-10``.

This file asserts presentation and accessibility contracts only. It deliberately
does not assert business behaviour; the module test files own that.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_ui_foundation_and_shared_shell.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "api") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "api"))

from portal_ui import assets as portal_assets  # noqa: E402
from portal_ui import i18n, shell  # noqa: E402

STATIC = REPO_ROOT / "api" / "static"
TOKENS_CSS = (STATIC / "css" / "tokens.css").read_text(encoding="utf-8")
PORTAL_CSS = (STATIC / "css" / "portal.css").read_text(encoding="utf-8")
FONTS_CSS = (STATIC / "css" / "fonts.css").read_text(encoding="utf-8")
THEME_JS = (STATIC / "js" / "theme.js").read_text(encoding="utf-8")
SHELL_JS = (STATIC / "js" / "shell.js").read_text(encoding="utf-8")


def _fail(message: str) -> None:
    raise AssertionError(message)


# ---------------------------------------------------------------------------
# 1. Static asset foundation
# ---------------------------------------------------------------------------
def test_static_asset_foundation() -> None:
    assert portal_assets.STATIC_DIR == STATIC, portal_assets.STATIC_DIR
    assert portal_assets.STATIC_URL_PREFIX == "/static"

    for relative in (*portal_assets.STYLESHEETS, *portal_assets.SCRIPTS):
        path = STATIC / relative
        assert path.is_file(), f"declared asset missing: {relative}"

    # The cache key follows content, so a redeploy invalidates caches without a
    # manual version bump, and it is stable for a given checkout.
    assert re.fullmatch(r"[0-9a-f]{10}", portal_assets.ASSET_VERSION), portal_assets.ASSET_VERSION
    assert portal_assets._compute_asset_version() == portal_assets.ASSET_VERSION

    url = portal_assets.asset_url("css/tokens.css")
    assert url == f"/static/css/tokens.css?v={portal_assets.ASSET_VERSION}", url

    head = portal_assets.head_asset_tags()
    for relative in portal_assets.STYLESHEETS:
        assert f'href="/static/{relative}?v=' in head, relative
    # theme.js must stay render-blocking: it applies the stored theme before
    # first paint, which is what prevents a light/dark flash on load.
    assert '<script src="/static/js/theme.js?v=' in head, head
    assert 'defer src="/static/js/shell.js?v=' in head, head

    # woff2 must be registered, or some browsers refuse the font.
    import mimetypes

    assert mimetypes.guess_type("x.woff2")[0] == "font/woff2"
    print("PASS: static asset foundation is content-versioned and complete")


# ---------------------------------------------------------------------------
# 2. Approved design tokens
# ---------------------------------------------------------------------------
APPROVED_LIGHT = {
    "--lp-surface-page": "#f4f6f7",
    "--lp-surface-raised": "#ffffff",
    "--lp-surface-appbar": "#14171c",
    "--lp-border-default": "#e2e5e9",
    "--lp-text-primary": "#14171c",
    "--lp-text-secondary": "#33393f",
    "--lp-text-muted": "#5c646f",
    "--lp-text-faint": "#8b939e",
    "--lp-accent-base": "#ff7a18",
    "--lp-accent-on-surface": "#a85a0d",
    "--lp-action-primary-bg": "#14171c",
    "--lp-state-positive-fg": "#1f6b45",
    "--lp-state-warning-fg": "#8a5b09",
    "--lp-state-negative-fg": "#9e2c26",
}

APPROVED_DARK = {
    "--lp-surface-page": "#0f1215",
    "--lp-surface-raised": "#16191e",
    "--lp-surface-appbar": "#0a0c0f",
    "--lp-border-default": "#262b33",
    "--lp-text-primary": "#eef1f4",
    "--lp-accent-base": "#ff8a33",
    "--lp-accent-on-surface": "#ffab63",
    "--lp-action-primary-bg": "#eef1f4",
}

APPROVED_DIMENSIONS = {
    "--lp-height-appbar": "56px",
    "--lp-height-context-bar": "64px",
    "--lp-height-toolbar": "48px",
    "--lp-height-footer": "44px",
    "--lp-height-row-compact": "32px",
    "--lp-height-row-comfortable": "40px",
    "--lp-width-filter-panel": "352px",
    "--lp-width-row-panel": "520px",
    "--lp-radius-lg": "4px",
    "--lp-radius-md": "3px",
}


def _root_block() -> str:
    start = TOKENS_CSS.index(":root {")
    end = TOKENS_CSS.index("\n}", start)
    return TOKENS_CSS[start:end]


def _dark_override_block() -> str:
    start = TOKENS_CSS.index(':root[data-theme="dark"] {')
    end = TOKENS_CSS.index("\n}", start)
    return TOKENS_CSS[start:end]


def test_design_tokens_match_approved_contract() -> None:
    root = _root_block()
    for token, value in APPROVED_LIGHT.items():
        assert f"{token}: {value};" in root, f"light {token} must be {value}"
    for token, value in APPROVED_DIMENSIONS.items():
        assert f"{token}: {value};" in root, f"{token} must be {value}"

    dark = _dark_override_block()
    for token, value in APPROVED_DARK.items():
        assert f"{token}: {value};" in dark, f"dark {token} must be {value}"

    # The superseded navy/alpha palette must be gone everywhere.
    for stale in ("#080b10", "#ff7300", "#101722", "#273244"):
        assert stale not in TOKENS_CSS, f"superseded token value {stale} still present"
        assert stale not in PORTAL_CSS, f"superseded token value {stale} still present"

    # SH-10: light and dark are the same layout. No dimension token may be
    # redefined in a theme block, or the two themes could drift apart.
    for block_name, block in (
        ("dark override", dark),
        ("prefers-color-scheme", TOKENS_CSS[TOKENS_CSS.index("@media (prefers-color-scheme: dark)"):]),
    ):
        for token in APPROVED_DIMENSIONS:
            assert token not in block, f"{token} redefined in {block_name}: themes must share layout"

    # The two dark blocks must stay identical, or the explicit override and the
    # AUTO path would render differently.
    auto_start = TOKENS_CSS.index(':root:not([data-theme="light"]) {')
    auto_block = TOKENS_CSS[auto_start:TOKENS_CSS.index("\n  }", auto_start)]

    def _decls(text: str) -> set[str]:
        return {line.strip() for line in text.splitlines() if line.strip().startswith("--")}

    missing = _decls(dark) - _decls(auto_block)
    assert not missing, f"explicit dark declares tokens AUTO-dark does not: {sorted(missing)}"
    print("PASS: design tokens implement the approved contract in both themes")


# ---------------------------------------------------------------------------
# 3. Typography — self-hosted, no third-party CDN
# ---------------------------------------------------------------------------
def test_typography_is_self_hosted() -> None:
    assert '"IBM Plex Sans"' in TOKENS_CSS and '"IBM Plex Mono"' in TOKENS_CSS

    faces = re.findall(r'src:\s*url\("([^"]+)"\)', FONTS_CSS)
    assert len(faces) == 6, faces
    for relative in faces:
        assert relative.startswith("../fonts/"), relative
        resolved = (STATIC / "css" / relative).resolve()
        assert resolved.is_file(), f"font file missing: {relative}"
        assert resolved.stat().st_size > 0, relative

    # Weights above 600 are not used by the approved design.
    for weight in re.findall(r"font-weight:\s*(\d+);", FONTS_CSS):
        assert int(weight) <= 600, weight

    combined = (FONTS_CSS + TOKENS_CSS + PORTAL_CSS + portal_assets.head_asset_tags()).lower()
    for host in ("fonts.googleapis.com", "fonts.gstatic.com", "//fonts.", "use.typekit", "cdn.jsdelivr", "unpkg.com"):
        assert host not in combined, f"third-party font/asset CDN referenced: {host}"

    # Only the fonts the application actually uses may ship in the repository.
    shipped = {p.name for p in (STATIC / "fonts").glob("*.woff2")}
    referenced = {Path(r).name for r in faces}
    assert shipped == referenced, f"unused or missing font files: {shipped ^ referenced}"
    assert (STATIC / "fonts" / "LICENSE-IBM-Plex.txt").is_file(), "font licence must ship with the fonts"
    print("PASS: IBM Plex is self-hosted with no third-party CDN dependency")


# ---------------------------------------------------------------------------
# 4. Theme engine (SHL-003)
# ---------------------------------------------------------------------------
def test_theme_engine() -> None:
    # AUTO is the ABSENCE of data-theme, so the prefers-color-scheme media query
    # keeps applying and an OS change re-themes the page live.
    assert "@media (prefers-color-scheme: dark)" in TOKENS_CSS
    assert ':root:not([data-theme="light"])' in TOKENS_CSS, "AUTO-dark must not override explicit light"
    assert ':root[data-theme="dark"]' in TOKENS_CSS
    assert ':root[data-theme="light"] { color-scheme: light; }' in TOKENS_CSS
    assert 'removeAttribute("data-theme")' in THEME_JS, "AUTO must clear data-theme"
    assert 'setAttribute("data-theme", mode)' in THEME_JS

    # The browser-local mirror is still the pre-authentication path.
    assert 'window.localStorage' in THEME_JS
    assert '"logplatform.theme"' in THEME_JS
    # Approved stage S13 made the ACCOUNT row the durable authority, so a write
    # to the server is now correct. It is still the only server call, it is
    # still gated on the document declaring server scope, and it must not become
    # a beacon or a synchronous request. Account isolation, reconciliation and
    # the failure contract are owned by
    # `test_portal_server_preferences_and_saved_views.py`.
    assert 'data-theme-scope' in THEME_JS, "the server preference must be the declared authority"
    assert THEME_JS.count("fetch(") == 1, "exactly one server call, the preference write"
    for forbidden in ("XMLHttpRequest", "navigator.sendBeacon"):
        assert forbidden not in THEME_JS, f"theme switching must not call the server: {forbidden}"

    # Switching theme must not reload or navigate, so route, query string,
    # URL-held filters and scroll position all survive by construction.
    for forbidden in ("location.reload", "location.href", "location.assign", "window.location =", "form.submit"):
        assert forbidden not in THEME_JS, f"theme switching must not navigate: {forbidden}"

    # A blocked or unavailable store must not break the page.
    assert "catch (err)" in THEME_JS

    switcher = shell._theme_switcher_html()
    for mode in ("auto", "light", "dark"):
        assert f'data-theme-option="{mode}"' in switcher, mode
    assert switcher.count("<button") == 3, switcher
    assert 'aria-pressed="true"' in switcher and 'aria-pressed="false"' in switcher
    assert 'role="group"' in switcher
    assert "☀" in switcher and "☾" in switcher
    # The control cannot act without scripting, so it is served hidden and
    # revealed by theme.js rather than offered as an inert control.
    assert " hidden " in switcher, switcher
    assert 'removeAttribute("hidden")' in THEME_JS
    print("PASS: AUTO/light/dark theme engine is reload-free and browser-local")


# ---------------------------------------------------------------------------
# 5. Shared shell structure (SHL-001 / SHL-002)
# ---------------------------------------------------------------------------
def test_shell_structure_and_wide_workspace() -> None:
    items = shell.primary_nav_items(active_key="database", is_admin=True, has_eco_ranking_access=True)
    appbar = shell.appbar_html(nav_items=items, user_label="Alice Kowalska")

    assert '<header class="lp-appbar">' in appbar
    assert 'class="lp-brand-mark"' in appbar and "Log Platform" in appbar
    assert 'aria-label="Nawigacja główna"' in appbar
    assert 'class="lp-account-name">Alice Kowalska<' in appbar
    assert 'class="lp-avatar" aria-hidden="true">AK<' in appbar
    assert 'href="/logout"' in appbar

    # SH-1: the frame heights are the approved ones and come from tokens.
    assert "height: var(--lp-height-appbar)" in PORTAL_CSS
    assert "height: var(--lp-height-context-bar)" in PORTAL_CSS

    # SH-2: the working area is full width. The pre-redesign 1280 px cap is gone.
    assert not re.search(r"max-width:\s*1280px", PORTAL_CSS), "1280 px content cap must be gone"
    work = PORTAL_CSS[PORTAL_CSS.index(".lp-work {"):]
    work = work[: work.index("}")]
    assert "width: 100%;" in work, work
    # The cap is explicitly cleared rather than merely omitted, so a stray
    # inherited `max-width` cannot narrow the workspace again.
    assert "max-width: none;" in work, work
    assert not re.search(r"max-width:\s*\d", work), work

    context = shell.context_bar_html(
        module_name="Przejazdy", client_name="Acme Logistics", client_code="ACME_01"
    )
    assert 'class="lp-client-name">Acme Logistics<' in context
    assert 'class="lp-client-code">ACME_01<' in context
    assert 'class="lp-module-name">Przejazdy<' in context
    # SH-6: the client never disappears at narrow widths.
    assert ".lp-client-name," in PORTAL_CSS and "display: inline-block;" in PORTAL_CSS

    # A surface with no single client in scope leads with module identity rather
    # than inventing a client.
    admin_context = shell.context_bar_html(module_name="Administracja")
    assert 'class="lp-client-name">Administracja<' in admin_context
    assert "lp-client-code" not in admin_context
    print("PASS: shared shell frame, client context and full-width workspace")


def test_nav_active_state_is_not_colour_alone() -> None:
    items = shell.primary_nav_items(active_key="database", is_admin=True, has_eco_ranking_access=True)
    html = shell.appbar_html(nav_items=items, user_label="Alice")

    # SH-5: aria-current carries the state semantically...
    assert html.count('aria-current="page"') == 1, html
    assert 'href="/user/database" aria-current="page"' in html, html

    # ...and the visual carriers are an underline plus a weight step, so the
    # state survives with colour vision disabled.
    rule = PORTAL_CSS[PORTAL_CSS.index(".lp-nav-item[aria-current] {"):]
    rule = rule[: rule.index("}")]
    assert "box-shadow: inset 0 -2px 0" in rule, rule
    assert "font-weight" in rule, rule

    # SH-3 / SH-4: fixed order, and the tools group is a quieter step.
    assert [i.label for i in items] == ["Raporty", "Dane", "Analizy", "Artefakty", "Administracja"]
    assert [i.group for i in items[:3]] == ["work"] * 3
    assert [i.group for i in items[3:]] == ["tools"] * 2
    assert '.lp-nav-item[data-nav-group="tools"] { color: var(--lp-text-on-appbar-quiet); }' in PORTAL_CSS
    print("PASS: active navigation state is semantic and not colour-alone")


# ---------------------------------------------------------------------------
# 6. Navigation authorization
# ---------------------------------------------------------------------------
def test_navigation_authorization_matrix() -> None:
    def labels(**kw):
        return [i.label for i in shell.primary_nav_items(active_key="reports", **kw)]

    ordinary = labels(is_admin=False, has_eco_ranking_access=False)
    assert ordinary == ["Raporty", "Dane"], ordinary
    assert "Administracja" not in ordinary and "Artefakty" not in ordinary

    eco_user = labels(is_admin=False, has_eco_ranking_access=True)
    assert eco_user == ["Raporty", "Dane", "Analizy"], eco_user

    # Administrators do NOT bypass the Eco Driving client grant.
    admin_no_eco = labels(is_admin=True, has_eco_ranking_access=False)
    assert "Analizy" not in admin_no_eco, admin_no_eco
    assert "Artefakty" in admin_no_eco and "Administracja" in admin_no_eco

    # Section navigation is gated by the same admin flag.
    assert shell.section_nav_items(active_key="users", is_admin=False) == []
    assert shell.section_nav_items(active_key="files", is_admin=False) == []
    assert len(shell.section_nav_items(active_key="users", is_admin=True)) == 8

    # Every primary entry maps to a real route prefix.
    for key in shell.PRIMARY_NAV_ORDER:
        href = shell._PRIMARY_SPEC[key][1]
        assert href.startswith("/"), href
    print("PASS: navigation visibility preserves the existing access model")


# ---------------------------------------------------------------------------
# 7. Polish translation-key foundation
# ---------------------------------------------------------------------------
def test_translation_key_foundation() -> None:
    assert i18n.DEFAULT_LOCALE == "pl"

    # Every key the shell renders must resolve; an unresolved key would surface
    # as the raw key string in the UI.
    source = (REPO_ROOT / "api" / "portal_ui" / "shell.py").read_text(encoding="utf-8")
    used = set(re.findall(r't\(\s*["\']([a-z0-9_.]+)["\']', source))
    used |= set(re.findall(r'label_key\s*=\s*["\']([a-z0-9_.]+)["\']', source))
    used |= {spec[0] for spec in shell._PRIMARY_SPEC.values()}
    used |= {entry[1] for entry in shell._ADMIN_SECTIONS}
    used |= {entry[1] for entry in shell._ARTIFACT_SECTIONS}
    used |= {entry[1] for entry in shell._DATA_SECTIONS}
    assert used, "no translation keys discovered — the scan is broken"

    available = i18n.available_keys()
    missing = sorted(used - available)
    assert not missing, f"shell uses undefined translation keys: {missing}"

    for key in sorted(used):
        assert i18n.t(key) != key, f"key {key} resolves to itself"

    # An unknown key degrades to a visible, greppable marker rather than raising.
    assert i18n.t("shell.does.not.exist") == "shell.does.not.exist"

    # The approved Polish vocabulary, verbatim.
    assert i18n.t("shell.nav.reports") == "Raporty"
    assert i18n.t("shell.nav.data") == "Dane"
    assert i18n.t("shell.nav.analytics") == "Analizy"
    assert i18n.t("shell.nav.artifacts") == "Artefakty"
    assert i18n.t("shell.nav.administration") == "Administracja"
    assert i18n.t("shell.account.logout") == "Wyloguj"
    assert i18n.t("shell.main.skip_link") == "Przejdź do treści"

    # The shell must not reintroduce hard-coded English chrome.
    items = shell.primary_nav_items(active_key="reports", is_admin=True, has_eco_ranking_access=True)
    chrome = shell.appbar_html(nav_items=items, user_label="Alice") + shell.nav_drawer_html(
        items, user_label="Alice"
    )
    for english in (">Reports<", ">Data<", ">Logout<", ">Sign out<", ">Administration<"):
        assert english not in chrome, english
    print("PASS: shared-shell terminology is Polish and routed through keys")


# ---------------------------------------------------------------------------
# 8. Accessibility foundation
# ---------------------------------------------------------------------------
def test_accessibility_foundation() -> None:
    # Visible keyboard focus, never removed, never hover-only.
    assert ":focus-visible" in PORTAL_CSS
    assert "outline: 2px solid var(--lp-accent-base)" in PORTAL_CSS
    assert "outline-offset: 2px" in PORTAL_CSS
    assert "outline: none" not in PORTAL_CSS and "outline: 0" not in PORTAL_CSS

    # Skip link into the working area.
    assert ".lp-skip-link" in PORTAL_CSS
    assert ".lp-skip-link:focus" in PORTAL_CSS

    # Reduced motion suppresses transitions.
    assert "@media (prefers-reduced-motion: reduce)" in PORTAL_CSS

    items = shell.primary_nav_items(active_key="reports", is_admin=True, has_eco_ranking_access=True)

    # Semantic landmarks with accessible names.
    appbar = shell.appbar_html(nav_items=items, user_label="Alice")
    assert "<nav " in appbar and 'aria-label="Nawigacja główna"' in appbar
    assert 'aria-label="Otwórz nawigację"' in appbar
    assert 'aria-expanded="false"' in appbar

    # Every shell control is a real button or link — no div-with-onclick.
    assert "onclick=" not in appbar
    assert 'role="button"' not in appbar

    # The drawer is a labelled dialog, focus-trapped, and returns focus on close.
    drawer = shell.nav_drawer_html(items, user_label="Alice")
    assert 'role="dialog"' in drawer and 'aria-modal="true"' in drawer
    assert 'aria-label="Nawigacja"' in drawer
    assert 'aria-label="Zamknij nawigację"' in drawer
    assert 'event.key === "Escape"' in SHELL_JS
    assert "lastFocused.focus()" in SHELL_JS
    assert 'event.key !== "Tab"' in SHELL_JS, "drawer must trap Tab"

    # At <=1023 px the app bar drops the account name and the sign-out link, so
    # the drawer must carry sign-out or it becomes unreachable.
    assert 'href="/logout"' in drawer, drawer
    assert "Wyloguj" in drawer, drawer

    # Section navigation is a named landmark and marks its active entry.
    section = shell.section_bar_html(
        shell.section_nav_items(active_key="audit", is_admin=True)
    )
    assert 'aria-label="Nawigacja sekcji"' in section
    assert section.count('aria-current="page"') == 1, section
    print("PASS: shared-shell accessibility foundation")


def test_markup_escapes_untrusted_values() -> None:
    hostile = '"><script>alert(1)</script>'
    context = shell.context_bar_html(
        module_name=hostile, client_name=hostile, client_code=hostile
    )
    appbar = shell.appbar_html(
        nav_items=shell.primary_nav_items(
            active_key="reports", is_admin=False, has_eco_ranking_access=False
        ),
        user_label=hostile,
    )
    for html in (context, appbar, shell.page_path_html(hostile, hostile)):
        assert "<script>" not in html, html
        assert "&lt;script&gt;" in html, html
    print("PASS: shell markup escapes untrusted values")


def main() -> None:
    test_static_asset_foundation()
    test_design_tokens_match_approved_contract()
    test_typography_is_self_hosted()
    test_theme_engine()
    test_shell_structure_and_wide_workspace()
    test_nav_active_state_is_not_colour_alone()
    test_navigation_authorization_matrix()
    test_translation_key_foundation()
    test_accessibility_foundation()
    test_markup_escapes_untrusted_values()
    print("\nALL PASS: portal UI foundation and approved shared shell")


if __name__ == "__main__":
    main()
