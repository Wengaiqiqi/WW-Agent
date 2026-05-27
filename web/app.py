"""FastAPI app factory for the web UI. The factory takes explicit deps
(db_path, JWT secret, the streaming bridge fn) so tests can inject a tmp db
and a fake bridge without spawning the orchestrator."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from web import auth, config, models, store
from web.ratelimit import RateLimiter

COOKIE_NAME = "session"

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
    if bridge_fn is None:
        from web.bridge import run_turn_streaming as bridge_fn  # type: ignore
    limiter = RateLimiter(
        capacity=config.rate_limit_per_min(),
        refill_per_sec=config.rate_limit_per_min() / 60.0,
    )

    app = FastAPI(title="Agent Web UI")

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
    _mount_chat_route(app, db, current_user, _owned_conversation, limiter, bridge_fn)
    _mount_static(app)
    return app


def _mount_auth_routes(app, db, secret, set_cookie):
    @app.post("/api/auth/register")
    def register(req: RegisterReq) -> JSONResponse:
        gate = config.signup_code()
        if gate and (req.signup_code or "") != gate:
            raise HTTPException(status_code=403, detail="invalid signup code")
        if not req.username.strip() or len(req.password) < 6:
            raise HTTPException(status_code=400, detail="username required, password >= 6 chars")
        pwd_hash, salt = auth.hash_password(req.password)
        try:
            uid = store.create_user(db, req.username.strip(), pwd_hash, salt)
        except store.DuplicateUsername:
            raise HTTPException(status_code=409, detail="username taken")
        token = auth.mint_token(user_id=uid, username=req.username.strip(), secret=secret)
        resp = JSONResponse({"id": uid, "username": req.username.strip()})
        set_cookie(resp, token)
        return resp

    @app.post("/api/auth/login")
    def login(req: LoginReq) -> JSONResponse:
        user = store.get_user_by_username(db, req.username.strip())
        if not user or not auth.verify_password(req.password, user["pwd_hash"], user["salt"]):
            raise HTTPException(status_code=401, detail="invalid credentials")
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

        # Persist the user's message up front.
        store.add_message(db, conv_id, "user", content, "[]")

        async def event_stream():
            collected: list[dict] = []
            final_text = ""
            try:
                async for ev in bridge_fn(
                    content,
                    trace_id=f"web-{conv_id[:8]}",
                    session_key=conv_id,
                    user_id=user["id"],
                    model_id=(req.model or ""),
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
                store.add_message(
                    db, conv_id, "assistant", final_text.strip(),
                    json.dumps(collected, ensure_ascii=False),
                )
                store.touch_conversation(db, conv_id)

        return StreamingResponse(event_stream(), media_type="text/event-stream")


def _mount_static(app):  # replaced in Task 19
    pass
