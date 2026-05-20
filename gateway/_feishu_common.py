"""Shared helpers for the Feishu webhook + WS adapters.

Both ``gateway.feishu`` (webhook) and ``gateway.feishu_ws`` (long connection)
need the same things: config coercion, text extraction, mention stripping,
bot-identity heuristics, and outbound reply truncation. Keeping them in one
place stops the two adapters from drifting apart -- which is how the
webhook adapter ended up missing the session/memory wiring and the
group-@ failure-fallback that the WS adapter has.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, Mapping, Optional

log = logging.getLogger(__name__)


DEFAULT_DOMAIN = "open.feishu.cn"

# Feishu single text message body cap is documented at ~30 KB, but in
# practice replies much smaller than that get truncated by the server's
# rich-text renderer. Stay well under: 8 KB hard cap + a short suffix so
# users know it was cut off.
REPLY_CHAR_CAP = 8000
_TRUNCATION_SUFFIX = "\n...(已截断,完整内容过长)"


def coerce_common(cfg: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Normalise the fields both adapters share (app_id, app_secret, domain).

    Each adapter's own ``_coerce`` calls this then layers its mode-specific
    fields on top (verify_token / encrypt_key for webhook; nothing extra
    for WS).
    """
    cfg = dict(cfg or {})
    return {
        "app_id": str(cfg.get("app_id") or "").strip(),
        "app_secret": str(cfg.get("app_secret") or "").strip(),
        "domain": (str(cfg.get("domain") or "").strip() or DEFAULT_DOMAIN),
    }


def extract_text_from_dict(message: Mapping[str, Any]) -> str:
    """Parse a Feishu message JSON dict (webhook shape) into plain text."""
    msg_type = message.get("message_type", "")
    raw = message.get("content", "")
    if not raw:
        return ""
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    return _flatten_body(msg_type, body)


def extract_text_from_obj(message: Any) -> str:
    """Parse a Feishu message SDK object (WS shape) into plain text."""
    msg_type = getattr(message, "message_type", "")
    raw = getattr(message, "content", "")
    if not raw:
        return ""
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    return _flatten_body(msg_type, body)


def _flatten_body(msg_type: str, body: Mapping[str, Any]) -> str:
    if msg_type == "text":
        return str(body.get("text", "")).strip()
    if msg_type == "post":
        lines: list[str] = []
        for paragraph in body.get("content", []) or []:
            chunks = []
            for el in paragraph or []:
                if not isinstance(el, Mapping):
                    continue
                t = el.get("tag")
                if t == "text":
                    chunks.append(str(el.get("text", "")))
                elif t == "a":
                    chunks.append(str(el.get("text") or el.get("href", "")))
                elif t == "at":
                    chunks.append("@" + str(el.get("user_name", "")))
            line = "".join(chunks).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)
    return ""


# ---------------------------------------------------------------------------
# Mention handling
# ---------------------------------------------------------------------------


def bot_was_mentioned(mentions: Iterable[Any], *, bot_open_id: str) -> bool:
    """True iff a bot-mention is present.

    Strategy:
    * If we know our own ``open_id``, match it exactly against each mention's
      id.open_id (works for both dict and SDK-object shapes).
    * If ``bot_open_id`` is empty (probe failed), fall back to "any mention
      present". That over-triggers when a user @-mentions another person
      alongside the bot, but it never under-triggers, and group events get
      this filter only when at least one mention exists anyway.

    Accepts both webhook-style dicts and ws-style SDK objects in the same
    iterable; checks both shapes.
    """
    mentions_list = list(mentions) if mentions else []
    if not mentions_list:
        return False
    if not bot_open_id:
        return True
    for m in mentions_list:
        if _mention_open_id(m) == bot_open_id:
            return True
    return False


def strip_mentions(text: str, mentions: Iterable[Any]) -> str:
    """Remove every ``@_user_X`` placeholder from text.

    Strips ALL mentions, not just bot mentions: the orchestrator doesn't
    care about the at-prefix in either case, and detecting "which mention
    is the bot" requires our own open_id which may be unavailable.
    """
    if not mentions:
        return text
    for m in mentions:
        key = _mention_key(m)
        if key and key in text:
            text = text.replace(key, "").strip()
    return text


def _mention_open_id(m: Any) -> str:
    if isinstance(m, Mapping):
        return str((m.get("id") or {}).get("open_id") or "")
    m_id = getattr(m, "id", None)
    if m_id is None:
        return ""
    return str(getattr(m_id, "open_id", "") or "")


def _mention_key(m: Any) -> str:
    if isinstance(m, Mapping):
        return str(m.get("key") or "")
    return str(getattr(m, "key", "") or "")


# ---------------------------------------------------------------------------
# Reply truncation
# ---------------------------------------------------------------------------


def truncate_reply(text: str, *, cap: int = REPLY_CHAR_CAP) -> str:
    """Trim a reply to fit Feishu's text message cap, with a clear suffix.

    Without this Feishu silently rejects oversized messages (the API returns
    success but no message is delivered), or in rare cases truncates them
    mid-character at the rendering layer.
    """
    if not text:
        return text
    if len(text) <= cap:
        return text
    keep = cap - len(_TRUNCATION_SUFFIX)
    if keep < 200:
        # Cap is tiny -- just hard truncate without suffix.
        return text[:cap]
    return text[:keep] + _TRUNCATION_SUFFIX


# ---------------------------------------------------------------------------
# Session / memory key derivation
# ---------------------------------------------------------------------------


def session_key_for(chat_id: str) -> str:
    """Conversation-history key. Same chat = shared 25-turn rolling history."""
    return f"feishu:{chat_id}" if chat_id else ""


def extract_sender_open_id(sender: Any) -> str:
    """Pull the sender's ``open_id`` out of a webhook dict or SDK object."""
    if isinstance(sender, Mapping):
        return str((sender.get("sender_id") or {}).get("open_id") or "")
    sid = getattr(sender, "sender_id", None)
    if sid is None:
        return ""
    return str(getattr(sid, "open_id", "") or "")
