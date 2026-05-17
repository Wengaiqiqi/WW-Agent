"""settings.json read/write and active-config resolution.

The active selection lives at ``.langchain-agent/settings.json`` under the
``model`` key — a dict of {provider, model, base_url, api_key_env}. Selection
priority on startup: ``LANGCHAIN_AGENT_MODEL`` env > settings.json > the
provider registry's ``DEFAULT_PROVIDER``.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import agent_paths

from ._providers import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    ActiveConfig,
    make_config,
)

logger = logging.getLogger(__name__)


def load_active_config() -> ActiveConfig:
    """Resolve which model should be active.

    Order: ``LANGCHAIN_AGENT_MODEL`` env var (``provider`` or
    ``provider/model``), then ``.claude/settings.json`` ``model`` block,
    then ``DEFAULT_PROVIDER`` with its first model.
    """
    env_choice = os.getenv("LANGCHAIN_AGENT_MODEL", "").strip()
    if env_choice:
        if "/" in env_choice:
            prov_name, model_name = env_choice.split("/", 1)
        else:
            prov_name, model_name = env_choice, ""
        if prov_name in PROVIDERS:
            return make_config(prov_name, model=model_name)

    settings = _read_settings()
    model_block = settings.get("model")
    if isinstance(model_block, dict):
        prov_name = str(model_block.get("provider") or "")
        if prov_name in PROVIDERS:
            return make_config(
                prov_name,
                model=str(model_block.get("model") or ""),
                base_url=str(model_block.get("base_url") or ""),
                api_key_env=str(model_block.get("api_key_env") or ""),
            )
    elif isinstance(model_block, str) and model_block:
        # Legacy schema: settings.json["model"] used to be a preset name string
        # (e.g. "mimo-pro"). The new schema stores a {provider, model, base_url,
        # api_key_env} dict. Warn so the user knows their saved selection was
        # dropped.
        logger.warning(
            "Ignoring legacy settings.json model entry %r; the schema is now a "
            "dict. Run /model to reconfigure (falling back to provider %r).",
            model_block, DEFAULT_PROVIDER,
        )

    return make_config(DEFAULT_PROVIDER)


def save_active_config(cfg: ActiveConfig) -> None:
    """Persist *cfg* under the ``model`` key in the agent's settings.json."""
    if cfg.provider not in PROVIDERS:
        raise KeyError(f"Unknown provider: {cfg.provider!r}")
    settings = _read_settings()
    settings["model"] = cfg.to_settings_dict()
    settings_file = agent_paths.settings_path()
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _read_settings() -> dict[str, Any]:
    settings_file = agent_paths.settings_path()
    if not settings_file.is_file():
        return {}
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def settings_path():
    """Public alias so callers don't need to import ``agent_paths`` themselves."""
    return agent_paths.settings_path()
