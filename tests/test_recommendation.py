"""Tests for the medication recommendation rules.

Two things are worth pinning here, and only one of them is about thresholds.

The first is that the rules stay *quiet*: an in-range observation and a missing
observation both produce nothing. A recommendation on every patient is the
alarm-fatigue problem wearing a different hat.

The second is that every recommendation carries its own provenance. The band is
citable, the class mapping is not, and a surface that loses the second fact
while keeping the first is how a placeholder gets read as clinical guidance.
"""

from app.services.recommendation import (
    CONDITIONS,
    RULES_VERSION,
    UNREVIEWED_RULES,
    _CLASS_FOR_BAND,
    assess,
    recommend,
)


def _vitals(**overrides) -> dict:
    base = {
        "heart_rate": 75,
        "resp_rate": 16,
        "spo2": 98,
        "systolic_bp": 118,
        "diastolic_bp": 74,
        "glucose_mg_dl": 100,
        "temperature_c": 36.8,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# silence
# ---------------------------------------------------------------------------


def test_a_normal_patient_gets_no_recommendation_for_anything():
    well = _vitals()
    for condition in CONDITIONS:
        assert recommend(condition, well) is None, condition


def test_an_uncharted_observation_gets_no_recommendation():
    """Absent is not normal, and neither produces a suggestion."""
    assert recommend("blood_sugar", _vitals(glucose_mg_dl=None)) is None
    assert recommend("heart_rate", _vitals(heart_rate=None)) is None
    assert recommend("blood_pressure", _vitals(systolic_bp=None, diastolic_bp=None)) is None


def test_no_vitals_at_all_is_safe():
    for condition in CONDITIONS:
        assert recommend(condition, None) is None
        assert recommend(condition, {}) is None


def test_an_unknown_condition_is_not_guessed_at():
    assert recommend("potassium", _vitals()) is None
    assert assess("potassium", _vitals()) is None


def test_a_non_numeric_observation_does_not_crash():
    assert recommend("heart_rate", _vitals(heart_rate="unknown")) is None


# ---------------------------------------------------------------------------
# low readings: a finding, but not a drug
# ---------------------------------------------------------------------------


def test_hypoglycaemia_is_banded_but_gets_no_class():
    """The action is to withhold, and this vocabulary cannot say that.

    Returning a class here would be the one genuinely dangerous output the
    rules could produce -- recommending insulin for a glucose of 54.
    """
    result = recommend("blood_sugar", _vitals(glucose_mg_dl=54))
    assert result is not None
    assert result["band"] == "low"
    assert result["class_label"] is None
    assert "70" in result["rationale"]


def test_bradycardia_is_banded_but_gets_no_class():
    result = recommend("heart_rate", _vitals(heart_rate=42))
    assert result is not None
    assert result["band"] == "low"
    assert result["class_label"] is None


def test_hypotension_is_banded_but_gets_no_class():
    result = recommend("blood_pressure", _vitals(systolic_bp=82, diastolic_bp=55))
    assert result is not None
    assert result["band"] == "low"
    assert result["class_label"] is None


def test_no_low_band_maps_to_a_class_in_the_table():
    """Structural guard: the mapping must never acquire a 'low' entry."""
    for condition, mapping in _CLASS_FOR_BAND.items():
        assert "low" not in mapping, condition
        assert "normal" not in mapping, condition


# ---------------------------------------------------------------------------
# bands against the published cut points
# ---------------------------------------------------------------------------


def test_acc_aha_stage_boundaries():
    def band(systolic, diastolic=70):
        return assess("blood_pressure", _vitals(systolic_bp=systolic, diastolic_bp=diastolic))["band"]

    assert band(129) == "normal"
    assert band(130) == "watch"     # stage 1
    assert band(139) == "watch"
    assert band(140) == "high"      # stage 2
    assert band(180) == "high"
    assert band(181) == "critical"  # crisis


def test_the_worse_of_the_two_pressures_drives_the_band():
    """128/104 is stage 2 on the diastolic alone.

    Reading only the systolic would call this patient normal, which is the
    exact misread the pair exists to prevent.
    """
    result = assess("blood_pressure", _vitals(systolic_bp=128, diastolic_bp=104))
    assert result["band"] == "high"
    assert result["reading"] == "128/104 mmHg"


def test_a_single_pressure_still_bands():
    result = assess("blood_pressure", _vitals(systolic_bp=165, diastolic_bp=None))
    assert result["band"] == "high"
    assert result["reading"] == "165 mmHg"


def test_ada_glucose_boundaries():
    def band(value):
        return assess("blood_sugar", _vitals(glucose_mg_dl=value))["band"]

    assert band(69) == "low"
    assert band(70) == "normal"
    assert band(180) == "normal"
    assert band(181) == "watch"
    assert band(250) == "high"
    assert band(400) == "critical"


def test_heart_rate_boundaries():
    def band(value):
        return assess("heart_rate", _vitals(heart_rate=value))["band"]

    assert band(49) == "low"
    assert band(100) == "normal"
    assert band(101) == "watch"
    assert band(131) == "high"
    assert band(151) == "critical"


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_every_recommendation_declares_itself_unreviewed():
    """The warning in the UI keys off this string, for both nurse and doctor.

    Replace the rules with a reviewed protocol and the caveat disappears on its
    own -- which only works if the marker is on every single row.
    """
    findings = [
        recommend("blood_pressure", _vitals(systolic_bp=168, diastolic_bp=102)),
        recommend("blood_sugar", _vitals(glucose_mg_dl=260)),
        recommend("heart_rate", _vitals(heart_rate=118)),
        recommend("blood_sugar", _vitals(glucose_mg_dl=54)),  # low: no class
    ]
    for result in findings:
        assert result is not None
        assert result["rules_source"] == UNREVIEWED_RULES
        assert result["rules_version"] == RULES_VERSION
        assert result["rules_source"].startswith("UNREVIEWED")


def test_a_recommendation_carries_the_reading_it_was_made_from():
    """So a decision stays auditable after the vitals have moved on."""
    result = recommend("blood_pressure", _vitals(systolic_bp=168, diastolic_bp=102))
    assert result["reading"] == "168/102 mmHg"
    assert result["class_label"] == "ACE inhibitor"
    assert "stage 2" in result["rationale"]


def test_every_mapped_class_label_is_spelled_as_the_vocabulary_spells_it():
    """The labels are resolved against medication_classes by exact match.

    A typo here does not raise -- it silently yields no recommendation, so the
    spelling is worth a test rather than a comment.
    """
    vocabulary = {
        "blood_pressure": {
            "ACE inhibitor", "ARB", "Calcium channel blocker",
            "Thiazide diuretic", "Beta blocker",
        },
        "blood_sugar": {
            "Rapid-acting insulin", "Long-acting insulin",
            "Biguanide", "Sulfonylurea",
        },
        "heart_rate": {"Beta blocker", "Calcium channel blocker", "Antiarrhythmic"},
    }
    for condition, mapping in _CLASS_FOR_BAND.items():
        for band, label in mapping.items():
            assert label in vocabulary[condition], f"{condition}/{band}: {label}"
