"""NEWS2-style early warning score.

This is a faithful but simplified NEWS2 implementation: the standard
parameter bands, without the hypercapnic-respiratory-failure SpO2 Scale 2.
It exists to give the fusion model an objective baseline to pair worry
against, and to give the demo a credible "what the current system would have
said" number.

Reference bands: Royal College of Physicians NEWS2, 2017.
"""

from __future__ import annotations

from typing import Any

# A single band table keeps the scoring auditable -- you can diff it against
# the published chart rather than reading nested if-statements.
#   (low_inclusive, high_inclusive, points); None = unbounded
_BANDS: dict[str, list[tuple[float | None, float | None, int]]] = {
    "resp_rate": [
        (None, 8, 3),
        (9, 11, 1),
        (12, 20, 0),
        (21, 24, 2),
        (25, None, 3),
    ],
    "spo2": [
        (None, 91, 3),
        (92, 93, 2),
        (94, 95, 1),
        (96, None, 0),
    ],
    "temperature_c": [
        (None, 35.0, 3),
        (35.1, 36.0, 1),
        (36.1, 38.0, 0),
        (38.1, 39.0, 1),
        (39.1, None, 2),
    ],
    "systolic_bp": [
        (None, 90, 3),
        (91, 100, 2),
        (101, 110, 1),
        (111, 219, 0),
        (220, None, 3),
    ],
    "heart_rate": [
        (None, 40, 3),
        (41, 50, 1),
        (51, 90, 0),
        (91, 110, 1),
        (111, 130, 2),
        (131, None, 3),
    ],
}


def _band_score(param: str, value: float | None) -> int:
    """Points for one parameter. A missing value scores 0, not a crash.

    Charting in the real world is incomplete; a partial observation set should
    still produce a usable score rather than an exception.
    """
    if value is None:
        return 0
    for low, high, points in _BANDS[param]:
        if (low is None or value >= low) and (high is None or value <= high):
            return points
    return 0


def news_score(vitals: dict[str, Any]) -> int:
    """Total NEWS2 score for one observation set. Range 0-20ish."""
    total = sum(_band_score(param, vitals.get(param)) for param in _BANDS)

    # Supplemental oxygen: 2 points
    if vitals.get("supplemental_o2"):
        total += 2

    # Consciousness (AVPU): anything other than Alert scores 3
    loc = (vitals.get("consciousness") or "A").upper()
    if loc and loc != "A":
        total += 3

    return total


def news_band(score: int) -> str:
    """Clinical response band for a NEWS score."""
    if score >= 7:
        return "high"
    if score >= 5:
        return "medium"
    if score >= 1:
        return "low"
    return "none"
