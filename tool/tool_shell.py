from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass


@dataclass
class CommandOutput:
    stdout: str
    stderr: str
    exitCode: int | None
    interrupted: bool
    noOutputExpected: bool
    returnCodeInterpretation: str | None


def json_result(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# Env-var name patterns that almost certainly carry secrets. We strip these
# before handing the environment to a tool-agent shell/python child, so a
# prompt-injected `set | findstr KEY` (Windows) or `env | grep -i key` (POSIX)
# returns nothing useful even when the agent's own process has the credentials
# in env (it needs them for its own LLM API calls).
#
# The single broad pattern matches any name containing one of the sensitive
# keywords as a token-boundary-delimited word — covers OPENAI_API_KEY,
# AUTHZ_HMAC_KEY, MY_TOKEN, FOO_PASSWORD, AWS_ACCESS_KEY_ID, etc. The narrower
# provider-name pattern catches per-provider conventions like XIAOMI_AK that
# don't include a sensitive keyword in the name.
_SECRET_KEYWORD_RE = re.compile(
    r"(?i)(?:^|_)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|CREDENTIALS|"
    r"AUTH|HMAC|PRIVATE|API|SESSION|COOKIE|BEARER|CERT)(?:_|$)"
)
_PROVIDER_PREFIX_RE = re.compile(
    r"(?i)^(openai|anthropic|deepseek|xiaomi|qwen|moonshot|zhipu|mistral|"
    r"cohere|google|gemini|ollama|together|replicate|huggingface|aws|azure|"
    r"gcp|github|tavily|brave|serpapi)_"
)
_LANGCHAIN_ALLOWLIST = {
    "LANGCHAIN_AGENT_MODEL",
    "LANGCHAIN_AGENT_CONFIG_DIR",
    "LANGCHAIN_AGENT_PERMISSION_MODE",
    "LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS",
}


# Default subprocess timeouts (seconds). Shared by run_python / run_command at
# both the LangChain ``@tool`` surface (``tool/tools.py``) and the tool-agent
# wrapper surface (``agents/tool_agent/tool_executor.py``) so callers see
# consistent defaults. 180s matches the slow-path realities the agent hits
# routinely: ``pip install <pkg>`` over a flaky network, ``python-docx``
# opening a large .docx, ``pypdf`` on a 200-page PDF.
DEFAULT_SUBPROCESS_TIMEOUT = 180


def _skill_declared_env_keys() -> set[str]:
    """Return the union of env-var names declared by every installed skill.

    Skills opt their required credentials INTO the child-process env via a
    ``requiresEnv`` array in their ``_meta.json``. Without this, the secret
    filter below would strip e.g. ``BAIDU_EC_SEARCH_TOKEN`` (matches
    ``_TOKEN`` in the keyword regex) on its way to the skill's own bundled
    script, breaking the skill entirely.

    The lookup is wrapped in try/except so a missing or broken
    ``skills/`` directory never causes ``run_command`` to fail — we just
    fall back to "no opt-ins" and behave like before.
    """
    try:
        from skills.skill_loader import collect_skill_env_keys
        return collect_skill_env_keys()
    except Exception:
        return set()


def _filter_secrets_from_env(env: dict[str, str]) -> dict[str, str]:
    """Strip secret-looking entries from a child process env dict.

    The agent's own process retains its credentials (it needs them to call the
    LLM API). When it shells out via run_command / run_python, we wipe anything
    that pattern-matches an API key or auth token from the env handed to the
    child, so a prompt-injected attacker can't dump credentials by running
    ``set`` or ``env``.

    Anything starting with ``LANGCHAIN_AGENT_`` is treated as secret unless it
    appears in ``_LANGCHAIN_ALLOWLIST`` — these few config knobs are explicitly
    safe to propagate. AUTHZ_HMAC_KEY (the orchestrator → agent grant signing
    key) is one of the most important things to strip, and ``HMAC`` /
    ``KEY`` in the keyword set catches it.

    **Skill opt-in**: each skill's ``_meta.json`` can declare a
    ``requiresEnv`` list (e.g. ``["BAIDU_EC_SEARCH_TOKEN"]``). Those names
    bypass the keyword/prefix filters so the skill's bundled scripts actually
    see their tokens. The whitelist principle still holds — only explicitly
    declared keys leak, never every env var whose name happens to contain
    ``TOKEN``.
    """
    allowlist = _skill_declared_env_keys()
    cleaned: dict[str, str] = {}
    for name, value in env.items():
        upper = name.upper()
        if upper.startswith("LANGCHAIN_AGENT_"):
            if upper in _LANGCHAIN_ALLOWLIST:
                cleaned[name] = value
            continue
        if name in allowlist:
            cleaned[name] = value
            continue
        if _SECRET_KEYWORD_RE.search(name) or _PROVIDER_PREFIX_RE.search(name):
            continue
        cleaned[name] = value
    return cleaned


def run_subprocess(command: list[str] | str, timeout: int = DEFAULT_SUBPROCESS_TIMEOUT, shell: bool = False) -> str:
    # Force UTF-8 end-to-end so a child python `print(...)` on Chinese-Windows
    # doesn't deadlock writing a traceback through cp936 when the output
    # contains chars GBK can't encode (e.g. `²`, `—`, fullwidth punctuation).
    # Both the child's stdio AND our decoder must agree on UTF-8.
    child_env = _filter_secrets_from_env(os.environ.copy())
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.getcwd(),
        shell=shell,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        output = CommandOutput(
            stdout=stdout or "",
            stderr=f"Command exceeded timeout of {timeout} seconds",
            exitCode=None,
            interrupted=True,
            noOutputExpected=True,
            returnCodeInterpretation="timeout",
        )
        return json_result(asdict(output))
    output = CommandOutput(
        stdout=stdout,
        stderr=stderr,
        exitCode=proc.returncode,
        interrupted=False,
        noOutputExpected=not stdout.strip() and not stderr.strip(),
        returnCodeInterpretation=None if proc.returncode == 0 else f"exit_code:{proc.returncode}",
    )
    return json_result(asdict(output))


def run_python_code(code: str, timeout: int = DEFAULT_SUBPROCESS_TIMEOUT) -> str:
    return run_subprocess([sys.executable, "-c", code], timeout=timeout, shell=False)


def run_shell_command(command: str, timeout: int = DEFAULT_SUBPROCESS_TIMEOUT) -> str:
    if os.name == "nt":
        # shell=True so Windows passes the raw command line to cmd.exe.
        # Passing ["cmd.exe", "/c", command] with shell=False routes through
        # subprocess.list2cmdline, which escapes any internal `"` as `\"` —
        # that breaks commands like `"C:\Path With Spaces\python.exe" script.py`
        # because cmd.exe then tries to invoke the literal `\"C:\\…\\"` token.
        return run_subprocess(command, timeout=timeout, shell=True)
    shell_command = ["/bin/sh", "-lc", command]
    return run_subprocess(shell_command, timeout=timeout, shell=False)
