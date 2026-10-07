"""
Workflow A — retention worker.

Runs as a standard `ops/runner.py` job (contract: `run(client, run_id, params)`)
and deletes rows from client business tables whose registered retention key
column is older than `now() - retention_days`.

Design highlights (matches the central-config plan):

  * **Per-table policy** read from `workflow_a_control.client_table_retention`
    joined with `workflow_a_control.table_registry`.
  * **Cutoff is computed in Python (UTC)** as
    `datetime.now(timezone.utc) - timedelta(days=retention_days)` and passed
    as a single `%s` parameter. The SQL never builds `NOW() - INTERVAL '… days'`
    dynamically.
  * **Identifier safety**: schema, table, and retention key column are looked
    up in the registry-derived allowlist (`jobs.api.telematics.registry`) and
    interpolated via `psycopg.sql.Identifier`. Any mismatch between the
    platform table_registry and the Python registry aborts that (client, table).
  * **Batched DELETE** with `LIMIT batch_size` per round, COMMIT per batch, and
    optional `max_batches` cap. Each round identifies victim rows by `ctid` so
    the DELETE always matches a finite set even if writes happen concurrently.
  * **Default safe**: `dry_run=true` unless explicitly disabled in params.

Params accepted:

  * `dry_run`            — default True. When True, no DELETE is executed and
                           no audit row is updated; the worker only reports
                           how many rows would be deleted.
  * `client_id`          — optional UUID filter (one client).
  * `table_name`         — optional registry table filter (one table).
  * `batch_size`         — default 5000. Maximum rows deleted per round.
  * `max_batches`        — optional cap for testing. Unlimited if absent.

Audit:

After a successful (non-dry-run) purge the worker updates
`client_table_retention` with `last_purge_run_at`, `last_purge_cutoff_ts`,
`last_purge_deleted_count`. Failed clients are logged and skipped; one
failing client does not abort the whole run.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from api.timezone_utils import set_pg_session_timezone
from jobs.api.telematics import registry
from jobs.api.telematics.secret_resolver import resolve_secret
from ops.retention_registry import (
    GLOBAL_POLICY_ID,
    HARD_RETENTION_MONTHS,
    get as get_retention_policy,
)

#: The registry entry that governs every table this worker can touch. Its
#: enforcement cutoff — the deadline moved earlier by the responsible
#: maintenance cycle — is what the ceiling clamp below compares against, so a
#: weekly worker cannot leave a row alive past the deadline until next Sunday.
CLIENT_TABLE_POLICY_ID = "client_db.workflow_a_registered_tables"


JOB_SOURCE = "jobs.api.telematics.retention_purge"


def _require_dependency(module: str, feature: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency for {feature}: {module}") from exc


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _platform_pg_conn():
    psycopg = _require_dependency("psycopg", "Postgres connection")
    dsn = (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn))


def _client_business_pg_conn(*, host: str, port: int, dbname: str, user: str,
                             password_secret_ref: str):
    psycopg = _require_dependency("psycopg", "Postgres connection")
    password = resolve_secret(password_secret_ref)
    dsn = (
        f"host={host} port={port} dbname={dbname} user={user} password={password}"
    )
    # autocommit=False; we COMMIT explicitly per batch.
    return set_pg_session_timezone(psycopg.connect(dsn, autocommit=False))


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------

def _load_policies(
    *, only_client_id: Optional[str], only_table_name: Optional[str],
) -> List[Dict[str, Any]]:
    """Return enabled retention policies joined with the platform allowlist.

    Each row contains: client_id, client_code, client_name, client_db_host, client_db_port,
    client_db_name, client_db_user, client_db_password_secret_ref,
    client_db_schema (effective schema for client business DB),
    table_name, retention_days, schema_in_registry, retention_key_column.
    """
    psycopg = _require_dependency("psycopg", "Postgres connection")
    from psycopg.rows import dict_row

    conn = _platform_pg_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            sql_text = """
                SELECT
                  ca.client_id::text       AS client_id,
                  COALESCE(ctr.client_code, ca.client_code, '') AS client_code,
                  ca.client_name           AS client_name,
                  ca.client_db_host        AS client_db_host,
                  ca.client_db_port        AS client_db_port,
                  ca.client_db_name        AS client_db_name,
                  ca.client_db_user        AS client_db_user,
                  ca.client_db_password_secret_ref AS client_db_password_secret_ref,
                  ca.client_db_schema      AS client_db_schema,
                  ctr.table_name           AS table_name,
                  ctr.retention_days       AS retention_days,
                  tr.schema_name           AS schema_in_registry,
                  tr.retention_key_column  AS retention_key_column
                FROM workflow_a_control.client_table_retention ctr
                JOIN workflow_a_control.client_account ca
                  ON ca.client_id = ctr.client_id
                JOIN workflow_a_control.table_registry tr
                  ON tr.table_name = ctr.table_name
                WHERE ctr.enabled = true
                  AND ca.enabled  = true
            """
            params: list = []
            if only_client_id:
                sql_text += " AND ca.client_id::text = %s"
                params.append(only_client_id)
            if only_table_name:
                sql_text += " AND ctr.table_name = %s"
                params.append(only_table_name)
            sql_text += " ORDER BY ca.client_name, ctr.table_name"

            cur.execute(sql_text, tuple(params))
            return list(cur.fetchall())
    finally:
        conn.close()


def _update_audit(
    *, client_id: str, table_name: str, cutoff_ts: datetime,
    deleted_count: int, run_at: datetime,
) -> None:
    conn = _platform_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_a_control.client_table_retention
                   SET last_purge_run_at      = %s,
                       last_purge_cutoff_ts   = %s,
                       last_purge_deleted_count = %s,
                       updated_at = NOW()
                 WHERE client_id  = %s
                   AND table_name = %s
                """,
                (run_at, cutoff_ts, deleted_count, client_id, table_name),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Batched DELETE
# ---------------------------------------------------------------------------

def _batched_delete(
    *, conn, schema: str, table: str, retention_key_column: str,
    cutoff_ts: datetime, batch_size: int, max_batches: Optional[int],
    log_fn,
) -> int:
    """Execute the batched DELETE on one client business table.

    `schema`, `table`, and `retention_key_column` MUST already be allowlist-
    validated by the caller. They are interpolated via `psycopg.sql.Identifier`.

    Returns the total number of rows deleted (committed).
    """
    psycopg = _require_dependency("psycopg", "Postgres DELETE")
    from psycopg import sql as pgsql

    schema_ident = pgsql.Identifier(schema)
    table_ident = pgsql.Identifier(table)
    col_ident = pgsql.Identifier(retention_key_column)

    delete_sql = pgsql.SQL(
        "WITH victims AS ( "
        "  SELECT ctid FROM {schema}.{table} "
        "  WHERE {col} < %s "
        "  ORDER BY {col} "
        "  LIMIT %s "
        ") "
        "DELETE FROM {schema}.{table} "
        "WHERE ctid IN (SELECT ctid FROM victims)"
    ).format(schema=schema_ident, table=table_ident, col=col_ident)

    total_deleted = 0
    rounds = 0
    while True:
        rounds += 1
        with conn.cursor() as cur:
            cur.execute(delete_sql, (cutoff_ts, batch_size))
            rc = cur.rowcount or 0
        conn.commit()
        total_deleted += rc

        log_fn("INFO",
               f"Batch {rounds}: deleted {rc} rows "
               f"(running total {total_deleted}) from {schema}.{table}",
               {"schema": schema, "table": table, "batch_size": batch_size,
                "batch_round": rounds, "rows_deleted_this_batch": rc,
                "rows_deleted_total": total_deleted})

        if rc < batch_size:
            break
        if max_batches is not None and rounds >= max_batches:
            log_fn("WARNING",
                   f"max_batches={max_batches} reached for {schema}.{table}; stopping early",
                   {"schema": schema, "table": table,
                    "rows_deleted_total": total_deleted})
            break

    return total_deleted


def _count_eligible(
    *, conn, schema: str, table: str, retention_key_column: str,
    cutoff_ts: datetime,
) -> int:
    """Return the count of rows that WOULD be deleted under the policy.

    Used by dry-run to give an accurate impact estimate without writing.
    """
    psycopg = _require_dependency("psycopg", "Postgres count")
    from psycopg import sql as pgsql

    count_sql = pgsql.SQL(
        "SELECT COUNT(*) FROM {schema}.{table} WHERE {col} < %s"
    ).format(
        schema=pgsql.Identifier(schema),
        table=pgsql.Identifier(table),
        col=pgsql.Identifier(retention_key_column),
    )
    with conn.cursor() as cur:
        cur.execute(count_sql, (cutoff_ts,))
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# run() — runner contract
# ---------------------------------------------------------------------------

def run(client, run_id: str, params: dict):
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")

    dry_run = bool(params.get("dry_run", True))
    only_client_id = params.get("client_id")
    only_table_name = params.get("table_name")
    batch_size = int(params.get("batch_size", 5000))
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")
    max_batches_raw = params.get("max_batches")
    max_batches: Optional[int] = (
        int(max_batches_raw) if max_batches_raw not in (None, "") else None
    )

    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        f"Retention purge starting (dry_run={dry_run}, batch_size={batch_size})",
        run_id=run_id,
        context={
            "dry_run": dry_run,
            "batch_size": batch_size,
            "max_batches": max_batches,
            "filter_client_id": only_client_id,
            "filter_table_name": only_table_name,
        },
    )

    policies = _load_policies(
        only_client_id=only_client_id, only_table_name=only_table_name,
    )
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        f"Loaded {len(policies)} enabled retention policies",
        run_id=run_id,
        context={"policy_count": len(policies)},
    )

    summary: List[Dict[str, Any]] = []
    skipped = 0
    failed = 0

    for pol in policies:
        client_id = pol["client_id"]
        client_code = pol.get("client_code") or ""
        client_name = pol["client_name"] or "?"
        table_name = pol["table_name"]
        retention_days = int(pol["retention_days"])
        platform_schema = pol["schema_in_registry"]
        platform_col = pol["retention_key_column"]

        # Allowlist cross-check: the table MUST be known to the Python registry,
        # and the retention key column MUST match the registry value. This is
        # the second wall against accidental identifier drift.
        py_spec = registry.TABLES.get(table_name)
        if py_spec is None:
            client.log(
                "ERROR", "SCRIPT", JOB_SOURCE,
                f"Skipping {client_name}/{table_name}: table not in Python registry",
                run_id=run_id,
                context={"client_id": client_id, "client_code": client_code,
                         "table_name": table_name},
            )
            skipped += 1
            continue
        if py_spec.schema != platform_schema:
            client.log(
                "ERROR", "SCRIPT", JOB_SOURCE,
                f"Skipping {client_name}/{table_name}: schema mismatch "
                f"(python={py_spec.schema} platform={platform_schema})",
                run_id=run_id,
                context={"client_id": client_id, "client_code": client_code,
                         "table_name": table_name,
                         "python_schema": py_spec.schema,
                         "platform_schema": platform_schema},
            )
            skipped += 1
            continue
        if py_spec.retention_key_column != platform_col:
            client.log(
                "ERROR", "SCRIPT", JOB_SOURCE,
                f"Skipping {client_name}/{table_name}: retention column mismatch "
                f"(python={py_spec.retention_key_column} platform={platform_col})",
                run_id=run_id,
                context={"client_id": client_id, "client_code": client_code,
                         "table_name": table_name,
                         "python_col": py_spec.retention_key_column,
                         "platform_col": platform_col},
            )
            skipped += 1
            continue

        # The effective schema in the client business DB. We trust
        # `client_account.client_db_schema` (admin-only field) over the
        # registry default, which is just the canonical 'public' name.
        effective_schema = pol["client_db_schema"] or py_spec.schema

        # Cutoff in Python (UTC). Single timestamp per (client, table) — this
        # is a hard requirement from the plan.
        run_at = datetime.now(timezone.utc)
        policy_cutoff = run_at - timedelta(days=retention_days)

        # THE CEILING IS A FLOOR ON WHAT GETS DELETED.
        #
        # `retention_days` is an operator-editable column, so a policy row can
        # name any horizon at all — including one longer than the platform-wide
        # 13-calendar-month maximum, which would put this worker in the position
        # of enforcing a policy the owner has forbidden. The effective cutoff is
        # therefore the LATER of the two instants, because a later cutoff deletes
        # MORE: a shorter per-client policy keeps its shorter lifetime untouched,
        # and a longer one is silently unable to retain past the ceiling.
        #
        # Today every enabled policy is 365 days or less, so this changes no
        # current behaviour; it makes the ceiling impossible to edit around.
        ceiling_cutoff = get_retention_policy(
            CLIENT_TABLE_POLICY_ID
        ).enforcement_cutoff(run_at)
        cutoff_ts = max(policy_cutoff, ceiling_cutoff)
        ceiling_applied = cutoff_ts is ceiling_cutoff and ceiling_cutoff > policy_cutoff

        per_log_ctx = {
            "client_id": client_id,
            "client_code": client_code,
            "client_name": client_name,
            "table_name": table_name,
            "schema": effective_schema,
            "retention_days": retention_days,
            "retention_key_column": py_spec.retention_key_column,
            "cutoff_ts": cutoff_ts.isoformat(),
            "policy_cutoff_ts": policy_cutoff.isoformat(),
            "hard_retention_policy_id": GLOBAL_POLICY_ID,
            "hard_retention_months": HARD_RETENTION_MONTHS,
            "hard_retention_cutoff_ts": ceiling_cutoff.isoformat(),
            "hard_retention_ceiling_applied": ceiling_applied,
        }

        if ceiling_applied:
            client.log(
                "WARNING", "SCRIPT", JOB_SOURCE,
                f"{client_name}/{table_name}: configured retention of "
                f"{retention_days} days is longer than the global "
                f"{HARD_RETENTION_MONTHS}-calendar-month ceiling; the ceiling "
                f"cutoff is used instead",
                run_id=run_id,
                context=per_log_ctx,
            )

        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            f"Processing {client_name}/{table_name} "
            f"(retention={retention_days}d, cutoff={cutoff_ts.isoformat()})",
            run_id=run_id,
            context=per_log_ctx,
        )

        def _per_log(level: str, message: str, extra: Dict[str, Any]) -> None:
            ctx = dict(per_log_ctx)
            ctx.update(extra)
            client.log(level, "SCRIPT", JOB_SOURCE, message, run_id=run_id, context=ctx)

        try:
            conn = _client_business_pg_conn(
                host=pol["client_db_host"],
                port=int(pol["client_db_port"]),
                dbname=pol["client_db_name"],
                user=pol["client_db_user"],
                password_secret_ref=pol["client_db_password_secret_ref"],
            )
        except Exception as e:
            client.log(
                "ERROR", "SCRIPT", JOB_SOURCE,
                f"Failed to connect to client business DB for {client_name}: {e}",
                run_id=run_id,
                context={**per_log_ctx, "error": str(e)},
            )
            failed += 1
            continue

        try:
            if dry_run:
                eligible = _count_eligible(
                    conn=conn,
                    schema=effective_schema,
                    table=py_spec.name,
                    retention_key_column=py_spec.retention_key_column,
                    cutoff_ts=cutoff_ts,
                )
                client.log(
                    "INFO", "SCRIPT", JOB_SOURCE,
                    f"[DRY-RUN] {client_name}/{table_name}: {eligible} rows would be deleted",
                    run_id=run_id,
                    context={**per_log_ctx, "would_delete_count": eligible},
                )
                summary.append({
                    "client_id": client_id, "client_code": client_code,
                    "table_name": table_name,
                    "dry_run": True, "would_delete_count": eligible,
                })
            else:
                deleted = _batched_delete(
                    conn=conn,
                    schema=effective_schema,
                    table=py_spec.name,
                    retention_key_column=py_spec.retention_key_column,
                    cutoff_ts=cutoff_ts,
                    batch_size=batch_size,
                    max_batches=max_batches,
                    log_fn=_per_log,
                )
                _update_audit(
                    client_id=client_id, table_name=table_name,
                    cutoff_ts=cutoff_ts, deleted_count=deleted, run_at=run_at,
                )
                summary.append({
                    "client_id": client_id, "client_code": client_code,
                    "table_name": table_name,
                    "dry_run": False, "deleted_count": deleted,
                })
        except Exception as e:
            client.log(
                "ERROR", "SCRIPT", JOB_SOURCE,
                f"Purge failed for {client_name}/{table_name}: {e}",
                run_id=run_id,
                context={**per_log_ctx, "error": str(e)},
            )
            failed += 1
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        f"Retention purge complete: {len(summary)} table(s) processed, "
        f"{skipped} skipped (allowlist), {failed} failed",
        run_id=run_id,
        context={
            "dry_run": dry_run,
            "processed": len(summary),
            "skipped": skipped,
            "failed": failed,
            "summary": summary,
        },
    )
