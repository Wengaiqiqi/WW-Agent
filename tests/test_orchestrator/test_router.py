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
