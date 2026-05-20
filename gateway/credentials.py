"""Persistent credentials for chat-platform gateways.

Stored as one JSON file keyed by platform name under the agent config dir
(default: ``.langchain-agent/gateways.json``). A sibling ``.gitignore`` is
created automatically so credentials don't accidentally leak into git.

Schema::

    {
      "feishu": {
        "app_id":       "cli_...",
        "app_secret":   "...",
        "verify_token": "...",
        "encrypt_key":  "...",            # optional
        "domain":       "open.feishu.cn", # optional
        "host":         "0.0.0.0",        # last used --host
        "port":         8765              # last used --port
      },
      "qq": {
        "app_id":        "12345",
        "client_secret": "...",
        "intents":       1107296256,      # optional
        "sandbox":       false            # optional
      }
    }
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from agent_paths import config_dir

log = logging.getLogger(__name__)


_FILE_NAME = "gateways.json"


def gateways_path() -> Path:
    return config_dir() / _FILE_NAME


def _ensure_gitignore() -> None:
    """Create a ``.gitignore`` next to the credentials file (idempotent).

    Mirrors the protection added for ``credentials.json`` so a user who
    checks ``.langchain-agent/`` into source control doesn't leak bot
    secrets in the first commit.
    """
    gi = config_dir() / ".gitignore"
    if gi.exists():
        return
    try:
        gi.parent.mkdir(parents=True, exist_ok=True)
        gi.write_text("*\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem permission issue
        log.warning("could not write %s: %s", gi, exc)


def load_all() -> Dict[str, Dict[str, Any]]:
    p = gateways_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not parse %s: %s", p, exc)
        return {}
    return data if isinstance(data, dict) else {}


def load(platform: str) -> Dict[str, Any]:
    return dict(load_all().get(platform) or {})


def save(platform: str, creds: Dict[str, Any]) -> Path:
    p = gateways_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore()
    data = load_all()
    data[platform] = dict(creds)
    p.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return p


def clear(platform: str) -> None:
    data = load_all()
    if platform in data:
        data.pop(platform)
        gateways_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def mask(value: str, *, keep: int = 4) -> str:
    """Mask a credential for display: keeps the first ``keep`` chars + ``***``."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 6}"
