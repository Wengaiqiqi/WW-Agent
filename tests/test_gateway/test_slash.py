from __future__ import annotations

import json

import pytest

from gateway import slash


def test_platform_from_session_key():
    assert slash._platform_from_session_key("qq:123") == "qq"
    assert slash._platform_from_session_key("feishu:abc") == "feishu"
    assert slash._platform_from_session_key("") == ""
    assert slash._platform_from_session_key("nokey") == ""


def test_is_authorized_reads_allowlist(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("qq", {"app_id": "x", "allowed_users": "ou_a,ou_b"})
    assert slash._is_authorized("qq:123", "ou_a") is True
    assert slash._is_authorized("qq:123", "ou_b") is True
    assert slash._is_authorized("qq:123", "ou_other") is False


def test_is_authorized_empty_allowlist_denies(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("qq", {"app_id": "x"})  # no allowed_users
    assert slash._is_authorized("qq:123", "ou_a") is False


def test_is_authorized_no_user_denies(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("qq", {"allowed_users": "ou_a"})
    assert slash._is_authorized("qq:123", "") is False


def test_is_authorized_accepts_json_list(tmp_config_dir):
    from gateway import credentials as gw_creds

    gw_creds.save("feishu", {"allowed_users": ["ou_a", "ou_b"]})
    assert slash._is_authorized("feishu:c", "ou_b") is True


class _FakeHost:
    """Stands in for MCPHost.call_tool; returns canned comm.* JSON envelopes."""

    def __init__(self, responses: dict[str, dict] | None = None):
        self._responses = responses or {}
        self.calls: list[tuple[str, str, dict]] = []

    async def call_tool(self, agent_id: str, name: str, arguments: dict):
        self.calls.append((agent_id, name, arguments))
        payload = self._responses.get(name, {"ok": True})
        return {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        }


def _authorize(tmp_config_dir, platform="qq", user="ou_a"):
    from gateway import credentials as gw_creds
    gw_creds.save(platform, {"allowed_users": user})


@pytest.mark.asyncio
async def test_non_slash_returns_none(tmp_config_dir):
    host = _FakeHost()
    assert await slash.handle_slash("你好", host=host, session_key="qq:1", user_id="ou_a") is None
    assert host.calls == []


@pytest.mark.asyncio
async def test_unknown_command_returns_none(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost()
    assert await slash.handle_slash("/wat now", host=host, session_key="qq:1", user_id="ou_a") is None
    assert host.calls == []


@pytest.mark.asyncio
async def test_unauthorized_user_refused(tmp_config_dir):
    _authorize(tmp_config_dir, user="ou_owner")
    host = _FakeHost()
    reply = await slash.handle_slash("/peers", host=host, session_key="qq:1", user_id="ou_intruder")
    assert reply is not None
    assert "权限" in reply
    assert host.calls == []  # no comm tool touched


@pytest.mark.asyncio
async def test_help_lists_commands(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost()
    reply = await slash.handle_slash("/help", host=host, session_key="qq:1", user_id="ou_a")
    assert "/task" in reply and "/chat" in reply and "/peers" in reply


@pytest.mark.asyncio
async def test_peers_lists_registered(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost({"comm.list_peers": {"peers": [
        {"peer_id": "openclaw-home", "display_name": "Home box"},
    ]}})
    reply = await slash.handle_slash("/peers", host=host, session_key="qq:1", user_id="ou_a")
    assert "openclaw-home" in reply
    assert host.calls[0][1] == "comm.list_peers"


@pytest.mark.asyncio
async def test_task_delegates_and_renders_result(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost({"comm.delegate": {"final_result": "已总结:3 个要点", "events_count": 4}})
    reply = await slash.handle_slash(
        "/task openclaw-home 总结 ~/notes.md", host=host, session_key="qq:1", user_id="ou_a",
    )
    agent_id, tool, args = host.calls[0]
    assert tool == "comm.delegate"
    assert args["peer_id"] == "openclaw-home"
    assert args["task"] == "总结 ~/notes.md"
    assert args["stream"] is False
    assert "已总结:3 个要点" in reply


@pytest.mark.asyncio
async def test_task_renders_parts_dict_result(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost({"comm.delegate": {"final_result": {"parts": [{"text": "part-A"}, {"text": "part-B"}]}}})
    reply = await slash.handle_slash(
        "/task p hello", host=host, session_key="qq:1", user_id="ou_a",
    )
    assert "part-A" in reply and "part-B" in reply


@pytest.mark.asyncio
async def test_task_missing_args_shows_usage(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost()
    reply = await slash.handle_slash("/task openclaw-home", host=host, session_key="qq:1", user_id="ou_a")
    assert "用法" in reply
    assert host.calls == []


@pytest.mark.asyncio
async def test_task_surfaces_comm_error(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost({"comm.delegate": {"ok": False, "error": "unknown peer 'p'"}})
    reply = await slash.handle_slash("/task p do it", host=host, session_key="qq:1", user_id="ou_a")
    assert "unknown peer" in reply


@pytest.mark.asyncio
async def test_chat_replies_and_persists_context(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost({"comm.chat": {"reply": "你好呀", "context_id": "ctx-1"}})
    reply = await slash.handle_slash(
        "/chat openclaw-home 在吗", host=host, session_key="qq:1", user_id="ou_a",
    )
    _agent, tool, args = host.calls[0]
    assert tool == "comm.chat"
    assert args["peer_id"] == "openclaw-home"
    assert args["message"] == "在吗"
    assert args["context_id"] is None  # first turn: no prior context
    assert "你好呀" in reply
    assert slash._load_chat_context("qq:1", "openclaw-home") == "ctx-1"


@pytest.mark.asyncio
async def test_chat_reuses_saved_context(tmp_config_dir):
    _authorize(tmp_config_dir)
    slash._save_chat_context("qq:1", "openclaw-home", "ctx-existing")
    host = _FakeHost({"comm.chat": {"reply": "继续", "context_id": "ctx-existing"}})
    await slash.handle_slash(
        "/chat openclaw-home 接着聊", host=host, session_key="qq:1", user_id="ou_a",
    )
    assert host.calls[0][2]["context_id"] == "ctx-existing"


@pytest.mark.asyncio
async def test_chat_context_isolated_per_session_and_peer(tmp_config_dir):
    slash._save_chat_context("qq:1", "peerA", "ctxA")
    assert slash._load_chat_context("qq:1", "peerB") is None
    assert slash._load_chat_context("qq:2", "peerA") is None


@pytest.mark.asyncio
async def test_chat_missing_args_shows_usage(tmp_config_dir):
    _authorize(tmp_config_dir)
    host = _FakeHost()
    reply = await slash.handle_slash("/chat openclaw-home", host=host, session_key="qq:1", user_id="ou_a")
    assert "用法" in reply
    assert host.calls == []


@pytest.mark.asyncio
async def test_run_turn_routes_slash_and_skips_history(tmp_config_dir, monkeypatch):
    """A slash command is handled by handle_slash and NOT appended to the
    25-turn session history (it must not pollute planner context)."""
    from gateway import runner, session_store

    async def fake_bootstrap(host, router):
        return None

    async def fake_handle_slash(line, *, host, session_key, user_id):
        assert line == "/peers"
        return "SLASH_REPLY"

    monkeypatch.setattr(runner, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr("gateway.slash.handle_slash", fake_handle_slash)

    reply = await runner.run_turn(
        "/peers", session_key="qq:42", user_id="ou_a",
    )
    assert reply == "SLASH_REPLY"
    assert session_store.load("qq:42") == []


@pytest.mark.asyncio
async def test_run_turn_non_slash_still_reaches_planner(tmp_config_dir, monkeypatch):
    """A None from handle_slash must fall through to the normal planner path."""
    from gateway import runner

    async def fake_bootstrap(host, router):
        return None

    async def fake_handle_slash(line, *, host, session_key, user_id):
        return None

    called = {"dispatch": False}

    async def fake_dispatch(**kwargs):
        called["dispatch"] = True
        return "PLANNER_REPLY"

    monkeypatch.setattr(runner, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr("gateway.slash.handle_slash", fake_handle_slash)
    monkeypatch.setattr(runner, "_build_planner", lambda router, context_text="": (lambda state: {"capability": "", "response": "PLANNER_REPLY"}))
    monkeypatch.setattr(runner, "_dispatch_decision", fake_dispatch)

    reply = await runner.run_turn("ordinary message", session_key="qq:7", user_id="ou_a")
    assert called["dispatch"] is True
    assert reply == "PLANNER_REPLY"
