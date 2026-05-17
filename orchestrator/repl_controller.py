from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time

from rich.markdown import Markdown
from rich.live import Live
from rich.text import Text

from agent_display import format_tool_arg_summary
from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI
from orchestrator.turns import LLMPlanner, TurnRunner, _stub_planner

log = logging.getLogger(__name__)


def _build_tool_line(name: str, args: dict, *, active: bool) -> Text:
    """Build the `⏺ tool_name  arg` line for a tool call.

    ``active=True``: the tool is still running — bullet blinks.
    ``active=False``: the tool returned — bullet stays static.

    Used by the streaming Live region in ``_delegate_to_agent``: the same
    function renders both states, so freezing a running call into its final
    form is just an ``update()`` with ``active=False`` followed by ``stop()``.
    """
    summary = format_tool_arg_summary(name, args)
    t = Text()
    # Rich's `blink` maps to ANSI SGR 5; terminals that ignore it fall back
    # to a static bold-black bullet, which is still readable.
    t.append("⏺ ", style="bold black blink" if active else "bold black")
    t.append(name, style="bold")
    if summary:
        t.append(f"  {summary}", style="dim")
    return t


class REPLController:
    def __init__(
        self,
        *,
        host,
        router,
        hmac_key: str,
        state,
        commands,
        ui: ReplUI,
    ):
        self.host = host
        self.router = router
        self.hmac_key = hmac_key
        self.state = state
        self.commands = commands
        self.ui = ui
        self._planner = None

    async def handle_input(self, text: str) -> LoopAction:
        if text.startswith("/"):
            result = self.commands.handle(text)
            if result is not None:
                return result
        return await self._execute_turn(text)

    async def _execute_turn(self, text: str) -> LoopAction:
        trace_id = secrets.token_hex(4)
        MAX_RETRIES = 3
        error_context = ""

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await self._ensure_planner()
                plan_input = {"user_input": text}
                if error_context:
                    plan_input["user_input"] = (
                        f"{text}\n\n"
                        f"[Previous attempt failed: {error_context}. "
                        "Try a different approach or use different tools.]"
                    )

                # -- conversational path streams; tool-dispatch returns silently --
                plan, streamed = await self._plan_with_streaming(plan_input)
                capability = plan.get("capability", "")

                if not capability:
                    response = plan.get("response", "")
                    self.state.record_turn(
                        user_input=text, capability="", owner="orchestrator",
                        observation=response, error=None,
                    )
                    if not streamed and response:
                        # Non-streaming planner (e.g. stub) — render once at end.
                        self.ui.render_markdown(response)
                    self.ui.render_divider()
                    return LoopAction.CONTINUE

                # -- agent delegation path (tool.task / skill.* → A2A streaming) --
                if capability == "tool.task" or capability.startswith("skill."):
                    if capability == "tool.task":
                        peer_id = "tool-agent"
                        task_text = plan.get("arguments", {}).get("task", text)
                        extra_meta: dict = {}
                    else:
                        peer_id = "skill-agent"
                        # Skills don't take a separate `task` argument — the
                        # raw user text plus structured planner arguments
                        # form the skill payload. The slug rides in meta so
                        # skill-agent's stream handler knows which SKILL.md
                        # to load.
                        slug = capability[len("skill."):]
                        arguments = plan.get("arguments", {}) or {}
                        if arguments:
                            task_text = (
                                f"{text}\n\n[Planner arguments] "
                                + json.dumps(arguments, ensure_ascii=False)
                            )
                        else:
                            task_text = text
                        # Mint an authz_grant just like the MCP graph path
                        # does in orchestrator/graph.py. Without this,
                        # skill-agent's verify_grant rejects the request with
                        # "missing authz_grant", which used to bubble up as
                        # a turn error and silently demote the next attempt
                        # to tool.task.
                        from orchestrator.permission_gate import (
                            PermissionGate,
                            PermissionDenied,
                        )
                        gate = PermissionGate(
                            mode=self.state.permission_mode,
                            hmac_key=self.hmac_key,
                            trace_id=trace_id,
                        )
                        try:
                            grant = gate.sign(
                                target_specialist="skill-agent",
                                tool=capability,
                            )
                        except PermissionDenied as exc:
                            self.ui.render_error(
                                "Permission Denied", str(exc)
                            )
                            self.state.record_turn(
                                user_input=text, capability=capability,
                                owner="skill-agent", observation="",
                                error=str(exc),
                            )
                            return LoopAction.CONTINUE
                        extra_meta = {
                            "skill_slug": slug,
                            "authz_grant": grant,
                        }
                    try:
                        result_text = await self._delegate_to_agent(
                            peer_id, task_text, trace_id, extra_meta=extra_meta,
                        )
                    except Exception as exc:
                        if attempt < MAX_RETRIES and self._planner is not _stub_planner:
                            error_context = str(exc)
                            self.ui.render_warning(
                                f"Attempt {attempt} failed — re-planning..."
                            )
                            continue
                        self.ui.render_error("Agent Error", str(exc))
                        self.state.record_turn(
                            user_input=text, capability=capability,
                            owner=peer_id, observation="", error=str(exc),
                        )
                        return LoopAction.CONTINUE

                    # Streaming already displayed the answer — don't duplicate.
                    self.state.record_turn(
                        user_input=text, capability=capability,
                        owner=peer_id, observation=result_text, error=None,
                    )
                    self.ui.render_divider()
                    return LoopAction.CONTINUE

                # -- individual tool dispatch (existing MCP path) --
                runner = TurnRunner(
                    host=self.host,
                    router=self.router,
                    hmac_key=self.hmac_key,
                    permission_mode_provider=lambda: self.state.permission_mode,
                    planner=self._planner,
                )
                result = await runner.run(text, trace_id=trace_id)

            except asyncio.CancelledError:
                await self.host.cancel_all()
                self.ui.render_cancelled()
                return LoopAction.CONTINUE
            except Exception as exc:
                if self._is_fatal(exc):
                    self.ui.render_error("Fatal Error", str(exc))
                    return LoopAction.EXIT
                self.ui.render_error("Turn Error", str(exc))
                self.state.record_turn(
                    user_input=text, capability="", owner="",
                    observation="", error=str(exc),
                )
                return LoopAction.CONTINUE

            self.state.record_turn(
                user_input=text,
                capability=result.capability,
                owner=result.owner,
                observation=result.text,
                error=result.error,
            )

            if result.error:
                self.ui.render_error("Turn Error", result.error)
            elif result.text:
                self.ui.render_markdown(result.text)
            self.ui.render_divider()
            return LoopAction.CONTINUE

        # Exhausted retries
        self.ui.render_error("Max Retries", "Tool-agent failed after all attempts.")
        self.state.record_turn(
            user_input=text, capability="tool.task", owner="tool-agent",
            observation="", error="Max retries exhausted",
        )
        return LoopAction.CONTINUE

    async def _plan_with_streaming(self, plan_input: dict) -> tuple[dict, bool]:
        """Run the planner, streaming conversational text to the TUI as it arrives.

        Returns ``(decision, streamed)`` where ``streamed`` is True iff text
        was rendered live (caller must not re-render the response).

        A ``Loading...`` spinner is shown while waiting for the planner's first
        token (LLM TTFB) and torn down the moment text or a decision arrives,
        so the same Rich-primitive invariant from ``_delegate_to_agent`` holds:
        at most one of {status, live} owns the bottom of the screen.
        """
        astream = getattr(self._planner, "astream_plan", None)
        if astream is None:
            status = self.ui.console.status("[dim]Loading...[/dim]", spinner="dots")
            status.start()
            try:
                return self._planner(plan_input), False
            finally:
                status.stop()

        # Prose streaming uses direct ``console.print`` — see the equivalent
        # block in ``_delegate_to_agent`` for the rationale (Live + overflow
        # stacks each frame instead of redrawing in place on long answers).
        text_buffer = ""
        text_active = False
        decision: dict = {}

        status = self.ui.console.status("[dim]Loading...[/dim]", spinner="dots")
        status.start()
        status_active = True

        def _stop_status() -> None:
            nonlocal status_active
            if status_active:
                status.stop()
                status_active = False

        try:
            async for event in astream(plan_input):
                etype = event.get("type", "")
                if etype == "text":
                    chunk = event.get("chunk", "")
                    if not chunk:
                        continue
                    text_buffer += chunk
                    if not text_active:
                        _stop_status()
                        self.ui.render_agent_label("multi-agent")
                        text_active = True
                    self.ui.console.print(chunk, end="", soft_wrap=True, highlight=False)
                elif etype == "decision":
                    decision = event.get("decision", {}) or {}
        finally:
            _stop_status()
            if text_active:
                self.ui.console.print()

        return decision, bool(text_buffer)

    async def _delegate_to_agent(
        self, agent_id: str, task: str, trace_id: str,
        *, extra_meta: dict | None = None,
    ) -> str:
        """Stream an A2A task to a peer agent and render progress in the TUI.

        Rendering invariants:
        - At any time, AT MOST ONE Rich primitive owns the bottom of the screen
          (a Live region OR a Status spinner — never both). This prevents the
          escape-code fight that produced the layout chaos in earlier runs.
        - `text_buffer` is local to each contiguous prose segment. Once a tool
          call or thinking break ends the segment, the buffer resets so the
          next Live region starts fresh instead of replaying old narration.
        """
        from orchestrator.a2a_client import delegate_task

        self.ui.set_agent_context(agent_id)

        final_text = ""
        # Prose streaming uses direct ``console.print(chunk, end="")`` rather
        # than a Live region. Rich's Live + vertical_overflow="visible" stops
        # redrawing in place the moment content exceeds terminal height — each
        # frame is appended instead, producing the "answer printed 10 times,
        # each block one row longer" symptom on long responses. Direct prints
        # always behave correctly regardless of length.
        text_buffer = ""        # accumulated text — recorded in turn history
        text_active = False     # True while we're in the middle of a streamed
        #                          prose block; reset on tool_call / thinking
        status = None
        status_started_at = 0.0
        status_min_show = 0.0
        label_printed = False
        # Tool-call Live region: blinks while the tool is executing, freezes
        # to a static bullet on tool_result. We keep the (name, args) of the
        # currently-running call so the freeze step can repaint the final
        # frame without re-deriving anything.
        tool_live: Live | None = None
        tool_pending: tuple[str, dict] | None = None

        def _ensure_label() -> None:
            nonlocal label_printed
            if not label_printed:
                self.ui.render_agent_label(agent_id)
                label_printed = True

        def _end_text_block() -> None:
            """Close the current prose stream cleanly.

            Called when the agent transitions out of text mode (next thinking
            tick, next tool_call, or end of turn). Prints a trailing newline
            so a following tool-call header doesn't run into the last text
            line. ``text_buffer`` is preserved for turn-history recording.
            """
            nonlocal text_active
            if text_active:
                self.ui.console.print()
                text_active = False

        # Backwards-compat alias used by the rest of this function. Older
        # call sites are still expecting `_stop_live()` semantics ("tear down
        # whatever owns the prose region"), which now just means closing the
        # text block.
        _stop_live = _end_text_block

        def _freeze_tool_live() -> None:
            """Tear down the blinking tool line, leaving a static frame on screen.

            Repaint the live region with ``active=False`` first so the final
            frame Rich leaves behind no longer animates. If nothing is running
            (no pending tool), this is a no-op.
            """
            nonlocal tool_live, tool_pending
            if tool_live is None:
                return
            if tool_pending is not None:
                name, args = tool_pending
                tool_live.update(_build_tool_line(name, args, active=False))
            tool_live.stop()
            tool_live = None
            tool_pending = None

        async def _stop_status(*, hold: bool = True) -> None:
            """Stop the current status spinner.

            ``hold=True`` enforces the per-status ``min_show`` floor: if the
            spinner has been visible for less than its requested minimum, we
            sleep just enough to reach it before stopping. ``hold=False`` is
            for the finally-cleanup path where we want to tear down promptly
            (e.g. after Ctrl+C / error) instead of blocking on a UX timer.
            """
            nonlocal status
            if status is None:
                return
            if hold and status_min_show > 0:
                elapsed = time.monotonic() - status_started_at
                remaining = status_min_show - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
            status.stop()
            status = None

        async def _show_status(label: str, *, min_show: float = 0.0) -> None:
            """Replace the current status with a new label.

            ``min_show`` is the floor in seconds for how long this status must
            stay visible. The floor is enforced lazily — the next ``_show_status``
            or ``_stop_status(hold=True)`` will await the remainder. Use it for
            states that are diagnostically important to the user even when the
            underlying transition would otherwise flash by (e.g. confirming a
            delegation actually reached the peer agent).
            """
            nonlocal status, status_started_at, status_min_show
            await _stop_status(hold=True)
            status = self.ui.console.status(f"[dim]{label}[/dim]", spinner="dots")
            status.start()
            status_started_at = time.monotonic()
            status_min_show = min_show

        try:
            # min_show=1.0: keep `Delegating to tool-agent...` visible for at
            # least one second even if the peer's first `thinking` event comes
            # back faster. The user has explicitly asked to see this state —
            # it's their proof that the orchestrator made it to the right peer.
            await _show_status(f"Delegating to {agent_id}...", min_show=1.0)
            # ``permission_mode`` is propagated so skill-agent's
            # ``_mint_tool_grant`` can sign sub-grants for the peer tool-agent
            # using the user's actual mode, not the workspace-write default.
            meta: dict = {
                "trace_id": trace_id,
                "agent_caller": "orchestrator",
                "permission_mode": self.state.permission_mode,
            }
            if extra_meta:
                meta.update(extra_meta)
            async for event in delegate_task(
                peer_id=agent_id, task=task, meta=meta,
            ):
                etype = event.get("type", "")

                if etype == "thinking":
                    _stop_live()
                    await _show_status("Thinking...")

                elif etype == "tool_call":
                    _stop_live()
                    # If the previous tool never produced a result event (rare
                    # — e.g. agent loop crashed mid-call), freeze its line so
                    # we don't leave a blinking ghost on screen.
                    _freeze_tool_live()
                    await _stop_status()
                    _ensure_label()
                    name = event.get("name", "unknown")
                    args = event.get("args", {})
                    self.ui.console.print()
                    tool_pending = (name, args)
                    tool_live = Live(
                        _build_tool_line(name, args, active=True),
                        console=self.ui.console,
                        # 4 fps keeps blink visible without flicker; Rich
                        # also relies on this to repaint the blink frames
                        # when the terminal does not honor ANSI SGR 5.
                        refresh_per_second=4,
                    )
                    tool_live.start()
                    # No "Calling X..." status spinner: the blinking bullet
                    # on the tool line already communicates "in progress",
                    # and a second spinner at the bottom would duplicate it.

                elif etype == "tool_result":
                    # Freeze the blinking bullet on the tool line; do NOT
                    # render the tool's return preview — the user asked for
                    # a compact view that only shows what was called.
                    _freeze_tool_live()
                    await _show_status("Thinking...")

                elif etype == "text":
                    chunk = event.get("chunk", "")
                    if not chunk:
                        continue
                    text_buffer += chunk
                    if not text_active:
                        await _stop_status()
                        _ensure_label()
                        text_active = True
                    # Direct, in-order print of the delta. ``end=""`` keeps the
                    # next chunk on the same line; we add a closing newline in
                    # ``_end_text_block``. ``soft_wrap=True`` lets long lines
                    # wrap at terminal width without Rich inserting hard line
                    # breaks at the buffer boundary.
                    self.ui.console.print(chunk, end="", soft_wrap=True, highlight=False)

                elif etype == "done":
                    final_text = event.get("text", "")
                    break

                elif etype == "error":
                    raise RuntimeError(event.get("message", "Unknown agent error"))

                elif etype == "warning":
                    # Non-fatal — surfaced by the SSE client when a malformed
                    # peer event was dropped. Keep streaming going; just let
                    # the user know one event was lost so they don't blame the
                    # spinner for "freezing" silently.
                    _stop_live()
                    msg = event.get("message", "unknown warning")
                    self.ui.console.print(f"[dim yellow]⚠ {msg}[/dim yellow]")

                elif etype == "clarify_request":
                    # tool-agent's ReAct loop chose the ``clarify`` tool; pause
                    # the streaming UI, render the question, collect the user's
                    # answer, and POST it back so the wrapper's await unblocks.
                    _freeze_tool_live()
                    _stop_live()
                    await _stop_status()
                    request_id = str(event.get("id") or "")
                    question = str(event.get("question") or "")
                    choices = event.get("choices") or []
                    if not request_id or not question:
                        self.ui.console.print(
                            "[dim yellow]⚠ clarify_request missing id/question — ignoring[/dim yellow]"
                        )
                        continue
                    answer = await asyncio.to_thread(
                        self._ask_user_clarify, question, list(choices),
                    )
                    try:
                        from orchestrator.a2a_client import send_clarify_response
                        await send_clarify_response(
                            peer_id=agent_id,
                            request_id=request_id,
                            answer=answer,
                        )
                    except Exception as exc:  # pragma: no cover - network rare
                        log.warning("failed to send clarify response: %s", exc)
                    await _show_status("Thinking...")

        finally:
            _stop_live()
            _freeze_tool_live()
            # hold=False so cancellation / errors don't park us on a UX timer.
            await _stop_status(hold=False)
            self.ui.set_agent_context("multi-agent")

        return final_text

    def _ask_user_clarify(self, question: str, choices: list[str]) -> str:
        """Synchronous user prompt for a tool-agent clarify_request event.

        Runs inside ``asyncio.to_thread`` from ``_delegate_to_agent``. Keeps
        the UI minimal — a titled panel for the question plus either a
        numbered choice list or a free-text prompt. Returns the empty
        string on Ctrl+C / EOF so the tool-agent wrapper sees a defined
        (if uninformative) answer rather than hanging until the 10-minute
        bridge timeout.
        """
        from rich.panel import Panel
        from rich.prompt import Prompt
        from rich import box

        self.ui.console.print()
        self.ui.console.print(Panel(
            question, title="Clarify", border_style="cyan", box=box.ROUNDED,
        ))
        if choices:
            for i, choice in enumerate(choices, 1):
                self.ui.console.print(f"  {i}. {choice}")
            self.ui.console.print(f"  {len(choices) + 1}. (type your own answer)")
            prompt_text = "Choice (number or text)"
        else:
            prompt_text = "Your answer"
        try:
            raw = Prompt.ask(prompt_text, console=self.ui.console).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        if choices and raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(choices):
                return choices[n - 1]
        return raw

    async def _ensure_planner(self) -> None:
        if self._planner is not None:
            return
        provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "")
        if provider.startswith("mock"):
            self._planner = _stub_planner
            return
        try:
            llm = _build_planner_llm()
        except Exception:
            log.warning(
                "Failed to build planner LLM, falling back to stub planner. "
                "Use /config or /model to fix, or set LANGCHAIN_AGENT_MODEL=mock.",
                exc_info=True,
            )
            self._planner = _stub_planner
            return
        self._planner = LLMPlanner(
            llm=llm,
            available_capabilities=self.router.all_capabilities(),
            context_provider=lambda: self.state.render_planner_context(
                self.router.all_capabilities()
            ),
            tool_schemas=self.router.describe_tools(),
        )

    def _is_fatal(self, error: Exception) -> bool:
        if isinstance(error, asyncio.CancelledError):
            return True
        return False


def _build_planner_llm():
    from config import build_llm, load_active_config

    return build_llm(load_active_config())
