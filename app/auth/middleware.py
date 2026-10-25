"""Auth middleware: decorators that establish and constrain request identity.

Composition order matters. `require_auth` must run outermost because the
role and scoping checks read the identity it installs on `g`.

    @bp.post("/worry")
    @require_auth
    @require_role("nurse", "charge_nurse")
    def create_worry(): ...
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from typing import Callable

from flask import g, jsonify, request

from app.auth.jwt import AuthError, verify_token
from app.extensions import profile_cache, supabase

ROLES = ("admin", "charge_nurse", "nurse", "prescriber")


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    org_id: str
    role: str
    unit_id: str | None
    full_name: str

    @property
    def is_org_wide(self) -> bool:
        """Admins and charge nurses see the whole org; nurses see their own."""
        return self.role in ("admin", "charge_nurse", "prescriber")


def _bearer_token() -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise AuthError("Expected 'Authorization: Bearer <token>' header")
    return header[7:].strip()


def _load_profile(user_id: str, email: str) -> CurrentUser:
    """Resolve org_id and role for a verified user.

    Cached for PROFILE_CACHE_SECONDS so the hot path is one JWT verification
    rather than a database round trip per request. If you later add a Supabase
    custom access token hook that stamps org_id and role into the JWT itself,
    this function becomes a pure claim read and the cache can go away.
    """
    cached = profile_cache.get(user_id)
    if cached is not None:
        return cached

    resp = (
        supabase()
        .table("profiles")
        .select("id, org_id, role, unit_id, full_name, email")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    rows = resp.data or []
    if not rows:
        # Authenticated against Supabase but not provisioned into an org. This
        # is a real state (user signed up before an admin added them), so it
        # gets its own status rather than a generic 401.
        raise AuthError(
            "No profile provisioned for this account. Ask an administrator "
            "to add you to an organization.",
            status=403,
        )

    row = rows[0]
    user = CurrentUser(
        id=row["id"],
        email=row.get("email") or email,
        org_id=row["org_id"],
        role=row["role"],
        unit_id=row.get("unit_id"),
        full_name=row.get("full_name") or "",
    )
    profile_cache.set(user_id, user)
    return user


def require_auth(fn: Callable) -> Callable:
    """Verify the bearer token and install `g.user`."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            claims = verify_token(_bearer_token())
            g.claims = claims
            g.user = _load_profile(claims["sub"], claims.get("email", ""))
        except AuthError as exc:
            return jsonify({"error": exc.message}), exc.status
        return fn(*args, **kwargs)

    return wrapper


def require_role(*allowed: str) -> Callable:
    """Restrict a route to the given roles.

    Unknown role names raise at import time rather than silently denying every
    request, which is the failure mode that costs you an hour at 3am.
    """
    unknown = set(allowed) - set(ROLES)
    if unknown:
        raise ValueError(f"Unknown role(s) in require_role: {sorted(unknown)}")

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = getattr(g, "user", None)
            if user is None:
                return jsonify({"error": "require_auth must precede require_role"}), 500
            if user.role not in allowed:
                return (
                    jsonify(
                        {
                            "error": "Insufficient role",
                            "required": list(allowed),
                            "actual": user.role,
                        }
                    ),
                    403,
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def current_org_id() -> str:
    """The caller's org. Every query must filter on this."""
    user = getattr(g, "user", None)
    if user is None:
        raise AuthError("No authenticated user on this request", status=500)
    return user.org_id


def scoped(table: str):
    """A Supabase query builder pre-filtered to the caller's organization.

    Using this instead of `supabase().table(...)` is what makes cross-tenant
    leakage a code-level impossibility rather than a policy you hope holds.
    The service-role key bypasses RLS, so this filter is the real boundary on
    the API path.
    """
    return supabase().table(table).select("*").eq("org_id", current_org_id())


def scoped_write(table: str) -> tuple:
    """Return (query_builder, org_id) for writes that must stamp org_id."""
    return supabase().table(table), current_org_id()


def assert_patient_in_org(patient_id: str) -> dict:
    """Fetch a patient, refusing to cross the org boundary.

    Returns the patient row. Raises AuthError(404) if the patient does not
    exist *or* belongs to another org -- the two cases are deliberately
    indistinguishable to the caller so the API cannot be used to probe for
    patient IDs in other organizations.
    """
    resp = (
        supabase()
        .table("patients")
        .select("*")
        .eq("id", patient_id)
        .eq("org_id", current_org_id())
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise AuthError("Patient not found", status=404)
    return rows[0]


def assert_nurse_assigned(patient_id: str) -> None:
    """A nurse may only raise worry for a patient they are on shift for.

    Charge nurses and admins are exempt: they cover the whole unit and need to
    be able to act on any patient.
    """
    user = g.user
    if user.is_org_wide:
        return

    resp = (
        supabase()
        .table("assignments")
        .select("id")
        .eq("patient_id", patient_id)
        .eq("nurse_id", user.id)
        .is_("shift_end", "null")
        .limit(1)
        .execute()
    )
    if not (resp.data or []):
        raise AuthError("You are not assigned to this patient", status=403)
