"""Health and identity routes."""

from __future__ import annotations

from flask import Blueprint, g, jsonify

from app.auth.jwt import project_uses_asymmetric_keys
from app.auth.middleware import require_auth
from app.config import Config
from app.extensions import supabase

bp = Blueprint("health", __name__, url_prefix="/api/v1")


@bp.get("/health")
def health():
    """Unauthenticated liveness check.

    Also reports which JWT scheme the project uses, which is the single most
    useful thing to know when auth is mysteriously failing.
    """
    db_ok = True
    db_error = None
    try:
        supabase().table("organizations").select("id").limit(1).execute()
    except Exception as exc:
        db_ok = False
        db_error = str(exc)

    return (
        jsonify(
            {
                "status": "ok" if db_ok else "degraded",
                "env": Config.ENV,
                "database": {"reachable": db_ok, "error": db_error},
                "jwt_scheme": (
                    "asymmetric (JWKS)"
                    if project_uses_asymmetric_keys()
                    else "symmetric (HS256 shared secret)"
                ),
                "scheduler_enabled": Config.ENABLE_SCHEDULER,
            }
        ),
        200 if db_ok else 503,
    )


@bp.get("/me")
@require_auth
def me():
    """The caller's identity as the API sees it.

    The frontends call this once after login to learn their role, which is what
    drives role-based routing.
    """
    user = g.user
    org = (
        supabase()
        .table("organizations")
        .select("id, name, slug, worry_sla_minutes")
        .eq("id", user.org_id)
        .limit(1)
        .execute()
    ).data or []

    unit = None
    if user.unit_id:
        unit_rows = (
            supabase()
            .table("units")
            .select("id, name")
            .eq("id", user.unit_id)
            .limit(1)
            .execute()
        ).data or []
        unit = unit_rows[0] if unit_rows else None

    return jsonify(
        {
            "user": {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role,
                "unit_id": user.unit_id,
                "is_org_wide": user.is_org_wide,
            },
            "organization": org[0] if org else None,
            "unit": unit,
        }
    )
