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
