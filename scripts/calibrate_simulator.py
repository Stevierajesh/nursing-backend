"""Fit reproducible empirical joint baseline distributions from a local CSV.

Usage: python scripts/calibrate_simulator.py /path/to/human_vital_signs_dataset_2024.csv
No Risk Category, patient identifiers, timestamps, or raw CSV are exported as
training features. This fits baseline combinations, not temporal physiology.
Only the Python standard library is required for fitting or runtime sampling.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
from collections import Counter

SOURCE_URL = "https://www.kaggle.com/datasets/nasirayub2/human-vital-sign-dataset"
FIELDS = {
    "heart_rate": ("Heart Rate", 40, 140),
    "resp_rate": ("Respiratory Rate", 8, 30),
    "spo2": ("Oxygen Saturation", 85, 100),
    "systolic_bp": ("Systolic Blood Pressure", 85, 200),
    "diastolic_bp": ("Diastolic Blood Pressure", 45, 120),
    "temperature_c": ("Body Temperature", 35, 39.5),
}
AGE_BANDS = ("18-39", "40-64", "65-79", "80+")


def age_band(age: float) -> str | None:
    if not math.isfinite(age) or not 18 <= age <= 120:
        return None
    return "18-39" if age < 40 else "40-64" if age < 65 else "65-79" if age < 80 else "80+"


class JointDistribution:
    def __init__(self, key: str, capacity: int, seed: int):
        self.count = 0
        self.mean = [0.0] * len(FIELDS)
        self.cross = [[0.0] * len(FIELDS) for _ in FIELDS]
        self.minimum = [math.inf] * len(FIELDS)
        self.maximum = [-math.inf] * len(FIELDS)
        self.samples = []
        self.capacity = capacity
        self.rng = random.Random(f"{seed}:{key}")

    def add(self, values):
        self.count += 1
        delta = [x - mean for x, mean in zip(values, self.mean)]
        for i, x in enumerate(values):
            self.mean[i] += delta[i] / self.count
            self.minimum[i] = min(self.minimum[i], x)
            self.maximum[i] = max(self.maximum[i], x)
        for i in range(len(values)):
            for j in range(len(values)):
                self.cross[i][j] += delta[i] * (values[j] - self.mean[j])
        # Joint reservoir: retain a complete observed tuple, never independently
        # resample HR, BP, etc. Rounded only at the simulator's recording precision.
        sample = [round(v, 1) if name == "temperature_c" else round(v)
                  for name, v in zip(FIELDS, values)]
        if len(self.samples) < self.capacity:
            self.samples.append(sample)
        else:
            index = self.rng.randrange(self.count)
            if index < self.capacity:
                self.samples[index] = sample

    def export(self):
        return {
            "count": self.count,
            "mean": [round(x, 8) for x in self.mean],
            "covariance": [[round(x / max(1, self.count - 1), 8) for x in row] for row in self.cross],
            "minimum": self.minimum, "maximum": self.maximum,
            "samples": self.samples,
        }


def fit(csv_path: Path, capacity: int = 256, seed: int = 2026) -> dict:
    if not 1 <= capacity <= 4096:
        raise ValueError("Reservoir size must be 1-4096")
    groups = {}
    overall = JointDistribution("overall", capacity, seed)
    ids = Counter()
    rejected = Counter()
    total = 0
    derived = {"pulse_pressure_checked": 0, "pulse_pressure_mismatches": 0,
               "map_checked": 0, "map_mismatches": 0}
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {spec[0] for spec in FIELDS.values()} | {"Age", "Gender", "Patient ID", "Timestamp"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Missing columns: {sorted(required - set(reader.fieldnames or []))}")
        for row in reader:
            total += 1
            ids[row["Patient ID"]] += 1
            try:
                band = age_band(float(row["Age"]))
                sex = {"male": "M", "female": "F"}.get((row.get("Gender") or "").strip().lower())
                values = [float(row[column]) for column, _, _ in FIELDS.values()]
                if band is None or sex is None:
                    rejected["unsupported_demographics"] += 1
                    continue
                if not all(math.isfinite(v) and low <= v <= high for v, (_, low, high) in zip(values, FIELDS.values())):
                    rejected["invalid_or_out_of_baseline_range"] += 1
                    continue
                if values[4] >= values[3]:
                    rejected["pressure_order"] += 1
                    continue
            except (ValueError, TypeError):
                rejected["missing_or_non_numeric"] += 1
                continue
            for column, expected, checked, mismatch in [
                ("Derived_Pulse_Pressure", values[3] - values[4], "pulse_pressure_checked", "pulse_pressure_mismatches"),
                ("Derived_MAP", values[4] + (values[3] - values[4]) / 3, "map_checked", "map_mismatches"),
            ]:
                if row.get(column):
                    derived[checked] += 1
                    try:
                        actual = float(row[column])
                        if not math.isfinite(actual) or abs(actual - expected) > 0.01:
                            derived[mismatch] += 1
                    except ValueError:
                        derived[mismatch] += 1
            key = f"{band}|{sex}"
            if key not in groups:
                groups[key] = JointDistribution(key, capacity, seed)
            groups[key].add(values)
            overall.add(values)
    if not groups:
        raise ValueError("No usable adult observations remain after validation")
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    return {
        "version": f"human-vitals-{digest[:8]}-r{capacity}-s{seed}-v1",
        "method": "stratified_joint_empirical_bootstrap",
        "source": {"url": SOURCE_URL, "license": "CC0-1.0", "filename": csv_path.name, "sha256": digest,
                   "clinical_provenance": "Clinical collection and real-patient provenance are not established by the dataset description."},
        "fit": {"reservoir_size_per_group": capacity, "seed": seed, "fields": list(FIELDS)},
        "audit": {
            "rows": total, "accepted_rows": overall.count, "rejected_rows": sum(rejected.values()),
            "rejection_reasons": dict(rejected), "unique_patient_ids": len(ids),
            "repeated_patient_ids": sum(n > 1 for n in ids.values()),
            "temporal_model_fitted": False,
            "temporal_note": "Rows with unique patient IDs do not identify within-patient changes. Scenario shifts, wobble amplitudes and time constants remain authored.",
            "excluded_features": ["Patient ID", "Timestamp", "Risk Category", "Derived_HRV", "Derived_BMI", "Weight (kg)", "Height (m)"],
            "derived_value_checks": derived,
        },
        "overall": {k: v for k, v in overall.export().items() if k != "samples"},
        "groups": {key: value.export() for key, value in sorted(groups.items())},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reservoir-size", type=int, default=256)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    model = fit(args.csv, args.reservoir_size, args.seed)
    output = args.output or Path(__file__).resolve().parents[1] / "app/data/calibration" / f"{model['version']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(model, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"artifact": str(output), "version": model["version"], "audit": model["audit"]}, indent=2))


if __name__ == "__main__":
    main()
