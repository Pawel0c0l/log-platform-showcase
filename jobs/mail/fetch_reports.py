import csv
import hashlib
import importlib
import imaplib
import io
import json
import math
import os
import re
import tempfile
import unicodedata
import uuid
from urllib.parse import unquote, urlsplit
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header
from email.policy import default as default_policy
from email.utils import parseaddr
from pathlib import Path

import requests

from api.timezone_utils import set_pg_session_timezone
from api.client import ArtifactUploadResult
from jobs.mail.stage1_artifact_sync import (
    NORMALIZED_ROLE,
    RAW_ROLE,
    ROLE_CONFIG,
    Stage1ArtifactReconciliationResult,
    Stage1ArtifactSyncError,
    Stage1ArtifactSyncItemResult,
    Stage1ArtifactSyncOutcome,
    reconcile_stage1_artifacts_batch,
    stage1_artifact_idempotency_key,
)
from jobs.mail.stage1_batch_contract import (
    Stage1BatchError,
    Stage1BatchResult,
    Stage1ItemResult,
    Stage1Outcome,
)
from jobs.reports.date_normalization import (
    ColumnDateStats,
    classify_date_column,
    normalize_date_cell,
)


JOB_SOURCE = "jobs.mail.fetch_reports"
ALLOWED_EXTENSIONS = {".xls", ".xlsx", ".xlsm", ".csv"}
DEFAULT_SENDER_FILTERS: tuple[str, ...] = ()
DEFAULT_REPORTS_DATA_DIR = "/home/logplatform/data/reports"
DEFAULT_IMAP_PORT = 993
DEFAULT_SINCE_DAYS = 30
DEFAULT_MAILBOX = "INBOX"
DEFAULT_CONTENT_DEDUP_MODE = "tables_70_90"
DEFAULT_CONTENT_DEDUP_RANGE_START = 0.70
DEFAULT_CONTENT_DEDUP_RANGE_END = 0.90
DEFAULT_REPORT_LINK_DOMAINS_ALLOWLIST = "telematics-provider.example,fleetmail.telematics-provider.example,mail.example.invalid"
DEFAULT_REPORT_LINK_MAX_BYTES = 104857600
DEFAULT_REPORT_LINK_TIMEOUT_SECS = 60
DEFAULT_REPORT_LINK_VERIFY_TLS = True
DEFAULT_REPORT_LINK_PREFLIGHT = True
DEFAULT_REPORT_LINK_RANGE_SAMPLE_BYTES = 65536
MIN_SEGMENT_ROWS = 10

_TS_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{2}[./-]\d{2}[./-]\d{4}\b"),
    re.compile(r"\b\d{2}:\d{2}(?::\d{2})?\b"),
]
_URL_PATTERN = re.compile(r"https?://[^\s<>'\"`]+", re.IGNORECASE)
_ALLOWED_TEXT_PART_CONTENT_TYPES = {"text/plain", "text/html"}


UUID_SUFFIX_RE = re.compile(
    r"_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\s*-\s*.*$",
    re.IGNORECASE,
)


class HttpRangePreflightError(RuntimeError):
    pass


class ReportDownloadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        expected_bytes: int | None,
        received_bytes: int,
        retry_attempt: int,
        cleanup_result: str,
        retryable: bool = True,
        exception_type: str | None = None,
    ):
        self.expected_bytes = expected_bytes
        self.received_bytes = received_bytes
        self.retry_attempt = retry_attempt
        self.cleanup_result = cleanup_result
        self.retryable = retryable
        self.exception_type = exception_type or type(self).__name__
        super().__init__(message)


def _safe_url_for_log(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "[invalid-url]"
    host = (parsed.hostname or "").lower()
    return f"{parsed.scheme.lower()}://{host}/[path-redacted]" if host else "[invalid-url]"


def _sanitized_exception_message(exc: BaseException, *, limit: int = 500) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    message = re.sub(
        r"https?://[^\s]+",
        lambda match: _safe_url_for_log(match.group(0)),
        message,
        flags=re.IGNORECASE,
    )
    message = re.sub(
        r"(?i)(token|password|secret|signature|sig|key)=([^&\s]+)",
        r"\1=[REDACTED]",
        message,
    )
    return message[:limit]


def _download_host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return (urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None

def _report_key_from_filename(original_filename: str) -> str | None:
    name = os.path.basename((original_filename or "").strip())
    if not name:
        return None
    stem, _ext = os.path.splitext(name)

    # wytnij typowe suffixy z linków: "_<uuid> - 15_lut_2026"
    stem = UUID_SUFFIX_RE.sub("", stem)

    # normalizacja whitespace
    stem = re.sub(r"\s+", " ", stem).strip().lower()
    return stem[:200]

def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out: list[str] = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            if enc:
                out.append(chunk.decode(enc, errors="replace"))
            else:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def _safe_filename(name: str, max_len: int = 140) -> str:
    decoded = (name or "attachment").strip()
    decoded = decoded.replace("/", "_").replace("\\", "_")
    decoded = unicodedata.normalize("NFKC", decoded)
    decoded = re.sub(r"[\x00-\x1f\x7f]+", "_", decoded)
    decoded = re.sub(r"\s+", " ", decoded).strip()
    decoded = re.sub(r"[^\w .()\-]+", "_", decoded, flags=re.UNICODE)
    decoded = decoded.strip(" ._") or "attachment"
    if len(decoded) > max_len:
        base, ext = os.path.splitext(decoded)
        keep_ext = ext[:20]
        keep_base = base[: max_len - len(keep_ext) - 1]
        decoded = f"{keep_base}_{keep_ext}" if keep_ext else keep_base
    return decoded


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _report_link_settings() -> tuple[set[str], int, int, bool, bool, int]:
    raw_allowlist = os.getenv("REPORT_LINK_DOMAINS_ALLOWLIST", DEFAULT_REPORT_LINK_DOMAINS_ALLOWLIST)
    allowset = {
        item.strip().lower().lstrip(".")
        for item in (raw_allowlist or "").split(",")
        if item.strip()
    }
    if not allowset:
        allowset = {
            item.strip().lower().lstrip(".")
            for item in DEFAULT_REPORT_LINK_DOMAINS_ALLOWLIST.split(",")
            if item.strip()
        }

    try:
        max_bytes = int(os.getenv("REPORT_LINK_MAX_BYTES", str(DEFAULT_REPORT_LINK_MAX_BYTES)))
    except ValueError:
        max_bytes = DEFAULT_REPORT_LINK_MAX_BYTES
    max_bytes = max(1, max_bytes)

    try:
        timeout_s = int(os.getenv("REPORT_LINK_TIMEOUT_SECS", str(DEFAULT_REPORT_LINK_TIMEOUT_SECS)))
    except ValueError:
        timeout_s = DEFAULT_REPORT_LINK_TIMEOUT_SECS
    timeout_s = max(1, timeout_s)

    verify_tls = _parse_bool_env("REPORT_LINK_VERIFY_TLS", DEFAULT_REPORT_LINK_VERIFY_TLS)

    preflight = _parse_bool_env("REPORT_LINK_PREFLIGHT", DEFAULT_REPORT_LINK_PREFLIGHT)
    try:
        sample_bytes = int(os.getenv("REPORT_LINK_RANGE_SAMPLE_BYTES", str(DEFAULT_REPORT_LINK_RANGE_SAMPLE_BYTES)))
    except ValueError:
        sample_bytes = DEFAULT_REPORT_LINK_RANGE_SAMPLE_BYTES
    sample_bytes = max(1, sample_bytes)

    return allowset, max_bytes, timeout_s, verify_tls, preflight, sample_bytes


def _extension_from_content_type(content_type: str | None) -> str | None:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in {"text/csv", "application/csv"}:
        return ".csv"
    if ctype in {"application/vnd.ms-excel"}:
        return ".xls"
    if ctype == "application/vnd.ms-excel.sheet.macroenabled.12":
        return ".xlsm"
    if ctype in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
    }:
        return ".xlsx" if "spreadsheetml" in ctype else None
    return None


def _normalize_candidate_filename(
    raw_filename: str | None,
    content_type: str | None,
    *,
    uid: int,
    part_idx: int,
    prefix: str,
) -> str | None:
    base_name = _decode_mime_header(raw_filename or "").strip()
    if not base_name:
        base_name = f"{prefix}-{uid}-{part_idx}.bin"
    ext = os.path.splitext(base_name)[1].lower()
    if ext in ALLOWED_EXTENSIONS:
        return base_name

    guessed_ext = _extension_from_content_type(content_type)
    if guessed_ext in ALLOWED_EXTENSIONS:
        stem = os.path.splitext(base_name)[0] if ext else base_name
        stem = stem or f"{prefix}-{uid}-{part_idx}"
        return f"{stem}{guessed_ext}"

    return None


def _extract_mime_filename(part, uid: int, part_idx: int) -> tuple[str | None, bool]:
    from_filename = _decode_mime_header(part.get_filename() or "")
    if from_filename:
        return from_filename, False

    from_ct_name = _decode_mime_header(part.get_param("name", header="content-type") or "")
    if from_ct_name:
        return from_ct_name, True

    from_cd_name = _decode_mime_header(part.get_param("filename", header="content-disposition") or "")
    if from_cd_name:
        return from_cd_name, True

    return f"attachment-{uid}-{part_idx}.bin", True


def _decode_part_text(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _clean_url_token(url: str) -> str:
    return url.strip().rstrip(").,;\"'<>")


def _extract_urls_from_email(msg) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        if content_type not in _ALLOWED_TEXT_PART_CONTENT_TYPES:
            continue
        text = _decode_part_text(part)
        if not text:
            continue
        for match in _URL_PATTERN.findall(text):
            url = _clean_url_token(match)
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)

    return urls


def _domain_allowed(url: str, allowset: set[str]) -> bool:
    try:
        host = (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if not host:
        return False
    for domain in allowset:
        d = domain.strip().lower().lstrip(".")
        if not d:
            continue
        if host == d or host.endswith(f".{d}"):
            return True
    return False


def _filename_from_content_disposition(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None
    star_match = re.search(r"filename\*\s*=\s*([^;]+)", content_disposition, flags=re.IGNORECASE)
    if star_match:
        value = star_match.group(1).strip().strip('"').strip("'")
        if "''" in value:
            value = value.split("''", 1)[1]
        decoded = unquote(value).strip()
        if decoded:
            return decoded

    plain_match = re.search(r'filename\s*=\s*"([^"]+)"', content_disposition, flags=re.IGNORECASE)
    if plain_match:
        decoded = plain_match.group(1).strip()
        if decoded:
            return decoded

    plain_match = re.search(r"filename\s*=\s*([^;]+)", content_disposition, flags=re.IGNORECASE)
    if plain_match:
        decoded = plain_match.group(1).strip().strip('"').strip("'")
        if decoded:
            return decoded
    return None


def _filename_from_url(url: str) -> str | None:
    try:
        path = urlsplit(url).path or ""
    except ValueError:
        return None
    if not path:
        return None
    name = os.path.basename(path)
    if not name:
        return None
    return unquote(name).strip() or None


def _download_url_to_bytes(
    url: str,
    timeout_s: int,
    max_bytes: int,
    verify_tls: bool,
    *,
    attempts: int = 2,
) -> tuple[bytes, str, str]:
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        part_path: Path | None = None
        expected_bytes: int | None = None
        received_bytes = 0
        cleanup_result = "not_needed"
        retryable = True
        try:
            with tempfile.NamedTemporaryFile(suffix=".part", delete=False) as part:
                part_path = Path(part.name)
                with requests.get(
                    url,
                    stream=True,
                    allow_redirects=True,
                    timeout=timeout_s,
                    verify=verify_tls,
                ) as response:
                    response.raise_for_status()
                    content_type = (
                        response.headers.get("Content-Type") or "application/octet-stream"
                    ).split(";")[0].strip()
                    filename_guess = (
                        _filename_from_content_disposition(response.headers.get("Content-Disposition"))
                        or _filename_from_url(response.url or url)
                        or "report-download"
                    )
                    if not os.path.splitext(filename_guess)[1]:
                        guessed_ext = _extension_from_content_type(content_type)
                        if guessed_ext:
                            filename_guess = f"{filename_guess}{guessed_ext}"

                    raw_content_length = response.headers.get("Content-Length")
                    if raw_content_length:
                        try:
                            parsed_length = int(raw_content_length)
                            if parsed_length >= 0:
                                expected_bytes = parsed_length
                        except ValueError:
                            expected_bytes = None
                    if expected_bytes is not None and expected_bytes > max_bytes:
                        retryable = False
                        raise RuntimeError(
                            f"download exceeds configured byte limit ({expected_bytes} > {max_bytes})"
                        )

                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        received_bytes += len(chunk)
                        if received_bytes > max_bytes:
                            retryable = False
                            raise RuntimeError(
                                f"download exceeds configured byte limit ({received_bytes} > {max_bytes})"
                            )
                        part.write(chunk)
                    part.flush()
                    os.fsync(part.fileno())

            if expected_bytes is not None and received_bytes != expected_bytes:
                raise RuntimeError(
                    f"incomplete response body ({received_bytes} of {expected_bytes} bytes)"
                )
            payload = part_path.read_bytes()
            part_path.unlink()
            cleanup_result = "temporary_removed_after_validation"
            return payload, filename_guess, content_type
        except Exception as exc:
            if part_path is not None and part_path.exists():
                try:
                    part_path.unlink()
                    cleanup_result = "temporary_removed"
                except OSError:
                    cleanup_result = "temporary_cleanup_failed"
            if attempt < attempts and retryable:
                continue
            raise ReportDownloadError(
                _sanitized_exception_message(exc),
                expected_bytes=expected_bytes,
                received_bytes=received_bytes,
                retry_attempt=attempt,
                cleanup_result=cleanup_result,
                retryable=retryable,
                exception_type=type(exc).__name__,
            ) from exc
    raise AssertionError("unreachable download retry state")


def _http_head_meta(
    url: str,
    *,
    timeout_s: int,
    verify_tls: bool,
) -> dict:
    try:
        with requests.head(
            url,
            allow_redirects=True,
            timeout=timeout_s,
            verify=verify_tls,
        ) as response:
            response.raise_for_status()
            headers = response.headers
            content_type = (headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
            content_disposition = headers.get("Content-Disposition")
            filename_guess = (
                _filename_from_content_disposition(content_disposition)
                or _filename_from_url(response.url or url)
                or "report-download"
            )
            if not os.path.splitext(filename_guess)[1]:
                guessed_ext = _extension_from_content_type(content_type)
                if guessed_ext:
                    filename_guess = f"{filename_guess}{guessed_ext}"

            content_length = None
            raw_content_length = headers.get("Content-Length")
            if raw_content_length:
                try:
                    parsed = int(raw_content_length)
                    if parsed > 0:
                        content_length = parsed
                except ValueError:
                    content_length = None

            return {
                "final_url": response.url or url,
                "status_code": int(response.status_code),
                "content_length": content_length,
                "content_type": content_type,
                "accept_ranges": headers.get("Accept-Ranges"),
                "filename_guess": filename_guess,
            }
    except requests.RequestException as exc:
        raise HttpRangePreflightError(f"HEAD request failed: {exc}") from exc


def _http_get_range_sha256(
    url: str,
    *,
    start: int,
    end: int,
    timeout_s: int,
    verify_tls: bool,
) -> str:
    if start < 0 or end < start:
        raise HttpRangePreflightError(f"invalid range {start}-{end}")

    headers = {"Range": f"bytes={start}-{end}"}
    h = hashlib.sha256()
    try:
        with requests.get(
            url,
            stream=True,
            allow_redirects=True,
            timeout=timeout_s,
            verify=verify_tls,
            headers=headers,
        ) as response:
            response.raise_for_status()
            if int(response.status_code) != 206:
                raise HttpRangePreflightError(
                    f"range request returned status={response.status_code}, expected 206"
                )
            for chunk in response.iter_content(chunk_size=8 * 1024):
                if chunk:
                    h.update(chunk)
    except requests.RequestException as exc:
        raise HttpRangePreflightError(f"range request failed: {exc}") from exc
    return h.hexdigest()


def _http_range_fingerprint(
    url: str,
    *,
    sample_bytes: int = 65536,
    timeout_s: int,
    verify_tls: bool,
) -> str:
    try:
        n = int(sample_bytes)
    except (TypeError, ValueError):
        raise HttpRangePreflightError("invalid sample_bytes")
    if n <= 0:
        raise HttpRangePreflightError("sample_bytes must be > 0")

    meta = _http_head_meta(url, timeout_s=timeout_s, verify_tls=verify_tls)
    content_length = meta.get("content_length")
    accept_ranges = (meta.get("accept_ranges") or "").strip().lower()
    effective_url = meta.get("final_url") or url

    if not isinstance(content_length, int) or content_length <= 0:
        raise HttpRangePreflightError("HEAD missing valid Content-Length")
    if "bytes" not in accept_ranges:
        raise HttpRangePreflightError("HEAD missing Accept-Ranges: bytes")

    n = min(n, content_length)
    first_end = min(content_length - 1, n - 1)
    last_start = max(0, content_length - n)

    sha_first = _http_get_range_sha256(
        effective_url,
        start=0,
        end=first_end,
        timeout_s=timeout_s,
        verify_tls=verify_tls,
    )
    sha_last = _http_get_range_sha256(
        effective_url,
        start=last_start,
        end=content_length - 1,
        timeout_s=timeout_s,
        verify_tls=verify_tls,
    )

    return f"{content_length}:{sha_first}:{sha_last}"

def _imap_since_date(days: int) -> str:
    target = datetime.now(timezone.utc) - timedelta(days=days)
    return target.strftime("%d-%b-%Y")


def _parse_internal_date(meta_bytes: bytes | None) -> datetime | None:
    if not meta_bytes:
        return None
    text = meta_bytes.decode("utf-8", errors="ignore")
    m = re.search(r'INTERNALDATE "([^"]+)"', text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def _require_dependency(module_name: str, for_extension: str):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"Missing dependency '{module_name}' required for {for_extension} conversion."
        ) from exc


def _sender_filters_from_env() -> list[str]:
    raw = os.getenv("IMAP_SENDER_FILTERS")
    if raw is None:
        raw = os.getenv("IMAP_SENDER_FILTER")
    if raw:
        filters = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        filters = list(DEFAULT_SENDER_FILTERS)
    seen: set[str] = set()
    out: list[str] = []
    for sender in filters:
        key = sender.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(sender)
    return out


def _imap_search_criteria(since_imap: str, sender_filters: list[str] | None = None) -> list[tuple[str, ...]]:
    filters = [sender.strip() for sender in (sender_filters or []) if sender and sender.strip()]
    if not filters:
        return [("SINCE", since_imap)]
    return [("FROM", f'"{sender}"', "SINCE", since_imap) for sender in filters]


def _describe_imap_search_criteria(criteria: tuple[str, ...]) -> str:
    return " ".join(criteria)


def _search_imap_uids(imap, *, since_imap: str, sender_filters: list[str] | None = None) -> tuple[list[bytes], list[str]]:
    uid_seen: set[bytes] = set()
    uid_list: list[bytes] = []
    criteria_list = _imap_search_criteria(since_imap, sender_filters)
    query_descriptions: list[str] = []
    for criteria in criteria_list:
        query_descriptions.append(_describe_imap_search_criteria(criteria))
        status, search_data = imap.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise RuntimeError(f"IMAP SEARCH failed for criteria={_describe_imap_search_criteria(criteria)}: {status}")
        for uid_value in search_data[0].split() if search_data and search_data[0] else []:
            if uid_value in uid_seen:
                continue
            uid_seen.add(uid_value)
            uid_list.append(uid_value)
    return uid_list, query_descriptions


def _normalize_cell(val) -> str:
    if val is None:
        return ""
    if isinstance(val, float):
        if math.isnan(val):
            return ""
        text = f"{val:.15g}"
        return text.strip()
    text = str(val)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_timestamps(text: str) -> str:
    out = text
    for pattern in _TS_PATTERNS:
        out = pattern.sub("", out)
    return re.sub(r"\s+", " ", out).strip()


# Minimum non-empty cells for a row to be treated as a header when detecting
# recognized date columns. Keeps short metadata rows (e.g. "Data początek:" + a
# single value) from being mistaken for the column header.
_DATE_HEADER_MIN_NON_EMPTY = 3
_DATE_HEADER_MAX_SCAN_ROWS = 200

# Paginated Excel exports (e.g. report_207 split across the 65,536-row sheet
# limit) repeat the column header on every sheet, but continuation sheets begin
# with a long run of data rows so the first repeated header sits far below the
# scan window. When a workbook sheet names no date column of its own, we reuse
# the date columns already confirmed on an earlier sheet of the same workbook,
# but only after sampling the candidate column on this sheet to make sure its
# values really are dates/serials. This keeps a differently shaped continuation
# sheet's numeric column from being reclassified as a date.
_DATE_COLUMN_CONFIRM_SAMPLE = 80
_DATE_COLUMN_CONFIRM_MIN_RATE = 0.6


def _detect_date_columns(
    raw_rows: list[list], date_stats: dict[str, ColumnDateStats]
) -> tuple[int | None, dict[int, tuple[str, object]]]:
    """Find the first header-like row that names at least one date column.

    Returns ``(header_idx, {col_idx: (column_name, DateColumn)})``. Registers a
    :class:`ColumnDateStats` accumulator (keyed by column name) for each detected
    date column so per-column metadata can be collected across sheets.
    """
    scan_limit = min(len(raw_rows), _DATE_HEADER_MAX_SCAN_ROWS)
    for idx in range(scan_limit):
        normalized = [_normalize_cell(value) for value in raw_rows[idx]]
        non_empty_positions = [i for i, value in enumerate(normalized) if value]
        if len(non_empty_positions) < _DATE_HEADER_MIN_NON_EMPTY:
            continue
        date_cols: dict[int, tuple[str, object]] = {}
        for pos in non_empty_positions:
            column = classify_date_column(normalized[pos])
            if column is not None:
                date_cols[pos] = (normalized[pos], column)
        if date_cols:
            for column_name, column in date_cols.values():
                if column_name not in date_stats:
                    date_stats[column_name] = ColumnDateStats(
                        column=column_name,
                        treated_as=column.kind,
                        source=column.source,
                    )
            return idx, date_cols
    return None, {}


def _confirm_inherited_date_columns(
    raw_rows: list[list], inherited_date_cols: dict[int, tuple[str, object]]
) -> dict[int, tuple[str, object]]:
    """Return the inherited date columns whose values on this sheet parse as dates.

    Samples up to ``_DATE_COLUMN_CONFIRM_SAMPLE`` non-empty cells at each inherited
    column position. A column is adopted only when the parse rate clears
    ``_DATE_COLUMN_CONFIRM_MIN_RATE`` so inheritance never turns a continuation
    sheet's unrelated numeric column into a (mis)parsed date column.
    """
    confirmed: dict[int, tuple[str, object]] = {}
    for pos, (column_name, column) in inherited_date_cols.items():
        seen = 0
        parsed = 0
        for raw_row in raw_rows:
            if pos >= len(raw_row):
                continue
            if not _normalize_cell(raw_row[pos]):
                continue
            seen += 1
            if normalize_date_cell(raw_row[pos], column).status == "normalized":
                parsed += 1
            if seen >= _DATE_COLUMN_CONFIRM_SAMPLE:
                break
        if seen and parsed / seen >= _DATE_COLUMN_CONFIRM_MIN_RATE:
            confirmed[pos] = (column_name, column)
    return confirmed


def _normalize_cells_with_dates(
    raw_row: list,
    row_idx: int,
    header_idx: int | None,
    date_cols: dict[int, tuple[str, object]],
    date_stats: dict[str, ColumnDateStats],
    *,
    fallback_fn,
) -> list[str]:
    """Normalize one raw row, canonicalizing recognized date columns.

    Non-date cells (and header / pre-header rows) keep the exact legacy output of
    ``fallback_fn`` so non-date columns are never altered. The fallback differs
    per source: workbook rows use ``_normalize_cell`` (legacy behavior), while CSV
    rows preserve the original cell text verbatim.
    """
    cells: list[str] = []
    for col_idx, value in enumerate(raw_row):
        fallback = fallback_fn(value)
        entry = date_cols.get(col_idx)
        if entry is None or header_idx is None or row_idx <= header_idx:
            cells.append(fallback)
            continue
        column_name, column = entry
        result = normalize_date_cell(value, column)
        date_stats[column_name].record(result, fallback)
        if result.status == "normalized" and result.normalized_text is not None:
            cells.append(result.normalized_text)
        else:
            cells.append(fallback)
    return cells


def _date_normalization_metadata(date_stats: dict[str, ColumnDateStats]) -> dict:
    columns = [stats.as_dict() for stats in date_stats.values()]
    return {
        "columns": columns,
        "total_columns": len(columns),
        "total_unparseable": sum(stats.unparseable for stats in date_stats.values()),
    }


def _detect_csv_text(raw: bytes) -> tuple[str, str]:
    candidates = ["utf-8-sig", "utf-8", "cp1250", "iso-8859-2", "latin2"]
    for enc in candidates:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _detect_csv_dialect(text: str) -> tuple[str, str]:
    allowed_delimiters = [",", ";", "\t", "|"]
    lines = text.splitlines()[:20]
    sample = "\n".join(lines)
    if not sample:
        return ";", '"'

    if len(lines) >= 2:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=allowed_delimiters)
            delimiter = dialect.delimiter if dialect.delimiter in allowed_delimiters else ";"
            quotechar = dialect.quotechar or '"'
            return delimiter, quotechar
        except csv.Error:
            pass

    counts = {delimiter: 0 for delimiter in allowed_delimiters}
    for line in lines:
        for delimiter in allowed_delimiters:
            counts[delimiter] += line.count(delimiter)

    delimiter = max(allowed_delimiters, key=lambda d: counts[d])
    quotechar = '"'
    return delimiter, quotechar


def _df_segments(df) -> list[tuple[int, int]]:
    if df is None or df.empty:
        return []

    row_counts: list[int] = []
    for _, row in df.iterrows():
        count = 0
        for value in row.tolist():
            if _normalize_cell(value):
                count += 1
        row_counts.append(count)

    max_non_empty = max(row_counts, default=0)
    if max_non_empty <= 0:
        return []

    threshold = max(2, math.ceil(0.5 * max_non_empty))
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for idx, count in enumerate(row_counts):
        if count >= threshold:
            if start is None:
                start = idx
            continue
        if start is not None:
            if idx - start >= MIN_SEGMENT_ROWS:
                segments.append((start, idx))
            start = None

    if start is not None and len(row_counts) - start >= MIN_SEGMENT_ROWS:
        segments.append((start, len(row_counts)))

    return segments


def _segment_slice_70_90(start: int, end: int, range_start: float, range_end: float) -> tuple[int, int]:
    n = end - start
    if n <= 0:
        return start, start

    a = start + int(math.floor(range_start * n))
    b = start + int(math.floor(range_end * n))
    a = max(start, min(a, end))
    b = max(start, min(b, end))

    if b <= a:
        b = min(end, a + 1)

    if b - a < MIN_SEGMENT_ROWS:
        b = min(end, start + min(n, 50))
        a = start + min(max(0, int(0.4 * n)), max(0, (b - start) - 20))
        if n >= MIN_SEGMENT_ROWS and b - a < MIN_SEGMENT_ROWS:
            a = max(start, b - MIN_SEGMENT_ROWS)
        if b <= a:
            a = start
            b = min(end, start + 1)

    return a, b


def _fingerprint_segment(df, a: int, b: int) -> str:
    lines: list[str] = []
    for row in df.iloc[a:b].itertuples(index=False, name=None):
        parts = [_normalize_cell(value) for value in row]
        line = _strip_timestamps("\t".join(parts))
        if line:
            lines.append(line)
    payload = "\n".join(lines)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fingerprint_df_fallback(df) -> str:
    lines: list[str] = []
    for row in df.itertuples(index=False, name=None):
        parts = [_normalize_cell(value) for value in row]
        line = _strip_timestamps("\t".join(parts))
        if line:
            lines.append(line)
    payload = "\n".join(lines)
    if len(payload) > 2000:
        payload = payload[:2000]
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compute_content_fingerprint_xls(raw: bytes, ext: str, range_start: float, range_end: float) -> tuple[str, dict]:
    pandas = _require_dependency("pandas", ext)
    engine = "openpyxl" if ext in {".xlsx", ".xlsm"} else "xlrd"
    if ext in {".xlsx", ".xlsm"}:
        _require_dependency("openpyxl", ext)
    if ext == ".xls":
        _require_dependency("xlrd", ext)

    sheets = pandas.read_excel(io.BytesIO(raw), engine=engine, sheet_name=None)
    if not isinstance(sheets, dict):
        sheets = {"Sheet1": sheets}

    segment_fingerprints: list[str] = []
    fallback_sheets = 0
    segments_total = 0

    for _, df in sheets.items():
        if df is None or df.empty:
            continue
        segments = _df_segments(df)
        if not segments:
            segment_fingerprints.append(_fingerprint_df_fallback(df))
            fallback_sheets += 1
            continue

        for start, end in segments:
            a, b = _segment_slice_70_90(start, end, range_start, range_end)
            segment_fingerprints.append(_fingerprint_segment(df, a, b))
            segments_total += 1

    if not segment_fingerprints:
        raise RuntimeError("Unable to compute content fingerprint: no non-empty sheet segments")

    report_payload = "\n".join(sorted(segment_fingerprints))
    report_fp = hashlib.sha256(report_payload.encode("utf-8")).hexdigest()
    debug = {
        "engine": engine,
        "sheets": len(sheets),
        "segments": segments_total,
        "fallback_sheets": fallback_sheets,
    }
    return report_fp, debug


def _compute_content_fingerprint_csv(raw: bytes) -> tuple[str, dict]:
    text, encoding = _detect_csv_text(raw)
    delimiter, quotechar = _detect_csv_dialect(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, quotechar=quotechar)

    lines: list[str] = []
    for row in reader:
        norm_row = [_normalize_cell(cell) for cell in row]
        line = _strip_timestamps("\t".join(norm_row))
        if line:
            lines.append(line)

    payload = "\n".join(lines)
    fp = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    debug = {
        "encoding": encoding,
        "delimiter": delimiter,
        "rows": len(lines),
    }
    return fp, debug


def _content_dedup_settings() -> tuple[str, float, float]:
    mode = (os.getenv("CONTENT_DEDUP_MODE", DEFAULT_CONTENT_DEDUP_MODE) or "").strip().lower()
    if mode not in {"off", "tables_70_90"}:
        mode = DEFAULT_CONTENT_DEDUP_MODE

    try:
        range_start = float(os.getenv("CONTENT_DEDUP_RANGE_START", str(DEFAULT_CONTENT_DEDUP_RANGE_START)))
    except ValueError:
        range_start = DEFAULT_CONTENT_DEDUP_RANGE_START

    try:
        range_end = float(os.getenv("CONTENT_DEDUP_RANGE_END", str(DEFAULT_CONTENT_DEDUP_RANGE_END)))
    except ValueError:
        range_end = DEFAULT_CONTENT_DEDUP_RANGE_END

    range_start = max(0.0, min(1.0, range_start))
    range_end = max(0.0, min(1.0, range_end))
    if range_end <= range_start:
        range_start = DEFAULT_CONTENT_DEDUP_RANGE_START
        range_end = DEFAULT_CONTENT_DEDUP_RANGE_END

    return mode, range_start, range_end


def _compute_content_fingerprint(raw: bytes, ext: str, mode: str, range_start: float, range_end: float) -> tuple[str | None, str | None, dict]:
    if mode == "off":
        return None, None, {}

    ext = ext.lower()
    if ext in {".xls", ".xlsx", ".xlsm"}:
        fp, debug = _compute_content_fingerprint_xls(raw, ext, range_start, range_end)
        return fp, mode, debug
    if ext == ".csv":
        fp, debug = _compute_content_fingerprint_csv(raw)
        return fp, "csv_text_v1", debug

    return None, None, {}


def _normalize_csv_bytes_to_canonical(
    raw: bytes, out_path: Path, *, date_stats: dict[str, ColumnDateStats] | None = None
) -> dict[str, ColumnDateStats]:
    text, _ = _detect_csv_text(raw)
    input_delimiter, input_quotechar = _detect_csv_dialect(text)

    reader = csv.reader(io.StringIO(text), delimiter=input_delimiter, quotechar=input_quotechar)
    raw_rows = [list(row) for row in reader]

    if date_stats is None:
        date_stats = {}
    header_idx, date_cols = _detect_date_columns(raw_rows, date_stats)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        for row_idx, raw_row in enumerate(raw_rows):
            if date_cols:
                writer.writerow(
                    _normalize_cells_with_dates(
                        raw_row,
                        row_idx,
                        header_idx,
                        date_cols,
                        date_stats,
                        fallback_fn=lambda value: value,
                    )
                )
            else:
                writer.writerow(raw_row)
    return date_stats


def _canonical_worksheet_rows(
    rows,
    *,
    date_stats: dict[str, ColumnDateStats] | None = None,
    inherited_date_cols: dict[int, tuple[str, object]] | None = None,
) -> tuple[list[list[str]], dict[int, tuple[str, object]]]:
    raw_rows = [list(row) for row in rows]
    if date_stats is None:
        date_stats = {}
    header_idx, date_cols = _detect_date_columns(raw_rows, date_stats)

    if date_cols and inherited_date_cols and header_idx is not None and header_idx > 0:
        # An earlier sheet already established these date columns, so the rows
        # above this sheet's first repeated header are continuation data (a
        # page split mid-table). Normalize them too; the shared normalizer
        # preserves non-date/header/metadata text untouched.
        header_idx = -1
    elif not date_cols and inherited_date_cols:
        confirmed = _confirm_inherited_date_columns(raw_rows, inherited_date_cols)
        if confirmed:
            date_cols = confirmed
            # No header on this continuation sheet: normalize every row. The
            # column was already registered in date_stats by the originating
            # sheet, so reuse that accumulator.
            header_idx = -1
            for column_name, column in date_cols.values():
                if column_name not in date_stats:
                    date_stats[column_name] = ColumnDateStats(
                        column=column_name,
                        treated_as=column.kind,
                        source=column.source,
                    )

    out: list[list[str]] = []
    sheet_started = False
    pending_empty_rows: list[list[str]] = []

    for row_idx, raw_row in enumerate(raw_rows):
        if date_cols:
            cells = _normalize_cells_with_dates(
                raw_row,
                row_idx,
                header_idx,
                date_cols,
                date_stats,
                fallback_fn=_normalize_cell,
            )
        else:
            cells = [_normalize_cell(value) for value in raw_row]
        if not any(cell != "" for cell in cells):
            if sheet_started:
                pending_empty_rows.append(cells)
            continue

        if pending_empty_rows:
            out.extend(pending_empty_rows)
            pending_empty_rows = []
        out.append(cells)
        sheet_started = True

    return out, date_cols


def _write_workbook_sheets_to_canonical_csv(
    sheets, out_path: Path, *, ext_label: str, date_stats: dict[str, ColumnDateStats] | None = None
) -> dict[str, ColumnDateStats]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    non_empty_sheets_written = 0
    if date_stats is None:
        date_stats = {}
    workbook_date_cols: dict[int, tuple[str, object]] | None = None

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        for _sheet_name, sheet_rows in sheets:
            rows, sheet_date_cols = _canonical_worksheet_rows(
                sheet_rows, date_stats=date_stats, inherited_date_cols=workbook_date_cols
            )
            if sheet_date_cols and workbook_date_cols is None:
                workbook_date_cols = sheet_date_cols
            if not rows:
                continue
            if non_empty_sheets_written > 0:
                writer.writerow([])
            writer.writerows(rows)
            non_empty_sheets_written += 1

    if non_empty_sheets_written == 0:
        raise RuntimeError(f"No non-empty worksheets in {ext_label.upper()} file")
    return date_stats


def _openpyxl_to_canonical_csv(raw: bytes, out_path: Path, *, ext: str) -> dict[str, ColumnDateStats]:
    openpyxl = _require_dependency("openpyxl", ext)
    workbook = openpyxl.load_workbook(
        filename=io.BytesIO(raw),
        read_only=True,
        data_only=True,
        keep_vba=False,
    )
    try:
        sheet_names = list(workbook.sheetnames)
        if not sheet_names:
            raise RuntimeError(f"No worksheets in {ext.upper()} file")

        def iter_sheet_rows(sheet_name: str):
            worksheet = workbook[sheet_name]
            calculate_dimension = getattr(worksheet, "calculate_dimension", None)
            dimension = calculate_dimension() if calculate_dimension is not None else ""
            reset_dimensions = getattr(worksheet, "reset_dimensions", None)
            if reset_dimensions is not None and dimension in {"A1", "A1:A1"}:
                reset_dimensions()
            return worksheet.iter_rows(values_only=True)

        return _write_workbook_sheets_to_canonical_csv(
            ((sheet_name, iter_sheet_rows(sheet_name)) for sheet_name in sheet_names),
            out_path,
            ext_label=ext,
        )
    finally:
        workbook.close()


def _xlsx_to_canonical_csv(raw: bytes, out_path: Path) -> dict[str, ColumnDateStats]:
    return _openpyxl_to_canonical_csv(raw, out_path, ext=".xlsx")


def _xlsm_to_canonical_csv(raw: bytes, out_path: Path) -> dict[str, ColumnDateStats]:
    """Normalize a macro-enabled workbook (.xlsm) to a single canonical CSV.

    Earlier versions emitted only the ``LOG`` worksheet. That hid header rows
    that live in sibling sheets (e.g. ALPHA00001 ``GPS_baza_START_skrypt.xlsm``
    keeps the Stage 2 detection header on the ``GPS_baza_START`` sheet, the
    cleaning-output header on ``LOG``, and the cleaning-stop header on
    ``Status_Prywatnosci``). Stage 2 detectors and cleaners (e.g.
    ``AlphaGPSBazaLog``) expect all of those header rows to be reachable in the
    same normalized CSV.

    We now write every non-empty worksheet into the same CSV in workbook order,
    separated by a single empty row. The output retains the original semicolon
    delimiter and ``utf-8-sig`` encoding so the rest of the Workflow B pipeline
    is unaffected.
    """

    return _openpyxl_to_canonical_csv(raw, out_path, ext=".xlsm")


def _xls_to_canonical_csv(raw: bytes, out_path: Path) -> dict[str, ColumnDateStats]:
    xlrd = _require_dependency("xlrd", ".xls")
    workbook = xlrd.open_workbook(file_contents=raw)
    try:
        # xlrd reports date cells as plain Excel serial floats; recognized date
        # columns are canonicalized via the serial path in the shared normalizer.
        return _write_workbook_sheets_to_canonical_csv(
            (
                (
                    sheet.name,
                    (sheet.row_values(row_idx) for row_idx in range(sheet.nrows)),
                )
                for sheet in workbook.sheets()
            ),
            out_path,
            ext_label=".xls",
        )
    finally:
        release_resources = getattr(workbook, "release_resources", None)
        if release_resources is not None:
            release_resources()


def _convert_to_canonical_csv(raw: bytes, ext: str, out_path: Path) -> dict:
    ext = ext.lower()
    if ext == ".csv":
        date_stats = _normalize_csv_bytes_to_canonical(raw, out_path)
    elif ext == ".xlsx":
        date_stats = _xlsx_to_canonical_csv(raw, out_path)
    elif ext == ".xlsm":
        date_stats = _xlsm_to_canonical_csv(raw, out_path)
    elif ext == ".xls":
        date_stats = _xls_to_canonical_csv(raw, out_path)
    else:
        raise ValueError(f"Unsupported extension: {ext}")
    return _date_normalization_metadata(date_stats)


def _atomic_write_bytes(final_path: Path, payload: bytes) -> bool:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.part")
    try:
        with part_path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if final_path.exists():
            if hashlib.sha256(final_path.read_bytes()).digest() != hashlib.sha256(payload).digest():
                raise RuntimeError("existing source path has incompatible bytes")
            part_path.unlink()
            return False
        os.replace(part_path, final_path)
        return True
    finally:
        if part_path.exists():
            part_path.unlink()


def _atomic_normalize(payload: bytes, ext: str, final_path: Path) -> tuple[dict, bool]:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.part")
    try:
        metadata = _convert_to_canonical_csv(payload, ext, part_path)
        if final_path.exists():
            if hashlib.sha256(final_path.read_bytes()).digest() != hashlib.sha256(part_path.read_bytes()).digest():
                raise RuntimeError("existing normalized path has incompatible bytes")
            part_path.unlink()
            return metadata, False
        os.replace(part_path, final_path)
        return metadata, True
    finally:
        if part_path.exists():
            part_path.unlink()


def _cleanup_message_paths(paths: list[Path]) -> str:
    removed = 0
    failed = 0
    for path in reversed(paths):
        try:
            if path.exists():
                path.unlink()
                removed += 1
        except OSError:
            failed += 1
    return f"removed={removed},failed={failed}"


def _stage1_failure_context(
    *,
    run_id: str,
    uid: int | None,
    message_identity: str | None,
    raw_file_id: str | None,
    download_url: str | None,
    exc: BaseException,
    retryable: bool,
    cleanup_result: str,
    retry_attempt: int | None = None,
    expected_bytes: int | None = None,
    received_bytes: int | None = None,
    report_type: str | None = None,
    client_code: str | None = None,
) -> dict:
    return {
        "run_id": run_id,
        "imap_uid": uid,
        "message_identity": message_identity,
        "client_code": client_code,
        "report_type": report_type,
        "raw_file_id": raw_file_id,
        "download_host": _download_host(download_url),
        "exception_type": getattr(exc, "exception_type", type(exc).__name__),
        "exception_message": _sanitized_exception_message(exc),
        "expected_bytes": expected_bytes,
        "received_bytes": received_bytes,
        "retry_attempt": retry_attempt,
        "retryable": retryable,
        "transaction_scope": "imap_message",
        "cleanup_result": cleanup_result,
    }


def _collect_mime_file_candidates(msg, uid: int) -> tuple[list[dict], dict]:
    candidates: list[dict] = []
    stats = {
        "attachments_seen": 0,
        "attachments_supported": 0,
        "attachments_unsupported": 0,
        "attachments_missing_filename_fallback": 0,
        "attachment_filenames_seen": [],
        "supported_attachment_filenames": [],
        "unsupported_attachment_filenames": [],
    }

    part_idx = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload:
            continue

        raw_filename, fallback_used = _extract_mime_filename(part, uid=uid, part_idx=part_idx)
        content_type = part.get_content_type() or "application/octet-stream"
        display_filename = _decode_mime_header(raw_filename or "").strip() or f"attachment-{uid}-{part_idx}.bin"
        stats["attachments_seen"] += 1
        stats["attachment_filenames_seen"].append(display_filename)
        normalized_filename = _normalize_candidate_filename(
            raw_filename,
            content_type,
            uid=uid,
            part_idx=part_idx,
            prefix="attachment",
        )
        part_idx += 1
        if not normalized_filename:
            stats["attachments_unsupported"] += 1
            stats["unsupported_attachment_filenames"].append(display_filename)
            continue

        stats["attachments_supported"] += 1
        stats["supported_attachment_filenames"].append(normalized_filename)
        if fallback_used:
            stats["attachments_missing_filename_fallback"] += 1

        candidates.append(
            {
                "raw_filename": normalized_filename,
                "payload": payload,
                "content_type": content_type,
                "source": "mime",
                "url": None,
            }
        )

    return candidates, stats


def _collect_link_file_candidates(
    msg,
    *,
    subject: str,
    uid: int,
    allowset: set[str],
) -> tuple[list[dict], dict, list[dict]]:
    candidates: list[dict] = []
    events: list[dict] = []
    urls = _extract_urls_from_email(msg)
    allowed_urls = [url for url in urls if _domain_allowed(url, allowset)]
    blocked_urls = [url for url in urls if url not in allowed_urls]
    stats = {
        "link_urls_found": len(urls),
        "link_urls_allowed": len(allowed_urls),
    }

    for url in blocked_urls:
        try:
            link_path = urlsplit(url).path or "/"
        except ValueError:
            link_path = "[invalid-path]"
        cancel_email = "cancelemail" in link_path.lower()
        events.append(
            {
                "level": "INFO" if cancel_email else "WARNING",
                "message": (
                    "Expected cancelEmail link skipped by allowlist"
                    if cancel_email else "Report link blocked by allowlist"
                ),
                "expected_skip": True,
                "context": {
                    "uid": uid,
                    "subject": subject,
                    "download_host": _download_host(url),
                    "link_kind": "cancelEmail" if cancel_email else "blocked_report_link",
                    "skip_reason": "url_allowlist",
                    "cancel_email": cancel_email,
                },
            }
        )

    for idx, url in enumerate(allowed_urls):
        filename_guess = _filename_from_url(url) or f"link-{uid}-{idx}.xlsx"
        ext = os.path.splitext(filename_guess)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            filename_guess = f"link-{uid}-{idx}.xlsx"

        candidates.append(
            {
                "raw_filename": _safe_filename(filename_guess),
                "content_type": "application/octet-stream",
                "source": "link",
                "url": url,
                "payload": None,
            }
        )

    return candidates, stats, events


def _insert_raw_file_new(
    cur,
    *,
    imap_message_id: str,
    account: str,
    sha256: str,
    raw_filename: str,
    content_type: str,
    size_bytes: int,
    raw_path: str,
    report_key: str | None,
    content_fingerprint: str | None,
    dedup_basis: str | None,
    http_range_fp: str | None,
) -> str | None:
    cur.execute(
        """
        INSERT INTO ingest.raw_file (
          imap_message_id, account, sha256, original_filename,
          content_type, size_bytes, raw_path, normalized_csv_path,
          status, error, report_key, content_fingerprint, dedup_basis, http_range_fp,
          persisted, duplicate_of_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, 'NEW', NULL, %s, %s, %s, %s, TRUE, NULL)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (
            imap_message_id,
            account,
            sha256,
            raw_filename,
            content_type,
            size_bytes,
            raw_path,
            report_key,
            content_fingerprint,
            dedup_basis,
            http_range_fp,
        ),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def _select_raw_file_by_sha(cur, *, account: str, sha256: str) -> tuple[str, str, str | None] | None:
    cur.execute(
        """
        SELECT id, status, duplicate_of_id
        FROM ingest.raw_file
        WHERE account=%s AND sha256=%s
        LIMIT 1
        """,
        (account, sha256),
    )
    row = cur.fetchone()
    if not row:
        return None
    rid, status, duplicate_of_id = row
    return str(rid), str(status), (str(duplicate_of_id) if duplicate_of_id else None)


def _select_raw_file_by_http_range_fp(cur, *, account: str, http_range_fp: str) -> tuple[str, str] | None:
    cur.execute(
        """
        SELECT id, sha256
        FROM ingest.raw_file
        WHERE account=%s AND http_range_fp=%s
          AND status <> 'DUPLICATE_CONTENT'
        ORDER BY id
        LIMIT 1
        """,
        (account, http_range_fp),
    )
    row = cur.fetchone()
    if not row:
        return None
    rid, sha256 = row
    return str(rid), str(sha256)



def _select_canonical_by_key(
    cur,
    *,
    account: str,
    report_key: str,
    content_fingerprint: str,
) -> tuple[str, str, str] | None:
    cur.execute(
        """
        SELECT id, sha256, status
        FROM ingest.raw_file
        WHERE account=%s AND report_key=%s AND content_fingerprint=%s
          AND status <> 'DUPLICATE_CONTENT'
        ORDER BY
          CASE status
            WHEN 'NORMALIZED' THEN 0
            WHEN 'NEW' THEN 1
            WHEN 'FAILED' THEN 2
            ELSE 9
          END,
          id
        LIMIT 1
        """,
        (account, report_key, content_fingerprint),
    )
    row = cur.fetchone()
    if not row:
        return None
    rid, sha256, status = row
    return str(rid), str(sha256), str(status)


def _insert_raw_file_duplicate(
    cur,
    *,
    imap_message_id: str,
    account: str,
    sha256: str,
    raw_filename: str,
    content_type: str,
    size_bytes: int,
    report_key: str | None,
    content_fingerprint: str | None,
    dedup_basis: str | None,
    canonical_id: str,
    http_range_fp: str | None,
) -> str | None:
    cur.execute(
        """
        INSERT INTO ingest.raw_file (
          imap_message_id, account, sha256, original_filename,
          content_type, size_bytes, raw_path, normalized_csv_path,
          status, error, report_key, content_fingerprint, dedup_basis, http_range_fp,
          persisted, duplicate_of_id
        ) VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, 'DUPLICATE_CONTENT', NULL, %s, %s, %s, %s, FALSE, %s)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (
            imap_message_id,
            account,
            sha256,
            raw_filename,
            content_type,
            size_bytes,
            report_key,
            content_fingerprint,
            dedup_basis,
            http_range_fp,
            canonical_id,
        ),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def _persist_raw_file_candidate(
    cur,
    *,
    candidate: dict | None,
    imap_message_id: str,
    account: str,
    sha256: str,
    raw_filename: str,
    content_type: str,
    size_bytes: int,
    raw_path: str,
    report_key: str | None,
    content_fingerprint: str | None,
    dedup_basis: str | None,
) -> dict:
    http_range_fp = (candidate or {}).get("http_range_fp")
    new_id = _insert_raw_file_new(
        cur,
        imap_message_id=imap_message_id,
        account=account,
        sha256=sha256,
        raw_filename=raw_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        raw_path=raw_path,
        report_key=report_key,
        content_fingerprint=content_fingerprint,
        dedup_basis=dedup_basis,
        http_range_fp=http_range_fp,
    )
    if new_id:
        return {"action": "NEW", "raw_file_id": new_id}

    existing_by_sha = _select_raw_file_by_sha(cur, account=account, sha256=sha256)
    if existing_by_sha:
        existing_id, existing_status, duplicate_of_id = existing_by_sha
        return {
            "action": "SKIP_EXISTING_SHA",
            "raw_file_id": existing_id,
            "existing_status": existing_status,
            "duplicate_of_id": duplicate_of_id,
        }

    if report_key and content_fingerprint:
        canonical = _select_canonical_by_key(
            cur,
            account=account,
            report_key=report_key,
            content_fingerprint=content_fingerprint,
        )
        if canonical:
            canonical_id, canonical_sha256, canonical_status = canonical
            duplicate_id = _insert_raw_file_duplicate(
                cur,
                imap_message_id=imap_message_id,
                account=account,
                sha256=sha256,
                raw_filename=raw_filename,
                content_type=content_type,
                size_bytes=size_bytes,
                report_key=report_key,
                content_fingerprint=content_fingerprint,
                dedup_basis=dedup_basis,
                canonical_id=canonical_id,
                http_range_fp=http_range_fp,
            )
            if duplicate_id:
                return {
                    "action": "DUPLICATE_CONTENT",
                    "raw_file_id": duplicate_id,
                    "canonical_id": canonical_id,
                    "canonical_sha256": canonical_sha256,
                    "canonical_status": canonical_status,
                }

            # race condition: another transaction could insert by sha between checks
            existing_by_sha = _select_raw_file_by_sha(cur, account=account, sha256=sha256)
            if existing_by_sha:
                existing_id, existing_status, duplicate_of_id = existing_by_sha
                return {
                    "action": "SKIP_EXISTING_SHA",
                    "raw_file_id": existing_id,
                    "existing_status": existing_status,
                    "duplicate_of_id": duplicate_of_id,
                }

            return {
                "action": "UNRESOLVED",
                "reason": "duplicate_insert_conflict_without_visible_row",
                "canonical_id": canonical_id,
            }

    return {"action": "SKIP_CONFLICT_NO_CANONICAL"}


def _artifact_exists_for_raw_file(cur, *, raw_file_id: str, artifact_role: str) -> tuple[str, str] | None:
    cur.execute(
        """
        SELECT artifact_id::text, sha256
        FROM artifacts
        WHERE raw_file_id = %s
          AND workflow_name = 'workflow_b'
          AND stage_name = 'stage_1_fetch'
          AND artifact_role = %s
        LIMIT 1
        """,
        (raw_file_id, artifact_role),
    )
    row = cur.fetchone()
    return (str(row[0]), str(row[1])) if row else None


def _upload_stage1_artifact_if_needed(
    client,
    cur,
    *,
    path: Path,
    run_id: str,
    raw_file_id: str,
    artifact_role: str,
    original_filename: str,
    source_sha256: str,
    metadata: dict | None = None,
) -> ArtifactUploadResult:
    existing = _artifact_exists_for_raw_file(cur, raw_file_id=raw_file_id, artifact_role=artifact_role)
    if existing:
        existing_id, existing_sha256 = existing
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != existing_sha256:
            response = requests.Response()
            response.status_code = 409
            raise requests.HTTPError("Existing Stage 1 artifact has incompatible bytes", response=response)
        return ArtifactUploadResult(existing_id, "reused")
    version, scope = ROLE_CONFIG[artifact_role]
    key = stage1_artifact_idempotency_key(raw_file_id, source_sha256, artifact_role)
    return client.upload_artifact(
        str(path),
        kind="REPORT",
        run_id=run_id,
        raw_file_id=raw_file_id,
        workflow_name="workflow_b",
        stage_name="stage_1_fetch",
        artifact_role=artifact_role,
        report_type="unknown",
        original_filename=original_filename or path.name,
        metadata=metadata,
        idempotency_scope=scope,
        idempotency_key=key,
        structured_response=True,
    )


def _upload_stage1_artifacts(client, cur, *, run_id: str, uploads: list[dict], counters: dict) -> Stage1ArtifactReconciliationResult:
    record_count = len({u["raw_file_id"] for u in uploads})
    result = Stage1ArtifactReconciliationResult(
        records_discovered=record_count,
        records_inspected=record_count,
    )
    for upload in uploads:
        role = upload["artifact_role"]
        version, _ = ROLE_CONFIG[role]
        source_sha256 = str(upload["source_sha256"])
        key_short = stage1_artifact_idempotency_key(upload["raw_file_id"], source_sha256, role)[:12]
        try:
            response = _upload_stage1_artifact_if_needed(
                client,
                cur,
                path=upload["path"],
                run_id=run_id,
                raw_file_id=upload["raw_file_id"],
                artifact_role=upload["artifact_role"],
                original_filename=upload["original_filename"],
                source_sha256=source_sha256,
                metadata=upload.get("metadata"),
            )
            if response.idempotency_status == "reused":
                counters["artifact_upload_skipped_existing"] += 1
                result.items.append(Stage1ArtifactSyncItemResult(
                    upload["raw_file_id"], role, Stage1ArtifactSyncOutcome.REUSED,
                    artifact_id=response.artifact_id, idempotency_status="reused",
                    idempotency_digest_short=key_short, contract_version=version,
                ))
                client.log(
                    "INFO",
                    "SCRIPT",
                    JOB_SOURCE,
                    "Stage 1 artifact upload skipped; artifact already exists",
                    run_id=run_id,
                    context={
                        "uid": upload["uid"],
                        "raw_file_id": upload["raw_file_id"],
                        "artifact_role": upload["artifact_role"],
                        "path": str(upload["path"]),
                    },
                )
                continue
            result.items.append(Stage1ArtifactSyncItemResult(
                upload["raw_file_id"], role, Stage1ArtifactSyncOutcome.CREATED,
                artifact_id=response.artifact_id, idempotency_status="created",
                idempotency_digest_short=key_short, contract_version=version,
            ))
            if role == "raw":
                counters["artifacts_raw_uploaded"] += 1
            elif role == "normalized":
                counters["artifacts_normalized_uploaded"] += 1
            client.log(
                "INFO",
                "SCRIPT",
                JOB_SOURCE,
                "Stage 1 artifact uploaded",
                run_id=run_id,
                context={
                    "uid": upload["uid"],
                    "artifact_id": response.artifact_id,
                    "raw_file_id": upload["raw_file_id"],
                    "artifact_role": upload["artifact_role"],
                    "path": str(upload["path"]),
                },
            )
        except Exception as exc:
            counters["artifact_upload_failed"] += 1
            conflict = isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 409
            outcome = (
                Stage1ArtifactSyncOutcome.FAILED_NON_RETRYABLE_CONFLICT
                if conflict else Stage1ArtifactSyncOutcome.FAILED_RETRYABLE_UPLOAD
            )
            result.items.append(Stage1ArtifactSyncItemResult(
                upload["raw_file_id"], role, outcome,
                retryable=not conflict, operator_action_required=conflict,
                idempotency_digest_short=key_short, contract_version=version,
                error_category="idempotency_conflict" if conflict else "artifact_upload_failure",
            ))
            client.log(
                "WARNING",
                "SCRIPT",
                JOB_SOURCE,
                "Stage 1 artifact upload failed; local ingest file remains persisted",
                run_id=run_id,
                context={
                    "run_id": run_id,
                    "imap_uid": upload["uid"],
                    "message_identity": upload.get("message_identity"),
                    "client_code": None,
                    "report_type": None,
                    "raw_file_id": upload["raw_file_id"],
                    "download_host": None,
                    "artifact_role": upload["artifact_role"],
                    "exception_type": type(exc).__name__,
                    "exception_message": _sanitized_exception_message(exc),
                    "error": _sanitized_exception_message(exc),
                    "expected_bytes": None,
                    "received_bytes": None,
                    "retry_attempt": 2,
                    "retryable": not conflict,
                    "transaction_scope": "artifact_reconciliation",
                    "cleanup_result": "durable_local_source_retained",
                    "idempotency_digest_short": key_short,
                },
            )
    if any(item.retryable or item.operator_action_required for item in result.items):
        raise Stage1ArtifactSyncError(result)
    return result


def _pg_conn():
    psycopg = _require_dependency("psycopg", "Postgres connection")
    dsn = (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn))


def _opaque_stage1_identity(namespace: str, *parts: object) -> str:
    payload = "\0".join([namespace, *(str(part) for part in parts)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fetch_reports_batch(client, run_id: str, params: dict) -> Stage1BatchResult:
    params = params or {}
    batch = Stage1BatchResult()
    try:
        reconciliation = reconcile_stage1_artifacts_batch(
            client,
            run_id,
            connection_factory=_pg_conn,
            limit=int(params.get("artifact_reconcile_limit", 50)),
            dry_run=False,
        )
    except Stage1ArtifactSyncError as exc:
        reconciliation = exc.partial_result
    batch.reconciliation = reconciliation
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE, "Stage 1 persisted artifact reconciliation finished",
        run_id=run_id, context=reconciliation.to_dict(),
    )
    since_days = int(params.get("since_days", DEFAULT_SINCE_DAYS))
    mailbox = params.get("mailbox") or os.getenv("IMAP_MAILBOX") or DEFAULT_MAILBOX

    imap_host = os.getenv("IMAP_HOST")
    imap_port = int(os.getenv("IMAP_PORT", str(DEFAULT_IMAP_PORT)))
    imap_user = os.getenv("IMAP_USER", "automations.scheduled@example.invalid")
    imap_password = os.getenv("IMAP_PASSWORD", "")
    reports_data_dir = Path(os.getenv("REPORTS_DATA_DIR", DEFAULT_REPORTS_DATA_DIR))
    content_dedup_mode, dedup_range_start, dedup_range_end = _content_dedup_settings()
    report_link_allowset, report_link_max_bytes, report_link_timeout_s, report_link_verify_tls, report_link_preflight, report_link_range_sample_bytes = _report_link_settings()

    if not imap_host:
        batch.items.append(Stage1ItemResult(
            Stage1Outcome.FAILED_NON_RETRYABLE_CONFIGURATION,
            operator_action_required=True,
            error_category="IMAP_HOST_missing",
        ))
        raise Stage1BatchError(batch)
    if not imap_password:
        batch.items.append(Stage1ItemResult(
            Stage1Outcome.FAILED_NON_RETRYABLE_CONFIGURATION,
            operator_action_required=True,
            error_category="IMAP_PASSWORD_missing",
        ))
        raise Stage1BatchError(batch)

    account = imap_user
    fetched_at = datetime.now(timezone.utc)

    counters = {
        "uids_total": 0,
        "uids_new": 0,
        "uids_skipped_dedup": 0,
        "uids_fetch_failed": 0,
        "uids_missing_rfc822": 0,
        "messages_without_file_candidates": 0,
        "attachments_seen": 0,
        "attachments_supported": 0,
        "attachments_unsupported": 0,
        "attachments_missing_filename_fallback": 0,
        "link_urls_found": 0,
        "link_urls_allowed": 0,
        "link_preflight_ok": 0,
        "link_preflight_failed": 0,
        "link_download_ok": 0,
        "link_download_skipped_duplicate": 0,
        "link_download_failed": 0,
        "dedupe_canonical_resolved": 0,
        "dedupe_duplicate_recorded": 0,
        "files_inserted": 0,
        "files_skipped_dedup": 0,
        "files_skipped_content_dedup": 0,
        "fingerprint_failed": 0,
        "files_normalized": 0,
        "files_failed": 0,
        "artifacts_raw_uploaded": 0,
        "artifacts_normalized_uploaded": 0,
        "artifact_upload_failed": 0,
        "artifact_upload_skipped_existing": 0,
    }
    current_uid: int | None = None
    current_message_identity: str | None = None
    current_message_paths: list[Path] = []
    current_message_committed = False

    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Starting IMAP fetch_reports",
        run_id=run_id,
        context={
            "since_days": since_days,
            "mailbox": mailbox,
            "account": account,
            "sender_filters": _sender_filters_from_env(),
            "content_dedup_mode": content_dedup_mode,
            "content_dedup_range_start": dedup_range_start,
            "content_dedup_range_end": dedup_range_end,
            "report_link_allowlist": sorted(report_link_allowset),
            "report_link_max_bytes": report_link_max_bytes,
            "report_link_timeout_s": report_link_timeout_s,
            "report_link_verify_tls": report_link_verify_tls,
        },
    )

    conn = None
    imap = None
    try:
        conn = _pg_conn()
        cur = conn.cursor()

        imap = imaplib.IMAP4_SSL(imap_host, imap_port)
        imap.login(imap_user, imap_password)

        status, _ = imap.select(f'"{mailbox}"')
        if status != "OK":
            raise RuntimeError(f"IMAP SELECT failed for mailbox={mailbox}: {status}")

        uidvalidity_resp = imap.response("UIDVALIDITY")
        uidvalidity_raw = (uidvalidity_resp[1][0] if uidvalidity_resp and uidvalidity_resp[1] else b"0")
        uidvalidity = int(uidvalidity_raw.decode("ascii", errors="ignore") or "0")

        since_imap = _imap_since_date(since_days)
        sender_filters = _sender_filters_from_env()
        uid_list, imap_search_queries = _search_imap_uids(
            imap,
            since_imap=since_imap,
            sender_filters=sender_filters,
        )
        counters["uids_total"] = len(uid_list)
        batch.mailbox_check_completed = True
        batch.messages_inspected = len(uid_list)
        batch.messages_matched = len(uid_list)

        client.log(
            "INFO",
            "SCRIPT",
            JOB_SOURCE,
            f"IMAP search result: mailbox={mailbox} uids_total={len(uid_list)} since={since_imap}",
            run_id=run_id,
            context={
                "mailbox": mailbox,
                "since": since_imap,
                "imap_search_queries": imap_search_queries,
                "uids": [u.decode(errors="ignore") for u in uid_list[:20]],
                "uids_total": len(uid_list),
                "sender_filters": sender_filters,
            },
        )

        for uid_b in uid_list:
            uid = int(uid_b.decode("ascii", errors="ignore") or "0")
            current_uid = uid
            current_message_identity = _opaque_stage1_identity("message", uidvalidity, uid)
            current_message_paths = []
            current_message_committed = False
            message_artifact_uploads: list[dict] = []
            message_failed = False
            message_item_start = len(batch.items)
            message_counter_snapshot = dict(counters)

            cur.execute(
                """
                SELECT id
                FROM ingest.imap_message
                WHERE account=%s AND mailbox=%s AND uidvalidity=%s AND uid=%s
                """,
                (account, mailbox, uidvalidity, uid),
            )
            existing = cur.fetchone()
            if existing:
                counters["uids_skipped_dedup"] += 1
                batch.items.append(Stage1ItemResult(
                    Stage1Outcome.REUSED_MESSAGE,
                    message_identity=_opaque_stage1_identity("message", uidvalidity, uid),
                    deduplication="imap_message",
                ))
                client.log(
                    "INFO",
                    "SCRIPT",
                    JOB_SOURCE,
                    "IMAP message skipped: already persisted",
                    run_id=run_id,
                    context={"uid": uid, "mailbox": mailbox, "skip_reason": "existing_imap_message"},
                )
                conn.commit()
                continue

            status, fetch_data = imap.uid("FETCH", str(uid), "(RFC822 INTERNALDATE)")
            if status != "OK" or not fetch_data:
                counters["uids_fetch_failed"] += 1
                failure = RuntimeError(f"IMAP FETCH returned status={status}")
                cleanup_result = _cleanup_message_paths(current_message_paths)
                conn.rollback()
                context = _stage1_failure_context(
                    run_id=run_id, uid=uid, message_identity=current_message_identity,
                    raw_file_id=None, download_url=None, exc=failure, retryable=True,
                    retry_attempt=1, cleanup_result=cleanup_result,
                )
                batch.items.append(Stage1ItemResult(
                    Stage1Outcome.FAILED_RETRYABLE_MAIL_FETCH,
                    message_identity=current_message_identity,
                    retryable=True,
                    error_category="imap_fetch_failed",
                    imap_uid=uid,
                    exception_type=context["exception_type"],
                    exception_message=context["exception_message"],
                    retry_attempt=1,
                    transaction_scope="imap_message",
                    cleanup_result=cleanup_result,
                ))
                client.log(
                    "WARNING", "SCRIPT", JOB_SOURCE,
                    "IMAP message fetch failed; message transaction rolled back",
                    run_id=run_id, context=context,
                )
                continue

            msg_bytes = None
            meta = None
            for item in fetch_data:
                if isinstance(item, tuple) and len(item) >= 2:
                    meta = item[0]
                    msg_bytes = item[1]
                    break
            if not msg_bytes:
                counters["uids_missing_rfc822"] += 1
                failure = RuntimeError("IMAP FETCH response did not contain an RFC822 payload")
                cleanup_result = _cleanup_message_paths(current_message_paths)
                conn.rollback()
                context = _stage1_failure_context(
                    run_id=run_id, uid=uid, message_identity=current_message_identity,
                    raw_file_id=None, download_url=None, exc=failure, retryable=True,
                    retry_attempt=1, cleanup_result=cleanup_result,
                )
                batch.items.append(Stage1ItemResult(
                    Stage1Outcome.FAILED_RETRYABLE_MAIL_FETCH,
                    message_identity=current_message_identity,
                    retryable=True,
                    error_category="rfc822_payload_missing",
                    imap_uid=uid,
                    exception_type=context["exception_type"],
                    exception_message=context["exception_message"],
                    retry_attempt=1,
                    transaction_scope="imap_message",
                    cleanup_result=cleanup_result,
                ))
                client.log(
                    "WARNING", "SCRIPT", JOB_SOURCE,
                    "IMAP message payload missing; message transaction rolled back",
                    run_id=run_id, context=context,
                )
                continue

            msg = message_from_bytes(msg_bytes, policy=default_policy)

            subject = _decode_mime_header(msg.get("Subject", ""))
            from_header = _decode_mime_header(msg.get("From", ""))
            _, from_addr = parseaddr(from_header)
            message_id = _decode_mime_header(msg.get("Message-ID", ""))
            internal_date = _parse_internal_date(meta)

            cur.execute(
                """
                INSERT INTO ingest.imap_message (
                  account, mailbox, uidvalidity, uid, message_id, from_addr,
                  subject, internal_date, fetched_at, run_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account, mailbox, uidvalidity, uid)
                DO NOTHING
                RETURNING id
                """,
                (
                    account,
                    mailbox,
                    uidvalidity,
                    uid,
                    message_id,
                    from_addr,
                    subject,
                    internal_date,
                    fetched_at,
                    run_id,
                ),
            )
            inserted = cur.fetchone()
            if not inserted:
                counters["uids_skipped_dedup"] += 1
                batch.items.append(Stage1ItemResult(
                    Stage1Outcome.REUSED_MESSAGE,
                    message_identity=current_message_identity,
                    imap_uid=uid,
                    deduplication="imap_message_concurrent",
                ))
                conn.commit()
                continue

            imap_message_id = inserted[0]
            counters["uids_new"] += 1

            file_candidates, mime_stats = _collect_mime_file_candidates(msg, uid=uid)
            counters["attachments_seen"] += mime_stats["attachments_seen"]
            counters["attachments_supported"] += mime_stats["attachments_supported"]
            counters["attachments_unsupported"] += mime_stats["attachments_unsupported"]
            counters["attachments_missing_filename_fallback"] += mime_stats["attachments_missing_filename_fallback"]

            client.log(
                "INFO",
                "SCRIPT",
                JOB_SOURCE,
                "IMAP message attachment scan",
                run_id=run_id,
                context={
                    "uid": uid,
                    "mailbox": mailbox,
                    "subject": subject,
                    "from_addr": from_addr,
                    "attachment_filenames_seen": mime_stats["attachment_filenames_seen"],
                    "supported_attachment_filenames": mime_stats["supported_attachment_filenames"],
                    "unsupported_attachment_filenames": mime_stats["unsupported_attachment_filenames"],
                    "attachments_seen": mime_stats["attachments_seen"],
                    "attachments_supported": mime_stats["attachments_supported"],
                    "attachments_unsupported": mime_stats["attachments_unsupported"],
                },
            )

            should_try_links = ("limit e-mail" in subject.lower()) or not file_candidates
            if should_try_links:
                link_candidates, link_stats, link_events = _collect_link_file_candidates(
                    msg,
                    subject=subject,
                    uid=uid,
                    allowset=report_link_allowset,
                )
                file_candidates.extend(link_candidates)
                counters["link_urls_found"] += link_stats["link_urls_found"]
                counters["link_urls_allowed"] += link_stats["link_urls_allowed"]
                for event in link_events:
                    batch.items.append(Stage1ItemResult(
                        Stage1Outcome.SKIPPED_EXPECTED_LINK,
                        message_identity=current_message_identity,
                        attachment_identity=_opaque_stage1_identity(
                            "blocked_link", uidvalidity, uid,
                            event["context"].get("download_host"),
                            event["context"].get("link_kind"),
                        ),
                        deduplication="url_allowlist",
                        imap_uid=uid,
                        download_host=event["context"].get("download_host"),
                        transaction_scope="imap_message",
                        cleanup_result="not_needed",
                    ))
                    client.log(
                        event["level"],
                        "SCRIPT",
                        JOB_SOURCE,
                        event["message"],
                        run_id=run_id,
                        context={
                            **event["context"],
                            "run_id": run_id,
                            "message_identity": current_message_identity,
                            "retryable": False,
                            "transaction_scope": "imap_message",
                            "cleanup_result": "not_needed",
                        },
                    )

            if not file_candidates:
                counters["messages_without_file_candidates"] += 1
                client.log(
                    "INFO",
                    "SCRIPT",
                    JOB_SOURCE,
                    "IMAP message skipped: no supported attachments or report links",
                    run_id=run_id,
                    context={
                        "uid": uid,
                        "mailbox": mailbox,
                        "subject": subject,
                        "from_addr": from_addr,
                        "skip_reason": "no_supported_file_candidates",
                        "attachment_filenames_seen": mime_stats["attachment_filenames_seen"],
                    },
                )

            for candidate_idx, candidate in enumerate(file_candidates):
                raw_filename = candidate["raw_filename"]
                payload = candidate.get("payload")
                content_type = candidate["content_type"]
                candidate_source = candidate.get("source") or "mime"
                candidate_url = candidate.get("url")
                candidate_effective_url = candidate.get("effective_url") or candidate_url
                http_range_fp = candidate.get("http_range_fp")

                if candidate_source == "link":
                    if report_link_preflight and candidate_url:
                        try:
                            http_range_fp = _http_range_fingerprint(
                                candidate_effective_url,
                                sample_bytes=report_link_range_sample_bytes,
                                timeout_s=report_link_timeout_s,
                                verify_tls=report_link_verify_tls,
                            )
                            candidate["http_range_fp"] = http_range_fp
                            counters["link_preflight_ok"] += 1
                        except Exception as exc:
                            counters["link_preflight_failed"] += 1
                            client.log(
                                "WARNING",
                                "SCRIPT",
                                JOB_SOURCE,
                                "Report link preflight failed (fallback to full download)",
                                run_id=run_id,
                                context={
                                    "uid": uid,
                                    "subject": subject,
                                    "download_host": _download_host(candidate_effective_url),
                                    "download_path": _safe_url_for_log(candidate_effective_url or ""),
                                    "exception_type": type(exc).__name__,
                                    "exception_message": _sanitized_exception_message(exc),
                                },
                            )

                    if http_range_fp:
                        canonical = _select_raw_file_by_http_range_fp(
                            cur,
                            account=account,
                            http_range_fp=http_range_fp,
                        )
                        if canonical:
                            canonical_id, canonical_sha256 = canonical
                            duplicate_id = _insert_raw_file_duplicate(
                                cur,
                                imap_message_id=imap_message_id,
                                account=account,
                                sha256=canonical_sha256,
                                raw_filename=raw_filename,
                                content_type=content_type,
                                size_bytes=0,
                                report_key=_report_key_from_filename(raw_filename),
                                content_fingerprint=None,
                                dedup_basis="http_range_fp",
                                canonical_id=canonical_id,
                                http_range_fp=http_range_fp,
                            )
                            if duplicate_id:
                                counters["link_download_skipped_duplicate"] += 1
                                counters["files_skipped_content_dedup"] += 1
                                counters["dedupe_canonical_resolved"] += 1
                                counters["dedupe_duplicate_recorded"] += 1
                                client.log(
                                    "INFO",
                                    "SCRIPT",
                                    JOB_SOURCE,
                                    "Report link skipped download due to http_range_fp",
                                    run_id=run_id,
                                    context={
                                        "uid": uid,
                                        "download_host": _download_host(candidate_effective_url),
                                        "download_path": _safe_url_for_log(candidate_effective_url or ""),
                                        "http_range_fp": http_range_fp,
                                        "canonical_id": canonical_id,
                                        "canonical_sha256": canonical_sha256,
                                        "raw_file_id": duplicate_id,
                                        "dedup_basis": "http_range_fp",
                                    },
                                )
                                continue

                    try:
                        payload, filename_guess, content_type = _download_url_to_bytes(
                            url=candidate_effective_url or candidate_url,
                            timeout_s=report_link_timeout_s,
                            max_bytes=report_link_max_bytes,
                            verify_tls=report_link_verify_tls,
                        )
                        normalized_filename = _normalize_candidate_filename(
                            filename_guess,
                            content_type,
                            uid=uid,
                            part_idx=candidate_idx,
                            prefix="link",
                        )
                        if not normalized_filename:
                            counters["link_download_failed"] += 1
                            client.log(
                                "WARNING",
                                "SCRIPT",
                                JOB_SOURCE,
                                "Report link downloaded but file type is unsupported",
                                run_id=run_id,
                                context={
                                    "uid": uid,
                                    "subject": subject,
                                    "download_host": _download_host(candidate_effective_url),
                                    "download_path": _safe_url_for_log(candidate_effective_url or ""),
                                    "filename": filename_guess,
                                    "content_type": content_type,
                                },
                            )
                            continue
                        raw_filename = normalized_filename
                        candidate["raw_filename"] = raw_filename
                        candidate["payload"] = payload
                        candidate["content_type"] = content_type
                        counters["link_download_ok"] += 1
                    except Exception as exc:
                        expected_bytes = getattr(exc, "expected_bytes", None)
                        received_bytes = getattr(exc, "received_bytes", None)
                        retry_attempt = getattr(exc, "retry_attempt", 1)
                        retryable = bool(getattr(exc, "retryable", True))
                        cleanup_result = getattr(exc, "cleanup_result", "not_reported")
                        local_cleanup = _cleanup_message_paths(current_message_paths)
                        cleanup_result = f"{cleanup_result};local_{local_cleanup}"
                        conn.rollback()
                        expected_skips = [
                            item for item in batch.items[message_item_start:]
                            if item.outcome == Stage1Outcome.SKIPPED_EXPECTED_LINK
                        ]
                        del batch.items[message_item_start:]
                        batch.items.extend(expected_skips)
                        counters.update(message_counter_snapshot)
                        counters["link_download_failed"] += 1
                        counters["files_failed"] += 1
                        context = _stage1_failure_context(
                            run_id=run_id, uid=uid,
                            message_identity=current_message_identity,
                            raw_file_id=None,
                            download_url=candidate_effective_url,
                            exc=exc,
                            retryable=retryable,
                            retry_attempt=retry_attempt,
                            expected_bytes=expected_bytes,
                            received_bytes=received_bytes,
                            cleanup_result=cleanup_result,
                        )
                        batch.items.append(Stage1ItemResult(
                            Stage1Outcome.FAILED_RETRYABLE_DOWNLOAD
                            if retryable else Stage1Outcome.FAILED_NON_RETRYABLE_VALIDATION,
                            message_identity=current_message_identity,
                            attachment_identity=_opaque_stage1_identity(
                                "download", uidvalidity, uid, candidate_idx
                            ),
                            retryable=retryable,
                            error_category=(
                                "report_download_retryable"
                                if retryable else "report_download_validation"
                            ),
                            imap_uid=uid,
                            download_host=context["download_host"],
                            exception_type=context["exception_type"],
                            exception_message=context["exception_message"],
                            expected_bytes=expected_bytes,
                            received_bytes=received_bytes,
                            retry_attempt=retry_attempt,
                            transaction_scope="imap_message",
                            cleanup_result=cleanup_result,
                        ))
                        client.log(
                            "WARNING", "SCRIPT", JOB_SOURCE,
                            "Report link download failed; message transaction rolled back",
                            run_id=run_id, context=context,
                        )
                        message_failed = True
                        break

                if payload is None:
                    continue

                report_key = _report_key_from_filename(raw_filename)

                ext = os.path.splitext(raw_filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    continue

                is_fallback = raw_filename.startswith("attachment-")
                is_texty = (content_type or "").lower().startswith("text/")
                if ext == ".csv" and is_fallback and is_texty and len(payload) < 2048:
                    client.log(
                        "INFO", "SCRIPT", JOB_SOURCE,
                        "Ignoring tiny CSV fallback (likely email body/metadata)",
                        run_id=run_id,
                        context={"uid": uid, "filename": raw_filename, "bytes": len(payload), "content_type": content_type},
                    )
                    continue


                sha256 = hashlib.sha256(payload).hexdigest()
                safe_name = _safe_filename(raw_filename)
                day = fetched_at.astimezone(timezone.utc)
                raw_dir = reports_data_dir / "raw" / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
                norm_dir = reports_data_dir / "normalized" / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
                raw_path = raw_dir / f"{sha256}__{safe_name}"
                norm_path = norm_dir / f"{sha256}.csv"

                content_fingerprint = None
                dedup_basis = None
                if content_dedup_mode != "off":
                    try:
                        content_fingerprint, dedup_basis, _ = _compute_content_fingerprint(
                            payload,
                            ext,
                            content_dedup_mode,
                            dedup_range_start,
                            dedup_range_end,
                        )
                    except Exception:
                        counters["fingerprint_failed"] += 1

                persist_result = _persist_raw_file_candidate(
                    cur,
                    candidate=candidate,
                    imap_message_id=imap_message_id,
                    account=account,
                    sha256=sha256,
                    raw_filename=raw_filename,
                    content_type=content_type,
                    size_bytes=len(payload),
                    raw_path=str(raw_path),
                    report_key=report_key,
                    content_fingerprint=content_fingerprint,
                    dedup_basis=dedup_basis,
                )
                action = persist_result["action"]

                if action == "DUPLICATE_CONTENT":
                    counters["files_skipped_content_dedup"] += 1
                    counters["dedupe_canonical_resolved"] += 1
                    counters["dedupe_duplicate_recorded"] += 1
                    batch.items.append(Stage1ItemResult(
                        Stage1Outcome.SKIPPED_DUPLICATE,
                        message_identity=_opaque_stage1_identity("message", uidvalidity, uid),
                        attachment_identity=_opaque_stage1_identity("attachment", sha256),
                        raw_file_id=persist_result.get("raw_file_id"),
                        deduplication=str(dedup_basis or "content"),
                    ))
                    client.log(
                        "INFO",
                        "SCRIPT",
                        JOB_SOURCE,
                        "Dedupe duplicate recorded",
                        run_id=run_id,
                        context={
                            "uid": uid,
                            "source": candidate_source,
                            "download_host": _download_host(candidate_effective_url),
                            "raw_file_id": persist_result.get("raw_file_id"),
                            "canonical_id": persist_result.get("canonical_id"),
                            "report_key": report_key,
                            "content_fingerprint": content_fingerprint,
                            "dedup_basis": dedup_basis,
                            "sha256": sha256,
                            "filename": raw_filename,
                        },
                    )
                    continue

                if action != "NEW":
                    counters["files_skipped_dedup"] += 1
                    log_level = "WARNING" if action == "UNRESOLVED" else "INFO"
                    if action == "UNRESOLVED":
                        counters["files_failed"] += 1
                    batch.items.append(Stage1ItemResult(
                        Stage1Outcome.FAILED_RETRYABLE_PERSISTENCE
                        if action == "UNRESOLVED" else Stage1Outcome.REUSED_RAW_FILE,
                        message_identity=_opaque_stage1_identity("message", uidvalidity, uid),
                        attachment_identity=_opaque_stage1_identity("attachment", sha256),
                        raw_file_id=persist_result.get("raw_file_id"),
                        deduplication=str(action).lower(),
                        retryable=action == "UNRESOLVED",
                        error_category="raw_file_persistence_unresolved" if action == "UNRESOLVED" else None,
                    ))
                    client.log(
                        log_level,
                        "SCRIPT",
                        JOB_SOURCE,
                        "Raw file skipped or unresolved",
                        run_id=run_id,
                        context={
                            "uid": uid,
                            "source": candidate_source,
                            "download_host": _download_host(candidate_effective_url),
                            "action": action,
                            "reason": persist_result.get("reason"),
                            "raw_file_id": persist_result.get("raw_file_id"),
                            "existing_status": persist_result.get("existing_status"),
                            "duplicate_of_id": persist_result.get("duplicate_of_id"),
                            "canonical_id": persist_result.get("canonical_id"),
                            "report_key": report_key,
                            "content_fingerprint": content_fingerprint,
                            "dedup_basis": dedup_basis,
                            "sha256": sha256,
                            "filename": raw_filename,
                        },
                    )
                    if action == "UNRESOLVED":
                        local_cleanup = _cleanup_message_paths(current_message_paths)
                        conn.rollback()
                        expected_skips = [
                            item for item in batch.items[message_item_start:]
                            if item.outcome == Stage1Outcome.SKIPPED_EXPECTED_LINK
                        ]
                        del batch.items[message_item_start:]
                        batch.items.extend(expected_skips)
                        counters.update(message_counter_snapshot)
                        counters["files_failed"] += 1
                        failure = RuntimeError(
                            persist_result.get("reason") or "raw_file persistence was unresolved"
                        )
                        context = _stage1_failure_context(
                            run_id=run_id, uid=uid,
                            message_identity=current_message_identity,
                            raw_file_id=persist_result.get("raw_file_id"),
                            download_url=candidate_effective_url,
                            exc=failure, retryable=True, retry_attempt=1,
                            cleanup_result=local_cleanup,
                        )
                        batch.items.append(Stage1ItemResult(
                            Stage1Outcome.FAILED_RETRYABLE_PERSISTENCE,
                            message_identity=current_message_identity,
                            attachment_identity=_opaque_stage1_identity("attachment", sha256),
                            raw_file_id=persist_result.get("raw_file_id"),
                            retryable=True,
                            error_category="raw_file_persistence_unresolved",
                            imap_uid=uid,
                            download_host=context["download_host"],
                            exception_type=context["exception_type"],
                            exception_message=context["exception_message"],
                            retry_attempt=1,
                            transaction_scope="imap_message",
                            cleanup_result=local_cleanup,
                        ))
                        client.log(
                            "WARNING", "SCRIPT", JOB_SOURCE,
                            "Raw file persistence unresolved; message transaction rolled back",
                            run_id=run_id, context=context,
                        )
                        message_failed = True
                        break
                    continue

                raw_file_id = persist_result["raw_file_id"]
                counters["files_inserted"] += 1
                client.log(
                    "INFO",
                    "SCRIPT",
                    JOB_SOURCE,
                    "Raw file inserted",
                    run_id=run_id,
                    context={
                        "uid": uid,
                        "source": candidate_source,
                        "download_host": _download_host(candidate_effective_url),
                        "raw_file_id": raw_file_id,
                        "status": "NEW",
                        "report_key": report_key,
                        "content_fingerprint": content_fingerprint,
                        "dedup_basis": dedup_basis,
                        "sha256": sha256,
                        "filename": raw_filename,
                    },
                )

                try:
                    if _atomic_write_bytes(raw_path, payload):
                        current_message_paths.append(raw_path)
                    message_artifact_uploads.append(
                        {
                            "path": raw_path,
                            "raw_file_id": raw_file_id,
                            "artifact_role": "raw",
                            "original_filename": raw_filename,
                            "uid": uid,
                            "message_identity": current_message_identity,
                            "source_sha256": sha256,
                        }
                    )

                    date_normalization, normalized_created = _atomic_normalize(
                        payload, ext, norm_path
                    )
                    if normalized_created:
                        current_message_paths.append(norm_path)

                    cur.execute(
                        """
                        UPDATE ingest.raw_file
                        SET status='NORMALIZED', normalized_csv_path=%s, error=NULL,
                            stage1_normalized_artifact_metadata=%s::jsonb
                        WHERE account=%s AND sha256=%s
                        """,
                        (
                            str(norm_path),
                            json.dumps({
                                "report_key": report_key,
                                "sha256": sha256,
                                "date_normalization": date_normalization,
                            }),
                            account,
                            sha256,
                        ),
                    )
                    counters["files_normalized"] += 1
                    batch.items.append(Stage1ItemResult(
                        Stage1Outcome.CREATED,
                        message_identity=current_message_identity,
                        attachment_identity=_opaque_stage1_identity("attachment", sha256),
                        raw_file_id=str(raw_file_id),
                        imap_uid=uid,
                        transaction_scope="imap_message",
                    ))
                    message_artifact_uploads.append(
                        {
                            "path": norm_path,
                            "raw_file_id": raw_file_id,
                            "artifact_role": "normalized",
                            "original_filename": raw_filename,
                            "uid": uid,
                            "message_identity": current_message_identity,
                            "source_sha256": sha256,
                            "metadata": {
                                "report_key": report_key,
                                "sha256": sha256,
                                "date_normalization": date_normalization,
                            },
                        }
                    )
                    client.log(
                        "INFO", "SCRIPT", JOB_SOURCE,
                        "Raw file normalized; pending per-message commit",
                        run_id=run_id,
                        context={
                            "uid": uid,
                            "message_identity": current_message_identity,
                            "raw_file_id": raw_file_id,
                            "status": "NORMALIZED",
                            "report_key": report_key,
                            "sha256": sha256,
                            "normalized_csv_path": str(norm_path),
                            "date_normalization": date_normalization,
                            "transaction_scope": "imap_message",
                            "transaction_state": "pending",
                        },
                    )
                except Exception as exc:
                    local_cleanup = _cleanup_message_paths(current_message_paths)
                    conn.rollback()
                    expected_skips = [
                        item for item in batch.items[message_item_start:]
                        if item.outcome == Stage1Outcome.SKIPPED_EXPECTED_LINK
                    ]
                    del batch.items[message_item_start:]
                    batch.items.extend(expected_skips)
                    counters.update(message_counter_snapshot)
                    counters["files_failed"] += 1
                    context = _stage1_failure_context(
                        run_id=run_id, uid=uid,
                        message_identity=current_message_identity,
                        raw_file_id=str(raw_file_id),
                        download_url=candidate_effective_url,
                        exc=exc,
                        retryable=True,
                        retry_attempt=1,
                        cleanup_result=local_cleanup,
                        report_type=None,
                        client_code=None,
                    )
                    batch.items.append(Stage1ItemResult(
                        Stage1Outcome.FAILED_RETRYABLE_PERSISTENCE,
                        message_identity=current_message_identity,
                        attachment_identity=_opaque_stage1_identity("attachment", sha256),
                        raw_file_id=str(raw_file_id),
                        retryable=True,
                        error_category="normalization_or_file_persistence",
                        imap_uid=uid,
                        download_host=context["download_host"],
                        exception_type=context["exception_type"],
                        exception_message=context["exception_message"],
                        retry_attempt=1,
                        transaction_scope="imap_message",
                        cleanup_result=local_cleanup,
                    ))
                    client.log(
                        "WARNING", "SCRIPT", JOB_SOURCE,
                        "Raw file normalization failed; message transaction rolled back",
                        run_id=run_id, context=context,
                    )
                    message_failed = True
                    break

            if message_failed:
                continue

            conn.commit()
            current_message_committed = True
            client.log(
                "INFO", "SCRIPT", JOB_SOURCE,
                "Stage 1 IMAP message transaction committed",
                run_id=run_id,
                context={
                    "run_id": run_id,
                    "imap_uid": uid,
                    "message_identity": current_message_identity,
                    "transaction_scope": "imap_message",
                    "transaction_state": "committed",
                    "artifact_roles_pending": len(message_artifact_uploads),
                },
            )

            if message_artifact_uploads:
                try:
                    uploaded = _upload_stage1_artifacts(
                        client,
                        cur,
                        run_id=run_id,
                        uploads=message_artifact_uploads,
                        counters=counters,
                    )
                except Stage1ArtifactSyncError as exc:
                    uploaded = exc.partial_result
                batch.reconciliation.records_discovered += uploaded.records_discovered
                batch.reconciliation.records_inspected += uploaded.records_inspected
                batch.reconciliation.items.extend(uploaded.items)
                # Artifact existence checks are read-only but start an implicit psycopg
                # transaction. End it here so the next transaction contains only the
                # next IMAP message's ingest work.
                conn.commit()

        _apply_stage1_artifact_results(batch)

        if counters["files_failed"] > 0:
            client.log(
                "WARNING",
                "SCRIPT",
                JOB_SOURCE,
                "Run completed with failed Stage 1 items",
                run_id=run_id,
                context={"files_failed": counters["files_failed"], "uids_new": counters["uids_new"]},
            )

        client.log(
            "INFO",
            "SCRIPT",
            JOB_SOURCE,
            (
                "IMAP fetch_reports completed: "
                f"uids_total={counters['uids_total']} uids_new={counters['uids_new']} "
                f"attachments_seen={counters['attachments_seen']} "
                f"link_urls_found={counters['link_urls_found']} "
                f"link_download_ok={counters['link_download_ok']} "
                f"link_download_failed={counters['link_download_failed']} "
                f"files_normalized={counters['files_normalized']} "
                f"files_failed={counters['files_failed']}"
            ),
            run_id=run_id,
            context=counters,
        )
        batch.messages_deduplicated = counters["uids_skipped_dedup"]
        batch.messages_skipped = counters["messages_without_file_candidates"] + counters["uids_skipped_dedup"]
        batch.attachments_discovered = counters["attachments_seen"] + counters["link_urls_allowed"]
        batch.attachments_attempted = counters["attachments_supported"] + counters["link_download_ok"]
        batch.attachments_created = counters["files_normalized"]
        batch.attachments_reused = counters["files_skipped_dedup"] + counters["files_skipped_content_dedup"]
        batch.attachments_skipped = counters["attachments_unsupported"] + counters["messages_without_file_candidates"]
        batch.unsupported_attachments = counters["attachments_unsupported"]
        batch.raw_files_created = counters["files_inserted"]
        batch.raw_files_reused = counters["files_skipped_dedup"] + counters["files_skipped_content_dedup"]
        if batch.has_failures:
            raise Stage1BatchError(batch)
        return batch
    except Stage1BatchError:
        raise
    except Exception as exc:
        message_scoped = current_uid is not None
        if conn is not None and message_scoped and not current_message_committed:
            try:
                conn.rollback()
                rollback_result = "database_rolled_back"
            except Exception:
                rollback_result = "database_rollback_failed"
            cleanup_result = f"{rollback_result};local_{_cleanup_message_paths(current_message_paths)}"
        elif current_message_committed:
            cleanup_result = "message_already_committed;durable_sources_retained_for_reconciliation"
        else:
            cleanup_result = "not_needed"
        context = _stage1_failure_context(
            run_id=run_id,
            uid=current_uid,
            message_identity=current_message_identity,
            raw_file_id=None,
            download_url=None,
            exc=exc,
            retryable=True,
            retry_attempt=1,
            cleanup_result=cleanup_result,
        )
        batch.items.append(Stage1ItemResult(
            Stage1Outcome.FAILED_RETRYABLE_PERSISTENCE
            if message_scoped else Stage1Outcome.FAILED_RETRYABLE_MAIL_FETCH,
            message_identity=current_message_identity,
            retryable=True,
            error_category=(
                "unexpected_message_processing_failure"
                if message_scoped else "mailbox_connection_or_search"
            ),
            imap_uid=current_uid,
            exception_type=context["exception_type"],
            exception_message=context["exception_message"],
            retry_attempt=1,
            transaction_scope="imap_message" if message_scoped else "mailbox_batch",
            cleanup_result=cleanup_result,
        ))
        context["transaction_scope"] = "imap_message" if message_scoped else "mailbox_batch"
        client.log(
            "ERROR", "SCRIPT", JOB_SOURCE,
            "Stage 1 aborted by unexpected exception",
            run_id=run_id, context=context,
        )
        raise Stage1BatchError(batch) from exc
    finally:
        if imap is not None:
            try:
                imap.close()
            except Exception:
                pass
            try:
                imap.logout()
            except Exception:
                pass
        if conn is not None:
            conn.close()


def _apply_stage1_artifact_results(batch: Stage1BatchResult) -> None:
    by_raw_file = {item.raw_file_id: item for item in batch.items if item.raw_file_id}
    for artifact in batch.reconciliation.items:
        item = by_raw_file.get(artifact.raw_file_id)
        if item is None:
            continue
        item.artifact_sync_outcomes.append(artifact.outcome.value)
        if artifact.artifact_role == RAW_ROLE:
            item.raw_artifact_id = artifact.artifact_id
        elif artifact.artifact_role == NORMALIZED_ROLE:
            item.normalized_artifact_id = artifact.artifact_id
        if artifact.retryable:
            item.retryable = True
            item.outcome = Stage1Outcome.FAILED_RETRYABLE_ARTIFACT_SYNC
            item.error_category = artifact.error_category
        if artifact.operator_action_required:
            item.operator_action_required = True
            item.outcome = Stage1Outcome.BLOCKED_OPERATOR_ACTION
            item.error_category = artifact.error_category


def run(client, run_id: str, params: dict) -> Stage1BatchResult:
    """Backward-compatible runner entrypoint returning the reusable typed result."""
    result = fetch_reports_batch(client, run_id, params)
    summary = result.to_dict()
    summary.pop("items", None)
    summary.pop("artifact_reconciliation", None)
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE, "Stage 1 mailbox batch result",
        run_id=run_id, context=summary,
    )
    return result
