"""Build + validate Google A2A v0.3 Agent Cards.

Only the fields we use are validated; unknown fields pass through so a
future spec extension doesn't break us (forward-compat per spec §8).
"""
from __future__ import annotations

from typing import Any


class AgentCardError(Exception):
    pass


_SCHEMA_VERSION = "0.3"

_REQUIRED_FIELDS = ("schemaVersion", "name", "description", "url", "version", "skills")


def build_self_card(
    *,
    name: str,
    description: str,
    public_url: str,
    version: str,
) -> dict[str, Any]:
    """Construct OUR agent card for /.well-known/agent.json."""
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "name": name,
        "description": description,
        "url": public_url,
        "version": version,
        "provider": {
            "organization": "agent-last",
            "url": "https://github.com/agent-last/agent-last",
        },
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "authentication": {
            "schemes": ["HMAC-SHA256"],
            "credentials": "see install instructions",
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "task.delegate",
                "name": "Delegate a task",
                "description": "Hand off a free-form task to this agent; returns SSE stream of progress + final result",
                "tags": ["delegation", "task"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            },
            {
                "id": "chat.message",
                "name": "Send chat message",
                "description": "Append a turn to a chat session (context_id-keyed)",
                "tags": ["chat", "multi-turn"],
            },
            {
                "id": "status.query",
                "name": "Query status",
                "description": "Return current agent state + tool inventory",
            },
        ],
    }


def validate_card(card: dict[str, Any]) -> None:
    """Validate the fields WE depend on. Unknown fields are tolerated."""
    for field in _REQUIRED_FIELDS:
        if field not in card:
            raise AgentCardError(f"missing required field {field!r}")
    if card["schemaVersion"] != _SCHEMA_VERSION:
        raise AgentCardError(
            f"unsupported schemaVersion {card['schemaVersion']!r}; "
            f"this client speaks {_SCHEMA_VERSION}"
        )
    if not isinstance(card["skills"], list):
        raise AgentCardError("'skills' must be a list")
