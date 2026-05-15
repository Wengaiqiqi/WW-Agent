from pathlib import Path
from orchestrator.registry import load_cards, Card


def test_load_cards_finds_tool_agent(tmp_path):
    cards_dir = tmp_path / ".agent" / "agents"
    cards_dir.mkdir(parents=True)
    (cards_dir / "tool-agent.card.json").write_text(
        '{"id":"tool-agent","display_name":"T","version":"1","entrypoint":'
        '{"type":"python","module":"agents.tool_agent.main","args":[]},'
        '"mcp":{"transport":"stdio"},"a2a":{"transport":"http","port_strategy":"ephemeral"},'
        '"capabilities_hint":["tool"],"model_override":null}',
        encoding="utf-8",
    )
    cards = load_cards(tmp_path / ".agent" / "agents")
    assert len(cards) == 1
    assert cards[0].id == "tool-agent"
    assert cards[0].entrypoint["module"] == "agents.tool_agent.main"


def test_load_cards_skips_invalid_json(tmp_path):
    cards_dir = tmp_path / ".agent" / "agents"
    cards_dir.mkdir(parents=True)
    (cards_dir / "broken.card.json").write_text("not json", encoding="utf-8")
    cards = load_cards(cards_dir)
    assert cards == []
