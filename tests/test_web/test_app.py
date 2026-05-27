from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from web import store
from web.app import create_app


@pytest.fixture
def client(db_path, web_secret):
    store.init_db(db_path)

    async def fake_bridge(prompt, *, trace_id, session_key, user_id, model_id):
        yield {"type": "text", "chunk": f"echo:{prompt}"}
        yield {"type": "done", "text": f"echo:{prompt}"}

    app = create_app(
        db_path=db_path, secret=web_secret, bridge_fn=fake_bridge, cookie_secure=False
    )
    return TestClient(app)


def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_me_requires_auth(client):
    r = client.get("/api/me")
    assert r.status_code == 401


def test_register_login_me_logout(client):
    r = client.post("/api/auth/register", json={"username": "alice", "password": "pw12345"})
    assert r.status_code == 200
    assert r.json()["username"] == "alice"
    # cookie set -> /api/me now works on the same client (cookie jar persists)
    me = client.get("/api/me")
    assert me.status_code == 200 and me.json()["username"] == "alice"
    # logout clears cookie
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/me").status_code == 401
    # login again
    r2 = client.post("/api/auth/login", json={"username": "alice", "password": "pw12345"})
    assert r2.status_code == 200
    assert client.get("/api/me").status_code == 200


def test_register_duplicate_username(client):
    client.post("/api/auth/register", json={"username": "bob", "password": "pw12345"})
    r = client.post("/api/auth/register", json={"username": "bob", "password": "other123"})
    assert r.status_code == 409


def test_login_wrong_password(client):
    client.post("/api/auth/register", json={"username": "carol", "password": "right123"})
    r = client.post("/api/auth/login", json={"username": "carol", "password": "wrong123"})
    assert r.status_code == 401


def test_signup_code_gate(db_path, web_secret, monkeypatch):
    monkeypatch.setenv("WEB_SIGNUP_CODE", "letmein")
    store.init_db(db_path)

    async def fake_bridge(prompt, **kw):
        yield {"type": "done", "text": ""}

    app = create_app(db_path=db_path, secret=web_secret, bridge_fn=fake_bridge, cookie_secure=False)
    c = TestClient(app)
    assert c.post("/api/auth/register", json={"username": "dan", "password": "pw12345"}).status_code == 403
    ok = c.post("/api/auth/register",
                json={"username": "dan", "password": "pw12345", "signup_code": "letmein"})
    assert ok.status_code == 200
