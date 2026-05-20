"""Pure-function tests for :mod:`gateway._feishu_common`.

These helpers are the shared substrate of both the webhook adapter
(:mod:`gateway.feishu`) and the WS adapter (:mod:`gateway.feishu_ws`), so a
bug here would silently break both delivery paths. The functions take
plain dicts or SDK-shaped objects -- no I/O, no fixtures beyond a stub
class for SDK shape.
"""

from __future__ import annotations

import json

import pytest

from gateway._feishu_common import (
    DEFAULT_DOMAIN,
    REPLY_CHAR_CAP,
    bot_was_mentioned,
    coerce_common,
    extract_sender_open_id,
    extract_text_from_dict,
    extract_text_from_obj,
    session_key_for,
    strip_mentions,
    truncate_reply,
)


# ---------------------------------------------------------------------------
# Helpers for the SDK-object shape used by feishu_ws
# ---------------------------------------------------------------------------


class _Id:
    def __init__(self, open_id: str = "", union_id: str = ""):
        self.open_id = open_id
        self.union_id = union_id


class _Mention:
    def __init__(self, key: str = "", open_id: str = "", name: str = ""):
        self.key = key
        self.id = _Id(open_id=open_id)
        self.name = name


class _Sender:
    def __init__(self, open_id: str = "", sender_type: str = "user"):
        self.sender_id = _Id(open_id=open_id)
        self.sender_type = sender_type


class _Message:
    def __init__(self, msg_type: str = "text", content: str = ""):
        self.message_type = msg_type
        self.content = content


# ---------------------------------------------------------------------------
# coerce_common
# ---------------------------------------------------------------------------


class TestCoerceCommon:
    def test_defaults_domain(self):
        out = coerce_common({"app_id": "x", "app_secret": "y"})
        assert out["domain"] == DEFAULT_DOMAIN

    def test_strips_whitespace(self):
        out = coerce_common({"app_id": "  x  ", "app_secret": "  y  "})
        assert out["app_id"] == "x"
        assert out["app_secret"] == "y"

    def test_keeps_explicit_domain(self):
        out = coerce_common({"app_id": "x", "app_secret": "y", "domain": "open.larksuite.com"})
        assert out["domain"] == "open.larksuite.com"

    def test_none_in_means_empty_out(self):
        # Caller's validation layer detects empties; coerce just normalises.
        out = coerce_common(None)
        assert out["app_id"] == "" and out["app_secret"] == ""
        assert out["domain"] == DEFAULT_DOMAIN


# ---------------------------------------------------------------------------
# extract_text_from_dict (webhook payload)
# ---------------------------------------------------------------------------


class TestExtractTextFromDict:
    def test_text_message(self):
        msg = {"message_type": "text", "content": json.dumps({"text": "你好"})}
        assert extract_text_from_dict(msg) == "你好"

    def test_text_strips_whitespace(self):
        msg = {"message_type": "text", "content": json.dumps({"text": "  hi  "})}
        assert extract_text_from_dict(msg) == "hi"

    def test_post_message_flattens_paragraphs(self):
        content = json.dumps({
            "content": [
                [
                    {"tag": "text", "text": "Hello "},
                    {"tag": "a", "text": "agent.md", "href": "..."},
                ],
                [{"tag": "text", "text": "second line"}],
            ]
        })
        msg = {"message_type": "post", "content": content}
        out = extract_text_from_dict(msg)
        assert out == "Hello agent.md\nsecond line"

    def test_post_at_mention(self):
        content = json.dumps({
            "content": [[{"tag": "at", "user_name": "bot"}, {"tag": "text", "text": " 在吗"}]]
        })
        out = extract_text_from_dict({"message_type": "post", "content": content})
        assert out == "@bot 在吗"

    def test_empty_content_returns_empty(self):
        assert extract_text_from_dict({"message_type": "text", "content": ""}) == ""

    def test_invalid_json_returns_empty(self):
        assert extract_text_from_dict({"message_type": "text", "content": "{not json"}) == ""

    def test_unknown_msg_type_returns_empty(self):
        msg = {"message_type": "image", "content": json.dumps({"image_key": "xxx"})}
        assert extract_text_from_dict(msg) == ""


# ---------------------------------------------------------------------------
# extract_text_from_obj (SDK shape)
# ---------------------------------------------------------------------------


class TestExtractTextFromObj:
    def test_text_object(self):
        msg = _Message(msg_type="text", content=json.dumps({"text": "hi"}))
        assert extract_text_from_obj(msg) == "hi"

    def test_empty_content(self):
        assert extract_text_from_obj(_Message(msg_type="text", content="")) == ""

    def test_invalid_json(self):
        assert extract_text_from_obj(_Message(msg_type="text", content="not-json")) == ""


# ---------------------------------------------------------------------------
# bot_was_mentioned
# ---------------------------------------------------------------------------


class TestBotWasMentioned:
    def test_no_mentions_false(self):
        assert bot_was_mentioned([], bot_open_id="ou_bot") is False

    def test_no_bot_id_falls_back_to_any_mention(self):
        # The "any mention present" fallback fires when our identity probe
        # failed: better to over-trigger than to silently never reply.
        assert bot_was_mentioned([{"id": {"open_id": "ou_other"}}], bot_open_id="") is True

    def test_dict_shape_exact_match(self):
        assert bot_was_mentioned(
            [{"id": {"open_id": "ou_bot"}}], bot_open_id="ou_bot"
        ) is True

    def test_dict_shape_other_user(self):
        assert bot_was_mentioned(
            [{"id": {"open_id": "ou_alice"}}], bot_open_id="ou_bot"
        ) is False

    def test_dict_shape_bot_among_others(self):
        mentions = [
            {"id": {"open_id": "ou_alice"}},
            {"id": {"open_id": "ou_bot"}},
        ]
        assert bot_was_mentioned(mentions, bot_open_id="ou_bot") is True

    def test_sdk_object_shape(self):
        assert bot_was_mentioned([_Mention(open_id="ou_bot")], bot_open_id="ou_bot") is True

    def test_mixed_shapes_in_same_list(self):
        mixed = [{"id": {"open_id": "ou_x"}}, _Mention(open_id="ou_bot")]
        assert bot_was_mentioned(mixed, bot_open_id="ou_bot") is True

    def test_iterator_input(self):
        # Generator gets exhausted once -- function must handle non-list iterables.
        gen = (m for m in [_Mention(open_id="ou_bot")])
        assert bot_was_mentioned(gen, bot_open_id="ou_bot") is True


# ---------------------------------------------------------------------------
# strip_mentions
# ---------------------------------------------------------------------------


class TestStripMentions:
    def test_strips_dict_key(self):
        text = "@_user_1 你好"
        assert strip_mentions(text, [{"key": "@_user_1"}]) == "你好"

    def test_strips_object_key(self):
        text = "@_user_1 在吗"
        assert strip_mentions(text, [_Mention(key="@_user_1")]) == "在吗"

    def test_strips_multiple(self):
        text = "@_user_1 @_user_2 hi"
        out = strip_mentions(text, [{"key": "@_user_1"}, {"key": "@_user_2"}])
        assert out == "hi"

    def test_empty_mentions_returns_unchanged(self):
        assert strip_mentions("hello", []) == "hello"
        assert strip_mentions("hello", None) == "hello"

    def test_mention_not_in_text_is_no_op(self):
        # Key advertised in payload but text doesn't actually contain it.
        assert strip_mentions("hello", [{"key": "@_user_9"}]) == "hello"


# ---------------------------------------------------------------------------
# truncate_reply
# ---------------------------------------------------------------------------


class TestTruncateReply:
    def test_short_text_unchanged(self):
        assert truncate_reply("hi") == "hi"

    def test_empty_unchanged(self):
        assert truncate_reply("") == ""

    def test_exactly_cap_unchanged(self):
        assert len(truncate_reply("x" * REPLY_CHAR_CAP)) == REPLY_CHAR_CAP

    def test_over_cap_truncates_to_cap(self):
        assert len(truncate_reply("x" * (REPLY_CHAR_CAP + 1000))) == REPLY_CHAR_CAP

    def test_truncation_has_suffix(self):
        assert truncate_reply("x" * (REPLY_CHAR_CAP + 1000)).endswith("过长)")

    def test_custom_cap_for_qq(self):
        # QQ wraps this helper with cap=3500.
        out = truncate_reply("x" * 8000, cap=3500)
        assert len(out) == 3500
        assert out.endswith("过长)")

    def test_tiny_cap_hard_truncates_without_suffix(self):
        # When cap is so small the suffix wouldn't fit, fall back to hard truncate.
        out = truncate_reply("x" * 200, cap=50)
        assert len(out) == 50


# ---------------------------------------------------------------------------
# session_key_for + extract_sender_open_id
# ---------------------------------------------------------------------------


class TestSessionKey:
    def test_with_chat_id(self):
        assert session_key_for("oc_abc") == "feishu:oc_abc"

    def test_empty_chat_id_returns_empty(self):
        # Empty key signals "no session memory" to the runner.
        assert session_key_for("") == ""


class TestExtractSenderOpenId:
    def test_dict_shape(self):
        assert extract_sender_open_id({"sender_id": {"open_id": "ou_x"}}) == "ou_x"

    def test_object_shape(self):
        assert extract_sender_open_id(_Sender(open_id="ou_y")) == "ou_y"

    def test_missing_returns_empty(self):
        assert extract_sender_open_id({}) == ""
        assert extract_sender_open_id(None) == ""
