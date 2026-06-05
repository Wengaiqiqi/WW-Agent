"""_bootstrap spawns specialists concurrently, not serially.

Each specialist subprocess pays a multi-second cold start (langchain/langgraph
import + a2a-url handshake). Serial spawning stacked those latencies; the
bootstrap now overlaps them with ``asyncio.gather``. These tests guard the
concurrency (wall-clock < sum of per-spawn delays) and the preserved
optional-skip / registration semantics.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from orchestrator import main as orch_main
from orchestrator.router import CapabilityRouter


class _Tool:
    def __init__(self, name: str):
        self.name = name
        self.description = f"desc {name}"
        self.inputSchema = {"type": "object"}


class _Card:
    def __init__(self, cid: str, optional: bool = False):
        self.id = cid
        self.optional = optional


class _SlowHost:
    """Fake host whose spawn sleeps, so serial vs concurrent is observable."""

    def __init__(self, *, spawn_delay: float = 0.3, fail_ids: set[str] | None = None):
        self.spawn_delay = spawn_delay
        self.fail_ids = fail_ids or set()
        self.spawned: list[str] = []

    async def spawn(self, card) -> None:
        await asyncio.sleep(self.spawn_delay)
        if card.id in self.fail_ids:
            raise RuntimeError(f"boom: {card.id}")
        self.spawned.append(card.id)

    async def list_tools(self, agent_id: str):
        return [_Tool(f"{agent_id}.do")]

    def a2a_urls(self) -> dict[str, str]:
        return {cid: f"http://127.0.0.1/{cid}" for cid in self.spawned}

    @property
    def runtime_dir(self):
        # Legacy host: resolve from the global helper (the autouse fixture
        # points LANGCHAIN_AGENT_RUNTIME_DIR at a tmp dir), matching a real
        # MCPHost built without a per-turn turn_env.
        from agent_paths import runtime_dir

        return runtime_dir()


@pytest.fixture(autouse=True)
def _isolated_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_AGENT_RUNTIME_DIR", str(tmp_path / "runtime"))


@pytest.mark.asyncio
async def test_bootstrap_spawns_concurrently(monkeypatch):
    cards = [_Card("a"), _Card("b"), _Card("c")]
    monkeypatch.setattr(orch_main, "load_cards", lambda _dir: cards)

    host = _SlowHost(spawn_delay=0.3)
    router = CapabilityRouter()

    t0 = time.monotonic()
    await orch_main._bootstrap(host, router)
    elapsed = time.monotonic() - t0

    # Serial would be ~0.9s (3 × 0.3s); concurrent stays close to one delay.
    assert elapsed < 0.6, f"bootstrap looks serial: {elapsed:.2f}s"
    assert set(host.spawned) == {"a", "b", "c"}
    caps = set(router.all_capabilities())
    assert {"a.do", "b.do", "c.do"} <= caps


@pytest.mark.asyncio
async def test_bootstrap_skips_optional_failure(monkeypatch):
    cards = [_Card("a"), _Card("flaky", optional=True), _Card("c")]
    monkeypatch.setattr(orch_main, "load_cards", lambda _dir: cards)

    host = _SlowHost(spawn_delay=0.05, fail_ids={"flaky"})
    router = CapabilityRouter()

    await orch_main._bootstrap(host, router)

    assert set(host.spawned) == {"a", "c"}
    caps = set(router.all_capabilities())
    assert "flaky.do" not in caps
    assert {"a.do", "c.do"} <= caps


@pytest.mark.asyncio
async def test_bootstrap_reraises_required_failure(monkeypatch):
    cards = [_Card("a"), _Card("required")]  # not optional
    monkeypatch.setattr(orch_main, "load_cards", lambda _dir: cards)

    host = _SlowHost(spawn_delay=0.02, fail_ids={"required"})
    router = CapabilityRouter()

    with pytest.raises(RuntimeError, match="boom: required"):
        await orch_main._bootstrap(host, router)
