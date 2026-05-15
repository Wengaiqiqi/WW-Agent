from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Card:
    id: str
    display_name: str
    version: str
    entrypoint: dict
    mcp: dict
    a2a: dict
    capabilities_hint: list[str]
    model_override: dict | None


def load_cards(cards_dir: Path) -> list[Card]:
    """Load all *.card.json files under cards_dir. Silently skip malformed files."""
    if not cards_dir.exists():
        return []
    out: list[Card] = []
    for path in sorted(cards_dir.glob("*.card.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append(Card(
                id=data["id"],
                display_name=data["display_name"],
                version=data["version"],
                entrypoint=data["entrypoint"],
                mcp=data["mcp"],
                a2a=data["a2a"],
                capabilities_hint=data.get("capabilities_hint", []),
                model_override=data.get("model_override"),
            ))
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("ignoring invalid card %s: %s", path, exc)
    return out
