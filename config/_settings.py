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
    """Resolve the active model from process env + settings.json.

    Thin shim over :func:`resolve_config` for the single-user CLI / legacy path;
    building a ``TurnContext`` from env reproduces the prior env-driven behavior
    (``LANGCHAIN_AGENT_MODEL`` > settings.json > ``DEFAULT_PROVIDER``, then the
    ``BASE_URL`` / ``PROTOCOL`` / ``API_KEY`` overrides).
    """
    from orchestrator.turn_context import TurnContext

    return resolve_config(TurnContext.from_env())


def resolve_config(ctx) -> ActiveConfig:
    """Resolve an ``ActiveConfig`` from an explicit ``TurnContext``.

    No ``os.environ`` reads beyond the settings.json base — the per-turn config
    comes from *ctx*, so two turns resolving concurrently can't see each other's
    selection. ctx overrides win over settings.json.
    """
    cfg = _resolve_base_config(ctx.model_id)
    if ctx.base_url:
        cfg.base_url = ctx.base_url
    if ctx.protocol:
        # Validate against the protocols ``config._llm`` actually understands.
        # An unknown/typo value (e.g. "gemini", "openai ") would otherwise be
        # accepted verbatim and silently fall through to the OpenAI client,
        # surfacing as an opaque parse/4xx error only at invoke time. Reject it
        # here, leaving the resolved protocol untouched.
        if ctx.protocol in _KNOWN_PROTOCOLS:
            cfg.protocol = ctx.protocol
        else:
            logger.warning(
                "Ignoring protocol %r from context: unknown (expected one of "
                "%s). Keeping %r.",
                ctx.protocol, sorted(_KNOWN_PROTOCOLS), cfg.protocol,
            )
    if ctx.api_key:
        cfg.api_key = ctx.api_key
    return cfg


def _resolve_base_config(model_choice: str = "") -> ActiveConfig:
    """Resolve provider+model from an explicit model choice (``provider/model``
    or ``provider``), falling back to settings.json then ``DEFAULT_PROVIDER``.

    The explicit arg replaces the prior direct ``LANGCHAIN_AGENT_MODEL`` env
    read so the source of truth is the caller's ``TurnContext``, not
    process-global env.
    """
    env_choice = (model_choice or "").strip()
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
