"""
Clarify tool — agent-initiated multiple-choice or open-ended question to user.

The tool itself only validates input and dispatches to a platform-provided
``callback(question, choices) -> str``. The CLI sets the callback at startup
(see :mod:`cli`) to either an arrow-key picker (TTY) or a numbered text prompt.

Without a callback (e.g. when imported outside the CLI), the tool returns a
JSON error explaining the situation rather than crashing.
"""

from __future__ import annotations

import json
from typing import Callable, List, Optional


MAX_CHOICES = 4
_OTHER_LABEL = "Other (type your answer)"

# Set by cli.py during startup. Type: (question: str, choices: list[str] | None) -> str
_callback: Optional[Callable[[str, Optional[List[str]]], str]] = None


def set_callback(fn: Callable[[str, Optional[List[str]]], str]) -> None:
    """Register the platform-specific UI callback (called by cli.py)."""
    global _callback
    _callback = fn


def clarify(question: str, choices: Optional[List[str]] = None) -> str:
    """Ask the user a clarifying question. Returns a JSON string with the answer."""
    question = (question or "").strip()
    if not question:
        return json.dumps({"error": "Question text is required."}, ensure_ascii=False)

    cleaned: Optional[List[str]] = None
    if choices is not None:
        if not isinstance(choices, list):
            return json.dumps({"error": "choices must be a list of strings."}, ensure_ascii=False)
        cleaned = [str(c).strip() for c in choices if str(c).strip()]
        if not cleaned:
            cleaned = None
        elif len(cleaned) > MAX_CHOICES:
            cleaned = cleaned[:MAX_CHOICES]

    if _callback is None:
        return json.dumps(
            {"error": "Clarify is not available in this execution context (no UI callback registered)."},
            ensure_ascii=False,
        )

    try:
        answer = _callback(question, cleaned)
    except Exception as exc:
        return json.dumps({"error": f"Failed to collect user input: {exc}"}, ensure_ascii=False)

    return json.dumps(
        {
            "question": question,
            "choices_offered": cleaned,
            "user_response": str(answer or "").strip(),
        },
        ensure_ascii=False,
    )


def get_other_label() -> str:
    """Label appended to the choice list by the CLI for free-form input."""
    return _OTHER_LABEL
