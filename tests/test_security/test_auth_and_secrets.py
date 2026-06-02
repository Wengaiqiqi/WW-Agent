from __future__ import annotations

import sqlite3

import pytest
from starlette.testclient import TestClient

from web import config, crypto, store
from web.app import create_app


def _fake_bridge(*a, **k):
    async def _gen():
        yield {"type": "done", "text": "ok"}
    return _gen()


@pytest.fixture
def client(db_path, web_secret):
    app = create_app(db_path=db_path, secret=web_secret,
                     bridge_fn=_fake_bridge, cookie_secure=False)
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("method,path", [
    ("get", "/api/me"),
    ("get", "/api/conversations"),
    ("get", "/api/endpoints"),
    ("get", "/api/models"),
])
def test_protected_routes_require_auth(client, method, path):
    resp = getattr(client, method)(path)
    assert resp.status_code == 401


def test_signup_gate_enforced(db_path, web_secret, monkeypatch):
    monkeypatch.setenv("WEB_SIGNUP_CODE", "letmein")
    app = create_app(db_path=db_path, secret=web_secret,
                     bridge_fn=_fake_bridge, cookie_secure=False)
    with TestClient(app) as c:
        bad = c.post("/api/auth/register",
                     json={"username": "u", "password": "secret123"})
        assert bad.status_code == 403
        ok = c.post("/api/auth/register",
                    json={"username": "u", "password": "secret123",
                          "signup_code": "letmein"})
        assert ok.status_code == 200


def test_jwt_secret_dev_fallback_is_stable(monkeypatch, tmp_config_dir):
    monkeypatch.delenv("WEB_AUTH_SECRET", raising=False)
    config._DEV_SECRET = None  # reset the process cache
    s1 = config.auth_secret()
    s2 = config.auth_secret()
    assert s1 and s1 == s2  # stable within a process (and persisted to disk)


def test_api_key_stored_as_ciphertext_not_plaintext(db_path, web_secret, tmp_config_dir):
    store.init_db(db_path)
    uid = store.create_user(db_path, "alice", "hash", "salt")  # FK target
    plaintext = "sk-super-secret-value-123"
    store.create_endpoint(db_path, user_id=uid, label="e", base_url="https://api.x/v1",
                          api_key=crypto.encrypt_secret(plaintext), model="m", protocol="openai")
    # Read the raw column straight from sqlite — must NOT be the plaintext.
    with sqlite3.connect(db_path) as conn:
        rows = [r[0] for r in conn.execute("SELECT api_key FROM endpoints").fetchall()]
    assert rows and plaintext not in rows[0]
    # And it must round-trip back to the plaintext in memory.
    assert crypto.decrypt_secret(rows[0]) == plaintext


def test_list_endpoints_never_returns_key(db_path, web_secret, tmp_config_dir):
    store.init_db(db_path)
    uid = store.create_user(db_path, "bob", "hash", "salt")  # FK target
    store.create_endpoint(db_path, user_id=uid, label="e", base_url="https://api.x/v1",
                          api_key=crypto.encrypt_secret("sk-x"), model="m", protocol="openai")
    listed = store.list_endpoints(db_path, uid)
    assert listed and all("api_key" not in row for row in listed)
