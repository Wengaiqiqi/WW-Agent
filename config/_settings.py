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

# Wire protocols ``config._llm`` knows how to build a client for. Anything else
# (a typo, an unsupported provider name) is rejected by the env override path.
_KNOWN_PROTOCOLS = {"openai", "anthropic", "mock"}


def load_active_config() -> ActiveConfig:
    """Resolve which model should be active, then apply per-turn env overrides.

    Base resolution order: ``LANGCHAIN_AGENT_MODEL`` env > settings.json >
    ``DEFAULT_PROVIDER``. After that, ``LANGCHAIN_AGENT_BASE_URL`` and
    ``LANGCHAIN_AGENT_PROTOCOL`` (when set) override the resolved config — the
    web "custom endpoint" feature sets these for the duration of a turn.
    """
    return _apply_env_overrides(_resolve_base_config())


def _resolve_base_config() -> ActiveConfig:
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
        logger.warning(
            "Ignoring legacy settings.json model entry %r; the schema is now a "
            "dict. Run /model to reconfigure (falling back to provider %r).",
            model_block, DEFAULT_PROVIDER,
        )

    return make_config(DEFAULT_PROVIDER)


def _apply_env_overrides(cfg: ActiveConfig) -> ActiveConfig:
    """Apply ``LANGCHAIN_AGENT_BASE_URL`` / ``LANGCHAIN_AGENT_PROTOCOL`` if set.

    No-op when neither is present, so non-web callers are unaffected.
    """
    base_url = os.getenv("LANGCHAIN_AGENT_BASE_URL", "").strip()
    protocol = os.getenv("LANGCHAIN_AGENT_PROTOCOL", "").strip()
    if base_url:
        cfg.base_url = base_url
    if protocol:
        # Validate against the protocols ``config._llm`` actually understands.
        # An unknown/typo value (e.g. "gemini", "openai ") would otherwise be
        # accepted verbatim and silently fall through to the OpenAI client,
        # surfacing as an opaque parse/4xx error only at invoke time. Reject it
        # at config resolution with a clear message instead, leaving the
        # resolved protocol untouched.
        if protocol in _KNOWN_PROTOCOLS:
            cfg.protocol = protocol
        else:
            logger.warning(
                "Ignoring LANGCHAIN_AGENT_PROTOCOL=%r: unknown protocol "
                "(expected one of %s). Keeping %r.",
                protocol, sorted(_KNOWN_PROTOCOLS), cfg.protocol,
            )
    return cfg


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
