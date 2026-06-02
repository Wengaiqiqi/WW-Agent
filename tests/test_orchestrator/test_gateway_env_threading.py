from __future__ import annotations


def test_build_orchestrator_llm_uses_explicit_cfg(monkeypatch):
    """An explicit cfg is forwarded to build_llm without consulting the
    process-global load_active_config() (so a per-turn cfg can't be clobbered)."""
    import config
    import orchestrator.main as m

    seen = {}
    monkeypatch.setattr(config, "hydrate_env_from_credentials", lambda: None)

    def _fake_build_llm(cfg):
        seen["cfg"] = cfg
        return "LLM"

    monkeypatch.setattr(config, "build_llm", _fake_build_llm)

    def _boom():
        raise AssertionError("load_active_config must not be called when cfg is given")

    monkeypatch.setattr(config, "load_active_config", _boom)

    sentinel = object()
    out = m._build_orchestrator_llm(sentinel)
    assert out == "LLM"
    assert seen["cfg"] is sentinel


def test_build_orchestrator_llm_falls_back_to_load_active_config(monkeypatch):
    import config
    import orchestrator.main as m

    monkeypatch.setattr(config, "hydrate_env_from_credentials", lambda: None)
    marker = object()
    monkeypatch.setattr(config, "load_active_config", lambda: marker)
    monkeypatch.setattr(config, "build_llm", lambda cfg: cfg)

    assert m._build_orchestrator_llm() is marker  # legacy path unchanged


def test_snapshot_for_system_prompt_accepts_explicit_user(monkeypatch, tmp_path):
    monkeypatch.setenv("LANGCHAIN_AGENT_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("LANGCHAIN_AGENT_MEMORY_USER", raising=False)
    from tool import tool_memory

    # Explicit user does not require the env var, and scopes to that user's dir.
    out = tool_memory.snapshot_for_system_prompt(user="alice")
    assert isinstance(out, str)          # no crash, no env needed
    assert tool_memory._user_scope_dir(user="alice") is not None
    assert tool_memory._user_scope_dir(user="") is None  # explicit empty = global
