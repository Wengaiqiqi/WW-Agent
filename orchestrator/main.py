# orchestrator/main.py
from __future__ import annotations
import asyncio
import json
import logging
import os
import secrets
import sys
from pathlib import Path

from orchestrator.registry import load_cards
from orchestrator.mcp_host import MCPHost
from orchestrator.router import CapabilityRouter
from orchestrator.stream_mux import StreamMux
from orchestrator.repl_types import LoopAction
from orchestrator.turns import LLMPlanner, _stub_planner, run_prompt_once

log = logging.getLogger(__name__)


def _agent_dir() -> Path:
    return Path(".agent") / "agents"


async def _bootstrap(host: MCPHost, router: CapabilityRouter) -> None:
    cards = load_cards(_agent_dir())

    async def _spawn(card) -> str | None:
        try:
            await host.spawn(card)
        except Exception:
            if card.optional:
                log.warning(
                    "optional specialist %r failed to spawn, skipping",
                    card.id, exc_info=True,
                )
                return None
            raise
        return card.id

    # Spawn every specialist concurrently. They are independent subprocesses
    # and each pays a multi-second cold-start (langchain/langgraph import plus
    # the a2a-url handshake poll). Serial spawning stacked those latencies;
    # gather overlaps them, roughly halving startup with the current 3 cards —
    # a win shared by the one-shot CLI, the chat gateway, and the web bridge
    # (which re-bootstraps on every message).
    #
    # return_exceptions=True so a non-optional failure does not abandon the
    # other in-flight spawns half-initialized: they finish and land in
    # host._clients, so the caller's ``shutdown_all`` can still tear them down
    # before we re-raise the first real error.
    results = await asyncio.gather(
        *(_spawn(card) for card in cards), return_exceptions=True
    )
    spawned: set[str] = set()
    for res in results:
        if isinstance(res, BaseException):
            raise res
        if res is not None:
            spawned.add(res)

    # Register each spawned specialist's MCP tools. ``list_tools`` is a cheap
    # MCP round-trip; overlap them too, then register in card order so the
    # advertised capability list stays deterministic.
    #
    # return_exceptions=True (matching the spawn gather above): a single
    # specialist whose MCP server crashes or hangs during enumeration must not
    # take down bootstrap for the others. We skip the failed one — its
    # capability just won't be advertised — and ``shutdown_all`` still reaps it.
    live_cards = [card for card in cards if card.id in spawned]
    tool_lists = await asyncio.gather(
        *(host.list_tools(card.id) for card in live_cards),
        return_exceptions=True,
    )
    for card, tools in zip(live_cards, tool_lists):
        if isinstance(tools, BaseException):
            log.warning(
                "specialist %r failed to enumerate tools, skipping: %s",
                card.id, tools,
            )
            continue
        tool_metas = {
            t.name: {
                "description": getattr(t, "description", ""),
                "inputSchema": getattr(t, "inputSchema", {}),
            }
            for t in tools
        }
        router.register(card.id, [t.name for t in tools], tool_metas=tool_metas)

    # After all specialists are up, broadcast their A2A URLs into THIS host's
    # runtime dir (per-turn when the web bridge set one via turn_env, else the
    # process-global dir). Writing via host.runtime_dir keeps the peers.json the
    # parent writes and the dir delegation later reads in lockstep with the
    # sidecars the children wrote — no cross-process clobber on a shared cwd.
    peers = host.a2a_urls()  # already returns {id: url} from Task 5.2
    rt_dir = host.runtime_dir
    rt_dir.mkdir(parents=True, exist_ok=True)
    (rt_dir / "peers.json").write_text(json.dumps(peers), encoding="utf-8")

    # Register agent-level task capabilities (A2A delegation, not MCP).
    # Gate on the SUCCESSFULLY spawned set — not the on-disk card set —
    # otherwise an optional specialist that failed to spawn would still
    # advertise tool.task to the planner, which then delegates to a peer
    # with no entry in peers.json and gets a cryptic "unknown peer" instead
    # of a clean "no such capability".
    if "tool-agent" in spawned:
        router.register("tool-agent", ["tool.task"], priority=10, tool_metas={
            "tool.task": {
                "description": (
                    "Delegate a task to the tool-agent. It autonomously reads, writes, "
                    "searches and lists files; fetches and extracts text from web pages "
                    "(including a single URL the user pasted, or a small crawl); runs "
                    "Python or shell commands; and chains tools to answer multi-step "
                    "questions. Pass the WHOLE user instruction in `task` verbatim."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["task"],
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": (
                                "The user's full instruction, verbatim. Includes any "
                                "URLs they want fetched / summarized / crawled."
                            ),
                        },
                    },
                },
            },
        })


def _build_orchestrator_llm(cfg=None):
    """Build a chat model for the orchestrator's planner (one-shot mode).

    ``cfg`` (a resolved ActiveConfig, e.g. from ``resolve_config(ctx)``) lets a
    caller avoid the process-global ``load_active_config()`` env read so two
    concurrent turns can't clobber each other's model selection. ``None`` keeps
    the legacy env-driven path for the CLI / single-user callers."""
    from config import build_llm, hydrate_env_from_credentials, load_active_config

    hydrate_env_from_credentials()
    return build_llm(cfg if cfg is not None else load_active_config())


async def run_prompt(prompt: str) -> int:
    hmac_key = secrets.token_urlsafe(32)
    host = MCPHost(hmac_key=hmac_key)
    router = CapabilityRouter()
    mux = StreamMux()
    try:
        await _bootstrap(host, router)
        provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "")
        if provider.startswith("mock") or not provider:
            planner = _stub_planner
        else:
            llm = _build_orchestrator_llm()
            planner = LLMPlanner(
                llm=llm,
                available_capabilities=router.all_capabilities(),
                tool_schemas=router.describe_tools(),
            )
        return await run_prompt_once(
            prompt=prompt,
            host=host,
            router=router,
            hmac_key=hmac_key,
            planner=planner,
            permission_mode_provider=lambda: os.environ.get(
                "LANGCHAIN_AGENT_PERMISSION_MODE", "workspace-write"
            ),
            mux=mux,
        )
    finally:
        await host.shutdown_all()


def _handle_slash_agents(host, *, out=None) -> None:
    """Render an /agents table to `out` (defaults to stdout)."""
    import sys
    out = out or sys.stdout
    rows = []
    for handle in host.list_handles():
        c = handle.card
        url = handle.a2a_url or "-"
        rows.append(f"{c.id:16s} v{c.version:6s} a2a={url}")
    out.write("\n".join(rows) + "\n")


async def run_repl() -> int:
    import config
    from orchestrator.repl_controller import REPLController
    from orchestrator.repl_commands import ReplCommandHandler
    from orchestrator.repl_state import MultiAgentSessionState
    from orchestrator.repl_ui import ReplUI
    from orchestrator import telemetry

    hmac_key = secrets.token_urlsafe(32)
    host = MCPHost(hmac_key=hmac_key)
    router = CapabilityRouter()
    mux = StreamMux()
    ui = ReplUI()

    try:
        telemetry.reset_log()
        stop_telemetry = asyncio.Event()
        tail_task = asyncio.create_task(telemetry.tail(mux, stop_telemetry))

        await _bootstrap(host, router)

        config.hydrate_env_from_credentials()
        active_cfg = config.load_active_config()

        memory_snapshot = ""
        try:
            from tool import tool_memory
            memory_snapshot = tool_memory.snapshot_for_system_prompt()
        except Exception:
            pass

        skills_list: list = []
        try:
            from skills.skill_loader import load_skills
            skills_list = load_skills()
        except Exception:
            pass

        instruction_files: list = []
        try:
            from project_context import discover_instruction_files
            instruction_files = discover_instruction_files()
        except Exception:
            pass

        state = MultiAgentSessionState.from_runtime(
            active_cfg=active_cfg,
            skills=skills_list,
            instruction_files=instruction_files,
            memory_snapshot=memory_snapshot,
            workspace=Path.cwd(),
        )

        commands = ReplCommandHandler(
            ui=ui, state=state, host=host, router=router,
        )
        controller = REPLController(
            host=host, router=router, hmac_key=hmac_key,
            state=state, commands=commands, ui=ui,
        )

        ui.render_welcome(
            provider=state.provider,
            model=state.model,
            protocol=state.protocol,
            permission_mode=state.permission_mode,
            agent_count=len(host.list_handles()),
            tool_count=len(router.all_capabilities()),
            skill_count=len(skills_list),
            instruction_count=len(instruction_files),
            workspace=str(state.workspace),
        )

        while True:
            try:
                text = await ui.read_input_async()
            except EOFError:
                ui.render_goodbye()
                break
            except KeyboardInterrupt:
                ui.render_cancelled()
                break
            if not text.strip():
                continue
            try:
                action = await controller.handle_input(text.strip())
            except KeyboardInterrupt:
                await host.cancel_all()
                ui.render_cancelled()
                action = LoopAction.CONTINUE
            if action == LoopAction.EXIT:
                break

        return 0
    finally:
        _stop_telemetry = locals().get("stop_telemetry")
        _tail_task = locals().get("tail_task")
        if _stop_telemetry is not None and _tail_task is not None:
            _stop_telemetry.set()
            try:
                await asyncio.wait_for(_tail_task, timeout=2.0)
            except asyncio.TimeoutError:
                _tail_task.cancel()
                try:
                    await _tail_task
                except asyncio.CancelledError:
                    pass
        # Tear down any /feishu or /qq gateway the user started during the
        # session so background tasks don't outlive the REPL.
        try:
            from gateway.manager import get_manager

            await get_manager().shutdown_all()
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            pass
        await host.shutdown_all()


def _silence_shutdown_noise(loop, context) -> None:
    """asyncio exception handler that hides one specific shutdown wart.

    When the user Ctrl+C's, ``asyncio.run`` cancels all running tasks. The
    MCP stdio_client we use to talk to specialists wraps its read/write
    streams in an anyio task group. anyio enforces that a task group's
    cancel scope is exited in the SAME task that entered it; the
    cancellation here is delivered from a different task, so the cleanup
    callback raises:

        RuntimeError: Attempted to exit cancel scope in a different task
        than it was entered in

    There is nothing the user (or we) can do about it — the agent
    subprocesses are about to be killed by the OS anyway. The default
    asyncio handler prints a multi-line traceback for each affected task,
    which buries the legitimate "Cancelled." message. Filter just that
    specific RuntimeError; everything else falls through to the default
    handler so real bugs still surface.
    """
    exc = context.get("exception")
    if isinstance(exc, RuntimeError) and "Attempted to exit cancel scope" in str(exc):
        return
    loop.default_exception_handler(context)


def main(*, prompt: str | None = None) -> int:
    async def _run() -> int:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(_silence_shutdown_noise)
        if prompt is not None:
            return await run_prompt(prompt)
        return await run_repl()

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        # User Ctrl+C'd. The asyncio context manager already triggered the
        # shutdown path via CancelledError; nothing more to do.
        print("\n[orchestrator] cancelled by user", file=__import__("sys").stderr)
        return 130  # conventional shell exit code for SIGINT
