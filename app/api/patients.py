"""Patient endpoints: the nurse's list, the charge nurse's board, and detail."""

from __future__ import annotations

from flask import Blueprint, g, jsonify, request
from pydantic import ValidationError

from app.auth.jwt import AuthError
from app.auth.middleware import (
    assert_patient_in_org,
    assert_nurse_assigned,
    require_auth,
    require_role,
)
from app.extensions import supabase
from app.schemas.requests import VitalsCreate
from app.services.escalation import evaluate_patient, recent_vitals, recent_worry
from app.services.news import news_band, news_score
from app.services.risk import RULE_LABELS, compute_risk

bp = Blueprint("patients", __name__, url_prefix="/api/v1/patients")


def _latest_risk_map(patient_ids: list[str]) -> dict[str, dict]:
    """Most recent risk score per patient.

    Supabase/PostgREST has no DISTINCT ON, so we over-fetch recent rows and
    keep the first occurrence per patient. Bounded by the patient count, which
    on a hospital unit is tens, not thousands.
    """
    if not patient_ids:
        return {}

    resp = (
        supabase()
        .table("risk_scores")
        .select("*")
        .in_("patient_id", patient_ids)
        .order("computed_at", desc=True)
        .limit(max(len(patient_ids) * 5, 50))
        .execute()
    )

    latest: dict[str, dict] = {}
    for row in resp.data or []:
        latest.setdefault(row["patient_id"], row)
    return latest


def _open_escalation_map(patient_ids: list[str]) -> dict[str, dict]:
    if not patient_ids:
        return {}
    resp = (
        supabase()
        .table("escalations")
        .select("*")
        .in_("patient_id", patient_ids)
        .in_("status", ["open", "acknowledged", "auto_escalated"])
        .order("triggered_at", desc=True)
        .execute()
    )
    out: dict[str, dict] = {}
    for row in resp.data or []:
        out.setdefault(row["patient_id"], row)
    return out


def _latest_worry_map(patient_ids: list[str]) -> dict[str, dict]:
    if not patient_ids:
        return {}
    resp = (
        supabase()
        .table("worry_signals")
        .select("*, nurse:profiles!worry_signals_nurse_id_fkey(id, full_name)")
        .in_("patient_id", patient_ids)
        .order("created_at", desc=True)
        .limit(max(len(patient_ids) * 5, 50))
        .execute()
    )
    out: dict[str, dict] = {}
    for row in resp.data or []:
        out.setdefault(row["patient_id"], row)
    return out


def _vitals_series_map(patient_ids: list[str]) -> dict[str, list[dict]]:
    """Recent vitals per patient, newest first.

    Returns the whole series rather than only the latest row, because the board
    needs the trend slope to compute risk live for any patient the background
    job has not scored yet. One query either way.
    """
    if not patient_ids:
        return {}
    resp = (
        supabase()
        .table("vitals")
        .select("*")
        .in_("patient_id", patient_ids)
        .order("recorded_at", desc=True)
        .limit(max(len(patient_ids) * 25, 200))
        .execute()
    )
    out: dict[str, list[dict]] = {}
    for row in resp.data or []:
        out.setdefault(row["patient_id"], []).append(row)
    return out


def _worry_series_map(patient_ids: list[str]) -> dict[str, list[dict]]:
    """Recent worry signals per patient, newest first, for live risk fallback."""
    if not patient_ids:
        return {}
    resp = (
        supabase()
        .table("worry_signals")
        .select("*")
        .in_("patient_id", patient_ids)
        .order("created_at", desc=True)
        .limit(max(len(patient_ids) * 10, 100))
        .execute()
    )
    out: dict[str, list[dict]] = {}
    for row in resp.data or []:
        out.setdefault(row["patient_id"], []).append(row)
    return out


def _decorate(patients: list[dict]) -> list[dict]:
    """Attach risk, open escalation, latest worry and latest vitals.

    Risk comes from the most recent persisted `risk_scores` row when one
    exists. When it does not -- a patient admitted since the last background
    recompute -- it is computed live rather than defaulting to zero.

    That fallback matters: defaulting to 0 renders an unscored, deteriorating
    patient as the safest person on the board. Observed during verification,
    where the seeded hero case displayed risk 0 against a NEWS slope of
    +0.8/hr purely because the recompute had not reached him yet.
    """
    ids = [p["id"] for p in patients]
    risks = _latest_risk_map(ids)
    escalations = _open_escalation_map(ids)
    worries = _latest_worry_map(ids)
    vitals_series = _vitals_series_map(ids)
    worry_series = _worry_series_map(ids)

    out = []
    for p in patients:
        pid = p["id"]
        risk = risks.get(pid)
        esc = escalations.get(pid)
        series = vitals_series.get(pid, [])
        latest_vitals = series[0] if series else None

        if risk is not None:
            score = float(risk["score"])
            components = {
                "worry": float(risk["worry_component"]),
                "vitals": float(risk["vitals_component"]),
                "trend": float(risk["trend_component"]),
            }
            inputs = risk.get("inputs")
        else:
            live = compute_risk(series, worry_series.get(pid, []))
            score = live.score
            components = {
                "worry": live.worry_component,
                "vitals": live.vitals_component,
                "trend": live.trend_component,
            }
            inputs = {**live.inputs, "computed": "live"}

        out.append(
            {
                **p,
                "risk_score": score,
                "risk_components": components,
                "risk_inputs": inputs,
                "latest_vitals": latest_vitals,
                "latest_news": (
                    latest_vitals.get("news_score") if latest_vitals else None
                ),
                "news_band": (
                    news_band(int(latest_vitals.get("news_score") or 0))
                    if latest_vitals
                    else "none"
                ),
                "latest_worry": worries.get(pid),
                "open_escalation": esc,
                "escalation_rule_label": (
                    RULE_LABELS.get(esc["rule"], esc["rule"]) if esc else None
                ),
            }
        )
    return out


@bp.get("/mine")
@require_auth
def my_patients():
    """Patients the caller is currently on shift for.

    Charge nurses and admins have no personal assignment list, so they get the
    unit/org view here too -- it keeps the mobile app usable for them without a
    separate route.
    """
    user = g.user

    if user.is_org_wide:
        query = (
            supabase()
            .table("patients")
            .select("*")
            .eq("org_id", user.org_id)
            .is_("discharged_at", "null")
        )
        if user.unit_id:
            query = query.eq("unit_id", user.unit_id)
        patients = (query.order("display_name").execute().data) or []
    else:
        assigned = (
            supabase()
            .table("assignments")
            .select("patient_id")
            .eq("nurse_id", user.id)
            .eq("org_id", user.org_id)
            .is_("shift_end", "null")
            .execute()
        )
        patient_ids = [r["patient_id"] for r in (assigned.data or [])]
        if not patient_ids:
            return jsonify({"patients": []})
        patients = (
            supabase()
            .table("patients")
            .select("*")
            .in_("id", patient_ids)
            .eq("org_id", user.org_id)
            .is_("discharged_at", "null")
            .order("display_name")
            .execute()
            .data
        ) or []

    return jsonify({"patients": _decorate(patients)})


@bp.get("/<patient_id>")
@require_auth
def patient_detail(patient_id: str):
    """Full detail: vitals series, worry timeline, escalation history."""
    try:
        patient = assert_patient_in_org(patient_id)
        assert_nurse_assigned(patient_id)
    except AuthError as exc:
        return jsonify({"error": exc.message}), exc.status

    user = g.user
    vitals = recent_vitals(patient_id, limit=120)
    worry = recent_worry(patient_id, limit=50)

    if not user.is_org_wide:
        worry = [w for w in worry if w["nurse_id"] == user.id]

    escalations = (
        supabase()
        .table("escalations")
        .select("*")
        .eq("patient_id", patient_id)
        .eq("org_id", user.org_id)
        .order("triggered_at", desc=True)
        .limit(50)
        .execute()
        .data
    ) or []

    # Computed live rather than read from risk_scores so the detail view always
    # reflects the newest vitals, even between background recomputes.
    result = compute_risk(vitals, worry)

    return jsonify(
        {
            "patient": patient,
            # oldest-first for charting
            "vitals": list(reversed(vitals)),
            "worry_signals": worry,
            "escalations": [
                {**e, "rule_label": RULE_LABELS.get(e["rule"], e["rule"])}
                for e in escalations
            ],
            "risk": {
                "score": result.score,
                "worry_component": result.worry_component,
                "vitals_component": result.vitals_component,
                "trend_component": result.trend_component,
                "latest_news": result.latest_news,
                "news_band": result.news_band,
                "trend_slope_per_hour": result.trend_slope,
                "worry_level": result.worry_level,
                "inputs": result.inputs,
            },
        }
    )


@bp.post("/<patient_id>/vitals")
@require_auth
@require_role("nurse", "charge_nurse", "admin")
def add_vitals(patient_id: str):
    """Chart an observation set.

    NEWS is computed here on write so the dashboard can sort and trend without
    recomputing across the series on every read.
    """
    try:
        assert_patient_in_org(patient_id)
        assert_nurse_assigned(patient_id)
    except AuthError as exc:
        return jsonify({"error": exc.message}), exc.status

    payload = request.get_json(silent=True) or {}
    payload["patient_id"] = patient_id
    try:
        body = VitalsCreate.model_validate(payload)
    except ValidationError as exc:
        return jsonify({"error": "Invalid request", "details": exc.errors(include_context=False)}), 422

    user = g.user
    row = body.model_dump(exclude_none=True)
    row["org_id"] = user.org_id
    row["news_score"] = news_score(row)

    inserted = supabase().table("vitals").insert(row).execute()
    rows = inserted.data or []
    if not rows:
        return jsonify({"error": "Failed to record vitals"}), 500

    # New vitals can push a previously-flagged worry over the pairing
    # threshold, so re-evaluate. This is the path where a patient who was
    # "flagged, not escalated" becomes an actual page.
    result, escalation = evaluate_patient(user.org_id, patient_id)

    return (
        jsonify(
            {
                "vitals": rows[0],
                "news_score": row["news_score"],
                "news_band": news_band(row["news_score"]),
                "escalated": escalation is not None,
                "escalation": escalation,
                "risk_score": result.score,
            }
        ),
        201,
    )
