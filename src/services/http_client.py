"""
Process-wide, pooled httpx.AsyncClient shared by every outbound HTTP call.

Why this exists
---------------
Creating a fresh ``httpx.AsyncClient`` per request opens a brand-new TCP + TLS
connection every time and burns an outbound (SNAT) port. On Azure App Service
each instance only gets ~128 SNAT ports to public endpoints, so a burst of
per-request clients (TTS per utterance, per-call auth/clinical/telnyx/upload/log)
drives the instance toward port exhaustion — which shows up as ``ConnectTimeout``
to *every* external host at once.

Reusing one pooled client with keep-alive collapses that to a small, bounded,
reused set of connections. A deliberately short *connect* timeout means an
egress/SNAT problem fails fast and frees the port instead of pinning it for the
full read timeout.

Usage
-----
    from src.services.http_client import get_http_client, request_timeout

    client = get_http_client()
    resp = await client.post(url, json=..., timeout=request_timeout(read=30))

Everything runs on the app's single event loop (uvicorn), so a module-level
singleton created lazily on first use is safe. It is closed on app shutdown via
``aclose_http_client()`` (wired in main.on_shutdown).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Hard cap on total concurrent outbound connections — a burst can never exhaust
# SNAT ports past this. Keep-alive connections are reused across requests.
_MAX_CONNECTIONS = int(os.getenv("HTTP_MAX_CONNECTIONS", "100"))
_MAX_KEEPALIVE = int(os.getenv("HTTP_MAX_KEEPALIVE_CONNECTIONS", "20"))
_KEEPALIVE_EXPIRY_S = float(os.getenv("HTTP_KEEPALIVE_EXPIRY_S", "30"))

# Connect is intentionally short: if we can't get a socket + handshake in this
# window it's an egress/SNAT problem, so fail fast and free the port rather than
# holding it for the (much longer) read timeout.
_DEFAULT_CONNECT_S = float(os.getenv("HTTP_CONNECT_TIMEOUT_S", "5"))
_DEFAULT_READ_S = float(os.getenv("HTTP_READ_TIMEOUT_S", "30"))

_limits = httpx.Limits(
    max_connections=_MAX_CONNECTIONS,
    max_keepalive_connections=_MAX_KEEPALIVE,
    keepalive_expiry=_KEEPALIVE_EXPIRY_S,
)

_client: Optional[httpx.AsyncClient] = None


def request_timeout(read: float, connect: float = _DEFAULT_CONNECT_S) -> httpx.Timeout:
    """Per-request timeout that keeps a short connect cap while allowing a longer
    read/write for big transfers (recording download, multipart file upload).

    Pass this as ``timeout=`` on individual requests so each call site keeps its
    original read budget without lengthening the connect cap.
    """
    return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)


def get_http_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating it on first use.

    Safe to call from any coroutine on the app's event loop; the connection pool
    binds to the running loop on the first request.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            limits=_limits,
            timeout=httpx.Timeout(
                connect=_DEFAULT_CONNECT_S,
                read=_DEFAULT_READ_S,
                write=_DEFAULT_READ_S,
                pool=_DEFAULT_CONNECT_S,
            ),
        )
        logger.info(
            f"🌐 Initialized shared httpx client "
            f"(max_connections={_MAX_CONNECTIONS}, max_keepalive={_MAX_KEEPALIVE})"
        )
    return _client


async def aclose_http_client() -> None:
    """Close the shared client on app shutdown (idempotent)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
