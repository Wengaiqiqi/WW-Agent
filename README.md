# W&W Agent CLI

A practical coding and research assistant running inside a terminal CLI, built with LangChain and LangGraph.

## Features

- **Interactive REPL**: Terminal-based chat interface with slash commands
- **Tool System**: File ops, shell, Python, V4A patch, web search/extract, persistent memory, agent-initiated clarify, and more
- **Skill System**: Extensible local skills with dynamic loading
- **Persistent Memory**: `MEMORY.md` / `USER.md` injected into the system prompt at session start; mid-session writes survive restart
- **Permission Management**: Three-tier model (read-only, workspace-write, danger-full-access)
- **Streaming Output**: Real-time response streaming with spinner animations
- **Session Management**: Conversation history with memory compaction

## Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Optional: Set Baidu ecommerce token (only needed for the Baidu skill)
export BAIDU_EC_SEARCH_TOKEN="your-token-here"
```

On first launch the CLI runs the `/model` wizard, asks you to pick a provider,
enter the API key, and confirm the base URL. The key is saved to
`.claude/credentials.json` (a sibling `.gitignore` is created automatically),
so subsequent launches read it back from there — no env var needed.

To preconfigure non-interactively, set `LANGCHAIN_AGENT_MODEL=<provider>` (or
`<provider>/<model>`) and export the provider's API-key env var; see the
table under "Model Configuration" for each provider's key name.

## Usage

### Interactive Mode

```bash
python cli.py
```

### Single Prompt Mode

```bash
python cli.py prompt "What can you do?"
```

### Slash Commands

- `/help` - Show available commands
- `/status` - Show current session status
- `/model [provider]` - Interactive 4-step model picker (skips Step 1 if a provider name is passed)
- `/tools` - List registered tools
- `/skills` - List installed skills
- `/instructions` - List loaded project instruction files
- `/permissions [mode]` - Show or set permission mode
- `/config` - Show effective configuration
- `/compact` - Start a fresh memory thread
- `/clear` - Clear the terminal
- `/exit` or `/quit` - Exit the CLI

## Multi-Agent Mode

`python cli.py` (no `--single`) boots a small multi-agent network:

- **orchestrator** — the CLI process; routes work to specialists and renders their output with `[orchestrator]` / `[skill]` / `[tool]` tags
- **skill-agent** — subprocess that loads `skills/*/SKILL.md` and runs each skill via its own LLM turn
- **tool-agent** — subprocess that exposes the `tool/*.py` functions

Specialists communicate with the orchestrator over **MCP** (stdio JSON-RPC). When a skill needs a tool mid-execution, skill-agent calls tool-agent directly over **A2A** (HTTP localhost JSON-RPC), and the call is mirrored back to the orchestrator via a telemetry side-channel so the user still sees the `[tool]` line in the unified stream.

### Single-agent fallback

`python cli.py --single` runs the original single-process REPL (unchanged from previous versions). No subprocesses are spawned.

### Runtime files

The multi-agent system uses `.agent/` for runtime state:

- `.agent/agents/<id>.card.json` — Agent Cards (registry entries)
- `.agent/runtime/peers.json` — A2A peer URL table written at startup
- `.agent/runtime/<id>.a2a-url` — per-specialist A2A URL sidecar
- `.agent/runtime/telemetry.ndjson` — A2A-call telemetry consumed by orchestrator
- `.agent/logs/` — specialist logs

`.agent/` is intentionally separate from `.claude/`, which is reserved for the Claude Code dev tool.

### Slash commands

- `/agents` — list registered specialists with their PIDs and A2A URLs
- (Full multi-agent REPL UX ships post-Day-1; for now, use `python cli.py prompt "..."` for non-interactive use)

### Design

For the full architecture (component responsibilities, protocol message shapes, permission model, testing strategy) see [docs/superpowers/specs/2026-05-15-multi-agent-orchestration-design.md](docs/superpowers/specs/2026-05-15-multi-agent-orchestration-design.md).

## Configuration

### Permission Modes

Set via environment variable or `/permissions` command:

- `read-only` - Only read operations allowed
- `workspace-write` - Read and write within workspace (default)
- `danger-full-access` - All operations including shell commands

```bash
export LANGCHAIN_AGENT_PERMISSION_MODE="read-only"
```

### Model Configuration

`config.py`'s `PROVIDERS` dict defines each provider's protocol, default
base URL, API-key env var, and the list of known model ids. The active
selection is an `ActiveConfig` (provider + model + base_url + api_key_env +
protocol) loaded from `.claude/settings.json` on startup.

Built-in providers (mirrored from `hermes-agent/hermes_cli`'s
`PROVIDER_REGISTRY` + `_PROVIDER_MODELS`, restricted to api-key auth with
OpenAI / Anthropic protocols):

- **First-party**: `anthropic`, `openai`, `deepseek`, `gemini`, `xai`, `nvidia`,
  `xiaomi`, `zai`, `kimi-coding`, `kimi-coding-cn`, `stepfun`, `minimax`,
  `minimax-cn`, `alibaba`, `alibaba-coding-plan`, `tencent-tokenhub`, `arcee`,
  `gmi`, `huggingface`
- **Aggregators**: `openrouter`, `ai-gateway`, `opencode-zen`, `opencode-go`,
  `kilocode`
- **Local / self-hosted**: `lmstudio`, `ollama-cloud`
- **Free-form**: `custom`

Providers with an empty model list (`custom`, `lmstudio`, `ollama-cloud`)
prompt for a free-form model id in Step 2 of the wizard.

> **xAI note**: hermes-agent uses xAI's `codex_responses` transport. This
> project routes xAI through OpenAI-compatible chat completions instead
> (`https://api.x.ai/v1`), so reasoning-only Grok variants may not surface
> the full reasoning trace. Standard chat models like `grok-4.3` work as
> expected.

Inside the REPL, `/model` opens an interactive wizard modeled on hermes-agent's
`hermes model` flow:

```
/model                # full wizard (4 steps)
/model deepseek       # skip Step 1; jump straight to model selection
/setup                # alias for /model
```

The four steps:

1. **Select provider** — numbered list with API-key status indicator
2. **Select model** — numbered list from that provider; `custom` provider
   prompts for free-form model id
3. **Enter API key** — masked input; press `y` to keep the existing key
4. **Enter base URL** — pre-filled with the provider default; required for
   `custom`

Selection priority on startup (highest first):

1. `LANGCHAIN_AGENT_MODEL` env var (`provider` or `provider/model`)
2. `.claude/settings.json` `model` block (written by the wizard)
3. `DEFAULT_PROVIDER` constant with its first model

The wizard writes the provider+model+base_url to `.claude/settings.json` and
saves the API key to `.claude/credentials.json` (a sibling `.gitignore`
shields the file on first save). Subsequent launches hydrate `os.environ`
from the credentials file, so the wizard only re-triggers when no key is
discoverable.

## Development

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=. --cov-report=html

# Run specific test file
pytest tests/test_calculator.py

# Run with verbose output
pytest -v
```

### Code Quality

```bash
# Format code
black .

# Lint code
flake8 .

# Type checking
mypy cli.py config.py tools.py
```

## Tools

Run `/tools` inside the REPL for the full live list. Highlights ported from
`hermes-agent/tools/`:

| Tool | Permission | What it does |
|---|---|---|
| `read_file` / `write_file` / `edit_file` | read / write | Single-file ops with workspace-boundary safety |
| `apply_patch` | write | **V4A multi-file unified-diff patches** — Add/Update/Delete/Move in one atomic call. Validates all hunks before writing anything; falls back with no side effects when any hunk fails to match |
| `glob_search` / `grep_search` / `list_directory` | read | Filesystem discovery |
| `run_python` / `run_command` | danger | Code / shell execution (requires `danger-full-access` mode) |
| `web_search` | read | **DuckDuckGo HTML search (no API key)** + Tavily when `TAVILY_API_KEY` is set. Returns `{title, url, snippet}` rows |
| `web_extract` | read | Fetch a URL and return readable plain text (HTML stripped, no JS rendering) |
| `memory` | write | **Cross-session memory** under `.claude/memories/`. `MEMORY.md` for agent notes, `USER.md` for user profile. Frozen snapshot is injected into the system prompt at session start |
| `clarify` | read | **Agent-initiated question to user** — multiple-choice (arrow-key picker in TTY) or open-ended. Use when the request is ambiguous or has meaningful trade-offs |
| `todo_write` | write | Structured task list under `.claude/todos.json` |
| `calculator` / `current_datetime` / `sleep` / `config` / `tool_manifest` | various | Misc utilities |

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
+content line 2
*** Delete File: obsolete.txt
*** Move File: src/old.py -> src/new.py
*** End Patch
```

### Persistent memory layout

```
.claude/memories/
├── MEMORY.md   # agent notes: codebase conventions, prior failures, environment quirks
└── USER.md     # user profile: preferences, goals, communication style
```

Entries are separated by `\n§\n` and capped per file (MEMORY 4 KB, USER 2 KB).
Mid-session `memory(action="add"|"replace"|"remove")` writes immediately to
disk so the next session picks them up; the in-prompt snapshot is frozen at
startup to keep the prefix cache stable.

### Logging

Logs are written to `.claude/agent.log`. Enable debug output to stderr:

```bash
export LANGCHAIN_AGENT_DEBUG=1
python cli.py
```

## Project Structure

```
.
├── cli.py                  # Main CLI entrypoint and agent loop
├── config.py               # Model configuration
├── tools.py                # Tool registration
├── tool_file_ops.py        # File operation implementations
├── tool_shell.py           # Shell command execution
├── tool_permissions.py     # Permission management
├── tool_registry.py        # Tool manifest
├── skill_loader.py         # Skill discovery and loading
├── project_context.py      # Project instruction files
├── skills/                 # Local skills directory
│   └── baidu-ecommerce-search/
│       ├── SKILL.md        # Skill instructions
│       ├── _meta.json      # Skill metadata
│       └── scripts/        # Skill-specific scripts
├── tests/                  # Test suite
│   ├── test_calculator.py
│   ├── test_permissions.py
│   ├── test_skill_loader.py
│   └── test_file_ops.py
├── requirements.txt        # Python dependencies
└── agent.md               # Project documentation

```

## Skills

Skills are loaded from `skills/<name>/SKILL.md`. Each skill can have:

- `SKILL.md` - Markdown instructions for the agent
- `_meta.json` - Metadata including `matchKeywords` for automatic activation
- `scripts/` - Python scripts called by the skill

### Creating a Skill

1. Create directory: `skills/my-skill/`
2. Add `SKILL.md` with instructions
3. Add `_meta.json` with keywords:

```json
{
  "slug": "my-skill",
  "version": "1.0.0",
  "matchKeywords": ["keyword1", "keyword2"]
}
```

## Security

- **Calculator**: Uses AST-based safe evaluation (no `eval()`)
- **Permissions**: Three-tier model with explicit authorization
- **Path Safety**: Workspace boundary enforcement
- **Shell Commands**: Windows uses `cmd.exe` for compatibility

## License

See project documentation for license information.
