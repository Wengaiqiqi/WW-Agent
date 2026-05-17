from __future__ import annotations

from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class ReplCommandHandler:
    def __init__(self, *, ui: ReplUI, state, host, router):
        self.ui = ui
        self.state = state
        self.host = host
        self.router = router

    def handle(self, line: str) -> LoopAction | None:
        """Returns LoopAction for recognized slash commands, None for non-commands."""
        command = line.split(maxsplit=1)[0].lower()
        if not command.startswith("/"):
            return None
        try:
            if command in {"/exit", "/quit"}:
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
                return self._cmd_model(line)
            if command == "/skills":
                return self._cmd_skills()
            if command == "/instructions":
                return self._cmd_instructions()
            if command == "/clear":
                return self._cmd_clear()
            if command == "/compact":
                return self._cmd_compact()
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

    def _cmd_model(self, line: str) -> LoopAction:
        self.ui.render_text(
            title="Model Configuration",
            text="Use python cli.py --single /model until the multi-agent wizard is implemented.",
            style="yellow",
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
        self.ui.render_text(
            title="Compacted", text=f"New thread: {self.state.thread_id}",
            style="green",
        )
        return LoopAction.CONTINUE
