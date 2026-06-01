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
        # Cert verification follows ``tls_verify`` only. A ``tls_pinned_sha256``
        # used to flip verification OFF here, but fingerprint enforcement is not
        # implemented yet (deferred to v1.1, spec §9) — so disabling verification
        # on a pin meant an https peer "secured" with a pin actually accepted ANY
        # certificate and was fully MITM-able. Until pinning is enforced, a pin is
        # recorded but does NOT weaken TLS; verification stays on unless the
        # caller explicitly sets ``tls_verify=False``.
        verify = peer.tls_verify
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
        last_exc: Exception | None = None
        for attempt, delay in enumerate((0.0, *self._retry_backoff)):
            if delay > 0:
                await asyncio.sleep(delay)
            # Mint a FRESH grant (new nonce + exp) per attempt. The verifier
            # burns the nonce in its replay cache BEFORE running the dispatcher,
            # so a peer that 5xx's *after* authenticating has already consumed
            # this nonce — reusing the same grant on retry would be rejected as
            # a replay (401), silently defeating the 5xx retry. A fresh nonce
            # also dodges a stale ``exp`` on the later, backed-off attempts.
            body, grant = self._build_envelope(method, params, skill or method)
            try:
                async with self._client() as c:
                    r = await c.post(
                        f"{self._peer.url}/a2a",
                        json=body,
                        headers={"Authorization": f"A2A-HMAC {grant}"},
                    )
            except httpx.ConnectError as exc:
                # Pre-flight failure: peer never received the request, so
                # retrying with a fresh grant is safe.
                last_exc = exc
                continue
            except (httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                # The request was sent but we never read a complete response.
                # The peer may have already processed it (delivered an email,
                # spawned a tool task, etc.). Retrying with a new nonce would
                # double-execute. Fail closed; the caller can decide whether
                # to re-issue.
                raise A2AClientError(
                    f"peer reply lost mid-flight (action may have executed): {exc!r}"
                ) from exc
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

    async def stream(
        self, *, method: str, params: dict, skill: str | None = None,
    ) -> AsyncIterator[dict]:
        """SSE stream. Yields parsed event dicts in chronological order.

        On truncation (connection drop mid-frame, unexpected EOF), yields a
        final ``{"type": "error", "message": "stream truncated after N events"}``
        instead of raising — spec §5 says do not crash the calling tool.
        """
        body, grant = self._build_envelope(method, params, skill or method)
        events_seen = 0
        try:
            async with self._client() as c:
                async with c.stream(
                    "POST",
                    f"{self._peer.url}/a2a/stream",
                    json=body,
                    headers={
                        "Authorization": f"A2A-HMAC {grant}",
                        "Accept": "text/event-stream",
                    },
                ) as r:
                    if r.status_code != 200:
                        text = await r.aread()
                        yield {
                            "type": "error",
                            "message": f"HTTP {r.status_code}: {text.decode(errors='replace')}",
                        }
                        return
                    buffer = ""
                    async for chunk in r.aiter_text():
                        buffer += chunk
                        # Split on the SSE frame terminator (blank line = \n\n).
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            event = _parse_sse_frame(frame)
                            if event is not None:
                                events_seen += 1
                                yield event
                    # Anything left in the buffer after the response ended is
                    # an incomplete frame — yield a truncation marker.
                    if buffer.strip():
                        yield {
                            "type": "error",
                            "message": f"stream truncated after {events_seen} events",
                        }
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            # TransportError is the common base of ConnectError, ReadError AND
            # RemoteProtocolError (incomplete chunked read when the peer drops
            # mid-stream). Spec §5: never crash the calling tool — surface a
            # final error event instead of propagating.
            yield {
                "type": "error",
                "message": f"stream transport error after {events_seen} events: {exc}",
            }


def _parse_sse_frame(frame: str) -> dict | None:
    """Parse a single SSE frame. Returns None for comments / non-data frames."""
    for line in frame.splitlines():
        line = line.rstrip("\r")
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].lstrip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return {"type": "error", "message": f"bad SSE JSON: {payload[:80]}"}
    return None


from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse, StreamingResponse


SkillDispatcher = Callable[[str, dict, dict], Awaitable[dict]]
StreamDispatcher = Callable[[str, dict, dict], AsyncIterator[dict]]


# A2A JSON-RPC wire method → advertised skill id. Per spec §6.1 the HMAC grant
# binds to the *skill id* (task.delegate / chat.message / status.query), not the
# raw method name, so the verifier must resolve the method to its skill before
# checking the grant. Unknown methods map to themselves so plain RPC methods
# (e.g. "ping" in tests) still verify against a same-named grant.
_METHOD_TO_SKILL = {
    "message/stream": "task.delegate",
    "message/send": "chat.message",
    "status/query": "status.query",
}


def _skill_for_method(method: str) -> str:
    return _METHOD_TO_SKILL.get(method, method)


def build_app(
    *,
    self_card: dict,
    hmac_secret: str,
    my_peer_id: str,
    skill_dispatcher: SkillDispatcher,
    stream_dispatcher: StreamDispatcher,
    nonce_cache: NonceCache | None = None,
) -> FastAPI:
    """Build the public-facing FastAPI app for our A2A endpoints.

    INBOUND AUTH MODEL — important when exposing this to more than one peer:

    ``hmac_secret`` is a SINGLE shared secret used to verify every inbound
    grant. The grant's ``peer_id`` is self-asserted by the caller and signed
    with this same secret, so verification proves "the caller knows our inbound
    secret", NOT "the caller is specifically peer X". The ``target_peer_id``
    anti-forward check only proves the grant was minted for *us*.

    Consequence: if you share ``hmac_secret`` with multiple inbound peers, any
    of them can impersonate another (sign with ``peer_id`` = someone else), and
    a dispatcher-level ``allowed_peer`` filter buys nothing. For per-peer
    isolation, provision a DISTINCT inbound secret per relationship (run one
    app/secret per peer) rather than a shared one. The 1:1 case
    (agent-last ↔ a single Hermes/OpenClaw) is safe with one secret.
    """
    app = FastAPI()
    cache = nonce_cache or NonceCache()

    @app.get("/.well-known/agent.json")
    async def get_card() -> dict:
        return self_card

    def _extract_grant(body: dict, headers) -> str:
        """Spec §6.1 double-write: header OR body param _meta."""
        h = headers.get("authorization", "")
        if h.startswith("A2A-HMAC "):
            return h[len("A2A-HMAC "):]
        meta = (body.get("params") or {}).get("_meta") or {}
        return meta.get("authz_grant", "")

    async def _authenticate(body: dict, headers, skill: str) -> dict:
        grant = _extract_grant(body, headers)
        if not grant:
            raise HTTPException(401, detail="missing authz_grant")
        try:
            claims = verify_cross_machine_grant(
                grant, key=hmac_secret,
                my_peer_id=my_peer_id, requested_skill=skill,
            )
        except AuthzError as exc:
            raise HTTPException(401, detail=str(exc)) from exc
        if not cache.check_and_remember(claims.get("nonce", "")):
            raise HTTPException(401, detail="replay detected")
        return claims

    @app.post("/a2a")
    async def post_a2a(req: Request) -> JSONResponse:
        body = await req.json()
        method = body.get("method", "")
        params = body.get("params") or {}
        claims = await _authenticate(body, req.headers, _skill_for_method(method))
        result = await skill_dispatcher(method, params, claims)
        return JSONResponse({
            "jsonrpc": "2.0", "id": body.get("id"), "result": result,
        })

    @app.post("/a2a/stream")
    async def post_stream(req: Request) -> StreamingResponse:
        body = await req.json()
        method = body.get("method", "")
        params = body.get("params") or {}
        claims = await _authenticate(body, req.headers, _skill_for_method(method))

        async def gen() -> AsyncIterator[bytes]:
            async for event in stream_dispatcher(method, params, claims):
                # Compact separators (no spaces) keep SSE frames small and match
                # the wire format the client-side parser tests assert against.
                payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                line = "data: " + payload + "\n\n"
                yield line.encode("utf-8")

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
