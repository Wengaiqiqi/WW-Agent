"""Model configuration — hermes-style provider + model selection.

A ``provider`` is an endpoint family (Xiaomi MiMo, DeepSeek, OpenAI, Anthropic,
or a user-supplied custom endpoint). Each provider declares ``label``,
``protocol``, ``base_url``, ``api_key_env``, and ``models``.

The active selection is an :class:`ActiveConfig`. The CLI's ``/model`` command
runs an interactive 4-step wizard that writes the result into
``.langchain-agent/settings.json``. API keys live in
``.langchain-agent/credentials.json`` (keyed by env var name). Override the
directory with the ``LANGCHAIN_AGENT_CONFIG_DIR`` env var.

Selection priority on startup (highest first):
    1. ``LANGCHAIN_AGENT_MODEL`` env var (provider name, optional ``/model`` suffix)
    2. ``model`` block in ``settings.json``
    3. Provider ``DEFAULT_PROVIDER`` with its first model

This package replaces a single 800-line ``config.py``. The split:

- ``_providers``    — registry + ActiveConfig + factories (zero I/O, zero langchain)
- ``_settings``     — settings.json read/write, load/save_active_config
- ``_credentials``  — credentials.json + hydrate + get_api_key + validate
- ``_llm``          — ReasoningChatOpenAI + build_llm (the only langchain importer)

External callers should keep using ``from config import build_llm, ...`` —
everything below is re-exported at this level for full backward compatibility.
"""
from __future__ import annotations

from ._providers import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROVIDER,
    DEFAULT_STREAMING,
    DEFAULT_TEMPERATURE,
    PROVIDERS,
    ActiveConfig,
    default_model_for,
    get_provider,
    list_providers,
    make_config,
)
from ._settings import (
    load_active_config,
    save_active_config,
    settings_path,
)
from ._credentials import (
    credentials_path,
    get_api_key,
    hydrate_env_from_credentials,
    is_config_ready,
    load_credentials,
    save_credential,
    validate_api_key,
)
from ._llm import (
    ReasoningChatOpenAI,
    build_llm,
)

__all__ = [
    # providers
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_PROVIDER",
    "DEFAULT_STREAMING",
    "DEFAULT_TEMPERATURE",
    "PROVIDERS",
    "ActiveConfig",
    "default_model_for",
    "get_provider",
    "list_providers",
    "make_config",
    # settings
    "load_active_config",
    "save_active_config",
    "settings_path",
    # credentials
    "credentials_path",
    "get_api_key",
    "hydrate_env_from_credentials",
    "is_config_ready",
    "load_credentials",
    "save_credential",
    "validate_api_key",
    # llm
    "ReasoningChatOpenAI",
    "build_llm",
]
