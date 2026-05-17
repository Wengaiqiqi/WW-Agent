from __future__ import annotations
from typing import Any, Callable, TypedDict
from langgraph.graph import StateGraph, END
from orchestrator.permission_gate import PermissionGate
from orchestrator.router import CapabilityRouter, RoutingError


class OrchestratorState(TypedDict, total=False):
    user_input: str
    trace_id: str
    capability: str
    arguments: dict
    response: str
    result: Any
    error: str


Planner = Callable[[OrchestratorState], dict]


def build_graph(*, router: CapabilityRouter, host, planner: Planner, hmac_key: str, mode: str):
    """Build the LangGraph orchestrator graph.

    `planner` is a callable that, given current state, decides which capability
    to invoke and with what args. In production this wraps an LLM call; in
    tests it's a hardcoded function.
    """

    async def _plan(state: OrchestratorState) -> OrchestratorState:
        decision = planner(state)
        return {**state, **decision}

    async def _dispatch(state: OrchestratorState) -> OrchestratorState:
        try:
            agent_id = router.resolve(state["capability"])
        except RoutingError as exc:
            return {**state, "error": str(exc)}

        gate = PermissionGate(mode=mode, hmac_key=hmac_key, trace_id=state["trace_id"])
        try:
            grant = gate.sign(target_specialist=agent_id, tool=state["capability"])
        except Exception as exc:
            return {**state, "error": f"permission_denied: {exc}"}

        args = dict(state.get("arguments") or {})
        args["_meta"] = {"authz_grant": grant, "trace_id": state["trace_id"]}
        result = await host.call_tool(agent_id, state["capability"], args)
        return {**state, "result": result}

    def _route_after_plan(state: OrchestratorState) -> str:
        if state.get("capability"):
            return "dispatch"
        return END

    g = StateGraph(OrchestratorState)
    g.add_node("plan", _plan)
    g.add_node("dispatch", _dispatch)
    g.set_entry_point("plan")
    g.add_conditional_edges("plan", _route_after_plan, {"dispatch": "dispatch", END: END})
    g.add_edge("dispatch", END)
    return g.compile()
