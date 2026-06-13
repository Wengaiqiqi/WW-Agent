"""Provider registry + ActiveConfig dataclass.

The bottom layer of the ``config`` package: pure data and pure functions, no
I/O, no LLM SDK imports. Both ``_settings`` and ``_credentials`` build on this
without circular-import risk.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# The default used when no env var is set AND no settings.json exists yet —
# i.e. a fresh install where the user hasn't run the ``/model`` wizard. Set
# to ``xiaomi`` because the original developer's primary endpoint is MiMo;
# the wizard immediately re-prompts at first launch so this default is only
# ever transient. Changing it is fine — only affects the first-run state.
DEFAULT_PROVIDER = "xiaomi"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 4096
DEFAULT_STREAMING = True


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
    # Token-plan (Anthropic-compatible) endpoint. Same MiMo family, request
    # format is Anthropic /v1/messages -- billed through token-plan-cn host.
    # Co-exists with ``xiaomi`` so the user picks whichever endpoint their
    # key is provisioned against, without juggling base_url + protocol by
    # hand in settings.json.
    "xiaomi-anthropic": {
        "label": "Xiaomi MiMo",
        "protocol": "anthropic",
        "base_url": "https://token-plan-cn.xiaomimimo.com/anthropic",
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


@dataclass
class ActiveConfig:
    """A fully-resolved runtime selection: which provider + model + URL +
    credential to talk to, plus the runtime knobs the LLM SDK needs."""
    provider: str
    model: str
    base_url: str
    api_key_env: str
    protocol: str
    # Resolved literal API key for THIS turn (web custom-endpoint flow sets it
    # via TurnContext). Empty = fall back to api_key_env / credentials file.
    api_key: str = ""
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    streaming: bool = DEFAULT_STREAMING

    def to_settings_dict(self) -> dict[str, Any]:
        """Subset persisted to the project's settings.json (default
        ``.langchain-agent/settings.json``; see ``agent_paths.settings_path``)."""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
        }


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
