"""Admin routes: org settings, units, users, patients, assignments.

Everything here is admin-only except patient/assignment management, which
charge nurses also need in order to run a shift.
"""

from __future__ import annotations

from flask import Blueprint, g, jsonify, request
from pydantic import ValidationError
from postgrest.exceptions import APIError

from app.auth.middleware import require_auth, require_role
from app.extensions import profile_cache, supabase
from app.schemas.requests import (
    AssignmentCreate,
    PatientCreate,
    UnitCreate,
    UserInvite,
)

bp = Blueprint("admin", __name__, url_prefix="/api/v1/admin")


def _validate(model, payload=None):
    try:
        return model.model_validate(payload or request.get_json(silent=True) or {}), None
    except ValidationError as exc:
        return None, (
            jsonify({"error": "Invalid request", "details": exc.errors(include_context=False)}),
            422,
        )


# ---------------------------------------------------------------------------
# organization
# ---------------------------------------------------------------------------


@bp.get("/org")
@require_auth
@require_role("admin", "charge_nurse")
def get_org():
    user = g.user
    rows = (
        supabase()
        .table("organizations")
        .select("*")
        .eq("id", user.org_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        return jsonify({"error": "Organization not found"}), 404
    return jsonify({"organization": rows[0]})


@bp.patch("/org")
@require_auth
@require_role("admin")
def update_org():
    user = g.user
    payload = request.get_json(silent=True) or {}

    allowed = {"name", "worry_sla_minutes"}
    update = {k: v for k, v in payload.items() if k in allowed}
    if not update:
        return jsonify({"error": f"No updatable fields. Allowed: {sorted(allowed)}"}), 422

    if "worry_sla_minutes" in update:
        try:
            minutes = int(update["worry_sla_minutes"])
        except (TypeError, ValueError):
            return jsonify({"error": "worry_sla_minutes must be an integer"}), 422
        if not 1 <= minutes <= 240:
            return jsonify({"error": "worry_sla_minutes must be 1-240"}), 422
        update["worry_sla_minutes"] = minutes

    rows = (
        supabase()
        .table("organizations")
        .update(update)
        .eq("id", user.org_id)
        .execute()
    ).data or []
    return jsonify({"organization": rows[0] if rows else None})


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------


@bp.get("/units")
@require_auth
def list_units():
    user = g.user
    rows = (
        supabase()
        .table("units")
        .select("*")
        .eq("org_id", user.org_id)
        .order("name")
        .execute()
    ).data or []
    return jsonify({"units": rows})


@bp.post("/units")
@require_auth
@require_role("admin")
def create_unit():
    body, err = _validate(UnitCreate)
    if err:
        return err
    user = g.user
    rows = (
        supabase()
        .table("units")
        .insert({"org_id": user.org_id, "name": body.name})
        .execute()
    ).data or []
    return jsonify({"unit": rows[0] if rows else None}), 201


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


@bp.get("/users")
@require_auth
@require_role("admin", "charge_nurse")
def list_users():
    user = g.user
    rows = (
        supabase()
        .table("profiles")
        .select("*")
        .eq("org_id", user.org_id)
        .order("full_name")
        .execute()
    ).data or []
    return jsonify({"users": rows})


@bp.post("/users")
@require_auth
@require_role("admin")
def invite_user():
    """Create an auth user and its profile in one call.

    Two writes that must both land: if the profile insert fails we delete the
    auth user, because an auth account with no profile is a user who can log
    in and then hit 403 on every route with no way to fix it themselves.
    """
    body, err = _validate(UserInvite)
    if err:
        return err
    user = g.user
    client = supabase()

    try:
        created = client.auth.admin.create_user(
            {
                "email": body.email,
                "password": body.password,
                "email_confirm": True,
                "user_metadata": {"full_name": body.full_name},
            }
        )
    except Exception as exc:  # supabase-py raises bare AuthApiError
        return jsonify({"error": f"Could not create auth user: {exc}"}), 400

    auth_user = getattr(created, "user", None)
    if auth_user is None:
        return jsonify({"error": "Auth user creation returned no user"}), 500

    try:
        rows = (
            client.table("profiles")
            .insert(
                {
                    "id": auth_user.id,
                    "org_id": user.org_id,
                    "unit_id": body.unit_id,
                    "role": body.role,
                    "full_name": body.full_name,
                    "email": body.email,
                }
            )
            .execute()
        ).data or []
    except Exception as exc:
        client.auth.admin.delete_user(auth_user.id)
        return jsonify({"error": f"Could not create profile, rolled back: {exc}"}), 400

    return jsonify({"user": rows[0] if rows else None}), 201


@bp.patch("/users/<user_id>")
@require_auth
@require_role("admin")
def update_user(user_id: str):
    user = g.user
    payload = request.get_json(silent=True) or {}
    allowed = {"role", "unit_id", "full_name"}
    update = {k: v for k, v in payload.items() if k in allowed}
    if not update:
        return jsonify({"error": f"No updatable fields. Allowed: {sorted(allowed)}"}), 422

    if update.get("role") not in (None, "admin", "charge_nurse", "nurse", "prescriber"):
        return jsonify({"error": "role must be admin, charge_nurse, nurse, or prescriber"}), 422

    rows = (
        supabase()
        .table("profiles")
        .update(update)
        .eq("id", user_id)
        .eq("org_id", user.org_id)
        .execute()
    ).data or []

    # The middleware caches resolved profiles; a role change must take effect
    # immediately rather than after the TTL.
    profile_cache.invalidate(user_id)
    return jsonify({"user": rows[0] if rows else None})


# ---------------------------------------------------------------------------
# patients + assignments
# ---------------------------------------------------------------------------


@bp.post("/patients")
@require_auth
@require_role("admin", "charge_nurse")
def create_patient():
    body, err = _validate(PatientCreate)
    if err:
        return err
    user = g.user
    if body.simulation is not None:
        try:
            data = supabase().rpc("create_simulation_patient", {
                "p_org": user.org_id, "p_actor": user.id,
                "p_patient": body.model_dump(exclude={"simulation", "nurse_id"}, exclude_none=True),
                "p_config": body.simulation.model_dump(), "p_nurse": body.nurse_id,
            }).execute().data
        except APIError as exc:
            from app.api.simulation import database_error
            return database_error(exc)
        return jsonify(data), 201
    if body.nurse_id is not None:
        return jsonify(error="An initial nurse assignment currently requires a simulation profile"), 422
    rows = (
        supabase()
        .table("patients")
        .insert({**body.model_dump(exclude={"simulation", "nurse_id"}, exclude_none=True), "org_id": user.org_id})
        .execute()
    ).data or []
    return jsonify({"patient": rows[0] if rows else None}), 201


@bp.get("/assignments")
@require_auth
@require_role("admin", "charge_nurse")
def list_assignments():
    user = g.user
    rows = (
        supabase()
        .table("assignments")
        .select(
            "*, nurse:profiles!assignments_nurse_id_fkey(id, full_name), "
            "patient:patients(id, display_name, room)"
        )
        .eq("org_id", user.org_id)
        .is_("shift_end", "null")
        .execute()
    ).data or []
    return jsonify({"assignments": rows})


@bp.post("/assignments")
@require_auth
@require_role("admin", "charge_nurse")
def create_assignment():
    body, err = _validate(AssignmentCreate)
    if err:
        return err
    user = g.user

    # Both sides must belong to the caller's org, or an admin could assign a
    # nurse to someone else's patient by guessing a UUID.
    nurse = (
        supabase()
        .table("profiles")
        .select("id")
        .eq("id", body.nurse_id)
        .eq("org_id", user.org_id)
        .limit(1)
        .execute()
    ).data or []
    patient = (
        supabase()
        .table("patients")
        .select("id")
        .eq("id", body.patient_id)
        .eq("org_id", user.org_id)
        .limit(1)
        .execute()
    ).data or []
    if not nurse or not patient:
        return jsonify({"error": "Nurse or patient not found in this organization"}), 404

    rows = (
        supabase()
        .table("assignments")
        .insert(
            {
                "org_id": user.org_id,
                "nurse_id": body.nurse_id,
                "patient_id": body.patient_id,
            }
        )
        .execute()
    ).data or []
    return jsonify({"assignment": rows[0] if rows else None}), 201


@bp.delete("/assignments/<assignment_id>")
@require_auth
@require_role("admin", "charge_nurse")
def end_assignment(assignment_id: str):
    """End a shift assignment. Soft -- the row is history, not garbage."""
    from datetime import datetime, timezone

    user = g.user
    rows = (
        supabase()
        .table("assignments")
        .update({"shift_end": datetime.now(timezone.utc).isoformat()})
        .eq("id", assignment_id)
        .eq("org_id", user.org_id)
        .execute()
    ).data or []
    return jsonify({"assignment": rows[0] if rows else None})


@bp.get("/audit")
@require_auth
@require_role("admin")
def audit_log():
    user = g.user
    rows = (
        supabase()
        .table("audit_log")
        .select("*, actor:profiles(id, full_name)")
        .eq("org_id", user.org_id)
        .order("created_at", desc=True)
        .limit(200)
        .execute()
    ).data or []
    return jsonify({"entries": rows})
