"""Organization-scoped demo configuration and playback controls."""
from flask import Blueprint, g, jsonify, request
from postgrest.exceptions import APIError
from pydantic import ValidationError

from app.auth.middleware import assert_patient_in_org, require_auth, require_role
from app.extensions import supabase
from app.schemas.simulation import SimulationAction, SimulationConfig
from app.services.simulation import SCENARIOS, observation, tick_patient

bp = Blueprint("simulation", __name__, url_prefix="/api/v1/simulations")


def database_error(exc):
    if exc.code in ("42P01", "PGRST202", "PGRST205", "42703"):
        return jsonify(error="Patient simulations require database migration 20261024001100_patient_simulation.sql"), 503
    if exc.code == "23505":
        return jsonify(error="This patient identifier, bed, or active simulation already exists"), 409
    if exc.code in ("22023", "22P02", "23503"):
        return jsonify(error=exc.message), 422
    if exc.code == "P0002":
        return jsonify(error="Patient not found"), 404
    if exc.code == "42501":
        return jsonify(error="Insufficient permission"), 403
    raise exc


def validate(model):
    try:
        return model.model_validate(request.get_json(silent=True) or {}), None
    except ValidationError as exc:
        return None, (jsonify(error="Invalid simulation configuration", details=exc.errors(include_context=False)), 422)


@bp.get("")
@require_auth
@require_role("admin", "charge_nurse")
def list_simulations():
    try:
        rows = supabase().table("simulation_runs").select("*").eq("org_id", g.user.org_id).order("created_at", desc=True).execute().data or []
        latest = {}
        for row in rows:
            latest.setdefault(row["patient_id"], row)
        return jsonify(simulations=list(latest.values()), scenarios=SCENARIOS)
    except APIError as exc:
        return database_error(exc)


@bp.post("/preview")
@require_auth
@require_role("admin", "charge_nurse")
def preview():
    config, err = validate(SimulationConfig)
    if err:
        return err
    duration = config.duration_minutes * 60
    return jsonify(observations=[{"elapsed_seconds": duration * i / 8, **observation(config, duration * i / 8)} for i in range(9)])


@bp.post("/<patient_id>/configure")
@require_auth
@require_role("admin", "charge_nurse")
def configure(patient_id):
    config, err = validate(SimulationConfig)
    if err:
        return err
    assert_patient_in_org(patient_id)
    try:
        run = supabase().rpc("simulation_control", {
            "p_org": g.user.org_id, "p_patient": patient_id, "p_actor": g.user.id,
            "p_action": "configure", "p_config": config.model_dump(),
        }).execute().data
        return jsonify(simulation=run), 201
    except APIError as exc:
        return database_error(exc)


@bp.post("/<patient_id>/control")
@require_auth
@require_role("admin", "charge_nurse")
def control(patient_id):
    body, err = validate(SimulationAction)
    if err:
        return err
    assert_patient_in_org(patient_id)
    try:
        run = supabase().rpc("simulation_control", {
            "p_org": g.user.org_id, "p_patient": patient_id, "p_actor": g.user.id,
            "p_action": body.action,
        }).execute().data
    except APIError as exc:
        return database_error(exc)
    # Emit the first observation immediately; periodic worker continues it.
    emitted = False
    warning = None
    if body.action in ("start", "resume", "restart"):
        try:
            emitted = tick_patient(g.user.org_id, patient_id)
        except Exception:
            from flask import current_app
            current_app.logger.exception("Initial simulation observation failed")
            warning = "Run started; first observation is pending a scheduler retry."
    return jsonify(simulation=run, emitted=emitted, warning=warning)
