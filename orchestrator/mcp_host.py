from __future__ import annotations
import asyncio
import os
import sys
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from orchestrator.registry import Card

log = logging.getLogger(__name__)


@dataclass
class _ClientHandle:
    card: Card
    session: ClientSession
    stack: AsyncExitStack
    a2a_url: str | None = None


class MCPHost:
    """Manages MCP client sessions to each specialist subprocess."""

    def __init__(self, *, hmac_key: str):
        self._hmac_key = hmac_key
        self._clients: dict[str, _ClientHandle] = {}

    async def spawn(self, card: Card) -> None:
        if card.id in self._clients:
            raise RuntimeError(f"specialist already spawned: {card.id}")
        if card.entrypoint["type"] != "python":
            raise NotImplementedError("only python entrypoints supported in Day-1")

        env = os.environ.copy()
        env["AUTHZ_HMAC_KEY"] = self._hmac_key
        env["AGENT_ID"] = card.id

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", card.entrypoint["module"], *card.entrypoint.get("args", [])],
            env=env,
        )

        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        init_result = await session.initialize()

        # Extract a2a_url if specialist included it in init result metadata.
        # (Specialists will set this in Phase 7. For now the field stays None.)
        a2a_url = None
        meta = getattr(init_result, "_meta", None) or {}
        if isinstance(meta, dict):
            a2a_url = meta.get("a2a_url")

        self._clients[card.id] = _ClientHandle(
            card=card, session=session, stack=stack, a2a_url=a2a_url,
        )
        log.info("spawned %s (a2a_url=%s)", card.id, a2a_url)

    async def list_tools(self, agent_id: str):
        client = self._clients[agent_id]
        result = await client.session.list_tools()
        return result.tools

    async def call_tool(self, agent_id: str, name: str, arguments: dict):
        client = self._clients[agent_id]
        return await client.session.call_tool(name, arguments=arguments)

    def a2a_urls(self) -> dict[str, str]:
        return {k: v.a2a_url for k, v in self._clients.items() if v.a2a_url}

    async def shutdown_all(self) -> None:
        # On Windows, anyio's stdio_client can raise exceptions during cleanup due to
        # cancel scope conflicts. Since we're terminating all processes anyway, we suppress
        # these errors and let OS cleanup handle the subprocesses.
        import sys
        if sys.platform == "win32":
            # On Windows, just clear clients without awaiting close
            # The subprocess will be cleaned up by the OS
            self._clients.clear()
        else:
            for cid, handle in list(self._clients.items()):
                try:
                    await asyncio.wait_for(handle.stack.aclose(), timeout=5.0)
                except (asyncio.TimeoutError, Exception) as e:
                    log.debug("error closing client %s: %s", cid, type(e).__name__)
            self._clients.clear()
