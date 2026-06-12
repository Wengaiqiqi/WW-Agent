from __future__ import annotations

import json
from typing import Any

from orchestrator.mcp_host import unwrap_tool_result as _unwrap
from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI

COMM_AGENT_ID = "comm-agent"


def _load_persisted_peer() -> str | None:
    """Read the persisted ``/comm use`` selection. Returns None if absent or
    unreadable — a missing/corrupt file should never block REPL startup."""
    from agent_paths import comm_session_path

    p = comm_session_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    peer = data.get("current_peer") if isinstance(data, dict) else None
    return peer if isinstance(peer, str) and peer else None


def _persist_peer(peer_id: str | None) -> None:
    """Write the current peer to disk so it survives a restart. Best-effort:
    failures are logged, not raised, so a read-only FS can't crash /comm."""
    import logging
    from agent_paths import comm_session_path

    p = comm_session_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"current_peer": peer_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - filesystem permission issue
        logging.getLogger(__name__).warning(
            "could not persist current peer to %s: %s", p, exc
        )


class ReplCommandHandler:
    def __init__(self, *, ui: ReplUI, state, host, router):
        self.ui = ui
        self.state = state
        self.host = host
        self.router = router
        # current peer persists across restarts (comm_session.json); chat
        # contexts stay memory-only.
        self._current_peer: str | None = _load_persisted_peer()
        self._chat_contexts: dict[str, str] = {}
        from orchestrator.repl_model_wizard import ModelWizard
        self._model = ModelWizard(ui=ui, state=state)
        from orchestrator.repl_gateway_commands import GatewayCommands
        self._gateway = GatewayCommands(ui=ui)

    async def handle(self, line: str) -> LoopAction | None:
        """Returns LoopAction for recognized slash commands, None for non-commands.

        Async because ``/gateway`` opens a picker that needs to share the
        REPL event loop (so a running gateway task can keep ticking while
        the user navigates the menu). Every other command stays sync and
        is just called directly.
        """
        command = line.split(maxsplit=1)[0].lower()
        if not command.startswith("/"):
            return None
        try:
            if command == "/exit":
                return LoopAction.EXIT
            if command == "/help":
                return self._cmd_help()
            if command == "/status":
                return self._cmd_status()
            if command == "/agents":
                return self._cmd_agents()
            if command == "/tools":
                return self._cmd_tools()
            if command == "/permissions":
                return self._cmd_permissions(line)
            if command == "/config":
                return self._cmd_config()
            if command == "/model":
                return await self._model.run(line)
            if command == "/skills":
                return self._cmd_skills()
            if command == "/instructions":
                return self._cmd_instructions()
            if command == "/clear":
                return self._cmd_clear()
            if command == "/compact":
                return self._cmd_compact()
            if command == "/gateway":
                return await self._gateway.run(line)
            if command == "/comm":
                return await self._cmd_comm(line)
            if command == "/task":
                return await self._cmd_task(line)
            if command == "/chat":
                return await self._cmd_chat(line)
            self.ui.render_command_error(
                "Unknown command",
                f"{command} — type /help for available commands.",
            )
            return LoopAction.CONTINUE
        except Exception as exc:
            self.ui.render_command_error(f"Command error: {command}", str(exc))
            return LoopAction.CONTINUE

    def _cmd_help(self) -> LoopAction:
        self.ui.render_help()
        return LoopAction.CONTINUE

    def _cmd_agents(self) -> LoopAction:
        rows = []
        for handle in self.host.list_handles():
            card = handle.card
            rows.append([
                card.id, str(card.version),
                str(handle.a2a_url or "-"), "healthy",
                str(len(card.capabilities_hint)),
            ])
        self.ui.render_table(
            title="Specialist Agents",
            columns=["ID", "Version", "A2A URL", "Health", "Hints"],
            rows=rows,
        )
        return LoopAction.CONTINUE

    def _cmd_tools(self) -> LoopAction:
        rows = [
            [cap, self.router.resolve(cap)]
            for cap in self.router.all_capabilities()
        ]
        self.ui.render_table(
            title="Registered Capabilities",
            columns=["Capability", "Owner"], rows=rows,
        )
        return LoopAction.CONTINUE

    def _cmd_permissions(self, line: str) -> LoopAction:
        from orchestrator.repl_state import VALID_PERMISSION_MODES

        parts = line.split(maxsplit=1)
        # Stable display order: safest → most permissive.
        ordered = ["read-only", "workspace-write", "danger-full-access"]
        modes = ordered + [m for m in VALID_PERMISSION_MODES if m not in ordered]

        if len(parts) == 1:
            current = self.state.permission_mode
            lines = [f"Current: [bold]{current}[/bold]", "", "Available modes:"]
            for mode in modes:
                marker = "→" if mode == current else " "
                lines.append(f"  {marker} {mode}")
            lines.append("")
            lines.append("Switch with: [bold]/permissions <mode>[/bold]")
            lines.append("Example: [dim]/permissions danger-full-access[/dim]")
            # Skills run under an *inner* whitelist that's more permissive
            # than the outer one: under workspace-write or above, an active
            # skill can mint a grant for any tool — including ``run_command``
            # — because it's executing curated code under skills/<slug>/.
            # Surface this so the user isn't surprised by a skill shelling
            # out under what looks like a write-only mode.
            if current != "read-only" and self.state.skills:
                lines.append("")
                lines.append(
                    "[yellow]Note:[/yellow] active skills can invoke any "
                    "tool-agent capability (including [bold]run_command[/bold] / "
                    "[bold]run_python[/bold]) under this mode. Drop to "
                    "read-only to disable skill execution entirely."
                )
            self.ui.render_text(
                title="Permission Mode",
                text="\n".join(lines),
            )
            return LoopAction.CONTINUE
        requested = parts[1].strip()
        if self.state.set_permission_mode(requested):
            self.ui.render_text(
                title="Permission Mode",
                text=f"Set to: {requested}", style="green",
            )
        else:
            self.ui.render_command_error(
                "Invalid permission mode",
                f"Got: {requested!r}\nValid modes: {', '.join(modes)}",
            )
        return LoopAction.CONTINUE

    def _cmd_config(self) -> LoopAction:
        rows = [
            ["provider", self.state.provider],
            ["model", self.state.model],
            ["protocol", self.state.protocol],
            ["base_url", self.state.base_url],
            ["api_key_env", self.state.api_key_env],
            ["permission mode", self.state.permission_mode],
            ["thread", self.state.thread_id],
            ["workspace", str(self.state.workspace)],
        ]
        self.ui.render_table(
            title="Effective Config", columns=["Key", "Value"], rows=rows,
        )
        return LoopAction.CONTINUE

    def _cmd_status(self) -> LoopAction:
        rows = [
            ["provider", self.state.provider],
            ["model", self.state.model],
            ["protocol", self.state.protocol],
            ["thread", self.state.thread_id],
            ["turns", str(self.state.turns)],
            ["tool calls", str(self.state.tool_calls)],
            ["agents", str(len(self.host.list_handles()))],
            ["capabilities", str(len(self.router.all_capabilities()))],
            ["skills", str(len(self.state.skills))],
            ["instructions", str(len(self.state.instruction_files))],
            ["permission mode", self.state.permission_mode],
            ["compacted turns", str(self.state.compacted_turns)],
            ["last error", self.state.last_error or "<none>"],
        ]
        self.ui.render_table(
            title="Session Status", columns=["Field", "Value"], rows=rows,
        )
        return LoopAction.CONTINUE

    def _cmd_skills(self) -> LoopAction:
        rows = [
            [
                getattr(s, "name", ""),
                getattr(s, "title", ""),
                str(getattr(s, "path", "")),
            ]
            for s in self.state.skills
        ]
        self.ui.render_table(
            title="Installed Skills", columns=["Name", "Title", "Path"], rows=rows,
        )
        return LoopAction.CONTINUE

    def _cmd_instructions(self) -> LoopAction:
        rows = [
            [str(getattr(f, "path", "")), str(len(getattr(f, "content", "")))]
            for f in self.state.instruction_files
        ]
        self.ui.render_table(
            title="Project Instructions", columns=["Path", "Characters"], rows=rows,
        )
        return LoopAction.CONTINUE

    def _cmd_clear(self) -> LoopAction:
        self.ui.clear()
        return LoopAction.CONTINUE

    def _cmd_compact(self) -> LoopAction:
        try:
            from tool import tool_memory
            fresh = tool_memory.snapshot_for_system_prompt()
        except Exception:
            fresh = self.state.memory_snapshot
            self.ui.render_warning("Memory refresh failed, using previous snapshot.")
        self.state.compact(memory_snapshot=fresh)
        # Surface the fact that recent_history (which feeds peer agents'
        # referring-expression context) was just cleared. Without this note
        # the next turn that says "上面的…" / "刚才那个…" silently can't
        # resolve, and the user blames the model rather than the compact.
        self.ui.render_text(
            title="Compacted",
            text=(
                f"New thread: {self.state.thread_id}\n"
                "Conversation history was cleared. References to earlier turns "
                "(\"上面的…\" / \"the previous reply\") won't resolve until new "
                "turns accumulate."
            ),
            style="green",
        )
        return LoopAction.CONTINUE

    # ------------------------------------------------------------------
    # comm infrastructure
    # ------------------------------------------------------------------

    async def _comm_call(self, tool_name: str, args: dict) -> tuple[bool, dict | None]:
        """Call a comm.* tool via the MCP host, return (ok, parsed_json | None).

        On error, renders a friendly message and returns (False, None).
        """
        import logging
        _log = logging.getLogger(__name__)
        try:
            result = await self.host.call_tool(COMM_AGENT_ID, tool_name, args)
        except Exception as exc:
            _log.exception("comm-agent call_tool raised for %s", tool_name)
            self.ui.render_command_error(
                "comm-agent error",
                f"comm-agent unreachable: {exc}",
            )
            return False, None
        is_error, text = _unwrap(result)
        if is_error:
            self.ui.render_command_error(
                "comm-agent error",
                text or f"comm-agent error on {tool_name} (no detail)",
            )
            return False, None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            self.ui.render_command_error("comm-agent error", f"invalid response: {text!r}")
            return False, None
        if not data.get("ok", True):
            self.ui.render_command_error("comm-agent error", data.get("error", str(data)))
            return False, None
        return True, data

    def _require_current_peer(self) -> str | None:
        """Return the current peer_id or render an error and return None."""
        if self._current_peer is None:
            self.ui.render_command_error(
                "No current peer",
                "Run /comm add or /comm use <name> first.",
            )
        return self._current_peer

    # ------------------------------------------------------------------
    # /comm sub-commands
    # ------------------------------------------------------------------

    async def _cmd_comm(self, line: str) -> LoopAction:
        parts = line.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub == "list":
            return await self._comm_list()
        if sub == "add":
            return await self._comm_add()
        if sub == "use":
            name = parts[2].strip() if len(parts) > 2 else ""
            return await self._comm_use(name)
        if sub == "rm":
            name = parts[2].strip() if len(parts) > 2 else ""
            return await self._comm_rm(name)
        self.ui.render_text(
            title="Usage",
            text=(
                "/comm list            — list registered peers\n"
                "/comm add             — register a new peer (interactive)\n"
                "/comm use <name>      — switch current peer\n"
                "/comm rm <name>       — remove a peer"
            ),
        )
        return LoopAction.CONTINUE

    async def _comm_list(self) -> LoopAction:
        ok, data = await self._comm_call("comm.list_peers", {})
        if not ok:
            return LoopAction.CONTINUE
        peers = data.get("peers", [])
        rows = []
        for p in peers:
            pid = p.get("peer_id", "")
            mark = "★" if pid == self._current_peer else " "
            rows.append([
                mark,
                pid,
                p.get("display_name", ""),
                p.get("url", ""),
            ])
        self.ui.render_table(
            title="Registered Peers",
            columns=["", "Peer ID", "Display Name", "URL"],
            rows=rows,
        )
        return LoopAction.CONTINUE

    async def _comm_use(self, name: str) -> LoopAction:
        if not name:
            self.ui.render_command_error("Usage", "/comm use <peer_id>")
            return LoopAction.CONTINUE
        ok, data = await self._comm_call("comm.list_peers", {})
        if not ok:
            return LoopAction.CONTINUE
        known = {p.get("peer_id") for p in data.get("peers", [])}
        if name not in known:
            self.ui.render_command_error(
                "Unknown peer",
                f"{name!r} not found. Run /comm list to see available peers.",
            )
            return LoopAction.CONTINUE
        self._current_peer = name
        _persist_peer(name)
        self.ui.render_text(
            title="Current peer",
            text=f"Switched to {name}",
            style="green",
        )
        return LoopAction.CONTINUE

    async def _comm_rm(self, name: str) -> LoopAction:
        if not name:
            self.ui.render_command_error("Usage", "/comm rm <peer_id>")
            return LoopAction.CONTINUE
        ok, data = await self._comm_call("comm.remove_peer", {"peer_id": name})
        if not ok:
            return LoopAction.CONTINUE
        if self._current_peer == name:
            self._current_peer = None
            _persist_peer(None)
            self._chat_contexts.pop(name, None)
        self.ui.render_text(
            title="Peer removed",
            text=name,
            style="yellow",
        )
        return LoopAction.CONTINUE

    async def _comm_add(self) -> LoopAction:
        from orchestrator.picker import can_use_interactive_picker
        from rich.prompt import Prompt

        if not can_use_interactive_picker():
            self.ui.render_command_error(
                "/comm add requires a TTY",
                "Run agent in an interactive terminal.",
            )
            return LoopAction.CONTINUE

        self.ui.render_text(
            title="Register remote peer",
            text="Enter peer details. Ctrl+C aborts.",
            style="cyan",
        )
        try:
            peer_id = Prompt.ask("  peer_id", console=self.ui.console).strip()
            if not peer_id:
                self.ui.render_command_error("Aborted", "peer_id is required.")
                return LoopAction.CONTINUE
            url = Prompt.ask("  url", console=self.ui.console).strip()
            if not url:
                self.ui.render_command_error("Aborted", "url is required.")
                return LoopAction.CONTINUE
            display_name = Prompt.ask(
                "  display_name [dim](blank = same as peer_id)[/dim]",
                console=self.ui.console, default="",
            ).strip() or peer_id
            self_signed = Prompt.ask(
                "  Self-signed certificate? [dim]y/N[/dim]",
                console=self.ui.console, default="n",
            ).strip().lower() in {"y", "yes"}
            tls_verify = True
            tls_pinned_sha256: str | None = None
            if self_signed:
                tls_pinned_sha256 = Prompt.ask(
                    "  SHA-256 fingerprint", console=self.ui.console,
                ).strip()
                if not tls_pinned_sha256:
                    self.ui.render_command_error("Aborted", "SHA-256 fingerprint required for self-signed.")
                    return LoopAction.CONTINUE
                tls_verify = False
            hmac_secret = Prompt.ask(
                "  HMAC secret", console=self.ui.console, password=True,
            ).strip()
            if not hmac_secret:
                self.ui.render_command_error("Aborted", "HMAC secret is required.")
                return LoopAction.CONTINUE
        except (EOFError, KeyboardInterrupt):
            self.ui.render_text(title="Cancelled", text="No changes.", style="yellow")
            return LoopAction.CONTINUE

        return await self._comm_add_execute(
            peer_id=peer_id, url=url, display_name=display_name,
            hmac_secret=hmac_secret, tls_verify=tls_verify,
            tls_pinned_sha256=tls_pinned_sha256,
        )

    async def _comm_add_execute(
        self, *, peer_id: str, url: str, display_name: str,
        hmac_secret: str, tls_verify: bool = True,
        tls_pinned_sha256: str | None = None,
    ) -> LoopAction:
        """Testable execute layer for /comm add (no TTY interaction)."""
        args: dict[str, Any] = {
            "peer_id": peer_id,
            "url": url,
            "hmac_secret_value": hmac_secret,
            "display_name": display_name,
        }
        if not tls_verify:
            args["tls_verify"] = False
        if tls_pinned_sha256:
            args["tls_pinned_sha256"] = tls_pinned_sha256
        ok, data = await self._comm_call("comm.add_peer", args)
        if not ok:
            return LoopAction.CONTINUE
        self._current_peer = peer_id
        note = data.get("note", "")
        self.ui.render_text(
            title="Peer registered",
            text=(
                f"peer_id: {peer_id}\n"
                f"url: {url}\n"
                f"Set as current peer.\n"
                f"{note}"
            ),
            style="green",
        )
        return LoopAction.CONTINUE

    # ------------------------------------------------------------------
    # /task
    # ------------------------------------------------------------------

    async def _cmd_task(self, line: str) -> LoopAction:
        parts = line.split(maxsplit=1)
        task_text = parts[1].strip() if len(parts) > 1 else ""
        if not task_text:
            self.ui.render_command_error("Usage", "/task <message to delegate>")
            return LoopAction.CONTINUE
        peer = self._require_current_peer()
        if peer is None:
            return LoopAction.CONTINUE
        ok, data = await self._comm_call("comm.list_peers", {})
        if not ok:
            return LoopAction.CONTINUE
        peer_url = ""
        for p in data.get("peers", []):
            if p.get("peer_id") == peer:
                peer_url = p.get("url", "")
                break
        self.ui.render_text(
            title=f"→ Delegating to {peer}",
            text=f"({peer_url})" if peer_url else "",
            style="cyan",
        )
        ok, result = await self._comm_call("comm.delegate", {
            "peer_id": peer, "task": task_text, "stream": False,
        })
        if not ok:
            return LoopAction.CONTINUE
        final = result.get("final_result")
        duration = result.get("duration_ms", "?")
        events_count = result.get("events_count", "?")
        reply_text = ""
        if isinstance(final, dict):
            parts_list = final.get("parts", [])
            reply_text = "\n".join(
                p.get("text", "") for p in parts_list if p.get("text")
            ) or json.dumps(final, ensure_ascii=False)
        elif final is not None:
            reply_text = str(final)
        else:
            reply_text = "(no result)"
        self.ui.render_text(
            title=f"Task result from {peer}",
            text=f"{reply_text}\n\n[dim]events={events_count}  duration={duration}ms[/dim]",
            style="green",
        )
        return LoopAction.CONTINUE

    # ------------------------------------------------------------------
    # /chat
    # ------------------------------------------------------------------

    async def _cmd_chat(self, line: str) -> LoopAction:
        parts = line.split(maxsplit=1)
        message = parts[1].strip() if len(parts) > 1 else ""
        if not message:
            self.ui.render_command_error("Usage", "/chat <message>")
            return LoopAction.CONTINUE
        peer = self._require_current_peer()
        if peer is None:
            return LoopAction.CONTINUE
        ok, data = await self._comm_call("comm.list_peers", {})
        if not ok:
            return LoopAction.CONTINUE
        peer_url = ""
        for p in data.get("peers", []):
            if p.get("peer_id") == peer:
                peer_url = p.get("url", "")
                break
        self.ui.render_text(
            title=f"→ Sending to {peer}",
            text=f"({peer_url})" if peer_url else "",
            style="cyan",
        )
        ctx = self._chat_contexts.get(peer)
        ok, result = await self._comm_call("comm.chat", {
            "peer_id": peer, "message": message, "context_id": ctx,
        })
        if not ok:
            return LoopAction.CONTINUE
        new_ctx = result.get("context_id")
        if new_ctx:
            self._chat_contexts[peer] = new_ctx
        reply = result.get("reply", "")
        self.ui.render_text(
            title=f"Reply from {peer}",
            text=reply or "(empty reply)",
            style="green",
        )
        return LoopAction.CONTINUE
