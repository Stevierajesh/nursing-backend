"""Tests for the fusion rules.

The product claim under test: worry alone never escalates. If that breaks, the
answer to "won't this create false alarms?" stops being true, so these are the
tests worth having.
"""

from datetime import datetime, timedelta, timezone

from app.services.news import news_band, news_score
from app.services.risk import (
    NEWS_PAIRING_THRESHOLD,
    compute_risk,
    news_slope,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _vitals(minutes_ago: int, **overrides) -> dict:
    base = {
        "recorded_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)),
        "heart_rate": 75,
        "resp_rate": 16,
        "spo2": 98,
        "systolic_bp": 120,
        "diastolic_bp": 75,
        "temperature_c": 36.8,
        "supplemental_o2": False,
        "consciousness": "A",
    }
    base.update(overrides)
    base["news_score"] = news_score(base)
    return base


def _worry(level: int, minutes_ago: int = 1, reasons=None) -> dict:
    return {
        "level": level,
        "reasons": reasons or ["just_a_feeling"],
        "created_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)),
    }


# ---------------------------------------------------------------------------
# NEWS
# ---------------------------------------------------------------------------


def test_normal_vitals_score_zero():
    assert news_score(_vitals(0)) == 0
    assert news_band(0) == "none"


def test_known_news_bands():
    # RR 22 -> 2, SpO2 93 -> 2, supplemental O2 -> 2, SBP 95 -> 2, HR 115 -> 2
    v = _vitals(
        0, resp_rate=22, spo2=93, supplemental_o2=True, systolic_bp=95, heart_rate=115
    )
    assert news_score(v) == 10
    assert news_band(10) == "high"


def test_missing_values_do_not_crash():
    partial = {"recorded_at": _iso(datetime.now(timezone.utc)), "heart_rate": 80}
    assert news_score(partial) == 0


def test_altered_consciousness_scores_three():
    assert news_score(_vitals(0, consciousness="V")) == 3


# ---------------------------------------------------------------------------
# trend
# ---------------------------------------------------------------------------


def test_single_observation_has_no_slope():
    assert news_slope([_vitals(10)]) == 0.0


def test_rising_news_gives_positive_slope():
    rows = [
        _vitals(10, resp_rate=24, spo2=93),   # newest, worse
        _vitals(70, resp_rate=21),
        _vitals(130, resp_rate=18),
        _vitals(190),                          # oldest, normal
    ]
    assert news_slope(rows) > 0


def test_improving_news_gives_negative_slope():
    rows = [
        _vitals(10),                           # newest, normal
        _vitals(70, resp_rate=21),
        _vitals(130, resp_rate=24, spo2=93),   # oldest, worse
    ]
    assert news_slope(rows) < 0


# ---------------------------------------------------------------------------
# the core claim: worry alone does not escalate
# ---------------------------------------------------------------------------


def test_very_worried_with_stable_patient_does_not_escalate():
    """The headline rule. A worried nurse plus a rock-stable patient is a
    flag on the board, not a page."""
    stable = [_vitals(i * 60) for i in range(6)]
    result = compute_risk(stable, [_worry(4)])

    assert result.should_escalate is False
    assert result.rule is None
    # but the worry is still visible in the score
    assert result.worry_component > 0
    assert result.worry_level == 4


def test_worry_raises_score_without_escalating():
    stable = [_vitals(i * 60) for i in range(6)]
    without = compute_risk(stable, [])
    with_worry = compute_risk(stable, [_worry(4)])

    assert with_worry.score > without.score
    assert with_worry.should_escalate is False


def test_very_worried_plus_drift_escalates():
    drifting = [
        _vitals(10, resp_rate=22, spo2=94),
        _vitals(70, resp_rate=20, spo2=95),
        _vitals(130, resp_rate=18),
        _vitals(190),
    ]
    result = compute_risk(drifting, [_worry(4, reasons=["breathing_off"])])

    assert result.should_escalate is True
    assert result.rule in ("very_worried_plus_drift", "very_worried_plus_news")


def test_slight_worry_needs_both_drift_and_news():
    """Level 1 worry has the strictest bar -- it needs drift AND an elevated
    NEWS, because a mild hunch on a mildly abnormal patient is the single
    biggest false-alarm source."""
    mild_drift_only = [
        _vitals(10, resp_rate=21),
        _vitals(70, resp_rate=19),
        _vitals(130),
    ]
    result = compute_risk(mild_drift_only, [_worry(1)])
    assert result.latest_news < NEWS_PAIRING_THRESHOLD
    assert result.should_escalate is False


def test_high_news_escalates_without_any_worry():
    """The worry layer augments the existing safety net; it must not replace
    it. An objectively crashing patient escalates even if nobody tapped."""
    bad = [_vitals(5, resp_rate=26, spo2=91, systolic_bp=88, heart_rate=125)]
    result = compute_risk(bad, [])

    assert result.latest_news >= 7
    assert result.should_escalate is True
    assert result.rule == "news_high"
    assert result.worry_level == 0


def test_level_zero_worry_is_not_worry():
    """'Not worried' is a real signal worth recording, but it must not
    contribute risk."""
    stable = [_vitals(i * 60) for i in range(4)]
    result = compute_risk(stable, [_worry(0)])

    assert result.worry_component == 0.0
    assert result.worry_level == 0
    assert result.should_escalate is False


def test_old_worry_decays():
    stable = [_vitals(i * 60) for i in range(4)]
    fresh = compute_risk(stable, [_worry(4, minutes_ago=1)])
    stale = compute_risk(stable, [_worry(4, minutes_ago=720)])  # 12h, 3 half-lives

    assert stale.worry_component < fresh.worry_component
    # decayed but not erased -- it still happened
    assert stale.worry_component > 0


def test_empty_inputs_are_safe():
    result = compute_risk([], [])
    assert result.score == 0.0
    assert result.should_escalate is False
    assert result.latest_news == 0


def test_five_worry_levels_have_increasing_intensity():
    from unittest.mock import patch

    now = datetime.now(timezone.utc)
    with patch('app.services.risk._now', return_value=now):
        results = [compute_risk([], [{'level': level, 'created_at': now.isoformat()}]) for level in range(5)]
    assert [r.worry_component for r in results] == [0, 6.25, 12.5, 18.75, 25]
    assert all(not r.should_escalate for r in results)


def test_intermediate_worry_levels_need_both_objective_signals():
    # Somewhat/Worried do not inherit the old level-2 "Very worried" rule.
    for level in (1, 2, 3):
        elevated_only = compute_risk([{**_vitals(0), "news_score": 5}], [_worry(level)])
        assert elevated_only.should_escalate is False
        rising = [{**_vitals(0), "news_score": 5}, {**_vitals(60), "news_score": 4}]
        assert compute_risk(rising, [_worry(level)]).should_escalate is True


def test_worry_validation_accepts_exactly_five_integer_levels():
    import pytest
    from pydantic import ValidationError
    from app.schemas.requests import WorryCreate

    for level in range(5):
        assert WorryCreate(patient_id='patient', level=level).level == level
    for level in (-1, 5, 1.5, True, '2'):
        with pytest.raises(ValidationError):
            WorryCreate(patient_id='patient', level=level)


def test_jerry_news_warning_is_independent_of_low_combined_score():
    from app.services.news import news_alert
    # The weighted model can be low while NEWS independently requires review.
    observation = _vitals(0, heart_rate=181, resp_rate=24, systolic_bp=133)
    result = compute_risk([observation], [])
    assert result.score < 35
    assert news_alert(observation) == {
        "score": 5, "band": "medium", "single_red_parameters": ["heart_rate"]
    }


def test_single_red_parameter_is_not_hidden_by_low_news_total():
    from app.services.news import news_alert
    alert = news_alert(_vitals(0, heart_rate=140))
    assert alert["score"] == 3
    assert alert["band"] == "single_red"
    assert alert["single_red_parameters"] == ["heart_rate"]


def test_news_alert_high_and_missing_observations():
    from app.services.news import news_alert
    assert news_alert(_vitals(0, heart_rate=125, resp_rate=24, systolic_bp=80))["band"] == "high"
    assert news_alert(None) == {"score": None, "band": "unknown", "single_red_parameters": []}
    assert news_alert(_vitals(0))["band"] == "none"


def test_news_alert_calculates_when_persisted_total_is_missing():
    from app.services.news import news_alert
    observation = _vitals(0, consciousness="V")
    del observation["news_score"]
    assert news_alert(observation) == {"score": 3, "band": "single_red", "single_red_parameters": ["consciousness"]}
