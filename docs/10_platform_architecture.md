# 10. Platform Architecture (canonical foundation)

Status: **foundation document.** It records the canonical naming, route,
authentication, and authorization decisions for treating the system as one
platform with subordinate modules. It is the stable reference produced by the
integrity audit (`ops/reports/platform_integrity_audit_and_unification_plan.md`)
and the Phase 1 / 2A foundation work.

This document describes **direction and current canonical names**. It does not
itself change runtime behavior. Where a target differs from what is implemented,
it is labelled **(future)**.

---

## 1. System concept

**Log Platform is one platform with subordinate modules.** There is one identity
store, one UI session, and one login. Each functional area is a module of the
single platform rather than a separate application.

Modules:

```
Auth              — login/logout/session; identity (artifact_users)
Admin Portal      — users, groups, clients, report folders, datasets, audit
User Workspace    — reports explorer, client database explorer
Artifacts         — artifact triage UI (RBAC) + token API (separate auth)
Reports           — report-folder resource model (Workflow B artifacts)
Client Databases  — allowlisted dataset catalog + read-only row browse/export
Jobs/Automation   — runner, timers, dispatcher (unchanged)
Audit             — append-only sanitized event log
Operations        — logdb, client DBs, MinIO, Docker/systemd, backup/retention
```

**Implementation status:** Phase 3A introduced a unified UI shell so the product
reads as one **Log Platform** with these modules — a shared brand and module
sub-label in the portal sidebar, lightweight breadcrumbs
(`Log Platform / <module> / <page>`), `/logout` as the preferred logout link, and
the Artifact Explorer header positioned as the **Artifacts** module with links
back to Admin / User Workspace. This was navigation/branding only: routes,
permissions, session cookie, RBAC, and the Artifact Browser token API are
unchanged, and `/artifact-explorer/login` + `/artifact-explorer/logout` remain
compatibility aliases.

---

## 2. Canonical route naming

Current canonical direction:

```
/login                       canonical UI login (Phase 1)
/logout                      canonical UI logout (Phase 1)

/artifact-explorer/login     compatibility alias (retained)
/artifact-explorer/logout    compatibility alias (retained)

/admin/*                     Admin Portal (admin only)
/user/*                      User Workspace (for now; see note)
/artifact-explorer/*         Artifact Explorer UI — retained as the technical
                             artifact module / admin-facing area
/artifact-browser/*          Artifact Browser token/API layer — retained
                             SEPARATELY (Bearer token, not UI session)
```

Notes:
- `/login` and `/logout` are additive aliases that delegate to the existing
  Artifact Explorer login/logout handlers. No second handler, identity model, or
  cookie is introduced.
- `/user/*` is **not** renamed to `/app/*` in this phase. Renaming `/user` to
  `/app` is documented as a **(future)** option only and must be done as an
  alias-first migration with redirects.
- Unauthenticated `/user` and `/admin` requests redirect to a safe login route
  (currently `/artifact-explorer/login?next=…`; `/login` is the canonical
  equivalent). The `next` target is validated against the allowed UI prefixes.

---

## 3. Authentication and session model

- `artifact_users` is the single local UI identity table (`user_id` UUID PK,
  `username`, `display_name`, `is_active`, `is_admin`, password hash). Passwords
  are PBKDF2-SHA256.
- `/login` is the **canonical** login route; `/artifact-explorer/login` is a
  retained **compatibility alias**. Both authenticate with the same logic.
- The UI session is a single signed cookie, **platform-wide**:
  - name `artifact_explorer_session` (unchanged),
  - path `/` (shared by `/artifact-explorer`, `/user`, `/admin`, `/login`),
  - HMAC-SHA256 signed with `ARTIFACT_EXPLORER_SESSION_SECRET`,
  - 12-hour lifetime, `HttpOnly`, `SameSite=Lax`.
  - If `ARTIFACT_EXPLORER_SESSION_SECRET` is unset, an ephemeral process-local
    secret is used and sessions reset on restart; set the env var in any
    persistent deployment. Since S6 the same secret also derives the Database
    Explorer opaque row references (`docs/29`), so an unset value additionally
    breaks every copied `row=` URL on restart. The production Compose API
    service declares it required and will not start without it.
- The **Artifact Browser token API** (`/artifact-browser/*`, plus `/runs`,
  `/logs`, `/artifacts*`, `/maintenance/prune`) authenticates with Bearer tokens
  (`API_READ_TOKEN` / `API_WRITE_TOKEN`) and is **separate** from the UI login
  session. The two auth schemes are never bridged.

---

## 4. Authorization model

Two authorization subsystems are layered on the single identity:

1. **Admin flag.** `artifact_users.is_admin` is the single gate for the Admin
   Portal (`/admin/*`) and bypasses Artifact RBAC.
2. **Artifact RBAC.** `artifact_roles` → `artifact_role_permissions` (attribute
   filters: workflow/stage/role/report_type/client_code/file_ext/layout_version/
   tag), assigned via `artifact_user_roles`. Governs the Artifacts module.
3. **Portal access.** Tenant + resource capabilities over portal clients, report
   folders, and datasets via `portal_*` tables.

Portal access semantics:

- **Client is the tenant boundary** (`portal_clients`, keyed by `client_code`).
  Every report folder and dataset belongs to exactly one client.
- **Client-level capabilities:** `can_view_reports`, `can_view_database`,
  `can_export_database` — direct (`portal_user_clients`) or via active group
  (`portal_group_clients`).
- **Resource-level capabilities:** report folders (`can_preview`,
  `can_download`); datasets (`can_view_rows`, `can_filter_rows`,
  `can_export_rows`) — direct or via active group.
- **Effective access = additive OR** of direct and active-group grants for every
  capability. A resource assignment requires the matching client-level
  capability on the same client.
- **`portal_clients.database_name`** maps a portal client to its physical
  business database for the Client Database Explorer. It is an allowlisted
  database name only (validated identifier), never a DSN or credential, and the
  portal connects to it read-only.
- **Generated Database Explorer exports** are requester-owned artifacts. Async export jobs store `requested_by_user_id`; completed artifacts carry `owner_user_id` and `expires_at`. The requester can view/download their own unexpired generated export through the Artifacts module. Other ordinary users cannot discover it through Artifact Explorer permission filters or download it by guessing the ID. Existing admins keep their current Artifact Explorer admin visibility, but download/preview is globally blocked after expiry.

Central effective-access helpers (Phase 2A; behavior-preserving wrappers around
the inline logic, not yet swapped into all call sites):

```
_get_effective_client_access_for_user(user_id, client_code)
_get_effective_dataset_access_for_user(user_id, dataset_id)          # capability flags or None
_get_effective_dataset_access_details_for_user(user_id, dataset_id)  # flags + source + gates
_get_effective_report_folder_access_for_user(user_id, folder_id)
```

---

## 5. Future migration principles

- **Preserve compatibility routes.** Keep `/artifact-explorer/login` (and any
  current route) working when introducing canonical names.
- **Add aliases before renaming.** New canonical routes are added as aliases that
  delegate to existing handlers; redirects/renames come later, deliberately.
- **Centralize helpers before switching call sites.** Introduce shared access
  helpers, prove parity, then migrate call sites incrementally.
- **Parity-test helper behavior.** A new helper must return the same result as
  the logic it wraps before any call site is switched.
- **Do not change permission semantics** without an explicit, separately scoped
  migration. Naming and structure may change; effective access must not change
  silently.
- **Keep the token API separate** from the UI session at all times.
