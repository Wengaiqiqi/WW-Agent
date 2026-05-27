"""Expose the provider/model choices a web user may pick from — only those
whose API key is discoverable server-side (env or credentials.json). The
selected ``id`` ("provider/model") is set as LANGCHAIN_AGENT_MODEL per turn."""
from __future__ import annotations

import os
from typing import Any


def _providers() -> dict[str, dict[str, Any]]:
    from config import PROVIDERS

    return PROVIDERS


def _credentials() -> dict[str, str]:
    from config import load_credentials

    return load_credentials()


def available_models() -> list[dict[str, str]]:
    creds = _credentials()
    out: list[dict[str, str]] = []
    for name, prov in _providers().items():
        env = prov.get("api_key_env")
        if not env:
            continue
        if not (os.getenv(env) or env in creds):
            continue
        label = prov.get("label", name)
        for model in prov.get("models", []):
            out.append(
                {"id": f"{name}/{model}", "provider": name, "label": label, "model": model}
            )
    return out
