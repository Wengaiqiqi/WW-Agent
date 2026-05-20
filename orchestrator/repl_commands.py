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
            if command == "/gateway":
                return self._cmd_gateway()
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

    # ------------------------------------------------------------------
    # /gateway -- chat-platform gateway menu (Feishu, QQ, ...)
    # ------------------------------------------------------------------

    _GATEWAY_PLATFORMS: tuple[tuple[str, str], ...] = (
        ("feishu", "Feishu / Lark"),
        ("qq", "QQ Official Bot"),
    )

    def _cmd_gateway(self) -> LoopAction:
        """Two-step menu just like /model: platform -> action -> execute, loop.

        The menu redraws after every action so the user sees fresh status
        without re-typing the command. Esc / q exits back to the REPL.
        """
        from orchestrator.picker import can_use_interactive_picker

        if not can_use_interactive_picker():
            self.ui.render_command_error(
                "/gateway requires a TTY",
                "Run the agent in an interactive terminal, or use the env-var "
                "fallback `python -m gateway feishu` / `python -m gateway qq`.",
            )
            return LoopAction.CONTINUE

        default_platform_idx = 0
        while True:
            platform, default_platform_idx = self._gw_pick_platform(default_platform_idx)
            if platform is None:
                return LoopAction.CONTINUE
            keep_open = self._gw_platform_menu(platform)
            if not keep_open:
                return LoopAction.CONTINUE

    # -- menus ------------------------------------------------------------

    def _gw_pick_platform(self, default_idx: int) -> tuple[str | None, int]:
        from orchestrator.picker import interactive_select

        rows: list[tuple[str, str]] = []
        for slug, label in self._GATEWAY_PLATFORMS:
            primary, secondary = self._gw_platform_row(slug, label)
            rows.append((primary, secondary))

        self.ui.console.print()
        idx = interactive_select(
            "Chat Platform Gateways",
            rows,
            default_index=default_idx,
            instruction="up/down move - enter open - esc cancel",
        )
        if idx is None:
            return None, default_idx
        return self._GATEWAY_PLATFORMS[idx][0], idx

    def _gw_platform_menu(self, platform: str) -> bool:
        """Action menu for one platform. Returns True to redraw the platform list."""
        from orchestrator.picker import interactive_select

        while True:
            from gateway import credentials as gw_creds
            from gateway.manager import get_manager

            mgr = get_manager()
            cfg = gw_creds.load(platform)
            running = mgr.is_running(platform)
            configured = bool(cfg)

            label = dict(self._GATEWAY_PLATFORMS)[platform]
            self._gw_print_overview(platform, label, cfg, mgr)

            actions: list[tuple[str, str, str]] = []
            actions.append(("setup", "Setup credentials",
                            "Step through each field; Enter keeps the current value" if configured
                            else "Required before Start"))
            if configured and not running:
                actions.append(("start", "Start gateway",
                                "Run the adapter as a background task in this REPL"))
            if running:
                actions.append(("stop", "Stop gateway", "Cancel the running background task"))
            actions.append(("view", "View saved credentials",
                            "Show all stored fields (secrets are masked)"))
            if configured:
                actions.append(("clear", "Clear credentials",
                                "Delete the saved entry from gateways.json"))
            actions.append(("back", "Back to platform list", ""))

            rows = [(label, hint) for _, label, hint in actions]
            self.ui.console.print()
            idx = interactive_select(
                f"{label} -- choose action",
                rows,
                default_index=0,
                instruction="up/down move - enter run - esc back",
            )
            if idx is None:
                return True
            key = actions[idx][0]

            if key == "back":
                return True
            if key == "setup":
                self._gw_setup(platform)
            elif key == "start":
                self._gw_start(platform)
            elif key == "stop":
                self._gw_stop(platform)
            elif key == "view":
                self._gw_view(platform)
            elif key == "clear":
                self._gw_clear(platform)

    # -- rendering --------------------------------------------------------

    def _gw_platform_row(self, platform: str, label: str) -> tuple[str, str]:
        from gateway import credentials as gw_creds
        from gateway.manager import get_manager

        mgr = get_manager()
        cfg = gw_creds.load(platform)
        running = mgr.is_running(platform)
        configured = bool(cfg)
        task_status = mgr.status(platform)

        if running:
            mark = "*"  # bullet equivalent without unicode quirks
            status_word = "running"
        elif task_status.startswith("crashed"):
            mark = "!"
            status_word = task_status  # surface the crash reason inline
        elif configured:
            mark = "o"
            status_word = "configured"
        else:
            mark = "-"
            status_word = "not configured"

        primary = f"{mark} {label:<22s} {status_word}"
        secondary_bits: list[str] = []
        if platform == "feishu":
            secondary_bits.append(f"app_id={cfg.get('app_id') or '?'}")
            mode = cfg.get("mode") or "ws"
            secondary_bits.append(f"mode={mode}")
            if mode == "webhook":
                if running:
                    meta = mgr.meta("feishu")
                    secondary_bits.append(f"url={meta.get('url', '?')}")
                elif configured:
                    host = cfg.get("host") or "0.0.0.0"
                    port = cfg.get("port") or 8765
                    secondary_bits.append(f"default http://{host}:{port}/feishu/webhook")
            elif running:
                secondary_bits.append("ws connected")
        elif platform == "qq":
            secondary_bits.append(f"app_id={cfg.get('app_id') or '?'}")
            if cfg.get("sandbox"):
                secondary_bits.append("sandbox")
            if running:
                secondary_bits.append("ws connected")
        return primary, "  ".join(secondary_bits)

    def _gw_print_overview(self, platform: str, label: str, cfg: dict, mgr) -> None:
        rows: list[list[str]] = [["status", mgr.status(platform)]]
        for k, v in mgr.meta(platform).items():
            rows.append([k, str(v)])
        if not cfg:
            rows.append(["credentials", "<not configured>"])
        else:
            for key in self._gw_fields(platform):
                rows.append([key, self._gw_display(key, cfg.get(key, ""))])
        self.ui.render_table(
            title=f"{label} gateway",
            columns=["Field", "Value"],
            rows=rows,
        )

    # -- actions ----------------------------------------------------------

    def _gw_setup(self, platform: str) -> None:
        from gateway import credentials as gw_creds

        current = gw_creds.load(platform)

        # Feishu: pick mode first (ws long-connection vs webhook). The mode
        # gates which fields the rest of the wizard asks for.
        if platform == "feishu":
            mode = self._gw_pick_feishu_mode(current.get("mode") or "ws")
            if mode is None:
                self.ui.render_text(title="Cancelled", text="No changes saved.", style="yellow")
                return
            current = {**current, "mode": mode}

        self.ui.render_text(
            title=f"Configure {platform}",
            text=(
                "Press Enter on a field to keep its current value. "
                "Ctrl+C aborts without saving."
            ),
        )
        updated: dict[str, object] = {}
        try:
            for field, hint, secret, optional in self._gw_field_specs(platform, current):
                existing = current.get(field, "")
                value = self._ask_field(field, hint, existing, secret=secret)
                if not value and optional:
                    continue
                if not value:
                    self.ui.render_command_error(
                        "Setup aborted",
                        f"{field!r} is required.",
                    )
                    return
                updated[field] = self._coerce_field(platform, field, value)
        except (EOFError, KeyboardInterrupt):
            self.ui.render_text(title="Cancelled", text="No changes saved.", style="yellow")
            return

        merged = {**current, **updated}
        if platform == "feishu" and merged.get("mode") == "webhook":
            merged.setdefault("host", "0.0.0.0")
            merged.setdefault("port", 8765)
        path = gw_creds.save(platform, merged)
        self.ui.render_text(
            title="Saved",
            text=f"Credentials written to [bold]{path}[/bold].",
            style="green",
        )

    def _gw_pick_feishu_mode(self, default_mode: str) -> str | None:
        from orchestrator.picker import interactive_select

        options = [
            (
                "ws (long-connection, recommended)",
                "Bot opens an outbound WebSocket. No public URL needed.",
            ),
            (
                "webhook",
                "Feishu POSTs events to your /feishu/webhook URL (needs public host).",
            ),
        ]
        default_idx = 0 if default_mode != "webhook" else 1
        self.ui.console.print()
        idx = interactive_select(
            "Feishu connection mode",
            options,
            default_index=default_idx,
            instruction="up/down move - enter select - esc cancel",
        )
        if idx is None:
            return None
        return "ws" if idx == 0 else "webhook"

    def _gw_start(self, platform: str) -> None:
        from gateway import credentials as gw_creds
        from gateway.manager import get_manager

        cfg = gw_creds.load(platform)
        if not cfg:
            self.ui.render_command_error(
                f"{platform} not configured",
                "Pick [bold]Setup credentials[/bold] first.",
            )
            return

        mgr = get_manager()
        try:
            if platform == "feishu":
                host = str(cfg.get("host") or "0.0.0.0")
                port = int(cfg.get("port") or 8765)
                msg = mgr.start_feishu(cfg, host=host, port=port)
            elif platform == "qq":
                msg = mgr.start_qq(cfg)
            else:
                msg = "unknown platform"
        except Exception as exc:  # noqa: BLE001
            self.ui.render_command_error(f"{platform} start failed", str(exc))
            return
        self.ui.render_text(title=f"{platform} started", text=msg, style="green")

    def _gw_stop(self, platform: str) -> None:
        from gateway.manager import get_manager

        msg = get_manager().stop(platform)
        self.ui.render_text(
            title=f"{platform} stop",
            text=msg,
            style="yellow" if "not" in msg else "cyan",
        )

    def _gw_view(self, platform: str) -> None:
        from gateway import credentials as gw_creds
        from gateway.manager import get_manager

        cfg = gw_creds.load(platform)
        self._gw_print_overview(
            platform,
            dict(self._GATEWAY_PLATFORMS)[platform],
            cfg,
            get_manager(),
        )

    def _gw_clear(self, platform: str) -> None:
        from gateway import credentials as gw_creds

        gw_creds.clear(platform)
        self.ui.render_text(
            title=f"{platform} cleared",
            text="Stored credentials removed.",
            style="yellow",
        )

    # -- field metadata + IO ---------------------------------------------

    @staticmethod
    def _gw_fields(platform: str) -> list[str]:
        if platform == "feishu":
            return [
                "mode", "app_id", "app_secret", "domain",
                "verify_token", "encrypt_key", "reply_in_thread", "host", "port",
            ]
        if platform == "qq":
            return ["app_id", "client_secret", "intents", "sandbox"]
        return []

    @staticmethod
    def _gw_field_specs(
        platform: str, current: dict | None = None
    ) -> list[tuple[str, str, bool, bool]]:
        """(field_name, hint, is_secret, is_optional) for the setup wizard.

        For Feishu, branches on ``current['mode']`` so ws mode skips the
        webhook-only fields (verify_token, encrypt_key, host, port).
        """
        current = current or {}
        if platform == "feishu":
            mode = current.get("mode") or "ws"
            specs: list[tuple[str, str, bool, bool]] = [
                ("app_id", "App ID from Feishu developer console", False, False),
                ("app_secret", "App Secret", True, False),
                ("domain", "open.feishu.cn or open.larksuite.com", False, True),
            ]
            if mode == "webhook":
                specs += [
                    ("verify_token", "Event Subscription verification token", True, False),
                    ("encrypt_key", "Encrypt key (blank = Encrypt Mode is off)", True, True),
                    ("reply_in_thread", "Reply in thread? y/n", False, True),
                    ("host", "Bind host for webhook server", False, True),
                    ("port", "Bind port for webhook server", False, True),
                ]
            return specs
        if platform == "qq":
            return [
                ("app_id", "QQ Bot AppID", False, False),
                ("client_secret", "QQ Bot Client Secret", True, False),
                ("intents", "Intents bitmask (blank = C2C+Group@+Channel@)", False, True),
                ("sandbox", "Use sandbox host? y/n", False, True),
            ]
        return []

    @staticmethod
    def _coerce_field(platform: str, field: str, value: str):
        if field in {"reply_in_thread", "sandbox"}:
            return value.strip().lower() in {"1", "y", "yes", "true", "on"}
        if field in {"intents", "port"} and value.strip():
            return int(value.strip())
        return value.strip()

    def _gw_display(self, key: str, value) -> str:
        if value is True:
            return "true"
        if value is False:
            return "false"
        if value in ("", None):
            return "<unset>"
        if key in {"app_secret", "client_secret", "verify_token", "encrypt_key"}:
            from gateway.credentials import mask
            return mask(str(value))
        return str(value)

    def _ask_field(
        self, field: str, hint: str, existing, *, secret: bool
    ) -> str:
        from rich.prompt import Prompt

        if existing in ("", None):
            default_display = ""
        elif secret:
            from gateway.credentials import mask
            default_display = mask(str(existing))
        else:
            default_display = str(existing)

        prompt_text = f"  {field}  [dim]{hint}[/dim]"
        if default_display:
            prompt_text += f" [dim](current: {default_display})[/dim]"
        raw = Prompt.ask(
            prompt_text, console=self.ui.console, default="", show_default=False
        )
        raw = raw.strip()
        if not raw and existing not in ("", None):
            return str(existing)
        return raw

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
