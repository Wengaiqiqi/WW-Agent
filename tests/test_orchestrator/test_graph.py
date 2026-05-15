import pytest
from orchestrator.graph import build_graph, OrchestratorState
from orchestrator.router import CapabilityRouter


class _FakeMCPHost:
    def __init__(self):
        self.calls: list = []

    async def call_tool(self, agent_id, name, arguments):
        self.calls.append((agent_id, name, arguments))
        return {"content": [{"type": "text", "text": "ok"}]}


@pytest.mark.asyncio
async def test_graph_routes_to_correct_specialist():
    router = CapabilityRouter()
    router.register("tool-agent", ["read_file"])
    host = _FakeMCPHost()

    def fake_planner(state: OrchestratorState) -> dict:
        return {"capability": "read_file", "arguments": {"path": "x"}}

    graph = build_graph(router=router, host=host, planner=fake_planner, hmac_key="k", mode="read-only")
    out = await graph.ainvoke({"user_input": "read x", "trace_id": "t1"})

    assert host.calls[0][0] == "tool-agent"
    assert host.calls[0][1] == "read_file"
    assert host.calls[0][2]["path"] == "x"
    # _meta.authz_grant was injected
    assert "_meta" in host.calls[0][2]
    assert "authz_grant" in host.calls[0][2]["_meta"]
