"""Workflow A — Eco Driving Person weekly notification email job."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

from api.timezone_utils import set_pg_session_timezone
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.common.eco_smtp_submission import (
    AMBIGUOUS_SUBMISSION,
    classify_exception,
)
from jobs.ecodriving.email_safety import (
    ExecutionMode,
    fetch_period_candidates,
    resolve_execution_contract,
    select_period_for_send,
)
from jobs.ecodriving.email_visuals import (
    render_lost_points_tiles_html,
    render_score_bar_html,
)
from jobs.ecodriving_person.email_idempotency import (
    AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION,
    DEFAULT_PENDING_STALE_AFTER_MINUTES,
    insert_audit_log,
    mark_send_ambiguous,
    mark_send_failed,
    mark_send_sent,
    mark_sent_archive_result,
    mark_sent_mime_preserved,
    normal_send_scope,
    reserve_send,
    unresolved_ambiguous_send,
)
from jobs.ecodriving_dashboard.eco_mailing_integration import (
    MAILER_ECO_PERSON_WEEKLY,
    DashboardLinkSettings,
    EcoDashboardLinkService,
    authorize_dashboard_mailing,
    dashboard_link_context,
    render_with_dashboard_link,
    validate_template_dir_link_placeholders,
)
from jobs.common.eco_sent_archive import (
    SentCopyOutcome,
    count_sent_copy,
    resolve_sent_archive_settings,
    store_sent_copy,
)
from jobs.ecodriving_person.email_delivery import (
    BRAVO00016_WEEKLY_EMAIL_PREFIX,
    PreparedEmail,
    SentArchiveSettings,
    SmtpSettings,
    archive_sent_message,
    load_smtp_settings_from_env as load_delivery_smtp_settings_from_env,
    open_run_session,
    prepare_email,
    submit_prepared_email,
)


JOB_SOURCE = "jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications"
DATASET_NAME = "eco_person_driving_weekly_email_notifications"
REPORT_TYPE = "weekly"

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE_DIR = REPO_ROOT / "assets" / "email_templates" / "ecodriving_person" / "weekly"

RANKED_TEMPLATE_MAP = {
    "bezpieczny": ("bezpieczny", "Tygodniowe - Bezpieczni.html"),
    "akceptowalny": ("akceptowalny", "Tygodniowe - Akceptowalni.html"),
    "niebezpieczny": ("niebezpieczny", "Tygodniowe - Niebezpieczni.html"),
}
NORANK_TEMPLATE_MAP = {
    "bezpieczny": ("bezpieczny", "Tygodniowe - Bezpieczni - norank.html"),
    "akceptowalny": ("akceptowalny", "Tygodniowe - Akceptowalni - norank.html"),
    "niebezpieczny": ("niebezpieczny", "Tygodniowe - Niebezpieczni - norank.html"),
}
LOW_DISTANCE_TEMPLATE = ("low_distance", "Tygodniowe - Niezakwalifikowani.html")
SCORE_CARD_BACKGROUND_BY_RATING_TYPE = {
    "bezpieczny": "#F0FAF4",
    "akceptowalny": "#FEF2F2",
    "niebezpieczny": "#FEF2F2",
}
NON_QUALIFIED_TEMPLATE_STATUSES = {"LOW_DISTANCE", "NO_DISTANCE"}
# Backward-compatible alias used by older manual tests and snippets.
RATING_TEMPLATE_MAP = RANKED_TEMPLATE_MAP
REQUIRED_TEMPLATE_FILENAMES = tuple(
    filename
    for _template_type, filename in (
        list(RANKED_TEMPLATE_MAP.values())
        + list(NORANK_TEMPLATE_MAP.values())
        + [LOW_DISTANCE_TEMPLATE]
    )
)
RANKING_PLACEHOLDERS = {"ranking_position", "ranking_total_participants"}
SUPPORTED_EMAIL_COLUMNS = ("email", "person_email", "notification_email", "email_address")
OPTIONAL_DRIVER_COLUMNS = ("person_name", "driver_name", "first_name", "last_name")
RANKING_PERCENTILE_EXPRESSION = "{{ (ranking_position / ranking_total_participants) * 100 }}"
RATING_TYPE_SHARE_PLACEHOLDER = "ecodriving_rating_type_share_percent"
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")
SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

STAT_CONTEXT_COLUMNS = (
    "client_id",
    "client_code",
    "person_name_group_key",
    "person_name",
    "week_start_date",
    "week_end_date",
    "period_start_date",
    "period_end_date",
    "eco_driving_score_total",
    "ranking_position",
    "ranking_total_participants",
    "qualification_status",
    "ranking_included",
    "ecodriving_rating_type_share_percent",
    "overrev_maxpoints_subtract",
    "harsh_braking_maxpoints_subtract",
    "harsh_acceleration_maxpoints_subtract",
    "harsh_turning_maxpoints_subtract",
    "idle_maxpoints_subtract",
    "speeding_140_160_maxpoints_subtract",
    "speeding_160_170_maxpoints_subtract",
    "speeding_170_plus_maxpoints_subtract",
    "top_1_validation",
    "top_2_validation",
    "ecodriving_rating_type",
)

SEND_LOG_COLUMNS = (
    "client_id",
    "run_id",
    "person_name_group_key",
    "person_name",
    "recipient_email",
    "original_recipient_email",
    "ranking_type",
    "report_type",
    "template_type",
    "template_filename",
    "qualification_status",
    "ranking_included",
    "template_variant",
    "period_start_date",
    "period_end_date",
    "ecodriving_rating_type",
    "email_subject",
    "status",
    "smtp_message_id",
    "provider_response",
    "error_message",
    "sent_at",
    "metadata_json",
)


@dataclass(frozen=True)
class CandidateDecision:
    status: str | None
    template_type: str
    template_filename: str
    template_variant: str
    reason: str | None = None

    @property
    def should_send(self) -> bool:
        return self.status is None


def _require_dependency(module: str, feature: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency for {feature}: {module}") from exc


def _dict_row_factory():
    try:
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Missing dependency for Postgres row factory: psycopg") from exc
    return dict_row


def _load_client_account_config(*, client_id: str):
    try:
        from jobs.api.telematics.control_plane import load_client_account_config
    except ImportError as exc:
        raise RuntimeError("Missing dependency for Workflow A control plane: psycopg") from exc
    return load_client_account_config(client_id=client_id)


def _safe_ident(name: str) -> str:
    if not SAFE_IDENTIFIER_RE.match(name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _safe_qualified(schema: str, table: str) -> str:
    return f"{_safe_ident(schema)}.{_safe_ident(table)}"


def _quote_ident(name: str) -> str:
    return '"' + _safe_ident(name).replace('"', '""') + '"'


def _client_business_pg_conn(cfg):
    psycopg = _require_dependency("psycopg", "Postgres connection")
    dsn = (
        f"host={cfg.client_db_host} "
        f"port={cfg.client_db_port} "
        f"dbname={cfg.client_db_name} "
        f"user={cfg.client_db_user} "
        f"password={resolve_secret(cfg.client_db_password_secret_ref)}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn))


def _as_bool(params: dict, key: str, default: bool) -> bool:
    if key not in params:
        return default
    value = params[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    raise ValueError(f"{key} must be a boolean")


def _optional_positive_int(params: dict, key: str) -> int | None:
    if params.get(key) in (None, ""):
        return None
    value = int(params[key])
    if value <= 0:
        raise ValueError(f"{key} must be > 0")
    return value


def _trimmed_or_none(value: Any) -> str | None:
    if value is None:
        return None
    trimmed = str(value).strip()
    return trimmed or None


def _parse_date_param(raw: Any, name: str) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD format") from exc


def _resolve_explicit_period_params(params: dict) -> tuple[date, date] | None:
    start_raw = params.get("period_start_date", params.get("week_start_date"))
    end_raw = params.get("period_end_date", params.get("week_end_date"))
    if start_raw is None and end_raw is None:
        return None
    if start_raw is None or end_raw is None:
        raise ValueError(
            "period_start_date/period_end_date must be provided together "
            "(week_start_date/week_end_date aliases are also accepted)"
        )
    start = _parse_date_param(start_raw, "period_start_date")
    end = _parse_date_param(end_raw, "period_end_date")
    if end <= start:
        raise ValueError("period_end_date must be after period_start_date")
    return start, end


def load_smtp_settings_from_env(
    *,
    dry_run: bool,
    client_code: str | None = None,
) -> SmtpSettings:
    return load_delivery_smtp_settings_from_env(dry_run=dry_run, client_code=client_code)


def _format_polish_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _business_period_end_date(row: dict[str, Any]) -> date | None:
    end_date = _coerce_date(row.get("period_end_date") or row.get("week_end_date"))
    if end_date is None:
        return None
    return end_date - timedelta(days=1)


def _format_rating_type_share_percent(value: Any) -> str:
    if value is None:
        return ""
    try:
        decimal_value = Decimal(str(value))
    except Exception:
        return ""
    if decimal_value == decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP):
        return str(int(decimal_value))
    rendered = format(decimal_value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP).normalize(), "f")
    return rendered.replace(".", ",")


def _format_template_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return _format_polish_date(value)
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return str(value)


def _rating_type_share_percent(row: dict[str, Any]) -> str:
    return _format_rating_type_share_percent(row.get("ecodriving_rating_type_share_percent"))


def build_template_context(row: dict[str, Any]) -> dict[str, str]:
    context: dict[str, Any] = {key: row.get(key) for key in STAT_CONTEXT_COLUMNS}
    context["recipient_email"] = row.get("recipient_email")

    period_start = _coerce_date(row.get("period_start_date") or row.get("week_start_date"))
    business_end = _business_period_end_date(row)
    start_display = _format_polish_date(period_start) if period_start else ""
    end_display = _format_polish_date(business_end) if business_end else ""

    context["business_period_end_date"] = business_end
    context["period_start_date_display"] = start_display
    context["period_end_date_display"] = end_display
    context["period_start_date"] = start_display
    context["period_end_date"] = end_display
    context["week_start_date"] = f"{period_start:%d}" if period_start else ""
    context["week_end_date"] = end_display

    share_percent = _rating_type_share_percent(row)
    context[RATING_TYPE_SHARE_PLACEHOLDER] = share_percent
    # Backward-compatible placeholder name: old templates used this position for
    # ranking percentile, but it now renders the rating-type group share.
    context["ranking_percentile"] = share_percent

    for key in OPTIONAL_DRIVER_COLUMNS:
        if key in row:
            context[key] = row.get(key)

    escaped_context = {
        key: html.escape(_format_template_value(value), quote=True)
        for key, value in context.items()
    }
    rating_type = _trimmed_or_none(row.get("ecodriving_rating_type"))
    escaped_context["eco_score_bar_html"] = render_score_bar_html(
        row.get("eco_driving_score_total"),
        background_color=SCORE_CARD_BACKGROUND_BY_RATING_TYPE.get((rating_type or "").lower()),
    )
    escaped_context["lost_points_tiles_html"] = render_lost_points_tiles_html(row)
    # Always present, so a template carrying the dashboard placeholder renders
    # unchanged on a deployment that has no dashboard. The link, when there is
    # one, is substituted by the caller after this point — it is an HTML
    # fragment and must not be escaped a second time.
    escaped_context.update(dashboard_link_context(None))
    return escaped_context


def _template_placeholders(template_html: str) -> set[str]:
    normalized = template_html.replace(RANKING_PERCENTILE_EXPRESSION, "{" + RATING_TYPE_SHARE_PLACEHOLDER + "}")
    return set(PLACEHOLDER_RE.findall(normalized))


def validate_template_values(
    *,
    template_html: str,
    context: dict[str, str],
    template_variant: str,
) -> None:
    placeholders = _template_placeholders(template_html)
    missing_values = sorted(
        placeholder
        for placeholder in placeholders
        if placeholder in context and context[placeholder] == ""
    )
    if template_variant == "norank":
        missing_ranking = [p for p in missing_values if p in RANKING_PLACEHOLDERS]
        if missing_ranking:
            raise ValueError(
                "No-ranking Eco Driving Person template contains ranking placeholders without values: "
                + ", ".join(missing_ranking)
            )


def render_template(template_html: str, context: dict[str, str], *, template_variant: str = "ranked") -> str:
    normalized = template_html.replace(RANKING_PERCENTILE_EXPRESSION, "{" + RATING_TYPE_SHARE_PLACEHOLDER + "}")
    validate_template_values(
        template_html=template_html,
        context=context,
        template_variant=template_variant,
    )

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            return match.group(0)
        return context[key]

    rendered = PLACEHOLDER_RE.sub(repl, normalized)
    unresolved = sorted(set(PLACEHOLDER_RE.findall(rendered)))
    if unresolved or "{{" in rendered or "}}" in rendered:
        raise ValueError(f"Unresolved Eco Driving Person email template placeholders: {unresolved}")
    return rendered


def render_subject(subject_template: str, row: dict[str, Any]) -> str:
    context = {key: _format_template_value(value) for key, value in build_template_context(row).items()}

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return context.get(key, match.group(0))

    return PLACEHOLDER_RE.sub(repl, subject_template)


def _html_to_text(html_body: str) -> str:
    text = re.sub(r"(?is)<(br|/p|/div|/tr|/h[1-6])\b[^>]*>", "\n", html_body)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip() or "Program Ecodriving"


def build_email_message(
    *,
    settings: SmtpSettings,
    recipient_email: str,
    subject: str,
    html_body: str,
    message_id: str,
) -> Any:
    return prepare_email(
        settings=settings,
        recipient_email=recipient_email,
        subject=subject,
        html_body=html_body,
        text_body=_html_to_text(html_body),
        message_id=message_id,
    ).message


def send_html_email(
    *,
    settings: SmtpSettings,
    recipient_email: str,
    subject: str,
    html_body: str,
    smtp_factory: Callable[..., Any] | None = None,
) -> str:
    prepared = prepare_email(
        settings=settings,
        recipient_email=recipient_email,
        subject=subject,
        html_body=html_body,
        text_body=_html_to_text(html_body),
    )
    submit_prepared_email(settings=settings, prepared=prepared, smtp_factory=smtp_factory)
    return prepared.message_id


def _sent_archive_settings(smtp_settings: SmtpSettings) -> SentArchiveSettings | None:
    """The mailbox this sender files its Sent copies in, if it has one.

    BRAVO00016 keeps its existing hard requirement: that sender MUST have a
    Sent mailbox, and a missing or half-written IMAP configuration stops the
    run before any customer mail is sent. Any other sender identity opts in by
    configuring the same `{PREFIX}_IMAP_*` variables; with none of them set no
    copy is attempted, and the run summary says so.
    """
    return resolve_sent_archive_settings(
        env_prefix=smtp_settings.env_prefix,
        required=smtp_settings.env_prefix == BRAVO00016_WEEKLY_EMAIL_PREFIX,
    )


def _prepare_weekly_email(
    *,
    settings: SmtpSettings,
    recipient_email: str,
    subject: str,
    html_body: str,
) -> PreparedEmail:
    return prepare_email(
        settings=settings,
        recipient_email=recipient_email,
        subject=subject,
        html_body=html_body,
        text_body=_html_to_text(html_body),
    )


def _record_sent_copy(cur, *, send_log_table: str, send_log_id: str):
    """How THIS send log records a Sent-copy outcome: its own columns.

    The two person send logs carry `sent_archive_*`
    (`db/client_business/041_eco_person_sent_archive_state.sql`), so the outcome
    is written there. No column touched here is read by the reservation guard or
    by the partial unique indexes, which is what keeps a failed copy from ever
    affecting what may be sent.
    """
    def _record(outcome: SentCopyOutcome) -> None:
        mark_sent_archive_result(
            cur,
            send_log_table=send_log_table,
            send_log_id=send_log_id,
            status=outcome.status,
            mailbox=outcome.mailbox,
            message_id=outcome.message_id,
            error_message=outcome.error,
        )
    return _record


def _archive_only_rows(
    cur,
    *,
    send_log_table: str,
    client_id: str,
    period_start_date: date,
    period_end_date: date,
    message_ids: list[str] | None,
) -> list[dict[str, Any]]:
    message_filter = ""
    params: list[Any] = [client_id, period_start_date, period_end_date]
    if message_ids:
        message_filter = "AND smtp_message_id = ANY(%s)"
        params.append(message_ids)
    cur.execute(
        f"""
        SELECT send_log_id::text, smtp_message_id, sent_mime_bytes, sent_mime_sha256
        FROM {send_log_table}
        WHERE client_id = %s
          AND period_start_date = %s
          AND period_end_date = %s
          AND status = 'sent'
          AND send_scope IN ('normal','test','forced')
          AND sent_mime_bytes IS NOT NULL
          AND COALESCE(sent_archive_status, 'pending') IN ('pending','failed')
          {message_filter}
        ORDER BY attempted_at, send_log_id
        """,
        tuple(params),
    )
    return [dict(row) for row in cur.fetchall()]


def _decode_archive_message_ids(params: dict) -> list[str] | None:
    raw = params.get("archive_message_ids")
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(part).strip() for part in raw if str(part).strip()]
    raise ValueError("archive_message_ids must be a comma-separated string or list")


def _uses_non_qualified_template(row: dict[str, Any]) -> bool:
    return str(row.get("qualification_status") or "").strip().upper() in NON_QUALIFIED_TEMPLATE_STATUSES


def _is_no_ranking(row: dict[str, Any]) -> bool:
    return row.get("ranking_included") is False


def _select_template(row: dict[str, Any]) -> tuple[str, str, str] | None:
    if _uses_non_qualified_template(row):
        template_type, filename = LOW_DISTANCE_TEMPLATE
        return template_type, filename, "low_distance"

    rating_type = _trimmed_or_none(row.get("ecodriving_rating_type"))
    template_map = NORANK_TEMPLATE_MAP if _is_no_ranking(row) else RANKED_TEMPLATE_MAP
    template = template_map.get(rating_type or "")
    if template is None:
        return None
    template_type, filename = template
    return template_type, filename, "norank" if template_map is NORANK_TEMPLATE_MAP else "ranked"


def classify_candidate(
    row: dict[str, Any],
    *,
    already_sent: bool,
    force_resend: bool,
) -> CandidateDecision:
    selected = _select_template(row)
    if selected is None:
        rating_type = _trimmed_or_none(row.get("ecodriving_rating_type"))
        return CandidateDecision(
            status="skipped_unknown_rating_type",
            template_type=rating_type or "unknown",
            template_filename="",
            template_variant="ranked",
            reason="unsupported_or_missing_ecodriving_rating_type",
        )

    template_type, template_filename, template_variant = selected
    recipient_email = _trimmed_or_none(row.get("recipient_email"))
    if not recipient_email:
        return CandidateDecision(
            status="skipped_missing_email",
            template_type=template_type,
            template_filename=template_filename,
            template_variant=template_variant,
            reason="missing_recipient_email",
        )
    if already_sent and not force_resend:
        return CandidateDecision(
            status="skipped_already_sent",
            template_type=template_type,
            template_filename=template_filename,
            template_variant=template_variant,
            reason="sent_log_exists",
        )
    return CandidateDecision(
        status=None,
        template_type=template_type,
        template_filename=template_filename,
        template_variant=template_variant,
    )


def _table_columns(cur, *, schema: str, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    return {row["column_name"] for row in cur.fetchall()}


def _select_email_column(columns: set[str]) -> str:
    for candidate in SUPPORTED_EMAIL_COLUMNS:
        if candidate in columns:
            return candidate
    raise RuntimeError(
        "public.eco_person_people_email_view has no supported email column. "
        "Add one of: " + ", ".join(SUPPORTED_EMAIL_COLUMNS)
    )


def _fetch_candidates(
    cur,
    *,
    weekly_table: str,
    driver_chart_table: str,
    client_id: str,
    period_start_date: date,
    period_end_date: date,
    email_column: str,
    driver_columns: list[str],
    person_name_group_key: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    selected_driver_columns = [
        f"c.{_quote_ident(column)} AS {_quote_ident(column)}" for column in driver_columns
    ]
    select_driver_sql = ",\n          " + ",\n          ".join(selected_driver_columns) if selected_driver_columns else ""
    params: list[Any] = [client_id, period_start_date, period_end_date]
    person_sql = ""
    if person_name_group_key:
        person_sql = "AND s.person_name_group_key = %s"
        params.append(person_name_group_key)
    limit_sql = ""
    if limit:
        limit_sql = "LIMIT %s"
        params.append(limit)

    cur.execute(
        f"""
        SELECT
          s.client_id::text AS client_id,
          s.client_code,
          s.person_name_group_key,
          s.person_name,
          s.week_start_date,
          s.week_end_date,
          s.period_start_date,
          s.period_end_date,
          s.eco_driving_score_total,
          s.ranking_position,
          s.ranking_total_participants,
          s.overrev_maxpoints_subtract,
          s.harsh_braking_maxpoints_subtract,
          s.harsh_acceleration_maxpoints_subtract,
          s.harsh_turning_maxpoints_subtract,
          s.idle_maxpoints_subtract,
          s.speeding_140_160_maxpoints_subtract,
          s.speeding_160_170_maxpoints_subtract,
          s.speeding_170_plus_maxpoints_subtract,
          s.top_1_validation,
          s.top_2_validation,
          s.ecodriving_rating_type,
          s.ecodriving_rating_type_share_percent,
          s.qualification_status,
          s.ranking_group AS ranking_type,
          c.ranking_included AS ranking_included,
          c.{_quote_ident(email_column)} AS recipient_email,
          c.{_quote_ident(email_column)} AS original_recipient_email
          {select_driver_sql}
        FROM {weekly_table} s
        LEFT JOIN {driver_chart_table} c
          ON c.client_id = s.client_id
         AND c.person_name_group_key = s.person_name_group_key
        WHERE s.client_id = %s
          AND s.period_start_date = %s
          AND s.period_end_date = %s
          AND c.is_active IS TRUE
          {person_sql}
        ORDER BY s.ranking_position NULLS LAST, s.person_name_group_key ASC
        {limit_sql}
        """,
        params,
    )
    return [dict(row) for row in cur.fetchall()]


def _already_sent(
    cur,
    *,
    send_log_table: str,
    client_id: str,
    person_name_group_key: str,
    period_start_date: date,
    period_end_date: date,
    template_type: str | None = None,  # legacy argument; not part of identity
) -> bool:
    cur.execute(
        f"""
        SELECT 1
        FROM {send_log_table}
        WHERE client_id = %s
          AND person_name_group_key = %s
          AND report_type = %s
          AND period_start_date = %s
          AND period_end_date = %s
          AND send_scope = 'normal'
          AND status = 'sent'
        LIMIT 1
        """,
        (client_id, person_name_group_key, REPORT_TYPE, period_start_date, period_end_date),
    )
    return cur.fetchone() is not None


def _insert_send_log(
    cur,
    *,
    send_log_table: str,
    run_id: str,
    row: dict[str, Any],
    decision: CandidateDecision,
    recipient_email: str,
    original_recipient_email: str | None,
    subject: str,
    status: str,
    smtp_message_id: str | None = None,
    provider_response: str | None = None,
    error_message: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> None:
    sent_at_sql = "NOW()" if status == "sent" else "NULL"
    values = (
        row.get("client_id"),
        run_id,
        row.get("person_name_group_key") or "",
        row.get("person_name") or "",
        recipient_email,
        original_recipient_email,
        row.get("ranking_type"),
        REPORT_TYPE,
        decision.template_type,
        decision.template_filename,
        row.get("qualification_status"),
        row.get("ranking_included"),
        decision.template_variant,
        row.get("period_start_date"),
        row.get("period_end_date"),
        row.get("ecodriving_rating_type") or "",
        subject,
        status,
        smtp_message_id,
        provider_response,
        error_message,
        json.dumps(metadata_json or {}, ensure_ascii=False, default=str),
    )
    placeholders = ", ".join(["%s"] * (len(SEND_LOG_COLUMNS) - 2))
    cur.execute(
        f"""
        INSERT INTO {send_log_table} (
          client_id, run_id, person_name_group_key, person_name,
          recipient_email, original_recipient_email,
          ranking_type, report_type, template_type, template_filename,
          qualification_status, ranking_included, template_variant,
          period_start_date, period_end_date, ecodriving_rating_type,
          email_subject, status, smtp_message_id, provider_response,
          error_message, sent_at, metadata_json
        )
        VALUES ({placeholders}, {sent_at_sql}, %s::jsonb)
        ON CONFLICT DO NOTHING
        """,
        values,
    )


def validate_template_inventory(template_dir: Path) -> None:
    missing = [
        filename
        for filename in REQUIRED_TEMPLATE_FILENAMES
        if not (template_dir / filename).exists()
    ]
    if missing:
        raise RuntimeError(
            "Missing required Eco Driving Person weekly email templates: " + ", ".join(missing)
        )


def _load_template(*, template_dir: Path, filename: str) -> str:
    path = template_dir / filename
    if not path.exists():
        raise RuntimeError(f"Missing Eco Driving Person weekly email template: {path}")
    return path.read_text(encoding="utf-8")


def _summary_template_counts(summary: dict[str, Any], template_type: str) -> None:
    counts = summary.setdefault("template_counts_by_type", {})
    counts[template_type] = counts.get(template_type, 0) + 1


def run(client, run_id: str, params: dict):
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")

    client_id = _trimmed_or_none(params.get("client_id"))
    if not client_id:
        raise ValueError("Missing required param: client_id")

    execution = resolve_execution_contract(params)
    explicit_period = _resolve_explicit_period_params(params)
    mode = _trimmed_or_none(params.get("mode")) or "latest_completed_weekly_snapshot"
    if explicit_period is None and mode not in {"latest_completed_weekly_snapshot", "latest_available_period"}:
        raise ValueError(
            "Unsupported mode for weekly email notifications: "
            f"{mode!r}. This job only sends already-calculated weekly stats."
        )

    dry_run = execution.mode is ExecutionMode.RENDER_ONLY
    force_resend = execution.force_resend
    fail_fast = _as_bool(params, "fail_fast", False)
    archive_only = _as_bool(params, "archive_only", False)
    archive_message_ids = _decode_archive_message_ids(params)
    if archive_only and dry_run:
        raise ValueError("archive_only cannot be combined with dry_run=true")
    if archive_only and force_resend:
        raise ValueError("archive_only cannot be combined with force_resend=true")
    limit = _optional_positive_int(params, "limit")
    pending_stale_after_minutes = (
        _optional_positive_int(params, "pending_stale_after_minutes")
        or DEFAULT_PENDING_STALE_AFTER_MINUTES
    )
    selected_person_name_group_key = _trimmed_or_none(
        params.get("person_name_group_key")
    )
    test_recipient_email = execution.test_recipient_email
    template_dir = Path(params.get("template_dir") or DEFAULT_TEMPLATE_DIR)
    subject_template = (
        _trimmed_or_none(params.get("subject"))
        or "Program Ecodriving - Tygodniowa aktualizacja wyniku ({week_start_date} - {week_end_date})"
    )

    cfg = _load_client_account_config(client_id=client_id)
    validate_template_inventory(template_dir)

    # Resolved BEFORE any candidate is processed: a run that was explicitly
    # asked for dashboard links and cannot produce them stops here rather than
    # sending a fleet of link-less e-mails.
    dashboard_settings = DashboardLinkSettings.from_params(params, render_only=dry_run)
    if dashboard_settings.enabled:
        # The link's insertion point is a PROPERTY OF THE TEMPLATE and is
        # checked here, before the first snapshot is published and the first
        # capability is minted. A placeholder that a renderer would swallow —
        # inside a comment, inside a tag — must not cost a remote effect to
        # discover, and must not reach a driver as an invisible link.
        validate_template_dir_link_placeholders(
            template_dir, REQUIRED_TEMPLATE_FILENAMES)

    # BOTH conditions or nothing. The `--with-dashboard` opt-in above is only
    # half of it; this is the client-level rollout permission
    # (`ops/eco_dashboard_mailing_rollout.json`), and it is checked HERE — after
    # the client account is resolved and before `EcoDashboardLinkService` exists,
    # before the candidate loop, and therefore before the first snapshot build,
    # publication, capability, send-log reservation or SMTP connection. A client
    # whose dashboard mailing rollout is not enabled stops the run rather than
    # silently sending the ordinary e-mail instead.
    # A run that did not ask for a dashboard reads no declaration at all.
    authorize_dashboard_mailing(
        dashboard_settings, client_code=str(getattr(cfg, "client_code", "") or ""))

    smtp_settings = load_smtp_settings_from_env(dry_run=dry_run, client_code=cfg.client_code)
    sent_archive_settings = (
        _sent_archive_settings(smtp_settings)
        if not dry_run
        else None
    )
    schema = _safe_ident(cfg.client_db_schema)
    weekly_table = _safe_qualified(schema, "eco_person_weekly_stats")
    driver_chart_table = _safe_qualified(schema, "eco_person_people_email_view")
    send_log_table = _safe_qualified(schema, "eco_person_weekly_email_send_log")

    summary: dict[str, Any] = {
        "job_name": DATASET_NAME,
        "client_id": client_id,
        "selected_period": None,
        "candidates_count": 0,
        "source_recipient_count": 0,
        "rejected_period_candidate_count": 0,
        "failed_before_smtp_count": 0,
        "rendered_count": 0,
        "sent_count": 0,
        "failed_count": 0,
        "skipped_missing_email": 0,
        "skipped_unknown_rating_type": 0,
        "skipped_already_sent": 0,
        "skipped_existing_reservation": 0,
        "would_send_count": 0,
        "dashboard_link_blocked_count": 0,
        "stale_pending_count": 0,
        #: An SMTP submission whose remote acceptance could not be excluded.
        #: Durably frozen, never retried automatically, operator-resolved.
        "smtp_ambiguous_count": 0,
        #: Candidates this run refused to touch AT ALL — no dashboard
        #: publication, no capability, no SMTP — because an earlier ambiguous
        #: submission for the same logical delivery is still unresolved.
        "ambiguous_reconciliation_blocked_count": 0,
        "idempotency_blocked_count": 0,
        "smtp_attempt_count": 0,
        "smtp_accepted_count": 0,
        "smtp_failed_count": 0,
        "dry_run": dry_run,
        "execution_mode": execution.mode.value,
        "recipient_scope": execution.send_scope,
        "force_resend": force_resend,
        "force_resend_reason": execution.force_resend_reason,
        "allow_unclosed_period_for_test": execution.allow_unclosed_period_for_test,
        "archive_only": archive_only,
        "archive_message_ids_count": len(archive_message_ids or []),
        "pending_stale_after_minutes": pending_stale_after_minutes,
        "person_name_group_key": selected_person_name_group_key,
        "limit": limit,
        "test_recipient_email": test_recipient_email,
        "template_counts_by_type": {},
        "email_env_prefix": smtp_settings.env_prefix,
        "from_header": f"{smtp_settings.from_name} <{smtp_settings.from_email}>",
        "envelope_sender": smtp_settings.from_email,
        "sent_archive_enabled": sent_archive_settings is not None,
        "sent_archive_count": 0,
        "sent_archive_already_present_count": 0,
        "sent_archive_failed_count": 0,
        "sent_archive_skipped_count": 0,
    }

    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Starting Eco Driving Person weekly email notifications",
        run_id=run_id,
        context=summary,
    )

    dashboard: EcoDashboardLinkService | None = None
    #: ONE SMTP session for the whole run (opened on the first message,
    #: reconnected only when the relay ends it). See `ReusableSmtpSession`.
    smtp_session = open_run_session(smtp_settings) if smtp_settings is not None else None
    conn = _client_business_pg_conn(cfg)
    try:
        with conn.cursor(row_factory=_dict_row_factory()) as cur:
            chart_columns = _table_columns(cur, schema=schema, table="eco_person_people_email_view")
            email_column = _select_email_column(chart_columns)
            driver_columns = [column for column in OPTIONAL_DRIVER_COLUMNS if column in chart_columns]

            period_selection = select_period_for_send(
                fetch_period_candidates(
                    cur, stats_table=weekly_table, client_id=client_id, report_type=REPORT_TYPE
                ),
                report_type=REPORT_TYPE,
                contract=execution,
                explicit_period=explicit_period,
            )
            period_start_date = period_selection.selected.period_start_date
            period_end_date = period_selection.selected.period_end_date

            summary["selected_period"] = {
                "period_start_date": period_start_date.isoformat(),
                "period_end_date": period_end_date.isoformat(),
            }
            summary["period_selection"] = period_selection.diagnostics()
            summary["rejected_period_candidate_count"] = summary["period_selection"]["rejected_candidate_count"]

            if archive_only:
                if sent_archive_settings is None:
                    raise RuntimeError(
                        "MANUAL_STEP_REQUIRED_MISSING_SENT_FOLDER_CONFIG: "
                        f"{smtp_settings.env_prefix}_IMAP_*")
                rows = _archive_only_rows(
                    cur,
                    send_log_table=send_log_table,
                    client_id=client_id,
                    period_start_date=period_start_date,
                    period_end_date=period_end_date,
                    message_ids=archive_message_ids,
                )
                summary["archive_only_candidate_count"] = len(rows)
                for archive_row in rows:
                    message_id = str(archive_row["smtp_message_id"])
                    mime_bytes = archive_row["sent_mime_bytes"]
                    if isinstance(mime_bytes, memoryview):
                        mime_bytes = mime_bytes.tobytes()
                    try:
                        result = archive_sent_message(
                            settings=sent_archive_settings,
                            message_id=message_id,
                            mime_bytes=bytes(mime_bytes),
                            sent_at=datetime.now(timezone.utc),
                        )
                        mark_sent_archive_result(
                            cur,
                            send_log_table=send_log_table,
                            send_log_id=archive_row["send_log_id"],
                            status=result.status,
                            mailbox=result.mailbox,
                            message_id=result.message_id,
                        )
                        if result.status == "already_present":
                            summary["sent_archive_already_present_count"] += 1
                        else:
                            summary["sent_archive_count"] += 1
                    except Exception as exc:
                        mark_sent_archive_result(
                            cur,
                            send_log_table=send_log_table,
                            send_log_id=archive_row["send_log_id"],
                            status="failed",
                            mailbox=None,
                            message_id=message_id,
                            error_message=str(exc),
                        )
                        summary["sent_archive_failed_count"] += 1
                        if fail_fast:
                            raise
                    finally:
                        conn.commit()
                client.log(
                    "INFO",
                    "SCRIPT",
                    JOB_SOURCE,
                    "Finished Eco Driving Person weekly sent-archive recovery",
                    run_id=run_id,
                    context=summary,
                )
                return

            # The period below is the one the existing weekly job selected. It
            # is handed to the dashboard verbatim; no second resolver exists.
            dashboard = EcoDashboardLinkService(
                settings=dashboard_settings,
                client_id=client_id,
                client_code=str(getattr(cfg, "client_code", "") or ""),
                schema=schema,
                period_type=REPORT_TYPE,
                period_start_date=period_start_date,
                period_end_date=period_end_date,
                send_scope=execution.send_scope,
                mailer=MAILER_ECO_PERSON_WEEKLY,
                read_conn=conn,
                cfg=cfg,
                run_id=run_id,
                logger=lambda level, event, context: client.log(
                    level, "SCRIPT", JOB_SOURCE, event, run_id=run_id,
                    context=dict(context)),
            )
            summary.update(dashboard.summary())

            candidates = _fetch_candidates(
                cur,
                weekly_table=weekly_table,
                driver_chart_table=driver_chart_table,
                client_id=client_id,
                period_start_date=period_start_date,
                period_end_date=period_end_date,
                email_column=email_column,
                driver_columns=driver_columns,
                person_name_group_key=selected_person_name_group_key,
                limit=limit,
            )
            summary["candidates_count"] = len(candidates)
            summary["source_recipient_count"] = sum(
                1 for candidate in candidates if str(candidate.get("recipient_email") or "").strip()
            )
            if not candidates:
                raise RuntimeError(
                    "No Eco Driving Person aggregation rows found for the requested period; "
                    "run the corresponding aggregation dataset before sending emails"
                )

            for row in candidates:
                decision = classify_candidate(
                    row,
                    already_sent=False,
                    force_resend=force_resend,
                )
                if decision.should_send and execution.mode is ExecutionMode.NORMAL_SEND:
                    already_sent = _already_sent(
                        cur,
                        send_log_table=send_log_table,
                        client_id=client_id,
                        person_name_group_key=row["person_name_group_key"],
                        period_start_date=period_start_date,
                        period_end_date=period_end_date,
                    )
                    if already_sent and not force_resend:
                        decision = classify_candidate(
                            row,
                            already_sent=True,
                            force_resend=force_resend,
                        )
                _summary_template_counts(summary, decision.template_type)

                intended_recipient = _trimmed_or_none(row.get("recipient_email"))
                effective_recipient = test_recipient_email or intended_recipient or ""
                original_recipient = intended_recipient
                subject = render_subject(subject_template, row)
                metadata = {
                    "dry_run": dry_run,
                    "execution_mode": execution.mode.value,
                    "force_resend": force_resend,
                    "force_resend_reason": execution.force_resend_reason,
                    "send_scope": execution.send_scope,
                    "period_selection": period_selection.diagnostics(),
                    "decision_reason": decision.reason,
                    "email_column": email_column,
                    "test_recipient_email": test_recipient_email,
                    "original_recipient_email": original_recipient,
                    "qualification_status": row.get("qualification_status"),
                    "ranking_included": row.get("ranking_included"),
                    "template_variant": decision.template_variant,
                }

                if not decision.should_send:
                    summary[decision.status] += 1
                    insert_audit_log(
                        cur,
                        send_log_table=send_log_table,
                        report_type=REPORT_TYPE,
                        run_id=run_id,
                        row=row,
                        decision=decision,
                        recipient_email=effective_recipient,
                        original_recipient_email=original_recipient,
                        subject=subject,
                        status=decision.status,
                        send_scope="skipped",
                        metadata_json=metadata,
                    )
                    continue

                # ELIGIBILITY BEFORE REMOTE EFFECTS. The dashboard step below
                # publishes a snapshot and may ROTATE an expired capability;
                # neither may happen on behalf of a message the Eco send ledger
                # already knows must not be submitted. Both production modes are
                # gated, forced included: force_resend overrides an established
                # `sent`, never an unknown SMTP outcome. `reserve_send` refuses
                # it again below — that is the concurrency-correct refusal; this
                # one is what keeps the refusal free of remote side effects.
                if execution.blocks_on_unresolved_ambiguous_send:
                    unresolved = unresolved_ambiguous_send(
                        cur,
                        send_log_table=send_log_table,
                        subject_column="person_name_group_key",
                        client_id=client_id,
                        subject_value=row.get("person_name_group_key") or "",
                        report_type=REPORT_TYPE,
                        period_start_date=period_start_date,
                        period_end_date=period_end_date,
                    )
                    if unresolved is not None:
                        summary["idempotency_blocked_count"] += 1
                        summary["ambiguous_reconciliation_blocked_count"] += 1
                        summary["skipped_existing_reservation"] += 1
                        insert_audit_log(
                            cur,
                            send_log_table=send_log_table,
                            report_type=REPORT_TYPE,
                            run_id=run_id,
                            row=row,
                            decision=CandidateDecision(
                                status="skipped_existing_reservation",
                                template_type=decision.template_type,
                                template_filename=decision.template_filename,
                                template_variant=decision.template_variant,
                                reason=AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION,
                            ),
                            recipient_email=effective_recipient,
                            original_recipient_email=original_recipient,
                            subject=subject,
                            status="skipped_existing_reservation",
                            send_scope="skipped",
                            parent_send_log_id=unresolved.send_log_id,
                            metadata_json=metadata | {
                                "decision_reason":
                                    AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION,
                                "existing_send_log_id": unresolved.send_log_id,
                                "existing_status": unresolved.status,
                                "dashboard_capability_touched": False,
                            },
                        )
                        client.log(
                            "ERROR",
                            "SCRIPT",
                            JOB_SOURCE,
                            "Eco Driving Person weekly e-mail withheld: an earlier SMTP submission "
                            "for this person and period is unresolved and may "
                            "already have been delivered",
                            run_id=run_id,
                            context={
                                "client_id": client_id,
                                "person_name_group_key": row.get("person_name_group_key"),
                                "period_start_date": period_start_date.isoformat(),
                                "period_end_date": period_end_date.isoformat(),
                                "send_scope": execution.send_scope,
                                "force_resend": force_resend,
                                "dashboard_capability_touched": False,
                                "operator_action_required": True,
                                **unresolved.audit(),
                            },
                        )
                        continue

                template_html = _load_template(template_dir=template_dir, filename=decision.template_filename)
                html_body, dashboard_outcome = render_with_dashboard_link(
                    dashboard,
                    identity_key=str(row.get("person_name_group_key") or ""),
                    # The address this message will ACTUALLY go to. In a test
                    # send that is the test mailbox, and the delivery scope is
                    # 'test', so a test run can never bind the driver's real
                    # delivery to another address.
                    recipient_email=effective_recipient,
                    context=build_template_context(row),
                    template_html=template_html,
                    render=lambda context: render_template(
                        template_html,
                        context,
                        template_variant=decision.template_variant,
                    ),
                )
                metadata.update(dashboard_outcome.audit())
                if html_body is None:
                    # A TECHNICAL dashboard failure. This driver's e-mail carries
                    # no link, so it is not sent at all: a successful-looking Eco
                    # message missing the thing it was supposed to deliver would
                    # be worse than no message. Every other driver continues.
                    summary["failed_count"] += 1
                    summary["failed_before_smtp_count"] += 1
                    summary["dashboard_link_blocked_count"] += 1
                    insert_audit_log(
                        cur,
                        send_log_table=send_log_table,
                        report_type=REPORT_TYPE,
                        run_id=run_id,
                        row=row,
                        decision=decision,
                        recipient_email=effective_recipient,
                        original_recipient_email=original_recipient,
                        subject=subject,
                        status="failed",
                        send_scope="skipped",
                        error_message=dashboard_outcome.failure_code,
                        metadata_json=metadata | dashboard_outcome.audit(),
                    )
                    client.log(
                        "ERROR",
                        "SCRIPT",
                        JOB_SOURCE,
                        "Eco Dashboard link unavailable; weekly e-mail not sent for this person",
                        run_id=run_id,
                        context={
                            "client_id": client_id,
                            "person_name_group_key": row.get("person_name_group_key"),
                            "period_start_date": period_start_date.isoformat(),
                            "period_end_date": period_end_date.isoformat(),
                            **dashboard_outcome.audit(),
                            "fail_fast": fail_fast,
                        },
                    )
                    if fail_fast:
                        raise RuntimeError(
                            "Eco Dashboard link unavailable: "
                            f"{dashboard_outcome.failure_code}"
                        )
                    continue
                summary["rendered_count"] += 1

                if dry_run:
                    summary["would_send_count"] += 1
                    insert_audit_log(
                        cur,
                        send_log_table=send_log_table,
                        report_type=REPORT_TYPE,
                        run_id=run_id,
                        row=row,
                        decision=decision,
                        recipient_email=effective_recipient,
                        original_recipient_email=original_recipient,
                        subject=subject,
                        status="dry_run_rendered",
                        send_scope="dry_run",
                        metadata_json=metadata,
                    )
                    continue

                scope = execution.send_scope
                reservation = reserve_send(
                    cur,
                    send_log_table=send_log_table,
                    report_type=REPORT_TYPE,
                    run_id=run_id,
                    row=row,
                    decision=decision,
                    recipient_email=effective_recipient,
                    original_recipient_email=original_recipient,
                    subject=subject,
                    send_scope=scope,
                    pending_stale_after_minutes=pending_stale_after_minutes,
                    metadata_json=metadata,
                )
                if reservation.outcome == "STALE_PENDING_REQUIRES_RECONCILIATION":
                    summary["stale_pending_count"] += 1
                elif reservation.outcome == AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION:
                    # Reached when the ambiguity appeared after this run's
                    # pre-flight check, or under a forced scope. Either way the
                    # refusal is authoritative and nothing is submitted.
                    summary["ambiguous_reconciliation_blocked_count"] += 1
                if not reservation.reserved:
                    summary["skipped_existing_reservation"] += 1
                    summary["idempotency_blocked_count"] += 1
                    skip_decision = CandidateDecision(
                        status="skipped_existing_reservation",
                        template_type=decision.template_type,
                        template_filename=decision.template_filename,
                        template_variant=decision.template_variant,
                        reason=(reservation.outcome if reservation.outcome == "STALE_PENDING_REQUIRES_RECONCILIATION" else f"existing_{reservation.existing_status}_reservation"),
                    )
                    insert_audit_log(
                        cur,
                        send_log_table=send_log_table,
                        report_type=REPORT_TYPE,
                        run_id=run_id,
                        row=row,
                        decision=skip_decision,
                        recipient_email=effective_recipient,
                        original_recipient_email=original_recipient,
                        subject=subject,
                        status="skipped_existing_reservation",
                        send_scope="skipped",
                        idempotency_key=reservation.idempotency_key,
                        parent_send_log_id=reservation.existing_send_log_id,
                        metadata_json=metadata | {
                            "decision_reason": skip_decision.reason,
                            "existing_send_log_id": reservation.existing_send_log_id,
                            "existing_status": reservation.existing_status,
                        },
                    )
                    continue

                conn.commit()
                summary["smtp_attempt_count"] += 1
                #: The message whose SMTP acceptance is COMMITTED, and which
                #: therefore still owes the sender mailbox a Sent-folder copy.
                #: Set only on the success path, and read only OUTSIDE the
                #: block below — see the note where the copy is filed.
                accepted_message: PreparedEmail | None = None
                try:
                    prepared = _prepare_weekly_email(
                        settings=smtp_settings,
                        recipient_email=effective_recipient,
                        subject=subject,
                        html_body=html_body,
                    )
                    acceptance = submit_prepared_email(
                        settings=smtp_settings,
                        prepared=prepared,
                        session=smtp_session,
                    )
                    mark_send_sent(
                        cur,
                        send_log_table=send_log_table,
                        send_log_id=reservation.send_log_id,
                        smtp_message_id=prepared.message_id,
                        provider_response=acceptance.provider_response,
                        acceptance=acceptance,
                    )
                    mark_sent_mime_preserved(
                        cur,
                        send_log_table=send_log_table,
                        send_log_id=reservation.send_log_id,
                        mime_bytes=prepared.mime_bytes,
                        mime_sha256=prepared.mime_sha256,
                    )
                    conn.commit()
                    summary["sent_count"] += 1
                    summary["smtp_accepted_count"] += 1
                    accepted_message = prepared
                except Exception as exc:
                    # A definite pre-submission refusal produced no message and
                    # stays retryable. An ambiguous result may already be in this
                    # person's inbox, so the reservation is frozen instead — no
                    # automatic run will submit it again until an operator says
                    # what actually happened.
                    ambiguous = classify_exception(exc) == AMBIGUOUS_SUBMISSION
                    if ambiguous:
                        mark_send_ambiguous(
                            cur,
                            send_log_table=send_log_table,
                            send_log_id=reservation.send_log_id,
                            phase=getattr(exc, "phase", "TRANSMIT"),
                            detail=getattr(exc, "operator_detail", str(exc)),
                            error_message=str(exc),
                        )
                    else:
                        mark_send_failed(
                            cur,
                            send_log_table=send_log_table,
                            send_log_id=reservation.send_log_id,
                            error_message=str(exc),
                        )
                    conn.commit()
                    summary["failed_count"] += 1
                    summary["smtp_failed_count"] += 1
                    if ambiguous:
                        summary["smtp_ambiguous_count"] += 1
                    client.log(
                        "ERROR",
                        "SCRIPT",
                        JOB_SOURCE,
                        ("Eco Driving Person weekly notification email result is AMBIGUOUS; operator "
                         "reconciliation required before any resend"
                         if ambiguous else
                         "Eco Driving Person weekly notification email failed"),
                        run_id=run_id,
                        context={
                            "client_id": client_id,
                            "person_name_group_key": row.get("person_name_group_key"),
                            "recipient_email": effective_recipient,
                            "original_recipient_email": original_recipient,
                            "period_start_date": period_start_date.isoformat(),
                            "period_end_date": period_end_date.isoformat(),
                            "smtp_submission_result": classify_exception(exc),
                            "operator_action_required": ambiguous,
                            "send_log_id": reservation.send_log_id,
                            "fail_fast": fail_fast,
                        },
                        error=str(exc),
                    )
                    if fail_fast:
                        raise

                if accepted_message is not None:
                    # A SECOND, SEPARATE EFFECT — DELIBERATELY OUT HERE.
                    # example.invalid has accepted the message and the send log says so,
                    # committed. Filing the copy in the sender's Sent folder is
                    # a different system on a different protocol, and it is run
                    # outside the SMTP `try` above so that NO failure in it can
                    # ever reach the classifier that decides whether a
                    # submission was ambiguous. `store_sent_copy` never raises;
                    # a failed copy is counted, logged and left for an operator.
                    # It is never an authorization to send the customer a second
                    # e-mail.
                    sent_copy = store_sent_copy(
                        settings=sent_archive_settings,
                        message_id=accepted_message.message_id,
                        mime_bytes=accepted_message.mime_bytes,
                        conn=conn,
                        record=_record_sent_copy(
                            cur,
                            send_log_table=send_log_table,
                            send_log_id=reservation.send_log_id,
                        ),
                        client=client,
                        job_source=JOB_SOURCE,
                        run_id=run_id,
                        log_message=("Eco Driving Person weekly Sent-folder copy "
                                     "failed after SMTP acceptance"),
                        log_context={
                            "client_id": client_id,
                            "person_name_group_key": row.get("person_name_group_key"),
                            "period_start_date": period_start_date.isoformat(),
                            "period_end_date": period_end_date.isoformat(),
                            "archive_only_recovery": True,
                        },
                    )
                    count_sent_copy(summary, sent_copy)

            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if smtp_session is not None:
            summary.update(smtp_session.summary())
            smtp_session.close()
        if dashboard is not None:
            summary.update(dashboard.summary())
            dashboard.close()
        conn.close()

    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Eco Driving Person weekly email notifications complete",
        run_id=run_id,
        context=summary,
    )
    return summary
