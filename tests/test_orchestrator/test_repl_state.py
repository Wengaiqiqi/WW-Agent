from __future__ import annotations

import os
from pathlib import Path

from orchestrator.repl_state import MAX_HISTORY_ITEMS, MultiAgentSessionState


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


def test_state_from_runtime_normalizes_invalid_permission_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_PERMISSION_MODE", "invalid-mode")

    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[],
        instruction_files=[],
        memory_snapshot="memory",
        workspace=tmp_path,
    )

    assert state.permission_mode == "workspace-write"
    assert os.environ["LANGCHAIN_AGENT_PERMISSION_MODE"] == "workspace-write"


def test_state_from_runtime_copies_instruction_files_and_skills(tmp_path):
    instruction_files = [tmp_path / "AGENTS.md"]
    skills = [{"name": "python"}]

    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=skills,
        instruction_files=instruction_files,
        memory_snapshot="memory",
        workspace=tmp_path,
    )

    assert state.instruction_files == instruction_files
    assert state.instruction_files is not instruction_files
    assert state.skills == skills
    assert state.skills is not skills


def test_set_permission_mode_updates_valid_mode_and_rejects_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_PERMISSION_MODE", "read-only")
    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[],
        instruction_files=[],
        memory_snapshot="memory",
        workspace=tmp_path,
    )

    assert state.set_permission_mode("danger-full-access") is True
    assert state.permission_mode == "danger-full-access"
    assert os.environ["LANGCHAIN_AGENT_PERMISSION_MODE"] == "danger-full-access"

    assert state.set_permission_mode("invalid-mode") is False
    assert state.permission_mode == "danger-full-access"
    assert os.environ["LANGCHAIN_AGENT_PERMISSION_MODE"] == "danger-full-access"


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


def test_record_turn_tracks_tool_calls_errors_and_trims_history(tmp_path):
    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[],
        instruction_files=[],
        memory_snapshot="memory",
        workspace=tmp_path,
    )

    state.record_turn(
        user_input="chat only",
        capability="",
        owner="router",
        observation="no tool",
        error="first error",
    )

    assert state.tool_calls == 0
    assert state.last_error == "first error"

    for index in range(MAX_HISTORY_ITEMS + 1):
        state.record_turn(
            user_input=f"user {index}",
            capability="read_file",
            owner="tool-agent",
            observation=f"observation {index}",
            error=None,
        )

    assert state.tool_calls == MAX_HISTORY_ITEMS + 1
    assert state.last_error is None
    assert len(state.recent_history) == MAX_HISTORY_ITEMS
    assert state.recent_history[0]["user"] == "user 1"
    assert state.recent_history[-1]["user"] == f"user {MAX_HISTORY_ITEMS}"


def test_planner_context_aggregates_all_fields(tmp_path):
    class _Instruction:
        path = tmp_path / "AGENTS.md"
        content = "Always be careful."

    class _Skill:
        name = "ppt-master"
        title = "PowerPoint"
        path = tmp_path / "skills" / "ppt-master"

    state = MultiAgentSessionState.from_runtime(
        active_cfg=_Cfg(),
        skills=[_Skill()],
        instruction_files=[_Instruction()],
        memory_snapshot="Remember user prefers concise answers.",
        workspace=tmp_path,
    )
    state.record_turn(
        user_input="read README", capability="read_file",
        owner="tool-agent", observation="README text", error=None,
    )

    context = state.render_planner_context(["read_file", "skill.ppt-master"])

    assert "mock" in context
    assert "mock-default" in context
    assert "read_file" in context
    assert "skill.ppt-master" in context
    assert "Always be careful." in context
    assert "Remember user prefers concise answers." in context
    assert "ppt-master" in context
    assert "README text" in context
