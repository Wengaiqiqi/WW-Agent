from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Callable

from orchestrator.graph import build_graph
from orchestrator.router import CapabilityRouter
from orchestrator.stream_mux import StreamMux


@dataclass
class TurnResult:
    capability: str = ""
    owner: str = ""
    text: str = ""
    error: str | None = None


class LLMPlanner:
    _SYSTEM = (
        "You are the orchestrator's planning brain. The available capabilities are listed below. "
        "Reply with ONLY a JSON object of the form "
        '{"capability": "<name>", "arguments": {<args>}}. '
        "No prose, no markdown fence."
    )

    def __init__(
        self,
        *,
        llm,
        available_capabilities: list[str],
        context_provider: Callable[[], str] | None = None,
    ):
        self._llm = llm
        self._caps = available_capabilities
        self._context_provider = context_provider or (lambda: "")

    def __call__(self, state) -> dict:
        context = self._context_provider()
        prompt = (
            f"Available capabilities: {self._caps}\n\n"
            f"Session context:\n{context}\n\n"
            f"User: {state['user_input']}"
        )
        out = self._llm.invoke(
            [
                {"role": "system", "content": self._SYSTEM},
                {"role": "user", "content": prompt},
            ]
        )
        text = str(out.content).strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return json.loads(text)


def _stub_planner(state):
    scripted = os.environ.get("MOCK_ORCH_SCRIPT")
    if scripted:
        return json.loads(scripted)
    text = state["user_input"]
    if ":" in text:
        cap, _, arg = text.partition(":")
        return {"capability": cap.strip(), "arguments": {"path": arg.strip()}}
    raise ValueError("stub planner: expected 'CAPABILITY:ARG' input or MOCK_ORCH_SCRIPT env")


def extract_text(call_result) -> str:
    contents = getattr(call_result, "content", None)
    if contents is None and isinstance(call_result, dict):
        contents = call_result.get("content")
    parts: list[str] = []
    for piece in contents or []:
        text = getattr(piece, "text", None)
        if text is None and isinstance(piece, dict):
            text = piece.get("text", "")
        if text:
            parts.append(str(text))
    return "\n".join(parts)


class TurnRunner:
    def __init__(
        self,
        *,
        host,
        router: CapabilityRouter,
        hmac_key: str,
        permission_mode_provider: Callable[[], str],
        planner,
    ):
        self.host = host
        self.router = router
        self.hmac_key = hmac_key
        self.permission_mode_provider = permission_mode_provider
        self.planner = planner

    async def run(self, user_input: str, *, trace_id: str) -> TurnResult:
        graph = build_graph(
            router=self.router,
            host=self.host,
            planner=self.planner,
            hmac_key=self.hmac_key,
            mode=self.permission_mode_provider(),
        )
        try:
            result = await graph.ainvoke({"user_input": user_input, "trace_id": trace_id})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return TurnResult(error=str(exc))
        if result.get("error"):
            return TurnResult(error=str(result["error"]))
        capability = result.get("capability", "")
        owner = self.router.resolve(capability) if capability else ""
        text = extract_text(result.get("result"))
        return TurnResult(capability=capability, owner=owner, text=text, error=None)


async def run_prompt_once(
    *,
    prompt: str,
    host,
    router: CapabilityRouter,
    hmac_key: str,
    planner,
    permission_mode_provider: Callable[[], str],
    mux: StreamMux,
) -> int:
    runner = TurnRunner(
        host=host,
        router=router,
        hmac_key=hmac_key,
        permission_mode_provider=permission_mode_provider,
        planner=planner,
    )
    from orchestrator import telemetry

    telemetry.reset_log()
    stop = asyncio.Event()
    tail_task = asyncio.create_task(telemetry.tail(mux, stop))
    try:
        result = await runner.run(prompt, trace_id="t1")
        await asyncio.sleep(0.1)
    finally:
        stop.set()
        try:
            await asyncio.wait_for(tail_task, timeout=2.0)
        except asyncio.TimeoutError:
            tail_task.cancel()
    if result.error:
        mux.emit(agent_id="orchestrator", trace_id="t1", chunk=f"error: {result.error}\n")
        return 1
    if result.text:
        mux.emit(agent_id=result.owner, trace_id="t1", chunk=result.text + "\n")
    return 0
