"""Shared singletons: the Supabase service client and a tiny TTL cache."""

import threading
import time
from typing import Any

from supabase import Client, create_client

from app.config import Config

_client: Client | None = None
_client_lock = threading.Lock()


def supabase() -> Client:
    """Service-role Supabase client.

    This bypasses RLS by design -- the API is the trusted layer and enforces
    org scoping itself via the org_scoped decorator. Every query built on this
    client must filter on org_id.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = create_client(
                    Config.SUPABASE_URL, Config.SUPABASE_SERVICE_KEY
                )
    return _client


class TTLCache:
    """Minimal thread-safe TTL cache.

    Used for JWKS documents and resolved profiles. Deliberately not Redis --
    a single-process hackathon deployment does not need the operational cost,
    and the TTLs are short enough that staleness is bounded.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._data[key]
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, value)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


profile_cache = TTLCache(Config.PROFILE_CACHE_SECONDS)
jwks_cache = TTLCache(Config.JWKS_CACHE_SECONDS)
