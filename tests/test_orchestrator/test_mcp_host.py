import os
import pytest
from orchestrator.registry import Card
from orchestrator.mcp_host import MCPHost, _build_agent_env


def test_build_agent_env_strips_unwhitelisted_keys(monkeypatch):
    """The orchestrator must drop random user-shell env vars at the agent
    subprocess boundary — only whitelisted OS keys + ``MOCK_*`` + skills'
    declared requiresEnv should survive."""
    monkeypatch.setenv("NOTHING_TO_SEE_HERE", "leaky-secret")
    env = _build_agent_env(hmac_key="k", agent_id="x")
    assert "NOTHING_TO_SEE_HERE" not in env


def test_build_agent_env_passes_skill_declared_keys(monkeypatch, tmp_path):
    """Regression: a skill that declares ``requiresEnv: [\"BAIDU_EC_SEARCH_TOKEN\"]``
    must see that env var inside the subprocess. Previously every non-whitelist
    key was stripped, so skills that wrapped subprocess scripts (the whole
    baidu-ecommerce-search family) couldn't authenticate."""
    import json as _json
    skill_dir = tmp_path / "skill_under_test"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# x")
    (skill_dir / "_meta.json").write_text(
        _json.dumps({"requiresEnv": ["BAIDU_EC_SEARCH_TOKEN"]})
    )

    # Patch the skills dir helpers see — must redirect both the loader
    # default AND the helper that mcp_host imports.
    import skills.skill_loader as loader
    monkeypatch.setattr(loader, "SKILLS_DIR", tmp_path)

    monkeypatch.setenv("BAIDU_EC_SEARCH_TOKEN", "live-token-xyz")
    env = _build_agent_env(hmac_key="k", agent_id="tool-agent")
    assert env.get("BAIDU_EC_SEARCH_TOKEN") == "live-token-xyz"


def test_build_agent_env_forwards_custom_endpoint_vars(monkeypatch):
    """A web custom-endpoint turn sets base_url/protocol/api_key in the parent
    env; a delegated specialist must inherit them so it can build the same
    custom LLM. (These are only present when a custom endpoint is active.)"""
    monkeypatch.setenv("LANGCHAIN_AGENT_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("LANGCHAIN_AGENT_PROTOCOL", "openai")
    monkeypatch.setenv("LANGCHAIN_AGENT_API_KEY", "sk-turn-key")
    env = _build_agent_env(hmac_key="k", agent_id="tool-agent")
    assert env["LANGCHAIN_AGENT_BASE_URL"] == "https://example.test/v1"
    assert env["LANGCHAIN_AGENT_PROTOCOL"] == "openai"
    assert env["LANGCHAIN_AGENT_API_KEY"] == "sk-turn-key"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_mcp_host_spawns_tool_agent_and_calls_read_file(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hi there", encoding="utf-8")

    card = Card(
        id="tool-agent", display_name="T", version="1",
        entrypoint={"type": "python", "module": "agents.tool_agent.main", "args": []},
        mcp={"transport": "stdio"},
        a2a={"transport": "http", "port_strategy": "ephemeral"},
        capabilities_hint=["tool"], model_override=None,
    )

    host = MCPHost(hmac_key="test-key")
    await host.spawn(card)
    try:
        tools = await host.list_tools("tool-agent")
        assert "read_file" in [t.name for t in tools]
    finally:
        await host.shutdown_all()
