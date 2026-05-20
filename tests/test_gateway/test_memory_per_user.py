"""Tests for per-user memory scoping in :mod:`tool.tool_memory`.

The gateway sets ``LANGCHAIN_AGENT_MEMORY_USER`` before spawning tool-agent;
subprocesses inherit it; ``tool_memory._path_for`` then routes reads/writes
to ``memories/users/<sha256>/``. This is what keeps multi-user chat
platforms (Feishu group, QQ group) from leaking one person's facts into
another person's prompt.
"""

from __future__ import annotations

import pytest

from tool.tool_memory import _path_for, memory, snapshot_for_system_prompt


class TestPathScoping:
    def test_no_env_uses_global_path(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
        # Path goes directly under memories/ (legacy single-user shape).
        p = _path_for("user")
        assert p.name == "USER.md"
        assert "users" not in p.parts

    def test_env_set_routes_under_users_subdir(self, tmp_config_dir, monkeypatch):
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_aaa")
        p = _path_for("user")
        # Two users -> two distinct subdirs under memories/users/.
        assert "users" in p.parts
        assert p.name == "USER.md"

    def test_different_users_different_dirs(self, tmp_config_dir, monkeypatch):
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_aaa")
        path_a = _path_for("user")
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_bbb")
        path_b = _path_for("user")
        assert path_a.parent != path_b.parent


class TestPerUserIsolation:
    def test_user_a_does_not_see_user_b_memory(
        self, tmp_config_dir, monkeypatch
    ):
        # User A writes their name.
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_aaa")
        memory(action="add", target="user", content="名字是张三")
        # Switch to user B; their snapshot should be empty.
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_bbb")
        assert snapshot_for_system_prompt() == ""
        # User A still sees their fact when we switch back.
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_aaa")
        assert "张三" in snapshot_for_system_prompt()

    def test_global_scope_separate_from_per_user(
        self, tmp_config_dir, monkeypatch
    ):
        # Write a fact globally (REPL-style).
        monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
        memory(action="add", target="user", content="全局事实")
        # A gateway-scoped user must NOT see the global fact.
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_aaa")
        assert snapshot_for_system_prompt() == ""


class TestActions:
    def test_add_then_read(self, tmp_config_dir, monkeypatch):
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_test")
        r = memory(action="add", target="user", content="aa is a tester")
        assert r["success"] is True

        r2 = memory(action="read", target="user")
        assert any("aa is a tester" in e for e in r2.get("entries", []))

    def test_replace(self, tmp_config_dir, monkeypatch):
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_test")
        memory(action="add", target="user", content="姓名: 张三")
        r = memory(action="replace", target="user",
                   old_text="张三", content="姓名: 李四")
        assert r["success"] is True
        entries = memory(action="read", target="user").get("entries", [])
        assert any("李四" in e for e in entries)
        assert not any("张三" in e for e in entries)

    def test_remove(self, tmp_config_dir, monkeypatch):
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_test")
        memory(action="add", target="user", content="某事")
        r = memory(action="remove", target="user", old_text="某事")
        assert r["success"] is True
        assert memory(action="read", target="user").get("entries", []) == []

    def test_duplicate_add_is_idempotent(self, tmp_config_dir, monkeypatch):
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_test")
        memory(action="add", target="user", content="x")
        memory(action="add", target="user", content="x")
        entries = memory(action="read", target="user").get("entries", [])
        # Duplicate entries are silently de-duped, keeping the user file tidy.
        assert entries.count("x") == 1


class TestSnapshot:
    def test_empty_snapshot(self, tmp_config_dir, monkeypatch):
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_test")
        assert snapshot_for_system_prompt() == ""

    def test_snapshot_renders_entries(self, tmp_config_dir, monkeypatch):
        monkeypatch.setenv("LANGCHAIN_AGENT_MEMORY_USER", "ou_test")
        memory(action="add", target="user", content="名字是张三")
        memory(action="add", target="memory", content="项目使用 deepseek")
        out = snapshot_for_system_prompt()
        assert "张三" in out
        assert "deepseek" in out
        # Both sections should be labelled so the planner can tell user
        # profile facts from project notes.
        assert "USER.md" in out or "user" in out.lower()
