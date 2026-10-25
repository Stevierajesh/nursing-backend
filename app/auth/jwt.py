"""Supabase access-token verification.

The frontends authenticate directly against Supabase and send the resulting
access token as `Authorization: Bearer <jwt>`. This module verifies it. Flask
never issues tokens -- it is a pure resource server.

Two signing schemes are supported:

  * Asymmetric (RS256/ES256) -- the default for current Supabase projects.
    The signing keys are fetched from the project's JWKS endpoint and cached.
  * Symmetric (HS256) -- legacy projects using a shared JWT secret.

The scheme is chosen from the token's own `alg` header, so a project that
rotates from one to the other keeps working without a code change.
"""

from __future__ import annotations

import jwt
import requests
from jwt import PyJWKClient

from app.config import Config
from app.extensions import jwks_cache


class AuthError(Exception):
    """Raised when a token is missing, malformed, or fails verification."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


_jwk_client: PyJWKClient | None = None


def _jwks_url() -> str:
    return f"{Config.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        # PyJWKClient keeps its own small cache; ours guards the availability
        # check below so a cold start does not hammer the auth endpoint.
        _jwk_client = PyJWKClient(_jwks_url(), cache_keys=True)
    return _jwk_client


def project_uses_asymmetric_keys() -> bool:
    """True when the project publishes JWKS signing keys.

    Cached because it is checked on the request path and the answer only
    changes when someone rotates keys in the Supabase dashboard.
    """
    cached = jwks_cache.get("has_asymmetric")
    if cached is not None:
        return bool(cached)

    try:
        resp = requests.get(_jwks_url(), timeout=5)
        has_keys = resp.status_code == 200 and bool(resp.json().get("keys"))
    except (requests.RequestException, ValueError):
        has_keys = False

    jwks_cache.set("has_asymmetric", has_keys)
    return has_keys


def verify_token(token: str) -> dict:
    """Verify a Supabase access token and return its claims.

    Raises AuthError on any failure. Callers must not fall back to trusting
    unverified claims.
    """
    if not token:
        raise AuthError("Missing bearer token")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AuthError(f"Malformed token: {exc}") from exc

    alg = header.get("alg", "")

    try:
        if alg.startswith(("RS", "ES")):
            signing_key = _get_jwk_client().get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256", "ES256"],
                audience=Config.JWT_AUDIENCE,
                leeway=Config.JWT_LEEWAY_SECONDS,
                options={"require": ["exp", "sub"]},
            )
        elif alg == "HS256":
            if not Config.SUPABASE_JWT_SECRET:
                raise AuthError(
                    "Token is HS256 but SUPABASE_JWT_SECRET is not configured"
                )
            claims = jwt.decode(
                token,
                Config.SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience=Config.JWT_AUDIENCE,
                leeway=Config.JWT_LEEWAY_SECONDS,
                options={"require": ["exp", "sub"]},
            )
        else:
            # Refuse anything we do not explicitly understand -- notably "none".
            raise AuthError(f"Unsupported token algorithm: {alg or 'unknown'}")
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise AuthError("Token audience mismatch") from exc
    except jwt.ImmatureSignatureError as exc:
        # Past JWT_LEEWAY_SECONDS of skew this is a host clock problem, not a
        # bad token. Say so, because the default message ("not yet valid")
        # sends you looking at the frontend instead of at `timedatectl`.
        raise AuthError(
            f"Token is not yet valid: this host's clock is more than "
            f"{Config.JWT_LEEWAY_SECONDS}s behind the token issuer. Check NTP."
        ) from exc
    except jwt.PyJWTError as exc:
        raise AuthError(f"Token verification failed: {exc}") from exc

    if not claims.get("sub"):
        raise AuthError("Token has no subject claim")

    return claims
