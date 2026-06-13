"""The /comm, /task, /chat commands — cross-machine remote agent协作.

Extracted from ReplCommandHandler. Owns the current-peer selection (persisted
across restarts) and per-peer chat contexts, and drives the comm-agent over
the MCP host. Depends on the UI and the MCP host.
"""
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


class RemoteCommands:
    def __init__(self, *, ui: ReplUI, host):
        self.ui = ui
        self.host = host
        # current peer persists across restarts (comm_session.json); chat
        # contexts stay memory-only.
        self._current_peer: str | None = _load_persisted_peer()
        self._chat_contexts: dict[str, str] = {}

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

    async def comm(self, line: str) -> LoopAction:
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
        _persist_peer(peer_id)
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

    async def task(self, line: str) -> LoopAction:
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

    async def chat(self, line: str) -> LoopAction:
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
