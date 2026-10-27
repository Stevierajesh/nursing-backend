"""A recommended medication class per condition, from the latest observation.

Read this before changing a threshold.

This module does two separable things, and the split is the whole reason it is
safe to ship:

  1. **Bands an observation** against a published cut point. `systolic 168` is
     ACC/AHA 2017 stage 2 hypertension whoever is reading it, and that part is
     citable.
  2. **Selects a medication class** for that band. This part is *not* citable.
     It is a placeholder rule set over the placeholder vocabulary in
     `medication_classes`, and every recommendation it produces carries
     `UNREVIEWED_RULES` so no surface can present it as clinical guidance.

Keeping (2) labelled is not decoration. A nurse who reads "Salus recommends an
ARB" as a clinical recommendation, when the mapping was authored by whoever
wrote this file, is the failure mode worth designing against -- and it is why
`HANDOFF.md` had app-authored drug recommendations on the do-not-build list.
What makes this version shippable is that the recommendation states its own
provenance, the nurse can counter it, and a prescriber still decides.

Two deliberate silences:

  * **In-range observations get no recommendation.** A recommendation on every
    patient is noise, and noise is what the product exists not to add.
  * **Low values get no recommendation.** Hypoglycaemia and bradycardia are
    real findings, but the action is usually to *withhold* an agent, not start
    one, and this vocabulary contains nothing that can say that. A band is
    returned so the UI can show the finding; `class_label` stays None.

Thresholds are module constants so a reviewer can diff them against the
published chart without reading any logic.
"""

from __future__ import annotations

from typing import Any

CONDITIONS = ("blood_pressure", "blood_sugar", "heart_rate")

# The marker that rides on every recommendation. The frontend keys its warning
# off this string, the same way it does for `medication_classes.source`, so
# replacing the rules with a reviewed protocol removes the warning on its own.
UNREVIEWED_RULES = "UNREVIEWED — placeholder rule set, not clinical guidance"

# A reviewed rule set would carry its protocol name here instead.
RULES_VERSION = "placeholder-1"


# ---------------------------------------------------------------------------
# bands
# ---------------------------------------------------------------------------
#
# Each entry is (low_inclusive, high_inclusive, band, note). None is unbounded.
# `band` is one of: low, normal, watch, high, critical.

# ACC/AHA 2017: elevated 120-129 systolic, stage 1 130-139 or 80-89 diastolic,
# stage 2 >=140 or >=90, crisis >180 or >120. Hypotension is NEWS2's business,
# not this table's -- see news.py.
_SYSTOLIC_BANDS = [
    (None, 89, "low", "Hypotensive — scored by NEWS2"),
    (90, 129, "normal", "At or below the ACC/AHA elevated threshold"),
    (130, 139, "watch", "ACC/AHA stage 1 hypertension"),
    (140, 180, "high", "ACC/AHA stage 2 hypertension"),
    (181, None, "critical", "ACC/AHA hypertensive crisis range"),
]

_DIASTOLIC_BANDS = [
    (None, 59, "low", "Low diastolic"),
    (60, 79, "normal", "Below the ACC/AHA stage 1 threshold"),
    (80, 89, "watch", "ACC/AHA stage 1 hypertension"),
    (90, 120, "high", "ACC/AHA stage 2 hypertension"),
    (121, None, "critical", "ACC/AHA hypertensive crisis range"),
]

# ADA: <70 mg/dL is hypoglycaemia, >180 mg/dL exceeds the usual postprandial
# target, >=250 mg/dL is where ketosis risk enters the conversation.
_GLUCOSE_BANDS = [
    (None, 69, "low", "Hypoglycaemic (ADA <70 mg/dL)"),
    (70, 180, "normal", "Within the usual ADA target range"),
    (181, 249, "watch", "Above the usual ADA postprandial target"),
    (250, 399, "high", "Marked hyperglycaemia (ADA ≥250 mg/dL)"),
    (400, None, "critical", "Severe hyperglycaemia"),
]

# Conventional adult cut points. NEWS2 scores both tails of heart rate, so the
# low end is labelled and then left alone here.
_HEART_RATE_BANDS = [
    (None, 49, "low", "Bradycardic — scored by NEWS2"),
    (50, 100, "normal", "Within the conventional adult range"),
    (101, 130, "watch", "Tachycardic"),
    (131, 150, "high", "Marked tachycardia"),
    (151, None, "critical", "Severe tachycardia"),
]

_SEVERITY = {"low": 0, "normal": 0, "watch": 1, "high": 2, "critical": 3}


# ---------------------------------------------------------------------------
# band -> class label
# ---------------------------------------------------------------------------
#
# THIS TABLE IS THE UNREVIEWED PART. The labels must match
# `medication_classes.label` exactly; a label with no matching active row
# yields no recommendation rather than a near-miss, because silently
# substituting a different class is worse than saying nothing.

_CLASS_FOR_BAND: dict[str, dict[str, str]] = {
    "blood_pressure": {
        "watch": "Thiazide diuretic",
        "high": "ACE inhibitor",
        "critical": "Calcium channel blocker",
    },
    "blood_sugar": {
        "watch": "Biguanide",
        "high": "Rapid-acting insulin",
        "critical": "Rapid-acting insulin",
    },
    "heart_rate": {
        "watch": "Beta blocker",
        "high": "Beta blocker",
        "critical": "Antiarrhythmic",
    },
}


def _band_for(value: Any, table: list[tuple[int | None, int | None, str, str]]):
    """Band one numeric observation, or None when it was not charted."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    for low, high, band, note in table:
        if (low is None or number >= low) and (high is None or number <= high):
            return {"value": number, "band": band, "note": note}
    return None


def assess(condition: str, vitals: dict[str, Any] | None) -> dict[str, Any] | None:
    """Band the observation behind one condition.

    Returns None when nothing relevant was charted -- which is different from
    "normal", and the caller must not collapse the two.
    """
    if condition not in CONDITIONS or not vitals:
        return None

    if condition == "blood_pressure":
        systolic = _band_for(vitals.get("systolic_bp"), _SYSTOLIC_BANDS)
        diastolic = _band_for(vitals.get("diastolic_bp"), _DIASTOLIC_BANDS)
        parts = [p for p in (systolic, diastolic) if p]
        if not parts:
            return None
        # Worst of the two drives the band: 128/104 is stage 2 on the
        # diastolic alone, and reading only the systolic would call it normal.
        worst = max(parts, key=lambda p: _SEVERITY[p["band"]])
        reading = "/".join(
            str(int(p["value"])) for p in (systolic, diastolic) if p
        )
        return {"band": worst["band"], "note": worst["note"], "reading": f"{reading} mmHg"}

    if condition == "blood_sugar":
        part = _band_for(vitals.get("glucose_mg_dl"), _GLUCOSE_BANDS)
        if not part:
            return None
        return {
            "band": part["band"],
            "note": part["note"],
            "reading": f"{int(part['value'])} mg/dL",
        }

    part = _band_for(vitals.get("heart_rate"), _HEART_RATE_BANDS)
    if not part:
        return None
    return {"band": part["band"], "note": part["note"], "reading": f"{int(part['value'])} bpm"}


def recommend(condition: str, vitals: dict[str, Any] | None) -> dict[str, Any] | None:
    """Recommend a medication class label for one patient and condition.

    Returns None when there is nothing to say. When there is a finding but no
    class for it -- a low value, or a band this table does not map -- the
    assessment comes back with `class_label: None`, so the UI can show the
    finding without implying a drug answers it.
    """
    assessment = assess(condition, vitals)
    if assessment is None:
        return None
    if assessment["band"] == "normal":
        return None

    label = _CLASS_FOR_BAND.get(condition, {}).get(assessment["band"])
    return {
        "condition": condition,
        "band": assessment["band"],
        "reading": assessment["reading"],
        # The citable half: which published cut point this reading fell in.
        "rationale": assessment["note"],
        "class_label": label,
        "rules_version": RULES_VERSION,
        "rules_source": UNREVIEWED_RULES,
    }
