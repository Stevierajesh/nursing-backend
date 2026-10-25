"""The worry endpoint. One tap, two seconds.

This is the whole product in one route, so it is worth being explicit about
what it does and does not do:

  * It records the nurse's signal immutably, with their stated reason.
  * It recomputes fused risk synchronously, so the nurse gets feedback right
    away rather than wondering whether the tap registered.
  * It escalates only when the fusion rules fire. A worried nurse with a
    stable patient gets an honest "flagged, not escalated" response.
"""

from __future__ import annotations

from flask import Blueprint, g, jsonify, request
from pydantic import ValidationError

from app.auth.middleware import (
    assert_nurse_assigned,
    assert_patient_in_org,
    require_auth,
    require_role,
)
from app.auth.jwt import AuthError
from app.extensions import supabase
from app.schemas.requests import WorryCreate
from app.services.escalation import evaluate_patient
from app.services.risk import RULE_LABELS

bp = Blueprint("worry", __name__, url_prefix="/api/v1/worry")


@bp.post("")
@require_auth
@require_role("nurse", "charge_nurse", "admin")
def create_worry():
    try:
        body = WorryCreate.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return jsonify({"error": "Invalid request", "details": exc.errors(include_context=False)}), 422

    user = g.user

    try:
        assert_patient_in_org(body.patient_id)
        assert_nurse_assigned(body.patient_id)
    except AuthError as exc:
        return jsonify({"error": exc.message}), exc.status

    inserted = (
        supabase()
        .table("worry_signals")
        .insert(
            {
                "org_id": user.org_id,
                "patient_id": body.patient_id,
                "nurse_id": user.id,
                "level": body.level,
                "reasons": body.reasons,
                "note": body.note,
                "voice_url": body.voice_url,
                "capture_ms": body.capture_ms,
            }
        )
        .execute()
    )

    rows = inserted.data or []
    if not rows:
        return jsonify({"error": "Failed to record worry signal"}), 500
    signal = rows[0]

    # Fuse immediately so the response can tell the nurse what happened.
    result, escalation = evaluate_patient(
        org_id=user.org_id,
        patient_id=body.patient_id,
        worry_signal_id=signal["id"],
    )

    supabase().table("audit_log").insert(
        {
            "org_id": user.org_id,
            "actor_id": user.id,
            "action": "worry.created",
            "target_type": "patient",
            "target_id": body.patient_id,
            "payload": {
                "level": body.level,
                "reasons": body.reasons,
                "escalated": escalation is not None,
                "rule": result.rule,
            },
        }
    ).execute()

    return (
        jsonify(
            {
                "worry_signal": signal,
                "risk": {
                    "score": result.score,
                    "worry_component": result.worry_component,
                    "vitals_component": result.vitals_component,
                    "trend_component": result.trend_component,
                    "latest_news": result.latest_news,
                    "news_band": result.news_band,
                    "trend_slope_per_hour": result.trend_slope,
                },
                "escalated": escalation is not None,
                "escalation": escalation,
                # The nurse-facing explanation. When nothing escalated we say
                # so plainly -- silently swallowing the signal is how trust in
                # these systems dies.
                "message": (
                    f"Escalated: {RULE_LABELS.get(result.rule, result.rule)}. "
                    "The charge nurse has been notified."
                    if escalation
                    else "Recorded and flagged on the charge nurse's board. "
                    "Vitals are currently stable, so no page was sent."
                ),
            }
        ),
        201,
    )


@bp.get("/patient/<patient_id>")
@require_auth
def worry_history(patient_id: str):
    """Worry timeline for one patient, newest first."""
    try:
        assert_patient_in_org(patient_id)
    except AuthError as exc:
        return jsonify({"error": exc.message}), exc.status

    user = g.user
    query = (
        supabase()
        .table("worry_signals")
        .select("*, nurse:profiles!worry_signals_nurse_id_fkey(id, full_name, role)")
        .eq("patient_id", patient_id)
        .eq("org_id", user.org_id)
    )
    # A nurse sees their own signals; charge nurses and admins see everyone's.
    if not user.is_org_wide:
        query = query.eq("nurse_id", user.id)

    resp = query.order("created_at", desc=True).limit(100).execute()
    return jsonify({"worry_signals": resp.data or []})
