from __future__ import annotations

import json
from typing import Any

from orchestrator.mcp_host import unwrap_tool_result as _unwrap
from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI

COMM_AGENT_ID = "comm-agent"



class ReplCommandHandler:
    def __init__(self, *, ui: ReplUI, state, host, router):
        self.ui = ui
        self.state = state
        self.host = host
        self.router = router
        from orchestrator.repl_model_wizard import ModelWizard
        from orchestrator.repl_gateway_commands import GatewayCommands
        from orchestrator.repl_remote_commands import RemoteCommands
        self._model = ModelWizard(ui=ui, state=state)
        self._gateway = GatewayCommands(ui=ui)
        self._remote = RemoteCommands(ui=ui, host=host)

    # Remote-peer state lives on the remote handler; exposed here for status
    # rendering and tests that read/poke the current peer.
    @property
    def _current_peer(self) -> str | None:
        return self._remote._current_peer

    @_current_peer.setter
    def _current_peer(self, value: str | None) -> None:
        self._remote._current_peer = value

    @property
    def _chat_contexts(self) -> dict[str, str]:
        return self._remote._chat_contexts

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
                return await self._remote.comm(line)
            if command == "/task":
                return await self._remote.task(line)
            if command == "/chat":
                return await self._remote.chat(line)
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

