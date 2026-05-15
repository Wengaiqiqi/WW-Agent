from __future__ import annotations

import asyncio
import logging
import os
import secrets

from orchestrator.repl_types import LoopAction
from orchestrator.repl_ui import ReplUI
from orchestrator.turns import LLMPlanner, TurnRunner, _stub_planner

log = logging.getLogger(__name__)


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
        await self._ensure_planner()

        trace_id = secrets.token_hex(4)

        runner = TurnRunner(
            host=self.host,
            router=self.router,
            hmac_key=self.hmac_key,
            permission_mode_provider=lambda: self.state.permission_mode,
            planner=self._planner,
        )

        try:
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
            self.ui.render_text(
                title=result.owner or "orchestrator",
                text=result.text,
            )
        self.ui.render_divider()
        return LoopAction.CONTINUE

    async def _ensure_planner(self) -> None:
        if self._planner is not None:
            return
        provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "")
        if provider.startswith("mock") or not provider:
            self._planner = _stub_planner
            return
        try:
            llm = _build_planner_llm()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to build planner LLM: {exc}. "
                f"Use /config or /model to fix, or set LANGCHAIN_AGENT_MODEL=mock."
            ) from exc
        self._planner = LLMPlanner(
            llm=llm,
            available_capabilities=self.router.all_capabilities(),
            context_provider=lambda: self.state.render_planner_context(
                self.router.all_capabilities()
            ),
        )

    def _is_fatal(self, error: Exception) -> bool:
        if isinstance(error, asyncio.CancelledError):
            return True
        return False


def _build_planner_llm():
    from config import build_llm, load_active_config

    return build_llm(load_active_config())
