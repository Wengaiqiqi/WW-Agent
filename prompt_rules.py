"""Shared prompt-engineering constants used across all agent paths.

Centralizing here keeps language / style rules consistent. These constants
were previously inlined in four different system prompts with subtly
different wording — switching paths (legacy ↔ multi-agent ↔ tool-agent
↔ synthesize) could flip the agent's defaults. One source of truth.

Style philosophy:
- Rules are short, declarative, and reference behavior — not personality.
- Negative rules ("don't print raw markup") name the actual failure mode so
  the model can reason about edge cases instead of pattern-matching keywords.
- Language defaults are user-driven, not project-pinned, so the same agent
  serves bilingual users without per-prompt overrides.
"""

from __future__ import annotations

LANGUAGE_RULE = (
    "Reply in the same language the user used in their latest message. "
    "Match their formality."
)

NO_RAW_TOOL_MARKUP_RULE = (
    "Never print raw tool-call markup such as <tool_call>, <function=...>, or "
    "<parameter=...>. Tool use happens through the provided tool-calling API, "
    "not by writing tool calls as text."
)

CONCISE_RULE = (
    "Be concise. Start with the substance — skip wind-up phrases like "
    "'Sure, let me ...' or '好的，我来 ...'. Single-paragraph answers when "
    "possible; use bullets only for genuine lists."
)

STOP_WHEN_DONE_RULE = (
    "Stop the moment the requested action is verified. No re-reads to count "
    "characters, no exploring neighbor files, no defensive self-checks the "
    "user did not ask for."
)
