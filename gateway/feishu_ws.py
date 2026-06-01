"""Feishu / Lark long-connection adapter (WebSocket gateway mode).

Uses the official ``lark-oapi`` SDK to open a persistent WebSocket to Feishu
Open Platform's event gateway. No public webhook URL required, which makes
this the recommended development mode -- you can run the bot from a laptop
behind NAT without any tunneling.

The SDK handles auth, decryption, reconnection, and event dispatch
internally; this module is just the glue from a received message to
``gateway.runner.run_turn`` and back through the IM reply API.

Configuration (env vars or gateways.json):
    FEISHU_APP_ID      (required) -- bot app id
    FEISHU_APP_SECRET  (required) -- bot app secret
    FEISHU_DOMAIN      (optional) -- ``open.feishu.cn`` (default) or
                        ``open.larksuite.com`` for the international tenant

NOTE: the WS mode does NOT need ``verify_token`` or ``encrypt_key`` -- the
SDK negotiates auth with the bot's credentials directly. Existing webhook
configs in ``gateways.json`` keep those fields but the WS adapter ignores
them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

from gateway._feishu_common import (
    bot_was_mentioned as _bot_was_mentioned_common,
    coerce_common,
    extract_sender_open_id,
    extract_text_from_obj,
    session_key_for,
    strip_mentions,
    truncate_reply,
)
from gateway.runner import run_turn

log = logging.getLogger(__name__)


# Dedup: Feishu's WS server re-pushes events when it does not see an ACK
# from us within a few seconds. Our handler used to block until the LLM
# returned (8+ seconds), which guaranteed redelivery. We now run the LLM +
# reply in a fire-and-forget worker thread so the handler returns instantly
# AND we ignore duplicate ``message_id`` arrivals as belt-and-suspenders.
_DEDUP_TTL = 86400.0  # 24h — Feishu re-pushes un-ACKed events for ~30min in
# the wild; message_ids never get reused, so keeping them in memory for a
# whole day is essentially free and stops "yesterday's DM kept replying"
# class of symptoms.
_seen_msg_ids: dict[str, float] = {}
_seen_msg_ids_lock = threading.Lock()
# Serialise actual LLM dispatch across worker threads so concurrent inbound
# messages do not stomp on the orchestrator's shared ``.agent/runtime/``
# state. Per-message latency is dominated by the LLM round-trip, so a single
# queue is fine for chat-bot throughput.
#
# Why this lock AND ``runner._CONCURRENCY_GUARD`` (asyncio.Lock):
# * ``_dispatch_lock`` (threading) serialises across the SDK's worker
#   threads. Each thread does its own ``asyncio.run`` -> its own event loop,
#   so an asyncio.Lock alone would not span them.
# * ``_CONCURRENCY_GUARD`` (asyncio) covers the in-loop callers (the REPL
#   path / standalone QQ where dispatch is async-native).
# Together they ensure exactly one ``run_turn`` is active per process.
_dispatch_lock = threading.Lock()


def _is_duplicate_message(msg_id: str) -> bool:
    now = time.time()
    with _seen_msg_ids_lock:
        # Cheap O(n) cleanup; n is bounded by message rate * _DEDUP_TTL (24h).
        for k, ts in list(_seen_msg_ids.items()):
            if now - ts > _DEDUP_TTL:
                del _seen_msg_ids[k]
        if msg_id in _seen_msg_ids:
            return True
        _seen_msg_ids[msg_id] = now
        return False

def _coerce(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = coerce_common(cfg or {})
    missing = [k for k in ("app_id", "app_secret") if not out[k]]
    if missing:
        raise RuntimeError(
            "Feishu WS gateway config missing fields: " + ", ".join(missing)
        )
    return out


def _settings_from_env() -> Dict[str, Any]:
    return _coerce(
        {
            "app_id": os.environ.get("FEISHU_APP_ID"),
            "app_secret": os.environ.get("FEISHU_APP_SECRET"),
            "domain": os.environ.get("FEISHU_DOMAIN"),
        }
    )


# Text extraction + mention stripping share their bodies with the webhook
# adapter; the shims below just point at the common module so this file
# can keep using its local names.
_extract_text = extract_text_from_obj
_strip_all_mentions = strip_mentions


def _build_client(cfg: Dict[str, Any]):
    """Return a fresh lark-oapi Client. Cheap — re-use across reaction +
    reply calls within a single message-handling thread."""
    import lark_oapi as lark

    domain = cfg["domain"]
    if not domain.startswith("http"):
        domain = "https://" + domain
    return (
        lark.Client.builder()
        .app_id(cfg["app_id"])
        .app_secret(cfg["app_secret"])
        .domain(domain)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )


# Feishu emoji_type for the "bot is working" badge. `Typing` renders as a
# typing-indicator pill on the user message; matches hermes's UX. Override
# with FEISHU_PROCESSING_REACTION env var if you want a thumbs up etc.
_PROCESSING_REACTION = os.environ.get("FEISHU_PROCESSING_REACTION", "Typing")


# Cached bot identity (open_id), fetched once on startup. Used in group
# chats to tell apart "user @-mentioned this bot" from "user @-mentioned
# someone else" / "user @-mentioned nobody". Empty when the startup probe
# failed; the group filter then falls back to "any mention exists" which
# is what hermes does as a defence-in-depth.
_BOT_OPEN_ID: str = ""


def _fetch_bot_open_id(cfg: Dict[str, Any]) -> str:
    """Resolve the bot's own open_id via the v3 ``bot/info`` endpoint.

    The SDK's ws event payload has no ``event.bot`` field, and the parsed
    ``Mention`` class strips the ``mentioned_type`` from the raw JSON, so we
    cannot tell a bot-mention from a user-mention without knowing our own
    id. Fetch once on startup and cache.
    """
    import httpx

    domain = cfg["domain"]
    if not domain.startswith("http"):
        domain = "https://" + domain
    try:
        # Token: tenant_access_token is the right one for bot/info.
        token_resp = httpx.post(
            f"{domain}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]},
            timeout=10.0,
        )
        token = (token_resp.json() or {}).get("tenant_access_token", "")
        if not token:
            log.warning("feishu: bot identity probe -- no tenant token")
            return ""
        info_resp = httpx.get(
            f"{domain}/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        data = info_resp.json() or {}
        if data.get("code") != 0:
            log.warning("feishu: bot identity probe error: %s", data)
            return ""
        return ((data.get("bot") or {}).get("open_id") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("feishu: bot identity probe raised: %s", exc)
        return ""


def _bot_was_mentioned(mentions: list) -> bool:
    """Thin wrapper around the common helper that pulls the cached open_id."""
    return _bot_was_mentioned_common(mentions, bot_open_id=_BOT_OPEN_ID)


def _add_reaction(cfg: Dict[str, Any], message_id: str, emoji_type: str) -> str:
    """Add a reaction to a user message, returning ``reaction_id`` for later
    deletion. Returns empty string on failure (we log and keep going --
    the reaction is a UX nicety, never load-bearing).
    """
    try:
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
        )

        client = _build_client(cfg)
        body = (
            CreateMessageReactionRequestBody.builder()
            .reaction_type({"emoji_type": emoji_type})
            .build()
        )
        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        resp = client.im.v1.message_reaction.create(req)
        if resp.success():
            data = getattr(resp, "data", None)
            return getattr(data, "reaction_id", "") or ""
        log.debug(
            "feishu: add reaction %s on %s rejected: code=%s msg=%s",
            emoji_type, message_id,
            getattr(resp, "code", None), getattr(resp, "msg", None),
        )
    except Exception:  # noqa: BLE001
        log.debug("feishu: add reaction raised", exc_info=True)
    return ""


def _remove_reaction(cfg: Dict[str, Any], message_id: str, reaction_id: str) -> None:
    """Best-effort reaction cleanup. Silent on failure."""
    if not reaction_id:
        return
    try:
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        client = _build_client(cfg)
        req = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        client.im.v1.message_reaction.delete(req)
    except Exception:  # noqa: BLE001
        log.debug("feishu: remove reaction raised", exc_info=True)


def _send_reply(cfg: Dict[str, Any], message_id: str, text: str) -> None:
    """Reply to a message via the v1 IM reply endpoint (sync SDK call)."""
    from lark_oapi.api.im.v1 import (
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )

    client = _build_client(cfg)
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": truncate_reply(text)}, ensure_ascii=False))
            .msg_type("text")
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.reply(req)
    if not resp.success():
        log.error(
            "feishu reply failed: code=%s msg=%s log_id=%s",
            getattr(resp, "code", "?"),
            getattr(resp, "msg", "?"),
            getattr(resp, "get_log_id", lambda: "?")(),
        )


def _handle_message(cfg: Dict[str, Any], event: Any) -> None:
    """SDK callback. Runs in a worker thread spawned by lark-oapi."""
    try:
        ev = event.event
        message = ev.message
        sender = ev.sender
        # Ignore messages the bot itself emitted (prevents reply loops).
        if getattr(sender, "sender_type", "") == "app":
            return

        chat_type = getattr(message, "chat_type", "")
        mentions = getattr(message, "mentions", []) or []

        # In groups, only respond when the bot itself was @-mentioned.
        # We DON'T trust "we got a group event" to imply that anymore --
        # depending on the app's permission scope, Feishu may forward all
        # group messages to bots that hold the broader 'im:message:receive'
        # right, so we re-check explicitly. Filter BEFORE extracting text
        # so non-@ group chatter doesn't pay the JSON-parse cost.
        if chat_type == "group":
            if not _bot_was_mentioned(mentions):
                log.info(
                    "feishu: skipping group msg (bot not @-mentioned); "
                    "mentions=%d", len(mentions),
                )
                return

        text = _extract_text(message)
        if not text:
            return

        if chat_type == "group":
            text = _strip_all_mentions(text, mentions)
            if not text:
                log.info("feishu: skipping (text empty after strip)")
                return

        message_id = getattr(message, "message_id", "") or ""
        if not message_id:
            log.info("feishu: skipping (no message_id)")
            return

        if _is_duplicate_message(message_id):
            log.info("feishu: duplicate id=%s ignored", message_id)
            return

        chat_id = getattr(message, "chat_id", "") or "?"
        chat_type = getattr(message, "chat_type", "") or "?"
        # Memory key is per-chat: a group's members share one conversation
        # history (mimicking how humans remember "a thread of chat in this
        # group"), and DMs are naturally one chat_id per user.
        session_key = session_key_for(chat_id)
        # Long-term memory ("remember my name is X") is per-USER instead:
        # in a group with multiple humans, each person's facts stay private
        # to their own ``memories/users/<hash>/`` directory.
        memory_user_id = extract_sender_open_id(sender)

        log.info(
            "feishu: received %s id=%s chat_type=%s chat_id=%s sender=%s content=%r",
            getattr(message, "message_type", ""),
            message_id,
            chat_type,
            chat_id,
            memory_user_id,
            text[:200],
        )

        # CRITICAL: return from this callback fast. lark-oapi invokes us on
        # its WS event-loop thread; if we block here for the LLM round-trip,
        # the SDK can't send ACK / heartbeats and Feishu's server re-delivers
        # the same event after ~10s. Spawn a worker thread, return.
        threading.Thread(
            target=_run_dispatch_in_background,
            args=(cfg, message_id, text, session_key, memory_user_id),
            name=f"feishu-dispatch-{message_id[:8]}",
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001
        log.exception("feishu: handler crashed")


def _run_dispatch_in_background(
    cfg: Dict[str, Any],
    message_id: str,
    text: str,
    session_key: str,
    memory_user_id: str,
) -> None:
    """Worker-thread body: react "got it" -> run the orchestrator turn ->
    send the reply -> remove the reaction.

    Serialised across concurrent inbound messages via ``_dispatch_lock`` so
    we never spawn two parallel orchestrators on the shared MCP runtime.
    The reaction lifecycle runs OUTSIDE the lock so the "received" badge
    appears immediately even when the bot is still busy on a prior turn.
    """
    # Fire-and-forget the "I'm working on it" reaction. ~200ms latency to
    # Feishu so the user sees the badge within a second of their message.
    reaction_id = _add_reaction(cfg, message_id, _PROCESSING_REACTION)
    if reaction_id:
        log.info("feishu: reaction %s -> %s on id=%s",
                 _PROCESSING_REACTION, reaction_id, message_id)

    with _dispatch_lock:
        try:
            reply = asyncio.run(
                run_turn(
                    text,
                    trace_id=f"feishu-{message_id[:8]}",
                    session_key=session_key,
                    user_id=memory_user_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("feishu: orchestrator turn failed")
            reply = f"[error] {exc}"
    if not reply:
        reply = "(no response)"
    log.info("feishu: replying to id=%s (%d chars)", message_id, len(reply))
    try:
        _send_reply(cfg, message_id, reply)
        log.info("feishu: reply sent for id=%s", message_id)
    except Exception:  # noqa: BLE001
        log.exception("feishu: send failed")
    finally:
        # Always try to clean up the processing badge -- otherwise it
        # stays on the user message forever, which looks like the bot
        # is still working.
        if reaction_id:
            _remove_reaction(cfg, message_id, reaction_id)


def _ws_endpoint(domain: str) -> Optional[str]:
    """Return ``None`` to let lark-oapi pick its default endpoint."""
    # lark-oapi's WS client routes by domain via the SDK config; we just pass
    # the domain to Client.builder(). No manual ws URL needed for v0.4+.
    return None


def serve(cfg: Optional[Dict[str, Any]] = None) -> None:
    """Standalone entry: connect to Feishu WS and block on event dispatch."""
    # Avoid the Windows + ProactorEventLoop trap (see gateway/__main__.py
    # comment). The lark-oapi SDK creates its own event loop internally, and
    # under Proactor the asyncio.run() inside _handle_message would race with
    # the SDK loop in unpredictable ways.
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:  # noqa: BLE001
            pass

    resolved = _coerce(cfg) if cfg is not None else _settings_from_env()

    try:
        import lark_oapi as lark
    except ImportError as exc:
        raise RuntimeError(
            "Feishu WS mode requires lark-oapi. Install with:  "
            "pip install lark-oapi"
        ) from exc

    from gateway._pidlock import acquire, release, AlreadyRunning

    try:
        acquire("feishu")
    except AlreadyRunning as exc:
        # Don't traceback — this is a user error, give them the actionable
        # message directly and exit non-zero so scripts can detect it.
        log.error("%s", exc)
        sys.exit(2)

    # Cache the bot's own open_id so the group ``@`` filter is exact.
    global _BOT_OPEN_ID
    _BOT_OPEN_ID = _fetch_bot_open_id(resolved)
    if _BOT_OPEN_ID:
        log.info("feishu: bot open_id resolved = %s", _BOT_OPEN_ID)
    else:
        log.warning(
            "feishu: bot open_id probe failed -- group @ filter will fall "
            "back to 'any mention present', which can over-trigger when "
            "the message mentions other users alongside the bot."
        )

    def _on_message(event):
        _handle_message(resolved, event)

    def _on_chat_entered(event):
        # Fires every time a user opens the bot's P2P chat window (before
        # they send anything). We don't need to react -- but registering a
        # no-op handler silences the SDK's "processor not found" ERROR
        # line in gateway.log, which otherwise looks alarming to anyone
        # tailing the log.
        return

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message)
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(_on_chat_entered)
        .build()
    )

    log.info(
        "feishu ws: starting (app_id=%s domain=%s)",
        resolved["app_id"],
        resolved["domain"],
    )
    # SDK log level. INFO is enough for "connected / received message" type
    # visibility; set FEISHU_WS_DEBUG=1 to dump every inbound ws frame
    # (useful for debugging "why didn't my handler fire" but extremely
    # noisy in normal use).
    sdk_log_level = (
        lark.LogLevel.DEBUG
        if os.environ.get("FEISHU_WS_DEBUG", "0") == "1"
        else lark.LogLevel.INFO
    )
    ws_client = lark.ws.Client(
        resolved["app_id"],
        resolved["app_secret"],
        event_handler=handler,
        log_level=sdk_log_level,
    )
    # ``start()`` blocks until the WS closes / process exits. Reconnect is
    # handled by the SDK.
    try:
        ws_client.start()
    finally:
        release("feishu")


if __name__ == "__main__":
    from gateway._constants import LOG_FORMAT

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    serve()
