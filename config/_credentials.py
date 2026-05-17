"""credentials.json read/write — API keys keyed by env var name.

Lives at ``.langchain-agent/credentials.json`` (overridable via
``LANGCHAIN_AGENT_CONFIG_DIR``). On save we chmod 0o600 and append the file to
a sibling .gitignore so it doesn't get committed by accident.

``hydrate_env_from_credentials`` is the bridge between this on-disk format and
the SDK's expectation that ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` etc. are
present in process env — called at startup before any LLM is constructed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import agent_paths

from ._providers import ActiveConfig


def load_credentials() -> dict[str, str]:
    creds_file = agent_paths.credentials_path()
    if not creds_file.is_file():
        return {}
    try:
        data = json.loads(creds_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v}


def save_credential(env_name: str, value: str) -> None:
    if not env_name or not value:
        raise ValueError("env_name and value are required")
    creds = load_credentials()
    creds[env_name] = value
    creds_file = agent_paths.credentials_path()
    creds_file.parent.mkdir(parents=True, exist_ok=True)
    creds_file.write_text(
        json.dumps(creds, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(creds_file, 0o600)
    except OSError:
        pass
    gitignore = agent_paths.credentials_gitignore_path()
    if not gitignore.exists():
        try:
            gitignore.write_text("credentials.json\n", encoding="utf-8")
        except OSError:
            pass


def hydrate_env_from_credentials() -> None:
    for env_name, value in load_credentials().items():
        os.environ.setdefault(env_name, value)


def credentials_path() -> Path:
    return agent_paths.credentials_path()


def is_config_ready(cfg: ActiveConfig) -> bool:
    """A config is runnable when it has a model, base_url and a discoverable API key."""
    if not cfg.model or not cfg.base_url or not cfg.api_key_env:
        return False
    if os.getenv(cfg.api_key_env):
        return True
    return cfg.api_key_env in load_credentials()


def get_api_key(cfg: ActiveConfig) -> str:
    """Look up the API key for *cfg* from env then credentials file."""
    return os.getenv(cfg.api_key_env) or load_credentials().get(cfg.api_key_env, "")


def validate_api_key(cfg: ActiveConfig | None = None) -> None:
    # Imported lazily because ``_settings.load_active_config`` indirectly
    # touches ``agent_paths`` for I/O — keeping the import local avoids
    # forcing settings.json reads on callers that already passed an explicit
    # cfg.
    if cfg is None:
        from ._settings import load_active_config
        cfg = load_active_config()
    if not get_api_key(cfg):
        raise RuntimeError(
            f"{cfg.api_key_env} is not set. Run /model (or /setup) to configure "
            f"a provider and API key, or export {cfg.api_key_env}."
        )
