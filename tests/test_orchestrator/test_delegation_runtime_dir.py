"""Regression: A2A delegation must discover peers from the per-turn runtime dir.

The web bridge isolates each turn's specialists into a per-turn runtime dir
(``.agent/runtime/web-<id>``) handed to the spawned subprocesses via
``turn_env``. But the *parent-side* discovery (host sidecar read, peers.json
write, and ``delegate_task``'s peers lookup) historically called the
process-global ``agent_paths.runtime_dir()`` instead — so it read/wrote the
DEFAULT ``.agent/runtime`` shared with any REPL/gateway on the same cwd. The
web turn then delegated to whatever stale/foreign ``peers.json`` lived there,
producing ``httpx.ConnectError: All connection attempts failed``.

These tests pin the fix: the per-turn dir flows explicitly, sourced from the
MCPHost that already knows it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import agent_paths
from orchestrator.a2a_client import _load_peers, delegate_task
from orchestrator.delegation import delegate_via_a2a_stream
from orchestrator.mcp_host import MCPHost


def _write_peers(d: Path, peers: dict) -> None:
    import json

    d.mkdir(parents=True, exist_ok=True)
    (d / "peers.json").write_text(json.dumps(peers), encoding="utf-8")


def test_load_peers_reads_the_given_runtime_dir(tmp_path):
    _write_peers(tmp_path, {"tool-agent": "http://127.0.0.1:9"})
    assert _load_peers(tmp_path) == {"tool-agent": "http://127.0.0.1:9"}


def test_load_peers_without_arg_uses_global_default(monkeypatch, tmp_path):
    # Backward-compat: REPL/gateway/CLI set LANGCHAIN_AGENT_RUNTIME_DIR in
    # os.environ, so the no-arg path must still resolve via the global helper.
    monkeypatch.setenv("LANGCHAIN_AGENT_RUNTIME_DIR", str(tmp_path))
    _write_peers(tmp_path, {"skill-agent": "http://127.0.0.1:8"})
    assert _load_peers() == {"skill-agent": "http://127.0.0.1:8"}


@pytest.mark.asyncio
async def test_delegate_task_looks_up_peers_in_the_passed_dir(tmp_path):
    # peers.json exists in the per-turn dir but the requested peer is absent:
    # we should get "unknown peer" (proving it loaded THIS dir), not
    # "peers file not found".
    _write_peers(tmp_path, {"tool-agent": "http://127.0.0.1:9"})
    with pytest.raises(RuntimeError, match="unknown peer: nope"):
        async for _ in delegate_task(
            peer_id="nope", task="t", meta={}, runtime_dir=tmp_path,
        ):
            pass


@pytest.mark.asyncio
async def test_delegate_task_missing_peers_points_at_the_passed_dir(tmp_path):
    missing = tmp_path / "empty"
    with pytest.raises(RuntimeError, match="peers file not found") as exc:
        async for _ in delegate_task(
            peer_id="tool-agent", task="t", meta={}, runtime_dir=missing,
        ):
            pass
    assert str(missing) in str(exc.value)


def test_mcp_host_runtime_dir_from_turn_env(tmp_path):
    host = MCPHost(
        hmac_key="k",
        turn_env={"LANGCHAIN_AGENT_RUNTIME_DIR": str(tmp_path)},
    )
    assert host.runtime_dir == tmp_path


def test_mcp_host_runtime_dir_falls_back_to_global(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_RUNTIME_DIR", str(tmp_path))
    host = MCPHost(hmac_key="k")  # legacy path: no turn_env
    assert host.runtime_dir == tmp_path


@pytest.mark.asyncio
async def test_stream_threads_runtime_dir_into_the_real_delegate(monkeypatch, tmp_path):
    captured: dict = {}

    async def _fake_delegate_task(*, peer_id, task, meta, context="", runtime_dir=None):
        captured["runtime_dir"] = runtime_dir
        yield {"type": "done", "text": ""}

    monkeypatch.setattr(
        "orchestrator.a2a_client.delegate_task", _fake_delegate_task,
    )

    async for _ in delegate_via_a2a_stream(
        capability="tool.task",
        arguments={"task": "x"},
        user_input="x",
        hmac_key="k",
        trace_id="t",
        permission_mode="workspace-write",
        runtime_dir=tmp_path,
    ):
        pass

    assert captured["runtime_dir"] == tmp_path
