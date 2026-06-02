# Phase C: security gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing (but scattered and partly untested) security properties into an explicit, regression-proof gate: a `tests/test_security/` suite, a single `web.config.assert_safe_for_exposure()` self-check, and a `bandit` CI job with a checked-in baseline.

**Architecture:** No new runtime behavior — Phase C makes existing protections *enforced and tested*. The SSRF base_url guard (`web/app.py:_assert_safe_base_url` → `tool.tool_web.hostname_is_safe`), API-key encryption at rest (`web/crypto.py` Fernet), and the network-bind refusal (`web/__main__.py`) already exist; this phase consolidates the assertions, moves the bind refusal into `web.config` so both `__main__` and any embedder call one function, and adds a static-scan CI gate.

**Tech Stack:** Python 3.11+, pytest, FastAPI TestClient, bandit. New dev dependency: `bandit`.

**Spec:** `docs/superpowers/specs/2026-06-01-multi-user-concurrency-turncontext-design.md` (Phase C, lines 184–202).

**Scope:** Independent of A/B. Web surface security posture only.

---

## Current state (verified)

- `web/crypto.py`: `encrypt_secret`/`decrypt_secret` (Fernet, key derived from `config.auth_secret()`). Store holds ciphertext; `list_endpoints` never selects `api_key`.
- `web/app.py:_assert_safe_base_url` rejects private/loopback/link-local/metadata hosts at endpoint-create AND re-checks at chat-time (rebinding). Uses `tool.tool_web.hostname_is_safe`.
- `web/__main__.py:_is_loopback` + an inline guard that refuses a non-loopback bind without `WEB_AUTH_SECRET` + `WEB_SIGNUP_CODE`. **Untested**, and the logic lives in `__main__` (not reusable).
- CI (`.github/workflows/ci.yml`): test matrix + e2e + mypy. No static security scan.

---

## File Structure

**Create:**
- `tests/test_security/__init__.py` (empty, if the suite needs it — most pytest layouts don't; create only if collection fails).
- `tests/test_security/test_ssrf_base_url.py`
- `tests/test_security/test_auth_and_secrets.py`
- `tests/test_security/test_exposure_self_check.py`
- `.bandit` (or `pyproject.toml [tool.bandit]`) baseline config.

**Modify:**
- `web/config.py` — add `assert_safe_for_exposure(host)` (+ a small `is_loopback` helper moved from `__main__`).
- `web/__main__.py` — call `config.assert_safe_for_exposure(args.host)` instead of the inline guard.
- `pyproject.toml` — add `bandit` to the `[dev]` extra.
- `.github/workflows/ci.yml` — add a `bandit` job.

---

## Task 1: `web.config.assert_safe_for_exposure()` + reuse in `__main__`

**Files:** Modify `web/config.py`, `web/__main__.py`; Test `tests/test_security/test_exposure_self_check.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_security/test_exposure_self_check.py
from __future__ import annotations

import pytest

from web import config


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_loopback_bind_is_always_safe(host, monkeypatch):
    monkeypatch.delenv("WEB_AUTH_SECRET", raising=False)
    monkeypatch.delenv("WEB_SIGNUP_CODE", raising=False)
    # Loopback stays zero-config: no raise regardless of secrets.
    config.assert_safe_for_exposure(host)


def test_non_loopback_without_secrets_refused(monkeypatch):
    monkeypatch.delenv("WEB_AUTH_SECRET", raising=False)
    monkeypatch.delenv("WEB_SIGNUP_CODE", raising=False)
    with pytest.raises(config.UnsafeExposureError) as ei:
        config.assert_safe_for_exposure("0.0.0.0")
    msg = str(ei.value)
    assert "WEB_AUTH_SECRET" in msg and "WEB_SIGNUP_CODE" in msg


def test_non_loopback_with_both_secrets_allowed(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_SECRET", "s3cret-value")
    monkeypatch.setenv("WEB_SIGNUP_CODE", "gate")
    config.assert_safe_for_exposure("0.0.0.0")  # no raise


def test_non_loopback_missing_one_secret_refused(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_SECRET", "s3cret-value")
    monkeypatch.delenv("WEB_SIGNUP_CODE", raising=False)
    with pytest.raises(config.UnsafeExposureError) as ei:
        config.assert_safe_for_exposure("192.168.1.10")
    assert "WEB_SIGNUP_CODE" in str(ei.value)
    assert "WEB_AUTH_SECRET" not in str(ei.value)  # only the missing one named
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_security/test_exposure_self_check.py -v`
Expected: FAIL — `module 'web.config' has no attribute 'UnsafeExposureError'`.

- [ ] **Step 3: Implement in `web/config.py`**

Add near the top (after imports):

```python
class UnsafeExposureError(RuntimeError):
    """Raised when the server would bind to a network-reachable address without
    the secrets required to expose it safely."""


def is_loopback(host: str) -> bool:
    """True for binds reachable only from the local machine. ``0.0.0.0`` / ``::``
    (all-interfaces) and any concrete LAN/public address are NOT loopback."""
    h = (host or "").strip().strip("[]").lower()
    return h in ("127.0.0.1", "localhost", "::1")


def assert_safe_for_exposure(host: str) -> None:
    """Refuse a network-exposed bind without the mandatory secrets.

    On a non-loopback bind, anyone who can reach the port can register an
    account and drive a workspace-write agent (shell/file/python tools), so a
    persistent JWT secret AND a registration gate are required. Loopback binds
    stay zero-config for local dev. Single source of truth for "safe to expose"
    — both ``web.__main__`` and any embedding server call this."""
    if is_loopback(host):
        return
    missing = [
        name for name in ("WEB_AUTH_SECRET", "WEB_SIGNUP_CODE")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise UnsafeExposureError(
            f"Refusing to bind {host} (network-exposed) without "
            f"{' and '.join(missing)} set. Set them, or bind 127.0.0.1 for "
            "local-only use."
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_security/test_exposure_self_check.py -v`
Expected: PASS (7 cases)

- [ ] **Step 5: Refactor `web/__main__.py` to use it**

Replace the inline `_is_loopback` + bind-guard block. Remove the local `_is_loopback` def and the `if not _is_loopback(args.host): missing = [...]; if missing: print(...); return 2` block, replacing with:

```python
    from web import config

    # Single source of truth for "safe to expose" (see config.assert_safe_for_exposure).
    try:
        config.assert_safe_for_exposure(args.host)
    except config.UnsafeExposureError as exc:
        print(str(exc), file=sys.stderr)
        return 2
```

- [ ] **Step 6: Run the web + new security suites**

Run: `python -m pytest tests/test_web/ tests/test_security/ -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add web/config.py web/__main__.py tests/test_security/test_exposure_self_check.py
git commit -m "feat(web): assert_safe_for_exposure() — single source of truth for network-bind safety

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: SSRF base_url regression suite

**Files:** Test `tests/test_security/test_ssrf_base_url.py`.

- [ ] **Step 1: Write the test**

```python
# tests/test_security/test_ssrf_base_url.py
from __future__ import annotations

import pytest
from fastapi import HTTPException

from web.app import _assert_safe_base_url


@pytest.mark.parametrize("base_url", [
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://127.0.0.1:11434/v1",                  # loopback
    "http://localhost/v1",                        # loopback name
    "http://10.0.0.5/v1",                         # private (RFC1918)
    "http://192.168.1.10/v1",                     # private
    "http://172.16.0.1/v1",                       # private
    "http://[::1]/v1",                            # ipv6 loopback
])
def test_private_and_metadata_base_urls_rejected(base_url, monkeypatch):
    # Ensure the dev escape hatch is OFF so the guard is active.
    monkeypatch.delenv("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS", raising=False)
    with pytest.raises(HTTPException) as ei:
        _assert_safe_base_url(base_url)
    assert ei.value.status_code == 400
    assert "not allowed" in ei.value.detail


def test_public_base_url_allowed(monkeypatch):
    monkeypatch.delenv("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS", raising=False)
    # A public host must pass (no raise). Uses a stable public DNS name.
    _assert_safe_base_url("https://api.openai.com/v1")


def test_escape_hatch_allows_private(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS", "1")
    # With the documented dev escape hatch, a localhost endpoint is allowed.
    _assert_safe_base_url("http://127.0.0.1:11434/v1")
```

- [ ] **Step 2: Run the test**

Run: `python -m pytest tests/test_security/test_ssrf_base_url.py -v`
Expected: PASS. If `test_public_base_url_allowed` fails because the CI sandbox blocks DNS, change it to assert on a host that resolves offline, or mark it `@pytest.mark.skipif` on no-network — but first try as-is (the guard resolves DNS; a public name normally resolves).

> If the public-URL case is flaky offline, replace it with a monkeypatch of `tool.tool_web.hostname_is_safe` to return `(True, "")` and assert no raise — keeping the test hermetic.

- [ ] **Step 3: Commit**

```bash
git add tests/test_security/test_ssrf_base_url.py
git commit -m "test(security): SSRF base_url rejection (private/loopback/metadata) + escape hatch

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: auth, signup-gate, JWT-secret stability, key-at-rest encryption

**Files:** Test `tests/test_security/test_auth_and_secrets.py`.

- [ ] **Step 1: Write the test**

```python
# tests/test_security/test_auth_and_secrets.py
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
    plaintext = "sk-super-secret-value-123"
    store.create_endpoint(db_path, user_id="u1", label="e", base_url="https://api.x/v1",
                          api_key=crypto.encrypt_secret(plaintext), model="m", protocol="openai")
    # Read the raw column straight from sqlite — must NOT be the plaintext.
    with sqlite3.connect(db_path) as conn:
        rows = [r[0] for r in conn.execute("SELECT api_key FROM endpoints").fetchall()]
    assert rows and plaintext not in rows[0]
    # And it must round-trip back to the plaintext in memory.
    assert crypto.decrypt_secret(rows[0]) == plaintext


def test_list_endpoints_never_returns_key(db_path, tmp_config_dir):
    store.init_db(db_path)
    store.create_endpoint(db_path, user_id="u1", label="e", base_url="https://api.x/v1",
                          api_key=crypto.encrypt_secret("sk-x"), model="m", protocol="openai")
    listed = store.list_endpoints(db_path, "u1")
    assert listed and all("api_key" not in row for row in listed)
```

- [ ] **Step 2: Run the test**

Run: `python -m pytest tests/test_security/test_auth_and_secrets.py -v`
Expected: PASS. If `/api/models` returns 200 without auth (it may be public), drop it from the parametrize list — verify by reading the route in `web/app.py` (Grep `"/api/models"`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_security/test_auth_and_secrets.py
git commit -m "test(security): auth-required routes, signup gate, JWT stability, API-key-at-rest encryption

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: bandit static-scan CI job + baseline

**Files:** Modify `pyproject.toml`, `.github/workflows/ci.yml`; Create bandit config.

- [ ] **Step 1: Add bandit to the dev extra**

In `pyproject.toml`, add `"bandit"` to the `[project.optional-dependencies] dev = [...]` list (match the existing formatting).

- [ ] **Step 2: Add a bandit config that scopes the scan and silences known-safe patterns**

In `pyproject.toml`, add:

```toml
[tool.bandit]
# Scan first-party packages only; skip tests (asserts, subprocess fixtures).
exclude_dirs = ["tests", ".venv", "build", "dist"]
# B101 assert_used: we use asserts as invariants in non-test code sparingly;
# B404/B603/B607 subprocess: spawning specialist subprocesses is the product.
skips = ["B101", "B404", "B603", "B607"]
```

- [ ] **Step 3: Establish the baseline locally and confirm a clean run**

Run:
```bash
pip install bandit
bandit -c pyproject.toml -r . -q
```
Expected: exit 0 (no findings) given the skips. If real findings surface, triage: fix true issues; for accepted ones add a targeted `# nosec BXXX` with a reason, or extend `skips` with a comment. Re-run until exit 0.

- [ ] **Step 4: Add the CI job**

In `.github/workflows/ci.yml`, add a job mirroring the `mypy` job's shape:

```yaml
  bandit:
    name: bandit (static security scan)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: pyproject.toml
      - run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      - name: Run bandit (first-party, config-scoped)
        run: bandit -c pyproject.toml -r . -q
```

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .github/workflows/ci.yml
git commit -m "ci(security): add bandit static-scan job with a scoped baseline

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: full-suite regression gate

- [ ] **Step 1: Run the entire suite**

Run: `python -m pytest -q`
Expected: all pass (prior gate: 800 passed, 1 skipped; the security tests add to the count).

- [ ] **Step 2: Run bandit once more as the gate would**

Run: `bandit -c pyproject.toml -r . -q`
Expected: exit 0.

- [ ] **Step 3: Commit any fixups**

```bash
git add -A
git commit -m "test: Phase C security-gate fixups

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **No runtime behavior change** beyond moving the bind-refusal into `config` (identical logic). The value is the *gate*: tests + one self-check + CI scan.
- **Hermetic tests:** prefer monkeypatching `hostname_is_safe` over real DNS where a public-URL assertion would be network-dependent.
- **bandit is additive:** the scoped `skips` reflect intentional product behavior (subprocess spawning is the architecture). Keep the skip list small and commented so it gates *new* findings.
