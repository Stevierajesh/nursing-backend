"""Escalation lifecycle and the SLA sweeper.

An escalation moves: open -> acknowledged -> assessed -> resolved.

Every transition timestamp is written server-side. That is not incidental:
worry-to-response time is the metric this product is selling, and a
client-supplied `acknowledged_at` would make it unfalsifiable.

If nobody responds before `sla_deadline`, the sweeper flips the escalation to
`auto_escalated`. That is the closed loop -- a worried nurse cannot be
silently ignored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.extensions import supabase
from app.services.risk import RULE_LABELS, compute_risk

DEFAULT_SLA_MINUTES = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def org_sla_minutes(org_id: str) -> int:
    """SLA window for an org, falling back to the default."""
    resp = (
        supabase()
        .table("organizations")
        .select("worry_sla_minutes")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return DEFAULT_SLA_MINUTES
    return int(rows[0].get("worry_sla_minutes") or DEFAULT_SLA_MINUTES)


def recent_vitals(patient_id: str, limit: int = 40) -> list[dict[str, Any]]:
    resp = (
        supabase()
        .table("vitals")
        .select("*")
        .eq("patient_id", patient_id)
        .order("recorded_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


def recent_worry(patient_id: str, limit: int = 20) -> list[dict[str, Any]]:
    resp = (
        supabase()
        .table("worry_signals")
        .select("*")
        .eq("patient_id", patient_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


def has_open_escalation(patient_id: str) -> bool:
    """True if this patient already has an unresolved escalation.

    Deduplication matters more than it looks: without it, three nurses tapping
    worry on the same deteriorating patient produces three pages, which is
    precisely the alarm fatigue the product claims to reduce.
    """
    resp = (
        supabase()
        .table("escalations")
        .select("id")
        .eq("patient_id", patient_id)
        .in_("status", ["open", "acknowledged", "auto_escalated"])
        .limit(1)
        .execute()
    )
    return bool(resp.data or [])


def persist_risk(
    org_id: str, patient_id: str, result: Any
) -> dict[str, Any] | None:
    """Write a risk_scores row for the audit/learn trail."""
    payload = {
        "org_id": org_id,
        "patient_id": patient_id,
        "score": float(result.score),
        "worry_component": float(result.worry_component),
        "vitals_component": float(result.vitals_component),
        "trend_component": float(result.trend_component),
        "inputs": result.inputs,
    }
    resp = supabase().table("risk_scores").insert(payload).execute()
    rows = resp.data or []
    return rows[0] if rows else None


def open_escalation(
    org_id: str,
    patient_id: str,
    rule: str,
    worry_signal_id: str | None = None,
    sla_minutes: int | None = None,
) -> dict[str, Any] | None:
    """Create an escalation unless one is already live for this patient."""
    if has_open_escalation(patient_id):
        return None

    minutes = sla_minutes if sla_minutes is not None else org_sla_minutes(org_id)
    now = _now()

    payload = {
        "org_id": org_id,
        "patient_id": patient_id,
        "worry_signal_id": worry_signal_id,
        "rule": rule,
        "status": "open",
        "triggered_at": _iso(now),
        "sla_deadline": _iso(now + timedelta(minutes=minutes)),
    }
    resp = supabase().table("escalations").insert(payload).execute()
    rows = resp.data or []
    return rows[0] if rows else None


def evaluate_patient(
    org_id: str,
    patient_id: str,
    worry_signal_id: str | None = None,
) -> tuple[Any, dict[str, Any] | None]:
    """Recompute risk for one patient and escalate if the rules fire.

    Returns (RiskResult, escalation_row_or_None). This is called both on the
    worry-tap path (synchronously, so the nurse gets immediate feedback) and
    from the background recompute job.
    """
    vitals = recent_vitals(patient_id)
    worry = recent_worry(patient_id)

    result = compute_risk(vitals, worry)
    persist_risk(org_id, patient_id, result)

    escalation = None
    if result.should_escalate and result.rule:
        escalation = open_escalation(
            org_id=org_id,
            patient_id=patient_id,
            rule=result.rule,
            worry_signal_id=worry_signal_id,
        )

    return result, escalation


def _find_escalation(escalation_id: str, org_id: str) -> dict | None:
    rows = supabase().table("escalations").select("*").eq("id", escalation_id).eq("org_id", org_id).limit(1).execute().data or []
    return rows[0] if rows else None


def acknowledge(escalation_id: str, org_id: str, actor_id: str) -> dict[str, Any] | None:
    """Someone has seen it. Stops the clock on ack_seconds.

    Idempotent: re-acknowledging does not overwrite the original timestamp,
    because the first response is the one the metric cares about.
    """
    resp = (
        supabase()
        .table("escalations")
        .select("*")
        .eq("id", escalation_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return None
    row = rows[0]

    if row.get("acknowledged_at") or row["status"] in ("assessed", "resolved"):
        return row

    update = {
        "acknowledged_by": actor_id,
        "acknowledged_at": _iso(_now()),
    }
    # An auto_escalated row keeps that status -- the SLA breach is a permanent
    # fact about this escalation and must survive a late acknowledgement.
    if row["status"] == "open":
        update["status"] = "acknowledged"

    updated = (
        supabase()
        .table("escalations")
        .update(update)
        .eq("id", escalation_id)
        .eq("org_id", org_id)
        .is_("acknowledged_at", "null")
        .in_("status", ["open", "acknowledged", "auto_escalated"])
        .execute()
    )
    out = updated.data or []
    return out[0] if out else _find_escalation(escalation_id, org_id)


def assess(
    escalation_id: str, org_id: str, actor_id: str, note: str | None = None
) -> dict[str, Any] | None:
    """Bedside reassessment happened. Stops the clock on assess_seconds."""
    now = _now()
    update: dict[str, Any] = {
        "assessed_by": actor_id,
        "assessed_at": _iso(now),
        "status": "assessed",
    }
    if note:
        update["resolution_note"] = note

    # Assessing implies acknowledgement, for the case where a responder goes
    # straight to the bedside without tapping acknowledge first.
    existing = (
        supabase()
        .table("escalations")
        .select("*")
        .eq("id", escalation_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    rows = existing.data or []
    if not rows:
        return None
    if rows[0].get("assessed_at") or rows[0]["status"] == "resolved":
        return rows[0]
    if not rows[0].get("acknowledged_at"):
        update["acknowledged_by"] = actor_id
        update["acknowledged_at"] = _iso(now)

    updated = (
        supabase()
        .table("escalations")
        .update(update)
        .eq("id", escalation_id)
        .eq("org_id", org_id)
        .is_("assessed_at", "null")
        .in_("status", ["open", "acknowledged", "auto_escalated"])
        .execute()
    )
    out = updated.data or []
    return out[0] if out else _find_escalation(escalation_id, org_id)


def resolve(
    escalation_id: str, org_id: str, actor_id: str, note: str | None = None
) -> dict[str, Any] | None:
    update: dict[str, Any] = {
        "status": "resolved",
        "resolved_at": _iso(_now()),
    }
    if note:
        update["resolution_note"] = note
    updated = (
        supabase()
        .table("escalations")
        .update(update)
        .eq("id", escalation_id)
        .eq("org_id", org_id)
        .execute()
    )
    out = updated.data or []
    return out[0] if out else None


def sweep_sla_breaches() -> list[dict[str, Any]]:
    """Flip un-responded escalations past their deadline to auto_escalated.

    Runs on a timer. This is the mechanism behind "if a worried nurse gets no
    reassessment within the window, it escalates automatically" -- and in the
    demo it is the moment the system visibly takes the nurse's side.
    """
    now = _now()
    due = (
        supabase()
        .table("escalations")
        .select("*")
        .eq("status", "open")
        .lt("sla_deadline", _iso(now))
        .execute()
    )

    breached: list[dict[str, Any]] = []
    for row in due.data or []:
        updated = (
            supabase()
            .table("escalations")
            .update({"status": "auto_escalated", "rule": row["rule"]})
            .eq("id", row["id"])
            .eq("status", "open")  # guard against a concurrent acknowledge
            .execute()
        )
        if updated.data:
            breached.append(updated.data[0])
            supabase().table("audit_log").insert(
                {
                    "org_id": row["org_id"],
                    "actor_id": None,
                    "action": "escalation.auto_escalated",
                    "target_type": "escalation",
                    "target_id": row["id"],
                    "payload": {
                        "rule": row["rule"],
                        "label": RULE_LABELS.get("sla_breach"),
                        "sla_deadline": row["sla_deadline"],
                    },
                }
            ).execute()

    return breached
