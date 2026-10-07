"""Typed domain errors for the Eco Driving Explorer read-model foundation.

These errors are safe to surface to internal callers: they never carry SQL
text, connection strings, credentials, or raw personal data.
"""

from __future__ import annotations


class EcoDrivingExplorerError(Exception):
    """Base class for all Eco Driving Explorer domain errors."""


class ProviderNotFoundError(EcoDrivingExplorerError):
    """Raised when no provider is registered for a client/family key."""


class UnsupportedPeriodTypeError(EcoDrivingExplorerError):
    """Raised when a provider is asked for a period type it does not support."""


class PeriodNotFoundError(EcoDrivingExplorerError):
    """Raised when the requested ranking period is not persisted."""


class RankingEntryNotFoundError(EcoDrivingExplorerError):
    """Raised when the requested ranking entry does not exist."""


class InvalidRankingGroupError(EcoDrivingExplorerError):
    """Raised when a ranking-group value is outside the allowed enum."""


class InvalidSortFieldError(EcoDrivingExplorerError):
    """Raised when a sort field or direction is outside the allowlist."""


class InvalidPaginationError(EcoDrivingExplorerError):
    """Raised when page/limit values violate the pagination contract."""


class ReconstructionUnavailableError(EcoDrivingExplorerError):
    """Raised when contributing trips cannot be reconstructed at all."""


class EnvironmentClientMismatchError(EcoDrivingExplorerError):
    """Raised when the caller-supplied client identity is inconsistent."""


class InvalidWeekSelectionError(EcoDrivingExplorerError):
    """Raised when a month/week basis selection cannot be canonicalized.

    Browser-supplied month and week identifiers are *selection input only*.
    They never widen authorization, so an unparseable or out-of-month value is
    a request error, never a fallback to a wider range.
    """
