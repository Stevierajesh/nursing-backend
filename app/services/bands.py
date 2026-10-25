"""Observation bands for parameters NEWS2 does not score.

NEWS2 answers one question: how much does this observation contribute to a
deterioration score. That is not the same question as "is this value
abnormal", and the difference is not academic -- NEWS2 scores a blood
pressure of 168/102 as zero, because it watches for shock, not hypertension.

Anything banded here is therefore deliberately *outside* news.py and must stay
that way. Mixing the two tables would let a non-NEWS2 judgement leak into
news_score() and quietly change a cited standard.

Reference: pain is the 0-10 numeric rating scale, the conventional
mild/moderate/severe cut points.
"""

from __future__ import annotations

# (high_inclusive, label); None = unbounded. Ordered low to high.
_PAIN_BANDS: list[tuple[int | None, str]] = [
    (0, "normal"),   # no pain reported
    (3, "normal"),   # mild
    (6, "watch"),    # moderate
    (None, "high"),  # severe
]


def pain_band(score: int | None) -> dict[str, object] | None:
    """Band a 0-10 pain rating, or None when pain was not charted.

    Returning None rather than "normal" is the point: an unasked pain score and
    a pain score of zero are different facts, and only one of them is good news.
    """
    if score is None:
        return None
    for high, label in _PAIN_BANDS:
        if high is None or score <= high:
            return {"label": label, "direction": "high" if label != "normal" else "normal"}
    return {"label": "normal", "direction": "normal"}
