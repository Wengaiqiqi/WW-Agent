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
                return await self._cmd_model(line)
            if command == "/skills":
                return self._cmd_skills()
            if command == "/instructions":
                return self._cmd_instructions()
            if command == "/clear":
                return self._cmd_clear()
            if command == "/compact":
                return self._cmd_compact()
            if command == "/gateway":
                return await self._cmd_gateway()
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

    async def _cmd_model(self, line: str) -> LoopAction:
        """Interactive 4-step wizard: provider -> model -> key -> URL.

        Mirrors ``legacy/single_agent_loop.run_model_wizard`` but uses the
        async picker so gateway tasks keep ticking through the dialog.
        ``/model <provider>`` skips Step 1 just like the single-agent path.
        """
        import os
        from config import (
            PROVIDERS, make_config, save_active_config, save_credential,
        )
        from orchestrator.picker import can_use_interactive_picker

        parts = line.split(maxsplit=1)
        provider_hint = parts[1].strip() if len(parts) > 1 else ""
        if provider_hint and provider_hint not in PROVIDERS:
            self.ui.render_command_error(
                f"Unknown provider: {provider_hint}",
                "Run /model to choose interactively.",
            )
            return LoopAction.CONTINUE

        if not can_use_interactive_picker():
            self.ui.render_command_error(
                "/model requires a TTY",
                "Run agent in an interactive terminal, or use "
                "`python cli.py --single /model` for the legacy text fallback.",
            )
            return LoopAction.CONTINUE

        self._mw_print_intro()

        if provider_hint:
            provider_name: str = provider_hint
        else:
            picked = await self._mw_select_provider()
            if not picked:
                self.ui.render_text(
                    title="Model Wizard", text="Cancelled.", style="yellow",
                )
                return LoopAction.CONTINUE
            provider_name = picked

        provider = PROVIDERS[provider_name]
        is_custom = provider_name == "custom" or not provider.get("models")

        model = await self._mw_select_model(provider_name, provider, is_custom)
        if not model:
            self.ui.render_text(
                title="Model Wizard",
                text="Cancelled - no model selected.",
                style="yellow",
            )
            return LoopAction.CONTINUE

        api_key_env = provider.get("api_key_env") or "CUSTOM_API_KEY"
        api_key = self._mw_enter_api_key(api_key_env)
        if not api_key:
            self.ui.render_text(
                title="Model Wizard",
                text="Cancelled - API key required.",
                style="yellow",
            )
            return LoopAction.CONTINUE

        base_url = self._mw_enter_base_url(provider.get("base_url", ""), is_custom)
        if not base_url:
            self.ui.render_text(
                title="Model Wizard",
                text="Cancelled - base URL required.",
                style="yellow",
            )
            return LoopAction.CONTINUE

        new_cfg = make_config(
            provider=provider_name, model=model,
            base_url=base_url, api_key_env=api_key_env,
        )
        try:
            save_credential(api_key_env, api_key)
        except OSError as exc:
            self.ui.render_command_error("Failed to save credential", str(exc))
            return LoopAction.CONTINUE
        os.environ[api_key_env] = api_key

        try:
            save_active_config(new_cfg)
        except OSError as exc:
            self.ui.render_warning(
                f"Switched in memory only - failed to persist: {exc}"
            )

        self.state.apply_config(new_cfg)
        self.ui.render_text(
            title="Active Model",
            text=(
                f"{new_cfg.provider} / {new_cfg.model}\n"
                f"({new_cfg.protocol} @ {new_cfg.base_url})"
            ),
            style="green",
        )
        return LoopAction.CONTINUE

    # -- /model helpers ---------------------------------------------------

    def _mw_print_intro(self) -> None:
        self.ui.render_text(
            title="Model Configuration",
            text=(
                "Configure the active model in four steps:\n"
                "  1. Select provider\n"
                "  2. Select model\n"
                "  3. Enter API key\n"
                "  4. Enter base URL\n"
                "\n"
                "Picker controls: up/down move - enter confirm - esc cancel"
            ),
            style="cyan",
        )

    async def _mw_select_provider(self) -> str:
        import os
        from config import PROVIDERS, list_providers, load_credentials
        from orchestrator.picker import interactive_select_async

        provider_names = list_providers()
        creds = load_credentials()
        rows: list[tuple[str, str]] = []
        for name in provider_names:
            prov = PROVIDERS[name]
            env_name = prov.get("api_key_env", "")
            has_key = bool(
                env_name and (os.getenv(env_name) or env_name in creds)
            )
            mark = "[*]" if has_key else "[ ]"
            primary = f"{mark} {name:<22s} {prov.get('label', '')}"
            secondary = f"[{prov['protocol']:>9s}]  key={env_name}"
            rows.append((primary, secondary))

        try:
            default_idx = provider_names.index(self.state.provider)
        except ValueError:
            default_idx = 0

        idx = await interactive_select_async(
            "Step 1/4 - Select provider     [*] key set    [ ] needs key",
            rows,
            default_index=default_idx,
            instruction="up/down move - enter select - esc cancel",
        )
        if idx is None:
            return ""
        return provider_names[idx]

    async def _mw_select_model(
        self, provider_name: str, provider: dict, is_custom: bool,
    ) -> str:
        from orchestrator.picker import interactive_select_async
        from rich.prompt import Prompt

        models = list(provider.get("models") or [])

        if is_custom or not models:
            default = (
                self.state.model if self.state.provider == provider_name else ""
            )
            try:
                return Prompt.ask(
                    f"Step 2/4 - Model id (provider={provider_name})",
                    console=self.ui.console,
                    default=default or None,
                ).strip()
            except (EOFError, KeyboardInterrupt):
                return ""

        try:
            default_idx = (
                models.index(self.state.model)
                if self.state.model in models else 0
            )
        except ValueError:
            default_idx = 0

        OTHER = "+ Enter a model name not listed..."
        rows = [(m, "") for m in models] + [(OTHER, "")]
        idx = await interactive_select_async(
            f"Step 2/4 - Select model from {provider_name}",
            rows,
            default_index=default_idx,
            instruction="up/down move - enter select - esc cancel",
        )
        if idx is None:
            return ""
        if idx == len(models):
            try:
                return Prompt.ask(
                    "Model id", console=self.ui.console,
                ).strip()
            except (EOFError, KeyboardInterrupt):
                return ""
        return models[idx]

    def _mw_enter_api_key(self, env_name: str) -> str:
        """Sync prompt for the secret. Blocking is fine; the user is typing.

        First checks env + saved credentials for an existing value and
        offers to keep it -- avoids forcing the user to re-paste the same
        key when they're just switching models within the same provider.
        """
        import os
        from config import load_credentials
        from rich.prompt import Prompt

        existing = os.getenv(env_name) or load_credentials().get(env_name, "")
        if existing:
            masked = existing[:6] + "..." if len(existing) > 6 else "***"
            try:
                keep = Prompt.ask(
                    f"Step 3/4 - {env_name} already set ({masked}). Keep it?",
                    console=self.ui.console,
                    choices=["y", "n"], default="y",
                )
            except (EOFError, KeyboardInterrupt):
                return ""
            if keep == "y":
                return existing

        try:
            return Prompt.ask(
                f"Step 3/4 - {env_name}",
                console=self.ui.console, password=True,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def _mw_enter_base_url(self, default_url: str, is_custom: bool) -> str:
        from rich.prompt import Prompt

        try:
            if not is_custom and default_url:
                url = Prompt.ask(
                    "Step 4/4 - Base URL",
                    console=self.ui.console, default=default_url,
                ).strip()
            else:
                url = Prompt.ask(
                    "Step 4/4 - Base URL (e.g. https://api.example.com/v1)",
                    console=self.ui.console, default=default_url or None,
                ).strip()
        except (EOFError, KeyboardInterrupt):
            return ""

        if not url:
            return ""
        if not url.startswith(("http://", "https://")):
            self.ui.render_command_error(
                f"Invalid URL: {url}",
                "Must start with http:// or https://",
            )
            return ""
        return url

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

    async def _cmd_gateway(self) -> LoopAction:
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
                max_lines=8,
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
                footer_title="Recent log (last 8 lines, filtered)",
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
