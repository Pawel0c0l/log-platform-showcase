"""Portal UI foundation: static assets, design tokens, translation keys and the
approved shared application shell.

Presentation only. Nothing here reads the database, resolves a session or
decides access; the caller supplies an already-authorized navigation model.
"""
from __future__ import annotations

from . import assets, i18n, shell
from .assets import ASSET_VERSION, STATIC_DIR, STATIC_URL_PREFIX, asset_url, head_asset_tags
from .i18n import DEFAULT_LOCALE, t
from .shell import (
    NavItem,
    PRIMARY_ADMINISTRATION,
    PRIMARY_ANALYTICS,
    PRIMARY_ARTIFACTS,
    PRIMARY_DATA,
    PRIMARY_NAV_ORDER,
    PRIMARY_REPORTS,
    account_initials,
    appbar_html,
    context_bar_html,
    nav_drawer_html,
    page_path_html,
    primary_key_for,
    primary_label_for,
    primary_nav_items,
    section_bar_html,
    section_nav_items,
    technical_badge_html,
)

__all__ = [
    "ASSET_VERSION",
    "DEFAULT_LOCALE",
    "NavItem",
    "PRIMARY_ADMINISTRATION",
    "PRIMARY_ANALYTICS",
    "PRIMARY_ARTIFACTS",
    "PRIMARY_DATA",
    "PRIMARY_NAV_ORDER",
    "PRIMARY_REPORTS",
    "STATIC_DIR",
    "STATIC_URL_PREFIX",
    "account_initials",
    "appbar_html",
    "asset_url",
    "assets",
    "context_bar_html",
    "head_asset_tags",
    "nav_drawer_html",
    "i18n",
    "page_path_html",
    "primary_key_for",
    "primary_label_for",
    "primary_nav_items",
    "section_bar_html",
    "section_nav_items",
    "shell",
    "t",
    "technical_badge_html",
]
