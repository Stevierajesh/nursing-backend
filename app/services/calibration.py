"""Versioned empirical baseline model. No runtime CSV, ML library, or network."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path

CURRENT_MODEL = "human-vitals-d70375bc-r256-s2026-v1"
MODEL_FILES = {CURRENT_MODEL: Path(__file__).resolve().parents[1] / "data/calibration" / f"{CURRENT_MODEL}.json"}
AGE_BANDS = ("all", "18-39", "40-64", "65-79", "80+")
SEX_GROUPS = ("all", "F", "M")


@lru_cache(maxsize=8)
def load_model(version: str = CURRENT_MODEL) -> dict:
    if version not in MODEL_FILES:
        raise ValueError("Unknown baseline calibration model version")
    model = json.loads(MODEL_FILES[version].read_text())
    if model["version"] != version:
        raise ValueError("Calibration artifact version does not match its registry entry")
    return model


def model_metadata() -> dict:
    model = load_model()
    return {
        "version": model["version"], "method": model["method"], "source": model["source"],
        "audit": model["audit"], "age_bands": list(AGE_BANDS), "sex_groups": list(SEX_GROUPS),
        "note": "Fits baseline combinations from the source dataset. Clinical provenance is unverified; temporal variation and deterioration remain authored.",
    }


def sample_baseline(seed: int, age_band: str = "all", sex: str = "all", version: str = CURRENT_MODEL) -> dict:
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("Seed must be an integer from 0 to 2147483647")
    if age_band not in AGE_BANDS or sex not in SEX_GROUPS:
        raise ValueError("Unsupported calibration age band or sex group")
    model = load_model(version)
    groups = [(key, group) for key, group in sorted(model["groups"].items())
              if (age_band == "all" or key.split("|")[0] == age_band)
              and (sex == "all" or key.split("|")[1] == sex)]
    if not groups:
        raise ValueError("No baseline observations are available for that group")
    # Hash-based deterministic selection is stable across worker restarts and
    # Python versions. Group weights reflect all accepted rows, not reservoir size.
    digest = hashlib.sha256(f"{version}:{seed}:{age_band}:{sex}".encode()).digest()
    target = int.from_bytes(digest[:16], "big") % sum(group["count"] for _, group in groups)
    for key, group in groups:
        if target < group["count"]:
            break
        target -= group["count"]
    values = group["samples"][int.from_bytes(digest[16:], "big") % len(group["samples"])]
    baseline = dict(zip(model["fit"]["fields"], values))
    pp = baseline["systolic_bp"] - baseline["diastolic_bp"]
    return {
        "baseline": baseline,
        "calibration": {"model_version": version, "age_band": age_band, "sex": sex, "selected_group": key},
        "derived": {"pulse_pressure": pp, "mean_arterial_pressure": round(baseline["diastolic_bp"] + pp / 3, 1)},
    }
