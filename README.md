# W&W Agent CLI

A terminal coding/research assistant built on LangChain + LangGraph. Two
runtime shapes share one codebase:

- **Multi-agent** (default) — the orchestrator process routes work to
  specialist subprocesses (`tool-agent`, `skill-agent`) over MCP stdio and
  HTTP A2A streaming.
- **Single-agent** (`--single`) — the original in-process LangGraph ReAct
  loop, retained for fast non-interactive prompts and constrained
  environments.

## Install

```bash
pip install -r requirements.txt
```

First launch runs the `/model` wizard (provider → model → API key → base
URL). The key is saved to `.langchain-agent/credentials.json` (a sibling
`.gitignore` is created automatically); subsequent launches hydrate `os.environ`
from there.

To preconfigure non-interactively:

```bash
export LANGCHAIN_AGENT_MODEL=deepseek/deepseek-chat   # provider or provider/model
export DEEPSEEK_API_KEY=...
```

## Run

```bash
python cli.py                              # interactive multi-agent REPL
python cli.py prompt "what's in README?"   # one-shot multi-agent
python cli.py --single                     # legacy single-agent REPL
python cli.py --single prompt "..."        # one-shot single-agent
```

## Multi-agent mode (default)

The orchestrator process boots, spawns two specialists, and routes each
turn to the right one:

- **orchestrator** — planner LLM (a small router prompt); permission
  gate; A2A streaming dispatch; TUI rendering with `[orchestrator]` /
  `[skill]` / `[tool]` line tags.
- **tool-agent** — workspace + web specialist. ReAct loop over file
  ops, shell, Python, web fetch.
- **skill-agent** — runs a single `skills/<slug>/SKILL.md` as system
  prompt and uses a JSON-envelope protocol to call tools on tool-agent
  via A2A.

Inter-process trust:

- All cross-process calls carry a 60-second HS256 JWT (`AUTHZ_HMAC_KEY`
  is minted by the orchestrator at startup and passed only to spawned
  specialists). `agents.shared.authz.verify_grant` rejects expired,
  tampered, or wrong-tool grants.
- The orchestrator's `PermissionGate` is the *outer* whitelist. A skill
  that wants a tool calls back through `_mint_tool_grant`, gated by the
  *inner* whitelist (`_SKILL_INNER_WHITELIST`). The two-tier design lets
  workspace-write users invoke skills without auto-elevating their
  planner-level permissions.

Runtime files (created lazily under `.agent/`):

- `.agent/agents/<id>.card.json` — Agent Card per specialist
- `.agent/runtime/peers.json` — `{agent_id: a2a_url}` table written at boot
- `.agent/runtime/<id>.a2a-url` — per-specialist URL sidecar
- `.agent/runtime/telemetry.ndjson` — A2A telemetry tailed by orchestrator
- `.agent/logs/` — specialist logs

`.agent/` is separate from `.claude/` (reserved for Claude Code, an
unrelated dev tool).

### Design docs

Full architecture: [docs/superpowers/specs/2026-05-15-multi-agent-orchestration-design.md](docs/superpowers/specs/2026-05-15-multi-agent-orchestration-design.md).

## Slash commands

| Command | What it does |
|---|---|
| `/help` | Show available commands |
| `/status` | Session counters (turns, tool calls, last error) |
| `/model` | 4-step model wizard (provider → model → key → URL) |
| `/permissions [mode]` | Show or set permission mode |
| `/agents` | List specialists with PIDs and A2A URLs (multi-agent only) |
| `/tools` | List registered capabilities |
| `/skills` | List installed local skills |
| `/instructions` | List loaded project instruction files |
| `/config` | Effective session config |
| `/compact` | Start a fresh memory thread |
| `/clear` | Clear the terminal |
| `/exit` / `/quit` | Exit |

## Permission modes

Three tiers, default `workspace-write`:

| Mode | Allowed |
|---|---|
| `read-only` | Read/search file tools, `web_search` / `web_extract` / `web_crawl`, `calculator`, `current_datetime`, `tool_manifest`, `config`, `clarify`. **Skills are blocked.** |
| `workspace-write` | All of the above plus `write_file`, `edit_file`, `apply_patch`, `memory`, `todo_write`. Skills can execute and (via their inner whitelist) reach `run_command` / `run_python`. |
| `danger-full-access` | Everything, including direct `run_python` / `run_command` dispatch from the planner. |

Set via:

```bash
export LANGCHAIN_AGENT_PERMISSION_MODE=read-only
```

or inside the REPL: `/permissions read-only`.

## Tools

Run `/tools` for the live list. Highlights:

| Tool | Permission | What it does |
|---|---|---|
| `read_file` / `write_file` / `edit_file` | read / write / write | UTF-8 file ops with workspace-boundary checks |
| `apply_patch` | write | V4A unified-diff patches across multiple files (atomic — validates all hunks before writing) |
| `glob_search` / `grep_search` / `list_directory` | read | Filesystem discovery (ripgrep-style grep) |
| `run_python` | danger | Python in a subprocess. 180s default timeout |
| `run_command` | danger | Shell command. 180s default timeout |
| `web_search` | read | DuckDuckGo (no key) or Tavily (`TAVILY_API_KEY`) |
| `web_extract` | read | Fetch URL + readable text. SSRF-blocked on private/loopback addresses (including post-redirect) |
| `web_crawl` | read | Same-host BFS (≤25 pages). No JS rendering |
| `memory` | write | Cross-session `MEMORY.md` / `USER.md` under `.langchain-agent/memories/` |
| `todo_write` | write | Structured task list under `.langchain-agent/todos.json` |
| `clarify` | read | Agent-initiated question to user (multi-agent: reverse A2A bridge from tool-agent; legacy: direct UI callback) |
| `osv_check` | read | OSV malware/CVE lookup for `(package, ecosystem)` |
| `home_assistant` | danger | Home Assistant REST API (`HASS_TOKEN` required). Blocks `shell_command` / `python_script` / `command_line` HA domains |
| `x_search` | read | xAI's hosted X (Twitter) search (`XAI_API_KEY` required) |
| `vision_analyze` | read | Image + prompt → vision-capable LLM (SSRF-checked for remote URLs) |
| `mixture_of_agents` | read | Parallel reference models + aggregator (paper: arXiv:2406.04692) |
| `calculator` / `current_datetime` / `sleep` / `config` / `tool_manifest` | various | Misc utilities |

`clarify` works in both modes. In multi-agent mode, tool-agent's ReAct
loop emits a `clarify_request` event over its SSE stream; the orchestrator
pauses, prompts you (multi-choice or free-text), and POSTs the answer
back to tool-agent via the `/a2a` endpoint with `skill_id=_clarify_response`.
The bridge lives in `agents/tool_agent/clarify_bridge.py`.

### V4A patch format

```
*** Begin Patch
*** Update File: src/foo.py
@@ optional context hint @@
 unchanged line
-old line
+new line
*** Add File: NEW.md
+content line 1
*** Delete File: obsolete.txt
*** Move File: src/old.py -> src/new.py
*** End Patch
```

## Skills

Each directory under `skills/<slug>/` containing a `SKILL.md` is a skill.
Optional companion files:

- `_meta.json` — `{slug, version, matchKeywords[], requiresEnv[]}`
- `scripts/` — Python helpers invoked by the skill

`matchKeywords` drives single-agent's auto-injection of the SKILL.md
into the system prompt. In multi-agent, the planner LLM picks
`skill.<slug>` directly based on the skill's `description` (from SKILL.md
frontmatter).

`requiresEnv` lets a skill opt specific env-var names INTO the
subprocess environment despite the secret filter:

```json
{
  "slug": "baidu-ecommerce-search",
  "matchKeywords": ["全网", "比价", "京东"],
  "requiresEnv": ["BAIDU_EC_SEARCH_TOKEN", "BAIDU_EC_SEARCH_QPS"]
}
```

Bundled skills:

- `baidu-ecommerce-search` — China e-commerce search/comparison (needs
  `BAIDU_EC_SEARCH_TOKEN`).
- `ppt-master` — slide-deck generation (templates, asset palettes,
  multi-backend image generation).

## Memory layout

```
.langchain-agent/memories/
├── MEMORY.md   # agent notes: conventions, environment, prior failures
└── USER.md     # user profile: preferences, goals
```

Entries are separated by `\n§\n` and capped per file (MEMORY 4 KB, USER 2 KB).
Mid-session `memory(action="add"|"replace"|"remove")` writes immediately;
the in-prompt snapshot is frozen at session start to keep prefix caches
stable.

## Logging

Default file log: `.langchain-agent/agent.log`. Mirror to stderr:

```bash
export LANGCHAIN_AGENT_DEBUG=1
```

## Provider catalog

`config/_providers.py` registers each provider's protocol (OpenAI-chat or
Anthropic), default base URL, API-key env var, and known model ids.
First-party: `anthropic`, `openai`, `deepseek`, `gemini`, `xai`, `nvidia`,
`xiaomi`, `zai`, `kimi-coding`, `kimi-coding-cn`, `stepfun`, `minimax`,
`minimax-cn`, `alibaba`, `alibaba-coding-plan`, `tencent-tokenhub`,
`arcee`, `gmi`, `huggingface`. Aggregators: `openrouter`, `ai-gateway`,
`opencode-zen`, `opencode-go`, `kilocode`. Local: `lmstudio`,
`ollama-cloud`. Free-form: `custom` (prompts for model id in step 2 of
the wizard).

Selection priority on startup:

1. `LANGCHAIN_AGENT_MODEL` env var (`provider` or `provider/model`)
2. `.langchain-agent/settings.json` `model` block (written by the wizard)
3. `DEFAULT_PROVIDER` with its first model

## Development

```bash
pytest                                   # full suite (~45 test files)
pytest -k "not e2e"                      # skip subprocess-spawning e2e tests
pytest --cov=. --cov-report=html         # coverage report
```

Lint / format:

```bash
black .
flake8 .
```

## Project structure

```
.
├── cli.py                       # entrypoint (dispatches --single vs multi-agent)
├── agent_paths.py               # config-dir resolver
├── project_context.py           # discover project instruction files (agent.md, etc.)
├── prompt_rules.py              # shared style/behavior rules
│
├── config/                      # provider registry + settings + credentials + LLM factory
│   ├── _providers.py
│   ├── _settings.py
│   ├── _credentials.py
│   └── _llm.py
│
├── orchestrator/                # multi-agent orchestrator process
│   ├── main.py                  # entrypoint (run_prompt / run_repl)
│   ├── repl_controller.py       # turn execution + A2A streaming
│   ├── repl_commands.py         # slash-command handlers
│   ├── repl_state.py            # session state + planner-context budget
│   ├── repl_ui.py               # Rich-based TUI
│   ├── turns.py                 # LLMPlanner + TurnRunner
│   ├── graph.py                 # legacy LangGraph dispatch (plan → tool call)
│   ├── mcp_host.py              # spawn + JWT-gated MCP client sessions
│   ├── permission_gate.py       # outer authz: sign per-tool JWT grants
│   ├── a2a_client.py            # outbound SSE streaming + RPC
│   ├── router.py                # CapabilityRouter
│   ├── stream_mux.py            # tagged terminal stream
│   ├── telemetry.py             # tail .agent/runtime/telemetry.ndjson
│   ├── registry.py              # load Agent Cards
│   └── ui_input.py              # boxed prompt-toolkit input
│
├── agents/                      # specialist subprocesses
│   ├── shared/
│   │   ├── mcp_server.py        # ToolSpec → MCP server runner
│   │   ├── a2a_server.py        # FastAPI A2A endpoint (RPC + SSE)
│   │   ├── authz.py             # verify_grant
│   │   ├── permission_modes.py  # outer + inner whitelists
│   │   ├── telemetry.py         # emit_event into telemetry.ndjson
│   │   └── mock_chat_model.py   # scripted LLM for tests
│   ├── tool_agent/
│   │   ├── main.py              # entrypoint
│   │   ├── agent_loop.py        # ReAct loop with stream-dedup logic
│   │   └── tool_executor.py     # _TOOL_MAP (wrapped) + JWT-gated execute_tool
│   └── skill_agent/
│       ├── main.py              # entrypoint (MCP + A2A stream surfaces)
│       ├── skill_executor.py    # SKILL.md ReAct + JSON envelope protocol
│       └── a2a_client.py        # outbound A2A to tool-agent (call_peer)
│
├── tool/                        # tool implementations (single source of truth)
│   ├── tools.py                 # @tool LangChain surface (legacy single-agent)
│   ├── tool_registry.py         # TOOL_SPECS + required_permission_for
│   ├── tool_permissions.py      # PermissionMode + authorize_tool
│   ├── tool_file_ops.py
│   ├── tool_patch.py            # V4A apply_patch
│   ├── tool_shell.py            # run_python / run_command + secret filter
│   ├── tool_web.py              # web_search / web_extract / web_crawl + SSRF
│   ├── tool_memory.py
│   ├── tool_clarify.py
│   ├── tool_osv.py
│   ├── tool_homeassistant.py
│   ├── tool_x_search.py
│   ├── tool_vision.py
│   └── tool_moa.py
│
├── skills/
│   ├── skill_loader.py
│   ├── baidu-ecommerce-search/
│   └── ppt-master/
│
├── legacy/
│   └── single_agent_loop.py     # original single-process REPL (--single)
│
└── tests/                       # 45 test files; e2e under tests/test_e2e_multi_agent/
```

## Security notes

- **SSRF**: `web_extract` / `web_crawl` resolve every A/AAAA record of the
  target host and refuse private / loopback / link-local / multicast /
  reserved addresses. `SafeRedirectHandler` re-validates on each 30x.
  `vision_analyze` uses the same opener. Opt out for local dev with
  `LANGCHAIN_AGENT_ALLOW_PRIVATE_URLS=1`.
- **Secret env stripping**: subprocesses spawned by `run_python` /
  `run_command` see only a whitelisted slice of env. Names matching
  `KEY` / `TOKEN` / `SECRET` / `HMAC` / `API` / `AUTH` / etc. are filtered
  unless a skill's `_meta.json` opts the specific variable in via
  `requiresEnv`.
- **JWT-gated cross-process calls**: HS256, 60-second TTL, claim
  `allowed_tools` names the single tool the grant permits. Re-keyed
  every orchestrator launch.
- **Workspace boundary**: `tool/tool_file_ops.resolve_workspace_path`
  refuses paths that resolve outside `LANGCHAIN_AGENT_WORKSPACE_ROOT`
  (defaults to CWD).
- **Calculator**: AST-based safe evaluator (no `eval`).
- **HA call_service**: rejects `shell_command` / `python_script` /
  `command_line` / `rest_command` / `pyscript` / `hassio` domains.
