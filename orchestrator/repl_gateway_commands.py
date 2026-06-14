"""The /gateway command — interactive chat-platform gateway manager.

Extracted from ReplCommandHandler. Owns the platform list, the two-level
picker menu, credential setup wizard, and start/stop/view/clear actions for
the Feishu and QQ gateways. Only depends on the UI.
"""
from __future__ import annotations

from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class GatewayCommands:
    _GATEWAY_PLATFORMS: tuple[tuple[str, str], ...] = (
        ("feishu", "Feishu / Lark"),
        ("qq", "QQ Official Bot"),
    )

    def __init__(self, *, ui: ReplUI):
        self.ui = ui

    async def run(self, line: str = "") -> LoopAction:
        """Two-step menu just like /model: platform -> action -> execute, loop.

        The menu redraws after every action so the user sees fresh status
        without re-typing the command. Esc / q exits back to the REPL.

        Async on purpose: the inner pickers use ``interactive_select_async``
        so any gateway task started here keeps ticking while the user
        navigates the menu (the synchronous picker variant would freeze
        the REPL event loop on a worker-thread join).
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
            platform, default_platform_idx = await self._gw_pick_platform(
                default_platform_idx,
            )
            if platform is None:
                return LoopAction.CONTINUE
            keep_open = await self._gw_platform_menu(platform)
            if not keep_open:
                return LoopAction.CONTINUE

    # -- menus ------------------------------------------------------------

    async def _gw_pick_platform(
        self, default_idx: int,
    ) -> tuple[str | None, int]:
        from orchestrator.picker import interactive_select_async

        rows: list[tuple[str, str]] = []
        for slug, label in self._GATEWAY_PLATFORMS:
            primary, secondary = self._gw_platform_row(slug, label)
            rows.append((primary, secondary))

        # Fresh canvas for the platform list each time the user returns to
        # it (e.g., after "Back to platform list"). Same rationale as
        # ``_gw_platform_menu``.
        self.ui.clear()
        idx = await interactive_select_async(
            "Chat Platform Gateways",
            rows,
            default_index=default_idx,
            instruction="up/down move - enter open - esc cancel",
        )
        if idx is None:
            return None, default_idx
        return self._GATEWAY_PLATFORMS[idx][0], idx

    async def _gw_platform_menu(self, platform: str) -> bool:
        """Action menu for one platform. Returns True to redraw the platform list."""
        from orchestrator.picker import interactive_select_async
        from agent_paths import config_dir
        from gateway.log_tail import read_tail

        log_path = config_dir() / "gateway.log"

        # Stat-cache so the 5 Hz refresh doesn't re-read + re-decode the
        # whole log file every tick when nothing has changed. mtime_ns +
        # size together catch both edits and truncation/rotation.
        last_sig: tuple[int, int] | None = None
        last_lines: list[str] = []

        def _footer() -> list[str]:
            nonlocal last_sig, last_lines
            try:
                st = log_path.stat()
            except OSError:
                return []
            sig = (st.st_mtime_ns, st.st_size)
            if sig == last_sig:
                return last_lines
            # Truncate at console width - 4 so the panel never wraps and
            # breaks the picker layout. Sampled only on actual log churn,
            # so a mid-session resize is picked up on the next new line.
            last_lines = read_tail(
                log_path,
                platform=platform,  # type: ignore[arg-type]
                max_lines=4,
                max_width=max(20, self.ui.console.width - 4),
            )
            last_sig = sig
            return last_lines

        while True:
            from gateway import credentials as gw_creds
            from gateway.manager import get_manager

            mgr = get_manager()
            cfg = gw_creds.load(platform)
            running = mgr.is_running(platform)
            configured = bool(cfg)

            label = dict(self._GATEWAY_PLATFORMS)[platform]
            # Wipe the previous menu / overview / picker remnants so each
            # iteration of the action loop starts on a clean screen instead
            # of stacking on top of the last one.
            self.ui.clear()
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
            idx = await interactive_select_async(
                f"{label} -- choose action",
                rows,
                default_index=0,
                instruction="up/down move - enter run - esc back",
                footer_lines=_footer,
                footer_title="Recent log (last 4 lines, filtered)",
                footer_refresh_seconds=0.2,
                footer_empty_message="(no log yet — start the gateway to see activity)",
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

    @staticmethod
    def _parse_concurrency(raw: str, current: int) -> int | None:
        """Parse the Start-time concurrency input.

        Empty/whitespace keeps ``current``. A positive integer is returned as
        the new limit. Anything else (non-integer, zero, negative) returns
        ``None`` so the caller can report an error and abort the start instead
        of silently changing the limit."""
        raw = (raw or "").strip()
        if not raw:
            return current
        try:
            n = int(raw)
        except ValueError:
            return None
        if n < 1:
            return None
        return n

    def _gw_start(self, platform: str) -> None:
        from rich.prompt import Prompt

        from gateway import credentials as gw_creds
        from gateway import runner
        from gateway.manager import get_manager

        cfg = gw_creds.load(platform)
        if not cfg:
            self.ui.render_command_error(
                f"{platform} not configured",
                "Pick [bold]Setup credentials[/bold] first.",
            )
            return

        # Ask for the process-wide concurrency limit before starting. Enter
        # keeps the current value; an invalid entry aborts without starting so
        # we never silently change the limit. 1 = serialized (one turn at a
        # time), >1 = parallel.
        current = runner.current_max_concurrency()
        # Hint uses parentheses, not square brackets: Rich treats ``[...]`` as
        # markup tags and would silently drop a bracketed phrase from the prompt.
        raw = Prompt.ask(
            f"  concurrency  [dim](max simultaneous turns, 1 = serialized)[/dim]"
            f" [dim](current: {current})[/dim]",
            console=self.ui.console,
            default="",
            show_default=False,
        )
        n = self._parse_concurrency(raw, current)
        if n is None:
            self.ui.render_command_error(
                "Invalid concurrency",
                "Enter a positive integer (1 = serialized), or press Enter to "
                "keep the current value. Gateway not started.",
            )
            return
        runner.set_max_concurrency(n)

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
        mode_word = "serialized" if n == 1 else "parallel"
        msg = f"{msg}\nconcurrency: {n} ({mode_word})"
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
                "allowed_users",
            ]
        if platform == "qq":
            return ["app_id", "client_secret", "intents", "sandbox", "allowed_users"]
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
                ("allowed_users", "逗号分隔的授权 open_id(可用 /chat /task;留空=无人可用)", False, True),
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
                ("allowed_users", "逗号分隔的授权 openid(可用 /chat /task;留空=无人可用)", False, True),
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
