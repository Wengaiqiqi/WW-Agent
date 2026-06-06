"""FastAPI app factory for the web UI. The factory takes explicit deps
(db_path, JWT secret, the streaming bridge fn) so tests can inject a tmp db
and a fake bridge without spawning the orchestrator."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from web import auth, config, crypto, models, store
from web.ratelimit import RateLimiter

COOKIE_NAME = "session"


def _assert_safe_base_url(base_url: str) -> None:
    """Reject an unsafe custom-endpoint ``base_url``.

    Two checks:

    1. SSRF — reject a host that resolves to a private / loopback / link-local /
       metadata address, else an authenticated user could point the server-side
       LLM client at ``http://169.254.169.254/`` or an internal service and
       exfiltrate the response. Reuses the same DNS-resolving guard ``tool_web``
       applies to ``web_extract``.
    2. Transport — require ``https`` for remote endpoints. A plaintext ``http``
       endpoint sends the user's API key in the clear AND, unlike https, gives
       no protection against DNS rebinding between this check and the actual
       request (TLS cert validation pins the https case to the intended host).

    The ``LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS=1`` escape hatch relaxes BOTH for
    local dev (e.g. a localhost ``http`` Ollama endpoint).

    Residual (accepted): for https endpoints a determined attacker controlling
    DNS could still rebind, but TLS verification (on by default in build_llm —
    never disabled) makes the rebound host fail cert validation. Full IP pinning
    would close the remaining gap but is out of scope here."""
    import os
    from urllib.parse import urlparse

    from tool.tool_web import hostname_is_safe

    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    allowed, reason = hostname_is_safe(host)
    if not allowed:
        raise HTTPException(
            status_code=400, detail=f"base_url host not allowed: {reason}"
        )
    allow_private = os.environ.get("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS") == "1"
    if (parsed.scheme or "").lower() != "https" and not allow_private:
        raise HTTPException(
            status_code=400,
            detail="base_url must use https (set LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS=1 "
                   "for local http dev endpoints)",
        )

BridgeFn = Callable[..., Any]  # async generator: (prompt, *, trace_id, session_key, user_id, model_id)


class RegisterReq(BaseModel):
    username: str
    password: str
    signup_code: str | None = None


class LoginReq(BaseModel):
    username: str
    password: str


class ConvCreateReq(BaseModel):
    title: str | None = None


class ConvRenameReq(BaseModel):
    title: str


class MessageReq(BaseModel):
    content: str
    model: str | None = None
    endpoint_id: str | None = None


class EndpointCreateReq(BaseModel):
    label: str
    base_url: str
    api_key: str
    model: str
    protocol: str = "openai"


def create_app(
    *,
    db_path: Optional[str] = None,
    secret: Optional[str] = None,
    bridge_fn: Optional[BridgeFn] = None,
    cookie_secure: Optional[bool] = None,
) -> FastAPI:
    db = db_path or store.default_db_path()
    store.init_db(db)
    jwt_secret = secret or config.auth_secret()
    secure = config.cookie_secure() if cookie_secure is None else cookie_secure
    # Only the real bridge spawns specialists — tests inject a fake bridge_fn
    # and must never trigger a real spawn (incl. the startup catalog warm-up).
    use_real_bridge = bridge_fn is None
    if bridge_fn is None:
        from web.bridge import run_turn_streaming as bridge_fn  # type: ignore
    limiter = RateLimiter(
        capacity=config.rate_limit_per_min(),
        refill_per_sec=config.rate_limit_per_min() / 60.0,
    )

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Startup: warm the capability catalog in the background so the first
        # user turn doesn't wait ~10s for specialist spawn (held on app.state so
        # the task isn't GC'd), and start the idle-pool sweeper when pooling is on.
        if use_real_bridge:
            from web.bridge import warm_capability_catalog

            app.state._warm_task = asyncio.create_task(warm_capability_catalog())
            if config.pool_enabled():
                from web import bridge as _bridge

                loop = _bridge._ensure_turn_loop()
                app.state._sweeper = loop.run_coroutine_factory(
                    lambda: _bridge._pool_sweeper(interval=config.pool_idle_ttl()),
                )
        try:
            yield
        finally:
            # Shutdown: cancel the sweeper, reap pooled specialist subprocesses,
            # and stop the turn loop so the process exits cleanly (no orphans).
            if use_real_bridge:
                from web import bridge as _bridge

                sweeper = getattr(app.state, "_sweeper", None)
                if sweeper is not None and not sweeper.done():
                    _bridge._TURN_LOOP.call_soon(sweeper.cancel)
                try:
                    if _bridge._POOL is not None:
                        pool = _bridge._get_pool()
                        loop = _bridge._ensure_turn_loop()
                        # Block on the drain via a worker thread rather than
                        # awaiting a cross-loop wrap_future in the lifespan task
                        # (which entangles anyio's lifespan cancel scope).
                        fut = loop.run_coroutine_factory(pool.drain)
                        await asyncio.to_thread(fut.result)
                finally:
                    _bridge._TURN_LOOP.stop()

    app = FastAPI(title="Agent Web UI", lifespan=_lifespan)

    def current_user(session: str | None = Cookie(default=None)) -> dict:
        claims = auth.verify_token(session, jwt_secret) if session else None
        if not claims:
            raise HTTPException(status_code=401, detail="not authenticated")
        user = store.get_user(db, claims.get("sub", ""))
        if not user:
            raise HTTPException(status_code=401, detail="unknown user")
        return user

    def _owned_conversation(conv_id: str, user: dict) -> dict:
        conv = store.get_conversation(db, conv_id)
        if not conv or conv["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="conversation not found")
        return conv

    def _set_cookie(resp: JSONResponse, token: str) -> None:
        resp.set_cookie(
            COOKIE_NAME, token, httponly=True, samesite="strict",
            secure=secure, max_age=7 * 24 * 3600, path="/",
        )

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/me")
    def me(user: dict = Depends(current_user)) -> dict:
        return {"id": user["id"], "username": user["username"], "role": user["role"]}

    @app.get("/api/models")
    def list_models(user: dict = Depends(current_user)) -> list[dict]:
        return models.available_models()

    _mount_auth_routes(app, db, jwt_secret, _set_cookie)
    _mount_conversation_routes(app, db, current_user, _owned_conversation)
    _mount_endpoint_routes(app, db, current_user)
    _mount_chat_route(app, db, current_user, _owned_conversation, limiter, bridge_fn)
    _mount_static(app)

    return app


def _mount_auth_routes(app, db, secret, set_cookie):
    @app.post("/api/auth/register")
    def register(req: RegisterReq) -> JSONResponse:
        gate = config.signup_code()
        if gate and (req.signup_code or "") != gate:
            raise HTTPException(status_code=403, detail="邀请码无效")
        if not req.username.strip() or len(req.password) < 6:
            raise HTTPException(status_code=400, detail="请填写用户名，且密码至少 6 位")
        pwd_hash, salt = auth.hash_password(req.password)
        try:
            uid = store.create_user(db, req.username.strip(), pwd_hash, salt)
        except store.DuplicateUsername:
            raise HTTPException(status_code=409, detail="用户名已被占用")
        token = auth.mint_token(user_id=uid, username=req.username.strip(), secret=secret)
        resp = JSONResponse({"id": uid, "username": req.username.strip()})
        set_cookie(resp, token)
        return resp

    @app.post("/api/auth/login")
    def login(req: LoginReq) -> JSONResponse:
        user = store.get_user_by_username(db, req.username.strip())
        if not user or not auth.verify_password(req.password, user["pwd_hash"], user["salt"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = auth.mint_token(user_id=user["id"], username=user["username"], secret=secret)
        resp = JSONResponse({"id": user["id"], "username": user["username"]})
        set_cookie(resp, token)
        return resp

    @app.post("/api/auth/logout")
    def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp


def _mount_conversation_routes(app, db, current_user, owned):
    @app.get("/api/conversations")
    def list_conversations(user: dict = Depends(current_user)) -> list[dict]:
        return store.list_conversations(db, user["id"])

    @app.post("/api/conversations")
    def create_conversation(req: ConvCreateReq, user: dict = Depends(current_user)) -> dict:
        cid = store.create_conversation(db, user["id"], (req.title or "New chat").strip())
        return store.get_conversation(db, cid)

    @app.patch("/api/conversations/{conv_id}")
    def rename_conversation(conv_id: str, req: ConvRenameReq,
                            user: dict = Depends(current_user)) -> dict:
        owned(conv_id, user)
        store.rename_conversation(db, conv_id, req.title.strip() or "New chat")
        return store.get_conversation(db, conv_id)

    @app.delete("/api/conversations/{conv_id}")
    def delete_conversation(conv_id: str, user: dict = Depends(current_user)) -> dict:
        owned(conv_id, user)
        store.delete_conversation(db, conv_id)
        return {"ok": True}

    @app.get("/api/conversations/{conv_id}/messages")
    def list_messages(conv_id: str, user: dict = Depends(current_user)) -> list[dict]:
        owned(conv_id, user)
        return store.list_messages(db, conv_id)


def _mount_endpoint_routes(app, db, current_user):
    @app.get("/api/endpoints")
    def list_endpoints(user: dict = Depends(current_user)) -> list[dict]:
        return store.list_endpoints(db, user["id"])

    @app.post("/api/endpoints")
    def create_endpoint(req: EndpointCreateReq,
                        user: dict = Depends(current_user)) -> dict:
        label = req.label.strip()
        base_url = req.base_url.strip()
        model = req.model.strip()
        api_key = req.api_key.strip()
        protocol = (req.protocol or "openai").strip()
        if not (label and base_url and model and api_key):
            raise HTTPException(status_code=400,
                                detail="label, base_url, model, api_key required")
        if protocol not in ("openai", "anthropic"):
            raise HTTPException(status_code=400,
                                detail="protocol must be 'openai' or 'anthropic'")
        _assert_safe_base_url(base_url)
        return store.create_endpoint(
            db, user["id"], label, base_url, crypto.encrypt_secret(api_key),
            model, protocol,
        )

    @app.delete("/api/endpoints/{endpoint_id}")
    def delete_endpoint(endpoint_id: str,
                        user: dict = Depends(current_user)) -> dict:
        ep = store.get_endpoint(db, endpoint_id)
        if not ep or ep["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="endpoint not found")
        store.delete_endpoint(db, endpoint_id)
        return {"ok": True}


def _mount_chat_route(app, db, current_user, owned, limiter, bridge_fn):
    @app.post("/api/conversations/{conv_id}/messages")
    def send_message(conv_id: str, req: MessageReq, user: dict = Depends(current_user)):
        owned(conv_id, user)
        content = (req.content or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="empty message")
        if len(content) > config.MAX_MESSAGE_CHARS:
            raise HTTPException(status_code=413, detail="message too long")
        if not limiter.allow(user["id"]):
            raise HTTPException(status_code=429, detail="rate limit exceeded")

        if req.endpoint_id:
            ep = store.get_endpoint(db, req.endpoint_id)
            if not ep or ep["user_id"] != user["id"]:
                raise HTTPException(status_code=404, detail="endpoint not found")
            # Re-validate at use time: blocks an endpoint whose DNS now resolves
            # to a private address (rebinding) even if it passed at creation.
            _assert_safe_base_url(ep["base_url"])
            bridge_kwargs = {
                "model_id": f"custom/{ep['model']}",
                "base_url": ep["base_url"],
                "api_key": crypto.decrypt_secret(ep["api_key"]),
                "protocol": ep["protocol"],
            }
        else:
            bridge_kwargs = {"model_id": (req.model or "")}

        # Persist the user's message up front so the UI can render it during
        # streaming. ``user_mid`` lets us roll back the row if the turn
        # produces nothing — see the finally block below.
        user_mid = store.add_message(db, conv_id, "user", content, "[]")

        async def event_stream():
            collected: list[dict] = []
            final_text = ""
            try:
                async for ev in bridge_fn(
                    content,
                    trace_id=f"web-{conv_id[:8]}",
                    session_key=conv_id,
                    user_id=user["id"],
                    **bridge_kwargs,
                ):
                    etype = ev.get("type")
                    if etype == "text":
                        final_text += ev.get("chunk", "")
                    elif etype == "done" and ev.get("text"):
                        final_text = ev["text"]
                    elif etype in ("thinking", "tool_call", "tool_result", "error", "warning"):
                        collected.append(ev)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            finally:
                produced_anything = bool(final_text.strip()) or bool(collected)
                if produced_anything:
                    store.add_message(
                        db, conv_id, "assistant", final_text.strip(),
                        json.dumps(collected, ensure_ascii=False),
                    )
                    store.touch_conversation(db, conv_id)
                else:
                    # Bridge crashed before emitting anything. Don't leave a
                    # phantom turn (user row with no reply) — the conversation
                    # on refresh would show a question that was never asked
                    # of the agent.
                    store.delete_message(db, user_mid)

        return StreamingResponse(event_stream(), media_type="text/event-stream")


class _NoCacheStatic(StaticFiles):
    """StaticFiles that tells the browser to revalidate every time. The assets
    still get ETag/Last-Modified (so unchanged files return a cheap 304), but a
    code change shows up on a normal refresh — no more stale ``app.js`` served
    from cache after a deploy."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def _mount_static(app):
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", _NoCacheStatic(directory=str(static_dir)), name="static")

    @app.get("/")
    def index():
        from fastapi.responses import FileResponse

        return FileResponse(
            str(static_dir / "index.html"), headers={"Cache-Control": "no-cache"}
        )
