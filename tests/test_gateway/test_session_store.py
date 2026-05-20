"""Tests for :mod:`gateway.session_store`.

Covers the 25-turn rolling history per chat: load/append cycle, cap
enforcement, hash-derived filename per session_key, the budget-aware
``format_for_prompt`` renderer, and ``clear`` idempotency.

All tests use the ``tmp_config_dir`` fixture so each test's storage is
isolated.
"""

from __future__ import annotations

import json

from gateway import session_store


KEY = "feishu:oc_test"


class TestLoadAppend:
    def test_load_empty_session_returns_empty_list(self, tmp_config_dir):
        assert session_store.load(KEY) == []

    def test_load_with_empty_key_returns_empty(self, tmp_config_dir):
        # Empty session_key means "no memory wanted" -- callers pass "" when
        # the platform layer can't derive a chat id.
        assert session_store.load("") == []

    def test_append_then_load_roundtrip(self, tmp_config_dir):
        session_store.append(KEY, "hi", "hello")
        hist = session_store.load(KEY)
        assert hist == [("user", "hi"), ("assistant", "hello")]

    def test_append_with_empty_key_is_noop(self, tmp_config_dir):
        # Refusing to write to an empty key prevents accidental cross-chat
        # pollution if a caller forgets to compute session_key.
        session_store.append("", "u", "a")
        # Nothing should have been written to disk; we can't check "nothing"
        # directly, but a subsequent load on a real key is empty.
        assert session_store.load(KEY) == []

    def test_multiple_appends_preserve_order(self, tmp_config_dir):
        session_store.append(KEY, "u1", "a1")
        session_store.append(KEY, "u2", "a2")
        session_store.append(KEY, "u3", "a3")
        hist = session_store.load(KEY)
        assert hist == [
            ("user", "u1"), ("assistant", "a1"),
            ("user", "u2"), ("assistant", "a2"),
            ("user", "u3"), ("assistant", "a3"),
        ]


class TestCapAndTrim:
    def test_under_cap_keeps_all(self, tmp_config_dir):
        for i in range(5):
            session_store.append(KEY, f"u{i}", f"a{i}")
        assert len(session_store.load(KEY)) == 10

    def test_at_cap_keeps_all(self, tmp_config_dir):
        # _MAX_MESSAGES = HISTORY_TURNS * 2 = 50
        for i in range(session_store.HISTORY_TURNS):
            session_store.append(KEY, f"u{i}", f"a{i}")
        assert len(session_store.load(KEY)) == session_store.HISTORY_TURNS * 2

    def test_over_cap_evicts_oldest(self, tmp_config_dir):
        # Push past the cap and verify the front gets dropped.
        for i in range(session_store.HISTORY_TURNS + 5):
            session_store.append(KEY, f"u{i}", f"a{i}")
        hist = session_store.load(KEY)
        assert len(hist) == session_store.HISTORY_TURNS * 2
        # First retained should be u5 (turns 0..4 evicted).
        assert hist[0] == ("user", "u5")
        # Last should be the most recent assistant.
        assert hist[-1] == ("assistant", f"a{session_store.HISTORY_TURNS + 4}")


class TestPerSessionIsolation:
    def test_two_keys_dont_share(self, tmp_config_dir):
        session_store.append("feishu:chat_A", "hi A", "hello A")
        session_store.append("feishu:chat_B", "hi B", "hello B")
        assert session_store.load("feishu:chat_A") == [
            ("user", "hi A"), ("assistant", "hello A"),
        ]
        assert session_store.load("feishu:chat_B") == [
            ("user", "hi B"), ("assistant", "hello B"),
        ]

    def test_file_name_is_hash_not_raw_key(self, tmp_config_dir):
        # Raw open_ids contain characters Windows file systems don't like.
        # The on-disk filename must be a hex digest, not the literal key.
        weird_key = "feishu:oc_with/slash:and?question"
        session_store.append(weird_key, "u", "a")
        sessions = list((tmp_config_dir / "sessions").glob("*.json"))
        assert len(sessions) == 1
        # The filename should not contain any of the problem chars.
        name = sessions[0].name
        assert "/" not in name and "?" not in name and ":" not in name.replace(".json", "")


class TestFormatForPrompt:
    def test_empty_history_returns_empty_string(self, tmp_config_dir):
        assert session_store.format_for_prompt([]) == ""

    def test_renders_user_and_assistant(self, tmp_config_dir):
        out = session_store.format_for_prompt([
            ("user", "你好"),
            ("assistant", "你好!"),
        ])
        assert out.startswith("Recent conversation:")
        assert "User: 你好" in out
        assert "Assistant: 你好!" in out

    def test_keeps_most_recent_when_over_budget(self, tmp_config_dir):
        # Make the formatter drop early entries by setting a tight char budget.
        history = [("user", "EARLY"), ("assistant", "early-reply")]
        history += [("user", "x" * 100), ("assistant", "y" * 100)] * 20
        history.append(("user", "LATEST"))
        history.append(("assistant", "latest-reply"))
        out = session_store.format_for_prompt(history, max_chars=500)
        # The latest turn MUST be present.
        assert "LATEST" in out
        # The earliest entry should have been dropped.
        assert "EARLY" not in out

    def test_preserves_display_order_after_budget_trim(self, tmp_config_dir):
        history = [
            ("user", "Q1"), ("assistant", "A1"),
            ("user", "Q2"), ("assistant", "A2"),
            ("user", "Q3"), ("assistant", "A3"),
        ]
        out = session_store.format_for_prompt(history)
        # Even with reverse-walk budget logic, the rendered order must be
        # oldest -> newest so the LLM sees the conversation forward.
        assert out.index("Q1") < out.index("Q2") < out.index("Q3")


class TestClear:
    def test_clear_removes_session(self, tmp_config_dir):
        session_store.append(KEY, "u", "a")
        assert session_store.load(KEY) != []
        session_store.clear(KEY)
        assert session_store.load(KEY) == []

    def test_clear_nonexistent_is_noop(self, tmp_config_dir):
        # Must not raise -- callers use this defensively.
        session_store.clear("feishu:never-existed")

    def test_clear_empty_key_is_noop(self, tmp_config_dir):
        session_store.clear("")


class TestPersistence:
    def test_round_trips_unicode_correctly(self, tmp_config_dir):
        session_store.append(KEY, "你好", "我是 WW Agent")
        # Read the file directly to confirm utf-8 encoding survives.
        f = next((tmp_config_dir / "sessions").glob("*.json"))
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["messages"][0]["text"] == "你好"
        assert data["messages"][1]["text"] == "我是 WW Agent"

    def test_corrupted_file_returns_empty(self, tmp_config_dir):
        session_store.append(KEY, "u", "a")
        f = next((tmp_config_dir / "sessions").glob("*.json"))
        f.write_text("not valid json", encoding="utf-8")
        # Caller gets an empty list rather than a raised exception -- a fresh
        # chat is better than a hard crash on every message.
        assert session_store.load(KEY) == []
