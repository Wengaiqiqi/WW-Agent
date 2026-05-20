"""Feishu / Lark webhook adapter.

Receives Open Platform v2 events via HTTP webhook, dispatches each user
message to the orchestrator via :func:`gateway.runner.run_turn`, and replies
through the Feishu REST API (`im/v1/messages/{id}/reply`).

Configuration (env vars):
    FEISHU_APP_ID            (required) — bot app id
    FEISHU_APP_SECRET        (required) — bot app secret
    FEISHU_VERIFY_TOKEN      (required) — event verification token
    FEISHU_ENCRYPT_KEY       (optional) — if the app is configured with
                              "encrypt mode", this AES-256 key decrypts each
                              event body
    FEISHU_DOMAIN            (optional) — ``open.feishu.cn`` (default) or
                              ``open.larksuite.com`` for the international
                              tenant
    FEISHU_REPLY_IN_THREAD   (optional) — "1" to reply in the same thread
                              when the trigger was a thread message

Set up in the Feishu developer console:
    1. Enable "Receive event" / "事件订阅" and point it at
       ``https://<your-host>/feishu/webhook``.
    2. Subscribe to ``im.message.receive_v1``.
    3. Grant the bot ``im:message`` and ``im:message:send_as_bot`` scopes.

The adapter handles ``url_verification`` (challenge response) automatically.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, Response

from gateway.runner import run_turn

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


from gateway._feishu_common import coerce_common


def _coerce(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalise an explicit cfg dict — fill defaults, coerce types."""
    cfg = dict(cfg or {})
    out: Dict[str, Any] = {
        **coerce_common(cfg),
        "verify_token": str(cfg.get("verify_token") or "").strip(),
        "encrypt_key": str(cfg.get("encrypt_key") or "").strip(),
        "reply_in_thread": bool(cfg.get("reply_in_thread", False)),
    }
    missing = [k for k in ("app_id", "app_secret", "verify_token") if not out[k]]
    if missing:
        raise RuntimeError(
            "Feishu gateway config missing fields: " + ", ".join(missing)
        )
    return out


def _settings_from_env() -> Dict[str, Any]:
    return _coerce(
        {
            "app_id": os.environ.get("FEISHU_APP_ID"),
            "app_secret": os.environ.get("FEISHU_APP_SECRET"),
            "verify_token": os.environ.get("FEISHU_VERIFY_TOKEN"),
            "encrypt_key": os.environ.get("FEISHU_ENCRYPT_KEY"),
            "domain": os.environ.get("FEISHU_DOMAIN"),
            "reply_in_thread": os.environ.get("FEISHU_REPLY_IN_THREAD", "").strip()
            == "1",
        }
    )


# ---------------------------------------------------------------------------
# Encrypted-event decryption (Open Platform "encrypt mode")
# ---------------------------------------------------------------------------


def _decrypt_event(encrypted_b64: str, encrypt_key: str) -> Dict[str, Any]:
    """Decrypt an ``encrypt`` envelope using AES-256-CBC.

    Feishu derives the AES key as ``SHA256(encrypt_key)`` and prepends a
    16-byte IV to the ciphertext, then base64-encodes the whole thing.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = base64.b64decode(encrypted_b64)
    iv, ciphertext = raw[:16], raw[16:]
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = cipher.update(ciphertext) + cipher.finalize()
    pad = padded[-1]
    plaintext = padded[:-pad].decode("utf-8")
    return json.loads(plaintext)


# ---------------------------------------------------------------------------
# Tenant-access-token cache
# ---------------------------------------------------------------------------


class _TokenCache:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, client: httpx.AsyncClient, cfg: Dict[str, str]) -> str:
        async with self._lock:
            if self._token and self._expires_at - time.time() > 60:
                return self._token
            resp = await client.post(
                f"https://{cfg['domain']}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Feishu token error: {data}")
            self._token = data["tenant_access_token"]
            self._expires_at = time.time() + int(data.get("expire", 7200))
            return self._token


_TOKEN_CACHE = _TokenCache()


# Webhook adapter delegates text/mention/session helpers to
# :mod:`gateway._feishu_common` so it stays in sync with the WS adapter.


# ---------------------------------------------------------------------------
# Outbound reply
# ---------------------------------------------------------------------------


async def _send_reply(
    client: httpx.AsyncClient,
    cfg: Dict[str, str],
    *,
    message_id: str,
    text: str,
) -> None:
    from gateway._feishu_common import truncate_reply

    token = await _TOKEN_CACHE.get(client, cfg)
    body = {
        "content": json.dumps({"text": truncate_reply(text)}, ensure_ascii=False),
        "msg_type": "text",
        "reply_in_thread": cfg["reply_in_thread"],
    }
    resp = await client.post(
        f"https://{cfg['domain']}/open-apis/im/v1/messages/{message_id}/reply",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
        timeout=30.0,
    )
    if resp.status_code >= 300:
        log.error("feishu reply failed: %s %s", resp.status_code, resp.text)
        return
    data = resp.json()
    if data.get("code") != 0:
        log.error("feishu reply api error: %s", data)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


def build_app(cfg: Optional[Dict[str, Any]] = None) -> FastAPI:
    cfg = _coerce(cfg) if cfg is not None else _settings_from_env()
    app = FastAPI(title="agent-feishu-gateway")
    state = {"bot_open_id": ""}

    @app.on_event("startup")
    async def _hydrate_bot_identity() -> None:
        app.state.http = httpx.AsyncClient()
        try:
            token = await _TOKEN_CACHE.get(app.state.http, cfg)
            resp = await app.state.http.get(
                f"https://{cfg['domain']}/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            data = resp.json()
            state["bot_open_id"] = (data.get("bot") or {}).get("open_id", "")
            log.info("feishu gateway started: bot_open_id=%s", state["bot_open_id"])
        except Exception as exc:  # noqa: BLE001 - identity is optional
            log.warning("feishu bot identity probe failed: %s", exc)
        if not state["bot_open_id"]:
            log.warning(
                "feishu webhook: bot open_id unknown -- group @ filter will "
                "fall back to 'any mention present' (may over-trigger when "
                "the bot is mentioned alongside other users)."
            )

    @app.on_event("shutdown")
    async def _close() -> None:
        await app.state.http.aclose()

    @app.post("/feishu/webhook")
    async def webhook(request: Request) -> Response:
        raw = await request.body()
        try:
            envelope = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return Response(status_code=400, content="bad json")

        if cfg["encrypt_key"] and "encrypt" in envelope:
            try:
                envelope = _decrypt_event(envelope["encrypt"], cfg["encrypt_key"])
            except Exception as exc:  # noqa: BLE001
                log.error("feishu decrypt failed: %s", exc)
                return Response(status_code=400, content="decrypt failed")

        # ``url_verification`` is the URL-challenge handshake. It can arrive
        # at top level (v1) or under "header.event_type" (v2). Either way the
        # response is the same: echo back the challenge.
        if envelope.get("type") == "url_verification":
            if envelope.get("token") != cfg["verify_token"]:
                return Response(status_code=403, content="bad token")
            return Response(
                content=json.dumps({"challenge": envelope.get("challenge", "")}),
                media_type="application/json",
            )

        header = envelope.get("header") or {}
        if header.get("token") and header["token"] != cfg["verify_token"]:
            return Response(status_code=403, content="bad token")

        event_type = header.get("event_type") or envelope.get("event", {}).get("type")
        event = envelope.get("event") or {}

        if event_type == "im.message.receive_v1":
            asyncio.create_task(_handle_message(app, cfg, event, state["bot_open_id"]))

        # Feishu requires a 2xx within 3s to mark the event delivered.
        return Response(status_code=200, content="{}", media_type="application/json")

    return app


async def _handle_message(
    app: FastAPI, cfg: Dict[str, str], event: Dict[str, Any], bot_open_id: str
) -> None:
    from gateway._feishu_common import (
        bot_was_mentioned,
        extract_sender_open_id,
        extract_text_from_dict,
        session_key_for,
        strip_mentions,
    )

    message = event.get("message") or {}
    sender = event.get("sender") or {}
    if (sender.get("sender_type") or "").lower() == "app":
        return  # ignore bot-originated messages — prevents reply loops

    chat_type = message.get("chat_type", "")
    mentions = message.get("mentions") or []
    text = extract_text_from_dict(message)
    if not text:
        return

    # Group filter: require explicit bot @-mention. The helper falls back to
    # "any mention present" when bot_open_id is empty (probe failed) so we
    # don't accidentally respond to every group message in that mode.
    if chat_type == "group":
        if not bot_was_mentioned(mentions, bot_open_id=bot_open_id):
            return
        text = strip_mentions(text, mentions)
        if not text:
            return

    message_id = message.get("message_id") or ""
    if not message_id:
        return

    chat_id = message.get("chat_id") or ""
    session_key = session_key_for(chat_id)
    memory_user_id = extract_sender_open_id(sender)

    try:
        reply = await run_turn(
            text,
            trace_id=f"feishu-{message_id[:8]}",
            session_key=session_key,
            user_id=memory_user_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("orchestrator turn failed")
        reply = f"[error] {exc}"

    if not reply:
        reply = "(no response)"

    try:
        await _send_reply(app.state.http, cfg, message_id=message_id, text=reply)
    except Exception:  # noqa: BLE001
        log.exception("feishu send failed")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def serve(
    host: str = "0.0.0.0",
    port: int = 8765,
    *,
    cfg: Optional[Dict[str, Any]] = None,
) -> None:
    import uvicorn

    uvicorn.run(build_app(cfg), host=host, port=port, log_level="info")


if __name__ == "__main__":
    serve()
