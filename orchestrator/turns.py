from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from orchestrator.graph import build_graph
from orchestrator.router import CapabilityRouter
from orchestrator.stream_mux import StreamMux

log = logging.getLogger(__name__)


@dataclass
class TurnResult:
    capability: str = ""
    owner: str = ""
    text: str = ""
    error: str | None = None


class LLMPlanner:
    _SYSTEM = (
        "You are the orchestrator's planning brain. The available capabilities are listed below.\n\n"
        "DEFAULT TO tool.task FOR ANYTHING FILE-RELATED.\n"
        "ANY task that involves the file system — reading, writing, searching, listing, "
        "creating files, generating documents — must be delegated to the tool-agent by "
        "replying with ONLY:\n"
        '{"capability": "tool.task", "arguments": {"task": "<full task description>"}}\n\n'
        "This includes multi-step jobs like 'write a 500-word essay and save it to "
        "essay.txt' — pass the WHOLE instruction in `task`. Do NOT embed long content "
        "(essays, code, stories) directly inside JSON arguments — JSON with newlines in "
        "string values is fragile, and tool-agent can generate and write the content "
        "itself with its full toolchain.\n\n"
        "Use a specific individual capability ONLY when the user explicitly names a "
        "single tool with concrete short args (e.g. 'calculate 2+2'). Reply with ONLY:\n"
        '{"capability": "<name>", "arguments": {<short args>}}\n\n'
        "For ALL other messages — greetings, questions, creative writing (essays, poems, "
        "stories without a save target), explanations, general chat — DO NOT use JSON. "
        "Reply directly in natural language; the system auto-wraps your reply as a "
        "conversational response.\n\n"
        "Always reply in the same language the user used."
    )

    def __init__(
        self,
        *,
        llm,
        available_capabilities: list[str],
        context_provider: Callable[[], str] | None = None,
        tool_schemas: dict[str, dict] | None = None,
    ):
        self._llm = llm
        self._caps = available_capabilities
        self._context_provider = context_provider or (lambda: "")
        self._tool_schemas = tool_schemas or {}

    def _build_messages(self, state) -> list[dict]:
        context = self._context_provider()
        tool_lines: list[str] = []
        for cap in self._caps:
            schema = self._tool_schemas.get(cap, {})
            desc = schema.get("description", "")
            params = schema.get("inputSchema", {})
            tool_lines.append(f"- {cap}: {desc}")
            if params:
                props = params.get("properties", {})
                required = params.get("required", [])
                for pname, pinfo in props.items():
                    req_mark = " (required)" if pname in required else ""
                    type_ = pinfo.get("type", "any")
                    pdesc = f": {pinfo.get('description', '')}" if pinfo.get("description") else ""
                    tool_lines.append(f"    {pname} ({type_}){req_mark}{pdesc}")
        prompt = (
            f"Available capabilities:\n" + "\n".join(tool_lines) + "\n\n"
            f"Session context:\n{context}\n\n"
            f"User: {state['user_input']}"
        )
        return [
            {"role": "system", "content": self._SYSTEM},
            {"role": "user", "content": prompt},
        ]

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text

    @staticmethod
    def _parse_decision(text: str) -> dict:
        """Parse a planner response into a decision dict.

        Tolerant: if the LLM emits prose instead of JSON (common for creative
        writing / long-form answers), wrap it as a conversational response
        rather than failing the turn.
        """
        cleaned = LLMPlanner._strip_code_fences(text)
        if not cleaned:
            raise ValueError(
                "LLM returned empty response. Check model configuration with /config."
            )
        if not cleaned.startswith("{"):
            return {"capability": "", "response": cleaned}
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {"capability": "", "response": cleaned}

    def __call__(self, state) -> dict:
        out = self._llm.invoke(self._build_messages(state))
        return self._parse_decision(str(out.content))

    async def astream_plan(self, state) -> AsyncIterator[dict[str, Any]]:
        """Stream the planner LLM call, yielding text chunks for prose responses.

        Yields events:
          {"type": "text", "chunk": str}        — incremental conversational text
          {"type": "decision", "decision": dict} — final structured decision

        For JSON tool-dispatch responses, no text events are emitted; only the
        final decision. For prose (conversational / creative writing), each
        token is yielded so the UI can render it live.
        """
        astream = getattr(self._llm, "astream", None)
        if astream is None:
            # Fallback for LLMs without astream: invoke once, classify, replay.
            decision = self.__call__(state)
            if not decision.get("capability"):
                response = decision.get("response", "")
                if response:
                    yield {"type": "text", "chunk": response}
            yield {"type": "decision", "decision": decision}
            return

        buffer = ""
        mode: str | None = None  # None until classified, then 'json' or 'prose'

        async for chunk in astream(self._build_messages(state)):
            token = _extract_chunk_text(chunk)
            if not token:
                continue
            buffer += token

            if mode is None:
                stripped = buffer.lstrip()
                if not stripped:
                    continue
                if stripped.startswith("```"):
                    # Wait until we see content past the fence to decide.
                    if "\n" not in stripped:
                        continue
                    after_fence = stripped.split("\n", 1)[1].lstrip()
                    if not after_fence:
                        continue
                    mode = "json" if after_fence.startswith("{") else "prose"
                else:
                    mode = "json" if stripped.startswith("{") else "prose"

                if mode == "prose":
                    # Flush everything buffered so far as the first text chunk.
                    yield {"type": "text", "chunk": buffer}
            elif mode == "prose":
                yield {"type": "text", "chunk": token}
            # mode == "json": keep accumulating silently

        if not buffer.strip():
            yield {
                "type": "decision",
                "decision": {
                    "capability": "",
                    "response": "",
                },
            }
            return

        if mode == "prose":
            yield {
                "type": "decision",
                "decision": {"capability": "", "response": buffer.strip()},
            }
            return

        # JSON path — parse the accumulated buffer.
        cleaned = LLMPlanner._strip_code_fences(buffer)
        try:
            decision = json.loads(cleaned)
            yield {"type": "decision", "decision": decision}
            return
        except json.JSONDecodeError:
            pass

        # Malformed JSON (very common when the model tries to embed long
        # content like a 500-word essay inside an arguments string — literal
        # newlines break json.loads). Don't surface the broken JSON to the
        # user as prose. Instead, hand the whole original request to the
        # tool-agent and let its ReAct loop figure out how to fulfill it.
        log.warning(
            "Planner emitted malformed JSON (%d chars); falling back to tool.task",
            len(buffer),
        )
        yield {
            "type": "decision",
            "decision": {
                "capability": "tool.task",
                "arguments": {"task": state.get("user_input", "")},
            },
        }

    def synthesize(self, user_input: str, capability: str, tool_result: str) -> str:
        prompt = (
            f"User asked: {user_input}\n"
            f"Capability used: {capability}\n"
            f"Tool result:\n{tool_result}\n\n"
            "Synthesize a concise, natural response directly answering the user's question. "
            "Summarize key information from the tool result. "
            "Reply in the same language the user used. "
            "Do NOT return raw JSON — return plain natural language."
        )
        out = self._llm.invoke(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a helpful assistant. The user asked you to perform an action "
                        "and you have the result. Synthesize it into a natural, direct reply."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        )
        return str(out.content).strip()


def _stub_planner(state):
    scripted = os.environ.get("MOCK_ORCH_SCRIPT")
    if scripted:
        return json.loads(scripted)
    text = state["user_input"]
    if ":" in text:
        cap, _, arg = text.partition(":")
        return {"capability": cap.strip(), "arguments": {"path": arg.strip()}}
    raise ValueError("stub planner: expected 'CAPABILITY:ARG' input or MOCK_ORCH_SCRIPT env")


def _extract_chunk_text(chunk) -> str:
    """Pull the textual content out of a LangChain streaming chunk."""
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            elif item is not None:
                parts.append(str(item))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


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
        if not capability:
            return TurnResult(
                capability="", owner="orchestrator",
                text=result.get("response", ""), error=None,
            )
        owner = self.router.resolve(capability)
        raw_text = extract_text(result.get("result"))

        # Agent-task capabilities (tool.task, skill.*) already return
        # natural-language answers — skip the synthesizer.
        _AGENT_TASKS = {"tool.task"}
        if capability in _AGENT_TASKS or capability.startswith("skill."):
            return TurnResult(capability=capability, owner=owner, text=raw_text, error=None)

        if hasattr(self.planner, "synthesize"):
            try:
                synthesized = self.planner.synthesize(user_input, capability, raw_text)
                return TurnResult(capability=capability, owner="orchestrator", text=synthesized, error=None)
            except Exception:
                pass  # fall through to raw text
        return TurnResult(capability=capability, owner=owner, text=raw_text, error=None)


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
            try:
                await tail_task
            except asyncio.CancelledError:
                pass
    if result.error:
        mux.emit(agent_id="orchestrator", trace_id="t1", chunk=f"error: {result.error}\n")
        return 1
    if result.text:
        mux.emit(agent_id=result.owner, trace_id="t1", chunk=result.text + "\n")
    return 0
