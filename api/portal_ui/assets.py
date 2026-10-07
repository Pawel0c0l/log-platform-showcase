"""Static asset foundation for the portal UI.

The portal is server-rendered FastAPI with no build pipeline, and the approved
design does not require one: it is a token layer, a stylesheet and two small
progressive-enhancement scripts. So the "foundation" is exactly that — a
repository-native ``api/static`` tree served by the application itself, with a
content-derived version query so a redeploy invalidates browser caches without
anyone remembering to bump a number.

Assets live under ``api/static`` because the API image is built from the ``api``
directory (``docker-compose.yml`` -> ``build.context: ./api``), so anything
outside it would not reach the container.
"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_URL_PREFIX = "/static"

# Stylesheets and scripts the shared shell needs, in load order. Fonts are
# referenced from fonts.css by relative URL and therefore need no entry here.
STYLESHEETS = ("css/fonts.css", "css/tokens.css", "css/portal.css")
SCRIPTS = ("js/theme.js", "js/shell.js")

# Assets a single module asks for, loaded only on the pages that need them.
# They still take part in ASSET_VERSION so a change to a module stylesheet
# invalidates caches exactly like a change to a shared one.
PAGE_ASSETS = (
    "css/data-grid.css",
    # The selection presentation layer, shared by every grid that ships
    # `js/data-grid-selection.js`. It must be listed AFTER the surface
    # stylesheet it accompanies: several of its rules tie on specificity with
    # the hover and sticky-column rules they have to beat and win on order.
    "css/grid-selection.css",
    "js/data-grid.js",
    "js/data-grid-filters.js",
    # Typed dates and the range calendar. After the filters module, whose
    # dismissal rules it relies on, and before the distribution module, which
    # may re-render inside the same menu.
    "js/data-grid-daterange.js",
    "js/data-grid-distribution.js",
    "js/data-grid-columns.js",
    "js/data-grid-row-detail.js",
    "js/data-grid-selection.js",
    "js/data-grid-export.js",
    "js/data-grid-states.js",
    "js/data-grid-responsive.js",
    "css/artifact-explorer.css",
    "js/artifact-explorer.js",
    "css/eco-driving.css",
    "css/report-explorer.css",
    "js/report-explorer.js",
)

# Python's mimetypes database does not always know woff2; an incorrect content
# type makes some browsers refuse the font. Registering it is idempotent.
mimetypes.add_type("font/woff2", ".woff2")


def _compute_asset_version() -> str:
    """Short digest over the served CSS/JS, so the cache key follows content.

    Deterministic for a given checkout, which keeps rendered HTML stable across
    test runs. Falls back to a fixed marker when the tree is unreadable rather
    than failing a page render over a cache hint.
    """
    digest = hashlib.sha256()
    for relative in (*STYLESHEETS, *SCRIPTS, *PAGE_ASSETS):
        path = STATIC_DIR / relative
        try:
            digest.update(path.read_bytes())
        except OSError:
            return "dev"
    return digest.hexdigest()[:10]


ASSET_VERSION = _compute_asset_version()


def asset_url(relative_path: str) -> str:
    """Cache-busting URL for one asset under ``api/static``."""
    return f"{STATIC_URL_PREFIX}/{relative_path.lstrip('/')}?v={ASSET_VERSION}"


def head_asset_tags() -> str:
    """`<link>`/`<script>` tags for the shared shell, for the document head.

    ``theme.js`` is intentionally render-blocking: it applies the stored theme
    to <html> before first paint, which is what prevents a light/dark flash.
    ``shell.js`` only wires the collapsed-nav drawer and is deferred.
    """
    tags = [f'<link rel="stylesheet" href="{asset_url(sheet)}">' for sheet in STYLESHEETS]
    tags.append(
        f'<link rel="preload" as="font" type="font/woff2" crossorigin '
        f'href="{STATIC_URL_PREFIX}/fonts/IBMPlexSans-Regular.woff2">'
    )
    tags.append(f'<script src="{asset_url("js/theme.js")}"></script>')
    tags.append(f'<script defer src="{asset_url("js/shell.js")}"></script>')
    return "\n  ".join(tags)


def page_asset_tags(*relative_paths: str) -> str:
    """`<link>`/`<script>` tags for module-scoped assets, for the document head.

    A page passes only what it needs, so the Database Explorer's grid layer is
    not downloaded by every administration screen. Scripts are deferred: nothing
    here may block first paint, and every module asset must be a progressive
    enhancement over markup that already works without it.

    Unknown paths raise rather than emitting a dead tag, so a typo fails at
    render time instead of silently shipping a page with no styling.
    """
    tags = []
    for relative in relative_paths:
        if relative not in PAGE_ASSETS:
            raise ValueError(f"unknown page asset: {relative}")
        if relative.endswith(".css"):
            tags.append(f'<link rel="stylesheet" href="{asset_url(relative)}">')
        else:
            tags.append(f'<script defer src="{asset_url(relative)}"></script>')
    return "\n  ".join(tags)
