"""
Phase 2 hard safety limits for Telematics HTTP usage.

Prevents runaway request loops / pagination from exhausting provider token quotas.
Fail-fast: any limit breach or suspicious pagination aborts the run locally.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


class TelematicsProviderSafetyError(RuntimeError):
    """Safety stop: no further provider requests must be issued after this."""

    def __init__(self, code: str, message: str, *, context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.context = dict(context or {})


LogFn = Callable[[str, str, Dict[str, Any]], None]  # level, message, context


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(1, v)


@dataclass
class SafetyLimits:
    """Conservative defaults protect even large execution windows."""

    max_requests_per_run: int = field(default_factory=lambda: _int_env("TELEMATICS_PROVIDER_MAX_REQUESTS_PER_RUN", 500))
    max_requests_per_endpoint: int = field(
        default_factory=lambda: _int_env("TELEMATICS_PROVIDER_MAX_REQUESTS_PER_ENDPOINT", 300)
    )
    max_requests_per_subwindow: int = field(
        default_factory=lambda: _int_env("TELEMATICS_PROVIDER_MAX_REQUESTS_PER_SUBWINDOW", 80)
    )
    max_pages_per_subwindow: int = field(
        default_factory=lambda: _int_env("TELEMATICS_PROVIDER_MAX_PAGES_PER_SUBWINDOW", 50)
    )
    max_retries_per_http_call: int = field(
        default_factory=lambda: _int_env("TELEMATICS_PROVIDER_MAX_RETRIES", 2)
    )
    timeout_s: int = field(default_factory=lambda: _int_env("TELEMATICS_PROVIDER_TIMEOUT_S", 60))


# ---------------------------------------------------------------------------
# `/trips` compatibility mode (`data_invariants_v1`) budgets and taxonomy.
#
# Only used by the compatibility pagination state machine (docs/12 §4-§8,
# docs/16 §5-§6). `strict_meta` never reads any of it, and the values below may
# only ever *reduce* what the existing strict budgets already permit.
# ---------------------------------------------------------------------------

TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW_ENV = (
    "TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW"
)
TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES_ENV = (
    "TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES"
)
TELEMATICS_PROVIDER_COMPAT_MAX_ELAPSED_S_ENV = "TELEMATICS_PROVIDER_COMPAT_MAX_ELAPSED_S"

# docs/12 §5.5: 32 MiB per response, hard ceiling 64 MiB, 900 s per sub-window.
COMPAT_DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
COMPAT_MAX_RESPONSE_BYTES_CEILING = 64 * 1024 * 1024
COMPAT_DEFAULT_MAX_ELAPSED_S = 900

# Compatibility failure classifications (docs/12 §8, docs/16 §5.3).
PAGINATION_COMPAT_CONFIG_INVALID = "PAGINATION_COMPAT_CONFIG_INVALID"
PAGINATION_COMPAT_IDENTITY_MISSING = "PAGINATION_COMPAT_IDENTITY_MISSING"
PAGINATION_COMPAT_DUPLICATE_IN_PAGE = "PAGINATION_COMPAT_DUPLICATE_IN_PAGE"
PAGINATION_COMPAT_PAGE_OVERLAP = "PAGINATION_COMPAT_PAGE_OVERLAP"
PAGINATION_COMPAT_PAGE_REPEATED = "PAGINATION_COMPAT_PAGE_REPEATED"
PAGINATION_COMPAT_ROWS_EXCEED_LIMIT = "PAGINATION_COMPAT_ROWS_EXCEED_LIMIT"
PAGINATION_COMPAT_SHAPE_UNSTABLE = "PAGINATION_COMPAT_SHAPE_UNSTABLE"
PAGINATION_COMPAT_ROW_BUDGET_EXCEEDED = "PAGINATION_COMPAT_ROW_BUDGET_EXCEEDED"
PAGINATION_COMPAT_RESPONSE_BYTES_EXCEEDED = "PAGINATION_COMPAT_RESPONSE_BYTES_EXCEEDED"
PAGINATION_COMPAT_ELAPSED_BUDGET_EXCEEDED = "PAGINATION_COMPAT_ELAPSED_BUDGET_EXCEEDED"
# The four accepted advisory-`total` codes (docs/16 §5.3). These are the only
# `total` classifications that exist: an absent advisory total is a permitted
# compatibility state and must never be given a failure code of its own, and the
# earlier "changed"/"rows exceed"/"short page inconsistent"/"implausible"
# spellings are superseded or withdrawn.
PAGINATION_COMPAT_TOTAL_INVALID = "PAGINATION_COMPAT_TOTAL_INVALID"
PAGINATION_COMPAT_TOTAL_UNSTABLE = "PAGINATION_COMPAT_TOTAL_UNSTABLE"
PAGINATION_COMPAT_TOTAL_EXCEEDED = "PAGINATION_COMPAT_TOTAL_EXCEEDED"
PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED = (
    "PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED"
)


def _compat_int_env(name: str, default: int) -> int:
    """Strict, fail-closed integer ENV read for compatibility budgets.

    Unlike `_int_env`, a malformed or non-positive value is never silently
    replaced by the default: a misconfigured compatibility budget is the one
    thing that could weaken the only protection this mode has.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise TelematicsProviderSafetyError(
            PAGINATION_COMPAT_CONFIG_INVALID,
            f"{name} must be a positive integer",
            context={"env": name, "reason": "not_an_integer"},
        )
    if value <= 0:
        raise TelematicsProviderSafetyError(
            PAGINATION_COMPAT_CONFIG_INVALID,
            f"{name} must be > 0",
            context={"env": name, "reason": "not_positive", "value": value},
        )
    return value


@dataclass(frozen=True)
class CompatibilitySafetyLimits:
    """Bounded `data_invariants_v1` budgets for one sub-window."""

    max_rows_per_subwindow: int
    max_response_bytes: int
    max_response_bytes_per_subwindow: int
    max_elapsed_s: int

    @classmethod
    def from_env(cls, *, page_limit: int, max_pages_per_subwindow: int) -> "CompatibilitySafetyLimits":
        """Read and validate every compatibility budget exactly once.

        Raises `PAGINATION_COMPAT_CONFIG_INVALID` before any provider request
        when the configuration cannot be trusted.
        """
        if not isinstance(page_limit, int) or isinstance(page_limit, bool) or page_limit <= 0:
            raise TelematicsProviderSafetyError(
                PAGINATION_COMPAT_CONFIG_INVALID,
                "page_limit must be a positive integer for compatibility pagination",
                context={"reason": "invalid_page_limit"},
            )
        if (
            not isinstance(max_pages_per_subwindow, int)
            or isinstance(max_pages_per_subwindow, bool)
            or max_pages_per_subwindow <= 0
        ):
            raise TelematicsProviderSafetyError(
                PAGINATION_COMPAT_CONFIG_INVALID,
                "max_pages_per_subwindow must be a positive integer for compatibility pagination",
                context={"reason": "invalid_max_pages_per_subwindow"},
            )

        max_rows = _compat_int_env(
            TELEMATICS_PROVIDER_COMPAT_MAX_ROWS_PER_SUBWINDOW_ENV,
            page_limit * max_pages_per_subwindow,
        )
        max_response_bytes = _compat_int_env(
            TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES_ENV,
            COMPAT_DEFAULT_MAX_RESPONSE_BYTES,
        )
        if max_response_bytes > COMPAT_MAX_RESPONSE_BYTES_CEILING:
            raise TelematicsProviderSafetyError(
                PAGINATION_COMPAT_CONFIG_INVALID,
                (
                    f"{TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES_ENV} exceeds the "
                    f"{COMPAT_MAX_RESPONSE_BYTES_CEILING}-byte ceiling"
                ),
                context={
                    "env": TELEMATICS_PROVIDER_COMPAT_MAX_RESPONSE_BYTES_ENV,
                    "reason": "above_ceiling",
                    "ceiling": COMPAT_MAX_RESPONSE_BYTES_CEILING,
                    "value": max_response_bytes,
                },
            )
        max_elapsed_s = _compat_int_env(
            TELEMATICS_PROVIDER_COMPAT_MAX_ELAPSED_S_ENV,
            COMPAT_DEFAULT_MAX_ELAPSED_S,
        )
        return cls(
            max_rows_per_subwindow=max_rows,
            max_response_bytes=max_response_bytes,
            # Derived, never a separate variable: at most one full-size response
            # per permitted page. No approved ENV names or defaults are invented.
            max_response_bytes_per_subwindow=max_response_bytes * max_pages_per_subwindow,
            max_elapsed_s=max_elapsed_s,
        )

    def context(self) -> Dict[str, Any]:
        return {
            "compat_max_rows_per_subwindow": self.max_rows_per_subwindow,
            "compat_max_response_bytes": self.max_response_bytes,
            "compat_max_response_bytes_per_subwindow": self.max_response_bytes_per_subwindow,
            "compat_max_elapsed_s": self.max_elapsed_s,
        }


@dataclass
class ProviderRunBudget:
    """Tracks cumulative HTTP usage for one job run."""

    limits: SafetyLimits
    total_requests: int = 0
    endpoint_requests: Dict[str, int] = field(default_factory=dict)
    subwindow_request_counts: Dict[str, int] = field(default_factory=dict)
    subwindow_page_counts: Dict[str, int] = field(default_factory=dict)
    total_pages_fetched: int = 0

    def _sub_key(self, path: str, sub_window_label: str) -> str:
        return f"{path}|{sub_window_label}"

    def before_request(self, *, path: str, sub_window_label: str) -> None:
        if self.total_requests >= self.limits.max_requests_per_run:
            raise TelematicsProviderSafetyError(
                "MAX_REQUESTS_PER_RUN",
                f"Exceeded TELEMATICS_PROVIDER_MAX_REQUESTS_PER_RUN ({self.limits.max_requests_per_run})",
                context={
                    "limit": self.limits.max_requests_per_run,
                    "total_requests": self.total_requests,
                    "path": path,
                    "sub_window": sub_window_label,
                },
            )
        ep = self.endpoint_requests.get(path, 0)
        if ep >= self.limits.max_requests_per_endpoint:
            raise TelematicsProviderSafetyError(
                "MAX_REQUESTS_PER_ENDPOINT",
                f"Exceeded TELEMATICS_PROVIDER_MAX_REQUESTS_PER_ENDPOINT ({self.limits.max_requests_per_endpoint}) for {path}",
                context={
                    "limit": self.limits.max_requests_per_endpoint,
                    "endpoint": path,
                    "endpoint_requests": ep,
                    "sub_window": sub_window_label,
                },
            )
        sk = self._sub_key(path, sub_window_label)
        sw = self.subwindow_request_counts.get(sk, 0)
        if sw >= self.limits.max_requests_per_subwindow:
            raise TelematicsProviderSafetyError(
                "MAX_REQUESTS_PER_SUBWINDOW",
                f"Exceeded TELEMATICS_PROVIDER_MAX_REQUESTS_PER_SUBWINDOW ({self.limits.max_requests_per_subwindow})",
                context={
                    "limit": self.limits.max_requests_per_subwindow,
                    "path": path,
                    "sub_window": sub_window_label,
                    "subwindow_requests": sw,
                },
            )

    def record_request_issued(self, *, path: str, sub_window_label: str) -> None:
        self.before_request(path=path, sub_window_label=sub_window_label)
        self.total_requests += 1
        self.endpoint_requests[path] = self.endpoint_requests.get(path, 0) + 1
        sk = self._sub_key(path, sub_window_label)
        self.subwindow_request_counts[sk] = self.subwindow_request_counts.get(sk, 0) + 1

    def record_page_completed(self, *, path: str, sub_window_label: str, page_index: int) -> None:
        self.total_pages_fetched += 1
        sk = self._sub_key(path, sub_window_label)
        pc = self.subwindow_page_counts.get(sk, 0) + 1
        self.subwindow_page_counts[sk] = pc
        if pc > self.limits.max_pages_per_subwindow:
            raise TelematicsProviderSafetyError(
                "MAX_PAGES_PER_SUBWINDOW",
                f"Exceeded TELEMATICS_PROVIDER_MAX_PAGES_PER_SUBWINDOW ({self.limits.max_pages_per_subwindow})",
                context={
                    "limit": self.limits.max_pages_per_subwindow,
                    "path": path,
                    "sub_window": sub_window_label,
                    "pages_in_subwindow": pc,
                    "page_index": page_index,
                },
            )

    def accounting_context(self) -> Dict[str, Any]:
        return {
            "total_requests": self.total_requests,
            "total_pages": self.total_pages_fetched,
            "endpoint_requests": dict(self.endpoint_requests),
        }
