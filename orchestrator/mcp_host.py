from __future__ import annotations
import asyncio
import os
import sys
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from orchestrator.registry import Card

log = logging.getLogger(__name__)


def unwrap_tool_result(result: Any) -> tuple[bool, str]:
    """Normalize a ``call_tool`` result into ``(is_error, text)``.

    ``call_tool`` may return either an MCP SDK ``CallToolResult`` object
    (attribute access) or a plain dict host-level error envelope (subscript
    access) when the specialist is unavailable/crashed. Single source of truth
    shared by the REPL command handler and the gateway slash commands."""
    try:
        # Object path (MCP SDK CallToolResult)
        is_error = bool(getattr(result, "isError", False))
        content = getattr(result, "content", None)
        if content and hasattr(content[0], "text"):
            return is_error, content[0].text
    except (IndexError, TypeError, AttributeError):
        pass
    try:
        # Dict path (host-level error envelope)
        is_error = bool(result.get("isError", False))
        content = result.get("content", [])
        if content:
            return is_error, content[0].get("text", "")
    except (AttributeError, IndexError, TypeError):
        pass
    return True, f"unexpected call_tool result: {type(result).__name__}"


@dataclass
class _ClientHandle:
    card: Card
    session: ClientSession
    stack: AsyncExitStack
    a2a_url: str | None = None


# Env vars an agent subprocess legitimately needs. Everything else from the
# orchestrator's env — particularly the user's provider API keys, GitHub
# tokens, AWS creds, etc. — is dropped at the process boundary. The agent
# loads its own provider credentials from disk via hydrate_env_from_credentials,
# so it doesn't actually need them to ride along in the spawn env.
_OS_PASSTHROUGH = {
    # POSIX
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SHELL",
    "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
    # Windows
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "USERPROFILE", "USERNAME", "USERDOMAIN", "COMPUTERNAME",
    "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMDATA", "TEMP", "TMP", "OS", "PROCESSOR_ARCHITECTURE",
    # Python runtime
    "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING", "PYTHONUTF8",
    "PYTHONUNBUFFERED",
    # App config (not secrets)
    "LANGCHAIN_AGENT_MODEL", "LANGCHAIN_AGENT_CONFIG_DIR",
    "LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS",
    # Per-turn custom-endpoint overrides (set by the web UI). BASE_URL/PROTOCOL
    # are not secrets; API_KEY is the one key the user chose for THIS turn and
    # is only present in env while a custom endpoint is active — a deliberate,
    # minimal exception to the "no secrets across the boundary" default so a
    # delegated specialist can authenticate against the same endpoint.
    "LANGCHAIN_AGENT_BASE_URL", "LANGCHAIN_AGENT_PROTOCOL",
    "LANGCHAIN_AGENT_API_KEY",
    # RUNTIME_DIR must reach the specialist: it writes its ``<id>.a2a-url``
    # sidecar there, and the orchestrator reads it back from the same dir.
    # A gateway running in-process overrides this to isolate its discovery
    # files from the REPL's; the child has to agree on the location.
    "LANGCHAIN_AGENT_RUNTIME_DIR",
    # WORKSPACE_ROOT controls the sandbox tool-agent applies to file ops.
    # Must reach the subprocess; otherwise tool-agent falls back to its
    # own ``os.getcwd()`` and the orchestrator's intended boundary is lost.
    "LANGCHAIN_AGENT_WORKSPACE_ROOT",
}


def _build_agent_env(
    *, hmac_key: str, agent_id: str, turn_env: dict[str, str] | None = None
) -> dict[str, str]:
    """Return a minimal env to hand to a freshly-spawned agent subprocess.

    Whitelisting (rather than copying os.environ and stripping secrets) is the
    safer default: a new credential env var pattern we haven't anticipated
    silently fails CLOSED instead of leaking. Tool-agent and skill-agent both
    bootstrap their own provider credentials from
    ~/.langchain-agent/credentials.json on startup, so they don't need the
    orchestrator's env to carry API keys for them.

    ``MOCK_*`` is forwarded because the e2e test harness drives subprocess
    behavior via env vars like ``MOCK_TOOL_AGENT_SCRIPT`` — those aren't
    secrets and they MUST reach the spawned agent for the tests to be
    meaningful.

    **Skill-declared env passthrough**: each skill's ``_meta.json`` can
    declare a ``requiresEnv`` list (e.g. ``["BAIDU_EC_SEARCH_TOKEN"]``).
    The union of those keys is forwarded to subprocesses so skill scripts
    (run by tool-agent's ``run_command`` on behalf of skill-agent) actually
    see their required tokens. The whitelist principle still holds — only
    explicitly-declared keys leak, never the user's whole environment.
    """
    skill_env_keys: set[str] = set()
    try:
        # Local import: orchestrator and skills/ live in the same project,
        # but mcp_host is otherwise skill-agnostic. Tolerate any failure
        # so an unrelated skill_loader bug never blocks agent spawn.
        from skills.skill_loader import collect_skill_env_keys
        skill_env_keys = collect_skill_env_keys()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not collect skill requiresEnv: %s", exc)

    env: dict[str, str] = {
        k: v for k, v in os.environ.items()
        if (
            k.upper() in _OS_PASSTHROUGH
            or k.upper().startswith("MOCK_")
            or k.upper().startswith("COMM_")
            or k in skill_env_keys
        )
    }
    env["AUTHZ_HMAC_KEY"] = hmac_key
    env["AGENT_ID"] = agent_id
    # Permission model: the orchestrator's ``PermissionGate`` is the
    # authoritative wall. It signs a short-lived JWT grant that names exactly
    # the tool a specialist may invoke; ``tool_executor.execute_tool`` rejects
    # anything not in that grant's ``allowed_tools``. The agent subprocess
    # does not run ``tool/tools.py``'s ``@tool``-decorated functions (which is
    # the only code path that reads ``LANGCHAIN_AGENT_PERMISSION_MODE``); all
    # in-process tool dispatch goes through ``_wrap_*`` in tool_executor,
    # which is JWT-gated.
    #
    # We still set the env so any *defensive* future import of ``tool/tools.py``
    # in the subprocess context would fail closed at ``workspace-write`` —
    # forcing ``run_command`` / ``run_python`` into a deny path that the
    # operator can audit, rather than silently elevating because nobody set
    # the env. This is "safe by absence", not "permissive by default".
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"
    if turn_env:
        # Per-turn config travels explicitly (TurnContext), overriding both the
        # inherited parent env and the workspace-write default above. This is
        # what lets parallel turns spawn specialists with different
        # user/workspace/model/key without mutating the shared os.environ.
        env.update(turn_env)
    return env


class MCPHost:
    """Manages MCP client sessions to each specialist subprocess."""

    def __init__(self, *, hmac_key: str, turn_env: dict[str, str] | None = None):
        self._hmac_key = hmac_key
        # Per-turn env overlay merged into every spawned specialist's env (the
        # cross-process channel for TurnContext). Empty for the legacy path,
        # which still inherits per-turn vars from os.environ.
        self._turn_env = turn_env or {}
        self._clients: dict[str, _ClientHandle] = {}

    async def spawn(self, card: Card) -> None:
        if card.id in self._clients:
            raise RuntimeError(f"specialist already spawned: {card.id}")
        if card.entrypoint["type"] != "python":
            raise NotImplementedError("only python entrypoints supported in Day-1")

        env = _build_agent_env(
            hmac_key=self._hmac_key, agent_id=card.id, turn_env=self._turn_env,
        )

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", card.entrypoint["module"], *card.entrypoint.get("args", [])],
            env=env,
        )

        stack = AsyncExitStack()
        # The handle (and therefore the stack) is only registered in
        # ``self._clients`` once spawn fully succeeds. If anything below raises
        # first — a failed ``initialize`` handshake, a cancelled spawn — the
        # half-started stdio subprocess would otherwise leak: ``shutdown_all``
        # only reaps handles in ``_clients``, which we never reached. Closing
        # the stack here drives the stdio_client teardown so the child gets EOF
        # and exits. Matters more now that bootstrap spawns concurrently: one
        # required failure aborts the gather while siblings are still starting.
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            init_result = await session.initialize()

            # Read A2A URL sidecar file written by the specialist at startup.
            from agent_paths import runtime_dir
            a2a_url = None
            url_file = runtime_dir() / f"{card.id}.a2a-url"
            # Poll for up to 5 seconds. Tool-agent imports langchain + langgraph
            # before binding its A2A port; on a cold pip cache or slow disk the
            # import chain can take 3+ seconds, well past the previous 1s budget.
            # Missing the URL silently demoted A2A streaming to "specialist
            # unreachable" on slow machines — better to wait a bit and let the
            # subprocess catch up.
            for _ in range(100):  # 5 seconds at 50ms ticks
                if url_file.exists():
                    a2a_url = url_file.read_text(encoding="utf-8").strip()
                    break
                await asyncio.sleep(0.05)
            if a2a_url is None:
                log.warning(
                    "%s did not write its A2A URL within 5s; streaming "
                    "delegation will fail until the file appears.",
                    card.id,
                )
        except BaseException:
            # Best-effort cleanup; never let a teardown error (including the
            # Windows anyio cancel-scope wart) mask the original failure.
            try:
                await stack.aclose()
            except BaseException:
                pass
            raise

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
        """Close every MCP client session.

        On POSIX, ``stack.aclose()`` drives the stdio_client teardown which
        terminates the subprocess group cleanly. On Windows, anyio's
        stdio_client can raise during cleanup because the cancel scope is
        exited from a different task than it was entered (the orchestrator's
        Ctrl+C dispatch is the typical trigger). Re-raising the cancel-scope
        error would mask the real shutdown signal, so we swallow it and
        always finish with a short ``aclose`` attempt so the subprocess
        receives EOF on its stdin and exits, instead of relying purely on
        OS cleanup (which doesn't kick in until the Python process itself
        exits — a problem if the orchestrator is embedded).
        """
        import sys
        for cid, handle in list(self._clients.items()):
            try:
                await asyncio.wait_for(handle.stack.aclose(), timeout=5.0)
            except asyncio.CancelledError:
                # Shutdown path is allowed to be cancelled; don't propagate.
                pass
            except asyncio.TimeoutError:
                log.warning("shutdown_all: client %s aclose timed out (>5s)", cid)
            except RuntimeError as exc:
                # The anyio cancel-scope wart on Windows lives here. Other
                # RuntimeErrors are real and worth surfacing in the log.
                if sys.platform == "win32" and "cancel scope" in str(exc):
                    log.debug("shutdown_all: anyio cancel-scope wart on %s (ignored)", cid)
                else:
                    log.warning("shutdown_all: client %s raised %s", cid, exc)
            except Exception as exc:
                log.debug("shutdown_all: client %s raised %s", cid, type(exc).__name__)
        self._clients.clear()
