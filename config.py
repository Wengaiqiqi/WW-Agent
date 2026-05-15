"""
Model configuration -- hermes-style provider + model selection.

A ``provider`` is an endpoint family (Xiaomi MiMo, DeepSeek, OpenAI, Anthropic,
or a user-supplied custom endpoint). Each provider declares:
- ``label``        : human-readable name shown in the wizard.
- ``protocol``     : "openai" or "anthropic" wire protocol.
- ``base_url``     : default API endpoint (may be overridden per-session).
- ``api_key_env``  : env var name the SDK reads for credentials.
- ``models``       : list of known model ids (or [] for custom endpoints).

All OpenAI-protocol providers go through :class:`ReasoningChatOpenAI`, which
transparently round-trips ``reasoning_content`` for thinking-mode models
(MiMo, DeepSeek reasoner, Qwen thinking, ...) and is a no-op otherwise.

The active selection is an :class:`ActiveConfig` (provider + model + base_url +
api_key_env + protocol). The CLI's ``/model`` command runs an interactive
4-step wizard (Select provider → Select model → Enter API key → Enter base URL)
that writes the result into ``.langchain-agent/settings.json``. API keys live
in ``.langchain-agent/credentials.json`` (keyed by env var name). Override the
directory with the ``LANGCHAIN_AGENT_CONFIG_DIR`` env var.

Selection priority on startup (highest first):
    1. ``LANGCHAIN_AGENT_MODEL`` env var (provider name, optional ``/model`` suffix)
    2. ``model`` block in ``settings.json``
    3. Provider ``DEFAULT_PROVIDER`` with its first model
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI

import agent_paths

logger = logging.getLogger(__name__)


# ============================================================
#  ReasoningChatOpenAI: round-trip `reasoning_content` for thinking models
# ============================================================
class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI subclass that preserves the ``reasoning_content`` round-trip.

    A number of OpenAI-compatible providers (Xiaomi MiMo, DeepSeek reasoner
    series, Qwen thinking models, etc.) require the same multi-turn protocol:

    - The model emits chain-of-thought in ``reasoning_content`` alongside
      ``content`` / ``tool_calls`` on every assistant turn.
    - Every prior assistant message that had a ``reasoning_content`` MUST be
      echoed back verbatim on the next request. Omitting it triggers a 400
      ("The `reasoning_content` in the thinking mode must be passed back to
      the API.").

    Plain ``ChatOpenAI`` doesn't know about ``reasoning_content``: it neither
    captures it from the response nor sends it back. This subclass:

    - Captures ``reasoning_content`` from non-streamed responses and from
      streaming chunks into ``AIMessage.additional_kwargs``.
    - Injects ``reasoning_content`` into the outgoing payload for every
      assistant message that has one in ``additional_kwargs``.
    - Is a no-op for models that never emit ``reasoning_content`` (the field
      is only sent back when previously stored, so chat-only models like
      gpt-4o or deepseek-chat see no behavior change).

    All three hooks are robust to both dict-shaped and Pydantic-shaped chunks
    so the same code works across langchain-openai versions.
    """

    def _create_chat_result(self, response, generations):
        result = super()._create_chat_result(response, generations)
        try:
            for i, choice in enumerate(self._iter_choices(response)):
                if i >= len(result.generations):
                    break
                reasoning = self._get_attr(self._get_attr(choice, "message"), "reasoning_content") or ""
                if reasoning:
                    result.generations[i].message.additional_kwargs["reasoning_content"] = reasoning
        except Exception:  # pragma: no cover -- defensive
            logger.exception("ReasoningChatOpenAI: failed to capture reasoning_content from non-streamed response")
        return result

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if generation_chunk is None:
            return None
        reasoning = self._extract_reasoning_content_from_chunk(chunk)
        if reasoning:
            # AIMessageChunk.__add__ uses merge_dicts which concatenates string
            # values for duplicate keys, so per-chunk assignment accumulates
            # into the final AIMessage's additional_kwargs.
            generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        try:
            source_messages = self._coerce_to_messages(input_)
            payload_msgs = payload.get("messages") or []
            for src, dst in zip(source_messages, payload_msgs):
                if not isinstance(dst, dict) or dst.get("role") != "assistant":
                    continue
                extra = getattr(src, "additional_kwargs", None) or {}
                reasoning = extra.get("reasoning_content")
                if reasoning:
                    dst["reasoning_content"] = reasoning
        except Exception:  # pragma: no cover -- defensive
            logger.exception("ReasoningChatOpenAI: failed to inject reasoning_content into request payload")
        return payload

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _coerce_to_messages(input_):
        """Mirror what the base ``_get_request_payload`` does to derive messages,
        so our index alignment matches the payload exactly."""
        from langchain_core.prompt_values import PromptValue

        if isinstance(input_, PromptValue):
            return input_.to_messages()
        if isinstance(input_, list):
            return input_
        if isinstance(input_, str):
            return []
        try:
            return list(input_)
        except TypeError:
            return []

    @staticmethod
    def _extract_reasoning_content_from_chunk(chunk) -> str:
        """Return ``delta.reasoning_content`` for a streaming chunk.

        Handles both dict-shaped chunks (older / openai>=1 raw shape) and
        Pydantic ``ChatCompletionChunk`` objects (newer SDK shapes).
        """
        try:
            choices = ReasoningChatOpenAI._iter_choices(chunk)
            if not choices:
                return ""
            choice = choices[0]
            delta = ReasoningChatOpenAI._get_attr(choice, "delta")
            return ReasoningChatOpenAI._get_attr(delta, "reasoning_content") or ""
        except (AttributeError, KeyError, TypeError, IndexError):
            return ""

    @staticmethod
    def _iter_choices(obj):
        """Return ``obj.choices`` whether *obj* is a dict or a Pydantic object."""
        choices = ReasoningChatOpenAI._get_attr(obj, "choices")
        return list(choices) if choices else []

    @staticmethod
    def _get_attr(obj, name):
        """Look up *name* on *obj*, treating dicts and objects uniformly."""
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)


# ============================================================
#  Provider registry
# ============================================================
PROVIDERS: dict[str, dict[str, Any]] = {
    # ---------------- First-party model providers ----------------
    "anthropic": {
        "label": "Anthropic",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key_env": "ANTHROPIC_API_KEY",
        "models": [
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-opus-4-5-20251101",
            "claude-sonnet-4-5-20250929",
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-haiku-4-5-20251001",
        ],
    },
    "openai": {
        "label": "OpenAI",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "models": [
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5-mini",
            "gpt-5.3-codex",
            "gpt-5.2-codex",
            "gpt-4.1",
            "gpt-4o",
            "gpt-4o-mini",
        ],
    },
    "deepseek": {
        "label": "DeepSeek",
        "protocol": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "models": [
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-chat",
            "deepseek-reasoner",
        ],
    },
    "gemini": {
        "label": "Google AI Studio (Gemini)",
        "protocol": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GEMINI_API_KEY",
        "models": [
            "gemini-3.1-pro-preview",
            "gemini-3-pro-preview",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite-preview",
        ],
    },
    "xai": {
        "label": "xAI Grok",
        "protocol": "openai",
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "models": [
            "grok-4.20-0309-reasoning",
            "grok-4.20-0309-non-reasoning",
            "grok-4.20-multi-agent-0309",
            "grok-4.3",
        ],
    },
    "nvidia": {
        "label": "NVIDIA NIM",
        "protocol": "openai",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key_env": "NVIDIA_API_KEY",
        "models": [
            "nvidia/nemotron-3-super-120b-a12b",
            "nvidia/nemotron-3-nano-30b-a3b",
            "nvidia/llama-3.3-nemotron-super-49b-v1.5",
            "qwen/qwen3.5-397b-a17b",
            "deepseek-ai/deepseek-v3.2",
            "moonshotai/kimi-k2.6",
            "minimaxai/minimax-m2.5",
            "z-ai/glm5",
            "openai/gpt-oss-120b",
        ],
    },
    "xiaomi": {
        "label": "Xiaomi MiMo",
        "protocol": "openai",
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_key_env": "XIAOMI_API_KEY",
        "models": [
            "mimo-v2.5-pro",
            "mimo-v2.5",
            "mimo-v2-pro",
            "mimo-v2-omni",
            "mimo-v2-flash",
        ],
    },
    "zai": {
        "label": "Z.AI / GLM",
        "protocol": "openai",
        "base_url": "https://api.z.ai/api/paas/v4",
        "api_key_env": "GLM_API_KEY",
        "models": [
            "glm-5.1",
            "glm-5",
            "glm-5v-turbo",
            "glm-5-turbo",
            "glm-4.7",
            "glm-4.5",
            "glm-4.5-flash",
        ],
    },
    "kimi-coding": {
        "label": "Kimi / Moonshot",
        "protocol": "openai",
        "base_url": "https://api.moonshot.ai/v1",
        "api_key_env": "KIMI_API_KEY",
        "models": [
            "kimi-k2.6",
            "kimi-k2.5",
            "kimi-for-coding",
            "kimi-k2-thinking",
            "kimi-k2-thinking-turbo",
            "kimi-k2-turbo-preview",
            "kimi-k2-0905-preview",
        ],
    },
    "kimi-coding-cn": {
        "label": "Kimi / Moonshot (China)",
        "protocol": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "KIMI_CN_API_KEY",
        "models": [
            "kimi-k2.6",
            "kimi-k2.5",
            "kimi-k2-thinking",
            "kimi-k2-turbo-preview",
            "kimi-k2-0905-preview",
        ],
    },
    "stepfun": {
        "label": "StepFun Step Plan",
        "protocol": "openai",
        "base_url": "https://api.stepfun.ai/step_plan/v1",
        "api_key_env": "STEPFUN_API_KEY",
        "models": [
            "step-3.5-flash",
            "step-3.5-flash-2603",
        ],
    },
    "minimax": {
        "label": "MiniMax",
        "protocol": "anthropic",
        "base_url": "https://api.minimax.io/anthropic",
        "api_key_env": "MINIMAX_API_KEY",
        "models": [
            "MiniMax-M2.7",
            "MiniMax-M2.5",
            "MiniMax-M2.1",
            "MiniMax-M2",
        ],
    },
    "minimax-cn": {
        "label": "MiniMax (China)",
        "protocol": "anthropic",
        "base_url": "https://api.minimaxi.com/anthropic",
        "api_key_env": "MINIMAX_CN_API_KEY",
        "models": [
            "MiniMax-M2.7",
            "MiniMax-M2.5",
            "MiniMax-M2.1",
            "MiniMax-M2",
        ],
    },
    "alibaba": {
        "label": "Qwen Cloud (DashScope)",
        "protocol": "openai",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "models": [
            "qwen3.6-plus",
            "kimi-k2.5",
            "qwen3.5-plus",
            "qwen3-coder-plus",
            "qwen3-coder-next",
            "glm-5",
            "glm-4.7",
            "MiniMax-M2.5",
        ],
    },
    "alibaba-coding-plan": {
        "label": "Alibaba Cloud (Coding Plan)",
        "protocol": "openai",
        "base_url": "https://coding-intl.dashscope.aliyuncs.com/v1",
        "api_key_env": "ALIBABA_CODING_PLAN_API_KEY",
        "models": [
            "qwen3.6-plus",
            "qwen3.5-plus",
            "qwen3-coder-plus",
            "qwen3-coder-next",
            "kimi-k2.5",
            "glm-5",
            "glm-4.7",
            "MiniMax-M2.5",
        ],
    },
    "tencent-tokenhub": {
        "label": "Tencent TokenHub",
        "protocol": "openai",
        "base_url": "https://tokenhub.tencentmaas.com/v1",
        "api_key_env": "TOKENHUB_API_KEY",
        "models": ["hy3-preview"],
    },
    "arcee": {
        "label": "Arcee AI",
        "protocol": "openai",
        "base_url": "https://api.arcee.ai/api/v1",
        "api_key_env": "ARCEEAI_API_KEY",
        "models": [
            "trinity-large-thinking",
            "trinity-large-preview",
            "trinity-mini",
        ],
    },
    "gmi": {
        "label": "GMI Cloud",
        "protocol": "openai",
        "base_url": "https://api.gmi-serving.com/v1",
        "api_key_env": "GMI_API_KEY",
        "models": [
            "zai-org/GLM-5.1-FP8",
            "deepseek-ai/DeepSeek-V3.2",
            "moonshotai/Kimi-K2.5",
            "google/gemini-3.1-flash-lite-preview",
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.4",
        ],
    },
    "huggingface": {
        "label": "Hugging Face Router",
        "protocol": "openai",
        "base_url": "https://router.huggingface.co/v1",
        "api_key_env": "HF_TOKEN",
        "models": [
            "moonshotai/Kimi-K2.5",
            "Qwen/Qwen3.5-397B-A17B",
            "Qwen/Qwen3.5-35B-A3B",
            "deepseek-ai/DeepSeek-V3.2",
            "MiniMaxAI/MiniMax-M2.5",
            "zai-org/GLM-5",
            "XiaomiMiMo/MiMo-V2-Flash",
            "moonshotai/Kimi-K2-Thinking",
            "moonshotai/Kimi-K2.6",
        ],
    },

    # ---------------- Testing ----------------
    "mock": {
        "label": "Mock LLM (for testing)",
        "protocol": "mock",
        "base_url": "",
        "api_key_env": "MOCK_API_KEY",
        "models": ["mock-default", "mock-skill", "mock-tool"],
    },

    # ---------------- Aggregators ----------------
    "openrouter": {
        "label": "OpenRouter (aggregator)",
        "protocol": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "models": [
            "anthropic/claude-opus-4.7",
            "anthropic/claude-opus-4.6",
            "anthropic/claude-sonnet-4.6",
            "moonshotai/kimi-k2.6",
            "openrouter/pareto-code",
            "qwen/qwen3.6-plus",
            "anthropic/claude-haiku-4.5",
            "openai/gpt-5.5",
            "openai/gpt-5.5-pro",
            "openai/gpt-5.4-mini",
            "openai/gpt-5.3-codex",
            "xiaomi/mimo-v2.5-pro",
            "google/gemini-3.1-pro-preview",
            "google/gemini-3-flash-preview",
            "qwen/qwen3.6-35b-a3b",
            "stepfun/step-3.5-flash",
            "minimax/minimax-m2.7",
            "z-ai/glm-5.1",
            "x-ai/grok-4.3",
            "deepseek/deepseek-v4-pro",
        ],
    },
    "ai-gateway": {
        "label": "Vercel AI Gateway (aggregator)",
        "protocol": "openai",
        "base_url": "https://ai-gateway.vercel.sh/v1",
        "api_key_env": "AI_GATEWAY_API_KEY",
        "models": [
            "moonshotai/kimi-k2.6",
            "alibaba/qwen3.6-plus",
            "zai/glm-5.1",
            "minimax/minimax-m2.7",
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-opus-4.7",
            "anthropic/claude-haiku-4.5",
            "openai/gpt-5.4",
            "openai/gpt-5.4-mini",
            "openai/gpt-5.3-codex",
            "google/gemini-3.1-pro-preview",
            "google/gemini-3-flash",
            "xai/grok-4.20-reasoning",
        ],
    },
    "opencode-zen": {
        "label": "OpenCode Zen",
        "protocol": "openai",
        "base_url": "https://opencode.ai/zen/v1",
        "api_key_env": "OPENCODE_ZEN_API_KEY",
        "models": [
            "kimi-k2.5",
            "gpt-5.4-pro",
            "gpt-5.4",
            "gpt-5.3-codex",
            "gpt-5.2",
            "gpt-5.2-codex",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "gemini-3.1-pro",
            "gemini-3-flash",
            "minimax-m2.7",
            "glm-5",
            "kimi-k2-thinking",
            "qwen3-coder",
        ],
    },
    "opencode-go": {
        "label": "OpenCode Go",
        "protocol": "openai",
        "base_url": "https://opencode.ai/zen/go/v1",
        "api_key_env": "OPENCODE_GO_API_KEY",
        "models": [
            "kimi-k2.6",
            "kimi-k2.5",
            "glm-5.1",
            "glm-5",
            "mimo-v2.5-pro",
            "mimo-v2.5",
            "mimo-v2-pro",
            "minimax-m2.7",
            "qwen3.6-plus",
        ],
    },
    "kilocode": {
        "label": "Kilo Code",
        "protocol": "openai",
        "base_url": "https://api.kilo.ai/api/gateway",
        "api_key_env": "KILOCODE_API_KEY",
        "models": [
            "anthropic/claude-opus-4.6",
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.4",
            "google/gemini-3-pro-preview",
            "google/gemini-3-flash-preview",
        ],
    },

    # ---------------- Local / self-hosted ----------------
    "lmstudio": {
        "label": "LM Studio (local)",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key_env": "LM_API_KEY",
        "models": [],
    },
    "ollama-cloud": {
        "label": "Ollama Cloud",
        "protocol": "openai",
        "base_url": "https://ollama.com/v1",
        "api_key_env": "OLLAMA_API_KEY",
        "models": [],
    },

    # ---------------- Free-form ----------------
    "custom": {
        "label": "Custom OpenAI-compatible endpoint",
        "protocol": "openai",
        "base_url": "",
        "api_key_env": "CUSTOM_API_KEY",
        "models": [],
    },
}

DEFAULT_PROVIDER = "xiaomi"

DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 4096
DEFAULT_STREAMING = True



# ============================================================
#  ActiveConfig: a fully-resolved runtime selection
# ============================================================
@dataclass
class ActiveConfig:
    provider: str
    model: str
    base_url: str
    api_key_env: str
    protocol: str
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    streaming: bool = DEFAULT_STREAMING

    def to_settings_dict(self) -> dict[str, Any]:
        """Subset persisted to ``.claude/settings.json``."""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
        }


# ============================================================
#  Provider helpers
# ============================================================
def list_providers() -> list[str]:
    return list(PROVIDERS.keys())


def get_provider(name: str) -> dict[str, Any]:
    if name not in PROVIDERS:
        known = ", ".join(PROVIDERS.keys()) or "<none>"
        raise KeyError(f"Unknown provider: {name!r}. Known providers: {known}")
    return PROVIDERS[name]


def default_model_for(provider_name: str) -> str:
    """Return the first model for a provider, or empty string for custom."""
    provider = get_provider(provider_name)
    models = provider.get("models") or []
    return models[0] if models else ""


def make_config(
    provider: str,
    model: str = "",
    base_url: str = "",
    api_key_env: str = "",
) -> ActiveConfig:
    """Build an ActiveConfig from a provider name plus optional overrides.

    Fills missing fields from the provider's defaults.
    """
    prov = get_provider(provider)
    return ActiveConfig(
        provider=provider,
        model=model or default_model_for(provider),
        base_url=base_url or prov.get("base_url", ""),
        api_key_env=api_key_env or prov.get("api_key_env", ""),
        protocol=prov["protocol"],
    )


# ============================================================
#  settings.json -- active model selection
# ============================================================
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


# ============================================================
#  Credentials (API keys) -- credentials.json under agent_paths.config_dir()
# ============================================================
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


def settings_path() -> Path:
    return agent_paths.settings_path()


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
    cfg = cfg or load_active_config()
    if not get_api_key(cfg):
        raise RuntimeError(
            f"{cfg.api_key_env} is not set. Run /model (or /setup) to configure "
            f"a provider and API key, or export {cfg.api_key_env}."
        )


# ============================================================
#  LLM construction
# ============================================================
def build_llm(cfg: ActiveConfig | None = None):
    cfg = cfg or load_active_config()
    validate_api_key(cfg)

    common_kwargs = {
        "base_url": cfg.base_url,
        "api_key": get_api_key(cfg),
        "model": cfg.model,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "streaming": cfg.streaming,
    }

    if cfg.protocol == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(**common_kwargs)

    # All OpenAI-compatible endpoints get ReasoningChatOpenAI. It transparently
    # round-trips ``reasoning_content`` for thinking models (MiMo, DeepSeek
    # reasoner, Qwen-thinking, etc.) and is a no-op for models that never emit
    # the field (gpt-4o, deepseek-chat, etc.).
    return ReasoningChatOpenAI(**common_kwargs)
