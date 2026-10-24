"""Charge nurse dashboard: the ranked board."""

from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from app.auth.middleware import require_auth, require_role
from app.extensions import supabase
from app.services.risk import RULE_LABELS

from app.api.patients import _decorate

bp = Blueprint("dashboard", __name__, url_prefix="/api/v1/dashboard")


@bp.get("/patients")
@require_auth
@require_role("charge_nurse", "admin")
def ranked_patients():
    """Every active patient in the org, ranked by fused risk.

    Ordering is deliberately not pure risk score. An open escalation outranks
    everything, because the board's job is to make un-responded worry
    impossible to scroll past.
    """
    user = g.user
    unit_id = request.args.get("unit_id")

    query = (
        supabase()
        .table("patients")
        .select("*")
        .eq("org_id", user.org_id)
        .is_("discharged_at", "null")
    )
    if unit_id:
        query = query.eq("unit_id", unit_id)

    patients = (query.execute().data) or []
    decorated = _decorate(patients)

    def sort_key(p: dict):
        esc = p.get("open_escalation")
        # auto_escalated first (nobody responded), then open, then acknowledged
        status_rank = {
            "auto_escalated": 0,
            "open": 1,
            "acknowledged": 2,
        }.get(esc["status"], 3) if esc else 4
        worry = p.get("latest_worry")
        worry_level = int(worry["level"]) if worry else 0
        return (status_rank, -worry_level, -float(p.get("risk_score") or 0))

    decorated.sort(key=sort_key)
    return jsonify({"patients": decorated})


@bp.get("/escalations")
@require_auth
@require_role("charge_nurse", "admin")
def escalation_queue():
    """Live escalation queue with the nurse's own stated reason attached.

    Carrying the worry reason through to the responder is the point: "Bed 12,
    NEWS 4 but Maria says his breathing is off" is actionable in a way that a
    bare score is not.
    """
    user = g.user
    status = request.args.getlist("status") or [
        "open",
        "acknowledged",
        "auto_escalated",
    ]

    resp = (
        supabase()
        .table("escalations")
        .select(
            "*, "
            "patient:patients(id, display_name, room, unit_id), "
            "worry_signal:worry_signals("
            "  id, level, reasons, note, created_at, "
            "  nurse:profiles!worry_signals_nurse_id_fkey(id, full_name)"
            "), "
            "ack_by:profiles!escalations_acknowledged_by_fkey(id, full_name), "
            "assess_by:profiles!escalations_assessed_by_fkey(id, full_name)"
        )
        .eq("org_id", user.org_id)
        .in_("status", status)
        .order("triggered_at", desc=True)
        .limit(200)
        .execute()
    )

    rows = [
        {**r, "rule_label": RULE_LABELS.get(r["rule"], r["rule"])}
        for r in (resp.data or [])
    ]
    return jsonify({"escalations": rows})


@bp.get("/summary")
@require_auth
@require_role("charge_nurse", "admin")
def summary():
    """Header counters for the board."""
    user = g.user

    active = (
        supabase()
        .table("patients")
        .select("id", count="exact")
        .eq("org_id", user.org_id)
        .is_("discharged_at", "null")
        .execute()
    )
    open_esc = (
        supabase()
        .table("escalations")
        .select("id", count="exact")
        .eq("org_id", user.org_id)
        .in_("status", ["open", "auto_escalated"])
        .execute()
    )
    breached = (
        supabase()
        .table("escalations")
        .select("id", count="exact")
        .eq("org_id", user.org_id)
        .eq("status", "auto_escalated")
        .execute()
    )

    return jsonify(
        {
            "active_patients": active.count or 0,
            "open_escalations": open_esc.count or 0,
            "sla_breaches": breached.count or 0,
        }
    )
