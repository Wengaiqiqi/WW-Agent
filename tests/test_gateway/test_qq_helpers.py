"""Pure-helper tests for :mod:`gateway.qq`.

Covers the standalone utilities (truncate, mention strip, msg_seq, safe
json) -- the network / WS code is integration-test territory and not in
this file. Goal: catch regressions in the small functions that are easy
to break and easy to verify.
"""

from __future__ import annotations

import json

import pytest

from gateway.qq import (
    _AT_MENTION_RE,
    _msg_seq,
    _safe_json,
    _strip_at_mentions,
    _truncate_qq_reply,
)


# ---------------------------------------------------------------------------
# _truncate_qq_reply — cap=3500, suffix matches feishu_common helper
# ---------------------------------------------------------------------------


class TestTruncateQQReply:
    def test_short_unchanged(self):
        assert _truncate_qq_reply("hello") == "hello"

    def test_empty_unchanged(self):
        assert _truncate_qq_reply("") == ""

    def test_under_cap_unchanged(self):
        text = "x" * 3000
        assert _truncate_qq_reply(text) == text

    def test_at_cap_unchanged(self):
        text = "x" * 3500
        assert _truncate_qq_reply(text) == text

    def test_over_cap_trimmed_to_3500(self):
        out = _truncate_qq_reply("x" * 9000)
        assert len(out) == 3500

    def test_includes_truncation_suffix(self):
        out = _truncate_qq_reply("x" * 9000)
        assert "过长" in out


# ---------------------------------------------------------------------------
# _strip_at_mentions + _AT_MENTION_RE — guild-style ``<@!123>`` removal
# ---------------------------------------------------------------------------


class TestStripAtMentions:
    def test_strips_simple_mention(self):
        assert _strip_at_mentions("<@!12345> 你好") == "你好"

    def test_strips_mention_without_bang(self):
        assert _strip_at_mentions("<@67890> hi") == "hi"

    def test_strips_multiple(self):
        assert _strip_at_mentions("<@!1> <@!2> hello") == "hello"

    def test_no_mention_unchanged(self):
        assert _strip_at_mentions("plain message") == "plain message"

    def test_empty_string(self):
        assert _strip_at_mentions("") == ""

    def test_only_mention_returns_empty(self):
        assert _strip_at_mentions("<@!12345>") == ""

    def test_compiled_pattern_is_module_level(self):
        # T8: regex should be precompiled once, not built per call. We can't
        # observe per-call cost in a test, but we can verify the compiled
        # object is exposed and works.
        assert _AT_MENTION_RE.pattern == r"<@!?\d+>\s*"


# ---------------------------------------------------------------------------
# _msg_seq — bounded random int in 1..1e9 range
# ---------------------------------------------------------------------------


class TestMsgSeq:
    def test_in_valid_range(self):
        for _ in range(100):
            seq = _msg_seq()
            assert 1 <= seq <= 999_999_999

    def test_varies(self):
        # 100 calls -> very high probability of at least one distinct value.
        # If they all match we either have a 1-in-10^18 fluke or a real bug.
        values = {_msg_seq() for _ in range(100)}
        assert len(values) > 1


# ---------------------------------------------------------------------------
# _safe_json — returns None instead of raising on parse failure
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: str, raises: bool = False):
        self._body = body
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return json.loads(self._body)


class TestSafeJson:
    def test_valid_json_returns_parsed(self):
        resp = _FakeResponse(json.dumps({"code": 0, "msg": "ok"}))
        assert _safe_json(resp) == {"code": 0, "msg": "ok"}

    def test_invalid_json_returns_none(self):
        # The whole point: don't crash the caller when the server returns
        # HTML or empty body on a 5xx.
        assert _safe_json(_FakeResponse("", raises=True)) is None

    def test_raised_exception_returns_none(self):
        assert _safe_json(_FakeResponse("nope", raises=True)) is None
