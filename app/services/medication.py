"""Two-level escalation for medication review requests.

Level one is the prescriber the nurse named. Level two is every prescriber in
the org, reached by flipping the row to 'escalated' -- the RLS policy in
migration 20261024001300 widens the read from there, so no second role and no
fan-out table is needed.

This mirrors sweep_sla_breaches() in escalation.py on purpose. A medication
request that nobody answers is the same failure as a worry escalation that
nobody answers, and it should not be a second mechanism to reason about.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.extensions import supabase

log = logging.getLogger(__name__)
DEFAULT_SLA_MINUTES = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def org_medication_sla_minutes(org_id: str) -> int:
    """The org's medication SLA, falling back rather than failing closed.

    A missing column or row must not stop requests being created; the default
    matches the migration's.
    """
    try:
        rows = (
            supabase()
            .table("organizations")
            .select("medication_sla_minutes")
            .eq("id", org_id)
            .execute()
        ).data or []
    except Exception:
        log.exception("Could not read medication SLA for org %s", org_id)
        return DEFAULT_SLA_MINUTES
    if not rows or rows[0].get("medication_sla_minutes") is None:
        return DEFAULT_SLA_MINUTES
    return int(rows[0]["medication_sla_minutes"])


def sla_deadline_for(org_id: str, created_at: datetime | None = None) -> str:
    """When a request sent now stops being somebody's individual problem."""
    start = created_at or _now()
    return (start + timedelta(minutes=org_medication_sla_minutes(org_id))).isoformat()


def sweep_medication_review_breaches() -> list[dict[str, Any]]:
    """Escalate pending requests past their deadline to the whole prescriber pool.

    The status guard on the update is what makes this safe to run from more
    than one worker: a request the assigned prescriber answered a moment ago
    fails the `status = pending` predicate and is left alone.
    """
    due = (
        supabase()
        .table("medication_reviews")
        .select("id, org_id, patient_id, prescriber_id, sla_deadline")
        .eq("status", "pending")
        .lt("sla_deadline", _now().isoformat())
        .execute()
    ).data or []

    escalated: list[dict[str, Any]] = []
    for row in due:
        # Hand the row to a named second prescriber when the org has one, so
        # the queue shows an owner rather than going to everybody and nobody.
        # When it does not, 'escalated' alone still widens the read.
        backup = (
            supabase()
            .table("profiles")
            .select("id")
            .eq("org_id", row["org_id"])
            .eq("role", "prescriber")
            .neq("id", row["prescriber_id"])
            .limit(1)
            .execute()
        ).data or []

        update = {"status": "escalated", "escalated_at": _now().isoformat()}
        if backup:
            update["escalated_to"] = backup[0]["id"]

        changed = (
            supabase()
            .table("medication_reviews")
            .update(update)
            .eq("id", row["id"])
            .eq("status", "pending")
            .execute()
        ).data or []

        if changed:
            escalated.append(changed[0])
            supabase().table("audit_log").insert({
                "org_id": row["org_id"],
                "actor_id": None,
                "action": "medication_review.escalated",
                "target_type": "medication_review",
                "target_id": row["id"],
                "payload": {
                    "prescriber_id": row["prescriber_id"],
                    "escalated_to": update.get("escalated_to"),
                    "sla_deadline": row["sla_deadline"],
                },
            }).execute()

    return escalated
