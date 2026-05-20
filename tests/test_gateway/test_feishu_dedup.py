"""Tests for the Feishu WS message dedup.

Feishu re-pushes events when its ACK timer fires without seeing us
acknowledge. Our defense-in-depth: track every ``message_id`` we've
already accepted in a 24h LRU. The dedup helper is module-level
(``gateway.feishu_ws._is_duplicate_message``) because lark-oapi dispatches
events from multiple worker threads.
"""

from __future__ import annotations

import time

import pytest

from gateway import feishu_ws


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    """Each test starts with an empty dedup dict.

    The state is module-level so tests would interfere otherwise. Snapshot
    + restore is safer than ``.clear()`` because it preserves any state
    accidentally left by import-time code.
    """
    saved = dict(feishu_ws._seen_msg_ids)
    feishu_ws._seen_msg_ids.clear()
    try:
        yield
    finally:
        feishu_ws._seen_msg_ids.clear()
        feishu_ws._seen_msg_ids.update(saved)


class TestDedup:
    def test_first_time_message_is_new(self):
        assert feishu_ws._is_duplicate_message("om_a") is False

    def test_repeat_is_duplicate(self):
        feishu_ws._is_duplicate_message("om_a")
        assert feishu_ws._is_duplicate_message("om_a") is True

    def test_different_ids_independent(self):
        feishu_ws._is_duplicate_message("om_a")
        assert feishu_ws._is_duplicate_message("om_b") is False

    def test_expired_entry_evicted(self, monkeypatch):
        # Insert a fake entry with a stale timestamp; next call should
        # treat the new arrival as fresh after the cleanup pass.
        feishu_ws._seen_msg_ids["om_old"] = time.time() - feishu_ws._DEDUP_TTL - 10
        assert feishu_ws._is_duplicate_message("om_old") is False
        # And the second time we see it within the TTL, it's a duplicate.
        assert feishu_ws._is_duplicate_message("om_old") is True
