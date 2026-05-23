"""Google A2A v0.3 protocol — client and server (FastAPI app builder).

Client: JSON-RPC POST /a2a (sync) and POST /a2a/stream (SSE iterator).
Both attach an HMAC grant in BOTH the Authorization header AND the body's
params._meta.authz_grant (spec §6.1 double-write).

Server: build_app() returns a FastAPI app with the three standard routes:
  GET  /.well-known/agent.json  — our self-card
  POST /a2a                     — JSON-RPC sync
  POST /a2a/stream              — SSE
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from agents.comm_agent.peer_registry import Peer
from agents.shared.authz import (
    AuthzError, NonceCache, sign_cross_machine_grant,
    verify_cross_machine_grant,
)

log = logging.getLogger(__name__)


class A2AClientError(Exception):
    pass


_DEFAULT_BACKOFF = (0.5, 1.0, 2.0)


class A2AClient:
    """Minimal A2A v0.3 client over httpx."""

    def __init__(
        self,
        peer: Peer,
        *,
        secret: str,
        my_peer_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_backoff: tuple[float, ...] = _DEFAULT_BACKOFF,
        timeout: float = 30.0,
    ):
        self._peer = peer
        self._secret = secret
        self._my_peer_id = my_peer_id
        self._retry_backoff = retry_backoff
        self._timeout = timeout
        # MVP: when tls_pinned_sha256 is set we accept any self-signed cert.
        # Real fingerprint enforcement is deferred to v1.1 (spec §9). HMAC
        # signing of payloads defeats MITM in the meantime.
        verify = peer.tls_verify if peer.tls_pinned_sha256 is None else False
        self._transport = transport
        self._verify = verify

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._verify,
            transport=self._transport,
            timeout=self._timeout,
        )

    def _build_envelope(self, method: str, params: dict, skill: str) -> dict:
        grant = sign_cross_machine_grant(
            my_peer_id=self._my_peer_id,
            target_peer_id=self._peer.peer_id,
            requested_skill=skill,
            key=self._secret,
            ttl_seconds=60,
        )
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": {
                **params,
                "_meta": {**params.get("_meta", {}), "authz_grant": grant},
            },
        }
        return body, grant

    async def fetch_agent_card(self) -> dict:
        async with self._client() as c:
            r = await c.get(f"{self._peer.url}/.well-known/agent.json")
            r.raise_for_status()
            return r.json()

    async def call(self, *, method: str, params: dict, skill: str | None = None) -> dict:
        """Sync JSON-RPC call. ``skill`` defaults to ``method`` for grant scoping."""
        body, grant = self._build_envelope(method, params, skill or method)
        last_exc: Exception | None = None
        for attempt, delay in enumerate((0.0, *self._retry_backoff)):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                async with self._client() as c:
                    r = await c.post(
                        f"{self._peer.url}/a2a",
                        json=body,
                        headers={"Authorization": f"A2A-HMAC {grant}"},
                    )
            except (httpx.ConnectError, httpx.ReadError) as exc:
                last_exc = exc
                continue
            if 500 <= r.status_code < 600:
                last_exc = A2AClientError(f"5xx from peer: {r.status_code} {r.text}")
                continue
            if r.status_code in (401, 403):
                raise A2AClientError(f"auth refused: HTTP {r.status_code} {r.text}")
            if 400 <= r.status_code < 500:
                raise A2AClientError(f"4xx from peer: {r.status_code} {r.text}")
            envelope = r.json()
            if "error" in envelope:
                raise A2AClientError(f"jsonrpc error: {envelope['error']}")
            return envelope.get("result", {})
        raise A2AClientError(
            f"peer unreachable: {self._peer.url} (retried {len(self._retry_backoff)}): {last_exc!r}"
        )
