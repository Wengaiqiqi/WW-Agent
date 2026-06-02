from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.specialist_pool import Lease, SpecialistPool, pool_signature
from orchestrator.turn_context import TurnContext


def _ctx(**over) -> TurnContext:
    base = dict(turn_id="t", user_id="alice", workspace_root=Path("/ws/alice"),
                permission_mode="workspace-write", model_id="deepseek/chat",
                base_url="", api_key="", protocol="", session_key="s",
                trace_id="tr", hmac_key="per-turn-ignored",
                runtime_dir=Path("/rt/per-turn-ignored"))
    base.update(over)
    return TurnContext(**base)


def test_pool_signature_excludes_permission_runtime_hmac():
    a = _ctx(permission_mode="read-only", hmac_key="h1", runtime_dir=Path("/a"))
    b = _ctx(permission_mode="workspace-write", hmac_key="h2", runtime_dir=Path("/b"))
    # Same user/workspace/model/endpoint => same signature despite differing
    # permission_mode / hmac_key / runtime_dir.
    assert pool_signature(a) == pool_signature(b)
    # A different user is a different signature.
    assert pool_signature(a) != pool_signature(_ctx(user_id="bob"))
    # A different endpoint is a different signature.
    assert pool_signature(a) != pool_signature(_ctx(base_url="https://x/v1"))


class _FakeHost:
    """Records the env it was created with and whether it was shut down."""
    def __init__(self, *, hmac_key, turn_env):
        self.hmac_key = hmac_key
        self.turn_env = dict(turn_env)
        self.shutdown_called = False

    async def shutdown_all(self):
        self.shutdown_called = True


def _make_pool(**over):
    spawned: list[_FakeHost] = []

    async def factory(*, signature, runtime_dir, hmac_key):
        host = _FakeHost(hmac_key=hmac_key,
                         turn_env={"LANGCHAIN_AGENT_RUNTIME_DIR": str(runtime_dir)})
        spawned.append(host)
        return host, object()  # (host, router)

    kw = dict(factory=factory, max_hosts=8, idle_ttl=60.0)
    kw.update(over)
    return SpecialistPool(**kw), spawned


@pytest.mark.asyncio
async def test_acquire_cold_spawns_then_release_pools_for_reuse():
    pool, spawned = _make_pool()

    lease1 = await pool.acquire(_ctx())
    assert isinstance(lease1, Lease)
    assert len(spawned) == 1                 # cold spawn
    assert lease1.hmac_key == spawned[0].hmac_key  # host's baked key, not ctx's
    assert lease1.hmac_key != "per-turn-ignored"

    await pool.release(lease1)               # back to idle, NOT shut down
    assert spawned[0].shutdown_called is False

    lease2 = await pool.acquire(_ctx(turn_id="t2", hmac_key="other"))
    assert len(spawned) == 1                 # REUSED — no second spawn
    assert lease2.host is lease1.host
    assert lease2.hmac_key == lease1.hmac_key  # reused host => reused key


@pytest.mark.asyncio
async def test_acquire_different_signature_spawns_separate_host():
    pool, spawned = _make_pool()
    a = await pool.acquire(_ctx(user_id="alice"))
    b = await pool.acquire(_ctx(user_id="bob"))
    assert len(spawned) == 2
    assert a.host is not b.host


import asyncio as _asyncio


@pytest.mark.asyncio
async def test_lru_evicts_oldest_idle_when_over_cap():
    pool, spawned = _make_pool(max_hosts=2)
    clock = {"t": 100.0}
    pool._now = lambda: clock["t"]  # deterministic LRU ordering

    a = await pool.acquire(_ctx(user_id="a"))
    b = await pool.acquire(_ctx(user_id="b"))
    clock["t"] = 101.0
    await pool.release(a)          # a idle, last_used=101
    clock["t"] = 102.0
    await pool.release(b)          # b idle, last_used=102 (newer)

    # New signature at cap (2) -> evict the OLDEST idle (a), keep b.
    c = await pool.acquire(_ctx(user_id="c"))
    await _asyncio.sleep(0)        # let the eviction task run
    assert spawned[0].shutdown_called is True   # a evicted
    assert spawned[1].shutdown_called is False  # b kept
    assert len(spawned) == 3                     # c cold-spawned


@pytest.mark.asyncio
async def test_acquire_blocks_until_release_when_all_leased_at_cap():
    pool, spawned = _make_pool(max_hosts=1)
    a = await pool.acquire(_ctx(user_id="a"))

    started = _asyncio.Event()

    async def waiter():
        started.set()
        # Different signature, but cap=1 and the only host is leased -> must wait.
        return await pool.acquire(_ctx(user_id="b"))

    task = _asyncio.create_task(waiter())
    await started.wait()
    await _asyncio.sleep(0.05)
    assert not task.done()         # still blocked at cap

    await pool.release(a)          # frees the slot (a is idle, evictable)
    lease_b = await _asyncio.wait_for(task, timeout=1.0)
    assert lease_b.host is not a.host


@pytest.mark.asyncio
async def test_sweep_shuts_down_idle_past_ttl_only():
    pool, spawned = _make_pool(max_hosts=4, idle_ttl=30.0)
    clock = {"t": 0.0}
    pool._now = lambda: clock["t"]

    a = await pool.acquire(_ctx(user_id="a"))
    b = await pool.acquire(_ctx(user_id="b"))
    clock["t"] = 10.0
    await pool.release(a)          # idle at 10
    clock["t"] = 50.0
    await pool.release(b)          # idle at 50

    clock["t"] = 50.0              # a idle 40s (> ttl), b idle 0s
    await pool.sweep()
    assert spawned[0].shutdown_called is True    # a swept
    assert spawned[1].shutdown_called is False   # b kept


@pytest.mark.asyncio
async def test_drain_shuts_down_all_hosts():
    pool, spawned = _make_pool()
    a = await pool.acquire(_ctx(user_id="a"))
    b = await pool.acquire(_ctx(user_id="b"))
    await pool.release(a)          # a idle, b still leased
    await pool.drain()
    assert all(h.shutdown_called for h in spawned)  # drains idle AND leased
