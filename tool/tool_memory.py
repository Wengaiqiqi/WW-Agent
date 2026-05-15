"""
Persistent curated memory: ``MEMORY.md`` (agent notes) + ``USER.md`` (user profile).

Two files under ``<agent config dir>/memories/`` (see :mod:`agent_paths`):

- **MEMORY.md** — facts the agent learns about the codebase, environment,
  conventions, failed approaches, etc.
- **USER.md** — facts about the user: preferences, communication style,
  recurring goals.

The CLI injects a *frozen* snapshot of both files into the system prompt at
session start. Tool calls during the session update the files on disk
immediately (so the next session sees the changes) but do NOT mutate the
in-prompt snapshot (keeps prefix cache stable).

Entries are delimited by ``\\n§\\n`` and capped per-file. A lightweight
content scan blocks obvious prompt-injection / exfiltration payloads, since
memory text ends up inside the system prompt.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

import agent_paths


_DELIM = "\n§\n"
_MEMORY_CHAR_LIMIT = 4000
_USER_CHAR_LIMIT = 2000

_TARGETS = {"memory", "user"}


_THREAT_PATTERNS = [
    (r"ignore\s+(previous|all|above|prior)\s+instructions", "prompt_injection"),
    (r"you\s+are\s+now\s+", "role_hijack"),
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", "disregard_rules"),
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", "read_secrets"),
]
_INVISIBLE = {"​", "‌", "‍", "⁠", "﻿", "‪", "‫", "‬", "‭", "‮"}


def _scan(content: str) -> Optional[str]:
    for ch in _INVISIBLE:
        if ch in content:
            return f"Blocked: invisible unicode U+{ord(ch):04X} in memory content."
    for pattern, pid in _THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f"Blocked: content matches threat pattern '{pid}'."
    return None


def _path_for(target: str) -> Path:
    return agent_paths.memories_dir() / ("USER.md" if target == "user" else "MEMORY.md")


def _limit_for(target: str) -> int:
    return _USER_CHAR_LIMIT if target == "user" else _MEMORY_CHAR_LIMIT


def _read_entries(target: str) -> List[str]:
    path = _path_for(target)
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries = [e.strip() for e in raw.split(_DELIM)]
    return [e for e in entries if e]


def _write_entries(target: str, entries: List[str]) -> None:
    agent_paths.memories_dir().mkdir(parents=True, exist_ok=True)
    path = _path_for(target)
    body = _DELIM.join(entries)
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def _summary(target: str, message: str) -> dict:
    entries = _read_entries(target)
    used = len(_DELIM.join(entries)) if entries else 0
    return {
        "success": True,
        "target": target,
        "message": message,
        "usage": f"{used:,}/{_limit_for(target):,} chars",
        "entries": entries,
    }


def memory(action: str, target: str = "memory", content: str = "", old_text: str = "") -> dict:
    """``action`` ∈ {add, replace, remove, read}; ``target`` ∈ {memory, user}."""
    if target not in _TARGETS:
        return {"success": False, "error": f"target must be one of {sorted(_TARGETS)}"}

    if action == "read":
        return _summary(target, "Read OK.")

    if action == "add":
        text = (content or "").strip()
        if not text:
            return {"success": False, "error": "content cannot be empty."}
        scan_err = _scan(text)
        if scan_err:
            return {"success": False, "error": scan_err}
        entries = _read_entries(target)
        entries = list(dict.fromkeys(entries))  # de-dupe
        if text in entries:
            return _summary(target, "Entry already exists (no duplicate added).")
        new_total = len(_DELIM.join(entries + [text]))
        limit = _limit_for(target)
        if new_total > limit:
            return {
                "success": False,
                "error": (
                    f"Memory at {len(_DELIM.join(entries)):,}/{limit:,} chars. "
                    f"Adding this entry ({len(text)} chars) would exceed the limit. "
                    "Replace or remove existing entries first."
                ),
                "entries": entries,
            }
        entries.append(text)
        _write_entries(target, entries)
        return _summary(target, "Entry added.")

    if action == "replace":
        find_text = (old_text or "").strip()
        new_text = (content or "").strip()
        if not find_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_text:
            return {"success": False, "error": "content cannot be empty. Use action='remove' to delete entries."}
        scan_err = _scan(new_text)
        if scan_err:
            return {"success": False, "error": scan_err}
        entries = _read_entries(target)
        matches = [i for i, e in enumerate(entries) if find_text in e]
        if not matches:
            return {"success": False, "error": f"No entry matched {find_text!r}."}
        if len({entries[i] for i in matches}) > 1:
            previews = [entries[i][:80] + ("..." if len(entries[i]) > 80 else "") for i in matches]
            return {
                "success": False,
                "error": f"Multiple entries matched {find_text!r}; be more specific.",
                "matches": previews,
            }
        entries[matches[0]] = new_text
        new_total = len(_DELIM.join(entries))
        limit = _limit_for(target)
        if new_total > limit:
            return {"success": False, "error": f"Replacement would exceed limit ({new_total:,}/{limit:,})."}
        _write_entries(target, entries)
        return _summary(target, "Entry replaced.")

    if action == "remove":
        find_text = (old_text or content or "").strip()
        if not find_text:
            return {"success": False, "error": "old_text or content (substring to match) cannot be empty."}
        entries = _read_entries(target)
        keep = [e for e in entries if find_text not in e]
        removed = len(entries) - len(keep)
        if removed == 0:
            return {"success": False, "error": f"No entry matched {find_text!r}."}
        _write_entries(target, keep)
        return _summary(target, f"Removed {removed} entr{'y' if removed == 1 else 'ies'}.")

    return {"success": False, "error": f"Unknown action: {action!r}. Use add | replace | remove | read."}


def memory_tool(action: str, target: str = "memory", content: str = "", old_text: str = "") -> str:
    """JSON string entry point used by the LangChain tool wrapper."""
    return json.dumps(memory(action, target, content, old_text), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# System-prompt snapshot — read at session start, never mutated mid-session.
# ---------------------------------------------------------------------------
def snapshot_for_system_prompt() -> str:
    """Return a Markdown block to inject into the system prompt.

    Empty string if no memory has been recorded yet.
    """
    parts: list[str] = []
    user_entries = _read_entries("user")
    memory_entries = _read_entries("memory")
    if user_entries:
        parts.append("## What you know about the user (USER.md)")
        parts.extend(f"- {e}" for e in user_entries)
    if memory_entries:
        if parts:
            parts.append("")
        parts.append("## Working memory (MEMORY.md)")
        parts.extend(f"- {e}" for e in memory_entries)
    if not parts:
        return ""
    return "\n".join(["# Persistent memory (frozen snapshot for this session)", "", *parts])
