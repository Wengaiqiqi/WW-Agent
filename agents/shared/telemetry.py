"""Sidecar telemetry writer for agent subprocesses.

Agents append one JSON line per event to ``.agent/runtime/telemetry.ndjson``;
the orchestrator's tail task picks them up and surfaces them via the unified
stream. Previously each agent imported ``orchestrator.telemetry.emit_event``
to do this — a reverse-direction import that broke the layering (subprocess
reaching back into parent's modules). This module mirrors the file format so
agents stay strictly inside ``agents/`` and ``tool/``.

Secret redaction is intentionally re-applied here rather than relying on the
orchestrator side: by the time the orchestrator reads the file the secret
might have already been mirrored to the unified stream / on-disk log, so the
mask must happen at write time, in the process that originated the message.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_PATH = Path(".agent/runtime/telemetry.ndjson")


# Provider-token patterns. Kept in this module (rather than in
# orchestrator/telemetry.py) so subprocess writes go through the same mask
# without depending on orchestrator/*. New providers / token shapes should
# be added here; the parent process re-imports this list via re-export.
#
# Order matters: more-specific patterns first so e.g. ``sk-ant-...`` is
# captured by the Anthropic-specific entry rather than the generic ``sk-``
# fallback (the replacement string differs).
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # OpenAI / Anthropic / generic sk-* keys
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "sk-ant-***REDACTED***"),
    (re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}"), "sk-proj-***REDACTED***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-***REDACTED***"),
    # GitHub tokens
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "ghp_***REDACTED***"),
    (re.compile(r"\bgho_[A-Za-z0-9]{20,}"), "gho_***REDACTED***"),
    (re.compile(r"\bghs_[A-Za-z0-9]{20,}"), "ghs_***REDACTED***"),
    (re.compile(r"\bghu_[A-Za-z0-9]{20,}"), "ghu_***REDACTED***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}"), "github_pat_***REDACTED***"),
    # AWS access keys
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***REDACTED***"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "ASIA***REDACTED***"),
    # Google / Gemini API keys
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "AIza***REDACTED***"),
    # xAI / Grok
    (re.compile(r"\bxai-[A-Za-z0-9]{20,}"), "xai-***REDACTED***"),
    # Tavily
    (re.compile(r"\btvly-[A-Za-z0-9]{20,}"), "tvly-***REDACTED***"),
    # Slack / Discord webhooks (URLs carry the secret in the path)
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
     "https://hooks.slack.com/services/***REDACTED***"),
    (re.compile(r"https://discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+"),
     "https://discord.com/api/webhooks/***REDACTED***"),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{12,}"), r"\1 ***REDACTED***"),
    # Generic env-var-assignment fallback (catch-all for X_API_KEY=… etc).
    # Kept LAST so the provider-specific patterns above get first dibs and
    # produce more informative replacement strings.
    (re.compile(
        r"(?i)\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)"
        r"=\S{8,}"
    ), r"\1=***REDACTED***"),
)


def redact_secrets(message: str) -> str:
    if not message:
        return message
    redacted = message
    for pat, repl in _SECRET_PATTERNS:
        redacted = pat.sub(repl, redacted)
    return redacted


def emit_event(*, agent_id: str, trace_id: str, message: str) -> None:
    """Append one JSON line to ``telemetry.ndjson``.

    Safe to call from an agent subprocess: no dependency on orchestrator
    modules, no shared state with the parent, no setup required."""
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "agent_id": agent_id,
            "trace_id": trace_id,
            "message": redact_secrets(message),
        }) + "\n")
