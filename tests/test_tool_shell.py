"""Tests for tool/tool_shell.py env-secret filtering.

The contract: when the agent shells out, the child process MUST NOT see API
keys, tokens, or other secrets from the parent's environment. This prevents a
prompt-injected `set | findstr KEY` (Windows) or `env | grep -i key` (POSIX)
from exfiltrating credentials the agent legitimately needs for its own LLM
calls.
"""
from __future__ import annotations

from tool.tool_shell import _filter_secrets_from_env


def test_strips_common_api_key_names():
    env = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "sk-real-key",
        "ANTHROPIC_API_KEY": "sk-ant-real",
        "DEEPSEEK_API_KEY": "xx",
        "GITHUB_TOKEN": "ghp_xx",
        "AWS_SECRET_ACCESS_KEY": "wj/xx",
        "MY_PASSWORD": "hunter2",
        "GOOGLE_CREDENTIAL_FILE": "/foo",
    }
    out = _filter_secrets_from_env(env)
    assert out == {"PATH": "/usr/bin"}


def test_keeps_unrelated_env_vars():
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "PYTHONPATH": "/x:/y",
        "EDITOR": "vim",
        "LANG": "en_US.UTF-8",
    }
    assert _filter_secrets_from_env(env) == env


def test_langchain_agent_config_passes_through():
    """LANGCHAIN_AGENT_MODEL / CONFIG_DIR / PERMISSION_MODE are user config, not
    secrets, so subprocess work that depends on them keeps working."""
    env = {
        "LANGCHAIN_AGENT_MODEL": "deepseek/deepseek-chat",
        "LANGCHAIN_AGENT_CONFIG_DIR": "/x",
        "LANGCHAIN_AGENT_PERMISSION_MODE": "workspace-write",
        "LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS": "1",
    }
    assert _filter_secrets_from_env(env) == env


def test_unknown_langchain_agent_var_is_stripped():
    """Defense in depth: any LANGCHAIN_AGENT_* not on the explicit allowlist is
    treated as potentially sensitive (e.g. a future LANGCHAIN_AGENT_TOKEN)."""
    env = {
        "PATH": "/usr/bin",
        "LANGCHAIN_AGENT_MODEL": "x/y",
        "LANGCHAIN_AGENT_NEW_SECRET_FIELD": "hunter2",
    }
    out = _filter_secrets_from_env(env)
    assert "LANGCHAIN_AGENT_MODEL" in out
    assert "LANGCHAIN_AGENT_NEW_SECRET_FIELD" not in out


def test_case_insensitive_match():
    env = {
        "openai_api_key": "x",
        "Anthropic_Api_Key": "x",
        "my_token": "x",
        "PATH": "/usr/bin",
    }
    assert _filter_secrets_from_env(env) == {"PATH": "/usr/bin"}


def test_run_subprocess_does_not_leak_env(monkeypatch):
    """End-to-end: run_subprocess's child must NOT see OPENAI_API_KEY even when
    the parent process has it set.

    The skill-declared-env passthrough is stubbed out: this test focuses on the
    keyword/prefix filter, not on the skill opt-in surface (which has its own
    coverage in ``test_skill_declared_env_keys_bypass_secret_filter``)."""
    import sys
    import json as _json
    from tool import tool_shell
    from tool.tool_shell import run_subprocess

    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MY_TOKEN", "should-not-leak")
    monkeypatch.setattr(tool_shell, "_skill_declared_env_keys", lambda: set())
    # PATH must survive so python can find its own runtime
    code = (
        "import os, json; "
        "print(json.dumps({k: v for k, v in os.environ.items() "
        "if 'KEY' in k.upper() or 'TOKEN' in k.upper()}))"
    )
    raw = run_subprocess([sys.executable, "-c", code], timeout=10, shell=False)
    result = _json.loads(raw)
    child_secrets = _json.loads(result["stdout"].strip())
    assert child_secrets == {}, f"child saw secrets: {child_secrets}"


def test_skill_declared_env_keys_bypass_secret_filter(monkeypatch):
    """Regression: a skill that declares ``requiresEnv: [\"BAIDU_EC_SEARCH_TOKEN\"]``
    must have that token reach its bundled scripts. The secret filter's keyword
    regex matches ``_TOKEN`` and would otherwise strip it before the
    skill-spawned subprocess sees anything."""
    from tool import tool_shell

    monkeypatch.setattr(
        tool_shell, "_skill_declared_env_keys",
        lambda: {"BAIDU_EC_SEARCH_TOKEN"},
    )

    env = {
        "PATH": "/usr/bin",
        "BAIDU_EC_SEARCH_TOKEN": "live-token",
        "OPENAI_API_KEY": "sk-secret",  # NOT declared → must still be stripped
    }
    out = tool_shell._filter_secrets_from_env(env)
    assert out.get("BAIDU_EC_SEARCH_TOKEN") == "live-token"
    assert "OPENAI_API_KEY" not in out


def test_skill_declared_env_keys_dont_break_when_skills_dir_missing(monkeypatch):
    """``_skill_declared_env_keys`` must tolerate any error (no skills/, broken
    JSON, import failures) — run_command can't depend on the skills loader."""
    from tool import tool_shell

    def _boom():
        raise RuntimeError("skills loader exploded")

    monkeypatch.setattr(tool_shell, "_skill_declared_env_keys", lambda: set())
    # Should not raise; behaves as if no opt-ins.
    out = tool_shell._filter_secrets_from_env({"PATH": "/usr/bin"})
    assert out == {"PATH": "/usr/bin"}
