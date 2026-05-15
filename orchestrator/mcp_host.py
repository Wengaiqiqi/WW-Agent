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

        # Read A2A URL sidecar file written by the specialist at startup.
        from pathlib import Path
        a2a_url = None
        url_file = Path(".agent/runtime") / f"{card.id}.a2a-url"
        # Poll briefly — the specialist writes the file before stdio MCP init starts,
        # so it should already be there, but allow a small window.
        for _ in range(20):  # 1 second
            if url_file.exists():
                a2a_url = url_file.read_text(encoding="utf-8").strip()
                break
            await asyncio.sleep(0.05)

        self._clients[card.id] = _ClientHandle(
            card=card, session=session, stack=stack, a2a_url=a2a_url,
        )
        log.info("spawned %s (a2a_url=%s)", card.id, a2a_url)

    async def list_tools(self, agent_id: str):
        client = self._clients[agent_id]
        result = await client.session.list_tools()
        return result.tools

    async def call_tool(self, agent_id: str, name: str, arguments: dict):
        client = self._clients.get(agent_id)
        if client is None:
            # Return a dict-shaped error so the graph node sees the failure.
            return {
                "content": [
                    {"type": "text", "text": f"error: specialist {agent_id!r} unavailable"}
                ],
                "isError": True,
            }
        try:
            return await client.session.call_tool(name, arguments=arguments)
        except (BrokenPipeError, ConnectionError, EOFError, OSError) as exc:
            log.exception("call_tool: specialist %s appears to have crashed", agent_id)
            return {
                "content": [
                    {"type": "text", "text": f"error: specialist {agent_id!r} crashed: {exc}"}
                ],
                "isError": True,
            }
        except Exception as exc:
            # Catch-all for MCP SDK-specific errors. Don't swallow CancelledError.
            import asyncio
            if isinstance(exc, asyncio.CancelledError):
                raise
            log.exception("call_tool: %s/%s failed with unexpected error", agent_id, name)
            return {
                "content": [
                    {"type": "text", "text": f"error: specialist {agent_id!r} returned error: {exc}"}
                ],
                "isError": True,
            }

    def a2a_urls(self) -> dict[str, str]:
        return {k: v.a2a_url for k, v in self._clients.items() if v.a2a_url}

    def list_handles(self):
        """Return the list of internal client handles, for /agents display."""
        return list(self._clients.values())

    async def cancel_all(self) -> None:
        """Send MCP notifications/cancelled to every specialist.

        The MCP SDK may expose this via `session.send_notification(method=...)` or
        via a specific method; the call is best-effort. We swallow errors so a
        crashed specialist doesn't prevent cancellation of the others."""
        for cid, handle in self._clients.items():
            try:
                # Try the generic notification API first.
                if hasattr(handle.session, "send_notification"):
                    await handle.session.send_notification(
                        method="notifications/cancelled", params={}
                    )
            except Exception as exc:
                log.debug("cancel_all: error sending notification to %s: %s", cid, exc)

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
