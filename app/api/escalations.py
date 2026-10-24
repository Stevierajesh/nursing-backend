"""Escalation response endpoints.

Acknowledge and assess exist as separate actions on purpose. "I saw it" and
"I went and looked at the patient" are different clinical events, and
collapsing them would hide exactly the gap this product measures.
"""

from __future__ import annotations

from flask import Blueprint, g, jsonify, request
from pydantic import ValidationError

from app.auth.middleware import require_auth, require_role
from app.extensions import supabase
from app.schemas.requests import EscalationAction
from app.services import escalation as svc

bp = Blueprint("escalations", __name__, url_prefix="/api/v1/escalations")


def _body() -> tuple[EscalationAction | None, tuple | None]:
    try:
        return EscalationAction.model_validate(request.get_json(silent=True) or {}), None
    except ValidationError as exc:
        return None, (
            jsonify({"error": "Invalid request", "details": exc.errors()}),
            422,
        )


def _audit(action: str, escalation_id: str, payload: dict) -> None:
    user = g.user
    supabase().table("audit_log").insert(
        {
            "org_id": user.org_id,
            "actor_id": user.id,
            "action": action,
            "target_type": "escalation",
            "target_id": escalation_id,
            "payload": payload,
        }
    ).execute()


@bp.post("/<escalation_id>/acknowledge")
@require_auth
@require_role("charge_nurse", "admin")
def acknowledge(escalation_id: str):
    user = g.user
    row = svc.acknowledge(escalation_id, user.org_id, user.id)
    if row is None:
        return jsonify({"error": "Escalation not found"}), 404

    _audit("escalation.acknowledged", escalation_id, {"status": row["status"]})
    return jsonify({"escalation": row})


@bp.post("/<escalation_id>/assess")
@require_auth
@require_role("charge_nurse", "admin")
def assess(escalation_id: str):
    body, err = _body()
    if err:
        return err

    user = g.user
    row = svc.assess(escalation_id, user.org_id, user.id, body.note)
    if row is None:
        return jsonify({"error": "Escalation not found"}), 404

    _audit("escalation.assessed", escalation_id, {"note": body.note})
    return jsonify({"escalation": row})


@bp.post("/<escalation_id>/resolve")
@require_auth
@require_role("charge_nurse", "admin")
def resolve(escalation_id: str):
    body, err = _body()
    if err:
        return err

    user = g.user
    row = svc.resolve(escalation_id, user.org_id, user.id, body.note)
    if row is None:
        return jsonify({"error": "Escalation not found"}), 404

    _audit("escalation.resolved", escalation_id, {"note": body.note})
    return jsonify({"escalation": row})


@bp.get("/<escalation_id>")
@require_auth
def get_escalation(escalation_id: str):
    user = g.user
    resp = (
        supabase()
        .table("escalations")
        .select(
            "*, patient:patients(id, display_name, room), "
            "worry_signal:worry_signals(id, level, reasons, note, created_at)"
        )
        .eq("id", escalation_id)
        .eq("org_id", user.org_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return jsonify({"error": "Escalation not found"}), 404
    return jsonify({"escalation": rows[0]})
