import pytest
from orchestrator.router import CapabilityRouter, RoutingError


def test_router_resolves_unique_capability():
    r = CapabilityRouter()
    r.register("tool-agent", ["read_file", "write_file"])
    r.register("skill-agent", ["ppt-master"])
    assert r.resolve("read_file") == "tool-agent"
    assert r.resolve("ppt-master") == "skill-agent"


def test_router_raises_on_unknown_capability():
    r = CapabilityRouter()
    r.register("tool-agent", ["read_file"])
    with pytest.raises(RoutingError, match="unknown capability"):
        r.resolve("non_existent")


def test_router_uses_priority_on_collision():
    r = CapabilityRouter()
    r.register("skill-agent", ["echo"], priority=10)
    r.register("tool-agent", ["echo"], priority=20)
    assert r.resolve("echo") == "tool-agent"


def test_router_stores_and_returns_tool_metadata():
    r = CapabilityRouter()
    r.register("tool-agent", ["read_file"], tool_metas={
        "read_file": {
            "description": "Read contents of a file",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    })
    info = r.describe_tools()
    assert info["read_file"]["description"] == "Read contents of a file"
    assert info["read_file"]["inputSchema"]["required"] == ["path"]


def test_router_describe_tools_is_empty_by_default():
    r = CapabilityRouter()
    r.register("tool-agent", ["read_file"])
    assert r.describe_tools() == {}


def test_router_describe_tools_returns_copy():
    r = CapabilityRouter()
    r.register("tool-agent", ["read_file"], tool_metas={
        "read_file": {"description": "Desc"},
    })
    info = r.describe_tools()
    info["read_file"]["description"] = "mutated"
    assert r.describe_tools()["read_file"]["description"] == "Desc"
