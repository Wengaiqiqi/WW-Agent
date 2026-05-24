"""Translate comm-agent A2A skills into ACP calls on a HermesACPClient.

build_app passes the *raw method* to dispatchers:
  message/stream -> stream_dispatcher (task.delegate)
  message/send   -> skill_dispatcher  (chat.message)
  status/query   -> skill_dispatcher  (status.query)
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from bridge.hermes_a2a.acp_client import ACPError


def _caller_ok(claims: dict, allowed_peer: str | None) -> bool:
    return allowed_peer is None or claims.get("peer_id") == allowed_peer


def _text_of(params: dict) -> str:
    parts = (params.get("message") or {}).get("parts") or []
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            return p["text"]
    return ""


def make_dispatchers(acp, *, allowed_peer: str | None = None):
    """Return (skill_dispatcher, stream_dispatcher) bound to an ACP client."""

    async def stream_dispatcher(method: str, params: dict, claims: dict) -> AsyncIterator[dict]:
        if not _caller_ok(claims, allowed_peer):
            yield {"type": "task", "state": "failed", "error": "caller peer not allowed"}
            return
        text = _text_of(params)
        if not text:
            yield {"type": "task", "state": "failed", "error": "empty task"}
            return
        yield {"type": "task", "state": "working", "message": "delegating to hermes"}
        try:
            session_id = await acp.ensure_session(params.get("context_id"))
        except ACPError as exc:
            yield {"type": "task", "state": "failed", "error": f"hermes acp unavailable: {exc}"}
            return
        async for ev in acp.run_prompt(session_id, text):
            yield ev

    async def skill_dispatcher(method: str, params: dict, claims: dict) -> dict:
        if not _caller_ok(claims, allowed_peer):
            return {"error": "caller peer not allowed"}
        if method == "status/query":
            return acp.status()
        if method == "message/send":
            text = _text_of(params)
            if not text:
                return {"error": "empty message"}
            try:
                session_id = await acp.ensure_session(params.get("context_id"))
                reply = await acp.prompt_collect(session_id, text)
            except ACPError as exc:
                return {"error": f"hermes acp: {exc}"}
            return {"reply": reply, "context_id": session_id}
        return {"error": f"unsupported method {method!r}"}

    return skill_dispatcher, stream_dispatcher
