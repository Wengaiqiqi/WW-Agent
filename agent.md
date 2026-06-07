# agent.md

Durable guidance for the W&W Agent CLI codebase. Read this before making
non-trivial changes — it captures the shape that isn't obvious from any
single file.

## Project shape

`cli.py` → `orchestrator/main.py` boots the orchestrator process, spawns
`tool-agent` + `skill-agent` subprocesses, and routes turns over MCP stdio +
A2A streaming. (The earlier in-process single-agent loop under `legacy/` and
its `--single` flag were removed — see "Removed".)

Key modules:

- `cli.py` — argparse + dispatch.
- `agent_paths.py` — config-dir resolver (`LANGCHAIN_AGENT_CONFIG_DIR` or
  `./.langchain-agent/`).
- `config/` (package) — provider registry, active-config persistence,
  credential hydration, `build_llm` factory.
- `prompt_rules.py` — the four style/behavior rules shared across all
  system prompts.
- `orchestrator/` — planner LLM, permission gate, A2A client, REPL TUI.
- `agents/shared/` — MCP/A2A server frameworks, JWT verifier, permission
  whitelists, telemetry sidecar, mock chat model.
- `agents/tool_agent/` — workspace + web ReAct specialist.
- `agents/skill_agent/` — SKILL.md JSON-envelope executor.
- `tool/` — tool implementations (`tool/tool_*.py`) are the one source of
  truth; the multi-agent `_wrap_*` surface in
  `agents/tool_agent/tool_executor.py` calls them. The `@tool`/`ALL_TOOLS`
  surface in `tool/tools.py` is now exercised only by tests (see "Removed").
- `skills/<slug>/SKILL.md` — domain workflow definitions + `_meta.json`.
- `tests/` — 77 test files; e2e under `tests/test_e2e_multi_agent/`
  marker `e2e`.

## Running

- Use the `LangChain` conda env when available:
  `D:\Anaconda3\envs\LangChain\python.exe cli.py`
- First launch runs the `/model` wizard. The API key is persisted under
  `.langchain-agent/credentials.json`; no env var required after that.
- Override the active model: `LANGCHAIN_AGENT_MODEL=<provider>` or
  `<provider>/<model>`.
- Override permission mode: `LANGCHAIN_AGENT_PERMISSION_MODE=read-only` |
  `workspace-write` | `danger-full-access`.
- Skill credentials: bundled `baidu-ecommerce-search` needs
  `BAIDU_EC_SEARCH_TOKEN` (declared in its `_meta.json` so it propagates
  through the secret filter).

## Verification

Quick syntax sanity:

```
python -m py_compile cli.py agent_paths.py project_context.py prompt_rules.py
python -m py_compile config/__init__.py config/_providers.py config/_settings.py config/_credentials.py config/_llm.py
python -m py_compile orchestrator/main.py orchestrator/repl_controller.py orchestrator/turns.py
python -m py_compile agents/shared/a2a_server.py agents/tool_agent/agent_loop.py agents/skill_agent/skill_executor.py
python -m py_compile tool/tools.py tool/tool_web.py tool/tool_shell.py
```

CLI smoke tests:

```
python cli.py prompt "/tools"
python cli.py prompt "/skills"
python cli.py prompt "/status"
python cli.py prompt "/config"
```

Test suite:

```
pip install -e '.[dev]'      # one-time: installs pytest, trustme, etc.
pytest -k "not e2e"         # fast: ~few seconds, no subprocess spawning
pytest                       # full: includes subprocess e2e tests
```

The `LangChain` conda env does NOT ship the `[dev]` deps. In particular the
comm-agent TLS tests import `trustme`; it's imported lazily via
`pytest.importorskip` so a missing `trustme` skips those tests instead of
aborting the whole collection — but you still want it installed for full
coverage (`pip install trustme` or `pip install -e '.[dev]'`).

## Working agreements

- **Tool source of truth lives in `tool/tool_*.py`.** The multi-agent
  `_wrap_*` surface in `agents/tool_agent/tool_executor.py` calls those
  underlying functions, so a bug fix there fixes the live path. The
  `@tool` wrappers in `tool/tools.py` call the same functions but are now
  only imported by tests.
- **Don't read `LANGCHAIN_AGENT_PERMISSION_MODE` from anything that runs
  inside a spawned agent subprocess.** The orchestrator's JWT grant is
  the authoritative gate via `agents.shared.authz.verify_grant`. The env
  var only governs `tool/tools.py`'s `@tool` decorator wrappers, which are
  imported only by tests, not by specialists.
- **CLI behavior stays generic.** Skill-specific behavior belongs in
  `skills/<name>/SKILL.md` (instructions) or `skills/<name>/scripts/`
  (helpers).
- **Avoid hardcoded secrets.** Read credentials from environment
  variables. The secret filter in `tool/tool_shell._filter_secrets_from_env`
  strips them at the subprocess boundary unless a skill declares the
  variable in `_meta.json::requiresEnv`.
- **Tool default timeouts** come from
  `tool.tool_shell.DEFAULT_SUBPROCESS_TIMEOUT` (180s). Don't redefine
  per-surface — both `tool/tools.py` and
  `agents/tool_agent/tool_executor.py` import the constant.
- **SSRF guard** for any new `urllib.request` / `httpx` fetcher: import
  `tool.tool_web.hostname_is_safe` and check before opening the socket;
  use `tool.tool_web.OPENER` if redirects are followed. Set
  `LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS=1` to opt out during local dev.
- **Prefer small, focused changes.** Run the relevant test file after
  each edit; full `pytest` before pushing.

## Removed

- **Single-agent mode (`--single`, `legacy/`).** Deleted in favour of the
  multi-agent orchestrator: the `legacy/single_agent_loop.py` loop, the
  `--single`/`--output-format` CLI args, and the `test_e2e_legacy_mode.py`
  e2e check are gone. `orchestrator/picker.py` and `orchestrator/ui_input.py`
  hold the input/picker UX that was originally extracted from that loop.
- **Follow-up — `tool/tools.py`.** Its `@tool`/`ALL_TOOLS` surface lost its
  only production consumer (the legacy loop) and is now imported solely by
  tests. The underlying `tool/tool_*.py` implementations remain live via
  `agents/tool_agent/tool_executor.py`. Decide whether to keep `tool/tools.py`
  as a tested-but-unwired surface or retire it and point those tests at the
  underlying functions.

## Design docs

- `docs/superpowers/specs/2026-05-15-multi-agent-orchestration-design.md`
  — protocol message shapes, capability negotiation, permission model.
- `docs/superpowers/specs/2026-05-15-multi-agent-repl-design.md`
  — REPL UX, slash commands, streaming UI invariants.
- `docs/superpowers/plans/` — implementation plans by phase.
