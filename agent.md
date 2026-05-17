# agent.md

Durable guidance for the W&W Agent CLI codebase. Read this before making
non-trivial changes — it captures the shape that isn't obvious from any
single file.

## Project shape

Two runtime modes share one codebase:

- **Multi-agent (default)** — `cli.py` → `orchestrator/main.py` boots the
  orchestrator process, spawns `tool-agent` + `skill-agent` subprocesses,
  routes turns over MCP stdio + A2A streaming.
- **Single-agent (`--single`)** — `cli.py` → `legacy/single_agent_loop.py`
  in-process LangGraph ReAct loop.

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
- `tool/` — tool implementations (one source of truth — both legacy and
  multi-agent surface the same underlying functions).
- `skills/<slug>/SKILL.md` — domain workflow definitions + `_meta.json`.
- `tests/` — 45 test files; e2e under `tests/test_e2e_multi_agent/`
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
python cli.py --single prompt "/tools"
```

Test suite:

```
pytest -k "not e2e"         # fast: ~few seconds, no subprocess spawning
pytest                       # full: includes subprocess e2e tests
```

## Working agreements

- **Tool source of truth lives in `tool/`.** Both surfaces (legacy `@tool`
  in `tool/tools.py` and multi-agent `_wrap_*` in
  `agents/tool_agent/tool_executor.py`) call the same underlying
  functions. A bug fix in the underlying tool fixes both paths.
- **Don't read `LANGCHAIN_AGENT_PERMISSION_MODE` from anything that runs
  inside a spawned agent subprocess.** The orchestrator's JWT grant is
  the authoritative gate via `agents.shared.authz.verify_grant`. The env
  var only governs `tool/tools.py`'s `@tool` decorator wrappers, which
  are imported by the legacy single-agent process, not by specialists.
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
  use `tool.tool_web._OPENER` if redirects are followed. Set
  `LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS=1` to opt out during local dev.
- **Prefer small, focused changes.** Run the relevant test file after
  each edit; full `pytest` before pushing.

## Design docs

- `docs/superpowers/specs/2026-05-15-multi-agent-orchestration-design.md`
  — protocol message shapes, capability negotiation, permission model.
- `docs/superpowers/specs/2026-05-15-multi-agent-repl-design.md`
  — REPL UX, slash commands, streaming UI invariants.
- `docs/superpowers/plans/` — implementation plans by phase.
