"""The Report Explorer authorization boundary.

```
GENERATED_REPORT_VISIBILITY =
      authenticated portal account
  AND portal_clients.is_active
  AND effective can_view_reports for that client   (direct OR active-group, additive OR)
```

That is `docs/40` §9, and it is the CANONICAL client boundary the rest of the
portal already uses — `portal_user_clients.can_view_reports` (`db/migrations/033`)
OR `portal_group_clients.can_view_reports` (`db/migrations/037`), resolved by
`_get_effective_client_access_for_user`. This module adds no grant model of its
own.

Three things it deliberately does NOT do:

* **No `portal_report_folders` requirement.** Folders are an artifact-query
  access mechanism (`docs/39` §11). Requiring one would gate generated reports
  on artifact plumbing they have nothing to do with, and would make a legacy
  folder membership the primary grant for a module that does not read artifacts.
* **No per-report-type grant.** No approved criterion requires one; adding it
  now would be an authorization surface nobody approved.
* **No admin bypass.** An administrator with no report grant for a client sees
  that client's reports exactly as any other account does: not at all. Hiding a
  navigation entry is presentation; every route authorizes for itself.

`RP-19` falls out of this model rather than being special-cased: an account with
`can_view_database` and without `can_view_reports` for the same client is
precisely the state that must be explained as two separate grants, so the denial
carries that distinction instead of collapsing it into one "denied".
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import ReportAccessDeniedError, ReportClientNotFoundError

# One query, matching the `effective_client_access` CTE the portal already uses
# everywhere: direct grants UNION ALL active-group grants, additively OR-ed, and
# only for active clients.
_REPORT_CLIENTS_SQL = """
WITH effective_client_access AS (
  SELECT user_id, client_code,
         bool_or(can_view_reports) AS can_view_reports
  FROM (
    SELECT puc.user_id, puc.client_code, puc.can_view_reports
    FROM portal_user_clients puc
    UNION ALL
    SELECT pgu.user_id, pgc.client_code, pgc.can_view_reports
    FROM portal_group_users pgu
    JOIN portal_groups pg ON pg.group_id = pgu.group_id AND pg.is_active IS TRUE
    JOIN portal_group_clients pgc ON pgc.group_id = pg.group_id
  ) grants
  GROUP BY user_id, client_code
)
SELECT pc.client_code, pc.display_name
  FROM effective_client_access eca
  JOIN portal_clients pc ON pc.client_code = eca.client_code
  JOIN artifact_users u ON u.user_id = eca.user_id
 WHERE eca.user_id = %s
   AND eca.can_view_reports IS TRUE
   AND pc.is_active IS TRUE
   AND u.is_active IS TRUE
 ORDER BY pc.client_code ASC
"""


@dataclass(frozen=True)
class ReportClient:
    client_code: str
    display_name: str


class ReportAccessResolver:
    """Answers exactly one question: may this account see this client's reports."""

    def __init__(self, connection_factory, *, effective_client_access) -> None:
        self._connect = connection_factory
        # Injected rather than reimplemented. There must be one gate, and this
        # is not the place where a second interpretation of it appears.
        self._effective = effective_client_access

    def list_report_clients(self, user_id: str) -> list[ReportClient]:
        if not user_id:
            return []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_REPORT_CLIENTS_SQL, (str(user_id),))
                rows = cur.fetchall()
        return [
            ReportClient(
                client_code=str(row["client_code"]),
                display_name=str(row.get("display_name") or row["client_code"]),
            )
            for row in rows
        ]

    def require_report_access(self, user: dict, client_code: str) -> ReportClient:
        """Return the client, or raise. Called on EVERY request, not once.

        A rendered link, an instance id, a member id and an artifact id are all
        untrusted inputs. Access is decided here from the account's CURRENT
        grants each time, so a revocation between rendering a list and clicking
        a row takes effect on the click.
        """
        user_id = str((user or {}).get("user_id") or "")
        code = str(client_code or "").strip()
        if not user_id or not code:
            raise ReportClientNotFoundError("no client in context")
        access = self._effective(user_id, code)
        if not access.get("client_is_active"):
            # An inactive client is indistinguishable from one that does not
            # exist. That is deliberate: "inactive" is not a fact an
            # unauthorized caller gets to learn.
            raise ReportClientNotFoundError("client not available")
        if not access.get("can_view_reports"):
            if access.get("can_view_database"):
                # `RP-19`. The account can see this client's data but not its
                # reports; the state must say so and keep client context.
                raise ReportAccessDeniedError(
                    "no report access for this client",
                    has_database_access=True,
                    client_code=code,
                    client_display_name=self._display_name(code) or code,
                )
            raise ReportClientNotFoundError("client not available")
        return ReportClient(client_code=code, display_name=self._display_name(code) or code)

    def _display_name(self, client_code: str) -> str:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT display_name FROM portal_clients WHERE client_code = %s AND is_active IS TRUE",
                    (client_code,),
                )
                row = cur.fetchone()
        return str((row or {}).get("display_name") or "")
