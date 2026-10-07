# 22 — Portal UI foundation and shared application shell

Durable reference for the portal's frontend foundation: static assets, design
tokens, typography, the theme engine, the shared application shell, the
translation-key mechanism and the shell accessibility contract.

This is the first implementation slice of the approved Log Platform redesign. It
establishes the foundation the later Database Explorer, Report Explorer, Eco
Driving, Artifact Explorer and responsive/accessibility stages build on. The
detailed Database Explorer grid redesign is **not** part of it.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— screens `SHL-001`–`SHL-003`, criteria `SH-1`–`SH-10`.

---

## 1. Architecture decision: no frontend framework

The portal is server-rendered FastAPI with Python-generated markup. The approved
design needs a token layer, a stylesheet, self-hosted fonts and two small
progressive-enhancement scripts — nothing that requires React, Vue, Svelte or a
build pipeline. None was introduced.

Consequences worth preserving:

- Pages render fully without JavaScript. The only script-dependent affordances
  are the theme override and the collapsed navigation drawer.
- The theme switcher is served `hidden` and revealed by `theme.js`. Without
  scripting it could not act, and AUTO already follows the OS, so an inert
  control would be worse than none.
- There is no client-side router and no client-side state store. View state
  stays in the URL, which is what makes it linkable and Back-navigable.

## 2. Static assets

Assets live in `api/static` and are served by the application itself, mounted at
`/static` by `_mount_portal_static_assets()` in `api/main.py`.

They live under `api/` because the API image is built with `build.context:
./api` (`docker-compose.yml`); anything outside that directory would not reach
the container. Serving them from the app rather than nginx means **a deploy
needs no production web-server change**.

```
api/static/
  css/fonts.css      @font-face declarations for the self-hosted families
  css/tokens.css     approved design tokens + legacy --portal-* compatibility
  css/portal.css     shared shell and base component layer
  css/data-grid.css  Database Explorer data sheet (module-scoped)
  js/theme.js        AUTO / light / dark engine (render-blocking, by design)
  js/shell.js        collapsed navigation drawer (deferred)
  js/data-grid.js    Database Explorer sheet behaviour (module-scoped, deferred)
  fonts/*.woff2      IBM Plex Sans + Mono, 400/500/600
  fonts/LICENSE-IBM-Plex.txt
```

`api/portal_ui/assets.py` owns the URL and cache contract:

- `ASSET_VERSION` is a short digest **over the served CSS/JS content**, so a
  redeploy invalidates browser caches without anyone remembering to bump a
  number, and stays stable for a given checkout so rendered HTML is
  test-comparable.
- `asset_url()` appends `?v=<version>`; `head_asset_tags()` emits the document
  head block.
- `theme.js` is deliberately **not** deferred: it applies the stored theme to
  `<html>` before first paint, which is what prevents a light/dark flash.
- **Shared vs module-scoped.** `STYLESHEETS`/`SCRIPTS` load on every page;
  `PAGE_ASSETS` load only where a page asks for them, via
  `_portal_layout(extra_assets=…)` → `page_asset_tags()`. So the Database
  Explorer's grid layer is not downloaded by every administration screen. All of
  them feed `ASSET_VERSION`, so a change to a module stylesheet still
  invalidates caches. `page_asset_tags()` raises on an unknown path rather than
  emitting a dead tag, so a typo fails at render time instead of shipping an
  unstyled page.
- A module's CSS and JS belong here, not in a Python string. New inline
  `<style>`/`<script>` blocks in `api/main.py` are a regression.
- `mimetypes.add_type("font/woff2", ".woff2")` is registered because Python's
  database does not always know woff2, and some browsers refuse a font served
  with the wrong content type.

## 3. Design tokens

`api/static/css/tokens.css` implements the approved token contract: surfaces,
borders, text hierarchy, accent, action, semantic state, data-visualization
colour, typography, spacing (2 px scale), radii, elevation, component heights
and fixed panel widths.

Two rules keep the system honest:

- **Every dimension token is declared once, in `:root`.** No height, width,
  radius or spacing token may be redefined inside a theme block. Light and dark
  are the same layout with a different colour layer (`SH-10`).
- **The two dark blocks must be edited together.** `@media (prefers-color-scheme:
  dark)` (guarded by `:root:not([data-theme="light"])`) and
  `:root[data-theme="dark"]` carry identical declarations; the first serves
  AUTO, the second serves the explicit override.

### Legacy `--portal-*` compatibility layer

Module CSS written before the redesign (Database Explorer, Eco Driving,
Artifacts) references `--portal-bg`, `--portal-accent` and friends. Those names
survive in `tokens.css` as **aliases onto the approved tokens**, so pre-redesign
surfaces inherit the approved colour layer in both themes without being
rewritten in this slice.

Do not introduce new uses. Each module's own redesign stage should migrate its
CSS to the `--lp-*` tokens and drop its aliases.

## 4. Typography

IBM Plex Sans and IBM Plex Mono, self-hosted, three weights each (400/500/600 —
the approved design uses nothing heavier). Licensed under SIL OFL 1.1; the
licence ships beside the fonts.

**There is no production dependency on Google Fonts or any other third-party
font CDN**, and none may be added: the portal must render correctly on a host
with no outbound internet access. Only fonts the application actually uses
belong in the repository.

## 5. Theme engine (`SHL-003`)

Three modes, exposed as an `AUTO · ☀ · ☾` segmented control in the app bar.

| Mode | `<html>` state | Resolution |
|---|---|---|
| AUTO | no `data-theme` attribute | `prefers-color-scheme`, live |
| Light | `data-theme="light"` | explicit, beats a dark OS setting |
| Dark | `data-theme="dark"` | explicit, beats a light OS setting |

AUTO being the *absence* of the attribute is the load-bearing detail: the media
query keeps applying, so changing the OS colour scheme re-themes the page with
no reload and no listener.

An explicit choice sets the attribute and persists to `localStorage` under
`logplatform.theme`. Switching **never navigates, never submits and never
re-renders the document**, so the route, the query string, every URL-held filter
and the scroll position are preserved by construction. A blocked or unavailable
store degrades to AUTO for that document rather than breaking the page.

> **Persistence scope.** Preference is browser-local only. The approved design
> also calls for per-account, server-side persistence (`SH-8`), which needs a
> schema change and is deferred to a separate, separately authorized stage. It
> must not be slipped in via an unauthorized migration.

## 6. Shared application shell

`api/portal_ui/shell.py` builds the markup; `_portal_layout()` in `api/main.py`
assembles the document. The pre-redesign 268 px sidebar and the 1280 px content
cap are gone.

Frame, top to bottom:

| Band | Height | Contents |
|---|---|---|
| App bar | 56 px | brand, horizontal primary nav, theme switcher, account, sign out |
| Client context bar | 64 px | accent rule, client name + code, module/page name, metadata, page actions |
| Section bar | optional | secondary nav for modules that group several routes |
| Working area | remaining | full viewport width, no content cap |

### Primary navigation

Five entries in fixed order: **Raporty · Dane · Analizy · Artefakty ·
Administracja**.

`Raporty`, `Dane` and `Analizy` are the three data-access modes. `Artefakty` and
`Administracja` are the tools group and render one visible step quieter
(`data-nav-group="tools"`), because **Artefakty is an operator surface and must
not read as a fourth data-access mode**.

### Navigation visibility is not authorization

`primary_nav_items()` mirrors the pre-redesign sidebar exactly, so the redesign
grants nobody a new entry point:

| Entry | Visible to |
|---|---|
| `Raporty`, `Dane` | every authenticated account |
| `Analizy` | only with effective Eco Driving ranking access — **administrators do not bypass the client grant** |
| `Artefakty` | administrators only |
| `Administracja` | administrators only |

`shell.py` holds **no** authorization logic. It renders the navigation model it
is handed; `_platform_primary_nav_items()` derives that model from the same
server-side rules that gate the routes. Hiding a navigation item is
presentation and never substitutes for a route's own check.

## 7. Translation keys

`api/portal_ui/i18n.py` is the whole mechanism: a frozen catalogue plus `t()`.

Per decision `D-010` every user-facing string routes through a key even though
only Polish ships. It is deliberately **not** a localisation framework — no
plural rules, no runtime catalogue loading, no extraction tooling — because the
product ships one locale and machinery for a hypothetical second one would be
speculative.

- Key convention: `<module>.<surface>.<element>`, e.g. `shell.nav.reports`.
- An unknown key returns the key itself, so a missing translation degrades to a
  visible, greppable marker instead of breaking a render. A test asserts that no
  key the shell uses is missing, so this never reaches production silently.
- Scope of this slice is the shared shell. Module surfaces migrate their own
  terminology in their own stages; the standing rule is only that **new**
  shared-UI strings arrive as keys, not as literals.
- Polish vocabulary is taken verbatim from the approved
  `COPY_AND_TERMINOLOGY.md`. Do not invent alternatives.

## 8. Accessibility contract

Implemented for the shared shell. **No formal WCAG audit has been performed and
no conformance level is claimed.**

- Semantic `<nav>` landmarks with accessible names; `<main>` carries the working
  area and a skip link targets it.
- Active navigation state carries three independent signals: `aria-current="page"`,
  a 2 px accent underline and a weight step — never colour alone.
- Focus is visible everywhere (2 px accent outline, 2 px offset) and is never
  removed or replaced by hover.
- Every shell control is a real `<button>` or `<a>`. No div-with-onclick, and no
  custom control introduced for visual fidelity alone.
- Theme segments are buttons with accessible names and `aria-pressed` state.
- The collapsed nav drawer is a labelled `role="dialog"`, traps Tab, closes on
  `Escape` and returns focus to the menu button.
- At ≤1023 px the app bar drops the account name and the sign-out link, so the
  drawer carries both — otherwise signing out would become unreachable.
- `prefers-reduced-motion: reduce` suppresses transitions and animations.

## 9. Responsive scope

Desktop is primary. The shell degrades coherently: at ≤1279 px the horizontal
nav collapses into the drawer; at ≤1023 px the context bar wraps and touch
targets grow to 44 px; at ≤767 px the brand shrinks to its mark. **The client
name and code never disappear at any width** (`SH-6`).

Full `RSP-001`–`RSP-003` completion, including narrow-width Database Explorer
behaviour, belongs to the later responsive stage.

## 10. Tests

`ops/tests_manual/test_portal_ui_foundation_and_shared_shell.py` covers the
asset foundation, token contract in both themes, self-hosted typography, the
theme engine, shell structure, the full-width workspace, active-state
semantics, the navigation authorization matrix, the translation-key foundation,
the accessibility contract and markup escaping.

```bash
cd /opt/log-platform
env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_ui_foundation_and_shared_shell.py
```

Shell-adjacent suites that also assert the approved contract:
`test_portal_dark_theme_ui.py`, `test_platform_shell_unification_phase3a.py`,
`test_portal_artifact_shell_phase1.py`, `test_portal_shell_phase1.py`.

Tests that previously encoded the superseded sidebar, the dark-only palette or
English shell labels were updated to the approved contract. Assertions
protecting routing, authentication, authorization, permission-gated navigation
and business behaviour were left intact.
