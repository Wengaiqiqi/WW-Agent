"""QQ Official Bot adapter (WebSocket gateway mode).

Authenticates with the QQ Open Platform's official bot API v2, opens a
WebSocket to ``wss://api.sgroup.qq.com/websockets``, identifies with the
appropriate intents, and dispatches each ``@bot`` message to the
orchestrator via :func:`gateway.runner.run_turn`. Replies go back through
the v2 REST API:

    - GROUP_AT_MESSAGE_CREATE → POST /v2/groups/{group_openid}/messages
    - C2C_MESSAGE_CREATE      → POST /v2/users/{user_openid}/messages
    - AT_MESSAGE_CREATE       → POST /channels/{channel_id}/messages

Configuration (env vars):
    QQ_APP_ID           (required) — bot app id
    QQ_CLIENT_SECRET    (required) — bot client secret
    QQ_INTENTS          (optional) — override intents bitmask; defaults to
                         C2C_GROUP_AT_MESSAGES (1<<25) |
                         PUBLIC_GUILD_MESSAGES (1<<30)
    QQ_SANDBOX          (optional) — "1" to use the sandbox API host

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, Optional

import httpx

from gateway.runner import run_turn


# Compiled once at module load instead of in ``_strip_at_mentions`` per call.
# QQ guild messages embed bot/user mentions as ``<@!12345>``; we strip them
# so the orchestrator sees a clean prompt.
_AT_MENTION_RE = re.compile(r"<@!?\d+>\s*")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROD_API = "https://api.sgroup.qq.com"
_SANDBOX_API = "https://sandbox.api.sgroup.qq.com"
_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

# Default intents:  C2C_GROUP_AT_MESSAGES | PUBLIC_GUILD_MESSAGES
# Add DIRECT_MESSAGE (1<<12) if your bot uses guild DMs.
_DEFAULT_INTENTS = (1 << 25) | (1 << 30)


def _coerce(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalise an explicit cfg dict — fill defaults, coerce types."""
    cfg = dict(cfg or {})
    sandbox = cfg.get("sandbox")
    if sandbox is None:
        sandbox = os.environ.get("QQ_SANDBOX", "").strip() == "1"
    intents = cfg.get("intents")
    if intents in (None, ""):
        intents = _DEFAULT_INTENTS
    out: Dict[str, Any] = {
        "app_id": str(cfg.get("app_id") or "").strip(),
        "client_secret": str(cfg.get("client_secret") or "").strip(),
        "intents": int(intents),
        "sandbox": bool(sandbox),
        "api_base": _SANDBOX_API if bool(sandbox) else _PROD_API,
    }
    missing = [k for k in ("app_id", "client_secret") if not out[k]]
    if missing:
        raise RuntimeError(
            "QQ gateway config missing fields: " + ", ".join(missing)
        )
    return out


def _settings_from_env() -> Dict[str, Any]:
    return _coerce(
        {
            "app_id": os.environ.get("QQ_APP_ID"),
            "client_secret": os.environ.get("QQ_CLIENT_SECRET"),
            "intents": os.environ.get("QQ_INTENTS"),
            "sandbox": os.environ.get("QQ_SANDBOX", "").strip() == "1",
        }
    )


# ---------------------------------------------------------------------------
# Access token (Bearer used for both REST and WS handshake)
# ---------------------------------------------------------------------------


class _TokenCache:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._issued_at: float = 0.0
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, cfg: Dict[str, Any]) -> str:
        async with self._lock:
            if self._token and self._expires_at - time.time() > 60:
                log.debug(
                    "qq: token cache hit, age=%.1fs",
                    time.time() - self._issued_at,
                )
                return self._token
            log.info("qq: token cache miss, fetching new")
            # Use the SAME sync-httpx-in-thread pattern as ``QQGateway._api``.
            # Token refresh runs once on startup AND again every ~2h while the
            # WebSocket is active; if we used async httpx here and the WS-vs-
            # httpx event-loop trap hit, ``async with self._lock`` would hold
            # the lock forever and every subsequent reply would deadlock.
            app_id = cfg["app_id"]
            client_secret = cfg["client_secret"]

            def _sync_fetch() -> Dict[str, Any]:
                with httpx.Client() as client:
                    response = client.post(
                        _TOKEN_URL,
                        json={"appId": app_id, "clientSecret": client_secret},
                        timeout=15.0,
                    )
                response.raise_for_status()
                return response.json()

            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(_sync_fetch), timeout=20.0,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError("QQ token fetch timed out (20s)") from exc

            token = data.get("access_token")
            if not token:
                raise RuntimeError(f"QQ token response missing access_token: {data}")
            self._token = token
            self._issued_at = time.time()
            self._expires_at = self._issued_at + int(data.get("expires_in", 7200))
            return token


# ---------------------------------------------------------------------------
# Gateway client
# ---------------------------------------------------------------------------


class QQGateway:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        # Accept either an already-coerced cfg (has ``api_base``) or a raw
        # cred dict from gateways.json. Normalising in one place keeps
        # callers (manager.py, serve(), tests) from having to know which is
        # which.
        self._cfg = cfg if "api_base" in cfg else _coerce(cfg)
        self._tokens = _TokenCache()
        # NOTE: do NOT keep a long-lived ``httpx.AsyncClient`` here. On
        # Windows + ProactorEventLoop, a shared client co-existing with an
        # active WebSocket gets into a state where outbound POSTs to the QQ
        # messaging endpoint hang forever (probe with a fresh client returns
        # in <0.5s against the same endpoint). Every ``_api`` call builds its
        # own short-lived client to sidestep that interaction.
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_interval: float = 30.0
        self._seen_msg_ids: dict[str, float] = {}
        self._ws = None  # type: ignore[var-annotated]
        # Cooperative stop signal -- ``threading.Event`` because the manager
        # sets it from the REPL's main thread while ``_loop`` runs inside a
        # worker thread (see ``manager.start_qq``). The inner loop polls
        # this on every iteration and a small watcher task closes the WS
        # when it fires, so the ``async for`` reading loop exits promptly
        # instead of waiting for the next inbound frame.
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        """Ask the gateway to shut down cooperatively.

        Cross-thread safe: callable from any thread. The internal loop will
        observe the flag on its next iteration AND a watcher task closes
        the WebSocket, which makes the read loop exit immediately rather
        than waiting up to ~30s for the next message frame.
        """
        self._stop_event.set()

    async def _stop_watcher(self, ws: Any) -> None:
        """Poll the stop event and close the WS when it fires.

        Lives one per WS connection. ``asyncio.sleep`` yields to other
        tasks so it doesn't starve the read loop. When the event is set,
        we close the WS, which makes ``async for raw in ws`` raise
        ``ConnectionClosed`` -- ``_read_loop`` propagates that and
        ``_loop`` checks ``_stop_event`` to decide whether to reconnect
        or return.
        """
        while not self._stop_event.is_set():
            await asyncio.sleep(0.2)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    # -- HTTP --------------------------------------------------------------

    async def _api(
        self, method: str, path: str, *, json_body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        token = await self._tokens.get(self._cfg)
        url = f"{self._cfg['api_base']}{path}"
        body_preview = json.dumps(json_body, ensure_ascii=False) if json_body else "<none>"
        log.info("qq api %s %s -> requesting body=%s", method, path, body_preview[:300])
        # Bulletproof HTTP path: raw ``threading.Thread`` (NOT
        # ``asyncio.to_thread``) + manual ``asyncio.sleep`` poll loop.
        # The previous ``asyncio.wait_for(asyncio.to_thread(...), 20)``
        # silently failed to time out in REPL mode -- whatever was on the
        # event loop (picker UI / WS reader) prevented the wait_for timer
        # from firing. asyncio.sleep(0.1) in a poll loop is a primitive
        # we can rely on across every loop type and policy.
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
        }
        result_box: Dict[str, Any] = {}

        def _worker() -> None:
            try:
                with httpx.Client() as client:
                    response = client.request(
                        method, url, headers=headers, json=json_body, timeout=15.0,
                    )
                result_box["packed"] = {
                    "status": response.status_code,
                    "json": _safe_json(response),
                    "text": response.text,
                }
            except Exception as exc:  # noqa: BLE001
                result_box["error"] = exc

        thread = threading.Thread(
            target=_worker, name=f"qq-api-{path[-20:]}", daemon=True,
        )
        thread.start()

        deadline = time.monotonic() + 20.0
        while thread.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        if thread.is_alive():
            log.error(
                "qq api %s %s -> HARD TIMEOUT after 20s (thread still alive)",
                method, path,
            )
            return {"code": -1, "message": "client-side timeout"}

        if "error" in result_box:
            log.error(
                "qq api %s %s -> request raised: %s",
                method, path, result_box["error"],
            )
            return {"code": -1, "message": f"request error: {result_box['error']}"}

        packed = result_box["packed"]
        status = packed["status"]
        data = packed["json"] if packed["json"] is not None else {"_raw": packed["text"]}
        if status >= 300:
            log.error("qq api %s %s -> %s %s", method, path, status, data)
        elif isinstance(data, dict) and data.get("code") not in (None, 0):
            # 2xx with an error code in body — common when reply token is
            # expired, msg_seq collides, or content fails moderation.
            log.warning("qq api %s %s -> %s body=%s", method, path, status, data)
        else:
            log.info("qq api %s %s -> %s ok", method, path, status)
        return data

    async def _get_gateway_url(self) -> str:
        data = await self._api("GET", "/gateway")
        url = data.get("url")
        if not url:
            raise RuntimeError(f"QQ gateway endpoint missing url: {data}")
        return url

    # -- Dedup -------------------------------------------------------------
    #
    # QQ dedup lives on the gateway instance (not module-level like Feishu)
    # because all QQ events arrive in a single asyncio task — the WS loop —
    # so no cross-thread state is involved. Feishu's lark-oapi SDK spawns
    # worker threads per event and needs the module-level dict + threading
    # lock to coordinate them. Same purpose, different ownership.

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        # Drop entries older than 5 minutes; cheap O(n) cleanup is fine since
        # the dict size is bounded by message rate × 5min.
        for k, ts in list(self._seen_msg_ids.items()):
            if now - ts > 300:
                del self._seen_msg_ids[k]
        if msg_id in self._seen_msg_ids:
            return True
        self._seen_msg_ids[msg_id] = now
        return False

    # -- Inbound dispatch --------------------------------------------------

    async def _handle_dispatch(self, event_type: str, d: Dict[str, Any]) -> None:
        msg_id = str(d.get("id") or "")
        if not msg_id:
            log.debug("qq: dispatch %s missing id, dropping", event_type)
            return
        if self._is_duplicate(msg_id):
            log.debug("qq: dispatch %s duplicate id=%s", event_type, msg_id)
            return

        content = str(d.get("content") or "").strip()
        # QQ's group/c2c messages already strip the @bot prefix server-side,
        # but guild messages keep it. Strip leftover ``<@!bot>`` mentions.
        content = _strip_at_mentions(content)
        if not content:
            log.info("qq: %s id=%s empty content, ignoring", event_type, msg_id)
            return

        # Derive the conversation memory key. QQ events don't have a single
        # "chat_id" field; the right id depends on event type:
        #   - GROUP_AT_MESSAGE_CREATE -> group_openid (all members of the
        #     group share a thread)
        #   - C2C_MESSAGE_CREATE -> author.user_openid (1:1)
        #   - AT_MESSAGE_CREATE -> channel_id
        chat_id = ""
        author = d.get("author") or {}
        # ``user_openid`` for C2C, ``member_openid`` for group/channel chats.
        if isinstance(author, dict):
            memory_user_id = str(
                author.get("user_openid")
                or author.get("member_openid")
                or author.get("id")
                or ""
            ).strip()
        else:
            memory_user_id = ""
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            chat_id = (d.get("group_openid") or "").strip()
        elif event_type == "C2C_MESSAGE_CREATE":
            chat_id = ((d.get("author") or {}).get("user_openid") or "").strip()
        elif event_type in {"AT_MESSAGE_CREATE", "GUILD_AT_MESSAGE_CREATE", "MESSAGE_CREATE"}:
            chat_id = (d.get("channel_id") or "").strip()
        session_key = f"qq:{chat_id}" if chat_id else ""

        log.info(
            "qq: received %s id=%s chat_id=%s user=%s content=%r",
            event_type,
            msg_id,
            chat_id or "?",
            memory_user_id or "?",
            content[:200],
        )

        try:
            reply = await run_turn(
                content,
                trace_id=f"qq-{msg_id[:8]}",
                session_key=session_key,
                user_id=memory_user_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("qq: orchestrator turn failed")
            reply = f"[error] {exc}"
        if not reply:
            reply = "(no response)"

        log.info("qq: replying to %s id=%s (%d chars)", event_type, msg_id, len(reply))
        try:
            await self._send_reply(event_type, d, reply, msg_id=msg_id)
            log.info("qq: reply sent for id=%s", msg_id)
        except Exception:  # noqa: BLE001
            log.exception("qq: send failed")

    async def _send_reply(
        self, event_type: str, d: Dict[str, Any], text: str, *, msg_id: str
    ) -> None:
        log.debug("qq: _send_reply entered event=%s msg_id=%s", event_type, msg_id[:30])
        text = _truncate_qq_reply(text)
        body = {
            "msg_type": 0,
            "content": text,
            "msg_id": msg_id,
            "msg_seq": _msg_seq(),
        }
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            group_id = (d.get("group_openid") or "").strip()
            if group_id:
                log.debug("qq: _send_reply -> group %s", group_id)
                await self._api("POST", f"/v2/groups/{group_id}/messages", json_body=body)
                return
        if event_type == "C2C_MESSAGE_CREATE":
            user_id = ((d.get("author") or {}).get("user_openid") or "").strip()
            if user_id:
                log.debug("qq: _send_reply -> c2c user %s", user_id[:8])
                await self._api("POST", f"/v2/users/{user_id}/messages", json_body=body)
                return
            else:
                # Log only structural keys, NOT ``d`` itself -- the dispatch
                # payload contains the user's chat content under ``content``
                # and we don't want it ending up in gateway.log.
                log.warning(
                    "qq: _send_reply C2C missing user_openid (event keys=%s)",
                    sorted(d.keys()),
                )
        if event_type in {"AT_MESSAGE_CREATE", "GUILD_AT_MESSAGE_CREATE", "MESSAGE_CREATE"}:
            channel_id = (d.get("channel_id") or "").strip()
            if channel_id:
                body["msg_type"] = 0
                # Channel API expects different field names; "msg_id" is the
                # passive-reply token (same semantics).
                await self._api(
                    "POST",
                    f"/channels/{channel_id}/messages",
                    json_body={
                        "content": text,
                        "msg_id": msg_id,
                    },
                )
                return
        # Same redaction as above -- payload contains user content.
        log.warning(
            "qq: no route for event %s (payload keys=%s)",
            event_type,
            sorted(d.keys()),
        )

    # -- WebSocket loop ----------------------------------------------------

    async def run(self) -> None:
        await self._loop()

    async def _loop(self) -> None:
        try:
            from websockets.asyncio.client import connect as ws_connect  # type: ignore
        except ImportError:  # pragma: no cover - fall back to legacy import path
            from websockets.client import connect as ws_connect  # type: ignore

        backoff = 2
        while not self._stop_event.is_set():
            watcher: Optional[asyncio.Task] = None
            try:
                url = await self._get_gateway_url()
                log.info("qq: connecting to %s", url)
                async with ws_connect(url, max_size=2**22) as ws:
                    self._ws = ws
                    # Side task: closes the WS as soon as request_stop()
                    # fires. Without this, ``async for raw in ws`` would
                    # block until the next inbound frame (potentially
                    # minutes) before we'd notice the stop signal.
                    watcher = asyncio.create_task(self._stop_watcher(ws))
                    try:
                        await self._read_loop(ws)
                    finally:
                        if watcher is not None:
                            watcher.cancel()
                            try:
                                await watcher
                            except (asyncio.CancelledError, Exception):
                                pass
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                if self._stop_event.is_set():
                    # The exception is almost certainly the watcher closing
                    # the WS; don't log it as an "error", we asked for it.
                    log.info("qq: ws closed by stop request")
                    return
                log.warning("qq ws error: %s (reconnect in %ds)", exc, backoff)
            if self._stop_event.is_set():
                log.info("qq: stop requested, exiting reconnect loop")
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _read_loop(self, ws) -> None:
        self._cancel_heartbeat()
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except Exception:
                log.warning("qq: non-json frame %r", raw)
                continue
            await self._handle_frame(ws, payload)

    async def _handle_frame(self, ws, payload: Dict[str, Any]) -> None:
        op = payload.get("op")
        s = payload.get("s")
        t = payload.get("t")
        d = payload.get("d") or {}
        if isinstance(s, int) and (self._last_seq is None or s > self._last_seq):
            self._last_seq = s

        if op == 10:  # Hello
            interval = (d.get("heartbeat_interval") or 30000) / 1000.0
            self._heartbeat_interval = interval * 0.8
            self._heartbeat_task = asyncio.create_task(self._heartbeat(ws))
            if self._session_id and self._last_seq is not None:
                await self._send_resume(ws)
            else:
                await self._send_identify(ws)
            return
        if op == 0:  # Dispatch
            if t == "READY":
                self._session_id = d.get("session_id")
                user = d.get("user") or {}
                log.info(
                    "qq: ready, bot=%s session=%s",
                    user.get("username"),
                    self._session_id,
                )
                return
            if t in {
                "C2C_MESSAGE_CREATE",
                "GROUP_AT_MESSAGE_CREATE",
                "AT_MESSAGE_CREATE",
                "GUILD_AT_MESSAGE_CREATE",
                "DIRECT_MESSAGE_CREATE",
            }:
                asyncio.create_task(self._handle_dispatch(t, d))
                return
            log.debug("qq: unhandled dispatch %s", t)
            return
        if op == 11:  # Heartbeat ACK
            return
        log.debug("qq: unhandled op %s", op)

    async def _send_identify(self, ws) -> None:
        token = await self._tokens.get(self._cfg)
        payload = {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": self._cfg["intents"],
                "shard": [0, 1],
                "properties": {
                    "$os": "linux",
                    "$browser": "agent-gateway",
                    "$device": "agent-gateway",
                },
            },
        }
        await ws.send(json.dumps(payload))

    async def _send_resume(self, ws) -> None:
        token = await self._tokens.get(self._cfg)
        payload = {
            "op": 6,
            "d": {
                "token": f"QQBot {token}",
                "session_id": self._session_id,
                "seq": self._last_seq,
            },
        }
        await ws.send(json.dumps(payload))

    async def _heartbeat(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                await ws.send(json.dumps({"op": 1, "d": self._last_seq}))
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("qq heartbeat ended: %s", exc)

    def _cancel_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json(response: "httpx.Response"):
    """Best-effort ``response.json()``; returns ``None`` on parse error so the
    caller can fall back to the raw body without surfacing a json exception."""
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return None


def _msg_seq() -> int:
    """QQ requires a 1..1e9 monotonic-ish seq per reply. Random is acceptable."""
    return int(uuid.uuid4().int >> 96) % 999_999_999 + 1


def _strip_at_mentions(text: str) -> str:
    return _AT_MENTION_RE.sub("", text).strip()


# QQ V2 message endpoints reject content longer than ~4000 chars (the API
# silently 200s but no message is delivered). Stay well under.
_QQ_REPLY_CAP = 3500


def _truncate_qq_reply(text: str) -> str:
    # Reuses the shared trim+suffix helper from the Feishu common module so
    # we have one definition of "what does truncated look like".
    from gateway._feishu_common import truncate_reply

    return truncate_reply(text, cap=_QQ_REPLY_CAP)


def serve(cfg: Optional[Dict[str, Any]] = None) -> None:
    import sys as _sys

    from gateway._pidlock import acquire, release, AlreadyRunning

    resolved = _coerce(cfg) if cfg is not None else _settings_from_env()
    try:
        acquire("qq")
    except AlreadyRunning as exc:
        log.error("%s", exc)
        _sys.exit(2)
    try:
        asyncio.run(QQGateway(resolved).run())
    finally:
        release("qq")


if __name__ == "__main__":
    serve()
