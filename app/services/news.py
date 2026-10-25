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


#   points -> the label the approval review prints beside the value
_POINT_LABELS = {0: "normal", 1: "watch", 2: "elevated", 3: "high"}


def _direction(param: str, value: float) -> str:
    """Which side of normal a value sits on, read off the 0-point band.

    Derived rather than tabulated: the 0-point row already encodes where normal
    starts and stops, so a second hand-written table could only drift from it.
    """
    for low, high, points in _BANDS[param]:
        if points == 0:
            if low is not None and value < low:
                return "low"
            if high is not None and value > high:
                return "high"
            return "normal"
    return "normal"


def news_components(vitals: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-parameter NEWS2 contributions, for showing *why* a total is what it is.

    Returns {param: {"points", "label", "direction"}} for the five scored
    parameters only. Callers must not band anything else against this: a
    parameter NEWS2 does not score has no entry here, and rendering it as
    "normal" would assert a judgement the score never made.

    This is the single source of truth for those bands. Do not mirror _BANDS
    into the frontend -- two copies of a threshold table drift, and the copy
    that drifts is the one a nurse is looking at.
    """
    components: dict[str, dict[str, Any]] = {}
    for param in _BANDS:
        value = vitals.get(param)
        if value is None:
            continue
        points = _band_score(param, value)
        components[param] = {
            "points": points,
            "label": _POINT_LABELS[points],
            "direction": _direction(param, value),
        }
    return components


def news_band(score: int) -> str:
    """Clinical response band for a NEWS score."""
    if score >= 7:
        return "high"
    if score >= 5:
        return "medium"
    if score >= 1:
        return "low"
    return "none"


def news_alert(vitals: dict[str, Any] | None) -> dict[str, Any]:
    """NEWS response trigger, separate from the experimental combined score.

    RCP NEWS2 Chart 2: aggregate 5–6 is medium, >=7 high; any individual
    parameter scoring 3 has its own response trigger even below aggregate 5.
    Missing observations are not a NEWS zero or an all-clear.
    """
    if not vitals:
        return {"score": None, "band": "unknown", "single_red_parameters": []}
    score = vitals.get("news_score")
    score = int(score) if score is not None else news_score(vitals)
    red = [name for name, component in news_components(vitals).items() if component["points"] == 3]
    if (vitals.get("consciousness") or "A").upper() != "A":
        red.append("consciousness")
    band = news_band(score)
    if red and score < 5:
        band = "single_red"
    return {"score": score, "band": band, "single_red_parameters": red}
