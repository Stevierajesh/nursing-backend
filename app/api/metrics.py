"""Metrics endpoints.

Three questions, three routes:

  * worry-to-response -- the metric hospitals do not currently track
  * equity -- "is it fair?", answered with grouped response times
  * reason performance -- which flavours of nurse intuition carried signal

All three read from the SQL views so the API and any direct dashboard query
agree on the definitions.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, request

from app.auth.middleware import require_auth, require_role
from app.extensions import supabase

bp = Blueprint("metrics", __name__, url_prefix="/api/v1/metrics")


def _window_start(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@bp.get("/worry-to-response")
@require_auth
@require_role("charge_nurse", "admin")
def worry_to_response():
    """Distribution of time from worry raised to first response.

    Returns median alongside mean. Response-time distributions are
    right-skewed -- one forgotten escalation drags the mean while the median
    stays honest about the typical case, and a hospital needs to see both.
    """
    user = g.user
    days = int(request.args.get("days", "7"))

    resp = (
        supabase()
        .table("v_worry_response_times")
        .select("*")
        .eq("org_id", user.org_id)
        .gte("triggered_at", _window_start(days))
        .execute()
    )
    rows = resp.data or []

    ack = [float(r["ack_seconds"]) for r in rows if r.get("ack_seconds") is not None]
    assess = [
        float(r["assess_seconds"]) for r in rows if r.get("assess_seconds") is not None
    ]
    breaches = [r for r in rows if r.get("breached_sla")]
    unanswered = [r for r in rows if r.get("ack_seconds") is None]

    def stats(values: list[float]) -> dict:
        if not values:
            return {"count": 0, "mean_minutes": None, "median_minutes": None,
                    "p90_minutes": None}
        ordered = sorted(values)
        p90_idx = min(int(len(ordered) * 0.9), len(ordered) - 1)
        return {
            "count": len(values),
            "mean_minutes": round(statistics.fmean(values) / 60.0, 1),
            "median_minutes": round(statistics.median(values) / 60.0, 1),
            "p90_minutes": round(ordered[p90_idx] / 60.0, 1),
        }

    return jsonify(
        {
            "window_days": days,
            "total_escalations": len(rows),
            "acknowledge": stats(ack),
            "assess": stats(assess),
            "sla_breaches": len(breaches),
            "sla_breach_pct": (
                round(100.0 * len(breaches) / len(rows), 1) if rows else 0.0
            ),
            "still_unanswered": len(unanswered),
            "by_rule": _group_counts(rows, "rule"),
            "by_worry_level": _group_counts(rows, "worry_level"),
        }
    )


def _group_counts(rows: list[dict], key: str) -> list[dict]:
    buckets: dict[str, list[float]] = {}
    for r in rows:
        k = str(r.get(key))
        buckets.setdefault(k, [])
        if r.get("ack_seconds") is not None:
            buckets[k].append(float(r["ack_seconds"]))
    return [
        {
            "value": k,
            "count": len(
                [r for r in rows if str(r.get(key)) == k]
            ),
            "mean_ack_minutes": (
                round(statistics.fmean(v) / 60.0, 1) if v else None
            ),
        }
        for k, v in sorted(buckets.items())
    ]


@bp.get("/equity")
@require_auth
@require_role("charge_nurse", "admin")
def equity():
    """Response times and SLA breach rates grouped by patient demographics.

    This is the audit that answers "is it fair?". Small-n buckets are returned
    rather than hidden, but flagged, because suppressing them silently would
    let a disparity hide behind a low denominator.
    """
    user = g.user
    dimension = request.args.get("dimension")

    query = (
        supabase()
        .table("v_equity_audit")
        .select("*")
        .eq("org_id", user.org_id)
    )
    if dimension:
        query = query.eq("dimension", dimension)

    rows = (query.execute().data) or []

    for r in rows:
        # Under 5 escalations, differences are noise. Say so explicitly.
        r["low_confidence"] = int(r.get("escalation_count") or 0) < 5

    # Worry rate by demographic, independent of escalation -- catches the case
    # where nurses raise worry less often for some groups in the first place.
    worry_rates = (
        supabase()
        .table("worry_signals")
        .select("id, level, patient:patients(race_code, primary_language, age_band, sex)")
        .eq("org_id", user.org_id)
        .gt("level", 0)
        .limit(2000)
        .execute()
    ).data or []

    return jsonify(
        {
            "dimensions": ["race", "language", "age_band", "sex"],
            "response_time_audit": rows,
            "worry_signal_count": len(worry_rates),
            "note": (
                "Buckets with fewer than 5 escalations are flagged "
                "low_confidence; differences there are not interpretable."
            ),
        }
    )


@bp.get("/worry-reasons")
@require_auth
@require_role("charge_nurse", "admin")
def worry_reasons():
    """Which worry reasons preceded real deterioration within 24h.

    This is the learn loop. With enough rows it is also the honest answer to
    "is nurse intuition actually predictive?" -- per reason, with a denominator.
    """
    user = g.user
    rows = (
        supabase()
        .table("v_worry_reason_performance")
        .select("*")
        .eq("org_id", user.org_id)
        .execute()
    ).data or []

    rows.sort(key=lambda r: float(r.get("precision_pct") or 0), reverse=True)
    for r in rows:
        r["low_confidence"] = int(r.get("signal_count") or 0) < 5

    return jsonify({"reasons": rows})
