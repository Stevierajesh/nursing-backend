"""Risk fusion: combine nurse worry with objective vitals data.

This module encodes the central design claim of the Nurse Worry Layer:

    Worry alone never raises an alarm.

A worried nurse with a stable patient produces a visible flag on the charge
nurse's board and nothing more. An alarm fires only when worry coincides with
an objective signal -- an elevated NEWS, or a NEWS that is drifting upward.
That pairing is what keeps precision high enough to answer the "won't this
create false alarms?" question with a number instead of a promise.

The weights below are hand-set priors, not learned. They are deliberately
exposed as module constants so they can be tuned from observed outcomes once
`outcomes` has enough rows, and so the demo can explain every number on
screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.news import news_band, news_score

# ---------------------------------------------------------------------------
# tunable priors
# ---------------------------------------------------------------------------

# How long a worry signal stays clinically relevant.
WORRY_HALF_LIFE_MINUTES = 240

# NEWS is 0..~20 in practice; normalise against this ceiling.
NEWS_CEILING = 12.0

# Window over which NEWS slope is measured.
TREND_WINDOW_HOURS = 8
# NEWS points per hour that counts as a meaningful upward drift.
TREND_DRIFT_THRESHOLD = 0.25

# Fusion weights. Sum to 1.0 so `score` lands on a 0-100 scale.
W_VITALS = 0.45
W_TREND = 0.30
W_WORRY = 0.25

# Objective thresholds that worry can pair with to justify an escalation.
NEWS_PAIRING_THRESHOLD = 5  # NEWS medium band
NEWS_HIGH_THRESHOLD = 7  # NEWS high band -- escalates on its own


@dataclass
class RiskResult:
    score: float
    worry_component: float
    vitals_component: float
    trend_component: float
    latest_news: int
    news_band: str
    trend_slope: float
    worry_level: int
    should_escalate: bool
    rule: str | None
    inputs: dict[str, Any] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime:
    """Parse a Postgres timestamptz as returned by the Supabase REST API."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------


def vitals_component(latest_news: int) -> float:
    """Current objective severity, 0..1."""
    return min(latest_news / NEWS_CEILING, 1.0)


def news_slope(vitals_rows: list[dict[str, Any]]) -> float:
    """Least-squares slope of NEWS over the trend window, in points per hour.

    Positive means deteriorating. Returns 0.0 with fewer than two observations
    in the window -- a single data point has no direction, and guessing one
    would manufacture the very false alarm this design is trying to avoid.
    """
    cutoff = _now() - timedelta(hours=TREND_WINDOW_HOURS)

    points: list[tuple[float, float]] = []
    for row in vitals_rows:
        ts = _parse_ts(row["recorded_at"])
        if ts < cutoff:
            continue
        score = row.get("news_score")
        if score is None:
            score = news_score(row)
        # x in hours relative to the cutoff
        points.append(((ts - cutoff).total_seconds() / 3600.0, float(score)))

    if len(points) < 2:
        return 0.0

    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator == 0:
        return 0.0
    return numerator / denominator


def trend_component(slope: float) -> float:
    """Upward drift normalised to 0..1.

    Only upward movement contributes. An improving patient should not get
    credit for the magnitude of their improvement.
    """
    if slope <= 0:
        return 0.0
    # 1.0 point/hour of NEWS deterioration saturates the component.
    return min(slope / 1.0, 1.0)


def worry_component(worry_rows: list[dict[str, Any]]) -> tuple[float, int]:
    """Time-decayed worry intensity, 0..1, plus the raw peak level.

    Returns (component, peak_level). Decay is exponential on
    WORRY_HALF_LIFE_MINUTES so that a worry raised eight hours ago no longer
    dominates the score, but is not discarded either.
    """
    if not worry_rows:
        return 0.0, 0

    now = _now()
    best = 0.0
    peak_level = 0

    for row in worry_rows:
        level = int(row.get("level") or 0)
        if level == 0:
            continue
        age_minutes = (now - _parse_ts(row["created_at"])).total_seconds() / 60.0
        decay = 0.5 ** (age_minutes / WORRY_HALF_LIFE_MINUTES)
        # Five levels: 0=none, 1=slightly, 2=somewhat, 3=worried, 4=very.
        intensity = (level / 4.0) * decay
        if intensity > best:
            best = intensity
        peak_level = max(peak_level, level)

    return min(best, 1.0), peak_level


# ---------------------------------------------------------------------------
# escalation rules
# ---------------------------------------------------------------------------


def _decide_escalation(
    worry_level: int, latest_news: int, slope: float
) -> tuple[bool, str | None]:
    """Decide whether this patient state warrants paging the charge nurse.

    Rules are evaluated most-specific first and the matched rule name is
    stored on the escalation, so the dashboard can always show the nurse
    exactly why they were paged.
    """
    drifting = slope >= TREND_DRIFT_THRESHOLD

    # Objective deterioration escalates regardless of worry -- the worry layer
    # augments the existing safety net, it does not replace it.
    if latest_news >= NEWS_HIGH_THRESHOLD:
        return True, "news_high"

    # The core pairing rules.
    if worry_level >= 4 and drifting:
        return True, "very_worried_plus_drift"
    if worry_level >= 4 and latest_news >= NEWS_PAIRING_THRESHOLD:
        return True, "very_worried_plus_news"
    if worry_level >= 1 and drifting and latest_news >= NEWS_PAIRING_THRESHOLD:
        return True, "worried_plus_drift_and_news"

    # Worry with a stable patient: flag on the board, do not alarm. This branch
    # is the whole reason the layer does not add to alarm fatigue.
    return False, None


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def compute_risk(
    vitals_rows: list[dict[str, Any]],
    worry_rows: list[dict[str, Any]],
) -> RiskResult:
    """Fuse vitals and worry into a 0-100 risk score plus an escalation verdict.

    `vitals_rows` and `worry_rows` should be newest-first, which is the order
    the API queries return.
    """
    latest = vitals_rows[0] if vitals_rows else {}
    latest_news = latest.get("news_score")
    if latest_news is None:
        latest_news = news_score(latest) if latest else 0
    latest_news = int(latest_news)

    slope = news_slope(vitals_rows)
    v_comp = vitals_component(latest_news)
    t_comp = trend_component(slope)
    w_comp, worry_level = worry_component(worry_rows)

    score = 100.0 * (W_VITALS * v_comp + W_TREND * t_comp + W_WORRY * w_comp)
    should_escalate, rule = _decide_escalation(worry_level, latest_news, slope)

    return RiskResult(
        score=round(score, 2),
        worry_component=round(100.0 * W_WORRY * w_comp, 2),
        vitals_component=round(100.0 * W_VITALS * v_comp, 2),
        trend_component=round(100.0 * W_TREND * t_comp, 2),
        latest_news=latest_news,
        news_band=news_band(latest_news),
        trend_slope=round(slope, 3),
        worry_level=worry_level,
        should_escalate=should_escalate,
        rule=rule,
        inputs={
            "latest_news": latest_news,
            "news_slope_per_hour": round(slope, 3),
            "drifting": slope >= TREND_DRIFT_THRESHOLD,
            "worry_level": worry_level,
            "vitals_observations": len(vitals_rows),
            "worry_signals": len(worry_rows),
            "weights": {"vitals": W_VITALS, "trend": W_TREND, "worry": W_WORRY},
        },
    )


# Human-readable rule descriptions for the dashboard.
RULE_LABELS = {
    "news_high": "NEWS in high band (≥7)",
    "very_worried_plus_drift": "Nurse very worried + NEWS drifting upward",
    "very_worried_plus_news": "Nurse very worried + NEWS ≥5",
    "worried_plus_drift_and_news": "Nurse worried + NEWS ≥5 and drifting",
    "sla_breach": "No reassessment within SLA window",
}
