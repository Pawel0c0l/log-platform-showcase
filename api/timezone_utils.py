import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_BUSINESS_TIMEZONE = "Europe/Warsaw"
BUSINESS_TIMEZONE_ENV = "BUSINESS_TIMEZONE"


def get_business_timezone_name() -> str:
    return os.getenv(BUSINESS_TIMEZONE_ENV, DEFAULT_BUSINESS_TIMEZONE).strip() or DEFAULT_BUSINESS_TIMEZONE


def get_business_timezone() -> ZoneInfo:
    name = get_business_timezone_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid {BUSINESS_TIMEZONE_ENV}: {name!r}") from exc


def now_business_tz() -> datetime:
    return datetime.now(get_business_timezone())


def to_business_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_business_timezone())


def parse_report_local_timestamp(value, tz: str = DEFAULT_BUSINESS_TIMEZONE) -> datetime:
    if value is None:
        raise ValueError("timestamp value is required")

    zone = ZoneInfo(tz)
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("timestamp value is empty")
        parsed = _parse_datetime_text(text)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _parse_datetime_text(text: str) -> datetime:
    normalized = text.replace("T", " ")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    )
    for fmt in formats:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unsupported timestamp format: {text!r}")


def format_business_timestamp(dt: datetime) -> str:
    return to_business_tz(dt).isoformat()


def business_time_log_context(dt: datetime | None = None) -> dict:
    local_dt = to_business_tz(dt or datetime.now(timezone.utc))
    return {
        "timestamp_local": local_dt.isoformat(),
        "timezone": get_business_timezone_name(),
    }


def set_pg_session_timezone(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('TimeZone', %s, false)", (get_business_timezone_name(),))
    return conn
