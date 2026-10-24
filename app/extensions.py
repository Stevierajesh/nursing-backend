"""Shared singletons: the Supabase service client and a tiny TTL cache."""

import logging
import threading
import time
from typing import Any

import httpx
from supabase import Client, create_client

from app.config import Config

log = logging.getLogger(__name__)

_client: Client | None = None
_client_lock = threading.Lock()

# Message httpx uses when a pooled keep-alive connection was closed by the
# server before it sent any response bytes.
_IDLE_CLOSE = "Server disconnected without sending a response"

_IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


class _ReconnectingTransport(httpx.HTTPTransport):
    """Retry requests killed by an idle connection being closed server-side.

    Supabase sits behind Cloudflare, which closes idle HTTP/2 connections.
    httpx keeps them pooled, so the first request after a quiet period fails
    with `RemoteProtocolError: Server disconnected` rather than transparently
    reconnecting. Observed here as 500s on the first call after startup, which
    in practice means the dashboard errors the moment anyone comes back to it.

    Retrying is safe for this specific failure: the server closed the
    connection *before sending any response*, so the request was never
    processed. Non-idempotent methods are still only retried on exactly that
    condition -- a generic protocol error mid-response could mean a POST
    was applied, and replaying it would duplicate a worry signal or an
    escalation.
    """

    MAX_ATTEMPTS = 3

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        last_exc: Exception | None = None

        for attempt in range(self.MAX_ATTEMPTS):
            try:
                return super().handle_request(request)
            except (httpx.RemoteProtocolError, httpx.ConnectError) as exc:
                idle_close = _IDLE_CLOSE in str(exc)
                replayable = request.method in _IDEMPOTENT or idle_close
                if attempt == self.MAX_ATTEMPTS - 1 or not replayable:
                    raise
                last_exc = exc
                log.warning(
                    "Supabase connection dropped (%s %s), retry %d/%d: %s",
                    request.method,
                    request.url.path,
                    attempt + 1,
                    self.MAX_ATTEMPTS - 1,
                    exc,
                )
                time.sleep(0.1 * (2**attempt))

        assert last_exc is not None
        raise last_exc


def _install_retrying_transport(client: Client) -> None:
    """Swap the PostgREST session's transport for the retrying one.

    supabase-py exposes no hook for a custom httpx client, so this reaches into
    the constructed session. Guarded because the attribute path is not public
    API and may move between releases -- losing retries is far better than
    failing to start.
    """
    try:
        session = client.postgrest.session
        session._transport = _ReconnectingTransport()
    except Exception as exc:  # pragma: no cover - depends on library internals
        log.warning(
            "Could not install the reconnecting transport; transient "
            "'Server disconnected' errors will surface as 500s. (%s)",
            exc,
        )


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
                client = create_client(
                    Config.SUPABASE_URL, Config.SUPABASE_SERVICE_KEY
                )
                _install_retrying_transport(client)
                _client = client
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
