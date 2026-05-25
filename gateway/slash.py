"""Slash commands for chat-platform gateways (QQ / Feishu).

Whitelisted users drive remote A2A peers from chat:
  /task <peer_id> <task>     one-shot delegation (comm.delegate)
  /chat <peer_id> <message>  multi-turn conversation (comm.chat, context kept)
  /peers                     list registered peer_ids
  /help                      usage

``handle_slash`` returns the reply STRING for a handled command, or ``None`` to
fall through to the normal planner path (non-slash input, or an unrecognized
/command). UI-free on purpose: the REPL's ReplCommandHandler is coupled to Rich
rendering and an in-memory current-peer; the gateway runs one isolated turn per
message and needs a plain-text reply.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gateway import credentials as gw_creds

COMM_AGENT_ID = "comm-agent"
_RECOGNIZED = {"/task", "/chat", "/peers", "/help"}


def _platform_from_session_key(session_key: str) -> str:
    """``qq:123`` -> ``qq``; ``feishu:abc`` -> ``feishu``; no prefix -> ``""``."""
    if not session_key or ":" not in session_key:
        return ""
    return session_key.split(":", 1)[0]


def _allowed_users(platform: str) -> list[str]:
    """Read the per-platform allowlist from gateways.json.

    Accepts either a comma-separated string (what the setup wizard writes) or a
    JSON list (a hand-edited gateways.json). Empty / missing -> ``[]``.
    """
    if not platform:
        return []
    users = gw_creds.load(platform).get("allowed_users") or []
    if isinstance(users, str):
        users = [u.strip() for u in users.split(",") if u.strip()]
    return [str(u) for u in users]


def _is_authorized(session_key: str, user_id: str) -> bool:
    """Fail-safe: no user id, or empty/missing allowlist, denies."""
    if not user_id:
        return False
    return user_id in _allowed_users(_platform_from_session_key(session_key))


def _unwrap(result: Any) -> tuple[bool, str]:
    """Normalize call_tool result into (is_error, text). Mirrors the REPL handler."""
    try:
        is_error = bool(getattr(result, "isError", False))
        content = getattr(result, "content", None)
        if content and hasattr(content[0], "text"):
            return is_error, content[0].text
    except (IndexError, TypeError, AttributeError):
        pass
    try:
        is_error = bool(result.get("isError", False))
        content = result.get("content", [])
        if content:
            return is_error, content[0].get("text", "")
    except (AttributeError, IndexError, TypeError):
        pass
    return True, "unexpected call_tool result format"


async def _call_comm(host, tool: str, args: dict) -> tuple[bool, dict]:
    """Call a comm.* tool; return (ok, data). data carries {'error': ...} on failure."""
    result = await host.call_tool(COMM_AGENT_ID, tool, args)
    is_error, text = _unwrap(result)
    if is_error:
        return False, {"error": text or "comm-agent unavailable"}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False, {"error": f"invalid comm response: {text!r}"}
    if not data.get("ok", True):
        return False, {"error": data.get("error", str(data))}
    return True, data


_USAGE = (
    "可用命令:\n"
    "/task <peer_id> <任务>  — 委托一次性任务给远程 peer\n"
    "/chat <peer_id> <消息>  — 与远程 peer 多轮对话\n"
    "/peers                  — 列出已注册的 peer\n"
    "/help                   — 显示本帮助"
)


async def _do_peers(host) -> str:
    ok, data = await _call_comm(host, "comm.list_peers", {})
    if not ok:
        return f"获取 peer 列表失败:{data.get('error')}"
    peers = data.get("peers", [])
    if not peers:
        return "还没有注册任何 peer。(在 REPL 里用 /comm add 添加)"
    lines = ["已注册的 peer:"]
    for p in peers:
        lines.append(f"- {p.get('peer_id', '')} — {p.get('display_name', '')}")
    return "\n".join(lines)


def _render_final(final: Any) -> str:
    """comm.delegate final_result may be a dict with A2A parts, a str, or None."""
    if isinstance(final, dict):
        parts_list = final.get("parts", [])
        joined = "\n".join(
            p.get("text", "") for p in parts_list
            if isinstance(p, dict) and p.get("text")
        )
        return joined or json.dumps(final, ensure_ascii=False)
    if final is None:
        return "(无结果)"
    return str(final)


async def _do_task(host, parts: list[str]) -> str:
    if len(parts) < 3 or not parts[2].strip():
        return "用法:/task <peer_id> <任务>"
    peer_id, task = parts[1], parts[2]
    ok, data = await _call_comm(host, "comm.delegate", {
        "peer_id": peer_id, "task": task, "stream": False,
    })
    if not ok:
        return f"委托失败:{data.get('error')}"
    return f"[{peer_id}] {_render_final(data.get('final_result'))}"


async def _do_chat(host, parts: list[str], session_key: str) -> str:  # replaced in Task 4
    return "(chat not implemented)"


async def handle_slash(line: str, *, host, session_key: str, user_id: str) -> str | None:
    """Dispatch a chat-platform slash command. See module docstring for contract."""
    line = (line or "").strip()
    if not line.startswith("/"):
        return None
    parts = line.split(maxsplit=2)
    command = parts[0].lower()
    if command not in _RECOGNIZED:
        return None  # unknown slash -> planner fall-through (today's behaviour)

    if not _is_authorized(session_key, user_id):
        return (
            "抱歉,你没有权限使用这个命令。"
            "(管理员可在 /gateway setup 的 allowed_users 里添加你的 user_id)"
        )

    if command == "/help":
        return _USAGE
    if command == "/peers":
        return await _do_peers(host)
    if command == "/task":
        return await _do_task(host, parts)
    if command == "/chat":
        return await _do_chat(host, parts, session_key)
    return None  # unreachable (command in _RECOGNIZED)
