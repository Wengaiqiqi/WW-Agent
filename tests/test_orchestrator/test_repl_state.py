from __future__ import annotations

from pathlib import Path

from orchestrator.repl_state import MultiAgentSessionState


class _Cfg:
    provider = "mock"
    model = "mock-default"
    protocol = "openai"
    base_url = "http://mock.invalid/v1"
    api_key_env = "MOCK_API_KEY"


def test_state_from_runtime_loads_config_and_static_context(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_PERMISSION_MODE", "read-only")

    instruction_file = tmp_path / "AGENTS.md"
    instruction_file.write_text("Project rule", encoding="utf-8")

    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[],
        instruction_files=[],
        memory_snapshot="# Memory\nRemember this.",
        workspace=tmp_path,
    )

    assert state.provider == "mock"
    assert state.model == "mock-default"
    assert state.protocol == "openai"
    assert state.base_url == "http://mock.invalid/v1"
    assert state.api_key_env == "MOCK_API_KEY"
    assert state.permission_mode == "read-only"
    assert state.thread_id == "multi-agent-session-1"
    assert state.turns == 0
    assert state.recent_history == []
    assert state.memory_snapshot == "# Memory\nRemember this."
    assert state.workspace == tmp_path


def test_state_records_turn_result_and_compacts(tmp_path):
    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[],
        instruction_files=[],
        memory_snapshot="memory",
        workspace=tmp_path,
    )

    state.record_turn(
        user_input="read README",
        capability="read_file",
        owner="tool-agent",
        observation="README contents",
        error=None,
    )

    assert state.turns == 1
    assert state.seen_messages == 1
    assert state.recent_history == [
        {
            "user": "read README",
            "capability": "read_file",
            "owner": "tool-agent",
            "observation": "README contents",
            "error": None,
        }
    ]

    state.compact(memory_snapshot="fresh memory")

    assert state.compacted_turns == 1
    assert state.turns == 0
    assert state.seen_messages == 0
    assert state.thread_id == "multi-agent-session-2"
    assert state.recent_history == []
    assert state.memory_snapshot == "fresh memory"
