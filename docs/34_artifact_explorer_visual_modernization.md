# 34 — Artifact Explorer: visual modernization onto the shared platform shell

Durable reference for the approved **`ART-001`** treatment of Artifact Explorer:
shared-shell and shared-token adoption, the `TRYB OPERATORA` identity, the
neutral context rule, the artifact-kind rail and its authorization-scoped
counts, and the boundaries this slice deliberately did not cross.

This is the eleventh implementation slice of the approved redesign (stage `S11`),
built on the shared shell in `docs/22_portal_ui_foundation_and_shared_shell.md`
and the responsive/accessibility completion in
`docs/33_database_explorer_responsive_and_accessibility.md`.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— `SCREEN_CATALOG.md` `ART-001`, `PRODUCT_BEHAVIOR_CONTRACT.md` §5,
`SCREEN_STATE_MATRIX.md` `ART-001`, `COMPONENT_CATALOG.md` (Operator-mode badge,
Technical badge, Rail), `COPY_AND_TERMINOLOGY.md` §8, `ASSET_MANIFEST.md` §2,
and criteria `AR-1`–`AR-5` in `IMPLEMENTATION_ACCEPTANCE_CRITERIA.md`.

**No schema change.** `S11` adds no migration, no table, no column and no
persisted preference. **No product-model change**: no report model, no artifact
lifecycle state, no ownership change, no deletion, no saved views, no global
search, and no new API surface.

> **Superseded in one respect, 2026-09-08.** Two read-only export routes now
> exist on this surface (`GET /artifact-explorer/export` and
> `.../folders/{folder_id}/export`), by owner decision recorded in
> `docs/45_analytics_and_artifact_table_exports.md` §5. Everything else this
> document states about `S11` still holds — including the asset boundary below:
> the export is plain links, so Database Explorer's grid layer still never
> loads here.

---

## 1. What Artifact Explorer is — and is not

Artifact Explorer is a **technical operator triage surface**. It is deliberately
outside the product's three client-data access modes (`Raporty` · `Dane` ·
`Analizy`), and adopting the shared visual system does not move it into them.

| | |
|---|---|
| Primary nav group | `Narzędzia` (tools), rendered one step quieter than the work group |
| Nav visibility | administrators only — unchanged by `S11` |
| Product taxonomy | **not** a data-access mode (`PRODUCT_BEHAVIOR_CONTRACT` §1.2, §5) |
| Artifact content | immutable and read-only; every screen action is read or inspect |

The screen says this in its own copy rather than relying on the reader to infer
it from placement: the context bar subtitle is the approved sentence
*inspekcja artefaktów systemu · nie jest trybem dostępu do danych klienta*
(`AR-2`), and the rail footer states
*Widok wymaga uprawnienia operatora. Artefakty są niezmienne i tylko do odczytu.*

## 2. Shared shell adoption

`_artifact_explorer_layout` in `api/main.py` is now a thin adapter over the
shared `_portal_layout`. Every browser-facing Artifact Explorer surface goes
through it — the root list, artifact detail, the virtual-folders root, folder
detail and the module's permission state — so none of them can drift into a
visually disconnected legacy shell.

What the module no longer carries:

* **its own inline stylesheet.** The former ~160-line `ARTIFACT_EXPLORER_COMPONENT_CSS`
  block is retired. It hard-coded a dark surface/field/header-cell/text palette
  that stayed dark under the shared light theme;
* **its own inline `<script>`.** The combo-filter and copy-to-clipboard
  enhancements moved unchanged into a versioned page-scoped asset;
* **its own header navigation.** `All artifacts` / `Virtual folders` duplicated
  the shared section bar (`Wszystkie artefakty` / `Foldery wirtualne`). The
  context-bar action is now the approved `Odśwież katalog`, which links back to
  the current list URL and preserves the active filters.

New assets, registered in `api/portal_ui/assets.py` as page-scoped (never loaded
by another module, and never pulling the Database Explorer grid layer onto an
Artifacts page):

| Asset | Purpose |
|---|---|
| `api/static/css/artifact-explorer.css` | rail, `ART-001` table, module components — token-only |
| `api/static/js/artifact-explorer.js` | combo filters and copy cells, progressive enhancement only |

The stylesheet was **rewritten** onto the approved `--lp-*` tokens rather than
moved; it defines no literal colour and no second theme palette, which is what
makes the module follow AUTO / light / dark like the rest of the portal.

### Section navigation

`artifact-folders` now maps to the `Artefakty` primary module in
`ACTIVE_KEY_TO_PRIMARY`. The section entry existed but no page could select it,
so the folder surfaces used to mark `Wszystkie artefakty` as current. Fixing the
mapping is presentation only; section navigation for this module remains
administrator-gated exactly as before.

## 3. `TRYB OPERATORA` and the neutral context rule (`AR-1`)

Two shell-level markers say "technical surface", and neither is authorization.

**Operator-mode badge.** `technical_badge_html` renders the approved neutral
bordered mono chip in the app bar, fed by `appbar_html(technical_badge=…)`. It is
plain text with an accessible name by construction, and it deliberately uses the
neutral technical-badge treatment rather than a semantic status colour: operator
mode is context, not health or severity. Only the Artifacts module passes it;
ordinary portal pages render no badge.

**Neutral context rule.** `context_bar_html(surface="operator")` swaps the 4 px
accent rule for the approved neutral `#3e4650` (`ASSET_MANIFEST` §2). The rule
already existed in `portal.css`; `S11` is what uses it.

**The shell does not invent a client.** Artifact Explorer passes no client name
and no client code, so the context bar leads with the module identity and the
approved subtitle. There is no fabricated customer account, dataset or reporting
period anywhere in the Artifacts chrome. The context region is also labelled
`Kontekst modułu` rather than `Kontekst klienta` when no client is in scope —
the shell may not claim a business scope the page does not have.

**Neither marker touches authorization.** `TRYB OPERATORA` is rendered for every
account that can reach the module, including a non-administrator with scoped
artifact grants, and it grants that account nothing: the primary navigation, the
section navigation and every route keep the permission checks they already had.

## 4. The artifact-kind rail (`AR-3`)

### The dimension

The rail's "kind" is the persisted **`artifacts.kind`** column — the same field
the existing `kind=` filter, the facet list and the artifact detail page already
use. It is deliberately *not* `artifact_role`, `report_type`, `workflow_name` or
`file_ext`. Those are distinct technical fields with distinct meanings, and
merging any of them into the rail would invent a dimension the product does not
have. The `ART-001` table column `Rodzaj` shows the same field.

### Counts and their authorization invariant

`_get_artifact_kind_counts(user, action="view")` issues **one** grouped,
parameterized, read-only aggregate:

```
SELECT kind, COUNT(*) AS total FROM artifacts
 WHERE kind IS NOT NULL AND BTRIM(kind) <> '' AND <permission predicate>
 GROUP BY kind ORDER BY total DESC, kind ASC LIMIT %s
```

The permission predicate is **exactly** the clause and parameters that
`build_artifact_permission_where_clause(user, "view")` produces for the artifact
list. This is the load-bearing security property of this slice:

> A count badge is an information-disclosure surface. The count for a kind means
> the same authorized universe the account can actually browse — never the
> global platform volume.

Consequences, all asserted deterministically:

* a kind whose every artifact is invisible to the account **does not appear at
  all**, so the rail cannot reveal that such artifacts exist;
* the rendered counts sum to the account's authorized universe, not to the store;
* an administrator sees every kind through the same code path — no special case.

The counts describe the account's whole authorized universe and therefore do not
move with the other filters. That is what makes the rail a navigation aid rather
than a probe: a user cannot narrow the list and read differences off the rail.
`LIMIT` is a bounded safety valve on an enum-like column, not a paging model.

There is **no** `Wszystkie` entry in the rail. The approved `ART-001` rail has
none, and the section bar's `Wszystkie artefakty` (`/artifact-explorer`) is
already the single entry point to the complete authorized universe; a second one
would be the duplicate navigation concept the approved hierarchy removes.

### Active state and filter composition

A rail entry is current when — and only when — the request carries exactly one
exact `kind=` value. Two kinds, or a typed `kind_search=` substring, are
legitimate filter-form states that no single rail entry represents, and marking
one anyway would tell the user the list is narrower than it is.

The active state is carried by `aria-current="true"` first; the tint, the weight
step and the 2 px accent mark are its visual echoes, so it is never colour alone.

Rail links are ordinary server-authoritative URLs:

* every unrelated filter, the sort and the page size ride along untouched;
* `offset` resets to `0` — a new selection has no page 2 yet;
* `kind_search` is dropped, because the rail and the typed substring are two
  expressions of the same dimension and the exact selection already overrides
  the substring. One filtering grammar, one visible truth;
* an unknown or forged kind activates nothing and travels as an ordinary
  parameterized filter value.

## 5. The `ART-001` table

The approved column set leads, in the approved order, with the approved Polish
names: `Utworzono` · `Rodzaj` · `Nazwa` · `Klient` · `Hash` · `Rozmiar` ·
`Stan`, and the row action `Inspekcja`.

The module's remaining technical columns follow, under the internal names they
already carried — `Workflow`, `Stage`, `Role`, `Report type`, `Tags`,
`Description`, `Original filename`, `Extension`, `Layout`, `Run`, `Raw file`.
Two rules produce that shape:

1. Artifact Explorer is a technical operator tool. Dropping lineage or workflow
   metadata to match a screenshot would be a capability reduction, and `S11` is
   a presentation slice, not a redaction project.
2. Fields the approved design does not name keep their existing names. Renaming
   them would be invented terminology and a parallel vocabulary.

`Hash` (`sha256`) is new to the list. It is not new exposure: the same
view-authorized account already reads it on the artifact detail page, and the
list is scoped by the identical predicate.

### `Stan`

Derived from the artifact record the account already reads:

| Value | Condition |
|---|---|
| `USUNIĘTY` | `expired_at` set, or `expires_at` has passed — retention/cleanup removed the object |
| `ZWERYFIKOWANY` | otherwise |

**Recorded deviation.** The approved vocabulary also names `W TRAKCIE`. No
persisted artifact field represents an in-progress artifact — every row is
written after the object and its digest exist — so that value is not emitted.
Inventing a source for it would be an artifact lifecycle state, which this stage
must not add. This is a data-model gap to resolve in design, not in `S11`.

### Sort

Sorting is unchanged: the same whitelist, the same toggle links, the same hidden
`sort` field so a bookmarked value round-trips. `S11` adds the approved
`aria-sort` semantics (`ascending` / `descending` / `none`, exactly one header
non-`none`) and adds **no new sort capability** — `Rodzaj`, `Hash` and `Stan`
are presentation-only columns because the artifact list never offered a sort on
them.

### Search

The approved `ART-001` search placeholder is *Nazwa, hash lub referencja*.

**Recorded behavioural change.** `sha256` was added to
`ARTIFACT_BROWSER_SEARCH_COLUMNS` so the approved control does what its label
says. This widens what the free-text search matches; it widens nothing about who
may see a row, because the same permission predicate is ANDed onto every
artifact query. Smart folders that persist a `search` term match hashes too,
consistently with the list.

## 6. Page hierarchy

One coherent hierarchy per page:

* **root list** — the context bar carries `Artefakty` and the approved subtitle,
  so the in-page path and title are suppressed (`lp-work-flush`). The document
  still has exactly one `h1`, visually hidden, so the heading structure is
  correct for a screen reader without pushing the rail and table down;
* **detail, folders root, folder detail** — keep a visible `h1` and the in-page
  path, because those pages name a specific artifact or folder that the shell
  context bar does not.

The module's own folder breadcrumb is unchanged and remains the *local* artifact
hierarchy; the shell context bar is the *global* module identity. They are two
different things and neither was deleted in favour of the other.

## 7. Responsive behaviour

`S11` is not a second `S10`. It inherits the shared responsive shell — the
collapsed navigation, the drawer, the account/sign-out relocation, the theme
control — and adds only what `ART-001` needs inside it:

* below 1280 px the rail moves above the table as a wrapping row rather than
  competing with it for width;
* the operator table **stays a table** at every width and scrolls horizontally.
  An operator compares hashes and sizes across rows; turning that into cards
  would destroy the comparison. No artifact table becomes a card list.

## 8. Authorization: what did not change

`S11` restyles and relocates controls. It broadens and narrows nothing.

| Capability | Gate | Status |
|---|---|---|
| list / view | `build_artifact_permission_where_clause(user, "view")` | unchanged |
| artifact detail | `user_can_access_artifact(user, artifact, "view")` | unchanged |
| preview | `…"preview"`, plus expiry | unchanged |
| download | `…"download"`, plus expiry, ownership for background exports | unchanged |
| annotations / tags | `…"edit_annotations"` | unchanged |
| virtual-folder administration | `user["is_admin"]` | unchanged |
| `Artefakty` nav item and section bar | `user["is_admin"]` | unchanged |
| kind rail and counts | `…"view"`, identical to the list | new surface, existing gate |

A control the account may not use is **not rendered at all**, so nothing became
hidden-but-focusable. No route lost its check, and no UI state replaced a
backend check.

## 9. Verification

`ops/tests_manual/test_artifact_explorer_visual_modernization_s11.py` renders
real Artifact Explorer responses — root list, artifact detail, folders root,
folder detail and the permission state — and asserts the contract on the
rendered HTML rather than on helper source. It covers shared-shell and
shared-asset adoption, absence of the retired inline stylesheet and its literal
colours, the operator badge and its non-spread onto other modules, the neutral
context rule, the kind dimension and counts, the count/RBAC invariant against a
store holding artifacts the account may not view, rail URL round-trip and
composition with existing filters, forged kinds, filtering/sort/`aria-sort`/
pagination/copy preservation, the approved column and state contract,
permission-gated controls, the absence of any new mutation affordance, and the
token-only stylesheet.

Three existing suites were updated where `S11` superseded an assertion, not
weakened one: `test_portal_artifact_shell_phase1.py` and
`test_artifact_explorer_phase4.py` now assert the versioned module assets and
check the progressive-enhancement behaviour on the asset file, and
`test_artifact_explorer_phase4.py` / `test_artifact_explorer_phase6_rbac.py`
stub the new RBAC-scoped kind aggregate the list route calls.

Verification is browserless: rendered HTML, CSS token analysis and request-level
tests. No production artifact, tag, folder, permission or object was read or
mutated to produce it.
