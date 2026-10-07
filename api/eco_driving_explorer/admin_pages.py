"""Framework-independent server-rendered Eco Driving permission admin pages."""

from __future__ import annotations

import html
from dataclasses import dataclass
from math import ceil
from urllib.parse import quote, urlencode

from .access import ECO_ACCESS_FLAGS
from .admin_models import EcoPermissionEditor, EcoPermissionState
from .admin_service import EcoDrivingPermissionAdminService


ADMIN_PORTAL_LABEL = "Admin Portal"
ECO_ADMIN_CSS = """
.eco-admin-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(12rem,1fr));gap:1rem}
.eco-admin-state{display:grid;gap:.3rem;min-width:12rem}.eco-admin-state strong{color:var(--portal-text)}
.eco-admin-inherited{font-size:.9rem;color:var(--portal-muted)}
.eco-admin-checkboxes{display:grid;gap:.55rem;min-width:13rem}
.eco-admin-effective{display:grid;gap:.35rem;min-width:11rem}
.eco-admin-provider{display:block;color:var(--portal-muted);font-size:.9rem;margin-top:.25rem}
.eco-admin-table td{vertical-align:top}.eco-admin-table caption{text-align:left;padding:0 0 .75rem;color:var(--portal-muted)}
"""


@dataclass(frozen=True)
class AdminPageResult:
    title: str
    body_html: str
    status_code: int = 200
    active_key: str = "eco-driving-permissions"
    description: str = "Manage direct and group Eco Driving Explorer permissions."


def _e(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _status(active: bool) -> str:
    return '<span class="portal-badge success">Active</span>' if active else '<span class="portal-badge">Inactive</span>'


def _user_status(active: bool, is_admin: bool) -> str:
    admin = ' <span class="portal-badge success">Admin</span>' if is_admin else ""
    return _status(active) + admin


def _yes(value: bool) -> str:
    return '<span class="portal-badge success">Yes</span>' if value else '<span class="portal-badge">No</span>'


def _message(message: str | None, *, success: bool = False) -> str:
    if not message:
        return ""
    kind = "success" if success else "danger"
    return f'<div class="portal-callout {kind}" role="status">{_e(message)}</div>'


class EcoDrivingPermissionAdminPages:
    def __init__(self, service: EcoDrivingPermissionAdminService) -> None:
        self.service = service

    def landing(self, *, page: int = 1, limit: int = 50) -> AdminPageResult:
        data = self.service.dashboard(page=page, limit=limit)
        users = "".join(
            '<tr>'
            f'<td><strong>{_e(row.get("display_name") or row.get("username"))}</strong><br><span class="portal-muted">{_e(row.get("username"))}</span></td>'
            f'<td>{_user_status(bool(row.get("is_active")), bool(row.get("is_admin")))}</td>'
            f'<td>{int(row.get("direct_count") or 0)}</td>'
            f'<td>{int(row.get("inherited_count") or 0)}</td>'
            f'<td>{int(row.get("effective_count") or 0)}</td>'
            f'<td><a class="portal-link" href="/admin/client-access/eco-driving/users/{quote(str(row.get("user_id") or ""), safe="")}">Edit Eco grants</a></td>'
            '</tr>'
            for row in data["users"]
        ) or '<tr><td colspan="6">No portal users are available.</td></tr>'
        groups = "".join(
            '<tr>'
            f'<td><strong>{_e(row.get("group_name"))}</strong></td>'
            f'<td>{_status(bool(row.get("is_active")))}</td>'
            f'<td>{int(row.get("active_members") or 0)}</td>'
            f'<td>{int(row.get("client_grants") or 0)}</td>'
            f'<td><a class="portal-link" href="/admin/client-access/eco-driving/groups/{quote(str(row.get("group_id") or ""), safe="")}">Edit Eco grants</a></td>'
            '</tr>'
            for row in data["groups"]
        ) or '<tr><td colspan="5">No portal groups are available.</td></tr>'
        clients = "".join(
            '<tr>'
            f'<td><strong>{_e(row.get("display_name"))}</strong><br><span class="portal-muted">{_e(row.get("client_code"))}</span></td>'
            f'<td>{self._providers(row.get("providers") or ())}</td>'
            f'<td>{int(row.get("direct_user_grants") or 0)}</td>'
            f'<td>{int(row.get("group_grants") or 0)}</td>'
            f'<td>{int(row.get("effective_users") or 0)}</td>'
            '</tr>'
            for row in data["clients"]
        ) or '<tr><td colspan="5">No enabled portal clients are available.</td></tr>'
        total_pages = max(1, ceil(max(int(data["users_total"]), int(data["groups_total"])) / limit))
        pagination = self._pagination(page, limit, total_pages)
        body = (
            '<nav aria-label="Breadcrumb"><a class="portal-link" href="/admin/client-access">Client Access</a> → Eco Driving permissions</nav>'
            '<section class="portal-panel"><h2>Permission model</h2><p>Administrators can configure client-scoped Eco permissions here. '
            'Administrative access does not grant the administrator permission to browse Eco ranking data as a normal portal user.</p>'
            '<p class="portal-muted">Effective access is the additive union of a direct grant and grants from active groups. No permission is assigned automatically.</p></section>'
            '<section class="portal-panel"><h2>Users</h2><div class="portal-table-wrap"><table class="portal-table eco-admin-table">'
            '<caption>Direct, inherited and effective Eco Driving client grants for portal users.</caption><thead><tr><th scope="col">User</th><th scope="col">Status</th><th scope="col">Direct clients</th><th scope="col">Inherited clients</th><th scope="col">Effective clients</th><th scope="col">Action</th></tr></thead><tbody>'
            + users + '</tbody></table></div></section>'
            '<section class="portal-panel"><h2>Groups</h2><div class="portal-table-wrap"><table class="portal-table eco-admin-table">'
            '<caption>Eco Driving client grants configured on portal groups.</caption><thead><tr><th scope="col">Group</th><th scope="col">Status</th><th scope="col">Active members</th><th scope="col">Client grants</th><th scope="col">Action</th></tr></thead><tbody>'
            + groups + '</tbody></table></div>' + pagination + '</section>'
            '<section class="portal-panel"><h2>Configured Eco Driving clients</h2><div class="portal-table-wrap"><table class="portal-table eco-admin-table">'
            '<caption>All enabled portal clients remain configurable whether or not an Eco provider is registered. Provider status comes only from the controlled application registry.</caption><thead><tr><th scope="col">Client</th><th scope="col">Registered provider</th><th scope="col">Direct user grants</th><th scope="col">Group grants</th><th scope="col">Effective active users</th></tr></thead><tbody>'
            + clients + '</tbody></table></div>'
            + (f'<p class="portal-muted">Showing the first 500 of {int(data["clients_total"])} enabled clients.</p>' if int(data["clients_total"]) > 500 else '')
            + '</section>'
        )
        return AdminPageResult("Eco Driving permissions", body)

    @staticmethod
    def _providers(providers: object) -> str:
        values = tuple(providers)
        if not values:
            return '<span class="portal-badge">Not registered</span><span class="eco-admin-provider">Permission remains configurable</span>'
        return '<span class="portal-badge success">Registered</span>' + "".join(
            f'<span class="eco-admin-provider">{_e(item.display_name)} — {_e(item.ranking_family)}</span>'
            for item in values
        )

    @staticmethod
    def _pagination(page: int, limit: int, total_pages: int) -> str:
        links = [f'<span class="portal-muted">Page {page} of {total_pages}</span>']
        if page > 1:
            links.append(f'<a class="portal-button secondary" href="?{urlencode({"page": page - 1, "limit": limit})}">Previous</a>')
        if page < total_pages:
            links.append(f'<a class="portal-button secondary" href="?{urlencode({"page": page + 1, "limit": limit})}">Next</a>')
        return '<div class="portal-actions">' + "".join(links) + '</div>'

    def user_editor(self, user_id: str, *, actor_user_id: str, outcome: str | None = None, error: str | None = None, status_code: int = 200) -> AdminPageResult:
        return self._editor(self.service.user_editor(user_id), actor_user_id, outcome=outcome, error=error, status_code=status_code)

    def group_editor(self, group_id: str, *, actor_user_id: str, outcome: str | None = None, error: str | None = None, status_code: int = 200) -> AdminPageResult:
        return self._editor(self.service.group_editor(group_id), actor_user_id, outcome=outcome, error=error, status_code=status_code)

    def _editor(self, editor: EcoPermissionEditor, actor_user_id: str, *, outcome: str | None, error: str | None, status_code: int) -> AdminPageResult:
        subject_plural = "users" if editor.subject_type == "user" else "groups"
        action = f'/admin/client-access/eco-driving/{subject_plural}/{quote(editor.subject_id, safe="")}'
        csrf = self.service.csrf.issue(actor_user_id, self.service.csrf_scope(editor.subject_type, editor.subject_id))
        rows = "".join(self._client_row(editor, row, index) for index, row in enumerate(editor.clients))
        if not rows:
            rows = '<tr><td colspan="6">No enabled portal clients are available.</td></tr>'
        group_note = (
            f'<p class="portal-muted">Changes to this group may affect {editor.active_member_count} active member(s). Group grants remain dynamic and are not copied into user rows.</p>'
            if editor.subject_type == "group" else
            '<p class="portal-muted">Inherited group grants are read-only here. This form changes direct user grants only.</p>'
        )
        body = (
            '<nav aria-label="Breadcrumb"><a class="portal-link" href="/admin/client-access">Client Access</a> → '
            '<a class="portal-link" href="/admin/client-access/eco-driving">Eco Driving permissions</a> → Editor</nav>'
            + _message("Eco Driving permissions saved." if outcome == "updated" else ("No Eco Driving permission changes were needed." if outcome == "no-change" else None), success=True)
            + _message(error)
            + '<section class="portal-panel"><h2>Subject</h2>'
            f'<div class="eco-admin-summary"><div><span class="portal-muted">{_e(editor.subject_type.title())}</span><br><strong>{_e(editor.subject_name)}</strong></div><div><span class="portal-muted">Status</span><br>{_status(editor.subject_active)}</div></div>'
            + group_note + '</section>'
            '<section class="portal-panel"><h2>Client permissions</h2>'
            '<p>Trip-detail access requires ranking access. Route-data access requires trip-detail access. The server adds required parent permissions when a child is selected and clears dependent permissions when their parent is revoked.</p>'
            '<p class="portal-muted">Route-data permission is reserved for a later stage and currently exposes no location, address or map data.</p>'
            f'<form method="post" action="{_e(action)}" class="portal-form">'
            f'<input type="hidden" name="csrf_token" value="{_e(csrf)}"><input type="hidden" name="version_token" value="{_e(editor.version_token)}">'
            '<div class="portal-table-wrap"><table class="portal-table eco-admin-table"><caption>Direct, inherited and effective Eco Driving permissions by enabled portal client.</caption>'
            '<thead><tr><th scope="col">Client</th><th scope="col">Providers</th><th scope="col">Direct permissions</th><th scope="col">Inherited from groups</th><th scope="col">Effective access</th></tr></thead><tbody>'
            + rows + '</tbody></table></div><div class="portal-actions"><button class="portal-button" type="submit">Save Eco permissions</button>'
            '<a class="portal-button secondary" href="/admin/client-access/eco-driving">Cancel</a></div></form></section>'
        )
        return AdminPageResult(f'Eco permissions for {editor.subject_name}', body, status_code=status_code)

    def _client_row(self, editor: EcoPermissionEditor, row: object, index: int) -> str:
        code = row.client_code
        providers = "".join(
            f'<span class="eco-admin-provider">{_e(item.display_name)} — {_e(item.ranking_family)}</span>' for item in row.providers
        ) or '<span class="portal-muted">No registered Eco provider</span>'
        direct = self._checkboxes(code, row.direct, index)
        inherited = self._inherited(editor.subject_type, row.inherited)
        effective = self._effective(row.effective)
        return (
            '<tr>'
            f'<td><input type="hidden" name="client_code" value="{_e(code)}"><strong>{_e(row.display_name)}</strong><br><span class="portal-muted">{_e(code)}</span></td>'
            f'<td>{providers}</td><td>{direct}</td><td>{inherited}</td><td>{effective}</td></tr>'
        )

    @staticmethod
    def _checkboxes(code: str, state: EcoPermissionState, index: int) -> str:
        labels = {
            "can_view_eco_ranking": "View rankings",
            "can_view_eco_trip_details": "View trip details",
            "can_view_eco_trip_routes": "View route data",
        }
        parts = []
        for pos, flag in enumerate(ECO_ACCESS_FLAGS):
            field_id = f"eco-{index}-{pos}"
            checked = " checked" if getattr(state, flag) else ""
            parts.append(
                f'<label class="portal-checkbox" for="{field_id}"><input id="{field_id}" type="checkbox" name="clients[{_e(code)}][{flag}]" value="true"{checked}> <span>{labels[flag]}</span></label>'
            )
        return '<div class="eco-admin-checkboxes">' + "".join(parts) + '</div>'

    @staticmethod
    def _inherited(subject_type: str, inherited: object) -> str:
        if subject_type == "group":
            return '<span class="portal-muted">Not applicable to a group grant</span>'
        items = (
            ("Ranking", inherited.ranking_groups),
            ("Trip details", inherited.detail_groups),
            ("Route data", inherited.route_groups),
        )
        return '<div class="eco-admin-state">' + "".join(
            f'<div><strong>{label}:</strong> <span class="eco-admin-inherited">{_e(", ".join(groups)) if groups else "None"}</span></div>'
            for label, groups in items
        ) + '</div>'

    @staticmethod
    def _effective(state: EcoPermissionState) -> str:
        return '<div class="eco-admin-effective">' + "".join((
            f'<div>Ranking: {_yes(state.can_view_eco_ranking)}</div>',
            f'<div>Trip details: {_yes(state.can_view_eco_trip_details)}</div>',
            f'<div>Route data: {_yes(state.can_view_eco_trip_routes)}</div>',
        )) + '</div>'

    @staticmethod
    def error_page(title: str, message: str, status_code: int) -> AdminPageResult:
        body = (
            '<nav aria-label="Breadcrumb"><a class="portal-link" href="/admin/client-access/eco-driving">Eco Driving permissions</a></nav>'
            + _message(message)
            + '<div class="portal-actions"><a class="portal-button secondary" href="/admin/client-access/eco-driving">Back to Eco Driving permissions</a></div>'
        )
        return AdminPageResult(title, body, status_code=status_code)
