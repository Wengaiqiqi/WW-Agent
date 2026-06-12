"""REPL slash-command router.

Thin dispatcher: assembles the per-domain command handlers and routes each
``/command`` to the right one via a name→callable table. The actual command
logic lives in the sibling handlers:

  - CoreCommands     — /help /agents /tools /permissions /config /status
                       /skills /instructions /clear /compact
  - ModelWizard      — /model
  - GatewayCommands  — /gateway
  - RemoteCommands   — /comm /task /chat (owns current-peer + chat contexts)
"""
from __future__ import annotations

from typing import Any, Callable

from orchestrator.repl_core_commands import CoreCommands
from orchestrator.repl_gateway_commands import GatewayCommands
from orchestrator.repl_model_wizard import ModelWizard
from orchestrator.repl_remote_commands import RemoteCommands
from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI


class ReplCommandHandler:
    def __init__(self, *, ui: ReplUI, state, host, router):
        self.ui = ui
        self.state = state
        self.host = host
        self.router = router
        self._core = CoreCommands(ui=ui, state=state, host=host, router=router)
        self._model = ModelWizard(ui=ui, state=state)
        self._gateway = GatewayCommands(ui=ui)
        self._remote = RemoteCommands(ui=ui, host=host)
        # command name -> handler returning LoopAction (sync) or Awaitable[LoopAction]
        self._routes: dict[str, Callable[[str], Any]] = {
            "/help": self._core.help,
            "/status": self._core.status,
            "/agents": self._core.agents,
            "/tools": self._core.tools,
            "/permissions": self._core.permissions,
            "/config": self._core.config,
            "/skills": self._core.skills,
            "/instructions": self._core.instructions,
            "/clear": self._core.clear,
            "/compact": self._core.compact,
            "/model": self._model.run,
            "/gateway": self._gateway.run,
            "/comm": self._remote.comm,
            "/task": self._remote.task,
            "/chat": self._remote.chat,
        }

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
        """Returns LoopAction for recognized slash commands, None for non-commands."""
        command = line.split(maxsplit=1)[0].lower()
        if not command.startswith("/"):
            return None
        if command == "/exit":
            return LoopAction.EXIT
        fn = self._routes.get(command)
        if fn is None:
            self.ui.render_command_error(
                "Unknown command",
                f"{command} — type /help for available commands.",
            )
            return LoopAction.CONTINUE
        try:
            result = fn(line)
            # Some handlers are async (/model, /gateway, /comm, /task, /chat);
            # the rest return a LoopAction directly. Await only the coroutines.
            if hasattr(result, "__await__"):
                result = await result
            return result
        except Exception as exc:  # noqa: BLE001 - top-level command guard
            self.ui.render_command_error(f"Command error: {command}", str(exc))
            return LoopAction.CONTINUE
