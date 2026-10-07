# Asset Manifest

Implementation-relevant visual assets. The approved design is deliberately **asset-light**: it contains no logos beyond a geometric brand mark, no illustrations, and no bitmap imagery. This is a property of the design, not an omission.

---

## 1. Fonts

| Asset | Source | Purpose | Screens | Notes |
|---|---|---|---|---|
| IBM Plex Sans | Google Fonts — weights 400, 500, 600 | All prose, labels, controls, headings | all | Open source (SIL OFL 1.1). Self-host in production; do not depend on a third-party CDN. Only three weights are used. |
| IBM Plex Mono | Google Fonts — weights 400, 500, 600 | Identifiers, numbers, timestamps, hashes, physical column names, eyebrows | all | Same licence. Required — the digit alignment is what makes column scanning work. |

Fallback stacks are specified in `DESIGN_TOKENS.md` §2 and **MUST** be implemented; the layout must survive a font-load failure without reflowing row heights.

## 2. Brand mark

| Asset | File | Purpose | Screens | Notes |
|---|---|---|---|---|
| Log Platform mark | *none — CSS only* | Brand identifier in the app bar | all | A **6 × 18 px rounded rectangle** in `accent/base` with `radius/xs`, followed by the wordmark "Log Platform" in `type/nav` weight 600. No image file. Reproduce in CSS, not as an SVG or PNG. |
| Context accent rule | *none — CSS only* | Marks the client context bar | all modules | A **4 × 26 px rounded rectangle** in `accent/base`. `ART-001` uses a neutral `#3e4650` instead, to mark a technical surface. |

No logo file exists or is required. If the owner later supplies a real corporate logo, it replaces the wordmark only; the accent bar is structural.

## 3. Icons

The design uses **Unicode glyphs rendered in the text font**, not an icon set. This is deliberate: at 9–14 px, a text glyph in IBM Plex is sharper than a scaled SVG, and there is no icon-font payload to load.

| Glyph | Codepoint | Purpose | Screens |
|---|---|---|---|
| `⌕` | U+2315 | Search | all search fields |
| `▾` | U+25BE | Menu / dropdown affordance | column headers, selectors |
| `↑` `↓` | U+2191 U+2193 | Sort direction; row traversal | table headers, `DB-006` |
| `‹` `›` | U+2039 U+203A | Previous / next | pagination, month stepper, siblings, preview pages |
| `×` | U+00D7 | Remove / close | filter chips, panels |
| `≡` | U+2261 | Filtered-column marker | table headers |
| `⠿` | U+283F | Drag handle | `DB-008` |
| `⧉` | U+29C9 | Copy value | `DB-006` field rows |
| `⋯` | U+22EF | Overflow menu | responsive toolbars |
| `☰` | U+2630 | Collapsed navigation | `RSP-001`, `RSP-002` |
| `☀` `☾` | U+2600 U+263E | Light / dark theme override | `SHL-003` |
| `Σ` | U+03A3 | Sum-mode marker | `ECO-001`–`ECO-003` |
| `Δ` | U+0394 | Change in position | `ECO-001` |
| `⇤` | U+21E4 | Collapse panel | filter panel |
| `⌘K` | — | Keyboard hint | app-bar search |
| `↵` | U+21B5 | Enter-key hint | `DB-005` |

**If the implementation replaces these with an icon set**, it must match the optical weight at these sizes and keep the accessible names in `ACCESSIBILITY_SPEC.md` §7. Swapping in a heavier icon family will visibly coarsen the design.

## 4. Status dots and rules

| Asset | Purpose | Screens | Notes |
|---|---|---|---|
| Attention dot | Latest instance of a report type failed | `REP-001` rail | 6 px circle, `state/warning-edge`. CSS only. |
| Export activity dot | A background export is running | `DB-007`, app bar | 7 px circle, `accent/base`. CSS only. |
| Status left-edge | Report instance status | `REP-001` | 3 px vertical rule in the status colour. CSS only. |

## 5. Data-visualization graphics

All charts are **CSS/DOM primitives** — flex rows of coloured rectangles. There are no chart images, no canvas, and no charting-library assets.

| Graphic | Construction | Screens | Notes |
|---|---|---|---|
| Fleet score distribution | 24 flex children, height proportional to bucket count, `radius/xs` | `ECO-001`, `ECO-002`, `ECO-003` | Median and mean stated numerically alongside |
| Score bar | Track + proportional fill | `ECO-001` | Value always shown as a number too |
| Trend bars | 8 flex children with value above and period label below | `ECO-003` | Only real periods render |
| Component share bar | Track + proportional fill + percentage | `ECO-003`, `DB-005` | |
| Value distribution histogram | 14 flex children, threshold bucket in accent | `DB-005` numeric | min/max labelled |
| Distinct-value bars | 44 px track + proportional fill | `DB-005` text | Count shown numerically |
| Export progress bar | Track + fill + percentage | `DB-007` | The one animated element |

**No SVG chart libraries are required.** If the implementation introduces one, the acceptance criteria in `IMPLEMENTATION_ACCEPTANCE_CRITERIA.md` §4 and §8 still apply, including "colour is always accompanied by a number".

## 6. Placeholder graphics

| Asset | Purpose | Screens | Notes |
|---|---|---|---|
| Document preview placeholder | Stands in for the rendered report file | `REP-003` | A 520 px striped CSS gradient panel with a monospace caption. **This is a design placeholder, not a deliverable asset.** The implementation renders the real PDF/spreadsheet in this slot. |

## 7. Reference exports (this package)

| Asset | File | Purpose | Notes |
|---|---|---|---|
| Screen references | `reference/screens/<SCREEN-ID>.png` | Visual comparison target per Screen ID | Captured at the authored frame size in the theme the ID denotes |
| Interactive references | `reference/html/*.html` | Self-contained interactive walkthrough | `REFERENCE_PROTOTYPE_ONLY` |
| Prototype source | `reference/prototype-source/*.dc.html` | Preserves layout intent, proportions, composition, interaction examples | `REFERENCE_PROTOTYPE_ONLY` — **must not be copied into production** |

## 8. Deliberately absent

| Not included | Reason |
|---|---|
| Corporate logo file | None supplied; the brand mark is CSS-only |
| Illustrations, empty-state artwork | Empty states are typographic by design — an illustration would soften an operational tool |
| Photography, background imagery | None in the approved design |
| Icon font or SVG sprite | Unicode glyphs in the text font (§3) |
| Favicon / app icons | Not part of the approved scope; carry over whatever the repository already ships |
| Loading spinner graphic | Loading uses skeleton rows, never a spinner |
| Chart library assets | All charts are CSS primitives (§5) |
