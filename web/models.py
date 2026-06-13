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


def _detect_xiaomi_provider(api_key_env: str) -> str | None:
    """Detect which Xiaomi endpoint accepts the API key. Returns provider name or None."""
    from config._settings import _detect_xiaomi_provider as detect

    try:
        return detect(api_key_env)
    except Exception:
        return None


def available_models() -> list[dict[str, str]]:
    creds = _credentials()
    out: list[dict[str, str]] = []
    seen_models: set[str] = set()

    # Cache detected xiaomi provider to avoid repeated API calls
    _xiaomi_provider: str | None = None
    _xiaomi_detected = False

    for name, prov in _providers().items():
        env = prov.get("api_key_env")
        if not env:
            continue
        if not (os.getenv(env) or env in creds):
            continue

        # For xiaomi providers, only include the one that accepts the API key
        if name in ("xiaomi", "xiaomi-anthropic"):
            if not _xiaomi_detected:
                _xiaomi_detected = True
                _xiaomi_provider = _detect_xiaomi_provider(env)
            if _xiaomi_provider and name != _xiaomi_provider:
                continue  # Skip the provider that doesn't work

        label = prov.get("label", name)
        for model in prov.get("models", []):
            # Deduplicate models by model name, keeping only the first provider
            if model in seen_models:
                continue
            seen_models.add(model)
            out.append(
                {"id": f"{name}/{model}", "provider": name, "label": label, "model": model}
            )
    return out
