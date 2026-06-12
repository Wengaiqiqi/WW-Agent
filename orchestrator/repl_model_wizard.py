"""The /model command — interactive 4-step model configuration wizard.

Extracted from ReplCommandHandler so the wizard (provider → model → key → URL)
is a self-contained unit that only depends on the UI and session state.
"""
from __future__ import annotations

from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class ModelWizard:
    def __init__(self, *, ui: ReplUI, state):
        self.ui = ui
        self.state = state

    async def run(self, line: str) -> LoopAction:
        """Interactive 4-step wizard: provider -> model -> key -> URL.

        Uses the async picker so gateway tasks keep ticking through the dialog.
        ``/model <provider>`` skips Step 1.
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
                "Run the agent in an interactive terminal to use /model.",
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
