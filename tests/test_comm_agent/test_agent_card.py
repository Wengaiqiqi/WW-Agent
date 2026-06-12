"""Tests for agents/comm_agent/agent_card.py."""
from __future__ import annotations

import pytest

from agents.comm_agent.agent_card import (
    AgentCardError, build_self_card, validate_card,
)


def test_build_self_card_minimal() -> None:
    card = build_self_card(
        name="ww-agent-comm",
        description="test",
        public_url="https://example.com:8443",
        version="1.0.0",
    )
    assert card["schemaVersion"] == "0.3"
    assert card["name"] == "ww-agent-comm"
    assert card["url"] == "https://example.com:8443"
    assert card["capabilities"]["streaming"] is True
    assert card["capabilities"]["pushNotifications"] is False


def test_build_self_card_includes_skills() -> None:
    card = build_self_card(
        name="x", description="x", public_url="https://x:8443", version="1.0",
    )
    skill_ids = [s["id"] for s in card["skills"]]
    assert "task.delegate" in skill_ids
    assert "chat.message" in skill_ids
    assert "status.query" in skill_ids


def test_validate_card_accepts_self_card() -> None:
    card = build_self_card(
        name="x", description="x", public_url="https://x:8443", version="1.0",
    )
    validate_card(card)  # should not raise


def test_validate_card_rejects_missing_name() -> None:
    card = build_self_card(
        name="x", description="x", public_url="https://x:8443", version="1.0",
    )
    del card["name"]
    with pytest.raises(AgentCardError, match="missing required field 'name'"):
        validate_card(card)


def test_validate_card_rejects_wrong_schema_version() -> None:
    card = build_self_card(
        name="x", description="x", public_url="https://x:8443", version="1.0",
    )
    card["schemaVersion"] = "0.1"
    with pytest.raises(AgentCardError, match="schemaVersion"):
        validate_card(card)


def test_validate_card_forward_compat_with_unknown_fields() -> None:
    """Unknown extra fields are allowed (spec §8: forward-compat)."""
    card = build_self_card(
        name="x", description="x", public_url="https://x:8443", version="1.0",
    )
    card["futureExtension"] = {"foo": "bar"}
    validate_card(card)  # tolerated
