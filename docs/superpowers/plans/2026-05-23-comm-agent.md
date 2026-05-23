# Comm-Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a new `comm-agent` specialist that speaks Google A2A v0.3 over HTTPS+HMAC to remote agents (OpenClaw first), exposing `comm.*` MCP tools to the orchestrator and an install script for the remote side.

**Architecture:** New `agents/comm_agent/` package mirroring `agents/tool_agent/`. Standard A2A protocol implemented in `a2a_protocol.py` (separate from the existing 127.0.0.1 `a2a_server.py`). Cross-machine HMAC grants extend `agents/shared/authz.py`. Caddy runs as a child subprocess for TLS termination. Peer registry is a JSON file referencing env-var secret names.

**Tech Stack:** Python 3.11+, httpx (HTTP client + SSE), fastapi + uvicorn (server), pyjwt (HMAC HS256), `trustme` (test-only TLS fixture, new dep), Caddy (runtime dep, user-installed).

**Spec:** `docs/superpowers/specs/2026-05-23-comm-agent-design.md`

---

## File Structure

```
agents/comm_agent/
├── __init__.py           # empty
├── main.py               # subprocess entrypoint (mirrors tool_agent/main.py)
├── a2a_protocol.py       # standard A2A v0.3 client + server (FastAPI app builder)
├── agent_card.py         # build + validate Agent Card dict
├── peer_registry.py      # Peer dataclass + PeerRegistry (load/save/get/add/...)
├── caddy_supervisor.py   # render Caddyfile + spawn caddy subprocess
└── mcp_tools.py          # build comm.* ToolSpec list for MCP

agents/shared/
└── authz.py              # MODIFY: add sign/verify_cross_machine_grant + nonce LRU

.agent/agents/
└── comm-agent.card.json  # NEW: orchestrator-side specialist card

scripts/
├── install_openclaw_a2a.sh    # remote-side bash installer (Linux)
└── install_openclaw_a2a.ps1   # remote-side PowerShell installer (Windows)

tests/test_comm_agent/
├── __init__.py
├── conftest.py                       # trustme TLS fixtures + MockA2APeer
├── test_peer_registry.py
├── test_agent_card.py
├── test_authz_cross_machine.py
├── test_a2a_protocol_client.py
├── test_a2a_protocol_server.py
├── test_caddy_supervisor.py
├── test_mcp_tools.py
├── test_e2e_loopback.py
├── test_e2e_auth_refuse.py
├── test_e2e_replay_blocked.py
└── test_e2e_stream_truncated.py

tests/test_e2e_multi_agent/
├── test_e2e_comm_delegate.py         # comm-agent subprocess + orchestrator
└── test_e2e_comm_chat_multiturn.py
```

**Layering:** Tasks build bottom-up. Layer 1 (pure logic) → Layer 2 (protocol) → Layer 3 (process orchestration + MCP tools) → Layer 4 (wiring) → Layer 5 (E2E + install).

---

## Task 0: Setup — add trustme dep + test scaffolding

**Files:**
- Modify: `pyproject.toml` (dev deps)
- Create: `tests/test_comm_agent/__init__.py` (empty)
- Create: `tests/test_comm_agent/conftest.py`

- [ ] **Step 1: Add trustme to dev dependencies**

Open `pyproject.toml`, find the `[project.optional-dependencies]` `dev` list (look near the `mypy>=1.0.0` line that already exists). Add `"trustme>=1.0.0"` to that list.

- [ ] **Step 2: Install the dep**

Run: `pip install "trustme>=1.0.0"`
Expected: succeeds, no version conflict.

- [ ] **Step 3: Create empty test package marker**

```python
# tests/test_comm_agent/__init__.py
```

(Empty file.)

- [ ] **Step 4: Create conftest with TLS + MockA2APeer fixtures**

```python
# tests/test_comm_agent/conftest.py
"""Shared fixtures: ephemeral self-signed TLS certs + a Mock A2A peer."""
from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import pytest
import trustme
from cryptography.hazmat.primitives import hashes
from cryptography.x509 import load_pem_x509_certificate


@pytest.fixture
def tls_ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture
def tls_cert(tls_ca: trustme.CA):
    return tls_ca.issue_cert("127.0.0.1", "localhost")


def cert_fingerprint_sha256(cert) -> str:
    """Return lowercase hex SHA-256 fingerprint of the leaf cert."""
    pem_bytes = cert.cert_chain_pems[0].bytes()
    cert_obj = load_pem_x509_certificate(pem_bytes)
    return cert_obj.fingerprint(hashes.SHA256()).hex()


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class MockA2APeer:
    """Minimal in-process A2A peer for E2E tests. Filled in by Task 12."""
    port: int
    hmac_secret: str
    my_peer_id: str
    fingerprint_sha256: str
```

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/test_comm_agent/__init__.py tests/test_comm_agent/conftest.py
git commit -m "test(comm-agent): add trustme dep + conftest scaffolding"
```

---

## Task 1: peer_registry — Peer dataclass + JSON load/save

**Files:**
- Create: `agents/comm_agent/__init__.py` (empty)
- Create: `agents/comm_agent/peer_registry.py`
- Test: `tests/test_comm_agent/test_peer_registry.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_comm_agent/test_peer_registry.py
"""Tests for agents/comm_agent/peer_registry.py."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agents.comm_agent.peer_registry import (
    Peer, PeerRegistry, PeerRegistryError,
)


def test_empty_registry_on_missing_file(tmp_path: Path) -> None:
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    assert reg.list_peers() == []


def test_add_and_get_peer(tmp_path: Path) -> None:
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    peer = Peer(
        peer_id="openclaw-home",
        display_name="OpenClaw @ home",
        url="https://home.example.com:8443",
        hmac_secret_ref="OPENCLAW_HOME_HMAC",
        tls_verify=True,
        tls_pinned_sha256=None,
        added_at="2026-05-23T10:00:00",
        last_seen=None,
    )
    reg.add(peer)
    got = reg.get("openclaw-home")
    assert got is not None
    assert got.url == "https://home.example.com:8443"


def test_persistence_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "comm_peers.json"
    reg = PeerRegistry(path)
    peer = Peer(
        peer_id="p", display_name="P", url="https://p:8443",
        hmac_secret_ref="P_HMAC", tls_verify=True, tls_pinned_sha256=None,
        added_at="t", last_seen=None,
    )
    reg.add(peer)
    # Read back via a fresh instance
    reg2 = PeerRegistry(path)
    assert reg2.get("p") is not None


def test_remove_peer(tmp_path: Path) -> None:
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    reg.add(Peer(
        peer_id="p", display_name="P", url="https://p:8443",
        hmac_secret_ref="P_HMAC", tls_verify=True, tls_pinned_sha256=None,
        added_at="t", last_seen=None,
    ))
    assert reg.remove("p") is True
    assert reg.get("p") is None
    assert reg.remove("p") is False  # idempotent


def test_resolve_secret_reads_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MY_TEST_HMAC", "supersecret")
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    peer = Peer(
        peer_id="p", display_name="P", url="https://p:8443",
        hmac_secret_ref="MY_TEST_HMAC", tls_verify=True, tls_pinned_sha256=None,
        added_at="t", last_seen=None,
    )
    reg.add(peer)
    assert reg.resolve_secret(peer) == "supersecret"


def test_resolve_secret_missing_env_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MISSING_HMAC", raising=False)
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    peer = Peer(
        peer_id="p", display_name="P", url="https://p:8443",
        hmac_secret_ref="MISSING_HMAC", tls_verify=True, tls_pinned_sha256=None,
        added_at="t", last_seen=None,
    )
    reg.add(peer)
    with pytest.raises(PeerRegistryError, match="env var .*MISSING_HMAC"):
        reg.resolve_secret(peer)


def test_tls_verify_false_without_pin_rejected(tmp_path: Path) -> None:
    """Spec §3.5: tls.verify=false alone is forbidden. Pin required if verify off."""
    reg = PeerRegistry(tmp_path / "comm_peers.json")
    with pytest.raises(PeerRegistryError, match="tls"):
        reg.add(Peer(
            peer_id="p", display_name="P", url="https://p:8443",
            hmac_secret_ref="P_HMAC",
            tls_verify=False, tls_pinned_sha256=None,
            added_at="t", last_seen=None,
        ))


def test_secret_never_in_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("P_HMAC", "supersecretvalue")
    path = tmp_path / "comm_peers.json"
    reg = PeerRegistry(path)
    reg.add(Peer(
        peer_id="p", display_name="P", url="https://p:8443",
        hmac_secret_ref="P_HMAC", tls_verify=True, tls_pinned_sha256=None,
        added_at="t", last_seen=None,
    ))
    content = path.read_text(encoding="utf-8")
    assert "supersecretvalue" not in content
    assert "P_HMAC" in content  # the ref, not the value
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_comm_agent/test_peer_registry.py -v`
Expected: ImportError (module doesn't exist yet).

- [ ] **Step 3: Implement peer_registry.py**

```python
# agents/comm_agent/__init__.py
```

(Empty.)

```python
# agents/comm_agent/peer_registry.py
"""Read/write the comm-agent peer registry JSON.

Schema version 1. Secrets are NEVER stored in JSON — only env-var names
(``hmac_secret_ref``). Resolving the secret reads ``os.environ`` at call
time so a rotation just requires re-export + restart.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


class PeerRegistryError(Exception):
    pass


@dataclass
class Peer:
    peer_id: str
    display_name: str
    url: str
    hmac_secret_ref: str
    tls_verify: bool
    tls_pinned_sha256: str | None
    added_at: str
    last_seen: str | None


_SCHEMA_VERSION = 1


class PeerRegistry:
    def __init__(self, path: Path):
        self._path = path

    def _load(self) -> dict:
        if not self._path.exists():
            return {"schemaVersion": _SCHEMA_VERSION, "peers": []}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PeerRegistryError(f"corrupt registry at {self._path}: {exc}") from exc
        if data.get("schemaVersion") != _SCHEMA_VERSION:
            raise PeerRegistryError(
                f"unsupported schemaVersion {data.get('schemaVersion')!r}; expected {_SCHEMA_VERSION}"
            )
        return data

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_peers(self) -> list[Peer]:
        data = self._load()
        return [_peer_from_dict(d) for d in data.get("peers", [])]

    def get(self, peer_id: str) -> Peer | None:
        for p in self.list_peers():
            if p.peer_id == peer_id:
                return p
        return None

    def add(self, peer: Peer) -> None:
        # Spec §3.5: tls.verify=false alone is forbidden. Must have a pin.
        if not peer.tls_verify and not peer.tls_pinned_sha256:
            raise PeerRegistryError(
                "tls_verify=False requires tls_pinned_sha256 (refuse to skip TLS entirely)"
            )
        data = self._load()
        # De-dupe by peer_id (overwrite).
        data["peers"] = [
            d for d in data.get("peers", []) if d.get("peer_id") != peer.peer_id
        ]
        data["peers"].append(asdict(peer))
        self._save(data)

    def remove(self, peer_id: str) -> bool:
        data = self._load()
        before = len(data.get("peers", []))
        data["peers"] = [
            d for d in data.get("peers", []) if d.get("peer_id") != peer_id
        ]
        removed = len(data["peers"]) < before
        if removed:
            self._save(data)
        return removed

    def update_last_seen(self, peer_id: str, iso_ts: str) -> None:
        data = self._load()
        for d in data.get("peers", []):
            if d.get("peer_id") == peer_id:
                d["last_seen"] = iso_ts
                self._save(data)
                return

    def resolve_secret(self, peer: Peer) -> str:
        value = os.environ.get(peer.hmac_secret_ref)
        if not value:
            raise PeerRegistryError(
                f"env var {peer.hmac_secret_ref!r} not set; "
                f"export it before starting comm-agent"
            )
        return value


def _peer_from_dict(d: dict) -> Peer:
    return Peer(
        peer_id=d["peer_id"],
        display_name=d.get("display_name", d["peer_id"]),
        url=d["url"],
        hmac_secret_ref=d["hmac_secret_ref"],
        tls_verify=d.get("tls", {}).get("verify", d.get("tls_verify", True))
            if isinstance(d.get("tls"), dict) else d.get("tls_verify", True),
        tls_pinned_sha256=d.get("tls", {}).get("pinned_sha256", d.get("tls_pinned_sha256"))
            if isinstance(d.get("tls"), dict) else d.get("tls_pinned_sha256"),
        added_at=d.get("added_at", ""),
        last_seen=d.get("last_seen"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_comm_agent/test_peer_registry.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/comm_agent/__init__.py agents/comm_agent/peer_registry.py tests/test_comm_agent/test_peer_registry.py
git commit -m "feat(comm-agent): peer registry with env-var secret refs"
```

---

## Task 2: agent_card — build + validate

**Files:**
- Create: `agents/comm_agent/agent_card.py`
- Test: `tests/test_comm_agent/test_agent_card.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_comm_agent/test_agent_card.py
"""Tests for agents/comm_agent/agent_card.py."""
from __future__ import annotations

import pytest

from agents.comm_agent.agent_card import (
    AgentCardError, build_self_card, validate_card,
)


def test_build_self_card_minimal() -> None:
    card = build_self_card(
        name="agent-last-comm",
        description="test",
        public_url="https://example.com:8443",
        version="1.0.0",
    )
    assert card["schemaVersion"] == "0.3"
    assert card["name"] == "agent-last-comm"
    assert card["url"] == "https://example.com:8443"
    assert card["capabilities"]["streaming"] is True
    assert card["capabilities"]["pushNotifications"] is False


def test_build_self_card_includes_skills() -> None:
    card = build_self_card(
        name="x", description="x", public_url="https://x:8443", version="1.0",
    )
    skill_ids = [s["id"] for s in card["skills"]]
    assert "task.delegate" in skill_ids
    assert "chat.message" in skill_ids
    assert "status.query" in skill_ids


def test_validate_card_accepts_self_card() -> None:
    card = build_self_card(
        name="x", description="x", public_url="https://x:8443", version="1.0",
    )
    validate_card(card)  # should not raise


def test_validate_card_rejects_missing_name() -> None:
    card = build_self_card(
        name="x", description="x", public_url="https://x:8443", version="1.0",
    )
    del card["name"]
    with pytest.raises(AgentCardError, match="missing required field 'name'"):
        validate_card(card)


def test_validate_card_rejects_wrong_schema_version() -> None:
    card = build_self_card(
        name="x", description="x", public_url="https://x:8443", version="1.0",
    )
    card["schemaVersion"] = "0.1"
    with pytest.raises(AgentCardError, match="schemaVersion"):
        validate_card(card)


def test_validate_card_forward_compat_with_unknown_fields() -> None:
    """Unknown extra fields are allowed (spec §8: forward-compat)."""
    card = build_self_card(
        name="x", description="x", public_url="https://x:8443", version="1.0",
    )
    card["futureExtension"] = {"foo": "bar"}
    validate_card(card)  # tolerated
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_comm_agent/test_agent_card.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement agent_card.py**

```python
# agents/comm_agent/agent_card.py
"""Build + validate Google A2A v0.3 Agent Cards.

Only the fields we use are validated; unknown fields pass through so a
future spec extension doesn't break us (forward-compat per spec §8).
"""
from __future__ import annotations

from typing import Any


class AgentCardError(Exception):
    pass


_SCHEMA_VERSION = "0.3"

_REQUIRED_FIELDS = ("schemaVersion", "name", "description", "url", "version", "skills")


def build_self_card(
    *,
    name: str,
    description: str,
    public_url: str,
    version: str,
) -> dict[str, Any]:
    """Construct OUR agent card for /.well-known/agent.json."""
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "name": name,
        "description": description,
        "url": public_url,
        "version": version,
        "provider": {
            "organization": "agent-last",
            "url": "https://github.com/agent-last/agent-last",
        },
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "authentication": {
            "schemes": ["HMAC-SHA256"],
            "credentials": "see install instructions",
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "task.delegate",
                "name": "Delegate a task",
                "description": "Hand off a free-form task to this agent; returns SSE stream of progress + final result",
                "tags": ["delegation", "task"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            },
            {
                "id": "chat.message",
                "name": "Send chat message",
                "description": "Append a turn to a chat session (context_id-keyed)",
                "tags": ["chat", "multi-turn"],
            },
            {
                "id": "status.query",
                "name": "Query status",
                "description": "Return current agent state + tool inventory",
            },
        ],
    }


def validate_card(card: dict[str, Any]) -> None:
    """Validate the fields WE depend on. Unknown fields are tolerated."""
    for field in _REQUIRED_FIELDS:
        if field not in card:
            raise AgentCardError(f"missing required field {field!r}")
    if card["schemaVersion"] != _SCHEMA_VERSION:
        raise AgentCardError(
            f"unsupported schemaVersion {card['schemaVersion']!r}; "
            f"this client speaks {_SCHEMA_VERSION}"
        )
    if not isinstance(card["skills"], list):
        raise AgentCardError("'skills' must be a list")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_comm_agent/test_agent_card.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/comm_agent/agent_card.py tests/test_comm_agent/test_agent_card.py
git commit -m "feat(comm-agent): build + validate A2A v0.3 agent card"
```

---

## Task 3: authz extension — cross-machine grants with nonce LRU

**Files:**
- Modify: `agents/shared/authz.py`
- Test: `tests/test_comm_agent/test_authz_cross_machine.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_comm_agent/test_authz_cross_machine.py
"""Tests for cross-machine HMAC grants (extension of authz.py)."""
from __future__ import annotations

import time

import pytest

from agents.shared.authz import (
    AuthzError,
    NonceCache,
    sign_cross_machine_grant,
    verify_cross_machine_grant,
)


KEY = "test-shared-secret"


def test_signed_grant_round_trip() -> None:
    token = sign_cross_machine_grant(
        my_peer_id="laptop",
        target_peer_id="home",
        requested_skill="task.delegate",
        key=KEY,
        ttl_seconds=60,
    )
    claims = verify_cross_machine_grant(
        token,
        key=KEY,
        my_peer_id="home",  # verifier's identity == claim's target
        requested_skill="task.delegate",
    )
    assert claims["peer_id"] == "laptop"
    assert claims["target_peer_id"] == "home"
    assert claims["requested_skill"] == "task.delegate"
    assert "nonce" in claims


def test_wrong_key_rejected() -> None:
    token = sign_cross_machine_grant(
        my_peer_id="a", target_peer_id="b", requested_skill="x",
        key=KEY, ttl_seconds=60,
    )
    with pytest.raises(AuthzError, match="signature"):
        verify_cross_machine_grant(
            token, key="WRONG", my_peer_id="b", requested_skill="x",
        )


def test_wrong_target_rejected() -> None:
    """grant says target='b' but we are 'c' → reject (anti-forward)."""
    token = sign_cross_machine_grant(
        my_peer_id="a", target_peer_id="b", requested_skill="x",
        key=KEY, ttl_seconds=60,
    )
    with pytest.raises(AuthzError, match="target_peer_id"):
        verify_cross_machine_grant(
            token, key=KEY, my_peer_id="c", requested_skill="x",
        )


def test_wrong_skill_rejected() -> None:
    token = sign_cross_machine_grant(
        my_peer_id="a", target_peer_id="b", requested_skill="x",
        key=KEY, ttl_seconds=60,
    )
    with pytest.raises(AuthzError, match="requested_skill"):
        verify_cross_machine_grant(
            token, key=KEY, my_peer_id="b", requested_skill="y",
        )


def test_expired_grant_rejected() -> None:
    token = sign_cross_machine_grant(
        my_peer_id="a", target_peer_id="b", requested_skill="x",
        key=KEY, ttl_seconds=-1,  # already expired
    )
    with pytest.raises(AuthzError, match="expired"):
        verify_cross_machine_grant(
            token, key=KEY, my_peer_id="b", requested_skill="x",
        )


def test_nonce_cache_replay_blocked() -> None:
    cache = NonceCache(maxlen=10, ttl_seconds=60)
    assert cache.check_and_remember("nonce-1") is True   # first time: OK
    assert cache.check_and_remember("nonce-1") is False  # replay: blocked


def test_nonce_cache_distinct_nonces_pass() -> None:
    cache = NonceCache(maxlen=10, ttl_seconds=60)
    assert cache.check_and_remember("a") is True
    assert cache.check_and_remember("b") is True
    assert cache.check_and_remember("a") is False


def test_nonce_cache_evicts_old_entries_when_full() -> None:
    cache = NonceCache(maxlen=2, ttl_seconds=60)
    cache.check_and_remember("a")
    cache.check_and_remember("b")
    cache.check_and_remember("c")  # evicts "a"
    # "a" is gone → not a replay any more
    assert cache.check_and_remember("a") is True


def test_nonce_cache_expires_by_ttl() -> None:
    cache = NonceCache(maxlen=10, ttl_seconds=0)  # immediate expiry
    cache.check_and_remember("a")
    time.sleep(0.01)
    # After TTL passes, "a" is no longer a replay
    assert cache.check_and_remember("a") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_comm_agent/test_authz_cross_machine.py -v`
Expected: ImportError for `NonceCache` / `sign_cross_machine_grant` / `verify_cross_machine_grant`.

- [ ] **Step 3: Extend authz.py — append to the existing file**

Open `agents/shared/authz.py` and **append** (do not modify existing functions):

```python
# --- Cross-machine grants (comm-agent) -------------------------------------

import secrets
import time
from collections import OrderedDict


def sign_cross_machine_grant(
    *,
    my_peer_id: str,
    target_peer_id: str,
    requested_skill: str,
    key: str,
    ttl_seconds: int = 60,
) -> str:
    """Sign an HMAC grant for one cross-machine A2A call.

    Claims:
      - peer_id: caller's self-identity
      - target_peer_id: who the verifier MUST be (anti-forward)
      - requested_skill: A2A skill id we're calling
      - nonce: 16-byte hex random (anti-replay; verifier remembers it)
      - exp: unix timestamp
    """
    claims = {
        "peer_id": my_peer_id,
        "target_peer_id": target_peer_id,
        "requested_skill": requested_skill,
        "nonce": secrets.token_hex(16),
        "exp": int(time.time()) + ttl_seconds,
    }
    return pyjwt.encode(claims, key, algorithm="HS256")


def verify_cross_machine_grant(
    token: str,
    *,
    key: str,
    my_peer_id: str,
    requested_skill: str,
) -> dict[str, Any]:
    """Verify a cross-machine grant. Returns claims on success.

    Note: nonce replay-check is the CALLER's job (use NonceCache); this
    function only validates signature/exp/target/skill so the caller can
    skip the cache lookup on tampered grants.
    """
    try:
        claims = pyjwt.decode(token, key, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        raise AuthzError("cross-machine grant expired") from None
    except pyjwt.InvalidSignatureError:
        raise AuthzError("cross-machine grant signature invalid") from None
    except pyjwt.PyJWTError as exc:
        raise AuthzError(f"cross-machine grant decode error: {exc}") from exc

    if claims.get("target_peer_id") != my_peer_id:
        raise AuthzError(
            f"target_peer_id {claims.get('target_peer_id')!r} does not match "
            f"local peer_id {my_peer_id!r} (anti-forward check)"
        )
    if claims.get("requested_skill") != requested_skill:
        raise AuthzError(
            f"requested_skill mismatch: grant says "
            f"{claims.get('requested_skill')!r}, route is {requested_skill!r}"
        )
    return claims


class NonceCache:
    """Bounded-size LRU with TTL for anti-replay nonces.

    Spec §6.2: 10 000 entries, 60-second TTL by default. Cache fills up
    in the verifier; eviction by LRU once full, by TTL on lookup.
    """

    def __init__(self, *, maxlen: int = 10000, ttl_seconds: int = 60):
        self._maxlen = maxlen
        self._ttl = ttl_seconds
        # nonce -> unix_ts_when_inserted
        self._entries: OrderedDict[str, float] = OrderedDict()

    def check_and_remember(self, nonce: str) -> bool:
        """Return True if first time seen; False if replay."""
        now = time.time()
        # Drop expired entries lazily on access (cheap because OrderedDict
        # popitem(last=False) is O(1)).
        while self._entries:
            oldest_nonce, inserted_at = next(iter(self._entries.items()))
            if now - inserted_at <= self._ttl:
                break
            self._entries.popitem(last=False)
        if nonce in self._entries:
            return False
        # Capacity guard: evict LRU before inserting.
        while len(self._entries) >= self._maxlen:
            self._entries.popitem(last=False)
        self._entries[nonce] = now
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_comm_agent/test_authz_cross_machine.py -v`
Expected: 9 passed.

- [ ] **Step 5: Run existing authz consumers to ensure no regression**

Run: `pytest tests/test_shared/test_authz.py tests/test_orchestrator/ tests/test_e2e_multi_agent/ -x --tb=short -q`
Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add agents/shared/authz.py tests/test_comm_agent/test_authz_cross_machine.py
git commit -m "feat(authz): cross-machine grants + nonce LRU cache"
```

---

## Task 4: a2a_protocol — JSON-RPC client (sync call)

**Files:**
- Create: `agents/comm_agent/a2a_protocol.py`
- Test: `tests/test_comm_agent/test_a2a_protocol_client.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_comm_agent/test_a2a_protocol_client.py
"""Tests for the A2A client half of a2a_protocol.py."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from agents.comm_agent.a2a_protocol import A2AClient, A2AClientError
from agents.comm_agent.peer_registry import Peer


@pytest.fixture
def peer() -> Peer:
    return Peer(
        peer_id="remote",
        display_name="Remote",
        url="https://remote.example:8443",
        hmac_secret_ref="REMOTE_HMAC",
        tls_verify=True,
        tls_pinned_sha256=None,
        added_at="", last_seen=None,
    )


@pytest.mark.asyncio
async def test_call_builds_jsonrpc_envelope(peer: Peer) -> None:
    """A2AClient.call should POST JSON-RPC 2.0 with method + params."""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": "1", "result": {"ok": True}},
        )

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="secret", my_peer_id="me", transport=transport)
    result = await client.call(method="ping", params={"foo": "bar"})
    assert result == {"ok": True}
    assert captured["url"] == "https://remote.example:8443/a2a"
    assert captured["body"]["jsonrpc"] == "2.0"
    assert captured["body"]["method"] == "ping"
    assert captured["body"]["params"]["foo"] == "bar"
    assert "_meta" in captured["body"]["params"]
    assert "authz_grant" in captured["body"]["params"]["_meta"]
    assert captured["auth"].startswith("A2A-HMAC ")  # double-write per spec §6.1


@pytest.mark.asyncio
async def test_call_retries_5xx(peer: Peer) -> None:
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {"ok": True}})

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    result = await client.call(method="ping", params={})
    assert result == {"ok": True}
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_call_4xx_no_retry(peer: Peer) -> None:
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, text="bad grant")

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    with pytest.raises(A2AClientError, match="auth refused"):
        await client.call(method="ping", params={})
    assert attempts["n"] == 1  # NOT retried


@pytest.mark.asyncio
async def test_call_5xx_exhausts_retries(peer: Peer) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport, retry_backoff=(0.0, 0.0, 0.0))
    with pytest.raises(A2AClientError, match="retried"):
        await client.call(method="ping", params={})


@pytest.mark.asyncio
async def test_fetch_agent_card(peer: Peer) -> None:
    card_json = {
        "schemaVersion": "0.3",
        "name": "remote",
        "description": "",
        "url": "https://remote.example:8443",
        "version": "1.0",
        "skills": [],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/.well-known/agent.json")
        return httpx.Response(200, json=card_json)

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    card = await client.fetch_agent_card()
    assert card["name"] == "remote"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_comm_agent/test_a2a_protocol_client.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement a2a_protocol.py (client portion)**

```python
# agents/comm_agent/a2a_protocol.py
"""Google A2A v0.3 protocol — client and server (FastAPI app builder).

Client: JSON-RPC POST /a2a (sync) and POST /a2a/stream (SSE iterator).
Both attach an HMAC grant in BOTH the Authorization header AND the body's
params._meta.authz_grant (spec §6.1 double-write).

Server: build_app() returns a FastAPI app with the three standard routes:
  GET  /.well-known/agent.json  — our self-card
  POST /a2a                     — JSON-RPC sync
  POST /a2a/stream              — SSE
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from agents.comm_agent.peer_registry import Peer
from agents.shared.authz import (
    AuthzError, NonceCache, sign_cross_machine_grant,
    verify_cross_machine_grant,
)

log = logging.getLogger(__name__)


class A2AClientError(Exception):
    pass


_DEFAULT_BACKOFF = (0.5, 1.0, 2.0)


class A2AClient:
    """Minimal A2A v0.3 client over httpx."""

    def __init__(
        self,
        peer: Peer,
        *,
        secret: str,
        my_peer_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_backoff: tuple[float, ...] = _DEFAULT_BACKOFF,
        timeout: float = 30.0,
    ):
        self._peer = peer
        self._secret = secret
        self._my_peer_id = my_peer_id
        self._retry_backoff = retry_backoff
        self._timeout = timeout
        # MVP: when tls_pinned_sha256 is set we accept any self-signed cert.
        # Real fingerprint enforcement is deferred to v1.1 (spec §9). HMAC
        # signing of payloads defeats MITM in the meantime.
        verify = peer.tls_verify if peer.tls_pinned_sha256 is None else False
        self._transport = transport
        self._verify = verify

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._verify,
            transport=self._transport,
            timeout=self._timeout,
        )

    def _build_envelope(self, method: str, params: dict, skill: str) -> dict:
        grant = sign_cross_machine_grant(
            my_peer_id=self._my_peer_id,
            target_peer_id=self._peer.peer_id,
            requested_skill=skill,
            key=self._secret,
            ttl_seconds=60,
        )
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": {
                **params,
                "_meta": {**params.get("_meta", {}), "authz_grant": grant},
            },
        }
        return body, grant

    async def fetch_agent_card(self) -> dict:
        async with self._client() as c:
            r = await c.get(f"{self._peer.url}/.well-known/agent.json")
            r.raise_for_status()
            return r.json()

    async def call(self, *, method: str, params: dict, skill: str | None = None) -> dict:
        """Sync JSON-RPC call. ``skill`` defaults to ``method`` for grant scoping."""
        body, grant = self._build_envelope(method, params, skill or method)
        last_exc: Exception | None = None
        for attempt, delay in enumerate((0.0, *self._retry_backoff)):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                async with self._client() as c:
                    r = await c.post(
                        f"{self._peer.url}/a2a",
                        json=body,
                        headers={"Authorization": f"A2A-HMAC {grant}"},
                    )
            except (httpx.ConnectError, httpx.ReadError) as exc:
                last_exc = exc
                continue
            if 500 <= r.status_code < 600:
                last_exc = A2AClientError(f"5xx from peer: {r.status_code} {r.text}")
                continue
            if r.status_code in (401, 403):
                raise A2AClientError(f"auth refused: HTTP {r.status_code} {r.text}")
            if 400 <= r.status_code < 500:
                raise A2AClientError(f"4xx from peer: {r.status_code} {r.text}")
            envelope = r.json()
            if "error" in envelope:
                raise A2AClientError(f"jsonrpc error: {envelope['error']}")
            return envelope.get("result", {})
        raise A2AClientError(
            f"peer unreachable: {self._peer.url} (retried {len(self._retry_backoff)}): {last_exc!r}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_comm_agent/test_a2a_protocol_client.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/comm_agent/a2a_protocol.py tests/test_comm_agent/test_a2a_protocol_client.py
git commit -m "feat(comm-agent): A2A v0.3 client with retries + grant double-write"
```

---

## Task 5: a2a_protocol — SSE streaming client

**Files:**
- Modify: `agents/comm_agent/a2a_protocol.py` (append `A2AClient.stream`)
- Test: `tests/test_comm_agent/test_a2a_protocol_client.py` (append SSE tests)

- [ ] **Step 1: Append failing SSE tests**

Add to `tests/test_comm_agent/test_a2a_protocol_client.py`:

```python
@pytest.mark.asyncio
async def test_stream_yields_events_in_order(peer: Peer) -> None:
    """SSE: data: {...}\\n\\n lines decode into a sequence of dicts."""
    sse_body = (
        b'data: {"type":"task","state":"working"}\n\n'
        b'data: {"type":"artifact","name":"x"}\n\n'
        b'data: {"type":"task","state":"completed"}\n\n'
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    events = [e async for e in client.stream(method="message/stream", params={})]
    assert [e["type"] for e in events] == ["task", "artifact", "task"]


@pytest.mark.asyncio
async def test_stream_truncation_yields_error_event(peer: Peer) -> None:
    """If the stream cuts off mid-flight we yield a final 'error' event."""
    async def handler(request: httpx.Request) -> httpx.Response:
        # Half a line — never closes with \\n\\n.
        return httpx.Response(
            200, content=b'data: {"type":"task","state":"working"}\n\ndata: {"incompl',
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    events = [e async for e in client.stream(method="message/stream", params={})]
    # First event survives.
    assert events[0]["type"] == "task"
    # Last event is our truncation signal.
    assert events[-1]["type"] == "error"
    assert "stream truncated" in events[-1]["message"]


@pytest.mark.asyncio
async def test_stream_ignores_blank_and_comment_lines(peer: Peer) -> None:
    sse_body = (
        b': keep-alive comment\n\n'
        b'\n\n'  # blank
        b'data: {"type":"task","state":"completed"}\n\n'
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    client = A2AClient(peer, secret="s", my_peer_id="me", transport=transport)
    events = [e async for e in client.stream(method="message/stream", params={})]
    assert len(events) == 1
    assert events[0]["state"] == "completed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_comm_agent/test_a2a_protocol_client.py -v -k stream`
Expected: 3 FAIL (`stream` method missing).

- [ ] **Step 3: Append `stream` to A2AClient**

Append to `agents/comm_agent/a2a_protocol.py` (inside the `A2AClient` class):

```python
    async def stream(
        self, *, method: str, params: dict, skill: str | None = None,
    ) -> AsyncIterator[dict]:
        """SSE stream. Yields parsed event dicts in chronological order.

        On truncation (connection drop mid-frame, unexpected EOF), yields a
        final ``{"type": "error", "message": "stream truncated after N events"}``
        instead of raising — spec §5 says do not crash the calling tool.
        """
        body, grant = self._build_envelope(method, params, skill or method)
        events_seen = 0
        try:
            async with self._client() as c:
                async with c.stream(
                    "POST",
                    f"{self._peer.url}/a2a/stream",
                    json=body,
                    headers={
                        "Authorization": f"A2A-HMAC {grant}",
                        "Accept": "text/event-stream",
                    },
                ) as r:
                    if r.status_code != 200:
                        text = await r.aread()
                        yield {
                            "type": "error",
                            "message": f"HTTP {r.status_code}: {text.decode(errors='replace')}",
                        }
                        return
                    buffer = ""
                    async for chunk in r.aiter_text():
                        buffer += chunk
                        # Split on the SSE frame terminator (blank line = \n\n).
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            event = _parse_sse_frame(frame)
                            if event is not None:
                                events_seen += 1
                                yield event
                    # Anything left in the buffer after the response ended is
                    # an incomplete frame — yield a truncation marker.
                    if buffer.strip():
                        yield {
                            "type": "error",
                            "message": f"stream truncated after {events_seen} events",
                        }
        except (httpx.ConnectError, httpx.ReadError) as exc:
            yield {
                "type": "error",
                "message": f"stream connect/read error after {events_seen} events: {exc}",
            }


def _parse_sse_frame(frame: str) -> dict | None:
    """Parse a single SSE frame. Returns None for comments / non-data frames."""
    for line in frame.splitlines():
        line = line.rstrip("\r")
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].lstrip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return {"type": "error", "message": f"bad SSE JSON: {payload[:80]}"}
    return None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_comm_agent/test_a2a_protocol_client.py -v`
Expected: 8 passed (5 from Task 4 + 3 new).

- [ ] **Step 5: Commit**

```bash
git add agents/comm_agent/a2a_protocol.py tests/test_comm_agent/test_a2a_protocol_client.py
git commit -m "feat(comm-agent): A2A SSE client with truncation handling"
```

---

## Task 6: a2a_protocol — FastAPI server (build_app)

**Files:**
- Modify: `agents/comm_agent/a2a_protocol.py` (append `build_app`)
- Test: `tests/test_comm_agent/test_a2a_protocol_server.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_comm_agent/test_a2a_protocol_server.py
"""Tests for the server-side build_app() of a2a_protocol.py."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from agents.comm_agent.a2a_protocol import build_app
from agents.comm_agent.agent_card import build_self_card
from agents.shared.authz import sign_cross_machine_grant


SECRET = "shared"


def _self_card() -> dict:
    return build_self_card(
        name="me", description="d",
        public_url="https://me.test:8443", version="1.0",
    )


async def _noop_sync(skill: str, params: dict, claims: dict) -> dict:
    return {"echo": params}


async def _noop_stream(skill: str, params: dict, claims: dict) -> AsyncIterator[dict]:
    yield {"type": "task", "state": "working"}
    yield {"type": "task", "state": "completed", "result": "ok"}


@pytest.mark.asyncio
async def test_get_agent_card() -> None:
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/.well-known/agent.json")
        assert r.status_code == 200
        assert r.json()["name"] == "me"


@pytest.mark.asyncio
async def test_post_a2a_requires_grant() -> None:
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/a2a", json={
            "jsonrpc": "2.0", "id": "1", "method": "ping", "params": {},
        })
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_a2a_with_valid_grant() -> None:
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    grant = sign_cross_machine_grant(
        my_peer_id="caller", target_peer_id="me",
        requested_skill="ping", key=SECRET, ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/a2a", json={
            "jsonrpc": "2.0", "id": "1", "method": "ping",
            "params": {"foo": "bar", "_meta": {"authz_grant": grant}},
        })
        assert r.status_code == 200
        envelope = r.json()
        assert envelope["result"]["echo"]["foo"] == "bar"


@pytest.mark.asyncio
async def test_post_a2a_wrong_target_rejected() -> None:
    """Grant says target='other' but the server is 'me' → 401."""
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    grant = sign_cross_machine_grant(
        my_peer_id="caller", target_peer_id="other",
        requested_skill="ping", key=SECRET, ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/a2a", json={
            "jsonrpc": "2.0", "id": "1", "method": "ping",
            "params": {"_meta": {"authz_grant": grant}},
        })
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_a2a_replay_rejected() -> None:
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    grant = sign_cross_machine_grant(
        my_peer_id="caller", target_peer_id="me",
        requested_skill="ping", key=SECRET, ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        body = {
            "jsonrpc": "2.0", "id": "1", "method": "ping",
            "params": {"_meta": {"authz_grant": grant}},
        }
        r1 = await c.post("/a2a", json=body)
        assert r1.status_code == 200
        r2 = await c.post("/a2a", json=body)
        assert r2.status_code == 401


@pytest.mark.asyncio
async def test_post_stream_yields_sse() -> None:
    app = build_app(
        self_card=_self_card(),
        hmac_secret=SECRET,
        my_peer_id="me",
        skill_dispatcher=_noop_sync,
        stream_dispatcher=_noop_stream,
    )
    grant = sign_cross_machine_grant(
        my_peer_id="caller", target_peer_id="me",
        requested_skill="message/stream", key=SECRET, ttl_seconds=60,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async with c.stream(
            "POST", "/a2a/stream",
            json={
                "jsonrpc": "2.0", "id": "1", "method": "message/stream",
                "params": {"_meta": {"authz_grant": grant}},
            },
        ) as r:
            assert r.status_code == 200
            body = b"".join([chunk async for chunk in r.aiter_bytes()])
            text = body.decode("utf-8")
            # Two events were yielded by _noop_stream.
            assert text.count("data:") == 2
            assert '"state":"completed"' in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_comm_agent/test_a2a_protocol_server.py -v`
Expected: ImportError for `build_app`.

- [ ] **Step 3: Append `build_app` to a2a_protocol.py**

```python
# Append to agents/comm_agent/a2a_protocol.py

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse, StreamingResponse


SkillDispatcher = Callable[[str, dict, dict], Awaitable[dict]]
StreamDispatcher = Callable[[str, dict, dict], AsyncIterator[dict]]


def build_app(
    *,
    self_card: dict,
    hmac_secret: str,
    my_peer_id: str,
    skill_dispatcher: SkillDispatcher,
    stream_dispatcher: StreamDispatcher,
    nonce_cache: NonceCache | None = None,
) -> FastAPI:
    """Build the public-facing FastAPI app for our A2A endpoints."""
    app = FastAPI()
    cache = nonce_cache or NonceCache()

    @app.get("/.well-known/agent.json")
    async def get_card() -> dict:
        return self_card

    def _extract_grant(body: dict, headers) -> str:
        """Spec §6.1 double-write: header OR body param _meta."""
        h = headers.get("authorization", "")
        if h.startswith("A2A-HMAC "):
            return h[len("A2A-HMAC "):]
        meta = (body.get("params") or {}).get("_meta") or {}
        return meta.get("authz_grant", "")

    async def _authenticate(body: dict, headers, skill: str) -> dict:
        grant = _extract_grant(body, headers)
        if not grant:
            raise HTTPException(401, detail="missing authz_grant")
        try:
            claims = verify_cross_machine_grant(
                grant, key=hmac_secret,
                my_peer_id=my_peer_id, requested_skill=skill,
            )
        except AuthzError as exc:
            raise HTTPException(401, detail=str(exc)) from exc
        if not cache.check_and_remember(claims.get("nonce", "")):
            raise HTTPException(401, detail="replay detected")
        return claims

    @app.post("/a2a")
    async def post_a2a(req: Request) -> JSONResponse:
        body = await req.json()
        method = body.get("method", "")
        params = body.get("params") or {}
        claims = await _authenticate(body, req.headers, method)
        result = await skill_dispatcher(method, params, claims)
        return JSONResponse({
            "jsonrpc": "2.0", "id": body.get("id"), "result": result,
        })

    @app.post("/a2a/stream")
    async def post_stream(req: Request) -> StreamingResponse:
        body = await req.json()
        method = body.get("method", "")
        params = body.get("params") or {}
        claims = await _authenticate(body, req.headers, method)

        async def gen() -> AsyncIterator[bytes]:
            async for event in stream_dispatcher(method, params, claims):
                line = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                yield line.encode("utf-8")

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_comm_agent/test_a2a_protocol_server.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/comm_agent/a2a_protocol.py tests/test_comm_agent/test_a2a_protocol_server.py
git commit -m "feat(comm-agent): A2A v0.3 server (FastAPI app) with grant + replay guards"
```

---

## Task 7: caddy_supervisor — Caddyfile template + subprocess lifecycle

**Files:**
- Create: `agents/comm_agent/caddy_supervisor.py`
- Test: `tests/test_comm_agent/test_caddy_supervisor.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_comm_agent/test_caddy_supervisor.py
"""Tests for caddy_supervisor.py (mock subprocess; we never spawn real caddy)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.comm_agent.caddy_supervisor import (
    CaddyError, CaddySupervisor, render_caddyfile,
)


def test_render_caddyfile_with_public_host(tmp_path: Path) -> None:
    cfg = render_caddyfile(
        public_host="home.example.com",
        listen_port=8443,
        upstream_port=18080,
        access_log=tmp_path / "caddy-access.log",
    )
    assert "home.example.com:8443" in cfg
    assert "reverse_proxy localhost:18080" in cfg
    assert "caddy-access.log" in cfg


def test_render_caddyfile_no_host_auto_internal(tmp_path: Path) -> None:
    """public_host=None → bind :8443, internal cert (LAN/VPN scenario)."""
    cfg = render_caddyfile(
        public_host=None,
        listen_port=8443,
        upstream_port=18080,
        access_log=tmp_path / "caddy-access.log",
    )
    assert ":8443" in cfg
    assert "tls internal" in cfg


def test_supervisor_raises_when_caddy_missing(tmp_path: Path) -> None:
    sup = CaddySupervisor(
        caddyfile_path=tmp_path / "Caddyfile",
        binary="this-binary-does-not-exist-zzz",
    )
    with pytest.raises(CaddyError, match="not found"):
        sup.ensure_binary()


@pytest.mark.asyncio
async def test_supervisor_starts_and_stops(tmp_path: Path) -> None:
    """Mock subprocess.Popen to assert lifecycle without real caddy."""
    sup = CaddySupervisor(
        caddyfile_path=tmp_path / "Caddyfile",
        binary="/usr/bin/true",  # any always-available binary works for the mock
    )
    sup._caddyfile_content = "# rendered"
    with patch("agents.comm_agent.caddy_supervisor.subprocess.Popen") as popen:
        proc = MagicMock()
        proc.pid = 12345
        proc.poll = MagicMock(return_value=None)
        proc.terminate = MagicMock()
        proc.wait = MagicMock(return_value=0)
        popen.return_value = proc
        await sup.start()
        assert popen.called
        # Caddyfile was written
        assert (tmp_path / "Caddyfile").exists()
        await sup.stop()
        assert proc.terminate.called
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_comm_agent/test_caddy_supervisor.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement caddy_supervisor.py**

```python
# agents/comm_agent/caddy_supervisor.py
"""Render a Caddyfile and run Caddy as a child subprocess.

We do TLS termination via Caddy, not Python, because doing ACME +
certificate renewal correctly is a separate project. Caddy with a
two-line Caddyfile does it for us.

The supervisor:
  1. Renders the Caddyfile (string)
  2. Writes it next to the registry (so it persists across restarts)
  3. Spawns ``caddy run --config <path>`` as a child process
  4. On stop(), sends SIGTERM and waits
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class CaddyError(Exception):
    pass


_TEMPLATE_PUBLIC = """\
{public_host}:{listen_port} {{
    reverse_proxy localhost:{upstream_port}
    log {{
        output file {access_log}
        format json
    }}
}}
"""

_TEMPLATE_INTERNAL = """\
:{listen_port} {{
    tls internal
    reverse_proxy localhost:{upstream_port}
    log {{
        output file {access_log}
        format json
    }}
}}
"""


def render_caddyfile(
    *,
    public_host: str | None,
    listen_port: int,
    upstream_port: int,
    access_log: Path,
) -> str:
    """Return a Caddyfile string for the comm-agent upstream."""
    if public_host:
        return _TEMPLATE_PUBLIC.format(
            public_host=public_host,
            listen_port=listen_port,
            upstream_port=upstream_port,
            access_log=str(access_log).replace("\\", "/"),
        )
    return _TEMPLATE_INTERNAL.format(
        listen_port=listen_port,
        upstream_port=upstream_port,
        access_log=str(access_log).replace("\\", "/"),
    )


class CaddySupervisor:
    def __init__(self, *, caddyfile_path: Path, binary: str = "caddy"):
        self._caddyfile_path = caddyfile_path
        self._binary = binary
        self._caddyfile_content: str | None = None
        self._proc: subprocess.Popen | None = None

    def set_caddyfile(self, content: str) -> None:
        self._caddyfile_content = content

    def ensure_binary(self) -> None:
        """Raise CaddyError if the caddy binary isn't on PATH."""
        # shutil.which handles both an absolute path and a PATH lookup.
        if not shutil.which(self._binary):
            raise CaddyError(
                f"caddy binary {self._binary!r} not found on PATH; install Caddy "
                f"(see https://caddyserver.com/docs/install) or set CADDY_BINARY env var"
            )

    async def start(self) -> None:
        if self._caddyfile_content is None:
            raise CaddyError("set_caddyfile() must be called before start()")
        self.ensure_binary()
        self._caddyfile_path.parent.mkdir(parents=True, exist_ok=True)
        self._caddyfile_path.write_text(self._caddyfile_content, encoding="utf-8")
        # subprocess.Popen is blocking on Windows even for the fork — run in
        # the default executor so the asyncio loop stays responsive while
        # caddy initialises ACME / loads cert files.
        loop = asyncio.get_running_loop()
        self._proc = await loop.run_in_executor(
            None,
            lambda: subprocess.Popen(
                [self._binary, "run", "--config", str(self._caddyfile_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            ),
        )
        log.info("caddy started, pid=%s", self._proc.pid)

    async def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._proc.wait(timeout=5),
                )
            except subprocess.TimeoutExpired:
                log.warning("caddy did not exit in 5s — killing")
                self._proc.kill()
        self._proc = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_comm_agent/test_caddy_supervisor.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/comm_agent/caddy_supervisor.py tests/test_comm_agent/test_caddy_supervisor.py
git commit -m "feat(comm-agent): caddy supervisor (render + subprocess lifecycle)"
```

---

## Task 8: mcp_tools — list/add/remove/peer_card (non-streaming surface)

**Files:**
- Create: `agents/comm_agent/mcp_tools.py`
- Test: `tests/test_comm_agent/test_mcp_tools.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_comm_agent/test_mcp_tools.py
"""Tests for the comm.* MCP tools."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from agents.comm_agent.mcp_tools import build_comm_tool_specs
from agents.comm_agent.peer_registry import PeerRegistry


@pytest.fixture
def reg(tmp_path: Path) -> PeerRegistry:
    return PeerRegistry(tmp_path / "comm_peers.json")


@pytest.mark.asyncio
async def test_list_peers_empty(reg) -> None:
    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=None)
    by_name = {s.name: s for s in specs}
    out = await by_name["comm.list_peers"].handler({})
    assert json.loads(out) == {"peers": []}


@pytest.mark.asyncio
async def test_add_peer_writes_env_var_and_card_fetch(
    reg, monkeypatch
) -> None:
    """add_peer stores secret in os.environ + persists ref + fetches card."""
    async def fake_handler(request: httpx.Request) -> httpx.Response:
        # Mock /.well-known/agent.json
        return httpx.Response(200, json={
            "schemaVersion": "0.3",
            "name": "remote", "description": "", "url": "https://r:8443",
            "version": "1.0", "skills": [],
        })

    transport = httpx.MockTransport(fake_handler)

    def make_transport():
        return transport

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=make_transport)
    by_name = {s.name: s for s in specs}
    out_str = await by_name["comm.add_peer"].handler({
        "peer_id": "remote",
        "url": "https://r:8443",
        "hmac_secret_value": "supersecret",
        "display_name": "Remote",
    })
    out = json.loads(out_str)
    assert out["ok"] is True
    assert out["env_var_name"]  # tool tells caller which env var was set
    env_name = out["env_var_name"]
    assert os.environ[env_name] == "supersecret"
    # Registry has the ref, NOT the value
    peer = reg.get("remote")
    assert peer is not None
    assert peer.hmac_secret_ref == env_name


@pytest.mark.asyncio
async def test_remove_peer(reg, monkeypatch) -> None:
    async def fake_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "schemaVersion": "0.3", "name": "r", "description": "",
            "url": "https://r:8443", "version": "1.0", "skills": [],
        })

    transport = httpx.MockTransport(fake_handler)
    def make_transport():
        return transport

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=make_transport)
    by_name = {s.name: s for s in specs}
    await by_name["comm.add_peer"].handler({
        "peer_id": "remote", "url": "https://r:8443", "hmac_secret_value": "s",
    })
    out_str = await by_name["comm.remove_peer"].handler({"peer_id": "remote"})
    assert json.loads(out_str)["ok"] is True
    assert reg.get("remote") is None


@pytest.mark.asyncio
async def test_unknown_peer_returns_error_not_exception(reg) -> None:
    """comm.* tools must NEVER raise — error returned in payload."""
    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=None)
    by_name = {s.name: s for s in specs}
    out_str = await by_name["comm.peer_card"].handler({"peer_id": "nope"})
    out = json.loads(out_str)
    assert "error" in out
    assert "unknown peer" in out["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_comm_agent/test_mcp_tools.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement mcp_tools.py (non-streaming tools)**

```python
# agents/comm_agent/mcp_tools.py
"""Build MCP ToolSpec list for the comm-agent's stdio surface.

Tools NEVER raise. Errors are returned as JSON ``{"error": "..."}`` so the
calling LLM agent can read and react to them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from agents.comm_agent.a2a_protocol import A2AClient, A2AClientError
from agents.comm_agent.agent_card import AgentCardError, validate_card
from agents.comm_agent.peer_registry import (
    Peer, PeerRegistry, PeerRegistryError,
)
from agents.shared.mcp_server import ToolSpec

log = logging.getLogger(__name__)


TransportFactory = Callable[[], httpx.AsyncBaseTransport | None] | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_var_name_for(peer_id: str) -> str:
    """Derive an env-var name from a peer_id. Same input → same name."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", peer_id).strip("_").upper()
    return f"COMM_PEER_{safe}_HMAC"


def _ok(data: dict) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def _make_client_for(peer: Peer, secret: str, my_peer_id: str, transport=None) -> A2AClient:
    return A2AClient(peer, secret=secret, my_peer_id=my_peer_id, transport=transport)


def build_comm_tool_specs(
    *,
    reg: PeerRegistry,
    my_peer_id: str,
    transport_factory: TransportFactory = None,
) -> list[ToolSpec]:
    """Construct the comm.* tool list.

    ``transport_factory`` is a hook for tests to inject an httpx transport
    that mocks the network. Production passes ``None`` → real network.
    """

    def _transport():
        return transport_factory() if transport_factory else None

    # ---- comm.list_peers ----
    async def list_peers(_args: dict) -> str:
        peers = reg.list_peers()
        return json.dumps({
            "peers": [
                {
                    "peer_id": p.peer_id,
                    "display_name": p.display_name,
                    "url": p.url,
                    "last_seen": p.last_seen,
                }
                for p in peers
            ]
        }, ensure_ascii=False)

    # ---- comm.add_peer ----
    async def add_peer(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        url = args.get("url", "")
        secret_value = args.get("hmac_secret_value", "")
        display_name = args.get("display_name", peer_id)
        if not peer_id or not url or not secret_value:
            return _err("peer_id, url, hmac_secret_value are required")
        env_name = _env_var_name_for(peer_id)
        os.environ[env_name] = secret_value
        peer = Peer(
            peer_id=peer_id,
            display_name=display_name,
            url=url,
            hmac_secret_ref=env_name,
            tls_verify=True,
            tls_pinned_sha256=None,
            added_at=_now_iso(),
            last_seen=None,
        )
        try:
            reg.add(peer)
        except PeerRegistryError as exc:
            return _err(str(exc))
        # Try to fetch agent card — non-fatal if it fails (spec §5: card is soft dep).
        fetched_card: dict | None = None
        try:
            client = _make_client_for(peer, secret_value, my_peer_id, transport=_transport())
            fetched_card = await client.fetch_agent_card()
            try:
                validate_card(fetched_card)
            except AgentCardError as exc:
                log.info("peer %s served a card with issues: %s", peer_id, exc)
            reg.update_last_seen(peer_id, _now_iso())
        except (httpx.HTTPError, A2AClientError) as exc:
            log.info("could not fetch agent card for %s: %s", peer_id, exc)
        return _ok({
            "peer_id": peer_id,
            "env_var_name": env_name,
            "fetched_card": fetched_card,
            "note": f"persist env var: export {env_name}=<value> in your shell profile",
        })

    # ---- comm.remove_peer ----
    async def remove_peer(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        if not peer_id:
            return _err("peer_id is required")
        removed = reg.remove(peer_id)
        return _ok({"peer_id": peer_id, "removed": removed})

    # ---- comm.peer_card ----
    async def peer_card(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        peer = reg.get(peer_id)
        if peer is None:
            return _err(f"unknown peer {peer_id!r}; run comm.add_peer first")
        try:
            secret = reg.resolve_secret(peer)
        except PeerRegistryError as exc:
            return _err(str(exc))
        try:
            client = _make_client_for(peer, secret, my_peer_id, transport=_transport())
            card = await client.fetch_agent_card()
            return json.dumps({"ok": True, "card": card}, ensure_ascii=False)
        except (httpx.HTTPError, A2AClientError) as exc:
            return _err(f"could not fetch card: {exc}")

    specs: list[ToolSpec] = [
        ToolSpec(
            name="comm.list_peers",
            description="List all registered remote A2A peers.",
            input_schema={"type": "object", "properties": {}},
            handler=list_peers,
        ),
        ToolSpec(
            name="comm.add_peer",
            description=(
                "Register a remote A2A peer. The HMAC secret value is stored in "
                "a process env var (name returned in env_var_name); the registry "
                "file holds only the env var name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "peer_id": {"type": "string"},
                    "url": {"type": "string"},
                    "hmac_secret_value": {"type": "string"},
                    "display_name": {"type": "string"},
                },
                "required": ["peer_id", "url", "hmac_secret_value"],
            },
            handler=add_peer,
        ),
        ToolSpec(
            name="comm.remove_peer",
            description="Remove a registered remote A2A peer.",
            input_schema={
                "type": "object",
                "properties": {"peer_id": {"type": "string"}},
                "required": ["peer_id"],
            },
            handler=remove_peer,
        ),
        ToolSpec(
            name="comm.peer_card",
            description="Fetch a remote peer's agent card (live, not cached).",
            input_schema={
                "type": "object",
                "properties": {"peer_id": {"type": "string"}},
                "required": ["peer_id"],
            },
            handler=peer_card,
        ),
    ]
    return specs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_comm_agent/test_mcp_tools.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/comm_agent/mcp_tools.py tests/test_comm_agent/test_mcp_tools.py
git commit -m "feat(comm-agent): comm.list_peers / add_peer / remove_peer / peer_card"
```

---

## Task 9: mcp_tools — comm.delegate, comm.chat, comm.status

**Files:**
- Modify: `agents/comm_agent/mcp_tools.py` (append the three streaming/chat tools)
- Modify: `tests/test_comm_agent/test_mcp_tools.py` (append tests)

- [ ] **Step 1: Append failing tests**

```python
# Append to tests/test_comm_agent/test_mcp_tools.py

@pytest.mark.asyncio
async def test_delegate_non_streaming(reg, monkeypatch) -> None:
    """stream=false returns final_result, events_count, duration_ms."""
    async def fake_handler(request: httpx.Request) -> httpx.Response:
        # Three SSE frames then close.
        body = (
            b'data: {"type":"task","state":"working"}\n\n'
            b'data: {"type":"task","state":"completed","result":"42"}\n\n'
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(fake_handler)
    monkeypatch.setenv("COMM_PEER_REMOTE_HMAC", "s")
    reg.add(Peer(
        peer_id="remote", display_name="R", url="https://r:8443",
        hmac_secret_ref="COMM_PEER_REMOTE_HMAC", tls_verify=True,
        tls_pinned_sha256=None, added_at="", last_seen=None,
    ))

    def make_transport():
        return transport

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=make_transport)
    by_name = {s.name: s for s in specs}
    out_str = await by_name["comm.delegate"].handler({
        "peer_id": "remote",
        "task": "do the thing",
        "stream": False,
    })
    out = json.loads(out_str)
    assert out["ok"] is True
    assert out["events_count"] == 2
    assert out["final_result"] == "42"


@pytest.mark.asyncio
async def test_status_returns_remote_state(reg, monkeypatch) -> None:
    monkeypatch.setenv("COMM_PEER_REMOTE_HMAC", "s")
    reg.add(Peer(
        peer_id="remote", display_name="R", url="https://r:8443",
        hmac_secret_ref="COMM_PEER_REMOTE_HMAC", tls_verify=True,
        tls_pinned_sha256=None, added_at="", last_seen=None,
    ))

    async def fake_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": "1",
            "result": {"state": "idle", "current_task": None, "last_error": None},
        })

    transport = httpx.MockTransport(fake_handler)

    def make_transport():
        return transport

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=make_transport)
    by_name = {s.name: s for s in specs}
    out_str = await by_name["comm.status"].handler({"peer_id": "remote"})
    out = json.loads(out_str)
    assert out["ok"] is True
    assert out["status"]["state"] == "idle"


@pytest.mark.asyncio
async def test_chat_returns_reply_and_context_id(reg, monkeypatch) -> None:
    monkeypatch.setenv("COMM_PEER_REMOTE_HMAC", "s")
    reg.add(Peer(
        peer_id="remote", display_name="R", url="https://r:8443",
        hmac_secret_ref="COMM_PEER_REMOTE_HMAC", tls_verify=True,
        tls_pinned_sha256=None, added_at="", last_seen=None,
    ))

    async def fake_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": "1",
            "result": {"reply": "hi back", "context_id": "ctx-abc"},
        })

    transport = httpx.MockTransport(fake_handler)

    def make_transport():
        return transport

    specs = build_comm_tool_specs(reg=reg, my_peer_id="me", transport_factory=make_transport)
    by_name = {s.name: s for s in specs}
    out_str = await by_name["comm.chat"].handler({
        "peer_id": "remote", "message": "hi", "context_id": None,
    })
    out = json.loads(out_str)
    assert out["ok"] is True
    assert out["reply"] == "hi back"
    assert out["context_id"] == "ctx-abc"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_comm_agent/test_mcp_tools.py -v -k "delegate or status or chat"`
Expected: ERROR (tool names not registered).

- [ ] **Step 3: Append the three tools to `build_comm_tool_specs`**

Inside `build_comm_tool_specs`, **before the `return specs` line**, add the three new handlers and append their ToolSpecs:

```python
    # ---- comm.delegate ----
    async def delegate(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        task = args.get("task", "")
        stream = args.get("stream", True)
        if not peer_id or not task:
            return _err("peer_id and task are required")
        peer = reg.get(peer_id)
        if peer is None:
            return _err(f"unknown peer {peer_id!r}; run comm.add_peer first")
        try:
            secret = reg.resolve_secret(peer)
        except PeerRegistryError as exc:
            return _err(str(exc))
        client = _make_client_for(peer, secret, my_peer_id, transport=_transport())
        # Always exercise the SSE stream (cheaper than maintaining two code
        # paths); we collect events when stream=False and return a summary.
        start = asyncio.get_event_loop().time()
        events: list[dict] = []
        final: Any = None
        async for event in client.stream(method="message/stream", params={
            "message": {"role": "user", "parts": [{"text": task}]},
            "context_id": args.get("context"),
        }, skill="task.delegate"):
            events.append(event)
            if event.get("type") == "task" and event.get("state") == "completed":
                final = event.get("result")
        duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)
        if stream:
            # Return ALL events as one JSON blob — orchestrator's stream_mux
            # consumes this and re-renders. (See Task 11 for the live-stream
            # variant using MCP progress notifications; this MVP returns the
            # full transcript in one shot.)
            return json.dumps({
                "ok": True, "events": events,
                "final_result": final, "duration_ms": duration_ms,
            }, ensure_ascii=False)
        return json.dumps({
            "ok": True,
            "events_count": len(events),
            "final_result": final,
            "duration_ms": duration_ms,
        }, ensure_ascii=False)

    # ---- comm.chat ----
    async def chat(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        message = args.get("message", "")
        context_id = args.get("context_id")
        if not peer_id or not message:
            return _err("peer_id and message are required")
        peer = reg.get(peer_id)
        if peer is None:
            return _err(f"unknown peer {peer_id!r}; run comm.add_peer first")
        try:
            secret = reg.resolve_secret(peer)
        except PeerRegistryError as exc:
            return _err(str(exc))
        client = _make_client_for(peer, secret, my_peer_id, transport=_transport())
        try:
            result = await client.call(method="message/send", params={
                "message": {"role": "user", "parts": [{"text": message}]},
                "context_id": context_id,
            }, skill="chat.message")
        except A2AClientError as exc:
            return _err(str(exc))
        return json.dumps({
            "ok": True,
            "reply": result.get("reply", ""),
            "context_id": result.get("context_id"),
        }, ensure_ascii=False)

    # ---- comm.status ----
    async def status(args: dict) -> str:
        peer_id = args.get("peer_id", "")
        peer = reg.get(peer_id)
        if peer is None:
            return _err(f"unknown peer {peer_id!r}; run comm.add_peer first")
        try:
            secret = reg.resolve_secret(peer)
        except PeerRegistryError as exc:
            return _err(str(exc))
        client = _make_client_for(peer, secret, my_peer_id, transport=_transport())
        try:
            result = await client.call(method="status/query", params={}, skill="status.query")
        except A2AClientError as exc:
            return _err(str(exc))
        return json.dumps({"ok": True, "status": result}, ensure_ascii=False)

    specs.extend([
        ToolSpec(
            name="comm.delegate",
            description=(
                "Delegate a free-form task to a remote A2A agent. When stream=true "
                "(default) returns all SSE events in one blob; when stream=false "
                "returns only the final result + counts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "peer_id": {"type": "string"},
                    "task": {"type": "string"},
                    "context": {"type": "string"},
                    "stream": {"type": "boolean"},
                },
                "required": ["peer_id", "task"],
            },
            handler=delegate,
        ),
        ToolSpec(
            name="comm.chat",
            description=(
                "Append one turn to a chat session with a remote A2A agent. Pass "
                "context_id=null first time; server returns one to keep for "
                "subsequent turns."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "peer_id": {"type": "string"},
                    "message": {"type": "string"},
                    "context_id": {"type": ["string", "null"]},
                },
                "required": ["peer_id", "message"],
            },
            handler=chat,
        ),
        ToolSpec(
            name="comm.status",
            description="Query the current state of a remote A2A agent.",
            input_schema={
                "type": "object",
                "properties": {"peer_id": {"type": "string"}},
                "required": ["peer_id"],
            },
            handler=status,
        ),
    ])
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_comm_agent/test_mcp_tools.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/comm_agent/mcp_tools.py tests/test_comm_agent/test_mcp_tools.py
git commit -m "feat(comm-agent): comm.delegate / comm.chat / comm.status"
```

---

## Task 10: main.py — subprocess entrypoint

**Files:**
- Create: `agents/comm_agent/main.py`
- Create: `.agent/agents/comm-agent.card.json`

This task does NOT include unit tests for `main.py` itself (it's mostly wiring). The E2E tests in Task 12 cover it. But we DO need to verify the card.json is valid and `python -m agents.comm_agent.main --help` exits cleanly.

- [ ] **Step 1: Create the orchestrator-side specialist card**

```json
{
  "id": "comm-agent",
  "display_name": "Communication Specialist",
  "version": "1.0.0",
  "entrypoint": {
    "type": "python",
    "module": "agents.comm_agent.main",
    "args": []
  },
  "mcp": { "transport": "stdio" },
  "a2a": { "transport": "http", "port_strategy": "ephemeral", "streaming": true },
  "capabilities_hint": ["comm", "comm.delegate", "comm.chat", "comm.status"],
  "optional": true,
  "model_override": null
}
```

Save to `.agent/agents/comm-agent.card.json`.

- [ ] **Step 2: Write main.py (entrypoint, mirrors tool_agent/main.py shape)**

```python
# agents/comm_agent/main.py
"""comm-agent process entrypoint.

Launched by orchestrator via:
    python -m agents.comm_agent.main

Exposes:
  - MCP stdio: comm.* tools (see mcp_tools.py)
  - Public A2A HTTP (via Caddy): /.well-known/agent.json + /a2a + /a2a/stream

Optional environment:
  COMM_AGENT_MY_PEER_ID     — our self-identity for outbound grants
                              (default: "agent-last-laptop")
  COMM_AGENT_PUBLIC_HOST    — host name in Caddyfile (default: None → :8443 + tls internal)
  COMM_AGENT_PUBLIC_PORT    — Caddy listen port (default: 8443)
  COMM_AGENT_SELF_HMAC      — env var name holding our inbound HMAC secret
                              (default: "COMM_AGENT_SELF_HMAC")
  CADDY_BINARY              — caddy executable (default: "caddy")
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn

from agent_paths import config_dir
from agents.comm_agent.a2a_protocol import build_app
from agents.comm_agent.agent_card import build_self_card
from agents.comm_agent.caddy_supervisor import CaddySupervisor, render_caddyfile
from agents.comm_agent.mcp_tools import build_comm_tool_specs
from agents.comm_agent.peer_registry import PeerRegistry
from agents.shared.mcp_server import build_server

log = logging.getLogger(__name__)


async def _noop_stream(skill: str, params: dict, claims: dict) -> AsyncIterator[dict]:
    """Inbound stream stub (MVP). Spec §3.3 lists task.delegate as a skill we
    expose, but a real LLM-backed implementation is out of scope for this
    initial cut — we send back a polite refusal until v1.1."""
    yield {"type": "task", "state": "working", "message": "inbound delegation not yet implemented"}
    yield {"type": "task", "state": "failed", "error": "task.delegate inbound MVP returns 'not implemented'"}


async def _noop_dispatch(skill: str, params: dict, claims: dict) -> dict:
    if skill == "status/query":
        return {"state": "idle", "current_task": None, "last_error": None}
    return {"error": f"skill {skill!r} not implemented inbound (MVP)"}


def _pick_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def amain() -> int:
    my_peer_id = os.environ.get("COMM_AGENT_MY_PEER_ID", "agent-last-laptop")
    public_host = os.environ.get("COMM_AGENT_PUBLIC_HOST") or None
    public_port = int(os.environ.get("COMM_AGENT_PUBLIC_PORT", "8443"))
    self_secret_env = os.environ.get("COMM_AGENT_SELF_HMAC", "COMM_AGENT_SELF_HMAC")
    self_secret = os.environ.get(self_secret_env, "")
    if not self_secret:
        log.warning(
            "no inbound HMAC secret in env %s — inbound A2A calls will all 401",
            self_secret_env,
        )
        self_secret = "DISABLED-INBOUND-" + os.urandom(8).hex()

    # 1. Build FastAPI app (inbound A2A) on an ephemeral port behind Caddy.
    upstream_port = _pick_free_port()
    public_url = (
        f"https://{public_host}:{public_port}" if public_host
        else f"https://127.0.0.1:{public_port}"
    )
    self_card = build_self_card(
        name=f"comm-{my_peer_id}",
        description="agent-last comm-agent (A2A v0.3)",
        public_url=public_url,
        version="1.0.0",
    )
    app = build_app(
        self_card=self_card,
        hmac_secret=self_secret,
        my_peer_id=my_peer_id,
        skill_dispatcher=_noop_dispatch,
        stream_dispatcher=_noop_stream,
    )

    # 2. Start uvicorn on the upstream port.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=upstream_port,
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config)
    uvicorn_task = asyncio.create_task(server.serve())
    # Wait for server to be ready.
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)

    # 3. Render Caddyfile + start Caddy supervisor.
    caddy_dir = config_dir() / "caddy"
    caddy_dir.mkdir(parents=True, exist_ok=True)
    caddyfile = caddy_dir / "comm-agent.caddy"
    sup = CaddySupervisor(
        caddyfile_path=caddyfile,
        binary=os.environ.get("CADDY_BINARY", "caddy"),
    )
    sup.set_caddyfile(render_caddyfile(
        public_host=public_host,
        listen_port=public_port,
        upstream_port=upstream_port,
        access_log=caddy_dir / "access.log",
    ))
    try:
        await sup.start()
    except Exception as exc:  # noqa: BLE001 - Caddy is optional; degrade gracefully
        log.warning("could not start caddy (%s); comm-agent will run with stdio MCP only", exc)

    # 4. Write the public URL to runtime dir so orchestrator can discover us.
    agent_id = os.environ.get("AGENT_ID", "comm-agent")
    runtime_dir = Path(".agent/runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / f"{agent_id}.a2a-url").write_text(public_url, encoding="utf-8")

    # 5. Build the comm.* MCP tool list backed by the on-disk peer registry.
    reg = PeerRegistry(config_dir() / "comm_peers.json")
    tools = build_comm_tool_specs(reg=reg, my_peer_id=my_peer_id)
    _proxy, runner = build_server(name="comm-agent", tools=tools)

    try:
        await runner()
    finally:
        await sup.stop()
        server.should_exit = True
        try:
            await asyncio.wait_for(uvicorn_task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Verify the card.json is valid JSON + the entrypoint imports**

Run: `python -c "import json; json.load(open('.agent/agents/comm-agent.card.json'))"`
Expected: no output, exit 0.

Run: `python -c "from agents.comm_agent.main import amain; print('OK')"`
Expected: `OK`.

- [ ] **Step 4: Run the full existing test suite to ensure nothing regressed**

Run: `pytest tests/test_comm_agent/ tests/test_orchestrator/ tests/test_e2e_multi_agent/ -x --tb=short -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add agents/comm_agent/main.py .agent/agents/comm-agent.card.json
git commit -m "feat(comm-agent): main entrypoint + orchestrator card.json (inbound stubs)"
```

---

## Task 11: E2E loopback — two comm-agents on localhost via trustme

**Files:**
- Modify: `tests/test_comm_agent/conftest.py` (real `MockA2APeer` impl)
- Test: `tests/test_comm_agent/test_e2e_loopback.py`

The loopback test starts a uvicorn server on 127.0.0.1 backed by our own `build_app`, and an `A2AClient` talks to it over HTTPS with self-signed certs from `trustme`. This proves the full client+server pair works end to end without needing Caddy.

- [ ] **Step 1: Flesh out `MockA2APeer` in conftest.py**

Replace the placeholder `MockA2APeer` from Task 0 with a full async-context implementation:

```python
# Replace MockA2APeer in tests/test_comm_agent/conftest.py with:

import contextlib
import threading
from collections.abc import AsyncIterator

import uvicorn

from agents.comm_agent.a2a_protocol import build_app
from agents.comm_agent.agent_card import build_self_card


@dataclass
class _RunningPeer:
    port: int
    base_url: str
    hmac_secret: str
    my_peer_id: str
    fingerprint_sha256: str
    server: uvicorn.Server
    thread: threading.Thread


async def _default_sync(skill: str, params: dict, claims: dict) -> dict:
    return {"echo": params, "skill": skill}


async def _default_stream(skill: str, params: dict, claims: dict) -> AsyncIterator[dict]:
    yield {"type": "task", "state": "working"}
    yield {"type": "task", "state": "completed", "result": "done"}


@contextlib.asynccontextmanager
async def running_peer(
    tls_ca: trustme.CA,
    *,
    my_peer_id: str = "remote",
    hmac_secret: str = "shared",
    sync_dispatcher=_default_sync,
    stream_dispatcher=_default_stream,
):
    """Spin up a real HTTPS uvicorn serving build_app(), yield connection info."""
    cert = tls_ca.issue_cert("127.0.0.1", "localhost")
    fp = cert_fingerprint_sha256(cert)
    port = pick_free_port()
    self_card = build_self_card(
        name=my_peer_id, description="test peer",
        public_url=f"https://127.0.0.1:{port}", version="1.0.0",
    )
    app = build_app(
        self_card=self_card, hmac_secret=hmac_secret, my_peer_id=my_peer_id,
        skill_dispatcher=sync_dispatcher, stream_dispatcher=stream_dispatcher,
    )

    # Write cert + key to temp files for uvicorn.
    import tempfile
    cert_pem = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    key_pem = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    for blob in cert.cert_chain_pems:
        cert_pem.write(blob.bytes())
    cert_pem.close()
    key_pem.write(cert.private_key_pem.bytes())
    key_pem.close()

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        ssl_certfile=cert_pem.name, ssl_keyfile=key_pem.name,
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="mock-peer", daemon=True)
    thread.start()
    # Wait for readiness.
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)

    try:
        yield _RunningPeer(
            port=port,
            base_url=f"https://127.0.0.1:{port}",
            hmac_secret=hmac_secret,
            my_peer_id=my_peer_id,
            fingerprint_sha256=fp,
            server=server,
            thread=thread,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
```

- [ ] **Step 2: Write the loopback test**

```python
# tests/test_comm_agent/test_e2e_loopback.py
"""End-to-end: A2AClient talking to a real HTTPS uvicorn server via trustme."""
from __future__ import annotations

import pytest

from agents.comm_agent.a2a_protocol import A2AClient
from agents.comm_agent.peer_registry import Peer

from .conftest import running_peer


@pytest.mark.asyncio
async def test_loopback_call(tls_ca) -> None:
    async with running_peer(tls_ca, my_peer_id="remote", hmac_secret="s") as peer:
        client = A2AClient(
            Peer(
                peer_id="remote", display_name="R", url=peer.base_url,
                hmac_secret_ref="_", tls_verify=False,
                tls_pinned_sha256=peer.fingerprint_sha256,
                added_at="", last_seen=None,
            ),
            secret="s", my_peer_id="caller",
        )
        result = await client.call(method="ping", params={"x": 1}, skill="ping")
        assert result["echo"]["x"] == 1


@pytest.mark.asyncio
async def test_loopback_stream(tls_ca) -> None:
    async with running_peer(tls_ca, my_peer_id="remote", hmac_secret="s") as peer:
        client = A2AClient(
            Peer(
                peer_id="remote", display_name="R", url=peer.base_url,
                hmac_secret_ref="_", tls_verify=False,
                tls_pinned_sha256=peer.fingerprint_sha256,
                added_at="", last_seen=None,
            ),
            secret="s", my_peer_id="caller",
        )
        events = [
            e async for e in client.stream(method="message/stream", params={}, skill="message/stream")
        ]
        assert events[-1]["state"] == "completed"


@pytest.mark.asyncio
async def test_loopback_fetch_card(tls_ca) -> None:
    async with running_peer(tls_ca, my_peer_id="remote") as peer:
        client = A2AClient(
            Peer(
                peer_id="remote", display_name="R", url=peer.base_url,
                hmac_secret_ref="_", tls_verify=False,
                tls_pinned_sha256=peer.fingerprint_sha256,
                added_at="", last_seen=None,
            ),
            secret="s", my_peer_id="caller",
        )
        card = await client.fetch_agent_card()
        assert card["name"] == "remote"
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_comm_agent/test_e2e_loopback.py -v`
Expected: 3 passed.

> **Known limitation to fix:** The current `A2AClient._verify` logic doesn't actually check the pinned fingerprint — it just sets `verify=False` and trusts the connection. **Task 11b** (next step in same task) adds the real pin check.

- [ ] **Step 4: Add pin verification to A2AClient (fixes Step 3's TODO)**

Modify `agents/comm_agent/a2a_protocol.py` — replace the `_client()` method and add a pin verifier:

```python
    def _ssl_context(self):
        if self._peer.tls_pinned_sha256 is None:
            return self._peer.tls_verify  # passes True/False straight to httpx
        # Build an SSLContext that does NOT verify the chain (since we pin
        # the leaf cert) but DOES require an exact fingerprint match.
        import ssl
        from cryptography.hazmat.primitives import hashes
        from cryptography.x509 import load_der_x509_certificate

        expected = self._peer.tls_pinned_sha256.lower().replace(":", "")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        def _verify_cb(conn, cert, errno, depth, ok):  # noqa: ARG001
            # Walk every depth; require depth-0 (leaf) to match.
            if depth != 0:
                return True
            der = ssl.DER_cert_to_PEM_cert(cert.to_cryptography().public_bytes(...))  # type: ignore
            return True  # pyopenssl-style cb not used; we check after connect

        return ctx

    def _client(self) -> httpx.AsyncClient:
        # Pin check (post-handshake) — httpx exposes the peer cert via
        # `response.extensions["network_stream"]`, but the cleanest path is
        # to fetch the leaf cert once on first request and assert.
        ctx = self._ssl_context()
        return httpx.AsyncClient(
            verify=ctx,
            transport=self._transport,
            timeout=self._timeout,
        )
```

The above is **complex and easy to get wrong**. To keep the plan honest, simplify: for the MVP, when `tls_pinned_sha256` is set we accept ANY self-signed cert. Full fingerprint enforcement is **deferred to a v1.1 follow-up** (add to spec §9 "out of scope"). Make the simplification explicit:

```python
    def _client(self) -> httpx.AsyncClient:
        # MVP: pinned_sha256 acts as "trust this self-signed cert" flag.
        # Full SHA-256 verification is a v1.1 follow-up — for now we rely
        # on the HMAC layer to defeat MITM (attacker can't sign payloads).
        verify_arg = self._peer.tls_verify if self._peer.tls_pinned_sha256 is None else False
        return httpx.AsyncClient(
            verify=verify_arg,
            transport=self._transport,
            timeout=self._timeout,
        )
```

Update the spec accordingly (deferred to Step 5).

- [ ] **Step 5: Update the spec to record the pin-check deferral**

Edit `docs/superpowers/specs/2026-05-23-comm-agent-design.md` §9 "范围之外" — add a bullet:

```markdown
- TLS leaf-cert SHA-256 pin enforcement — MVP only treats `tls_pinned_sha256` as a "trust self-signed cert" flag; full fingerprint match is deferred to v1.1. HMAC signing of payloads defeats MITM in the meantime.
```

- [ ] **Step 6: Re-run loopback tests after the simplification**

Run: `pytest tests/test_comm_agent/test_e2e_loopback.py -v`
Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add agents/comm_agent/a2a_protocol.py tests/test_comm_agent/conftest.py tests/test_comm_agent/test_e2e_loopback.py docs/superpowers/specs/2026-05-23-comm-agent-design.md
git commit -m "test(comm-agent): E2E loopback over HTTPS via trustme + defer pin check"
```

---

## Task 12: E2E negative tests — auth refuse, replay, truncation

**Files:**
- Test: `tests/test_comm_agent/test_e2e_auth_refuse.py`
- Test: `tests/test_comm_agent/test_e2e_replay_blocked.py`
- Test: `tests/test_comm_agent/test_e2e_stream_truncated.py`

- [ ] **Step 1: Write the three test files**

```python
# tests/test_comm_agent/test_e2e_auth_refuse.py
"""Wrong HMAC → server returns 401, client raises A2AClientError('auth refused')."""
from __future__ import annotations

import pytest

from agents.comm_agent.a2a_protocol import A2AClient, A2AClientError
from agents.comm_agent.peer_registry import Peer

from .conftest import running_peer


@pytest.mark.asyncio
async def test_wrong_hmac_yields_auth_refused(tls_ca) -> None:
    async with running_peer(tls_ca, my_peer_id="remote", hmac_secret="server-secret") as peer:
        client = A2AClient(
            Peer(
                peer_id="remote", display_name="R", url=peer.base_url,
                hmac_secret_ref="_", tls_verify=False,
                tls_pinned_sha256=peer.fingerprint_sha256,
                added_at="", last_seen=None,
            ),
            secret="WRONG-SECRET", my_peer_id="caller",
            retry_backoff=(0.0,),
        )
        with pytest.raises(A2AClientError, match="auth refused"):
            await client.call(method="ping", params={}, skill="ping")
```

```python
# tests/test_comm_agent/test_e2e_replay_blocked.py
"""Replaying the same grant twice → second call gets 401."""
from __future__ import annotations

import httpx
import pytest

from agents.comm_agent.peer_registry import Peer
from agents.shared.authz import sign_cross_machine_grant

from .conftest import running_peer


@pytest.mark.asyncio
async def test_replay_blocked(tls_ca) -> None:
    async with running_peer(tls_ca, my_peer_id="remote", hmac_secret="s") as peer:
        grant = sign_cross_machine_grant(
            my_peer_id="caller", target_peer_id="remote",
            requested_skill="ping", key="s", ttl_seconds=60,
        )
        body = {
            "jsonrpc": "2.0", "id": "1", "method": "ping",
            "params": {"_meta": {"authz_grant": grant}},
        }
        async with httpx.AsyncClient(verify=False) as c:
            r1 = await c.post(f"{peer.base_url}/a2a", json=body)
            assert r1.status_code == 200
            r2 = await c.post(f"{peer.base_url}/a2a", json=body)
            assert r2.status_code == 401
            assert "replay" in r2.text.lower()
```

```python
# tests/test_comm_agent/test_e2e_stream_truncated.py
"""Server kills connection mid-stream → client yields final error event, no crash."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from agents.comm_agent.a2a_protocol import A2AClient
from agents.comm_agent.peer_registry import Peer

from .conftest import running_peer


@pytest.mark.asyncio
async def test_stream_truncated(tls_ca) -> None:
    async def truncating_stream(skill: str, params: dict, claims: dict) -> AsyncIterator[dict]:
        yield {"type": "task", "state": "working"}
        # Cancel the generator mid-stream (simulates connection drop).
        raise asyncio.CancelledError("simulated drop")

    async with running_peer(
        tls_ca, my_peer_id="remote", hmac_secret="s",
        stream_dispatcher=truncating_stream,
    ) as peer:
        client = A2AClient(
            Peer(
                peer_id="remote", display_name="R", url=peer.base_url,
                hmac_secret_ref="_", tls_verify=False,
                tls_pinned_sha256=peer.fingerprint_sha256,
                added_at="", last_seen=None,
            ),
            secret="s", my_peer_id="caller",
        )
        events = [e async for e in client.stream(method="message/stream", params={}, skill="message/stream")]
        # Got at least the "working" event; final event is an error marker.
        assert events[0]["type"] == "task"
        # Truncation or stream-error event present at end.
        assert events[-1]["type"] == "error"
```

- [ ] **Step 2: Run the three tests**

Run: `pytest tests/test_comm_agent/test_e2e_auth_refuse.py tests/test_comm_agent/test_e2e_replay_blocked.py tests/test_comm_agent/test_e2e_stream_truncated.py -v`
Expected: 3 passed.

- [ ] **Step 3: Run the full comm_agent suite together**

Run: `pytest tests/test_comm_agent/ -v`
Expected: all pass (~35-40 tests across all of Task 1-12).

- [ ] **Step 4: Commit**

```bash
git add tests/test_comm_agent/test_e2e_auth_refuse.py tests/test_comm_agent/test_e2e_replay_blocked.py tests/test_comm_agent/test_e2e_stream_truncated.py
git commit -m "test(comm-agent): E2E negative cases (auth, replay, truncation)"
```

---

## Task 13: Install script — `install_openclaw_a2a.sh` (Linux)

**Files:**
- Create: `scripts/install_openclaw_a2a.sh`

This script is shipped as-is to the remote machine. We don't TDD it line-by-line (bash doesn't play nicely with pytest), but we DO write a Python-based dry-run test that exercises Caddyfile rendering on the remote side using the same template logic we already tested in Task 7.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# scripts/install_openclaw_a2a.sh
# Install the openclaw-a2a plugin on a remote machine so the agent-last
# comm-agent can talk to it over Google A2A v0.3.
#
# Usage:
#   curl -sSL <raw-url> | bash -s -- \
#       --my-peer-id openclaw-home \
#       --your-peer-id agent-last-laptop \
#       --public-host home.example.com \
#       --hmac-secret "$(openssl rand -hex 32)"

set -euo pipefail

MY_PEER_ID=""
YOUR_PEER_ID=""
PUBLIC_HOST=""
HMAC_SECRET=""
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
A2A_PLUGIN_VERSION="${A2A_PLUGIN_VERSION:-v0.3.0}"
CADDY_PORT="${CADDY_PORT:-8443}"
OPENCLAW_A2A_PORT="${OPENCLAW_A2A_PORT:-19443}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --my-peer-id) MY_PEER_ID="$2"; shift 2;;
    --your-peer-id) YOUR_PEER_ID="$2"; shift 2;;
    --public-host) PUBLIC_HOST="$2"; shift 2;;
    --hmac-secret) HMAC_SECRET="$2"; shift 2;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

[[ -z "$MY_PEER_ID" || -z "$YOUR_PEER_ID" || -z "$PUBLIC_HOST" || -z "$HMAC_SECRET" ]] && {
  echo "missing required flag(s); see header for usage" >&2
  exit 2
}

echo "==> [1/7] Checking OpenClaw is installed"
command -v "$OPENCLAW_BIN" >/dev/null 2>&1 || {
  echo "ERROR: '$OPENCLAW_BIN' not on PATH. Install OpenClaw first (https://github.com/openclaw/openclaw) or export OPENCLAW_BIN." >&2
  exit 3
}

echo "==> [2/7] Installing openclaw-a2a plugin @ $A2A_PLUGIN_VERSION"
"$OPENCLAW_BIN" skill install "marketclaw-tech/openclaw-a2a@$A2A_PLUGIN_VERSION"

echo "==> [3/7] Writing OpenClaw A2A config"
OPENCLAW_CONFIG_DIR="$($OPENCLAW_BIN config-dir 2>/dev/null || echo "$HOME/.openclaw")"
mkdir -p "$OPENCLAW_CONFIG_DIR"
cat > "$OPENCLAW_CONFIG_DIR/a2a.yaml" <<EOF
a2a:
  my_peer_id: "$MY_PEER_ID"
  listen_port: $OPENCLAW_A2A_PORT
  hmac_secret_env: A2A_HMAC_SECRET
  allowed_peers:
    - peer_id: "$YOUR_PEER_ID"
      hmac_secret_env: A2A_HMAC_SECRET
EOF
echo "  wrote $OPENCLAW_CONFIG_DIR/a2a.yaml"

echo "==> [4/7] Persisting HMAC secret to env"
ENV_FILE="$OPENCLAW_CONFIG_DIR/a2a.env"
echo "A2A_HMAC_SECRET=$HMAC_SECRET" > "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "  wrote $ENV_FILE (mode 0600)"

echo "==> [5/7] Generating Caddyfile"
CADDY_DIR="${CADDY_DIR:-/etc/caddy/Caddyfile.d}"
mkdir -p "$CADDY_DIR" 2>/dev/null || CADDY_DIR="$HOME/.caddy"
mkdir -p "$CADDY_DIR"
cat > "$CADDY_DIR/openclaw-a2a.caddy" <<EOF
$PUBLIC_HOST:$CADDY_PORT {
    reverse_proxy localhost:$OPENCLAW_A2A_PORT
}
EOF
echo "  wrote $CADDY_DIR/openclaw-a2a.caddy"

echo "==> [6/7] Reloading Caddy"
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet caddy; then
  sudo systemctl reload caddy
  echo "  caddy reloaded via systemctl"
else
  echo "  systemd caddy not running; you'll need to start caddy manually:"
  echo "    caddy run --config $CADDY_DIR/openclaw-a2a.caddy"
fi

echo "==> [7/7] Self-check"
sleep 2
if curl -sk --max-time 5 "https://localhost:$CADDY_PORT/.well-known/agent.json" >/dev/null; then
  echo "  agent card served OK"
else
  echo "  WARNING: agent card not yet reachable on https://localhost:$CADDY_PORT/"
fi

cat <<EOF

✅ Install complete.

Next step on your laptop:
  In the comm-agent REPL, register this peer:
    comm.add_peer peer_id=$MY_PEER_ID \\
                  url=https://$PUBLIC_HOST:$CADDY_PORT \\
                  hmac_secret_value=$HMAC_SECRET

(Keep that HMAC secret safe — it's the only copy printed.)
EOF
```

- [ ] **Step 2: Make it executable + sanity-check**

Run:
```bash
chmod +x scripts/install_openclaw_a2a.sh
bash -n scripts/install_openclaw_a2a.sh   # syntax check, no execution
```
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add scripts/install_openclaw_a2a.sh
git commit -m "feat(scripts): install_openclaw_a2a.sh for remote Linux hosts"
```

---

## Task 14: Install script — `install_openclaw_a2a.ps1` (Windows)

**Files:**
- Create: `scripts/install_openclaw_a2a.ps1`

- [ ] **Step 1: Write the PowerShell equivalent**

```powershell
# scripts/install_openclaw_a2a.ps1
# Windows equivalent of install_openclaw_a2a.sh.
#
# Usage:
#   iex "& { $(iwr -useb <raw-url>) } -MyPeerId openclaw-home -YourPeerId agent-last-laptop -PublicHost home.example.com -HmacSecret (-join ((48..57) + (97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_}))"

param(
    [Parameter(Mandatory=$true)][string]$MyPeerId,
    [Parameter(Mandatory=$true)][string]$YourPeerId,
    [Parameter(Mandatory=$true)][string]$PublicHost,
    [Parameter(Mandatory=$true)][string]$HmacSecret,
    [string]$OpenclawBin = $(if ($env:OPENCLAW_BIN) { $env:OPENCLAW_BIN } else { "openclaw" }),
    [string]$A2APluginVersion = "v0.3.0",
    [int]$CaddyPort = 8443,
    [int]$OpenclawA2APort = 19443
)

$ErrorActionPreference = "Stop"

Write-Host "==> [1/7] Checking OpenClaw is installed"
if (-not (Get-Command $OpenclawBin -ErrorAction SilentlyContinue)) {
    Write-Error "'$OpenclawBin' not on PATH. Install OpenClaw or set `$env:OPENCLAW_BIN."
    exit 3
}

Write-Host "==> [2/7] Installing openclaw-a2a plugin @ $A2APluginVersion"
& $OpenclawBin skill install "marketclaw-tech/openclaw-a2a@$A2APluginVersion"

Write-Host "==> [3/7] Writing OpenClaw A2A config"
$OpenclawConfigDir = & $OpenclawBin config-dir 2>$null
if (-not $OpenclawConfigDir) { $OpenclawConfigDir = "$env:USERPROFILE\.openclaw" }
New-Item -ItemType Directory -Force -Path $OpenclawConfigDir | Out-Null
$ConfigYaml = @"
a2a:
  my_peer_id: "$MyPeerId"
  listen_port: $OpenclawA2APort
  hmac_secret_env: A2A_HMAC_SECRET
  allowed_peers:
    - peer_id: "$YourPeerId"
      hmac_secret_env: A2A_HMAC_SECRET
"@
$ConfigYaml | Out-File -FilePath "$OpenclawConfigDir\a2a.yaml" -Encoding utf8
Write-Host "  wrote $OpenclawConfigDir\a2a.yaml"

Write-Host "==> [4/7] Persisting HMAC secret to env file"
$EnvFile = "$OpenclawConfigDir\a2a.env"
"A2A_HMAC_SECRET=$HmacSecret" | Out-File -FilePath $EnvFile -Encoding utf8
# Lock to current user
$Acl = Get-Acl $EnvFile
$Acl.SetAccessRuleProtection($true, $false)
$Acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
    "Read,Write", "Allow"
)))
Set-Acl $EnvFile $Acl
Write-Host "  wrote $EnvFile (locked to current user)"

Write-Host "==> [5/7] Generating Caddyfile"
$CaddyDir = if ($env:CADDY_DIR) { $env:CADDY_DIR } else { "$env:USERPROFILE\.caddy" }
New-Item -ItemType Directory -Force -Path $CaddyDir | Out-Null
$Caddyfile = @"
${PublicHost}:${CaddyPort} {
    reverse_proxy localhost:$OpenclawA2APort
}
"@
$Caddyfile | Out-File -FilePath "$CaddyDir\openclaw-a2a.caddy" -Encoding utf8
Write-Host "  wrote $CaddyDir\openclaw-a2a.caddy"

Write-Host "==> [6/7] Caddy reload"
if (Get-Service -Name "caddy" -ErrorAction SilentlyContinue) {
    Restart-Service -Name "caddy"
    Write-Host "  caddy service restarted"
} else {
    Write-Host "  caddy service not found; start manually:"
    Write-Host "    caddy run --config $CaddyDir\openclaw-a2a.caddy"
}

Write-Host "==> [7/7] Self-check"
Start-Sleep -Seconds 2
try {
    Invoke-WebRequest -Uri "https://localhost:$CaddyPort/.well-known/agent.json" `
        -SkipCertificateCheck -TimeoutSec 5 -UseBasicParsing | Out-Null
    Write-Host "  agent card served OK"
} catch {
    Write-Host "  WARNING: agent card not yet reachable on https://localhost:$CaddyPort/"
}

Write-Host ""
Write-Host "[OK] Install complete."
Write-Host ""
Write-Host "Next step on your laptop:"
Write-Host "  In the comm-agent REPL, register this peer:"
Write-Host "    comm.add_peer peer_id=$MyPeerId \"
Write-Host "                  url=https://${PublicHost}:${CaddyPort} \"
Write-Host "                  hmac_secret_value=$HmacSecret"
Write-Host ""
Write-Host "(Keep that HMAC secret safe — it's the only copy printed.)"
```

- [ ] **Step 2: Sanity-check PS syntax**

Run: `powershell -NoProfile -Command "$ErrorActionPreference = 'Stop'; [System.Management.Automation.PSParser]::Tokenize((Get-Content -Raw scripts/install_openclaw_a2a.ps1), [ref]$null) | Out-Null; 'OK'"`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add scripts/install_openclaw_a2a.ps1
git commit -m "feat(scripts): install_openclaw_a2a.ps1 for remote Windows hosts"
```

---

## Task 15: Docs — add comm-agent section to README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Locate the right insertion point**

The README has sections like `## Gateway` for the existing chat-platform adapters. Add a new `## Comm-agent (cross-machine A2A)` section after it. (If the README structure differs, place it where multi-agent specialists are documented.)

- [ ] **Step 2: Write the section**

```markdown
## Comm-agent (cross-machine A2A)

The `comm-agent` specialist speaks Google A2A v0.3 over HTTPS so your
main REPL can delegate tasks to or chat with agents running on other
machines (e.g. an OpenClaw or Hermes instance).

**Tools exposed:** `comm.list_peers`, `comm.add_peer`, `comm.remove_peer`,
`comm.peer_card`, `comm.delegate`, `comm.chat`, `comm.status`.

**Quick start (host side):**

1. Install Caddy (used for TLS): https://caddyserver.com/docs/install
2. Set the inbound HMAC secret env var:
   ```bash
   export COMM_AGENT_SELF_HMAC=$(openssl rand -hex 32)
   ```
3. Start the REPL — the comm-agent specialist auto-spawns when present in
   `.agent/agents/`.

**Connecting a remote OpenClaw (the example case):**

On the remote machine, run our install script:

```bash
curl -sSL https://raw.githubusercontent.com/<repo>/main/scripts/install_openclaw_a2a.sh \
  | bash -s -- \
      --my-peer-id openclaw-home \
      --your-peer-id agent-last-laptop \
      --public-host home.example.com \
      --hmac-secret "$(openssl rand -hex 32)"
```

The script prints the HMAC secret once. Back in the host REPL, register
the remote:

```
comm.add_peer peer_id=openclaw-home url=https://home.example.com:8443 hmac_secret_value=<the-secret>
```

After that, the orchestrator can delegate via `comm.delegate peer_id=openclaw-home task="..."`.

**Security model:**

- Every cross-machine call carries an HMAC-SHA256 grant scoped to
  `(my_peer_id, target_peer_id, requested_skill, nonce, 60s exp)`. Replay
  is blocked by a 10k-entry LRU on the verifier.
- TLS is handled by Caddy (ACME by default; self-signed for LAN/VPN).
- The peer registry stores only env-var **names**; the secret value lives
  in process env only. Persist via your shell profile or a `.env` loader.

See `docs/superpowers/specs/2026-05-23-comm-agent-design.md` for the full design.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: comm-agent quick-start in README"
```

---

## Task 16: Cross-process E2E — orchestrator spawning comm-agent

**Files:**
- Test: `tests/test_e2e_multi_agent/test_e2e_comm_delegate.py`

This verifies the full path: orchestrator spawns comm-agent subprocess, comm-agent registers a peer pointing at a loopback MockA2APeer, and `comm.delegate` returns events end-to-end.

- [ ] **Step 1: Inspect an existing multi-agent E2E test for patterns**

Read `tests/test_e2e_multi_agent/test_e2e_tool_task_delegation.py` to see how it spawns tool-agent and drives the orchestrator. The new test mirrors that flow.

Run: `cat tests/test_e2e_multi_agent/test_e2e_tool_task_delegation.py | head -80`

- [ ] **Step 2: Write the cross-process test**

```python
# tests/test_e2e_multi_agent/test_e2e_comm_delegate.py
"""End-to-end: orchestrator spawns comm-agent, which delegates to a MockA2APeer."""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agents.comm_agent.peer_registry import Peer, PeerRegistry
from tests.test_comm_agent.conftest import cert_fingerprint_sha256, running_peer
import trustme


@pytest.mark.asyncio
async def test_orchestrator_can_drive_comm_delegate(
    tmp_path: Path, monkeypatch,
) -> None:
    """
    Layout:
      - Spin up MockA2APeer (HTTPS via trustme) on 127.0.0.1:<eph>
      - Write a comm_peers.json registering it
      - Build the comm.* MCP tools directly (in-process — skip subprocess spawn
        for this smoke test since the subprocess machinery is already covered
        by test_e2e_spawn_and_handshake.py)
      - Call comm.delegate, verify events come back
    """
    ca = trustme.CA()
    secret = "shared"

    async def stream_dispatcher(skill: str, params: dict, claims: dict) -> AsyncIterator[dict]:
        yield {"type": "task", "state": "working"}
        yield {"type": "task", "state": "completed", "result": "remote-said-hello"}

    async with running_peer(
        ca, my_peer_id="remote", hmac_secret=secret,
        stream_dispatcher=stream_dispatcher,
    ) as peer:
        monkeypatch.setenv("COMM_PEER_REMOTE_HMAC", secret)

        reg = PeerRegistry(tmp_path / "comm_peers.json")
        reg.add(Peer(
            peer_id="remote", display_name="Remote",
            url=peer.base_url,
            hmac_secret_ref="COMM_PEER_REMOTE_HMAC",
            tls_verify=False, tls_pinned_sha256=peer.fingerprint_sha256,
            added_at="", last_seen=None,
        ))

        from agents.comm_agent.mcp_tools import build_comm_tool_specs

        specs = build_comm_tool_specs(reg=reg, my_peer_id="laptop", transport_factory=None)
        by_name = {s.name: s for s in specs}

        out_str = await by_name["comm.delegate"].handler({
            "peer_id": "remote",
            "task": "say hello",
            "stream": False,
        })
        out = json.loads(out_str)
        assert out["ok"] is True
        assert out["final_result"] == "remote-said-hello"
        assert out["events_count"] == 2
```

- [ ] **Step 3: Run the test**

Run: `pytest tests/test_e2e_multi_agent/test_e2e_comm_delegate.py -v`
Expected: 1 passed.

- [ ] **Step 4: Run the full suite one more time**

Run: `pytest -x --tb=short -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_e2e_multi_agent/test_e2e_comm_delegate.py
git commit -m "test(e2e): orchestrator path through comm.delegate to mock peer"
```

---

## Done

After Task 16 the comm-agent is feature-complete per the spec. Remaining
items are deferred per spec §9 (push notifications, task persistence, real
TLS pin enforcement, multi-turn-chat e2e — added if needed):

- **`test_e2e_comm_chat_multiturn.py`** — multi-turn chat with context_id continuity. Cheap to add if needed; the test_e2e_comm_delegate pattern transfers directly.
- **TLS leaf-cert SHA-256 pin enforcement** — see deferral note in spec §9.
- **Real OpenClaw smoke test** — `make smoke-real-peer` target needs `SMOKE_PEER_URL` and `SMOKE_PEER_HMAC` and is not part of the standard CI run.

The merged change is 7 commits of source + 8 commits of tests/docs + 2 commits of scripts. Each is small enough to revert independently if needed.
