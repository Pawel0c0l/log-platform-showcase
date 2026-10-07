# 29 — Database Explorer: hidden row identity and row detail

Durable reference for the separation between the **technical row identity** a
dataset is addressed by and the **user-visible business columns** a dataset is
made of, and for the row-detail drawer that identity makes linkable.

This is the sixth implementation slice of the approved redesign (stage `S6`),
built on the column management in
`docs/26_database_explorer_column_management.md`.

**Approved design reference (read-only, not tracked in this repository):**
`design-handoffs/log-platform/approved/v1.0/log-platform-approved-design-handoff`
— screen `DB-006`, `PRODUCT_BEHAVIOR_CONTRACT.md` §2.10,
`TABLE_AND_DATA_GRID_SPEC.md` §6, `INTERACTION_SPEC.md` §2.3/§3,
`ACCESSIBILITY_SPEC.md` §4, `COMPONENT_CATALOG.md`, criteria `DB-36`–`DB-39`
and `AC-4`.

---

## 1. The product decision

```
USER_VISIBLE_COLUMNS  !=  INTERNAL_ROW_IDENTITY
```

`record_id` is the physical column that identifies a row. It is **not** a
business field, and the owner decision is that ordinary portal users must never
see it. Once a column is configured as the technical row identifier it is absent
from every ordinary Database Explorer surface:

| Surface | Contains the identifier? |
|---|---|
| table headers, cells, `<colgroup>` | no |
| `cols`, `colorder`, `colw`, `colpin` | no |
| column-management panel (`DB-008`) and its `n/m` count | no |
| dataset catalogue `Visible columns: n` on `/user/database` | no |
| sort and filter controls, active-filter chips | no |
| global text search eligibility | no |
| S4 value distributions | no |
| row-detail drawer fields | no |
| user exports | no |

A forged URL cannot change any of that: the separation is enforced server-side,
never by hiding a rendered value with CSS.

## 2. How the separation is enforced

The whole boundary rests on one predicate, because every ordinary user surface
derives its column universe from one function.

```sql
-- _get_portal_database_visible_columns
WHERE dataset_id = %s AND is_visible IS TRUE AND is_row_identifier IS NOT TRUE
```

`is_row_identifier` **wins over** `is_visible`. That is what makes the
transition safe: the two BRAVO datasets still carry the legacy `is_visible =
true` on their `record_id`, and configuring it as the identifier removes it from
the user's universe in the same act, with no second migration and no window in
which the legacy flag leaks it.

The identity itself is loaded separately, as authorized dataset metadata:

```sql
-- _get_portal_database_row_identity_column
WHERE dataset_id = %s AND is_row_identifier IS TRUE  LIMIT 2
```

Exactly one configured identifier resolves. Zero is the ordinary "no linkable
row detail" state. Two or more is invalid configuration and resolves to none, so
the product never guesses which column is authoritative. A failure of this
lookup is caught and treated as "no identity" — it fails **closed**.

`_portal_database_resolve_row_identifier` no longer filters on `is_visible`;
that code-level invariant, not the schema, was what previously forced the
identifier to be exposed.

### 2.0 Every ordinary count uses the same universe

The exclusion is not only in the column loader. Every *ordinary user-facing*
notion of "visible columns" resolves the same universe — visible **and not** the
internal identity — because a count is a disclosure too: under the transitional
`is_visible = true` catalog state a plain count would report one more column
than the sheet can show, which reveals that a hidden technical column exists.

| Query | Audience | Excludes identity? |
|---|---|---|
| `_get_portal_database_visible_columns` | user | yes |
| `_list_effective_dataset_access_for_user` (`Visible columns: n` card) | user | yes |
| `_get_portal_database_dataset_for_user` | user | yes |
| `_list_portal_database_datasets`, `_get_portal_database_dataset` | admin | **no, deliberately** |

Admin and configuration surfaces still describe catalog metadata and may know
the identity exists; that is what the row-identifier control is for. The count
stays server-derived from trusted catalog metadata — it is never computed from
anything the browser holds.

### 2.1 The schema was already sufficient

`portal_database_dataset_columns.is_visible` and `.is_row_identifier` are
independent booleans. Representing `is_row_identifier = true` with `is_visible =
false` needed **no migration** — only the removal of the code that assumed the
two travelled together, including the admin validation that used to refuse a
hidden column as identifier.

### 2.2 The one place the identity is read

The row browser appends the identifier column to the row `SELECT` for a single
purpose: minting the opaque reference below. The value is consumed while the row
is rendered and never reaches the response. From that same `SELECT` list the
query builder also picks it up as a deterministic `ORDER BY` tie-breaker, which
is an internal ordering detail — `sort=record_id` remains unavailable to a user,
because sort is validated against the user-visible set.

## 3. The opaque row reference

The row-detail URL carries `row=<reference>`. The reference is **AES-256-GCM
ciphertext**, so it is confidentiality-preserving and integrity-protected in one
established primitive. It is not an encoding: not the raw value, not hex, not
base64, not signed cleartext.

`api/row_reference.py`, using `cryptography` only:

| Concern | Mechanism |
|---|---|
| confidentiality + integrity | `AESGCM` (`cryptography.hazmat.primitives.ciphers.aead`) |
| key derivation | `HKDF`-SHA256 with a fixed purpose-separating `info` label |
| context binding | GCM **associated data**: version, `dataset_id`, `client_code`, identifier column |
| freshness | a random 12-byte nonce per call |
| versioning | one leading version byte; an unknown version is rejected before any crypto |

No custom cryptography: no home-grown cipher, MAC, padding or key-derivation
scheme.

Because the binding is associated data, a reference minted for dataset A does
not merely fail a check against dataset B — it **cannot be decrypted** there at
all. The same holds for a different client or a different identifier column.

The fresh nonce means two references for the same row differ. A reference is
therefore never a valid cache key or an equality test for "same row"; the server
compares resolved identities instead, and the open row re-uses the reference the
URL already carries so the address bar and the rendered drawer always name the
same one.

### 3.1 Key material

Derived from the existing stable application secret,
`ARTIFACT_EXPLORER_SESSION_SECRET`, through HKDF's purpose label — so a row
reference is cryptographically unrelated to a session cookie despite the shared
root, and **no new secret has to be deployed**.

`development fallback != production deployment contract`:

- **Production.** The Compose API service declares
  `ARTIFACT_EXPLORER_SESSION_SECRET: ${ARTIFACT_EXPLORER_SESSION_SECRET:?required}`,
  matching the `:?required` convention the identity variables already use. The
  operator supplies the value from the deployment environment; Compose refuses
  to start the service if it is absent, so production can no longer *silently*
  fall back to a per-process key. The value is never generated by Compose, never
  written by the application and never committed.
- **Local development.** The ephemeral per-process fallback is retained on
  purpose so a developer instance runs without configuration. Sessions and row
  links then reset on restart, and the login page states both consequences.

The application does not repair a missing secret: it is a deployment contract,
not something to bootstrap. Secret quality (a long random value, 48 random
bytes) is already specified in `docs/06_security.md` and is unchanged here.

### 3.2 A reference is not authorization

Possession of a reference never grants access. Every row-detail request re-runs
the ordinary checks first — authenticated and active user, client grant, dataset
grant, `can_view_rows`, dataset active state, currently configured identifier —
and only then resolves the reference. Revoking access makes an old row URL
unusable immediately.

### 3.3 Failure is uniform

Malformed, truncated, oversized, tampered, unsupported-version, foreign-dataset
and foreign-client references all collapse to one generic unavailable state, as
do a row that has since been deleted and an identifier that turns out not to be
unique. Nothing distinguishes them from outside, and nothing reveals the raw
identifier, the token payload, the physical table, the SQL or the driver error.
The sheet behind the panel stays usable.

## 4. Row detail (`DB-006`)

A **docked** 520 px panel on the right, not a modal — the approved component
catalogue contains no modal dialog, and the accessibility contract lists the
docked row panel as explicitly *not* focus-trapped. It is a labelled `region`.
At ≤768 px it becomes a full-width overlay (`RSP-002`).

| Concern | Behaviour |
|---|---|
| open | `Szczegóły` link, or a click/`Enter` anywhere on the row with scripting |
| fields | approved user-visible columns, grouped into sections with counts |
| default scope | the columns currently on the sheet; `Wszystkie` switches to all approved (`DB-39`) |
| traversal | `↑`/`↓` between the references minted for the current page (`DB-37`) |
| close | `×` or `Esc`; outside click and table scroll have no effect (`INT` §3) |
| focus | to the panel heading on open; back to the row on close (`DB-38`, `AC-4`) |
| scroll | vertical and horizontal table scroll survive open/close/traverse |

A column hidden from the grid still appears in the drawer: sheet layout is a
presentation preference, while the drawer shows the record. The technical
identifier never appears in either, and there is deliberately no "copy technical
ID" action.

### 4.0 `rowfields` is an allowlisted mode, not a column selector

`rowfields` selects the drawer's field scope and nothing else. Its entire
accepted vocabulary is:

| Value | Meaning |
|---|---|
| absent | the fields currently on the sheet (default) |
| `all` | every approved user-visible field |

Any other value — `record_id`, an arbitrary string, a SQL- or script-shaped
string — is **discarded during resolution**, not merely ignored downstream, so
it cannot reflect into a generated link, a hidden input, a data attribute or a
label. Repeated values collapse to the first valid one. There is deliberately no
syntax by which `rowfields` could name a physical column: the drawer's field
universe is always the approved user-visible set, after the identity exclusion.

With no row open the parameter is detail-only state and is dropped rather than
left stale.

### 4.1 Field grouping — a stated limitation

`DB-39` requires labelled sections with counts. The approved design names four
dataset-specific sections for the Telematics trips dataset, but the column catalog
carries **no section metadata**, so asserting that membership would be a
fabricated business grouping. The honest design-compatible fallback groups by
the presentation family the catalog does know — identity/text, time, metrics,
classification — which preserves the section-with-count structure the criterion
is about. Real section membership needs catalog metadata that does not yet
exist.

### 4.2 Datasets without a configured identity

No reference is minted, no row-detail affordance is rendered, and a forged `row`
parameter reaches the same generic unavailable state. **No positional key is
ever invented** — page-plus-offset is not stable identity, because a non-unique
sort reorders rows between requests.

## 5. The retired raw-identifier route

`GET /user/database/datasets/{dataset_id}/rows/{row_id}` accepted the technical
identifier as a path segment. It both disclosed the identifier in the address
bar and offered a second way to address a row beside the opaque reference, so it
is **retired**: it now authenticates and then answers `404`. A redirect would
have confirmed that a supplied value was a real identifier. Its renderer is
deleted rather than merely unrouted, so two security models cannot coexist.

## 6. Canonicalization of column-scoped parameters

Filter parameters naming a column the catalog does not approve were already
inert — the filter builder validates every column before it reaches SQL — but
they used to ride along into every generated link and hidden input. They are now
dropped at the same point `cols` and the layout state are canonicalized, so a
crafted `filter__record_id=…` is not echoed back inside the page. The effect is
general and not specific to the identifier.

## 7. Audit

Row-detail access is audited with the user, client, dataset and outcome. It
records **none** of: the raw identifier, the resolved identity, the opaque
reference, or any row value. An unresolvable reference is recorded as the safe
category `row_reference_invalid` rather than by its value.

## 8. Progressive enhancement

Every S6 capability is a plain link carrying `?row=`: opening, closing,
traversal and the field-scope toggle. The drawer is server-rendered from the
reference, so a copied URL reconstructs it with no previous JavaScript session.

`data-grid-row-detail.js` adds click/`Enter` opening, `Esc`, focus return, table
scroll preservation and arrow traversal. It is loaded page-scoped from the
row-browser response's `extra_assets`, alongside the other data-grid modules, so
it is present on every Database Explorer row-browser page — whether or not a row
is currently open and whether or not the dataset has a configured identity — and
the *first* interaction is already enhanced. It is not loaded on unrelated portal
pages, and with no row-detail triggers in the DOM it safely does nothing. It **decodes nothing** — the reference
is opaque ciphertext it only copies from one place to another — issues no
request of its own, and owns no authorization. The browser never possesses the
raw identifier.

## 9. Configuration target — NOT executed

Production configuration is a separately authorized operator action and was
**not performed**. Live read-only verification confirmed every candidate is
non-null and unique:

| Client / dataset | Physical table | Rows | Non-null `record_id` | Distinct `record_id` |
|---|---|---|---|---|
| `BRAVO00016` / `acrtclienttrips` | `public.client_trips` | 52 009 | 52 009 | 52 009 |
| `BRAVO00016` / `areport207bravo` | `telematics_reports.report_207` | 1 142 952 | 1 142 952 | 1 142 952 |
| `ALPHA00001` / `client-trips` | `public.client_trips` | 809 620 | 809 620 | 809 620 |

Current catalog state, and the action each dataset still needs:

| Client / dataset | `record_id` in catalog | Identifier configured | Required action |
|---|---|---|---|
| `BRAVO00016` / `acrtclienttrips` | yes, `is_visible = true` | no | set `record_id` as row identifier; it leaves the 42-column visible set automatically |
| `BRAVO00016` / `areport207bravo` | yes, `is_visible = true` | no | set `record_id` as row identifier; it leaves the 15-column visible set automatically |
| `ALPHA00001` / `client-trips` | **not cataloged** | no | add `record_id` to the catalog with `is_visible = false`, then set it as row identifier; the 36-column visible set is unchanged |

The intended end state for all three is identical:

```
record_id = internal row identifier
record_id = NOT ordinary user-visible
```

No schema migration is required for any of them.

## 10. Security contract

Unchanged from S2–S5, and specifically:

- authentication, client grants, dataset grants, `can_view_rows`,
  `can_filter_rows`, `can_export_rows` and aggregate authorization are untouched;
- the client database stays read-only, the statement timeout stays, every value
  is a bound parameter and every identifier is quoted from the catalog;
- the single-row lookup keeps `LIMIT 2`, so a non-unique identifier is detected
  rather than silently yielding an arbitrary row;
- S6 adds exactly one narrowly authorized backend capability — internally
  selecting and comparing the configured hidden identifier column — and that
  capability does not enter the general user-column authorization model;
- no new endpoint, no persistence table, no migration.

## 11. Implementation and tests

| File | Role |
|---|---|
| `api/row_reference.py` | AES-GCM row references: minting, resolution, binding |
| `api/main.py` | identity/visibility separation, reference plumbing, drawer, retired route |
| `api/static/js/data-grid-row-detail.js` | open/close, focus, scroll, traversal |
| `api/static/css/data-grid.css` | docked panel, selected row, ≤768 px overlay |
| `api/portal_ui/i18n.py` | the Polish vocabulary for the panel |
| `ops/tests_manual/test_portal_database_hidden_row_identity.py` | the deterministic suite |
| `ops/tests_manual/data_grid_row_detail_harness.js` | DOM stub that executes the shipped script |
