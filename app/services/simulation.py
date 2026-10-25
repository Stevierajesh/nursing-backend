"""Real-time, reproducible demo observations. No inferred or generated worry."""
from __future__ import annotations

import logging
import math
import time
from postgrest.exceptions import APIError

from app.extensions import supabase
from app.schemas.simulation import SimulationConfig
from app.services.news import news_score

log = logging.getLogger(__name__)
SAMPLE_SECONDS = 15
_last_missing_migration_warning = float("-inf")
SCENARIOS = [
    {"id": "stable", "label": "Stable", "description": "Small fluctuations around the baseline."},
    {"id": "respiratory_decline", "label": "Respiratory decline", "description": "Breathing rate rises as oxygen saturation falls."},
    {"id": "circulatory_decline", "label": "Circulatory decline", "description": "Blood pressure falls as heart rate rises."},
    {"id": "recovery", "label": "Recovery", "description": "An initially abnormal observation set improves toward the baseline."},
]


def observation(config: SimulationConfig, elapsed_seconds: float) -> dict:
    """A smooth trajectory with bounded correlated variation; time is real time.

    Baseline is the starting point except in recovery, where it is the target.
    A baseline can be sampled from a versioned empirical calibration model and
    saved in config. The temporal wobble and shifts below remain authored:
    the calibration source has no repeated patient observations to fit them.
    These are authored scenarios, not disease or treatment-effect models.

    Two clocks, deliberately. `arc` saturates at duration_minutes and drives the
    trajectory, so once a scenario has played out the patient *holds* at its end
    state rather than resetting or looping. `elapsed_seconds` runs unbounded and
    drives the variation, so a held patient keeps producing moving observations
    instead of one frozen reading forever. Clamping both -- which is what this
    did before -- is why finished runs went silent.
    """
    arc = max(0.0, min(elapsed_seconds, config.duration_minutes * 60))
    onset = config.onset_minutes * 60
    progress = max(0.0, min(1.0, (arc - onset) / (config.duration_minutes * 60 - onset)))
    progress = progress * progress * (3 - 2 * progress)
    # Smooth variation is deterministic across workers and process restarts.
    elapsed = max(0.0, elapsed_seconds)
    phase = config.seed * 0.173
    variation = math.sin(elapsed / 43 + phase) * 0.65 + math.sin(elapsed / 97 + phase) * 0.35
    severity = (1 - progress) if config.scenario == "recovery" else progress
    values = config.baseline.model_dump()
    shifts = {
        "stable": {},
        "respiratory_decline": {"heart_rate": 30, "resp_rate": 14, "spo2": -10, "temperature_c": 0.4},
        "circulatory_decline": {"heart_rate": 40, "resp_rate": 8, "systolic_bp": -44, "diastolic_bp": -22},
        "recovery": {"heart_rate": 30, "resp_rate": 12, "spo2": -8, "systolic_bp": -25},
    }[config.scenario]
    bounds = {"heart_rate": (35, 190), "resp_rate": (8, 45), "spo2": (75, 100),
              "systolic_bp": (60, 220), "diastolic_bp": (35, 130), "temperature_c": (35, 40)}
    noise = {"heart_rate": 2, "resp_rate": 0.6, "spo2": 0.3,
             "systolic_bp": 2, "diastolic_bp": 1, "temperature_c": 0.08}
    for key, value in values.items():
        low, high = bounds[key]
        value = max(low, min(high, value + shifts.get(key, 0) * severity + noise[key] * variation))
        values[key] = round(value, 1) if key == "temperature_c" else round(value)
    values["diastolic_bp"] = min(values["diastolic_bp"], values["systolic_bp"] - 10)
    values.update(supplemental_o2=False, consciousness="A")
    values["news_score"] = news_score(values)
    return values


def tick_patient(org_id: str, patient_id: str) -> bool:
    """Read a DB-clock snapshot, then commit only if its version is still current.

    The SQL row lock serializes writes with pause/restart and duplicate workers.
    Risk evaluation remains in the existing periodic job, so a transient scoring
    failure never causes observations to be inserted twice or stops the stream.
    """
    client = supabase()
    snapshot = client.rpc("simulation_snapshot", {"p_org": org_id, "p_patient": patient_id}).execute().data
    if not snapshot:
        return False
    values = observation(SimulationConfig.model_validate(snapshot["config"]), snapshot["elapsed_seconds"])
    result = client.rpc("simulation_append", {
        "p_org": org_id, "p_run": snapshot["id"], "p_revision": snapshot["revision"],
        "p_elapsed": snapshot["elapsed_seconds"], "p_values": values,
    }).execute().data
    if result:
        try:
            from app.services.escalation import evaluate_patient
            evaluate_patient(org_id, patient_id)
        except Exception:
            log.exception("Observation saved; risk recompute pending for %s", patient_id)
    return bool(result)


def simulation_tick() -> None:
    """Isolate failures per patient; completed/discharged patients stop emitting."""
    try:
        runs = supabase().table("simulation_runs").select("org_id, patient_id").eq("status", "running").execute().data or []
        for run in runs:
            try:
                tick_patient(run["org_id"], run["patient_id"])
            except Exception:
                log.exception("Simulation tick failed for patient %s", run["patient_id"])
    except APIError as exc:
        if exc.code in ("42P01", "PGRST205"):
            global _last_missing_migration_warning
            if time.monotonic() - _last_missing_migration_warning >= 60:
                log.warning("Patient simulations are unavailable until migration 20261024001100 is applied")
                _last_missing_migration_warning = time.monotonic()
        else:
            log.exception("Simulation scheduler could not load running patients")
    except Exception:
        log.exception("Simulation scheduler could not load running patients")
