"""Approved shared application shell (``SHL-001`` / ``SHL-002`` / ``SHL-003``).

Frame, top to bottom: a 56 px app bar, a 64 px client context bar, then a
working area that consumes the full remaining width and height. The
pre-redesign 268 px sidebar is gone; primary navigation is horizontal and lives
in the app bar.

This module builds markup only. It holds no authorization logic: it renders the
navigation model it is handed, and the caller decides — from the same
server-side rules that gate the routes — which entries exist. Hiding a
navigation item is presentation; it never replaces route authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape as _escape

from .i18n import t

# ---------------------------------------------------------------------------
# Primary navigation model
# ---------------------------------------------------------------------------

# The collapsed-navigation drawer is referenced by the menu button through
# `aria-controls`, so the trigger and the surface it opens are one relationship
# rather than two unrelated controls (ACCESSIBILITY_SPEC §5).
NAV_DRAWER_ID = "lp-nav-drawer"

PRIMARY_REPORTS = "reports"
PRIMARY_DATA = "data"
PRIMARY_ANALYTICS = "analytics"
PRIMARY_ARTIFACTS = "artifacts"
PRIMARY_ADMINISTRATION = "administration"

# The three data-access modes render one step louder than the tools group
# (PRODUCT_BEHAVIOR_CONTRACT §1.2). Artefakty is a technical surface and must
# not read as a fourth data mode.
GROUP_WORK = "work"
GROUP_TOOLS = "tools"

# Fixed order: Raporty · Dane · Analizy · Artefakty · Administracja.
PRIMARY_NAV_ORDER = (
    PRIMARY_REPORTS,
    PRIMARY_DATA,
    PRIMARY_ANALYTICS,
    PRIMARY_ARTIFACTS,
    PRIMARY_ADMINISTRATION,
)

_PRIMARY_SPEC = {
    PRIMARY_REPORTS: ("shell.nav.reports", "/user/reports", GROUP_WORK),
    PRIMARY_DATA: ("shell.nav.data", "/user/database", GROUP_WORK),
    PRIMARY_ANALYTICS: ("shell.nav.analytics", "/user/eco-driving", GROUP_WORK),
    PRIMARY_ARTIFACTS: ("shell.nav.artifacts", "/artifact-explorer", GROUP_TOOLS),
    PRIMARY_ADMINISTRATION: ("shell.nav.administration", "/admin", GROUP_TOOLS),
}

# Legacy per-page ``active_key`` values map onto the five primary modules. The
# granular keys stay meaningful: they also select the active section entry.
ACTIVE_KEY_TO_PRIMARY = {
    "reports": PRIMARY_REPORTS,
    "database": PRIMARY_DATA,
    "database-exports": PRIMARY_DATA,
    "eco-driving": PRIMARY_ANALYTICS,
    "files": PRIMARY_ARTIFACTS,
    "artifact-folders": PRIMARY_ARTIFACTS,
    "artifact-explorer": PRIMARY_ARTIFACTS,
    "admin-home": PRIMARY_ADMINISTRATION,
    "users": PRIMARY_ADMINISTRATION,
    "groups": PRIMARY_ADMINISTRATION,
    "client-access": PRIMARY_ADMINISTRATION,
    "database-access": PRIMARY_ADMINISTRATION,
    "eco-driving-permissions": PRIMARY_ADMINISTRATION,
    "report-folders": PRIMARY_ADMINISTRATION,
    "audit": PRIMARY_ADMINISTRATION,
}

# Section navigation for the two primary items that group several routes, plus
# the Database Explorer's own two surfaces. Raporty and Analizy have a single
# surface each and therefore no section bar.
_ADMIN_SECTIONS = (
    ("admin-home", "shell.subnav.admin.overview", "/admin"),
    ("users", "shell.subnav.admin.users", "/admin/users"),
    ("groups", "shell.subnav.admin.groups", "/admin/groups"),
    ("client-access", "shell.subnav.admin.client_access", "/admin/client-access"),
    ("database-access", "shell.subnav.admin.database_access", "/admin/client-access/database"),
    ("eco-driving-permissions", "shell.subnav.admin.eco_driving_permissions", "/admin/client-access/eco-driving"),
    ("report-folders", "shell.subnav.admin.report_folders", "/admin/report-folders"),
    ("audit", "shell.subnav.admin.audit", "/admin/audit"),
)

_ARTIFACT_SECTIONS = (
    ("files", "shell.subnav.artifacts.all", "/artifact-explorer"),
    ("artifact-folders", "shell.subnav.artifacts.folders", "/artifact-explorer/folders"),
)

_DATA_SECTIONS = (
    ("database", "shell.subnav.data.datasets", "/user/database"),
    ("database-exports", "shell.subnav.data.exports", "/user/database/exports"),
)


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    href: str
    group: str
    active: bool


def primary_key_for(active_key: str) -> str:
    """Primary module a page-level ``active_key`` belongs to."""
    return ACTIVE_KEY_TO_PRIMARY.get(active_key or "", PRIMARY_REPORTS)


def primary_label_for(active_key: str) -> str:
    """Polish module name for the primary module a page belongs to."""
    return t(_PRIMARY_SPEC[primary_key_for(active_key)][0])


def primary_nav_items(
    *,
    active_key: str,
    is_admin: bool,
    has_eco_ranking_access: bool,
) -> list[NavItem]:
    """The visible primary navigation for one signed-in account.

    Visibility mirrors the pre-redesign sidebar exactly, so the redesign grants
    nobody a new entry point:

    * ``Raporty`` / ``Dane`` — every authenticated account;
    * ``Analizy`` — only with effective Eco Driving ranking access, which
      administrators do not bypass;
    * ``Artefakty`` — administrators only (artifact triage is an operator
      surface, and it lived in the admin section of the old sidebar);
    * ``Administracja`` — administrators only.
    """
    active_primary = primary_key_for(active_key)
    allowed = {
        PRIMARY_REPORTS: True,
        PRIMARY_DATA: True,
        PRIMARY_ANALYTICS: bool(has_eco_ranking_access),
        PRIMARY_ARTIFACTS: bool(is_admin),
        PRIMARY_ADMINISTRATION: bool(is_admin),
    }
    items: list[NavItem] = []
    for key in PRIMARY_NAV_ORDER:
        if not allowed[key]:
            continue
        label_key, href, group = _PRIMARY_SPEC[key]
        items.append(
            NavItem(
                key=key,
                label=t(label_key),
                href=href,
                group=group,
                active=key == active_primary,
            )
        )
    return items


def section_nav_items(
    *,
    active_key: str,
    is_admin: bool,
    include_database_exports: bool = False,
) -> list[NavItem]:
    """Section entries for the active primary module, or an empty list."""
    primary = primary_key_for(active_key)
    if primary == PRIMARY_ADMINISTRATION:
        if not is_admin:
            return []
        spec = _ADMIN_SECTIONS
    elif primary == PRIMARY_ARTIFACTS:
        if not is_admin:
            return []
        spec = _ARTIFACT_SECTIONS
    elif primary == PRIMARY_DATA and include_database_exports:
        spec = _DATA_SECTIONS
    else:
        return []
    return [
        NavItem(key=key, label=t(label_key), href=href, group=primary, active=key == active_key)
        for key, label_key, href in spec
    ]


# ---------------------------------------------------------------------------
# Markup
# ---------------------------------------------------------------------------


def account_initials(display_name: str) -> str:
    """Up to two initials for the account chip. Decorative — the avatar is
    ``aria-hidden`` and the account name is exposed as text."""
    parts = [part for part in str(display_name or "").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[1][:1]).upper()


def _aria_current(active: bool) -> str:
    """`aria-current` is the semantic carrier of the active state; the
    underline and the weight step are its visual, non-colour carriers."""
    return ' aria-current="page"' if active else ""


def _nav_html(items: list[NavItem]) -> str:
    out = []
    for item in items:
        current = _aria_current(item.active)
        out.append(
            f'<a class="lp-nav-item" data-nav-group="{item.group}" '
            f'href="{_escape(item.href, quote=True)}"{current}>{_escape(item.label)}</a>'
        )
    return "".join(out)


def nav_drawer_html(items: list[NavItem], *, user_label: str = "") -> str:
    """Collapsed navigation for <=1279 px, grouped as Praca / Narzędzia.

    The drawer also carries the account name and the sign-out link, because at
    <=1023 px the app bar drops both to keep the 56 px bar workable. Without
    this section signing out would be unreachable at those widths, which the
    keyboard-reachability rule in ACCESSIBILITY_SPEC §4 does not allow.
    """
    sections = []
    for group, label_key in ((GROUP_WORK, "shell.nav.group_work"), (GROUP_TOOLS, "shell.nav.group_tools")):
        group_items = [item for item in items if item.group == group]
        if not group_items:
            continue
        links = "".join(
            f'<a class="lp-nav-drawer-item" href="{_escape(item.href, quote=True)}"'
            f'{_aria_current(item.active)}>{_escape(item.label)}</a>'
            for item in group_items
        )
        sections.append(
            f'<p class="lp-nav-drawer-group">{_escape(t(label_key))}</p>'
            f'<div class="lp-nav-drawer-list">{links}</div>'
        )
    account = (
        f'<p class="lp-nav-drawer-group">{_escape(t("shell.account.aria_label"))}</p>'
        '<div class="lp-nav-drawer-list">'
        + (
            f'<span class="lp-nav-drawer-account">{_escape(user_label)}</span>'
            if user_label
            else ""
        )
        + f'<a class="lp-nav-drawer-item" href="/logout">{_escape(t("shell.account.logout"))}</a>'
        "</div>"
    )
    return (
        '<div class="lp-nav-scrim" data-nav-scrim hidden></div>'
        f'<div class="lp-nav-drawer" id="{NAV_DRAWER_ID}" data-nav-drawer hidden role="dialog" aria-modal="true" '
        f'aria-label="{_escape(t("shell.nav.drawer_title"), quote=True)}">'
        '<div class="lp-nav-drawer-head">'
        f'<span class="lp-nav-drawer-title">{_escape(t("shell.nav.drawer_title"))}</span>'
        f'<button class="lp-nav-drawer-close" type="button" data-nav-drawer-close '
        f'aria-label="{_escape(t("shell.nav.close"), quote=True)}">&#215;</button>'
        "</div>" + "".join(sections) + account + "</div>"
    )


def _theme_switcher_html(
    *,
    mode: str = "auto",
    durable: bool = False,
    action: str = "",
    next_path: str = "",
    unavailable: bool = False,
) -> str:
    """AUTO · ☀ · ☾ override (`SHL-003`, `D-011`).

    Two shapes, and which one renders is decided by whether the SERVER can store
    the choice for this account (approved stage S13):

    * ``durable`` — the account preference is available. The control is a real
      POST form with three submit buttons, so it works with scripting
      unavailable, which is the progressive-enhancement rule every other portal
      control already follows. ``theme.js`` intercepts the submit and performs
      the same write in place, so the approved "switching must not reload the
      view, lose filters or reset scroll" behaviour is what a scripted browser
      actually gets.
    * not ``durable`` — no account preference is readable. Either none exists
      yet (an unauthenticated page, or a database that confirmed it does not
      have the S13 migration), in which case the control falls back to the
      pre-S13 browser-local behaviour; or ``unavailable``, where the account IS
      the authority and the server simply could not read it. Both are served
      ``hidden`` and revealed by ``theme.js``, because without scripting they
      could not act at all and AUTO already follows ``prefers-color-scheme``.
      Only the ``unavailable`` shape carries the "not stored on the account"
      message, because only there is a choice knowingly not durable — the
      pre-S13 browser-local case stores exactly what it promises.

    The status paragraph is empty at rest and is where the script states that a
    preference could NOT be stored. A failed write must never leave the UI
    implying the choice will follow the account to another device.
    """
    options = (
        ("auto", t("shell.theme.auto"), t("shell.theme.auto_name")),
        ("light", "☀", t("shell.theme.light_name")),
        ("dark", "☾", t("shell.theme.dark_name")),
    )
    active = mode if mode in {"auto", "light", "dark"} else "auto"
    status = (
        f'<p class="lp-theme-status" data-theme-status role="status" aria-live="polite" '
        f'aria-label="{_escape(t("shell.theme.status_aria"), quote=True)}"></p>'
    )
    if durable and action:
        buttons = "".join(
            f'<button class="lp-theme-option" type="submit" name="theme" value="{option}" '
            f'data-theme-option="{option}" '
            f'aria-pressed="{"true" if option == active else "false"}" '
            f'aria-label="{_escape(name, quote=True)}">{_escape(glyph)}</button>'
            for option, glyph, name in options
        )
        return (
            f'<form class="lp-theme" data-theme-switcher data-theme-form method="post" '
            f'action="{_escape(action, quote=True)}" role="group" '
            f'data-theme-failed-message="{_escape(t("shell.theme.save_failed"), quote=True)}" '
            f'aria-label="{_escape(t("shell.theme.group"), quote=True)}">'
            f'<input type="hidden" name="next" value="{_escape(next_path, quote=True)}">'
            f"{buttons}{status}</form>"
        )
    buttons = "".join(
        f'<button class="lp-theme-option" type="button" data-theme-option="{option}" '
        f'aria-pressed="{"true" if option == active else "false"}" '
        f'aria-label="{_escape(name, quote=True)}">{_escape(glyph)}</button>'
        for option, glyph, name in options
    )
    failed_attr = (
        f' data-theme-failed-message="{_escape(t("shell.theme.save_failed"), quote=True)}"'
        if unavailable
        else ""
    )
    return (
        f'<div class="lp-theme" data-theme-switcher role="group" hidden{failed_attr} '
        f'aria-label="{_escape(t("shell.theme.group"), quote=True)}">{buttons}{status}</div>'
    )


def export_indicator_html(active_exports: int, *, href: str = "/user/database/exports") -> str:
    """`n eksport w toku` in the app bar (`DB-007`, `DB-49`).

    Absent at zero rather than rendered empty: the approved component has an
    idle state that is *absent*, and an always-present "0 eksport" would be
    noise on every page. The dot is decorative — the count is stated in words,
    so the state does not depend on colour — and the whole thing is a link to
    the background-export list, which is where the user acts on it.

    The count is passed in already resolved and already scoped to the signed-in
    user; this function performs no lookup and knows nothing about ownership.
    """
    try:
        count = int(active_exports or 0)
    except (TypeError, ValueError):
        count = 0
    if count < 1:
        return ""
    return (
        f'<a class="lp-export-indicator" href="{_escape(href, quote=True)}" '
        f'aria-label="{_escape(t("shell.export.indicator_aria"), quote=True)}" '
        'data-export-indicator>'
        '<span class="lp-export-dot" aria-hidden="true"></span>'
        f'<span class="lp-export-count">{_escape(t("shell.export.indicator", count=count))}</span></a>'
    )


def technical_badge_html(label: str) -> str:
    """Uppercase mono marker for a technical surface (`COMPONENT_CATALOG`:
    Technical badge / Operator-mode badge).

    It is context, not authorization and not a semantic status: the approved
    treatment is a neutral bordered chip in muted text, so it can never read as
    a health or severity signal. It is plain text, so a screen reader announces
    it like any other label rather than depending on colour.
    """
    if not label:
        return ""
    return f'<span class="lp-badge-technical">{_escape(label)}</span>'


def appbar_html(
    *,
    nav_items: list[NavItem],
    user_label: str,
    home_href: str = "/user",
    active_exports: int = 0,
    technical_badge: str = "",
    theme_mode: str = "auto",
    theme_durable: bool = False,
    theme_action: str = "",
    theme_next: str = "",
    theme_unavailable: bool = False,
) -> str:
    current_module_label = next((item.label for item in nav_items if item.active), "")
    return (
        '<header class="lp-appbar">'
        f'<a class="lp-brand" href="{_escape(home_href, quote=True)}" '
        f'aria-label="{_escape(t("shell.brand.home_label"), quote=True)}">'
        '<span class="lp-brand-mark" aria-hidden="true"></span>'
        f'<span class="lp-brand-word">{_escape(t("shell.brand.name"))}</span></a>'
        f'<button class="lp-nav-toggle" type="button" data-nav-toggle aria-expanded="false" '
        f'aria-controls="{NAV_DRAWER_ID}" '
        f'aria-label="{_escape(t("shell.nav.open"), quote=True)}">&#9776;</button>'
        # RESPONSIVE_SPEC "Shell": in the 1024-1279 band the collapsed nav is the
        # menu button PLUS the active module name, so the user never loses the
        # answer to "where am I". It is a span, not a second navigation control:
        # `aria-current` on the real nav item stays the semantic carrier, and CSS
        # removes this element outright outside that band so it is never a
        # hidden-but-present duplicate.
        + (
            f'<span class="lp-nav-current" data-nav-current>{_escape(current_module_label)}</span>'
            if current_module_label
            else ""
        )
        + f'<nav class="lp-nav" aria-label="{_escape(t("shell.nav.aria_label"), quote=True)}">'
        f"{_nav_html(nav_items)}</nav>"
        '<div class="lp-appbar-end">'
        f"{technical_badge_html(technical_badge)}"
        f"{export_indicator_html(active_exports)}"
        f"{_theme_switcher_html(mode=theme_mode, durable=theme_durable, action=theme_action, next_path=theme_next, unavailable=theme_unavailable)}"
        f'<div class="lp-account" aria-label="{_escape(t("shell.account.aria_label"), quote=True)}">'
        f'<span class="lp-account-name">{_escape(user_label)}</span>'
        f'<span class="lp-avatar" aria-hidden="true">{_escape(account_initials(user_label))}</span>'
        "</div>"
        f'<a class="lp-logout" href="/logout">{_escape(t("shell.account.logout"))}</a>'
        "</div>"
        "</header>"
    )


def context_bar_html(
    *,
    module_name: str,
    client_name: str = "",
    client_code: str = "",
    meta_html: str = "",
    actions_html: str = "",
    surface: str = "",
) -> str:
    """The 64 px client context bar.

    When a client is in scope its name and code lead the bar and the module or
    dataset name follows; the client is never hidden at any width. Surfaces
    that are not scoped to a single client (administration, artifact triage)
    lead with the module identity instead of inventing a client.
    """
    surface_attr = f' data-surface="{_escape(surface, quote=True)}"' if surface else ""
    # The region is only "client context" when a client is actually in scope.
    aria_key = "shell.context.aria_label" if client_name else "shell.context.aria_label_module"
    if client_name:
        identity = (
            f'<span class="lp-client-name">{_escape(client_name)}</span>'
            + (f'<span class="lp-client-code">{_escape(client_code)}</span>' if client_code else "")
            + '<span class="lp-context-sep" aria-hidden="true">&#183;</span>'
            + f'<span class="lp-module-name">{_escape(module_name)}</span>'
        )
    else:
        identity = f'<span class="lp-client-name">{_escape(module_name)}</span>'
    meta = f'<div class="lp-context-meta">{meta_html}</div>' if meta_html else ""
    actions = f'<div class="lp-context-actions">{actions_html}</div>' if actions_html else ""
    return (
        f'<div class="lp-context"{surface_attr} '
        f'aria-label="{_escape(t(aria_key), quote=True)}">'
        '<span class="lp-context-rule" aria-hidden="true"></span>'
        f'<div class="lp-context-identity"><div class="lp-context-line">{identity}</div>{meta}</div>'
        f"{actions}"
        "</div>"
    )


def section_bar_html(items: list[NavItem]) -> str:
    if not items:
        return ""
    links = "".join(
        f'<a class="lp-subnav-item" href="{_escape(item.href, quote=True)}"'
        f'{_aria_current(item.active)}>{_escape(item.label)}</a>'
        for item in items
    )
    return (
        f'<nav class="lp-subnav" aria-label="{_escape(t("shell.subnav.aria_label"), quote=True)}">'
        f"{links}</nav>"
    )


def page_path_html(module_label: str, page_title: str) -> str:
    """`Dane / Wszystkie zbiory` — the in-page path shown above a page title.

    This is not the dedicated detail-page breadcrumb bar from
    ``PRODUCT_BEHAVIOR_CONTRACT`` §1.6; that bar belongs to the detail-screen
    stages and is not implemented here.
    """
    return (
        '<nav class="lp-page-path" aria-label="'
        + _escape(module_label, quote=True)
        + '">'
        f"<span>{_escape(module_label)}</span>"
        '<span class="lp-page-path-sep" aria-hidden="true">/</span>'
        f'<span class="lp-page-path-current">{_escape(page_title)}</span>'
        "</nav>"
    )
