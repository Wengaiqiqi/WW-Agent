from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from pydantic import BaseModel, Field, ValidationError

from orchestrator.delegation import delegate_via_a2a
from orchestrator.fast_route import fast_route
from orchestrator.graph import build_graph
from orchestrator.router import CapabilityRouter
from orchestrator.stream_mux import StreamMux
from prompt_rules import LANGUAGE_RULE


def _is_cancellation(exc: BaseException) -> bool:
    """True if *exc* is an ``asyncio.CancelledError`` or wraps one in its
    ``__cause__`` / ``__context__`` chain.

    A node that raises ``CancelledError`` (Ctrl-C / turn abort) must propagate
    as cancellation so the caller can run its abort path (e.g. the REPL
    controller's ``host.cancel_all()``). Older LangGraph let the bare
    CancelledError bubble through ``graph.ainvoke``; newer runtimes can surface
    it WRAPPED in a plain exception, which would otherwise be swallowed into a
    ``TurnResult(error=...)``. Walking the chain keeps cancellation working
    across langgraph versions (the bare case is still caught directly first)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, asyncio.CancelledError):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False

log = logging.getLogger(__name__)

# Fragments a trailing unclosed ``<…`` chunk must be a prefix of to count as a
# think tag still arriving in pieces (chunked streaming can split ``<think>``
# into ``<thi`` + ``nk>``). A stray ``<`` in prose ("3 < 5", "<3 you") is NOT a
# partial tag and must not stall the prose/JSON classifier forever.
_THINK_TAG_FRAGMENTS = (
    "<think>", "<thinking>", "<reasoning>",
    "</think>", "</thinking>", "</reasoning>",
)


@dataclass
class TurnResult:
    capability: str = ""
    owner: str = ""
    text: str = ""
    error: str | None = None


class _DecisionShape(BaseModel):
    """Schema the planner LLM is asked to emit when dispatching.

    ``response`` is allowed so a model that wraps its prose reply in JSON
    (some local quants do this defensively) still parses cleanly; the
    classifier treats ``capability == ""`` as a conversational reply.
    """

    capability: str = ""
    arguments: dict = Field(default_factory=dict)
    response: str = ""


class LLMPlanner:
    _SYSTEM_TEMPLATE = """\
You route a user message to the right capability.

# Identity
You are **WW Agent**, a multi-agent AI assistant. If asked who or what you
are, identify yourself as WW Agent -- a helpful agent that can read files,
search the web, run code, and call domain skills. Do NOT name a specific
underlying model, hosting company, or platform; you are WW Agent regardless
of which LLM powers you under the hood. Do NOT invent product names.

# Output protocol
- If a capability matches: reply with ONLY a JSON object; no prose, no
  markdown fences. Schema: {{"capability": "<name>", "arguments": {{<args>}}}}
- Otherwise (greetings, explanations, creative writing without a save target,
  general chat): reply directly in natural language. Do NOT wrap chat in JSON.

# Routing rules (apply in this order; STOP at the first that matches)

1. **Skills win when their description matches the request.** Look through
   the Available capabilities list for any name starting with `skill.`. If
   one of them describes the domain the user is asking about (shopping /
   e-commerce / prices / brand rankings / orders for a shopping skill; etc.),
   pick that skill:
   `{{"capability": "skill.<slug>", "arguments": {{<extracted args>}}}}`.
   Skills wrap curated domain APIs that a generic tool-agent cannot
   replicate; prefer them whenever the topic matches.
2. **Single-purpose capabilities** when the user explicitly names a tool
   with short concrete args (e.g. "calculate 17 * 23" -> calculator).
3. **Default to "{default_dispatch}"** for everything else that needs a
   tool: file reading/writing/searching/listing, generating-and-saving,
   FETCHING A URL, summarizing a web page the user pasted, scraping a
   small set of pages, running a shell or Python command, or any
   multi-step research. Put the user's full instruction verbatim in
   `arguments.task`. Do NOT embed long content (essays, code, stories)
   inside the JSON; pass the request and let the downstream agent
   generate / fetch the content itself.
4. **Prose** for greetings, explanations from your own knowledge,
   creative writing without a save target, and general chat.

Never tell the user "I can't access URLs" or "I can't browse the web";
the tool-agent has `web_extract` and `web_crawl`. Route the message to
"{default_dispatch}" and let it fetch.

If the user's permission_mode (shown in Session context) forbids the
required action, refuse in prose and explain how to raise the mode; do
NOT dispatch a capability that will be denied downstream.

# Examples

User: read README.md
Reply: {{"capability": "{default_dispatch}", "arguments": {{"task": "Read README.md and summarize it."}}}}

User: calculate 17 * 23
Reply: {{"capability": "calculator", "arguments": {{"expression": "17 * 23"}}}}

User: write a 200-word essay about cats and save it to cats.txt
Reply: {{"capability": "{default_dispatch}", "arguments": {{"task": "Write a 200-word essay about cats and save it to cats.txt"}}}}

User: summarize this web page https://example.com/article
Reply: {{"capability": "{default_dispatch}", "arguments": {{"task": "summarize this web page https://example.com/article"}}}}

User: what's a transformer in ML?
Reply: A transformer is a neural-network architecture that uses self-attention.

# Style
{language_rule}
"""

    def __init__(
        self,
        *,
        llm,
        available_capabilities: list[str],
        context_provider: Callable[[], str] | None = None,
        tool_schemas: dict[str, dict] | None = None,
        default_dispatch_capability: str = "tool.task",
    ):
        self._llm = llm
        self._caps = available_capabilities
        self._context_provider = context_provider or (lambda: "")
        self._tool_schemas = tool_schemas or {}
        self._default_dispatch = default_dispatch_capability
        self._tool_context = self._build_tool_context()
        # Resolve the system prompt once; it depends only on constants and
        # the default-dispatch capability name, both fixed for a session.
        self._system_prompt = self._SYSTEM_TEMPLATE.format(
            default_dispatch=default_dispatch_capability,
            language_rule=LANGUAGE_RULE,
        )

    def _build_tool_context(self) -> str:
        tool_lines: list[str] = []
        for cap in self._caps:
            schema = self._tool_schemas.get(cap, {})
            desc = schema.get("description", "")
            params = schema.get("inputSchema", {})
            tool_lines.append(f"- {cap}: {desc}")
            if params:
                props = params.get("properties", {})
                required = set(params.get("required", []))
                if props:
                    names = []
                    for pname, pinfo in props.items():
                        mark = "*" if pname in required else ""
                        type_ = pinfo.get("type", "any")
                        names.append(f"{pname}{mark}:{type_}")
                    tool_lines.append("    args: " + ", ".join(names))
        return "\n".join(tool_lines)

    def _build_messages(self, state) -> list[dict]:
        context = self._context_provider()
        prompt = (
            f"Available capabilities:\n{self._tool_context}\n\n"
            f"Session context:\n{context}\n\n"
            f"User: {state['user_input']}"
        )
        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": prompt},
        ]

    # ``<think>`` / ``<thinking>`` blocks emitted by reasoning models
    # (DeepSeek R1, Qwen-thinking, MiMo) precede the actual response payload.
    # If we hand them to the prose/JSON classifier verbatim, a leading ``<``
    # makes the classifier treat the whole stream as prose — the eventual
    # ``{"capability": ...}`` then gets rendered as plain text to the user
    # instead of dispatched. Strip non-greedily so multiple think blocks (or
    # a model that re-opens one) all get removed.
    _THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.IGNORECASE | re.DOTALL)

    @staticmethod
    def _strip_think_blocks(text: str) -> str:
        return LLMPlanner._THINK_BLOCK.sub("", text)

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        text = LLMPlanner._strip_think_blocks(text).strip()
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
        rather than failing the turn. JSON that doesn't match the schema is
        also treated as prose so the user never sees a parser error.
        """
        cleaned = LLMPlanner._strip_code_fences(text)
        if not cleaned:
            raise ValueError(
                "LLM returned empty response. Check model configuration with /config."
            )
        if not cleaned.startswith("{"):
            return {"capability": "", "response": cleaned}
        try:
            raw = json.loads(cleaned)
        except json.JSONDecodeError:
            return {"capability": "", "response": cleaned}
        if not isinstance(raw, dict):
            return {"capability": "", "response": cleaned}
        try:
            decision = _DecisionShape.model_validate(raw)
        except ValidationError:
            return {"capability": "", "response": cleaned}
        return decision.model_dump()

    def __call__(self, state) -> dict:
        out = self._llm.invoke(self._build_messages(state))
        return self._parse_decision(str(out.content))

    async def astream_plan(self, state) -> AsyncIterator[dict[str, Any]]:
        """Stream the planner LLM call, yielding text chunks for prose responses.

        Yields events:
          {"type": "text", "chunk": str}         incremental conversational text
          {"type": "decision", "decision": dict} final structured decision

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
        # Tracks whether we're currently inside a ``<think>`` block so we
        # neither classify on nor yield those tokens. Reasoning models emit
        # entire thinking traces before the actual response — letting that
        # leak to the UI is both a UX bug (user sees model's scratch work)
        # and a classifier bug (leading ``<`` ⇒ prose ⇒ JSON path missed).
        in_think = False

        async for chunk in astream(self._build_messages(state)):
            token = _extract_chunk_text(chunk)
            if not token:
                continue
            buffer += token

            # Drop completed think blocks from the live buffer used for
            # classification. We can't drop the *streamed* tokens once
            # emitted, but we haven't emitted any yet (mode is None) — so
            # this is purely about what the classifier sees.
            visible = LLMPlanner._strip_think_blocks(buffer)

            # ``in_think`` is True when we're mid-stream inside an unclosed
            # think block AND must NOT yet classify or yield the buffer.
            # Three cases trigger it:
            #
            # 1. Open count > close count: a complete ``<think>`` opened
            #    without its matching ``</think>``. Counting is exact even
            #    for two consecutive blocks (substring containment failed
            #    here in an earlier revision).
            # 2. A partial opening tag still arriving in chunks: chunked
            #    streaming can split ``<think>`` into ``<thi`` and ``nk>``.
            #    The first chunk has no full tag, but classifying on the
            #    leading ``<`` would route as prose and leak the literal
            #    ``<thi`` to the user. Defer if ``visible`` has any ``<``
            #    without a closing ``>`` after it.
            # 3. A partial closing tag while we believe we're inside a
            #    block — handled by case 1 because counting waits for the
            #    close to complete.
            lowered = buffer.lower()
            open_count = (
                lowered.count("<think>")
                + lowered.count("<thinking>")
                + lowered.count("<reasoning>")
            )
            close_count = (
                lowered.count("</think>")
                + lowered.count("</thinking>")
                + lowered.count("</reasoning>")
            )
            last_open = visible.rfind("<")
            partial_tag = False
            if last_open >= 0:
                tail = visible[last_open:].lower()
                # A complete-looking tag (already has its '>') isn't "partial";
                # and a '<' that can't grow into a think tag is just prose
                # punctuation, so it must not defer classification.
                if ">" not in tail:
                    partial_tag = any(t.startswith(tail) for t in _THINK_TAG_FRAGMENTS)
            in_think = open_count > close_count or partial_tag

            if mode is None:
                if in_think:
                    continue  # wait for the think block to close
                stripped = visible.lstrip()
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
                    # Flush the think-stripped buffer as the first text chunk.
                    yield {"type": "text", "chunk": visible}
            elif mode == "prose":
                # Don't echo tokens that fall inside an in-flight think block.
                if not in_think:
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

        # JSON path: strict parse + Pydantic validate. If anything fails we
        # fall back to default_dispatch instead of surfacing the broken JSON
        # buffer to the UI (which the prose path would do).
        cleaned = LLMPlanner._strip_code_fences(buffer)
        try:
            raw = json.loads(cleaned)
            if isinstance(raw, dict):
                decision = _DecisionShape.model_validate(raw).model_dump()
                if decision.get("capability") or decision.get("response"):
                    yield {"type": "decision", "decision": decision}
                    return
        except (json.JSONDecodeError, ValidationError):
            pass

        # Malformed JSON (very common when the model tries to embed long
        # content like a 500-word essay inside an arguments string; literal
        # newlines break json.loads). Hand the whole original request to the
        # default dispatch capability and let its loop figure it out.
        log.warning(
            "Planner emitted malformed JSON (%d chars); falling back to %s",
            len(buffer),
            self._default_dispatch,
        )
        yield {
            "type": "decision",
            "decision": {
                "capability": self._default_dispatch,
                "arguments": {"task": state.get("user_input", "")},
            },
        }

    _SYNTHESIZE_SYSTEM = (
        "You convert a tool result into a direct reply for the user.\n"
        "Rules:\n"
        "1. Lead with the answer or the single most important field. Don't "
        "recap the question.\n"
        "2. If the tool result contains an `error` field, the FIRST sentence "
        "names the error and what to do next.\n"
        "3. Never echo raw JSON. Quote specific values inline when useful "
        "(e.g. \"the file has 142 lines\").\n"
        "4. Keep it to ~3 sentences unless the user asked for detail.\n"
        f"5. {LANGUAGE_RULE}"
    )

    def synthesize(self, user_input: str, capability: str, tool_result: str) -> str:
        prompt = (
            f"User asked: {user_input}\n"
            f"Capability used: {capability}\n"
            f"Tool result:\n{tool_result}"
        )
        out = self._llm.invoke(
            [
                {"role": "system", "content": self._SYNTHESIZE_SYSTEM},
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
        delegate=None,
    ):
        self.host = host
        self.router = router
        self.hmac_key = hmac_key
        self.permission_mode_provider = permission_mode_provider
        self.planner = planner
        # Injectable A2A streaming delegate (tests pass a fake; production
        # leaves None and ``delegate_via_a2a`` uses the real client).
        self._delegate = delegate

    async def run(self, user_input: str, *, trace_id: str) -> TurnResult:
        state = {"user_input": user_input, "trace_id": trace_id}

        # Fast-route obvious tool-agent work (files / URLs / commands / repo
        # review) straight to ``tool.task``, skipping the planner LLM
        # round-trip entirely — the same heuristic the interactive REPL uses.
        # Gated on a real ``LLMPlanner``: there's no round-trip to save with
        # the mock/stub planner, and tests inject lightweight planners they
        # expect to actually run, so leave those paths alone.
        decision: dict | None = None
        if isinstance(self.planner, LLMPlanner):
            decision = fast_route(
                user_input,
                capabilities=self.router.all_capabilities(),
                mode=self.permission_mode_provider(),
            )

        # Run the planner once. ``tool.task`` / ``skill.<slug>`` are NOT MCP
        # tools — they must stream over A2A. The LangGraph/MCP path below only
        # handles single-shot MCP capabilities (calculator, read_file, …). The
        # decision is pinned into the graph so the planner isn't invoked twice
        # (a real LLM planner would otherwise double its cost/latency).
        #
        # LLMPlanner.__call__ is synchronous and internally blocks on
        # llm.invoke() for 10-30s. Calling it directly here would freeze the
        # event loop for the whole turn — Ctrl-C, cancel watchers, and the
        # SSE writer would all stall. Offload to a worker thread; if the
        # planner is a coroutine function it returns the coroutine
        # immediately (no blocking) and we await it on the loop.
        if decision is None:
            try:
                raw = await asyncio.to_thread(self.planner, state)
                if asyncio.iscoroutine(raw):
                    raw = await raw
                decision = dict(raw or {})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return TurnResult(error=str(exc))

        capability = (decision.get("capability") or "").strip()
        if capability == "tool.task" or capability.startswith("skill."):
            try:
                text = await delegate_via_a2a(
                    capability=capability,
                    arguments=decision.get("arguments") or {},
                    user_input=user_input,
                    hmac_key=self.hmac_key,
                    trace_id=trace_id,
                    permission_mode=self.permission_mode_provider(),
                    delegate=self._delegate,
                    # Discover peers from the host's own runtime dir (per-turn
                    # when set), keeping parent delegation and child sidecars on
                    # one dir; falls back to the global dir for legacy hosts.
                    runtime_dir=getattr(self.host, "runtime_dir", None),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _is_cancellation(exc):
                    raise asyncio.CancelledError from exc
                return TurnResult(error=str(exc))
            try:
                owner = self.router.resolve(capability)
            except Exception:
                owner = "tool-agent" if capability == "tool.task" else "skill-agent"
            return TurnResult(capability=capability, owner=owner, text=text, error=None)

        graph = build_graph(
            router=self.router,
            host=self.host,
            planner=lambda _state, _d=decision: _d,
            hmac_key=self.hmac_key,
            mode=self.permission_mode_provider(),
        )
        try:
            result = await graph.ainvoke(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A node's CancelledError may arrive WRAPPED on newer langgraph;
            # re-raise as cancellation so the caller's abort path runs.
            if _is_cancellation(exc):
                raise asyncio.CancelledError from exc
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
        # natural-language answers; skip the synthesizer.
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
