"""Configuration loaded from the environment."""

import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


class Config:
    # --- Supabase -----------------------------------------------------------
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    # Service role key: bypasses RLS. Never ship this to a browser.
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

    # --- JWT verification ---------------------------------------------------
    # Supabase projects sign access tokens either with an asymmetric key
    # (RS256/ES256, verified via JWKS) or with the legacy shared secret (HS256).
    # We support both so this runs regardless of project vintage.
    SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")
    JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "authenticated")
    JWKS_CACHE_SECONDS = int(os.environ.get("JWKS_CACHE_SECONDS", "3600"))

    # How long a resolved profile (org_id + role) is cached in-process. Keeps
    # the hot path to one JWT verification instead of a DB round trip.
    PROFILE_CACHE_SECONDS = int(os.environ.get("PROFILE_CACHE_SECONDS", "60"))

    # --- app ----------------------------------------------------------------
    ENV = os.environ.get("FLASK_ENV", "development")
    DEBUG = ENV == "development"
    CORS_ORIGINS = [
        o.strip()
        for o in os.environ.get(
            "CORS_ORIGINS",
            "http://localhost:5173,http://localhost:5174",
        ).split(",")
        if o.strip()
    ]

    # --- background jobs ----------------------------------------------------
    ENABLE_SCHEDULER = os.environ.get("ENABLE_SCHEDULER", "true").lower() == "true"
    RISK_RECOMPUTE_SECONDS = int(os.environ.get("RISK_RECOMPUTE_SECONDS", "60"))
    SLA_SWEEP_SECONDS = int(os.environ.get("SLA_SWEEP_SECONDS", "30"))

    @classmethod
    def validate(cls) -> None:
        _require("SUPABASE_URL")
        _require("SUPABASE_SERVICE_KEY")
        if not cls.SUPABASE_JWT_SECRET:
            # Not fatal: asymmetric projects verify via JWKS and need no secret.
            pass
