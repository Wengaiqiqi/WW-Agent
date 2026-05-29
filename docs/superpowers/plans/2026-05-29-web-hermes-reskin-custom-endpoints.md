# Web Hermes Reskin + Custom Endpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reskin the vanilla-JS web UI to the Hermes Teal terminal aesthetic and let a logged-in user add per-user custom model endpoints (Base URL + API key + model + protocol) selectable in the chat header.

**Architecture:** Keep the existing FastAPI-served static SPA (`web/static/`). Add a per-user `endpoints` table in the existing SQLite store, REST routes guarded by the current-user dependency, and per-turn env injection so a user-supplied URL/key reaches both the in-process planner and any delegated specialist subprocess (via three new env vars added to the spawn whitelist). The config layer learns to honor those env overrides.

**Tech Stack:** Python 3 / FastAPI / SQLite (backend), vanilla JS + CSS (frontend), pytest (tests). Reference spec: `docs/superpowers/specs/2026-05-29-web-hermes-reskin-custom-endpoints-design.md`.

---

## File Structure

**Backend (config layer):**
- `config/_settings.py` — `load_active_config` learns `LANGCHAIN_AGENT_BASE_URL` / `LANGCHAIN_AGENT_PROTOCOL` overrides.
- `config/_credentials.py` — `get_api_key` prefers `LANGCHAIN_AGENT_API_KEY`.
- `orchestrator/mcp_host.py` — add the three new env vars to `_OS_PASSTHROUGH` so spawned specialists inherit them.

**Backend (web):**
- `web/store.py` — `endpoints` table + CRUD.
- `web/app.py` — `/api/endpoints` routes + `MessageReq.endpoint_id` handling.
- `web/bridge.py` — thread `base_url`/`api_key`/`protocol` through to `_web_turn_env`.

**Frontend:**
- `web/static/app.js` — merge presets + custom endpoints into the picker; endpoint add/delete modal; send `endpoint_id`.
- `web/static/index.html` — model picker control, endpoints modal markup, font links.
- `web/static/styles.css` — full reskin to Hermes Teal.
- `web/static/fonts/` — bundled JetBrains Mono woff2 (Apache-2.0).

**Tests:**
- `tests/test_config_overrides.py` (new) — env-override behavior.
- `tests/test_orchestrator/test_mcp_host.py` — passthrough additions.
- `tests/test_web/test_store.py` — endpoints CRUD.
- `tests/test_web/test_app.py` — endpoints routes + endpoint_id chat.
- `tests/test_web/test_bridge.py` — endpoint env set/restore.

Implementation order: config (1–2) → store (3) → routes (4) → bridge (5) → frontend feature (6) → reskin (7). Each backend task is independently testable and committable; frontend tasks are verified in a browser.

---

## Task 1: Config — honor base_url / protocol / api_key env overrides

**Files:**
- Modify: `config/_settings.py` (rewrite `load_active_config`, add two helpers)
- Modify: `config/_credentials.py` (`get_api_key`)
- Test: `tests/test_config_overrides.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_overrides.py`:

```python
from __future__ import annotations

from config import _credentials, _settings


def test_base_url_and_protocol_overrides_applied(monkeypatch):
    # "custom" provider exists in the registry (base_url "", protocol openai).
    monkeypatch.setenv("LANGCHAIN_AGENT_MODEL", "custom/gpt-5.4")
    monkeypatch.setenv("LANGCHAIN_AGENT_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("LANGCHAIN_AGENT_PROTOCOL", "anthropic")
    cfg = _settings.load_active_config()
    assert cfg.provider == "custom"
    assert cfg.model == "gpt-5.4"
    assert cfg.base_url == "https://example.test/v1"
    assert cfg.protocol == "anthropic"


def test_no_overrides_leaves_registry_defaults(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_AGENT_MODEL", "openai/gpt-4o")
    monkeypatch.delenv("LANGCHAIN_AGENT_BASE_URL", raising=False)
    monkeypatch.delenv("LANGCHAIN_AGENT_PROTOCOL", raising=False)
    cfg = _settings.load_active_config()
    assert cfg.base_url == "https://api.openai.com/v1"
    assert cfg.protocol == "openai"


def test_get_api_key_prefers_env_override(monkeypatch):
    from config import make_config

    monkeypatch.setenv("LANGCHAIN_AGENT_API_KEY", "sk-override")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-provider-env")
    cfg = make_config("openai", model="gpt-4o")
    assert _credentials.get_api_key(cfg) == "sk-override"


def test_get_api_key_falls_back_when_no_override(monkeypatch):
    from config import make_config

    monkeypatch.delenv("LANGCHAIN_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-provider-env")
    cfg = make_config("openai", model="gpt-4o")
    assert _credentials.get_api_key(cfg) == "sk-from-provider-env"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config_overrides.py -v`
Expected: FAIL — `test_base_url_and_protocol_overrides_applied` shows `base_url == ""` (override ignored); `test_get_api_key_prefers_env_override` returns `sk-from-provider-env`.

- [ ] **Step 3: Implement the env overrides in `config/_settings.py`**

Replace the body of `load_active_config` and add two helpers. The full new shape (replace the existing `load_active_config` function):

```python
def load_active_config() -> ActiveConfig:
    """Resolve which model should be active, then apply per-turn env overrides.

    Base resolution order: ``LANGCHAIN_AGENT_MODEL`` env > settings.json >
    ``DEFAULT_PROVIDER``. After that, ``LANGCHAIN_AGENT_BASE_URL`` and
    ``LANGCHAIN_AGENT_PROTOCOL`` (when set) override the resolved config — the
    web "custom endpoint" feature sets these for the duration of a turn.
    """
    return _apply_env_overrides(_resolve_base_config())


def _resolve_base_config() -> ActiveConfig:
    env_choice = os.getenv("LANGCHAIN_AGENT_MODEL", "").strip()
    if env_choice:
        if "/" in env_choice:
            prov_name, model_name = env_choice.split("/", 1)
        else:
            prov_name, model_name = env_choice, ""
        if prov_name in PROVIDERS:
            return make_config(prov_name, model=model_name)

    settings = _read_settings()
    model_block = settings.get("model")
    if isinstance(model_block, dict):
        prov_name = str(model_block.get("provider") or "")
        if prov_name in PROVIDERS:
            return make_config(
                prov_name,
                model=str(model_block.get("model") or ""),
                base_url=str(model_block.get("base_url") or ""),
                api_key_env=str(model_block.get("api_key_env") or ""),
            )
    elif isinstance(model_block, str) and model_block:
        logger.warning(
            "Ignoring legacy settings.json model entry %r; the schema is now a "
            "dict. Run /model to reconfigure (falling back to provider %r).",
            model_block, DEFAULT_PROVIDER,
        )

    return make_config(DEFAULT_PROVIDER)


def _apply_env_overrides(cfg: ActiveConfig) -> ActiveConfig:
    """Apply ``LANGCHAIN_AGENT_BASE_URL`` / ``LANGCHAIN_AGENT_PROTOCOL`` if set.

    No-op when neither is present, so non-web callers are unaffected.
    """
    base_url = os.getenv("LANGCHAIN_AGENT_BASE_URL", "").strip()
    protocol = os.getenv("LANGCHAIN_AGENT_PROTOCOL", "").strip()
    if base_url:
        cfg.base_url = base_url
    if protocol:
        cfg.protocol = protocol
    return cfg
```

- [ ] **Step 4: Implement the api-key override in `config/_credentials.py`**

Replace `get_api_key`:

```python
def get_api_key(cfg: ActiveConfig) -> str:
    """Look up the API key for *cfg*.

    ``LANGCHAIN_AGENT_API_KEY`` (set per-turn by the web custom-endpoint flow)
    wins; otherwise fall back to the provider's ``api_key_env`` then the
    credentials file.
    """
    override = os.getenv("LANGCHAIN_AGENT_API_KEY", "").strip()
    if override:
        return override
    return os.getenv(cfg.api_key_env) or load_credentials().get(cfg.api_key_env, "")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_config_overrides.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Run the broader config-dependent suites for regressions**

Run: `python -m pytest tests/test_mock_provider.py tests/test_web/test_bridge.py -q`
Expected: PASS (no regressions from the `load_active_config` refactor)

- [ ] **Step 7: Commit**

```bash
git add config/_settings.py config/_credentials.py tests/test_config_overrides.py
git commit -m "feat(config): honor base_url/protocol/api_key env overrides"
```

---

## Task 2: Forward custom-endpoint env vars to specialist subprocesses

**Files:**
- Modify: `orchestrator/mcp_host.py:28-52` (`_OS_PASSTHROUGH`)
- Test: `tests/test_orchestrator/test_mcp_host.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orchestrator/test_mcp_host.py`:

```python
def test_build_agent_env_forwards_custom_endpoint_vars(monkeypatch):
    """A web custom-endpoint turn sets base_url/protocol/api_key in the parent
    env; a delegated specialist must inherit them so it can build the same
    custom LLM. (These are only present when a custom endpoint is active.)"""
    monkeypatch.setenv("LANGCHAIN_AGENT_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("LANGCHAIN_AGENT_PROTOCOL", "openai")
    monkeypatch.setenv("LANGCHAIN_AGENT_API_KEY", "sk-turn-key")
    env = _build_agent_env(hmac_key="k", agent_id="tool-agent")
    assert env["LANGCHAIN_AGENT_BASE_URL"] == "https://example.test/v1"
    assert env["LANGCHAIN_AGENT_PROTOCOL"] == "openai"
    assert env["LANGCHAIN_AGENT_API_KEY"] == "sk-turn-key"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator/test_mcp_host.py::test_build_agent_env_forwards_custom_endpoint_vars -v`
Expected: FAIL — KeyError / missing keys (vars stripped at the boundary).

- [ ] **Step 3: Add the vars to `_OS_PASSTHROUGH`**

In `orchestrator/mcp_host.py`, in the `_OS_PASSTHROUGH` set, extend the "App config" block. Change:

```python
    # App config (not secrets)
    "LANGCHAIN_AGENT_MODEL", "LANGCHAIN_AGENT_CONFIG_DIR",
    "LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS",
```

to:

```python
    # App config (not secrets)
    "LANGCHAIN_AGENT_MODEL", "LANGCHAIN_AGENT_CONFIG_DIR",
    "LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS",
    # Per-turn custom-endpoint overrides (set by the web UI). BASE_URL/PROTOCOL
    # are not secrets; API_KEY is the one key the user chose for THIS turn and
    # is only present in env while a custom endpoint is active — a deliberate,
    # minimal exception to the "no secrets across the boundary" default so a
    # delegated specialist can authenticate against the same endpoint.
    "LANGCHAIN_AGENT_BASE_URL", "LANGCHAIN_AGENT_PROTOCOL",
    "LANGCHAIN_AGENT_API_KEY",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_orchestrator/test_mcp_host.py -v -m "not e2e"`
Expected: PASS (the two existing non-e2e tests + the new one)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/mcp_host.py tests/test_orchestrator/test_mcp_host.py
git commit -m "feat(orchestrator): forward custom-endpoint env to specialists"
```

---

## Task 3: SQLite store — per-user endpoints CRUD

**Files:**
- Modify: `web/store.py` (`init_db` + four new functions)
- Test: `tests/test_web/test_store.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web/test_store.py`:

```python
def test_endpoint_crud_and_isolation(db_path):
    store.init_db(db_path)
    alice = store.create_user(db_path, "alice", "h", "s")
    bob = store.create_user(db_path, "bob", "h", "s")

    ep = store.create_endpoint(
        db_path, alice, "My LLM", "https://x.test/v1", "sk-secret",
        "gpt-5.4", "openai",
    )
    assert ep["id"] and ep["label"] == "My LLM" and ep["model"] == "gpt-5.4"
    assert "api_key" not in ep  # create returns metadata only

    # list omits api_key and is per-user.
    rows = store.list_endpoints(db_path, alice)
    assert len(rows) == 1 and "api_key" not in rows[0]
    assert store.list_endpoints(db_path, bob) == []

    # get_endpoint (internal) DOES include the key for the turn.
    full = store.get_endpoint(db_path, ep["id"])
    assert full["api_key"] == "sk-secret" and full["user_id"] == alice

    store.delete_endpoint(db_path, ep["id"])
    assert store.get_endpoint(db_path, ep["id"]) is None


def test_endpoint_defaults_protocol_openai(db_path):
    store.init_db(db_path)
    uid = store.create_user(db_path, "alice", "h", "s")
    ep = store.create_endpoint(db_path, uid, "L", "https://x/v1", "k", "m")
    assert store.get_endpoint(db_path, ep["id"])["protocol"] == "openai"


def test_delete_user_cascades_endpoints(db_path):
    store.init_db(db_path)
    uid = store.create_user(db_path, "alice", "h", "s")
    ep = store.create_endpoint(db_path, uid, "L", "https://x/v1", "k", "m")
    with store._connect(db_path) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    assert store.get_endpoint(db_path, ep["id"]) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web/test_store.py -k endpoint -v`
Expected: FAIL — `AttributeError: module 'web.store' has no attribute 'create_endpoint'`.

- [ ] **Step 3: Add the endpoints table to `init_db`**

In `web/store.py`, inside the `init_db` `executescript`, add these statements before the closing `idx_*` indexes (i.e. immediately after the `messages` table definition):

```sql
            CREATE TABLE IF NOT EXISTS endpoints (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                label TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                model TEXT NOT NULL,
                protocol TEXT NOT NULL DEFAULT 'openai',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
```

And add this index alongside the existing `CREATE INDEX` lines:

```sql
            CREATE INDEX IF NOT EXISTS idx_endpoints_user ON endpoints(user_id);
```

- [ ] **Step 4: Add the CRUD functions**

Append to `web/store.py` (after `list_messages`):

```python
def create_endpoint(
    db_path: str,
    user_id: str,
    label: str,
    base_url: str,
    api_key: str,
    model: str,
    protocol: str = "openai",
) -> dict[str, Any]:
    """Insert a per-user custom endpoint. Returns metadata WITHOUT the key."""
    eid = _new_id()
    now = _now()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO endpoints "
            "(id, user_id, label, base_url, api_key, model, protocol, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, user_id, label, base_url, api_key, model, protocol, now),
        )
    return {
        "id": eid, "label": label, "base_url": base_url,
        "model": model, "protocol": protocol, "created_at": now,
    }


def list_endpoints(db_path: str, user_id: str) -> list[dict[str, Any]]:
    """List a user's endpoints. Never selects ``api_key`` — safe to return to
    the browser as-is."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, user_id, label, base_url, model, protocol, created_at "
            "FROM endpoints WHERE user_id = ? "
            "ORDER BY created_at DESC, rowid DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_endpoint(db_path: str, endpoint_id: str) -> Optional[dict[str, Any]]:
    """Fetch the full endpoint row INCLUDING ``api_key`` — for server-side turn
    setup only. The route layer compares ``user_id`` for ownership."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM endpoints WHERE id = ?", (endpoint_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_endpoint(db_path: str, endpoint_id: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_web/test_store.py -v`
Expected: PASS (existing + 3 new endpoint tests)

- [ ] **Step 6: Commit**

```bash
git add web/store.py tests/test_web/test_store.py
git commit -m "feat(web): per-user custom endpoints table + CRUD"
```

---

## Task 4: Routes — /api/endpoints + endpoint_id on chat

**Files:**
- Modify: `web/app.py` (new `EndpointCreateReq`, `_mount_endpoint_routes`, `MessageReq.endpoint_id`, `send_message`)
- Test: `tests/test_web/test_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web/test_app.py` (the module already defines `_register`, `client`, `_sse_events`):

```python
def test_endpoint_routes_crud_and_no_key_leak(client):
    _register(client, "ned")
    r = client.post("/api/endpoints", json={
        "label": "My LLM", "base_url": "https://x.test/v1",
        "api_key": "sk-secret", "model": "gpt-5.4", "protocol": "openai",
    })
    assert r.status_code == 200
    eid = r.json()["id"]
    assert "api_key" not in r.json()
    listed = client.get("/api/endpoints").json()
    assert listed[0]["id"] == eid and "api_key" not in listed[0]
    assert client.delete(f"/api/endpoints/{eid}").status_code == 200
    assert client.get("/api/endpoints").json() == []


def test_endpoint_create_validates(client):
    _register(client, "nora")
    assert client.post("/api/endpoints", json={
        "label": "", "base_url": "https://x/v1", "api_key": "k", "model": "m",
    }).status_code == 400
    assert client.post("/api/endpoints", json={
        "label": "L", "base_url": "https://x/v1", "api_key": "k", "model": "m",
        "protocol": "weird",
    }).status_code == 400


def test_endpoints_require_auth(client):
    assert client.get("/api/endpoints").status_code == 401


def test_cannot_delete_other_users_endpoint(db_path, web_secret):
    store.init_db(db_path)

    async def fake_bridge(prompt, **kw):
        yield {"type": "done", "text": ""}

    app = create_app(db_path=db_path, secret=web_secret, bridge_fn=fake_bridge, cookie_secure=False)
    alice, bob = TestClient(app), TestClient(app)
    _register(alice, "alice")
    _register(bob, "bob")
    eid = alice.post("/api/endpoints", json={
        "label": "L", "base_url": "https://x/v1", "api_key": "k", "model": "m",
    }).json()["id"]
    assert bob.delete(f"/api/endpoints/{eid}").status_code == 404


def test_chat_with_endpoint_id_routes_endpoint_fields():
    """Selecting a custom endpoint passes base_url/api_key/protocol + a
    custom/<model> id into the bridge."""
    import tempfile, os as _os
    db = _os.path.join(tempfile.mkdtemp(), "app.db")
    store.init_db(db)
    captured = {}

    async def fake_bridge(prompt, *, trace_id, session_key, user_id,
                          model_id, base_url="", api_key="", protocol=""):
        captured.update(model_id=model_id, base_url=base_url,
                        api_key=api_key, protocol=protocol)
        yield {"type": "done", "text": "ok"}

    app = create_app(db_path=db, secret="test-secret-not-for-production",
                     bridge_fn=fake_bridge, cookie_secure=False)
    c = TestClient(app)
    _register(c, "olivia")
    eid = c.post("/api/endpoints", json={
        "label": "L", "base_url": "https://x.test/v1", "api_key": "sk-z",
        "model": "gpt-5.4", "protocol": "anthropic",
    }).json()["id"]
    cid = c.post("/api/conversations", json={}).json()["id"]
    r = c.post(f"/api/conversations/{cid}/messages",
               json={"content": "hi", "endpoint_id": eid})
    assert r.status_code == 200
    assert captured == {
        "model_id": "custom/gpt-5.4", "base_url": "https://x.test/v1",
        "api_key": "sk-z", "protocol": "anthropic",
    }
```

(`store` and `create_app` are already imported at the top of the test module.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web/test_app.py -k endpoint -v`
Expected: FAIL — 404/422 on `/api/endpoints` (routes don't exist yet).

- [ ] **Step 3: Add the request model + endpoint_id field**

In `web/app.py`, add a new request model next to the others:

```python
class EndpointCreateReq(BaseModel):
    label: str
    base_url: str
    api_key: str
    model: str
    protocol: str = "openai"
```

And add the optional field to `MessageReq`:

```python
class MessageReq(BaseModel):
    content: str
    model: str | None = None
    endpoint_id: str | None = None
```

- [ ] **Step 4: Mount the endpoint routes**

In `create_app`, after `_mount_conversation_routes(...)`, add:

```python
    _mount_endpoint_routes(app, db, current_user)
```

Then add the new mount function (next to `_mount_conversation_routes`):

```python
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
        return store.create_endpoint(
            db, user["id"], label, base_url, api_key, model, protocol
        )

    @app.delete("/api/endpoints/{endpoint_id}")
    def delete_endpoint(endpoint_id: str,
                        user: dict = Depends(current_user)) -> dict:
        ep = store.get_endpoint(db, endpoint_id)
        if not ep or ep["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="endpoint not found")
        store.delete_endpoint(db, endpoint_id)
        return {"ok": True}
```

- [ ] **Step 5: Route endpoint fields into the bridge in `send_message`**

In `_mount_chat_route`'s `send_message`, after the rate-limit check and before `store.add_message(... "user" ...)`, resolve the bridge kwargs:

```python
        if req.endpoint_id:
            ep = store.get_endpoint(db, req.endpoint_id)
            if not ep or ep["user_id"] != user["id"]:
                raise HTTPException(status_code=404, detail="endpoint not found")
            bridge_kwargs = {
                "model_id": f"custom/{ep['model']}",
                "base_url": ep["base_url"],
                "api_key": ep["api_key"],
                "protocol": ep["protocol"],
            }
        else:
            bridge_kwargs = {"model_id": (req.model or "")}
```

Then change the `bridge_fn(...)` call inside `event_stream` from:

```python
                async for ev in bridge_fn(
                    content,
                    trace_id=f"web-{conv_id[:8]}",
                    session_key=conv_id,
                    user_id=user["id"],
                    model_id=(req.model or ""),
                ):
```

to:

```python
                async for ev in bridge_fn(
                    content,
                    trace_id=f"web-{conv_id[:8]}",
                    session_key=conv_id,
                    user_id=user["id"],
                    **bridge_kwargs,
                ):
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_web/test_app.py -v`
Expected: PASS (existing route tests + the 5 new endpoint tests; the preset path still passes only `model_id`, so the existing `fake_bridge` signatures keep working).

- [ ] **Step 7: Commit**

```bash
git add web/app.py tests/test_web/test_app.py
git commit -m "feat(web): /api/endpoints routes + endpoint_id chat routing"
```

---

## Task 5: Bridge — apply custom endpoint env per turn

**Files:**
- Modify: `web/bridge.py` (`_web_turn_env`, `run_turn_streaming`, `_stream_off_loop`, `_run_streaming_locked`)
- Test: `tests/test_web/test_bridge.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web/test_bridge.py`:

```python
def test_web_turn_env_custom_endpoint_sets_and_restores(tmp_config_dir, monkeypatch):
    for k in ("LANGCHAIN_AGENT_BASE_URL", "LANGCHAIN_AGENT_API_KEY",
              "LANGCHAIN_AGENT_PROTOCOL", "LANGCHAIN_AGENT_MODEL"):
        monkeypatch.delenv(k, raising=False)

    with bridge._web_turn_env(
        user_id="u-alice", model_id="custom/gpt-5.4",
        base_url="https://x.test/v1", api_key="sk-z", protocol="anthropic",
    ):
        assert os.environ["LANGCHAIN_AGENT_MODEL"] == "custom/gpt-5.4"
        assert os.environ["LANGCHAIN_AGENT_BASE_URL"] == "https://x.test/v1"
        assert os.environ["LANGCHAIN_AGENT_API_KEY"] == "sk-z"
        assert os.environ["LANGCHAIN_AGENT_PROTOCOL"] == "anthropic"

    # all removed afterwards (were unset before)
    for k in ("LANGCHAIN_AGENT_BASE_URL", "LANGCHAIN_AGENT_API_KEY",
              "LANGCHAIN_AGENT_PROTOCOL", "LANGCHAIN_AGENT_MODEL"):
        assert k not in os.environ


def test_web_turn_env_no_endpoint_leaves_custom_vars_unset(tmp_config_dir, monkeypatch):
    for k in ("LANGCHAIN_AGENT_BASE_URL", "LANGCHAIN_AGENT_API_KEY",
              "LANGCHAIN_AGENT_PROTOCOL"):
        monkeypatch.delenv(k, raising=False)
    with bridge._web_turn_env(user_id="u-bob", model_id="openai/gpt-4o"):
        assert "LANGCHAIN_AGENT_BASE_URL" not in os.environ
        assert "LANGCHAIN_AGENT_API_KEY" not in os.environ
        assert "LANGCHAIN_AGENT_PROTOCOL" not in os.environ
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web/test_bridge.py -k custom_endpoint -v`
Expected: FAIL — `TypeError: _web_turn_env() got an unexpected keyword argument 'base_url'`.

- [ ] **Step 3: Extend `_web_turn_env`**

Replace `_web_turn_env` in `web/bridge.py` with:

```python
@contextlib.contextmanager
def _web_turn_env(
    *, user_id: str, model_id: str,
    base_url: str = "", api_key: str = "", protocol: str = "",
) -> Iterator[Path]:
    """Set the per-turn env (memory user, forced workspace-write, per-user
    workspace root, selected model, and — for custom endpoints — base_url /
    api_key / protocol) and restore the prior values on exit."""
    keys = (
        "LANGCHAIN_AGENT_MEMORY_USER",
        "LANGCHAIN_AGENT_PERMISSION_MODE",
        "LANGCHAIN_AGENT_WORKSPACE_ROOT",
        "LANGCHAIN_AGENT_MODEL",
        "LANGCHAIN_AGENT_BASE_URL",
        "LANGCHAIN_AGENT_API_KEY",
        "LANGCHAIN_AGENT_PROTOCOL",
    )
    prev = {k: os.environ.get(k) for k in keys}
    ws = _user_workspace(user_id)
    try:
        _set_or_clear("LANGCHAIN_AGENT_MEMORY_USER", user_id or None)
        _set_or_clear("LANGCHAIN_AGENT_PERMISSION_MODE", config.WEB_PERMISSION_MODE)
        _set_or_clear("LANGCHAIN_AGENT_WORKSPACE_ROOT", str(ws))
        _set_or_clear("LANGCHAIN_AGENT_MODEL", model_id or None)
        _set_or_clear("LANGCHAIN_AGENT_BASE_URL", base_url or None)
        _set_or_clear("LANGCHAIN_AGENT_API_KEY", api_key or None)
        _set_or_clear("LANGCHAIN_AGENT_PROTOCOL", protocol or None)
        yield ws
    finally:
        for k, v in prev.items():
            _set_or_clear(k, v)
```

- [ ] **Step 4: Thread the params through the three callers**

In `web/bridge.py`:

(a) `run_turn_streaming` signature + inner call:

```python
async def run_turn_streaming(
    prompt: str,
    *,
    trace_id: str = "web1",
    session_key: str = "",
    user_id: str = "",
    model_id: str = "",
    base_url: str = "",
    api_key: str = "",
    protocol: str = "",
) -> AsyncIterator[dict[str, Any]]:
```

and change its inner `_stream_off_loop(...)` call to pass them:

```python
        async for ev in _stream_off_loop(
            prompt,
            trace_id=trace_id,
            session_key=session_key,
            user_id=user_id,
            model_id=model_id,
            base_url=base_url,
            api_key=api_key,
            protocol=protocol,
        ):
            yield ev
```

(b) `_stream_off_loop` signature + its inner `_run_streaming_locked(...)` call (inside `_produce`):

```python
async def _stream_off_loop(
    prompt: str, *, trace_id: str, session_key: str, user_id: str, model_id: str,
    base_url: str = "", api_key: str = "", protocol: str = "",
) -> AsyncIterator[dict[str, Any]]:
```

```python
                async for ev in _run_streaming_locked(
                    prompt,
                    trace_id=trace_id,
                    session_key=session_key,
                    user_id=user_id,
                    model_id=model_id,
                    base_url=base_url,
                    api_key=api_key,
                    protocol=protocol,
                ):
```

(c) `_run_streaming_locked` signature + its `_web_turn_env(...)` call:

```python
async def _run_streaming_locked(
    prompt: str, *, trace_id: str, session_key: str, user_id: str, model_id: str,
    base_url: str = "", api_key: str = "", protocol: str = "",
) -> AsyncIterator[dict[str, Any]]:
```

```python
    with _web_turn_env(
        user_id=user_id, model_id=model_id,
        base_url=base_url, api_key=api_key, protocol=protocol,
    ):
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_web/test_bridge.py -v`
Expected: PASS (existing bridge tests + the 2 new env tests; the existing `test_web_turn_env_*` calls without endpoint args still pass because the new params default to `""`).

- [ ] **Step 6: Commit**

```bash
git add web/bridge.py tests/test_web/test_bridge.py
git commit -m "feat(web): thread custom endpoint base_url/key/protocol through the turn"
```

---

## Task 6: Frontend — endpoint picker + add/delete modal

**Files:**
- Modify: `web/static/index.html` (header control + modal markup)
- Modify: `web/static/app.js` (load endpoints, render picker, modal wiring, send endpoint_id)

No automated test (no JS harness in this repo) — verified manually in Step 4.

- [ ] **Step 1: Add header control + modal markup to `index.html`**

Replace the `<header class="chat-header">` block:

```html
      <header class="chat-header">
        <select id="model-select"></select>
        <button id="btn-endpoints" class="icon-btn" title="自定义端点">⚙</button>
        <span id="conv-title"></span>
      </header>
```

And add the modal just before the closing `</div>` of `#app-view` (after `</main>`):

```html
    <div id="endpoint-modal" class="modal hidden">
      <div class="modal-card">
        <button id="btn-close-endpoint" class="modal-close" aria-label="关闭">✕</button>
        <h2>自定义端点</h2>
        <ul id="endpoint-list" class="endpoint-list"></ul>
        <div class="endpoint-form">
          <input id="ep-label" placeholder="名称(如 My LLM)" />
          <input id="ep-base-url" placeholder="Base URL(如 https://api.openai.com/v1)" />
          <input id="ep-api-key" type="password" placeholder="API Key" />
          <input id="ep-model" placeholder="模型名(如 gpt-5.4)" />
          <select id="ep-protocol">
            <option value="openai">openai</option>
            <option value="anthropic">anthropic</option>
          </select>
          <div id="ep-error" class="error"></div>
          <button id="btn-save-endpoint">保存并使用</button>
        </div>
      </div>
    </div>
```

- [ ] **Step 2: Update `app.js` — state, model loading, picker, modal, send**

In `web/static/app.js`:

(a) Add `endpoints: []` to the initial state:

```javascript
let state = { user: null, convs: [], activeConv: null, models: [], endpoints: [], domCache: {} };
```

(b) Replace `loadModels` with a version that loads both and renders the picker:

```javascript
async function loadModels() {
  const [mr, er] = await Promise.all([api("/api/models"), api("/api/endpoints")]);
  state.models = mr.ok ? await mr.json() : [];
  state.endpoints = er.ok ? await er.json() : [];
  renderModelSelect();
}

function renderModelSelect() {
  const sel = $("model-select");
  const prev = sel.value;
  const presets = state.models
    .map((m) => `<option value="preset:${m.id}">${escapeHtml(m.label)} · ${escapeHtml(m.model)}</option>`)
    .join("");
  const customs = state.endpoints
    .map((e) => `<option value="endpoint:${e.id}">★ ${escapeHtml(e.label)} · ${escapeHtml(e.model)}</option>`)
    .join("");
  sel.innerHTML =
    `<optgroup label="预置">${presets}</optgroup>` +
    (customs ? `<optgroup label="自定义">${customs}</optgroup>` : "");
  if (prev) sel.value = prev;  // keep the current selection across refreshes
}
```

(c) Add the modal functions (place near `renderProcess`):

```javascript
function openEndpoints() {
  renderEndpointList();
  $("ep-error").textContent = "";
  $("endpoint-modal").classList.remove("hidden");
}
function closeEndpoints() { $("endpoint-modal").classList.add("hidden"); }

function renderEndpointList() {
  const ul = $("endpoint-list");
  ul.innerHTML = "";
  if (!state.endpoints.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "暂无自定义端点";
    ul.append(li);
    return;
  }
  for (const e of state.endpoints) {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.textContent = `${e.label} · ${e.model} · ${e.base_url}`;
    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "✕";
    del.onclick = async () => {
      await api(`/api/endpoints/${e.id}`, { method: "DELETE" });
      await loadModels();
      renderEndpointList();
    };
    li.append(span, del);
    ul.append(li);
  }
}

async function saveEndpoint() {
  $("ep-error").textContent = "";
  const body = {
    label: $("ep-label").value.trim(),
    base_url: $("ep-base-url").value.trim(),
    api_key: $("ep-api-key").value,
    model: $("ep-model").value.trim(),
    protocol: $("ep-protocol").value,
  };
  const r = await api("/api/endpoints", { method: "POST", body: JSON.stringify(body) });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    $("ep-error").textContent = e.detail || "保存失败";
    return;
  }
  const created = await r.json();
  await loadModels();
  $("model-select").value = `endpoint:${created.id}`;
  for (const id of ["ep-label", "ep-base-url", "ep-api-key", "ep-model"]) $(id).value = "";
  closeEndpoints();
}
```

(d) Replace the body-build in `sendMessage` that reads the model. Change:

```javascript
  const resp = await api(`/api/conversations/${state.activeConv}/messages`, {
    method: "POST",
    body: JSON.stringify({ content: text, model: $("model-select").value }),
  });
```

to:

```javascript
  const raw = $("model-select").value || "";
  const i = raw.indexOf(":");
  const kind = i >= 0 ? raw.slice(0, i) : "";
  const val = i >= 0 ? raw.slice(i + 1) : raw;
  const payload = { content: text };
  if (kind === "endpoint") payload.endpoint_id = val;
  else payload.model = val;
  const resp = await api(`/api/conversations/${state.activeConv}/messages`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
```

(e) Wire the new buttons (with the other `$("...").onclick` lines near the bottom):

```javascript
$("btn-endpoints").onclick = openEndpoints;
$("btn-close-endpoint").onclick = closeEndpoints;
$("btn-save-endpoint").onclick = saveEndpoint;
```

- [ ] **Step 3: Manual smoke test (golden path + edge cases)**

Start the server (use the existing launcher):

```bash
python -m web
```

In a browser at the served URL:
1. Register/login. Confirm the model `<select>` lists preset providers under "预置".
2. Click ⚙ → modal opens. Add an endpoint (Label + Base URL + a real key + model + protocol). Confirm it appears under "自定义" and is auto-selected.
3. Send a message with the custom endpoint selected; confirm a streamed reply (or a clean error event if the key/URL is wrong — not a hang).
4. Switch back to a preset; send a message; confirm it still works.
5. Reopen ⚙, delete the endpoint, confirm it disappears from the modal list and the `<select>`.
6. Edge: submit the add form with an empty field → inline "label, base_url, model, api_key required" (400 surfaced in `#ep-error`), modal stays open.

Note: if the UI can't be exercised in a browser in this environment, say so explicitly rather than reporting success.

- [ ] **Step 4: Commit**

```bash
git add web/static/index.html web/static/app.js
git commit -m "feat(web): custom endpoint picker + add/delete modal"
```

---

## Task 7: Reskin to the Hermes Teal terminal aesthetic

**Files:**
- Create: `web/static/fonts/JetBrainsMono-Regular.woff2`, `JetBrainsMono-Bold.woff2` (copied, Apache-2.0)
- Modify: `web/static/styles.css` (full rewrite)
- Modify: `web/static/index.html` (no new fonts CDN needed; keep marked/highlight CDN)

Visual change — verified manually.

- [ ] **Step 1: Bundle the monospace font**

```bash
mkdir -p web/static/fonts
cp "D:/something_all_in/面试/hermes-agent/web/public/fonts-terminal/JetBrainsMono-Regular.woff2" web/static/fonts/
cp "D:/something_all_in/面试/hermes-agent/web/public/fonts-terminal/JetBrainsMono-Bold.woff2" web/static/fonts/
ls web/static/fonts
```

Expected: both `.woff2` files listed.

- [ ] **Step 2: Rewrite `web/static/styles.css`**

Replace the entire file with:

```css
@font-face {
  font-family: "JetBrains Mono";
  font-weight: 400;
  font-display: swap;
  src: url("/static/fonts/JetBrainsMono-Regular.woff2") format("woff2");
}
@font-face {
  font-family: "JetBrains Mono";
  font-weight: 700;
  font-display: swap;
  src: url("/static/fonts/JetBrainsMono-Bold.woff2") format("woff2");
}

:root {
  --background: #041c1c;          /* deep teal canvas */
  --surface: #07292a;             /* card / sidebar */
  --foreground: #ffe6cb;          /* cream */
  --muted: rgba(255, 230, 203, 0.55);
  --border: rgba(255, 230, 203, 0.15);
  --accent: #ffbd38;              /* warm glow accent */
  --warm-glow: rgba(255, 189, 56, 0.18);
  --destructive: #fb2c36;
  --success: #4ade80;
  --mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  height: 100vh;
  font-family: var(--sans);
  color: var(--foreground);
  background:
    radial-gradient(120% 80% at 50% -10%, var(--warm-glow), transparent 60%),
    var(--background);
}
.hidden { display: none !important; }

h1, h2, .label-display {
  text-transform: uppercase;
  letter-spacing: 0.12em;
  font-weight: 700;
}

/* Buttons */
button {
  background: transparent;
  color: var(--foreground);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 8px 14px;
  cursor: pointer;
  font-family: var(--sans);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-size: 12px;
  transition: background 0.12s, border-color 0.12s;
}
button:hover { background: rgba(255, 230, 203, 0.08); border-color: var(--accent); }
.icon-btn { padding: 6px 9px; }

input, textarea, select {
  background: var(--background);
  color: var(--foreground);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 9px 10px;
  font-family: var(--mono);
  font-size: 13px;
}
input:focus, textarea:focus, select:focus { outline: none; border-color: var(--accent); }

/* Auth */
.auth-view { display: flex; align-items: center; justify-content: center; height: 100vh; }
.auth-card {
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 32px;
  border-radius: 6px;
  width: 340px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.auth-card h1 { margin: 0 0 8px; text-align: center; font-size: 20px; }
.auth-actions { display: flex; gap: 8px; }
.auth-actions button { flex: 1; }
.error { color: var(--destructive); font-size: 12px; min-height: 16px; font-family: var(--mono); }

/* App layout */
.app-view { display: flex; height: 100vh; }
.sidebar {
  width: 264px;
  background: var(--surface);
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border);
}
.new-conv { margin: 12px; }
.conv-list { list-style: none; margin: 0; padding: 0; flex: 1; overflow-y: auto; }
.conv-list li {
  padding: 10px 14px;
  cursor: pointer;
  display: flex;
  justify-content: space-between;
  gap: 6px;
  border-left: 2px solid transparent;
  font-size: 14px;
}
.conv-list li.active { background: rgba(255, 230, 203, 0.06); border-left-color: var(--accent); }
.conv-list li .del { opacity: 0.5; }
.conv-list li .del:hover { opacity: 1; color: var(--destructive); }
.sidebar-footer {
  padding: 12px 14px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-top: 1px solid var(--border);
  font-size: 12px;
  font-family: var(--mono);
}

.chat { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.chat-header {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: 10px;
  align-items: center;
}
.chat-header #conv-title { color: var(--muted); font-family: var(--mono); font-size: 13px; }

.messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 18px; }
.msg { max-width: 780px; line-height: 1.55; }
.msg.user {
  align-self: flex-end;
  background: rgba(255, 230, 203, 0.08);
  border: 1px solid var(--border);
  padding: 10px 14px;
  border-radius: 6px;
}
.msg.assistant { align-self: flex-start; }
.msg .process {
  font-size: 12px;
  color: var(--muted);
  border-left: 2px solid var(--border);
  padding-left: 10px;
  margin-bottom: 8px;
  font-family: var(--mono);
}
.msg .process summary { cursor: pointer; }
.msg .typing { color: var(--muted); animation: pulse 1.2s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 0.4; } 50% { opacity: 1; } }
.msg pre {
  background: #021414;
  border: 1px solid var(--border);
  padding: 12px;
  border-radius: 6px;
  overflow-x: auto;
  position: relative;
  font-family: var(--mono);
}
.msg code { font-family: var(--mono); }
.msg pre .copy { position: absolute; top: 6px; right: 6px; font-size: 11px; padding: 4px 8px; }

.composer { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--border); }
.composer textarea {
  flex: 1;
  resize: none;
  height: 54px;
  font-family: var(--sans);
}

/* Endpoints modal */
.modal {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(4, 28, 28, 0.85);
  backdrop-filter: blur(3px);
  padding: 16px;
}
.modal-card {
  position: relative;
  width: 100%;
  max-width: 520px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 24px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
}
.modal-card h2 { margin: 0 0 16px; font-size: 15px; }
.modal-close { position: absolute; right: 12px; top: 12px; border: 0; padding: 4px 8px; }
.endpoint-list { list-style: none; margin: 0 0 16px; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.endpoint-list li {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  font-family: var(--mono);
  font-size: 12px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
}
.endpoint-list li.empty { color: var(--muted); justify-content: center; border-style: dashed; }
.endpoint-list .del { border: 0; padding: 2px 8px; color: var(--muted); }
.endpoint-list .del:hover { color: var(--destructive); }
.endpoint-form { display: flex; flex-direction: column; gap: 10px; }
.endpoint-form #btn-save-endpoint { margin-top: 4px; align-self: flex-end; }
```

- [ ] **Step 3: Confirm `index.html` head is consistent**

No edit required if Task 6's markup is in place. Verify the existing `<link rel="stylesheet" href="/static/styles.css" />` and the marked/highlight.js CDN `<script>` tags are unchanged (the github-dark highlight theme already reads well on the teal canvas).

- [ ] **Step 4: Manual visual verification**

```bash
python -m web
```

In a browser:
1. Auth card: cream-on-teal, uppercase title, warm glow at top.
2. Chat view: teal canvas, cream text, bordered sidebar, active conversation has an accent left-border.
3. Assistant code blocks: monospace (JetBrains Mono), dark bordered panel, copy button visible.
4. ⚙ modal: matches the aesthetic (dark teal overlay, bordered card, monospace endpoint rows).
5. Streaming, `过程(n)` disclosure, markdown rendering all still work.

If a browser isn't available in this environment, state that explicitly instead of claiming the visual passed.

- [ ] **Step 5: Commit**

```bash
git add web/static/styles.css web/static/fonts/ web/static/index.html
git commit -m "feat(web): reskin to Hermes Teal terminal aesthetic"
```

---

## Final verification

- [ ] **Run the full web + touched suites**

Run: `python -m pytest tests/test_web tests/test_config_overrides.py tests/test_orchestrator/test_mcp_host.py -q -m "not e2e"`
Expected: all PASS.

- [ ] **Confirm no stray debug / leftover changes**

Run: `git status --short`
Expected: only the files this plan touched, all committed.

---

## Self-review notes (author)

- **Spec coverage:** A1 reskin → Task 7; B1 store → Task 3; B2 routes → Task 4; B3 bridge env → Task 5; B4 config overrides → Task 1; B5 passthrough → Task 2; B6 frontend → Task 6. All spec sections map to a task.
- **Deviations from spec (intentional, minor):** `endpoints.created_at` is `INTEGER` (matches the store's existing `_now()` timestamps) rather than `TEXT`; `list_endpoints` simply omits `api_key` rather than adding a `has_key` flag (endpoints always have a key, so the flag carries no information). Editing endpoints remains out of scope (add + delete only).
- **Type consistency:** the bridge param trio `base_url` / `api_key` / `protocol` is named identically across `web/app.py` (`bridge_kwargs`), `run_turn_streaming`, `_stream_off_loop`, `_run_streaming_locked`, and `_web_turn_env`. Store functions `create_endpoint` / `list_endpoints` / `get_endpoint` / `delete_endpoint` match their call sites in `web/app.py`.
