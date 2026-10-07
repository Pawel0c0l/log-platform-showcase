"""Presentation-only derivations over the repository's Eco Driving scoring rules.

Nothing here defines a scoring rule. Every value is read from
:mod:`api.eco_driving_explorer.eco_scoring` — the same deterministic
configuration the aggregation job scores with — or from a persisted stats
column. This module exists so the HTML layer can ask "how severe is this
metric?" and "which rung of the ladder applies?" without inventing an answer.

Two invariants are load-bearing and are covered by explicit regression tests:

1. **Semantic severity is always coefficient-derived.** The severity of a metric
   cell comes from the awarded points for the normalized ``/ 100 km``
   coefficient, never from the displayed number. When the user switches the
   display unit to ``Σ suma`` the rendered value changes and the colour does
   not, because the colour never looked at that value in the first place. There
   are deliberately **no** raw-count thresholds anywhere in this module.

2. **``Próg`` never collapses a ladder into one number.** The repository scores
   each metric with a multi-step bucket ladder, so the applicable rung is
   presented together with its position in the ladder (``krok 3/7``) and the
   whole ladder is available as the cell's title. Presenting only the first
   bucket bound would misstate the business rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .eco_scoring import (
    METRIC_MAX_POINTS,
    REQUIRED_METRICS,
    SCORING_RULES,
    RateValue,
    round_rate_for_scoring,
)

# Severity vocabulary. These are *derived states*, not thresholds: `ok` means the
# metric lost nothing against its own maximum, `warn` means it lost points while
# still earning a non-negative amount, `bad` means the coefficient reached a rung
# that subtracts from the total score. All three come from the repository ladder.
SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_BAD = "bad"
SEVERITY_NONE = "none"

SEVERITY_ORDER = (SEVERITY_NONE, SEVERITY_OK, SEVERITY_WARN, SEVERITY_BAD)

# The non-colour carrier for each severity (ACCESSIBILITY_SPEC §2: no state is
# communicated by colour alone). The marker is decorative; the word is the
# accessible text.
SEVERITY_MARKER = {
    SEVERITY_OK: "",
    SEVERITY_WARN: "▪",  # ▪
    SEVERITY_BAD: "▲",  # ▲
    SEVERITY_NONE: "",
}

SEVERITY_WORD_PL = {
    SEVERITY_OK: "bez straty punktów",
    SEVERITY_WARN: "strata punktów",
    SEVERITY_BAD: "punkty ujemne",
    SEVERITY_NONE: "brak danych",
}


@dataclass(frozen=True)
class LadderStep:
    """One rung of a metric's scoring ladder, as configured in the repository."""

    index: int  # 1-based
    total_steps: int
    upper_bound: Optional[Decimal]  # None == the open final step
    points: int

    @property
    def is_final(self) -> bool:
        return self.upper_bound is None


def ladder_steps(metric_key: str) -> tuple[LadderStep, ...]:
    """The full configured ladder for one metric, closed step by open final step."""

    rules = SCORING_RULES.get(metric_key)
    if rules is None:
        raise ValueError(f"unknown Eco Driving metric: {metric_key!r}")
    total = len(rules.buckets) + 1
    steps = [
        LadderStep(index=position + 1, total_steps=total, upper_bound=bucket.upper_bound, points=bucket.points)
        for position, bucket in enumerate(rules.buckets)
    ]
    steps.append(LadderStep(index=total, total_steps=total, upper_bound=None, points=rules.final_points))
    return tuple(steps)


def applicable_ladder_step(metric_key: str, rate: RateValue) -> Optional[LadderStep]:
    """The rung the normalized coefficient lands on, or ``None`` without a rate.

    Selection reproduces :func:`eco_scoring.score_metric` exactly: the rate is
    rounded ``ROUND_HALF_UP`` to a whole coefficient first, then compared with
    the bucket bounds in order.
    """

    rounded = round_rate_for_scoring(rate)
    if rounded is None:
        return None
    for step in ladder_steps(metric_key):
        if step.upper_bound is not None and rounded <= step.upper_bound:
            return step
    return ladder_steps(metric_key)[-1]


def metric_max_points(metric_key: str) -> int:
    return METRIC_MAX_POINTS[metric_key]


def loss_from_points(metric_key: str, points: Optional[Decimal]) -> Optional[Decimal]:
    """Points lost versus this metric's maximum — the persisted loss semantics.

    Mirrors ``calculate_maxpoints_subtractions``: ``min(points - max, 0)``, so
    the value is always ``<= 0``. Used only when the persisted
    ``*_maxpoints_subtract`` column is absent for a row.
    """

    if points is None:
        return None
    delta = Decimal(points) - Decimal(METRIC_MAX_POINTS[metric_key])
    return delta if delta < 0 else Decimal(0)


def metric_severity(
    metric_key: str,
    *,
    points: Optional[Decimal],
    loss: Optional[Decimal] = None,
) -> str:
    """Severity of one metric, derived from the coefficient-scored points.

    ``points`` is the persisted per-metric point value, which the aggregation
    job computed from the rounded ``/ 100 km`` coefficient. ``loss`` is the
    persisted ``*_maxpoints_subtract`` when available. Neither is a raw event
    count, and no caller may pass one: this function has no notion of a count,
    which is exactly why the ``Σ suma`` display mode cannot change its answer.
    """

    if points is None:
        # A persisted loss can still classify a row whose points column is null.
        if loss is None:
            return SEVERITY_NONE
        return SEVERITY_OK if Decimal(loss) >= 0 else SEVERITY_WARN
    points_dec = Decimal(points)
    effective_loss = Decimal(loss) if loss is not None else loss_from_points(metric_key, points_dec)
    if effective_loss is not None and effective_loss >= 0:
        return SEVERITY_OK
    if points_dec < 0:
        return SEVERITY_BAD
    return SEVERITY_WARN


def severity_from_rate(metric_key: str, rate: RateValue) -> str:
    """Severity computed straight from a normalized coefficient.

    Used where a row carries a rate but no persisted point column (for example
    a reconstructed diagnostic row). It re-enters the repository ladder rather
    than approximating it.
    """

    step = applicable_ladder_step(metric_key, rate)
    if step is None:
        return SEVERITY_NONE
    return metric_severity(metric_key, points=Decimal(step.points))


def loss_share_percent(
    loss: Optional[Decimal],
    total_loss_magnitude: Decimal,
) -> Optional[Decimal]:
    """This metric's share of the driver's total lost points, in percent.

    The denominator is the total lost-point **magnitude** for the same driver
    and period. When nothing was lost the share is undefined and stays ``None``
    — a zero-loss driver must not be shown a fabricated ``0 %`` of nothing.
    """

    if loss is None or total_loss_magnitude <= 0:
        return None
    magnitude = abs(Decimal(loss))
    return (magnitude * Decimal(100) / total_loss_magnitude).quantize(Decimal("0.1"))


def total_loss_magnitude(losses: dict[str, Optional[Decimal]]) -> Decimal:
    """Sum of the absolute per-metric losses for one driver and period."""

    total = Decimal(0)
    for metric in REQUIRED_METRICS:
        value = losses.get(metric)
        if value is not None:
            total += abs(Decimal(value))
    return total


def composition_sort_key(metric_key: str, loss: Optional[Decimal]) -> tuple[int, Decimal, int]:
    """Sort composition rows by lost-point impact descending, deterministically.

    Rows with no loss value at all sort last. Equal losses fall back to the
    canonical metric order from ``REQUIRED_METRICS``, so two metrics that lost
    the same number of points always render in the same order.
    """

    order = REQUIRED_METRICS.index(metric_key) if metric_key in REQUIRED_METRICS else len(REQUIRED_METRICS)
    if loss is None:
        return (1, Decimal(0), order)
    return (0, Decimal(loss), order)
