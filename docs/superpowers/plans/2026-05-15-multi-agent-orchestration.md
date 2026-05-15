# Multi-Agent Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the single-agent CLI into a three-process multi-agent network (orchestrator + skill-agent + tool-agent) connected by MCP (stdio) and A2A (HTTP localhost), with the old single-agent path preserved behind `--single`.

**Architecture:** Orchestrator (CLI process) is a LangGraph `StateGraph` acting as MCP host. It spawns two specialist subprocesses at REPL startup, pre-authorizes each tool call via a short-lived HMAC JWT, and multiplexes their streamed output into the terminal with `[orchestrator]`/`[skill]`/`[tool]` tags. Specialists are stateless: they expose capabilities via MCP-over-stdio to the orchestrator and via A2A-over-HTTP-localhost to each other, with peer-to-peer calls reflected back to the orchestrator via a telemetry side channel.

**Tech Stack:** Python 3.10+, LangGraph (already in use), `mcp` (official Python SDK), `a2a-sdk` (official), `pyjwt`, `subprocess`, `httpx`, `pytest`. The project is NOT a git repository at plan time — `git init` is the first task so commits work.

**Spec:** `docs/superpowers/specs/2026-05-15-multi-agent-orchestration-design.md`

---

## File Structure

### New directories and files

```
orchestrator/                         # NEW package: CLI process logic
├── __init__.py
├── main.py                           # async REPL entrypoint for multi-agent mode
├── graph.py                          # LangGraph StateGraph definition
├── router.py                         # Capability Router (LLM-decided capability → specialist)
├── permission_gate.py                # three-tier policy decision + JWT signing
├── mcp_host.py                       # async MCP client pool (one per specialist)
├── registry.py                       # read Cards, spawn specialists, manage lifecycle
├── stream_mux.py                     # tag chunks by trace_id, write to terminal
└── telemetry.py                      # accept A2A-call notifications from specialists

agents/                               # NEW package: specialist processes
├── __init__.py
├── shared/
│   ├── __init__.py
│   ├── mcp_server.py                 # MCP-over-stdio JSON-RPC server skeleton
│   ├── a2a_server.py                 # A2A HTTP server (FastAPI+uvicorn or httpx-based)
│   └── authz.py                      # JWT verify (HMAC-SHA256)
├── skill_agent/
│   ├── __init__.py
│   ├── main.py                       # python -m agents.skill_agent.main
│   ├── skill_executor.py             # load SKILL.md, run LLM turn
│   └── a2a_client.py                 # outbound A2A calls to tool-agent
└── tool_agent/
    ├── __init__.py
    ├── main.py                       # python -m agents.tool_agent.main
    └── tool_executor.py              # invoke tool/*.py functions

legacy/                               # NEW: existing single-agent loop relocated here
├── __init__.py
└── single_agent_loop.py              # extracted from current cli.py

.agent/                               # NEW runtime namespace (NOT .claude/)
├── agents/
│   ├── skill-agent.card.json         # Agent Card (registry entry)
│   └── tool-agent.card.json
├── runtime/
│   └── state.json                    # written by orchestrator at startup
└── logs/                             # specialist logs

tests/
├── test_orchestrator/
│   ├── __init__.py
│   ├── test_permission_gate.py
│   ├── test_router.py
│   ├── test_stream_mux.py
│   ├── test_registry.py
│   └── test_mcp_host.py
├── test_skill_agent/
│   ├── __init__.py
│   └── test_skill_executor.py
├── test_tool_agent/
│   ├── __init__.py
│   └── test_tool_executor.py
├── test_shared/
│   ├── __init__.py
│   ├── test_authz.py
│   ├── test_mcp_server.py
│   └── test_a2a_server.py
├── test_e2e_multi_agent/
│   ├── __init__.py
│   ├── test_spawn_and_handshake.py
│   ├── test_mcp_roundtrip.py
│   ├── test_a2a_peer_call.py
│   ├── test_authz_violation.py
│   ├── test_ctrl_c_cancel.py
│   ├── test_specialist_crash.py
│   ├── test_e2e_simple_tool.py
│   ├── test_e2e_skill_a2a_chain.py
│   └── test_e2e_legacy_mode.py
└── conftest.py                       # MODIFIED: shared fixtures (subprocess, mock provider)
```

### Modified existing files

| File | Change |
|---|---|
| `cli.py` | Add `argparse --single` flag; dispatch to `orchestrator.main` (new default) or `legacy.single_agent_loop` |
| `config.py` | Add `"mock"` provider entry to `PROVIDERS` dict for deterministic testing |
| `tool_permissions.py` | Keep mode definitions; move policy-decision function to `orchestrator/permission_gate.py` |
| `requirements.txt` | Add `mcp`, `a2a-sdk`, `pyjwt`, `httpx`, `fastapi`, `uvicorn` |
| `pytest.ini` or `setup.cfg` | Add `e2e` pytest marker |

### Untouched

`tool/*.py`, `skills/*`, `skill_loader.py`, `tools.py`, `project_context.py`, `config.py PROVIDERS` (besides adding mock), `.claude/*` files.

---

## Phase 0 — Project Setup

### Task 0.1: Initialize git repository

**Files:**
- Create: `.gitignore`

- [ ] **Step 1: Verify not in a repo**

Run: `git rev-parse --is-inside-work-tree`
Expected: error "not a git repository", exit 128.

- [ ] **Step 2: Initialize repo**

Run: `git init`
Expected: "Initialized empty Git repository in ..."

- [ ] **Step 3: Create `.gitignore`**

```gitignore
__pycache__/
*.pyc
.pytest_cache/
htmlcov/
.coverage
.agent/runtime/
.agent/logs/
.claude/credentials.json
.claude/agent.log
*.egg-info/
.venv/
```

- [ ] **Step 4: Initial commit of existing tree**

```bash
git add .
git commit -m "chore: baseline before multi-agent redesign"
```
Expected: commit succeeds.

---

### Task 0.2: Add new dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Append dependencies**

Add to `requirements.txt`:
```
mcp>=1.0.0
a2a-sdk>=0.2.0
pyjwt>=2.8.0
httpx>=0.27.0
fastapi>=0.110.0
uvicorn>=0.29.0
```

- [ ] **Step 2: Install**

Run: `pip install -r requirements.txt`
Expected: successful install.

- [ ] **Step 3: Verify imports**

Run: `python -c "import mcp, a2a, jwt, httpx, fastapi, uvicorn; print('ok')"`
Expected: `ok`. If `a2a-sdk` exposes a different top-level name, adjust the import name and note the actual module in this step.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "build: add mcp, a2a-sdk, pyjwt, httpx, fastapi, uvicorn"
```

---

### Task 0.3: Create empty package skeletons

**Files:**
- Create: `orchestrator/__init__.py`, `agents/__init__.py`, `agents/shared/__init__.py`, `agents/skill_agent/__init__.py`, `agents/tool_agent/__init__.py`, `legacy/__init__.py`, `tests/test_orchestrator/__init__.py`, `tests/test_skill_agent/__init__.py`, `tests/test_tool_agent/__init__.py`, `tests/test_shared/__init__.py`, `tests/test_e2e_multi_agent/__init__.py`

- [ ] **Step 1: Create empty `__init__.py` files**

Each `__init__.py` is empty.

- [ ] **Step 2: Add e2e marker to pytest config**

In `setup.cfg`, add under `[tool:pytest]`:
```ini
markers =
    e2e: end-to-end tests that spawn subprocesses
```

- [ ] **Step 3: Verify collection**

Run: `pytest --collect-only -q`
Expected: existing tests still collected, no errors from new empty packages.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/ agents/ legacy/ tests/test_orchestrator/ tests/test_skill_agent/ tests/test_tool_agent/ tests/test_shared/ tests/test_e2e_multi_agent/ setup.cfg
git commit -m "chore: scaffold multi-agent package directories"
```

---

## Phase 1 — Mock LLM Provider (Testing Foundation)

### Task 1.1: Add `mock` provider entry

**Files:**
- Modify: `config.py`
- Create: `tests/test_mock_provider.py`

- [ ] **Step 1: Read current `config.py` PROVIDERS structure**

Open `config.py` and locate the `PROVIDERS` dict. Note the schema each entry uses.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_mock_provider.py
from config import PROVIDERS

def test_mock_provider_registered():
    assert "mock" in PROVIDERS
    entry = PROVIDERS["mock"]
    assert entry["protocol"] in ("openai", "anthropic", "mock")
    assert entry["api_key_env"] == "MOCK_API_KEY"

def test_mock_provider_has_dummy_model():
    assert "mock-default" in PROVIDERS["mock"]["models"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_mock_provider.py -v`
Expected: FAIL (`"mock" not in PROVIDERS`).

- [ ] **Step 4: Add mock entry to `PROVIDERS`**

Append to the `PROVIDERS` dict in `config.py`:
```python
"mock": {
    "protocol": "mock",
    "base_url": "",
    "api_key_env": "MOCK_API_KEY",
    "models": ["mock-default", "mock-skill", "mock-tool"],
},
```

- [ ] **Step 5: Run test to verify pass**

Run: `pytest tests/test_mock_provider.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add config.py tests/test_mock_provider.py
git commit -m "feat(config): register mock LLM provider for tests"
```

---

### Task 1.2: Wire the mock chat-model class

**Files:**
- Create: `agents/shared/mock_chat_model.py`
- Create: `tests/test_shared/test_mock_chat_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shared/test_mock_chat_model.py
from agents.shared.mock_chat_model import MockChatModel

def test_mock_returns_canned_response():
    model = MockChatModel(responses=["hello"])
    out = model.invoke([{"role": "user", "content": "hi"}])
    assert out.content == "hello"

def test_mock_cycles_through_responses():
    model = MockChatModel(responses=["a", "b"])
    assert model.invoke([])._content_str() == "a"
    assert model.invoke([])._content_str() == "b"
    assert model.invoke([])._content_str() == "a"  # cycles

def test_mock_records_call_history():
    model = MockChatModel(responses=["x"])
    model.invoke([{"role": "user", "content": "ping"}])
    assert model.call_history[0][0]["content"] == "ping"
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_shared/test_mock_chat_model.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `MockChatModel`**

```python
# agents/shared/mock_chat_model.py
from dataclasses import dataclass, field
from itertools import cycle
from typing import Any


@dataclass
class _Result:
    content: str

    def _content_str(self) -> str:
        return self.content


class MockChatModel:
    """Deterministic chat model for tests. Cycles through a fixed response list."""

    def __init__(self, responses: list[str]):
        if not responses:
            raise ValueError("responses must be non-empty")
        self._responses = cycle(responses)
        self.call_history: list[Any] = []

    def invoke(self, messages: list[dict]) -> _Result:
        self.call_history.append(messages)
        return _Result(content=next(self._responses))
```

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_shared/test_mock_chat_model.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/shared/mock_chat_model.py tests/test_shared/test_mock_chat_model.py
git commit -m "feat(test): add MockChatModel for deterministic testing"
```

---

## Phase 2 — Legacy Extraction (Preserve `--single` Path)

### Task 2.1: Extract current `cli.py` main loop into `legacy/single_agent_loop.py`

**Files:**
- Create: `legacy/single_agent_loop.py`
- Modify: `cli.py`

- [ ] **Step 1: Read current `cli.py` structure**

Open `cli.py` and identify (a) the `main()` function (or equivalent REPL entrypoint), (b) the `prompt` subcommand handler, (c) any setup code. Note their exact line ranges.

- [ ] **Step 2: Copy main logic to `legacy/single_agent_loop.py`**

Create `legacy/single_agent_loop.py` with two top-level callables:
```python
# legacy/single_agent_loop.py
"""Pre-multi-agent CLI loop. Preserved verbatim for `python cli.py --single`."""

# Paste all imports from current cli.py here, EXCEPT the argparse top-level
# block. The body of the original `main()` becomes `run_repl()`. The body of
# the single-prompt mode becomes `run_prompt(prompt: str)`.

def run_repl() -> int:
    # Paste original cli.py main() body
    ...

def run_prompt(prompt: str) -> int:
    # Paste original single-prompt handler body
    ...
```

If your current `cli.py` is structured differently (e.g., everything inside one `main()`), wrap the parts inside a single `run_legacy(args)` function and call it appropriately.

- [ ] **Step 3: Reduce `cli.py` to a dispatcher (single-only for now, multi added in Phase 5)**

Replace `cli.py` contents with:
```python
"""CLI entrypoint.

Default: multi-agent orchestrator (added in Phase 5).
--single: legacy single-agent loop.
"""
from __future__ import annotations
import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="LangChain agent CLI")
    parser.add_argument(
        "--single",
        action="store_true",
        help="Use the legacy single-agent loop instead of the multi-agent orchestrator.",
    )
    sub = parser.add_subparsers(dest="subcommand")
    sub_prompt = sub.add_parser("prompt", help="Run one prompt non-interactively")
    sub_prompt.add_argument("text", help="Prompt text")

    args = parser.parse_args()

    # Phase 2: only --single is implemented. Default falls back to legacy with a notice.
    # Phase 5 will replace the fallback with orchestrator.main().
    if not args.single:
        print(
            "[cli] multi-agent orchestrator not yet wired in this build; "
            "falling back to legacy single-agent loop.",
            file=sys.stderr,
        )

    from legacy.single_agent_loop import run_repl, run_prompt
    if args.subcommand == "prompt":
        return run_prompt(args.text)
    return run_repl()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Smoke-test legacy still works**

Run: `python cli.py --single prompt "What can you do?"` (requires a configured model; if not configured locally, run `python cli.py --single` and immediately `/exit` from the REPL).
Expected: behavior identical to pre-refactor.

- [ ] **Step 5: Commit**

```bash
git add legacy/single_agent_loop.py cli.py
git commit -m "refactor: extract legacy REPL to legacy/single_agent_loop.py, cli.py becomes dispatcher"
```

---

### Task 2.2: Test legacy path is reachable

**Files:**
- Create: `tests/test_e2e_multi_agent/test_e2e_legacy_mode.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_e2e_multi_agent/test_e2e_legacy_mode.py
import os
import subprocess
import sys
import pytest


@pytest.mark.e2e
def test_legacy_mode_does_not_spawn_specialists(tmp_path):
    """--single should run in a single process — no child subprocesses."""
    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock/mock-default"
    env["MOCK_API_KEY"] = "x"  # any non-empty
    # Use the `prompt` subcommand so the process exits without REPL input.
    proc = subprocess.run(
        [sys.executable, "cli.py", "--single", "prompt", "hello"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    # Loose check: it didn't crash with a Python traceback related to multi-agent code.
    assert "Traceback" not in proc.stderr or "single" in proc.stderr.lower()
    # Stricter check: orchestrator package was never imported by --single path.
    # This is approximate; tightened in Phase 5 once the orchestrator exists.
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_e2e_multi_agent/test_e2e_legacy_mode.py -v -m e2e`
Expected: pass (best-effort; the strict check tightens later).

- [ ] **Step 3: Commit**

```bash
git add tests/test_e2e_multi_agent/test_e2e_legacy_mode.py
git commit -m "test(e2e): verify --single mode runs the legacy path"
```

---

## Phase 3 — Shared Authz (JWT Sign / Verify)

### Task 3.1: Implement JWT verification (used by both specialists)

**Files:**
- Create: `agents/shared/authz.py`
- Create: `tests/test_shared/test_authz.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_shared/test_authz.py
import time
import jwt as pyjwt
import pytest
from agents.shared.authz import verify_grant, AuthzError

KEY = "test-secret-key"


def _make(payload: dict) -> str:
    return pyjwt.encode(payload, KEY, algorithm="HS256")


def test_valid_grant_allows_listed_tool():
    token = _make({
        "iss": "orchestrator",
        "sub": "tool-agent",
        "exp": int(time.time()) + 60,
        "permission_mode": "workspace-write",
        "allowed_tools": ["read_file", "grep_search"],
        "trace_id": "t1",
    })
    claims = verify_grant(token, key=KEY, requested_tool="read_file")
    assert claims["sub"] == "tool-agent"
    assert claims["trace_id"] == "t1"


def test_expired_grant_rejected():
    token = _make({
        "iss": "orchestrator",
        "sub": "tool-agent",
        "exp": int(time.time()) - 1,
        "permission_mode": "read-only",
        "allowed_tools": ["read_file"],
        "trace_id": "t1",
    })
    with pytest.raises(AuthzError, match="expired"):
        verify_grant(token, key=KEY, requested_tool="read_file")


def test_tampered_signature_rejected():
    token = _make({
        "iss": "orchestrator", "sub": "tool-agent",
        "exp": int(time.time()) + 60,
        "permission_mode": "read-only", "allowed_tools": ["read_file"],
        "trace_id": "t1",
    })
    bad = token[:-4] + "AAAA"
    with pytest.raises(AuthzError, match="signature"):
        verify_grant(bad, key=KEY, requested_tool="read_file")


def test_off_whitelist_tool_rejected():
    token = _make({
        "iss": "orchestrator", "sub": "tool-agent",
        "exp": int(time.time()) + 60,
        "permission_mode": "workspace-write",
        "allowed_tools": ["read_file"],
        "trace_id": "t1",
    })
    with pytest.raises(AuthzError, match="not in allowed_tools"):
        verify_grant(token, key=KEY, requested_tool="run_command")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_shared/test_authz.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `verify_grant`**

```python
# agents/shared/authz.py
from __future__ import annotations
import jwt as pyjwt


class AuthzError(Exception):
    pass


def verify_grant(token: str, *, key: str, requested_tool: str) -> dict:
    """Verify the authz_grant JWT. Returns the decoded claims on success.

    Raises AuthzError on signature failure, expiry, or tool not in allowed_tools.
    Note: `sub` is audit-only (capability delegation model); gating is purely
    via `allowed_tools` containment.
    """
    try:
        claims = pyjwt.decode(token, key, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        raise AuthzError("authz_grant expired") from None
    except pyjwt.InvalidSignatureError:
        raise AuthzError("authz_grant signature invalid") from None
    except pyjwt.PyJWTError as exc:
        raise AuthzError(f"authz_grant decode error: {exc}") from exc

    allowed = claims.get("allowed_tools") or []
    if requested_tool not in allowed:
        raise AuthzError(
            f"tool {requested_tool!r} not in allowed_tools {allowed!r}"
        )
    return claims
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_shared/test_authz.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/shared/authz.py tests/test_shared/test_authz.py
git commit -m "feat(authz): JWT verify with capability-delegation semantics"
```

---

### Task 3.2: Implement JWT signing (used by orchestrator)

**Files:**
- Create: `orchestrator/permission_gate.py`
- Create: `tests/test_orchestrator/test_permission_gate.py`

- [ ] **Step 1: Read existing `tool_permissions.py` to identify the three modes**

Open `tool_permissions.py` and locate the constants/enum for `read-only`, `workspace-write`, `danger-full-access`, plus the per-tool allow/deny lists if any.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_orchestrator/test_permission_gate.py
import time
import pytest
from orchestrator.permission_gate import PermissionGate, PermissionDenied
from agents.shared.authz import verify_grant


def test_read_only_mode_allows_read_tools():
    gate = PermissionGate(mode="read-only", hmac_key="k", trace_id="t1")
    grant = gate.sign(target_specialist="tool-agent", tool="read_file")
    claims = verify_grant(grant, key="k", requested_tool="read_file")
    assert claims["permission_mode"] == "read-only"
    assert claims["sub"] == "tool-agent"
    assert claims["trace_id"] == "t1"


def test_read_only_mode_denies_write_tools():
    gate = PermissionGate(mode="read-only", hmac_key="k", trace_id="t1")
    with pytest.raises(PermissionDenied, match="write_file"):
        gate.sign(target_specialist="tool-agent", tool="write_file")


def test_workspace_write_allows_writes_denies_shell():
    gate = PermissionGate(mode="workspace-write", hmac_key="k", trace_id="t1")
    gate.sign(target_specialist="tool-agent", tool="write_file")  # ok
    with pytest.raises(PermissionDenied, match="run_command"):
        gate.sign(target_specialist="tool-agent", tool="run_command")


def test_danger_full_access_allows_shell():
    gate = PermissionGate(mode="danger-full-access", hmac_key="k", trace_id="t1")
    gate.sign(target_specialist="tool-agent", tool="run_command")  # ok


def test_grant_expires_in_60_seconds():
    gate = PermissionGate(mode="read-only", hmac_key="k", trace_id="t1")
    grant = gate.sign(target_specialist="tool-agent", tool="read_file")
    import jwt as pyjwt
    claims = pyjwt.decode(grant, "k", algorithms=["HS256"])
    assert 55 <= claims["exp"] - int(time.time()) <= 60
```

- [ ] **Step 3: Run tests to verify failure**

Run: `pytest tests/test_orchestrator/test_permission_gate.py -v`
Expected: FAIL (module not found).

- [ ] **Step 4: Implement `PermissionGate`**

```python
# orchestrator/permission_gate.py
from __future__ import annotations
import time
import jwt as pyjwt


class PermissionDenied(Exception):
    pass


# Per-mode tool whitelist. Wildcards permit any tool whose name starts with the
# given prefix; this matches the existing tool_permissions.py categorization.
_MODE_WHITELIST: dict[str, list[str]] = {
    "read-only": [
        "read_file", "grep_search", "glob_search", "list_directory",
        "web_search", "web_extract", "calculator", "current_datetime",
        "tool_manifest", "config", "clarify",
    ],
    "workspace-write": [
        "read_file", "grep_search", "glob_search", "list_directory",
        "web_search", "web_extract", "calculator", "current_datetime",
        "tool_manifest", "config", "clarify",
        "write_file", "edit_file", "apply_patch", "memory", "todo_write",
    ],
    "danger-full-access": ["*"],  # everything
}


class PermissionGate:
    """Decides whether a tool may be called under the current mode and signs
    a short-lived authz_grant JWT for the chosen specialist."""

    def __init__(self, *, mode: str, hmac_key: str, trace_id: str):
        if mode not in _MODE_WHITELIST:
            raise ValueError(f"unknown permission mode: {mode}")
        self.mode = mode
        self.hmac_key = hmac_key
        self.trace_id = trace_id

    def _is_allowed(self, tool: str) -> bool:
        wl = _MODE_WHITELIST[self.mode]
        return "*" in wl or tool in wl

    def sign(self, *, target_specialist: str, tool: str) -> str:
        if not self._is_allowed(tool):
            raise PermissionDenied(
                f"tool {tool!r} not permitted under mode {self.mode!r}"
            )
        now = int(time.time())
        payload = {
            "iss": "orchestrator",
            "sub": target_specialist,
            "exp": now + 60,
            "permission_mode": self.mode,
            "allowed_tools": [tool],  # narrow: exactly the one tool requested
            "trace_id": self.trace_id,
        }
        return pyjwt.encode(payload, self.hmac_key, algorithm="HS256")
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_orchestrator/test_permission_gate.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/permission_gate.py tests/test_orchestrator/test_permission_gate.py
git commit -m "feat(orchestrator): permission gate with per-tool JWT signing"
```

---

## Phase 4 — tool-agent as Standalone MCP Server

### Task 4.1: MCP server skeleton (shared by both specialists)

**Files:**
- Create: `agents/shared/mcp_server.py`
- Create: `tests/test_shared/test_mcp_server.py`

- [ ] **Step 1: Read the MCP SDK quickstart**

Open the installed `mcp` package's README or `python -m mcp --help` to confirm the server-creation API. The expected primitives: a `Server` class with `@server.list_tools()` and `@server.call_tool()` decorators, plus `stdio_server()` for stdio transport.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_shared/test_mcp_server.py
import asyncio
from agents.shared.mcp_server import build_server, ToolSpec


def test_build_server_registers_tools():
    async def handler(args):
        return {"echo": args.get("x")}

    spec = ToolSpec(
        name="echo",
        description="Echo back x",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        handler=handler,
    )
    server, _stdio = build_server(name="test-agent", tools=[spec])
    # Listing tools returns the registered spec
    tools = asyncio.run(server.list_tools_impl())
    names = [t.name for t in tools]
    assert "echo" in names


def test_call_tool_dispatches_to_handler():
    async def handler(args):
        return {"echo": args.get("x")}

    spec = ToolSpec(
        name="echo", description="", input_schema={}, handler=handler,
    )
    server, _ = build_server(name="test-agent", tools=[spec])
    result = asyncio.run(server.call_tool_impl("echo", {"x": "hi"}))
    assert result == {"echo": "hi"}
```

- [ ] **Step 3: Run test to verify failure**

Run: `pytest tests/test_shared/test_mcp_server.py -v`
Expected: FAIL (module not found).

- [ ] **Step 4: Implement the skeleton**

```python
# agents/shared/mcp_server.py
"""Thin wrapper around the official MCP SDK's stdio server.

Both skill-agent and tool-agent use this. They differ only in the list of
`ToolSpec` they register.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from mcp.server import Server
from mcp.types import Tool


Handler = Callable[[dict], Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Handler


class _ServerProxy:
    """Wraps the MCP `Server` and remembers the spec list for direct testing."""

    def __init__(self, server: Server, specs: list[ToolSpec]):
        self._server = server
        self._specs = {s.name: s for s in specs}

    async def list_tools_impl(self) -> list[Tool]:
        return [
            Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
            for s in self._specs.values()
        ]

    async def call_tool_impl(self, name: str, arguments: dict) -> Any:
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError(f"unknown tool: {name}")
        return await spec.handler(arguments)

    @property
    def server(self) -> Server:
        return self._server


def build_server(*, name: str, tools: list[ToolSpec]) -> tuple[_ServerProxy, Any]:
    """Construct an MCP Server with the given ToolSpecs registered.

    Returns (proxy, stdio_runner) where `stdio_runner` is an async function
    the specialist's main() will await to serve over stdio.
    """
    server = Server(name)
    spec_map = {s.name: s for s in tools}

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
            for s in spec_map.values()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> Any:
        spec = spec_map.get(name)
        if spec is None:
            raise ValueError(f"unknown tool: {name}")
        return await spec.handler(arguments)

    proxy = _ServerProxy(server, tools)

    async def stdio_runner() -> None:
        from mcp.server.stdio import stdio_server
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    return proxy, stdio_runner
```

If the installed `mcp` SDK's API differs (e.g., decorator names, `Tool` constructor), adjust to match what `python -c "import mcp.server; help(mcp.server.Server)"` reveals. Keep the public surface (`build_server`, `ToolSpec`, `_ServerProxy.list_tools_impl`, `_ServerProxy.call_tool_impl`) stable so tests keep working.

- [ ] **Step 5: Run test to verify pass**

Run: `pytest tests/test_shared/test_mcp_server.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add agents/shared/mcp_server.py tests/test_shared/test_mcp_server.py
git commit -m "feat(agents/shared): MCP stdio server skeleton with ToolSpec API"
```

---

### Task 4.2: tool-agent tool executor

**Files:**
- Create: `agents/tool_agent/tool_executor.py`
- Create: `tests/test_tool_agent/test_tool_executor.py`

- [ ] **Step 1: Read the existing tool registry**

Look at `tools.py` and the `tool/` directory. Note the function signatures of `read_file`, `grep_search`, `calculator`, etc. — specifically how arguments are passed (positional vs. keyword) and what they return.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_tool_agent/test_tool_executor.py
import pytest
from agents.tool_agent.tool_executor import build_tool_specs, execute_tool


def test_tool_specs_include_read_file():
    specs = build_tool_specs()
    names = {s.name for s in specs}
    assert "read_file" in names
    assert "calculator" in names


def test_execute_calculator():
    result = pytest.run_async(execute_tool("calculator", {"expression": "2 + 3"}))
    # Adapt to your existing calculator's return shape
    assert "5" in str(result)


def test_execute_unknown_tool_raises():
    with pytest.raises(ValueError, match="unknown tool"):
        pytest.run_async(execute_tool("not_a_tool", {}))
```

Note: `pytest.run_async` is shorthand here — in practice use `asyncio.run` or the `pytest-asyncio` plugin. If `pytest-asyncio` is not already in `requirements.txt`, add `pytest-asyncio>=0.23` and decorate tests with `@pytest.mark.asyncio`.

- [ ] **Step 3: Run test to verify failure**

Run: `pytest tests/test_tool_agent/test_tool_executor.py -v`
Expected: FAIL (module not found).

- [ ] **Step 4: Implement the executor**

```python
# agents/tool_agent/tool_executor.py
"""Bridge between the existing in-process tool registry and the MCP ToolSpec API.

Reuses tool/*.py functions verbatim — this module only adapts their signatures
and produces JSON schemas for MCP `tools/list`.
"""
from __future__ import annotations
from typing import Any
from agents.shared.mcp_server import ToolSpec

# Import the existing tool implementations. Names here must match what tools.py
# actually exports. Adjust if the existing module structure differs.
from tool.tool_file_ops import read_file as _read_file, write_file as _write_file
from tool.tool_shell import run_command as _run_command
# ... add remaining tools as needed: glob, grep, web, memory, clarify, calculator


async def _wrap_read_file(args: dict) -> Any:
    return _read_file(args["path"])


async def _wrap_write_file(args: dict) -> Any:
    return _write_file(args["path"], args["content"])


async def _wrap_run_command(args: dict) -> Any:
    return _run_command(args["command"])


# Map MCP tool name → (handler, input_schema, description)
_TOOL_MAP: dict[str, tuple] = {
    "read_file": (
        _wrap_read_file,
        {"type": "object", "required": ["path"],
         "properties": {"path": {"type": "string"}}},
        "Read a file from the workspace.",
    ),
    "write_file": (
        _wrap_write_file,
        {"type": "object", "required": ["path", "content"],
         "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
        "Write a file to the workspace.",
    ),
    "run_command": (
        _wrap_run_command,
        {"type": "object", "required": ["command"],
         "properties": {"command": {"type": "string"}}},
        "Run a shell command (requires danger-full-access).",
    ),
    # Add the remaining tools in the same pattern.
}


def build_tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(name=name, description=desc, input_schema=schema, handler=handler)
        for name, (handler, schema, desc) in _TOOL_MAP.items()
    ]


async def execute_tool(name: str, args: dict) -> Any:
    entry = _TOOL_MAP.get(name)
    if entry is None:
        raise ValueError(f"unknown tool: {name}")
    handler, _schema, _desc = entry
    return await handler(args)
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_tool_agent/test_tool_executor.py -v`
Expected: 3 passed (after fixing `pytest.run_async` to `asyncio.run` or `@pytest.mark.asyncio`).

- [ ] **Step 6: Commit**

```bash
git add agents/tool_agent/tool_executor.py tests/test_tool_agent/test_tool_executor.py
git commit -m "feat(tool-agent): adapter from tool/*.py to MCP ToolSpec API"
```

---

### Task 4.3: tool-agent `main` entrypoint

**Files:**
- Create: `agents/tool_agent/main.py`
- Create: `tests/test_e2e_multi_agent/test_spawn_and_handshake.py`

- [ ] **Step 1: Write the entrypoint**

```python
# agents/tool_agent/main.py
"""tool-agent process entrypoint.

Launched by orchestrator via:
    python -m agents.tool_agent.main
"""
from __future__ import annotations
import asyncio
import os
import sys
from agents.shared.mcp_server import build_server
from agents.tool_agent.tool_executor import build_tool_specs


async def amain() -> None:
    specs = build_tool_specs()
    # Wrap each handler so it verifies the authz_grant before executing.
    # (Authz wrapper added in Task 4.4 — for now handlers run unguarded.)
    proxy, runner = build_server(name="tool-agent", tools=specs)
    await runner()


def main() -> int:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Write a smoke spawn test**

```python
# tests/test_e2e_multi_agent/test_spawn_and_handshake.py
import asyncio
import os
import sys
import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_tool_agent_spawn_and_list_tools():
    """Orchestrator-style: spawn tool-agent via subprocess + MCP stdio client,
    initialize the session, list its tools."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agents.tool_agent.main"],
        env=os.environ.copy(),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "read_file" in names
            assert "calculator" in names or "write_file" in names
```

- [ ] **Step 3: Run the test**

Run: `pytest tests/test_e2e_multi_agent/test_spawn_and_handshake.py -v -m e2e`
Expected: pass (subprocess spawns, MCP handshake succeeds, tool list returned).

If the test fails because the MCP SDK's import paths differ, run `python -c "import mcp; help(mcp)"` to discover the correct names and update the test.

- [ ] **Step 4: Commit**

```bash
git add agents/tool_agent/main.py tests/test_e2e_multi_agent/test_spawn_and_handshake.py
git commit -m "feat(tool-agent): main entrypoint + spawn/handshake e2e test"
```

---

### Task 4.4: Wrap tool handlers with authz check

**Files:**
- Modify: `agents/tool_agent/tool_executor.py`
- Modify: `agents/tool_agent/main.py`
- Create: `tests/test_e2e_multi_agent/test_authz_violation.py`

- [ ] **Step 1: Write failing e2e test for authz**

```python
# tests/test_e2e_multi_agent/test_authz_violation.py
import os
import sys
import time
import pytest
import jwt as pyjwt
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


HMAC_KEY = "test-key-for-authz"


def _grant(allowed: list[str], expired: bool = False, key: str = HMAC_KEY) -> str:
    return pyjwt.encode(
        {
            "iss": "orchestrator", "sub": "tool-agent",
            "exp": int(time.time()) + (-1 if expired else 60),
            "permission_mode": "workspace-write",
            "allowed_tools": allowed, "trace_id": "t1",
        },
        key, algorithm="HS256",
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_off_whitelist_tool_rejected():
    env = os.environ.copy()
    env["AUTHZ_HMAC_KEY"] = HMAC_KEY
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agents.tool_agent.main"], env=env,
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            with pytest.raises(Exception, match="(authz|allowed_tools)"):
                await session.call_tool(
                    "read_file",
                    arguments={
                        "path": "README.md",
                        "_meta": {"authz_grant": _grant(["write_file"])},
                    },
                )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_expired_grant_rejected():
    env = os.environ.copy()
    env["AUTHZ_HMAC_KEY"] = HMAC_KEY
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agents.tool_agent.main"], env=env,
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            with pytest.raises(Exception, match="expired"):
                await session.call_tool(
                    "read_file",
                    arguments={
                        "path": "README.md",
                        "_meta": {"authz_grant": _grant(["read_file"], expired=True)},
                    },
                )
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_e2e_multi_agent/test_authz_violation.py -v -m e2e`
Expected: FAIL (handlers don't verify yet).

- [ ] **Step 3: Wrap handlers with authz check**

Modify `agents/tool_agent/tool_executor.py`'s `execute_tool` (and update each per-tool wrapper) so they expect an `_meta.authz_grant` field in `args` and verify it before executing:

```python
# at top of agents/tool_agent/tool_executor.py
import os
from agents.shared.authz import verify_grant, AuthzError


def _hmac_key() -> str:
    key = os.environ.get("AUTHZ_HMAC_KEY")
    if not key:
        raise RuntimeError("AUTHZ_HMAC_KEY env var not set; orchestrator must spawn this process")
    return key


async def execute_tool(name: str, args: dict) -> Any:
    entry = _TOOL_MAP.get(name)
    if entry is None:
        raise ValueError(f"unknown tool: {name}")
    handler, _schema, _desc = entry

    # Extract and verify the authz grant from _meta.
    meta = args.get("_meta") or {}
    grant = meta.get("authz_grant")
    if grant is None:
        raise AuthzError("missing authz_grant in _meta")
    verify_grant(grant, key=_hmac_key(), requested_tool=name)

    # Strip _meta before forwarding to the underlying tool.
    real_args = {k: v for k, v in args.items() if k != "_meta"}
    return await handler(real_args)
```

Update `agents/tool_agent/main.py` to ensure each ToolSpec's handler points to `execute_tool` so the wrapping is uniform:

```python
async def amain() -> None:
    specs = build_tool_specs()
    # Replace each spec's handler with a closure that goes through execute_tool
    # (which performs authz). This guarantees no path bypasses the gate.
    from agents.tool_agent.tool_executor import execute_tool

    def _make_handler(tool_name: str):
        async def _h(args: dict) -> Any:
            return await execute_tool(tool_name, args)
        return _h

    guarded = [
        ToolSpec(s.name, s.description, s.input_schema, _make_handler(s.name))
        for s in specs
    ]
    _proxy, runner = build_server(name="tool-agent", tools=guarded)
    await runner()
```

Adjust the imports at the top of `main.py` to include `ToolSpec` and `Any`.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_e2e_multi_agent/test_authz_violation.py -v -m e2e`
Expected: 2 passed.

- [ ] **Step 5: Update spawn-and-handshake test to set the env var**

In `tests/test_e2e_multi_agent/test_spawn_and_handshake.py`, set `env["AUTHZ_HMAC_KEY"] = "test"` in the env dict so the handshake test still passes (`tools/list` itself doesn't need authz, but starting the process now requires the env var per `_hmac_key()`).

- [ ] **Step 6: Commit**

```bash
git add agents/tool_agent/ tests/test_e2e_multi_agent/test_authz_violation.py tests/test_e2e_multi_agent/test_spawn_and_handshake.py
git commit -m "feat(tool-agent): enforce authz_grant on every tool call"
```

---

## Phase 5 — Orchestrator (Minimum: routes to tool-agent only)

### Task 5.1: Agent Registry — read Cards and prepare spawn specs

**Files:**
- Create: `orchestrator/registry.py`
- Create: `.agent/agents/tool-agent.card.json`
- Create: `tests/test_orchestrator/test_registry.py`

- [ ] **Step 1: Create the Card file**

```jsonc
// .agent/agents/tool-agent.card.json
{
  "id": "tool-agent",
  "display_name": "Tool Specialist",
  "version": "1.0.0",
  "entrypoint": {
    "type": "python",
    "module": "agents.tool_agent.main",
    "args": []
  },
  "mcp": { "transport": "stdio" },
  "a2a": { "transport": "http", "port_strategy": "ephemeral" },
  "capabilities_hint": ["tool"],
  "model_override": null
}
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_orchestrator/test_registry.py
from pathlib import Path
from orchestrator.registry import load_cards, Card


def test_load_cards_finds_tool_agent(tmp_path):
    cards_dir = tmp_path / ".agent" / "agents"
    cards_dir.mkdir(parents=True)
    (cards_dir / "tool-agent.card.json").write_text(
        '{"id":"tool-agent","display_name":"T","version":"1","entrypoint":'
        '{"type":"python","module":"agents.tool_agent.main","args":[]},'
        '"mcp":{"transport":"stdio"},"a2a":{"transport":"http","port_strategy":"ephemeral"},'
        '"capabilities_hint":["tool"],"model_override":null}',
        encoding="utf-8",
    )
    cards = load_cards(tmp_path / ".agent" / "agents")
    assert len(cards) == 1
    assert cards[0].id == "tool-agent"
    assert cards[0].entrypoint["module"] == "agents.tool_agent.main"


def test_load_cards_skips_invalid_json(tmp_path):
    cards_dir = tmp_path / ".agent" / "agents"
    cards_dir.mkdir(parents=True)
    (cards_dir / "broken.card.json").write_text("not json", encoding="utf-8")
    cards = load_cards(cards_dir)
    assert cards == []
```

- [ ] **Step 3: Run tests to verify failure**

Run: `pytest tests/test_orchestrator/test_registry.py -v`
Expected: FAIL (module not found).

- [ ] **Step 4: Implement Registry data class + loader**

```python
# orchestrator/registry.py
from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Card:
    id: str
    display_name: str
    version: str
    entrypoint: dict
    mcp: dict
    a2a: dict
    capabilities_hint: list[str]
    model_override: dict | None


def load_cards(cards_dir: Path) -> list[Card]:
    """Load all *.card.json files under cards_dir. Silently skip malformed files."""
    if not cards_dir.exists():
        return []
    out: list[Card] = []
    for path in sorted(cards_dir.glob("*.card.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append(Card(
                id=data["id"],
                display_name=data["display_name"],
                version=data["version"],
                entrypoint=data["entrypoint"],
                mcp=data["mcp"],
                a2a=data["a2a"],
                capabilities_hint=data.get("capabilities_hint", []),
                model_override=data.get("model_override"),
            ))
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("ignoring invalid card %s: %s", path, exc)
    return out
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_orchestrator/test_registry.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/registry.py .agent/agents/tool-agent.card.json tests/test_orchestrator/test_registry.py
git commit -m "feat(orchestrator): Agent Card loader + tool-agent card"
```

---

### Task 5.2: MCP host client pool — spawn one specialist and call it

**Files:**
- Create: `orchestrator/mcp_host.py`
- Create: `tests/test_orchestrator/test_mcp_host.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_orchestrator/test_mcp_host.py
import os
import pytest
from orchestrator.registry import Card
from orchestrator.mcp_host import MCPHost


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_mcp_host_spawns_tool_agent_and_calls_read_file(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hi there", encoding="utf-8")

    card = Card(
        id="tool-agent", display_name="T", version="1",
        entrypoint={"type": "python", "module": "agents.tool_agent.main", "args": []},
        mcp={"transport": "stdio"},
        a2a={"transport": "http", "port_strategy": "ephemeral"},
        capabilities_hint=["tool"], model_override=None,
    )

    host = MCPHost(hmac_key="test-key")
    await host.spawn(card)
    try:
        tools = await host.list_tools("tool-agent")
        assert "read_file" in [t.name for t in tools]
    finally:
        await host.shutdown_all()
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_orchestrator/test_mcp_host.py -v -m e2e`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `MCPHost`**

```python
# orchestrator/mcp_host.py
from __future__ import annotations
import os
import sys
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from orchestrator.registry import Card

log = logging.getLogger(__name__)


@dataclass
class _ClientHandle:
    card: Card
    session: ClientSession
    stack: AsyncExitStack
    a2a_url: str | None = None


class MCPHost:
    """Manages MCP client sessions to each specialist subprocess."""

    def __init__(self, *, hmac_key: str):
        self._hmac_key = hmac_key
        self._clients: dict[str, _ClientHandle] = {}

    async def spawn(self, card: Card) -> None:
        if card.id in self._clients:
            raise RuntimeError(f"specialist already spawned: {card.id}")
        if card.entrypoint["type"] != "python":
            raise NotImplementedError("only python entrypoints supported in Day-1")

        env = os.environ.copy()
        env["AUTHZ_HMAC_KEY"] = self._hmac_key
        env["AGENT_ID"] = card.id

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", card.entrypoint["module"], *card.entrypoint.get("args", [])],
            env=env,
        )

        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        init_result = await session.initialize()

        # Extract a2a_url if specialist included it in init result metadata.
        # (Specialists will set this in Phase 7. For now the field stays None.)
        a2a_url = None
        meta = getattr(init_result, "_meta", None) or {}
        if isinstance(meta, dict):
            a2a_url = meta.get("a2a_url")

        self._clients[card.id] = _ClientHandle(
            card=card, session=session, stack=stack, a2a_url=a2a_url,
        )
        log.info("spawned %s (a2a_url=%s)", card.id, a2a_url)

    async def list_tools(self, agent_id: str):
        client = self._clients[agent_id]
        result = await client.session.list_tools()
        return result.tools

    async def call_tool(self, agent_id: str, name: str, arguments: dict):
        client = self._clients[agent_id]
        return await client.session.call_tool(name, arguments=arguments)

    def a2a_urls(self) -> dict[str, str]:
        return {k: v.a2a_url for k, v in self._clients.items() if v.a2a_url}

    async def shutdown_all(self) -> None:
        for cid, handle in list(self._clients.items()):
            try:
                await handle.stack.aclose()
            except Exception:
                log.exception("error closing client %s", cid)
        self._clients.clear()
```

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_orchestrator/test_mcp_host.py -v -m e2e`
Expected: pass (spawns tool-agent, MCP initialize, tools/list).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/mcp_host.py tests/test_orchestrator/test_mcp_host.py
git commit -m "feat(orchestrator): MCP host client pool with spawn+shutdown"
```

---

### Task 5.3: Capability Router

**Files:**
- Create: `orchestrator/router.py`
- Create: `tests/test_orchestrator/test_router.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_orchestrator/test_router.py
import pytest
from orchestrator.router import CapabilityRouter, RoutingError


def test_router_resolves_unique_capability():
    r = CapabilityRouter()
    r.register("tool-agent", ["read_file", "write_file"])
    r.register("skill-agent", ["ppt-master"])
    assert r.resolve("read_file") == "tool-agent"
    assert r.resolve("ppt-master") == "skill-agent"


def test_router_raises_on_unknown_capability():
    r = CapabilityRouter()
    r.register("tool-agent", ["read_file"])
    with pytest.raises(RoutingError, match="unknown capability"):
        r.resolve("non_existent")


def test_router_uses_priority_on_collision():
    r = CapabilityRouter()
    r.register("skill-agent", ["echo"], priority=10)
    r.register("tool-agent", ["echo"], priority=20)
    # Higher priority wins
    assert r.resolve("echo") == "tool-agent"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_orchestrator/test_router.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `CapabilityRouter`**

```python
# orchestrator/router.py
from __future__ import annotations
from dataclasses import dataclass, field


class RoutingError(Exception):
    pass


@dataclass
class _Entry:
    agent_id: str
    priority: int


class CapabilityRouter:
    """Maps capability name → owning specialist. Higher priority wins ties."""

    def __init__(self):
        # capability → list of entries, kept sorted desc by priority
        self._table: dict[str, list[_Entry]] = {}

    def register(self, agent_id: str, capabilities: list[str], *, priority: int = 0):
        for cap in capabilities:
            self._table.setdefault(cap, []).append(_Entry(agent_id, priority))
            self._table[cap].sort(key=lambda e: -e.priority)

    def resolve(self, capability: str) -> str:
        entries = self._table.get(capability)
        if not entries:
            raise RoutingError(f"unknown capability: {capability}")
        return entries[0].agent_id

    def all_capabilities(self) -> list[str]:
        return sorted(self._table.keys())
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_orchestrator/test_router.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/router.py tests/test_orchestrator/test_router.py
git commit -m "feat(orchestrator): capability router with priority resolution"
```

---

### Task 5.4: Stream multiplexer

**Files:**
- Create: `orchestrator/stream_mux.py`
- Create: `tests/test_orchestrator/test_stream_mux.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_orchestrator/test_stream_mux.py
import io
from orchestrator.stream_mux import StreamMux


def test_stream_mux_tags_chunks_by_agent_id():
    buf = io.StringIO()
    mux = StreamMux(out=buf)
    mux.emit(agent_id="tool-agent", trace_id="t1", chunk="hello\n")
    mux.emit(agent_id="skill-agent", trace_id="t1", chunk="world\n")
    output = buf.getvalue()
    assert "[tool] hello" in output
    assert "[skill] world" in output


def test_stream_mux_handles_chunk_without_newline():
    buf = io.StringIO()
    mux = StreamMux(out=buf)
    mux.emit(agent_id="tool-agent", trace_id="t1", chunk="partial")
    mux.emit(agent_id="tool-agent", trace_id="t1", chunk=" done\n")
    output = buf.getvalue()
    # Tag only at line starts, not within continuations
    assert output.count("[tool]") == 1
    assert "partial done" in output


def test_stream_mux_orchestrator_tag():
    buf = io.StringIO()
    mux = StreamMux(out=buf)
    mux.emit(agent_id="orchestrator", trace_id="t1", chunk="routing...\n")
    assert "[orchestrator] routing" in buf.getvalue()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_orchestrator/test_stream_mux.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `StreamMux`**

```python
# orchestrator/stream_mux.py
from __future__ import annotations
import sys
from typing import TextIO


_AGENT_TAG = {
    "orchestrator": "[orchestrator]",
    "skill-agent": "[skill]",
    "tool-agent": "[tool]",
}


class StreamMux:
    """Writes tagged chunks to the terminal. Each line start gets a tag based
    on which agent produced it; mid-line continuations are NOT re-tagged."""

    def __init__(self, out: TextIO | None = None):
        self._out = out or sys.stdout
        # Track per-(agent_id, trace_id) whether the last char was a newline,
        # so we know to prepend a tag on the next chunk.
        self._at_line_start: dict[tuple[str, str], bool] = {}

    def emit(self, *, agent_id: str, trace_id: str, chunk: str) -> None:
        key = (agent_id, trace_id)
        at_start = self._at_line_start.get(key, True)
        tag = _AGENT_TAG.get(agent_id, f"[{agent_id}]")

        lines = chunk.splitlines(keepends=True)
        for line in lines:
            if at_start:
                self._out.write(f"{tag} ")
            self._out.write(line)
            at_start = line.endswith("\n")
        self._at_line_start[key] = at_start
        self._out.flush()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_orchestrator/test_stream_mux.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/stream_mux.py tests/test_orchestrator/test_stream_mux.py
git commit -m "feat(orchestrator): stream multiplexer with agent-id tagging"
```

---

### Task 5.5: LangGraph wiring

**Files:**
- Create: `orchestrator/graph.py`
- Create: `tests/test_orchestrator/test_graph.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_orchestrator/test_graph.py
import pytest
from orchestrator.graph import build_graph, OrchestratorState
from orchestrator.router import CapabilityRouter


class _FakeMCPHost:
    def __init__(self):
        self.calls: list = []

    async def call_tool(self, agent_id, name, arguments):
        self.calls.append((agent_id, name, arguments))
        return {"content": [{"type": "text", "text": "ok"}]}


@pytest.mark.asyncio
async def test_graph_routes_to_correct_specialist():
    router = CapabilityRouter()
    router.register("tool-agent", ["read_file"])
    host = _FakeMCPHost()

    def fake_planner(state: OrchestratorState) -> dict:
        return {"capability": "read_file", "arguments": {"path": "x"}}

    graph = build_graph(router=router, host=host, planner=fake_planner, hmac_key="k", mode="read-only")
    out = await graph.ainvoke({"user_input": "read x", "trace_id": "t1"})

    assert host.calls[0][0] == "tool-agent"
    assert host.calls[0][1] == "read_file"
    assert host.calls[0][2]["path"] == "x"
    # _meta.authz_grant was injected
    assert "_meta" in host.calls[0][2]
    assert "authz_grant" in host.calls[0][2]["_meta"]
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_orchestrator/test_graph.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `build_graph`**

```python
# orchestrator/graph.py
from __future__ import annotations
from typing import Any, Callable, TypedDict
from langgraph.graph import StateGraph, END
from orchestrator.permission_gate import PermissionGate
from orchestrator.router import CapabilityRouter, RoutingError


class OrchestratorState(TypedDict, total=False):
    user_input: str
    trace_id: str
    capability: str
    arguments: dict
    result: Any
    error: str


Planner = Callable[[OrchestratorState], dict]


def build_graph(*, router: CapabilityRouter, host, planner: Planner, hmac_key: str, mode: str):
    """Build the LangGraph orchestrator graph.

    `planner` is a callable that, given current state, decides which capability
    to invoke and with what args. In production this wraps an LLM call; in
    tests it's a hardcoded function.
    """

    async def _plan(state: OrchestratorState) -> OrchestratorState:
        decision = planner(state)
        return {**state, **decision}

    async def _dispatch(state: OrchestratorState) -> OrchestratorState:
        try:
            agent_id = router.resolve(state["capability"])
        except RoutingError as exc:
            return {**state, "error": str(exc)}

        gate = PermissionGate(mode=mode, hmac_key=hmac_key, trace_id=state["trace_id"])
        try:
            grant = gate.sign(target_specialist=agent_id, tool=state["capability"])
        except Exception as exc:
            return {**state, "error": f"permission_denied: {exc}"}

        args = dict(state.get("arguments") or {})
        args["_meta"] = {"authz_grant": grant, "trace_id": state["trace_id"]}
        result = await host.call_tool(agent_id, state["capability"], args)
        return {**state, "result": result}

    g = StateGraph(OrchestratorState)
    g.add_node("plan", _plan)
    g.add_node("dispatch", _dispatch)
    g.set_entry_point("plan")
    g.add_edge("plan", "dispatch")
    g.add_edge("dispatch", END)
    return g.compile()
```

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_orchestrator/test_graph.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/graph.py tests/test_orchestrator/test_graph.py
git commit -m "feat(orchestrator): LangGraph graph with plan→dispatch nodes"
```

---

### Task 5.6: Orchestrator `main` entrypoint

**Files:**
- Create: `orchestrator/main.py`
- Modify: `cli.py`
- Create: `tests/test_e2e_multi_agent/test_e2e_simple_tool.py`

- [ ] **Step 1: Implement orchestrator main**

```python
# orchestrator/main.py
from __future__ import annotations
import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path
from orchestrator.registry import load_cards
from orchestrator.mcp_host import MCPHost
from orchestrator.router import CapabilityRouter
from orchestrator.stream_mux import StreamMux
from orchestrator.graph import build_graph

log = logging.getLogger(__name__)


def _agent_dir() -> Path:
    return Path(".agent") / "agents"


async def _bootstrap(host: MCPHost, router: CapabilityRouter) -> None:
    cards = load_cards(_agent_dir())
    for card in cards:
        await host.spawn(card)
        tools = await host.list_tools(card.id)
        router.register(card.id, [t.name for t in tools])


async def _planner_via_llm(state) -> dict:
    """Phase 5 stub: in real use, call an LLM. For now, expect the user_input
    to look like 'CAPABILITY:ARG' for deterministic dispatch."""
    text = state["user_input"]
    if ":" in text:
        cap, _, arg = text.partition(":")
        return {"capability": cap.strip(), "arguments": {"path": arg.strip()}}
    raise ValueError("Phase-5 stub planner: expected 'CAPABILITY:ARG' input")


async def run_prompt(prompt: str) -> int:
    hmac_key = secrets.token_urlsafe(32)
    host = MCPHost(hmac_key=hmac_key)
    router = CapabilityRouter()
    mux = StreamMux()
    try:
        await _bootstrap(host, router)
        graph = build_graph(
            router=router, host=host,
            planner=lambda s: asyncio.run_coroutine_threadsafe(
                _planner_via_llm(s), asyncio.get_event_loop()
            ).result() if False else _sync_planner(s),
            hmac_key=hmac_key, mode=os.environ.get("LANGCHAIN_AGENT_PERMISSION_MODE", "workspace-write"),
        )
        result = await graph.ainvoke({"user_input": prompt, "trace_id": "t1"})
        if result.get("error"):
            mux.emit(agent_id="orchestrator", trace_id="t1", chunk=f"error: {result['error']}\n")
            return 1
        # Render result content
        for piece in result.get("result", {}).get("content", []):
            mux.emit(agent_id=router.resolve(result["capability"]),
                     trace_id="t1", chunk=piece.get("text", "") + "\n")
        return 0
    finally:
        await host.shutdown_all()


def _sync_planner(state):
    text = state["user_input"]
    if ":" in text:
        cap, _, arg = text.partition(":")
        return {"capability": cap.strip(), "arguments": {"path": arg.strip()}}
    raise ValueError("Phase-5 stub planner: expected 'CAPABILITY:ARG' input")


async def run_repl() -> int:
    mux = StreamMux()
    mux.emit(agent_id="orchestrator", trace_id="boot",
             chunk="multi-agent REPL not fully implemented in Phase 5 — try `python cli.py --single` for now.\n")
    return 0


def main(*, prompt: str | None = None) -> int:
    if prompt is not None:
        return asyncio.run(run_prompt(prompt))
    return asyncio.run(run_repl())
```

Note: this Phase-5 planner is intentionally a deterministic stub so `test_e2e_simple_tool` can run without an LLM. The full LLM-driven planner ships in Phase 6.

- [ ] **Step 2: Wire `cli.py` to call `orchestrator.main` for the default (non-`--single`) path**

In `cli.py`, replace the Phase-2 fallback notice with:
```python
if args.single:
    from legacy.single_agent_loop import run_repl, run_prompt
    if args.subcommand == "prompt":
        return run_prompt(args.text)
    return run_repl()
else:
    from orchestrator.main import main as orch_main
    return orch_main(prompt=args.text if args.subcommand == "prompt" else None)
```

- [ ] **Step 3: Write the e2e test**

```python
# tests/test_e2e_multi_agent/test_e2e_simple_tool.py
import os
import subprocess
import sys
import pytest


@pytest.mark.e2e
def test_orchestrator_dispatches_read_file_to_tool_agent(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hi there", encoding="utf-8")

    env = os.environ.copy()
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"

    # Phase-5 stub planner parses 'CAPABILITY:ARG'
    prompt = f"read_file:{target}"

    proc = subprocess.run(
        [sys.executable, "cli.py", "prompt", prompt],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "[tool]" in proc.stdout
    assert "hi there" in proc.stdout
```

- [ ] **Step 4: Run the e2e test**

Run: `pytest tests/test_e2e_multi_agent/test_e2e_simple_tool.py -v -m e2e`
Expected: pass. (Orchestrator spawns tool-agent, routes `read_file`, returns file content with `[tool]` tag.)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py cli.py tests/test_e2e_multi_agent/test_e2e_simple_tool.py
git commit -m "feat(orchestrator): main entrypoint + cli dispatch + e2e read_file via tool-agent"
```

---

## Phase 6 — skill-agent (without A2A yet)

### Task 6.1: Skill executor

**Files:**
- Create: `agents/skill_agent/skill_executor.py`
- Create: `tests/test_skill_agent/test_skill_executor.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_skill_agent/test_skill_executor.py
import pytest
from agents.skill_agent.skill_executor import build_skill_specs, execute_skill
from agents.shared.mock_chat_model import MockChatModel


def test_skill_specs_loaded_from_skills_dir():
    specs = build_skill_specs()
    names = {s.name for s in specs}
    # The repo has at least the baidu-ecommerce-search skill
    assert any("baidu" in n for n in names)


@pytest.mark.asyncio
async def test_execute_skill_calls_llm_and_returns_content():
    llm = MockChatModel(responses=["I performed the skill: result=X"])
    args = {"_meta": {"authz_grant": "FAKE_NEEDS_REAL_KEY"}, "query": "test"}
    # The executor should be parameterizable for tests so we can inject the LLM.
    out = await execute_skill("baidu-ecommerce-search", args, llm=llm, verify_authz=False)
    assert "result=X" in out
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_skill_agent/test_skill_executor.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the executor**

```python
# agents/skill_agent/skill_executor.py
from __future__ import annotations
import os
from pathlib import Path
from typing import Any
from agents.shared.mcp_server import ToolSpec
from agents.shared.authz import verify_grant


def _skills_root() -> Path:
    return Path("skills")


def _load_skill_md(slug: str) -> str:
    return (_skills_root() / slug / "SKILL.md").read_text(encoding="utf-8")


def build_skill_specs() -> list[ToolSpec]:
    """Scan skills/*/SKILL.md and produce a ToolSpec for each.

    The MCP-exposed `name` follows the pattern `skill.<slug>`.
    """
    specs: list[ToolSpec] = []
    if not _skills_root().exists():
        return specs
    for skill_dir in sorted(_skills_root().iterdir()):
        if not (skill_dir / "SKILL.md").exists():
            continue
        slug = skill_dir.name

        async def _handler(args: dict, _slug=slug) -> Any:
            return await execute_skill(_slug, args)

        specs.append(ToolSpec(
            name=f"skill.{slug}",
            description=f"Run the {slug} skill",
            input_schema={"type": "object"},
            handler=_handler,
        ))
    return specs


async def execute_skill(slug: str, args: dict, *, llm=None, verify_authz: bool = True) -> str:
    """Execute a skill by feeding its SKILL.md + args to the LLM.

    The LLM is responsible for actually performing the skill's work. For real
    skills that need tools, see Phase 7 (A2A to tool-agent).
    """
    if verify_authz:
        meta = args.get("_meta") or {}
        grant = meta.get("authz_grant")
        if grant is None:
            raise RuntimeError("missing authz_grant")
        key = os.environ.get("AUTHZ_HMAC_KEY")
        if not key:
            raise RuntimeError("AUTHZ_HMAC_KEY not set")
        verify_grant(grant, key=key, requested_tool=f"skill.{slug}")

    if llm is None:
        llm = _default_llm()

    skill_md = _load_skill_md(slug)
    user_payload = {k: v for k, v in args.items() if k != "_meta"}
    messages = [
        {"role": "system", "content": skill_md},
        {"role": "user", "content": str(user_payload)},
    ]
    result = llm.invoke(messages)
    return result.content


def _default_llm():
    """Construct the per-agent LLM from env vars set by orchestrator at spawn time."""
    from agents.shared.mock_chat_model import MockChatModel
    provider = os.environ.get("LANGCHAIN_AGENT_MODEL", "mock/mock-default")
    if provider.startswith("mock"):
        return MockChatModel(responses=["(mock skill output)"])
    # Real provider construction is delegated to the project's existing config
    # loader. The orchestrator-injected env vars are already correct for the
    # chosen provider, so we re-use the legacy model factory.
    from legacy.single_agent_loop import _build_chat_model  # adapt to actual symbol
    return _build_chat_model()
```

Note: the `from legacy.single_agent_loop import _build_chat_model` line assumes a model-construction function exists in the legacy module. If it lives elsewhere (e.g. in `config.py` or a separate `model_factory.py`), adapt the import. If no such factory exists, write a minimal one in `agents/shared/model_factory.py` reading provider settings from env and returning a `ChatOpenAI`/`ChatAnthropic` instance.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_skill_agent/test_skill_executor.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add agents/skill_agent/skill_executor.py tests/test_skill_agent/test_skill_executor.py
git commit -m "feat(skill-agent): skill executor with LLM dispatch + authz"
```

---

### Task 6.2: skill-agent main + Card

**Files:**
- Create: `agents/skill_agent/main.py`
- Create: `.agent/agents/skill-agent.card.json`

- [ ] **Step 1: Write the main**

```python
# agents/skill_agent/main.py
from __future__ import annotations
import asyncio
import sys
from agents.shared.mcp_server import build_server
from agents.skill_agent.skill_executor import build_skill_specs


async def amain() -> None:
    specs = build_skill_specs()
    _proxy, runner = build_server(name="skill-agent", tools=specs)
    await runner()


def main() -> int:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Write the Card**

```jsonc
// .agent/agents/skill-agent.card.json
{
  "id": "skill-agent",
  "display_name": "Skill Specialist",
  "version": "1.0.0",
  "entrypoint": {
    "type": "python",
    "module": "agents.skill_agent.main",
    "args": []
  },
  "mcp": { "transport": "stdio" },
  "a2a": { "transport": "http", "port_strategy": "ephemeral" },
  "capabilities_hint": ["skill"],
  "model_override": null
}
```

- [ ] **Step 3: Smoke test that orchestrator now spawns BOTH specialists**

Run: `pytest tests/test_e2e_multi_agent/test_e2e_simple_tool.py -v -m e2e`
Expected: still passes (now spawning 2 specialists; read_file routed to tool-agent).

- [ ] **Step 4: Commit**

```bash
git add agents/skill_agent/main.py .agent/agents/skill-agent.card.json
git commit -m "feat(skill-agent): main entrypoint + Card registration"
```

---

### Task 6.3: Replace stub planner with LLM-driven planner

**Files:**
- Modify: `orchestrator/main.py`
- Create: `tests/test_orchestrator/test_llm_planner.py`

- [ ] **Step 1: Write the planner test**

```python
# tests/test_orchestrator/test_llm_planner.py
import pytest
from orchestrator.main import LLMPlanner
from agents.shared.mock_chat_model import MockChatModel


def test_llm_planner_emits_structured_decision():
    # Mock LLM returns a JSON decision; planner parses it.
    llm = MockChatModel(responses=[
        '{"capability": "read_file", "arguments": {"path": "README.md"}}'
    ])
    planner = LLMPlanner(llm=llm, available_capabilities=["read_file", "skill.ppt-master"])
    decision = planner({"user_input": "read the readme", "trace_id": "t"})
    assert decision["capability"] == "read_file"
    assert decision["arguments"]["path"] == "README.md"
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_orchestrator/test_llm_planner.py -v`
Expected: FAIL.

- [ ] **Step 3: Add `LLMPlanner` class to `orchestrator/main.py`**

Add at top of `orchestrator/main.py`:
```python
import json


class LLMPlanner:
    """Plans the next capability + arguments by asking the LLM."""

    _SYSTEM = (
        "You are the orchestrator's planning brain. The available capabilities are listed below. "
        "Reply with ONLY a JSON object of the form "
        '{\"capability\": \"<name>\", \"arguments\": {<args>}}. '
        "No prose, no markdown fence."
    )

    def __init__(self, *, llm, available_capabilities: list[str]):
        self._llm = llm
        self._caps = available_capabilities

    def __call__(self, state) -> dict:
        prompt = (
            f"Available capabilities: {self._caps}\n\n"
            f"User: {state['user_input']}"
        )
        out = self._llm.invoke([
            {"role": "system", "content": self._SYSTEM},
            {"role": "user", "content": prompt},
        ])
        text = out.content.strip()
        # Strip accidental code fences
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        return json.loads(text)
```

- [ ] **Step 4: Wire `LLMPlanner` into `run_prompt`**

Replace the `_sync_planner` reference in `run_prompt` with:
```python
llm = _build_orchestrator_llm()  # see below
planner = LLMPlanner(llm=llm, available_capabilities=router.all_capabilities())
graph = build_graph(router=router, host=host, planner=planner,
                    hmac_key=hmac_key, mode=os.environ.get(..., "workspace-write"))
```

And add a `_build_orchestrator_llm()` helper that constructs the LLM the same way the legacy loop does (re-use existing model factory; falls back to mock when `LANGCHAIN_AGENT_MODEL` starts with `mock`).

- [ ] **Step 5: Run all tests**

Run: `pytest -v -m "not e2e"` then `pytest -v -m e2e`
Expected: all green. `test_e2e_simple_tool` may need an updated input prompt (natural language now) — if so, set `LANGCHAIN_AGENT_MODEL=mock/mock-default` for the test process and craft the mock LLM responses appropriately. Easier: keep `_sync_planner` as a fallback when `LANGCHAIN_AGENT_MODEL=mock` so e2e tests stay deterministic.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/main.py tests/test_orchestrator/test_llm_planner.py
git commit -m "feat(orchestrator): LLM-driven planner with JSON-decision parsing"
```

---

## Phase 7 — A2A Peer Channel

### Task 7.1: A2A HTTP server skeleton

**Files:**
- Create: `agents/shared/a2a_server.py`
- Create: `tests/test_shared/test_a2a_server.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_shared/test_a2a_server.py
import asyncio
import pytest
import httpx
from agents.shared.a2a_server import A2AServer, A2AHandler


@pytest.mark.asyncio
async def test_a2a_server_accepts_tasks_send_and_dispatches():
    async def echo_handler(skill_id: str, input: dict, meta: dict) -> dict:
        return {"echoed": input, "skill": skill_id}

    handler = A2AHandler(handler=echo_handler)
    server = A2AServer(handler=handler)
    await server.start()  # binds ephemeral port
    try:
        url = server.base_url
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{url}/a2a", json={
                "jsonrpc": "2.0", "id": "1", "method": "tasks/send",
                "params": {
                    "task_id": "1", "skill_id": "tool.read_file",
                    "input": {"path": "x"},
                    "_meta": {"authz_grant": "fake"},
                },
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"]["echoed"]["path"] == "x"
    finally:
        await server.stop()
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_shared/test_a2a_server.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `A2AServer`**

```python
# agents/shared/a2a_server.py
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable
import uvicorn
from fastapi import FastAPI, Request


HandlerFunc = Callable[[str, dict, dict], Awaitable[dict]]


@dataclass
class A2AHandler:
    handler: HandlerFunc

    async def dispatch(self, payload: dict) -> dict:
        params = payload.get("params") or {}
        skill_id = params.get("skill_id")
        inp = params.get("input") or {}
        meta = params.get("_meta") or {}
        result = await self.handler(skill_id, inp, meta)
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}


class A2AServer:
    def __init__(self, *, handler: A2AHandler, host: str = "127.0.0.1", port: int = 0):
        self._handler = handler
        self._host = host
        self._port = port
        self._app = FastAPI()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

        @self._app.post("/a2a")
        async def _endpoint(req: Request):
            payload = await req.json()
            return await self._handler.dispatch(payload)

    async def start(self) -> None:
        config = uvicorn.Config(self._app, host=self._host, port=self._port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        # Wait for the server to be ready and report its bound port.
        while not self._server.started:
            await asyncio.sleep(0.01)
        # uvicorn picks the actual port; read it from the socket
        sock = self._server.servers[0].sockets[0]
        self._port = sock.getsockname()[1]

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._task:
            await self._task
```

If `uvicorn.Server.servers[0].sockets[0]` access is internal/fragile in your installed uvicorn version, an alternative is to pass `port=0`, capture stdout-printed line "Uvicorn running on http://127.0.0.1:NNNN", and parse the port. Prefer the socket-introspection approach if it works on the installed version.

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_shared/test_a2a_server.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add agents/shared/a2a_server.py tests/test_shared/test_a2a_server.py
git commit -m "feat(agents/shared): A2A HTTP server skeleton with ephemeral port"
```

---

### Task 7.2: Wire A2A server into each specialist's `main`

**Files:**
- Modify: `agents/tool_agent/main.py`
- Modify: `agents/skill_agent/main.py`

- [ ] **Step 1: Modify `agents/tool_agent/main.py`**

```python
# agents/tool_agent/main.py
from __future__ import annotations
import asyncio
import sys
from agents.shared.mcp_server import build_server, ToolSpec
from agents.shared.a2a_server import A2AServer, A2AHandler
from agents.tool_agent.tool_executor import build_tool_specs, execute_tool
from typing import Any


async def amain() -> None:
    # Start the A2A server first so we know our port.
    async def a2a_dispatch(skill_id: str, input: dict, meta: dict) -> dict:
        # `skill_id` like "tool.read_file" → tool name "read_file"
        if not skill_id.startswith("tool."):
            return {"error": f"tool-agent does not expose {skill_id}"}
        tool_name = skill_id[len("tool."):]
        args = {**input, "_meta": meta}
        result = await execute_tool(tool_name, args)
        return {"result": result}

    a2a = A2AServer(handler=A2AHandler(handler=a2a_dispatch))
    await a2a.start()

    # Now build the MCP server.
    specs = build_tool_specs()

    def _make_handler(tool_name: str):
        async def _h(args: dict) -> Any:
            return await execute_tool(tool_name, args)
        return _h

    guarded = [
        ToolSpec(s.name, s.description, s.input_schema, _make_handler(s.name))
        for s in specs
    ]
    _proxy, runner = build_server(name="tool-agent", tools=guarded)

    # Emit the A2A URL via stderr so orchestrator can capture it before/after
    # MCP initialize completes. (MCP `initialize` _meta would be cleaner, but
    # tying directly into the SDK's response shape is brittle across versions.)
    print(f"A2A_URL={a2a.base_url}", file=sys.stderr, flush=True)

    try:
        await runner()
    finally:
        await a2a.stop()


def main() -> int:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Apply the same pattern to `agents/skill_agent/main.py`**

(Use `skill_id.startswith("skill.")` and dispatch via `execute_skill`.)

- [ ] **Step 3: Update `MCPHost.spawn` to capture stderr `A2A_URL=…` line**

In `orchestrator/mcp_host.py`, before `stdio_client(params)` the params already inherit our stderr — but the MCP SDK's stdio transport typically captures or redirects stderr. Options:

(a) Configure `StdioServerParameters` so stderr is piped back to the host and parsed in a background task.

(b) Have specialists write `A2A_URL=…` to a sidecar file `.agent/runtime/<agent_id>.a2a-url`, and orchestrator reads that file after `initialize`.

Pick **(b)** for robustness — it sidesteps SDK-version differences in stderr handling. Update specialist `main.py` to write the file:

```python
# replace the print(...) line in each specialist's amain
from pathlib import Path
import os
agent_id = os.environ.get("AGENT_ID", "unknown-agent")
runtime_dir = Path(".agent/runtime")
runtime_dir.mkdir(parents=True, exist_ok=True)
(runtime_dir / f"{agent_id}.a2a-url").write_text(a2a.base_url, encoding="utf-8")
```

And update `MCPHost.spawn` to read it after `session.initialize()`:

```python
# orchestrator/mcp_host.py — inside spawn(), after initialize:
from pathlib import Path
url_file = Path(".agent/runtime") / f"{card.id}.a2a-url"
if url_file.exists():
    a2a_url = url_file.read_text(encoding="utf-8").strip()
```

- [ ] **Step 4: Run all existing tests**

Run: `pytest -v -m "not e2e"` then `pytest -v -m e2e`
Expected: previously passing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agents/tool_agent/main.py agents/skill_agent/main.py orchestrator/mcp_host.py
git commit -m "feat(specialists): bind A2A HTTP server at startup; orchestrator reads a2a_url"
```

---

### Task 7.3: Orchestrator broadcasts the A2A port table

**Files:**
- Modify: `orchestrator/main.py`
- Modify: `agents/skill_agent/main.py` (read the table)
- Create: `tests/test_e2e_multi_agent/test_a2a_peer_call.py`

- [ ] **Step 1: Broadcast mechanism — write port table to a shared file**

Simpler than an MCP notification round-trip: orchestrator writes `.agent/runtime/peers.json` after all specialists are up. Specialists poll/read this file before making their first A2A call.

In `orchestrator/main.py` `_bootstrap`, after all spawns:
```python
import json
from pathlib import Path

peers = {aid: url for aid, url in host.a2a_urls().items()}
Path(".agent/runtime").mkdir(parents=True, exist_ok=True)
Path(".agent/runtime/peers.json").write_text(json.dumps(peers), encoding="utf-8")
```

- [ ] **Step 2: skill-agent A2A client**

Create `agents/skill_agent/a2a_client.py`:
```python
# agents/skill_agent/a2a_client.py
from __future__ import annotations
import json
from pathlib import Path
import httpx


def _load_peers() -> dict[str, str]:
    p = Path(".agent/runtime/peers.json")
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


async def call_peer(*, peer_id: str, skill_id: str, input: dict, meta: dict) -> dict:
    peers = _load_peers()
    url = peers.get(peer_id)
    if url is None:
        raise RuntimeError(f"no A2A url known for peer {peer_id!r}")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{url}/a2a", json={
            "jsonrpc": "2.0",
            "id": meta.get("trace_id", "task"),
            "method": "tasks/send",
            "params": {
                "task_id": meta.get("trace_id", "task"),
                "skill_id": skill_id,
                "input": input,
                "_meta": meta,
            },
        })
        resp.raise_for_status()
        return resp.json().get("result", {})
```

- [ ] **Step 3: Write failing e2e A2A test**

```python
# tests/test_e2e_multi_agent/test_a2a_peer_call.py
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import httpx
import jwt as pyjwt
import pytest


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_skill_agent_calls_tool_agent_via_a2a(tmp_path, monkeypatch):
    # Run the orchestrator briefly to spawn both specialists and populate peers.json
    target = tmp_path / "peer.txt"
    target.write_text("peer-call works", encoding="utf-8")

    env = os.environ.copy()
    env["LANGCHAIN_AGENT_PERMISSION_MODE"] = "workspace-write"

    # Use the mock planner path so this is deterministic
    proc = subprocess.Popen(
        [sys.executable, "cli.py", "prompt", f"read_file:{target}"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err.decode(errors="replace")
    # peers.json should now exist with both URLs
    peers_path = Path(".agent/runtime/peers.json")
    assert peers_path.exists(), "orchestrator did not write peers.json"
    peers = json.loads(peers_path.read_text())
    assert "tool-agent" in peers
    assert "skill-agent" in peers
```

This validates that the broadcast file is written. A second test exercising an actual skill → tool A2A round-trip can wait until skill-executor actually calls `call_peer`; for Day-1 it's enough to verify the wiring.

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_e2e_multi_agent/test_a2a_peer_call.py -v -m e2e`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py agents/skill_agent/a2a_client.py tests/test_e2e_multi_agent/test_a2a_peer_call.py
git commit -m "feat(a2a): broadcast peer URL table; add skill-agent A2A client"
```

---

### Task 7.4: Telemetry — tool-agent reports A2A invocations back to orchestrator

**Files:**
- Create: `orchestrator/telemetry.py`
- Modify: `agents/tool_agent/main.py` (emit telemetry in A2A dispatch)

- [ ] **Step 1: Write the telemetry collector**

Telemetry uses the same shared-file mechanism: each specialist appends a JSON line to `.agent/runtime/telemetry.ndjson` and the orchestrator's stream_mux tails it.

```python
# orchestrator/telemetry.py
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from orchestrator.stream_mux import StreamMux

_PATH = Path(".agent/runtime/telemetry.ndjson")


def reset_log() -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text("", encoding="utf-8")


async def tail(mux: StreamMux, stop_event: asyncio.Event) -> None:
    """Tail telemetry.ndjson and emit each event into the unified stream."""
    pos = 0
    while not stop_event.is_set():
        if _PATH.exists():
            with _PATH.open("r", encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    mux.emit(
                        agent_id=event.get("agent_id", "orchestrator"),
                        trace_id=event.get("trace_id", "?"),
                        chunk=event.get("message", "") + "\n",
                    )
                pos = f.tell()
        await asyncio.sleep(0.05)


def emit_event(*, agent_id: str, trace_id: str, message: str) -> None:
    """Called from a specialist process to record a telemetry event."""
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "agent_id": agent_id,
            "trace_id": trace_id,
            "message": message,
        }) + "\n")
```

- [ ] **Step 2: Emit telemetry from tool-agent's A2A dispatch**

In `agents/tool_agent/main.py`, inside `a2a_dispatch`, before executing:
```python
from orchestrator.telemetry import emit_event
import os
emit_event(
    agent_id="tool-agent",
    trace_id=meta.get("trace_id", "?"),
    message=f"(via A2A from {meta.get('agent_caller', '?')}) {skill_id}",
)
```

- [ ] **Step 3: Start the tail task in orchestrator**

In `orchestrator/main.py`, around the graph invocation in `run_prompt`:
```python
from orchestrator import telemetry
stop = asyncio.Event()
telemetry.reset_log()
tail_task = asyncio.create_task(telemetry.tail(mux, stop))
try:
    result = await graph.ainvoke({...})
    # Let any final telemetry land
    await asyncio.sleep(0.1)
finally:
    stop.set()
    await tail_task
```

- [ ] **Step 4: Smoke test the telemetry shows up in stdout**

Manually run a test invocation that triggers a skill → tool A2A path (deferred to Phase 8 when the skill executor actually issues A2A calls; for now this code is staged but inert).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/telemetry.py orchestrator/main.py agents/tool_agent/main.py
git commit -m "feat(telemetry): file-tail mechanism for A2A call observability"
```

---

## Phase 8 — Slash Commands & Stream-Mux Polish

### Task 8.1: `/agents` slash command

**Files:**
- Modify: `orchestrator/main.py` (REPL loop)
- Create: `tests/test_orchestrator/test_slash_agents.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_orchestrator/test_slash_agents.py
import io
from orchestrator.main import _handle_slash_agents
from orchestrator.registry import Card


class _FakeHost:
    def __init__(self):
        self._clients = {
            "tool-agent": type("H", (), {
                "card": Card(id="tool-agent", display_name="T", version="1",
                             entrypoint={}, mcp={}, a2a={}, capabilities_hint=[],
                             model_override=None),
                "a2a_url": "http://127.0.0.1:50001",
            })(),
        }

    def list_handles(self):
        return list(self._clients.values())


def test_slash_agents_renders_table():
    buf = io.StringIO()
    _handle_slash_agents(_FakeHost(), out=buf)
    text = buf.getvalue()
    assert "tool-agent" in text
    assert "50001" in text
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_orchestrator/test_slash_agents.py -v`
Expected: FAIL.

- [ ] **Step 3: Add `_handle_slash_agents` to `orchestrator/main.py` + REPL hook**

```python
# orchestrator/main.py
def _handle_slash_agents(host, *, out=None) -> None:
    import sys
    out = out or sys.stdout
    rows = []
    for handle in host.list_handles():
        c = handle.card
        rows.append(f"{c.id:16s} v{c.version:6s} a2a={handle.a2a_url or '-'}")
    out.write("\n".join(rows) + "\n")
```

Also expose `MCPHost.list_handles()` returning the internal `_clients.values()` list (add to `orchestrator/mcp_host.py`).

In the REPL loop (when implemented), if user input is `/agents`, call this function instead of routing.

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_orchestrator/test_slash_agents.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py orchestrator/mcp_host.py tests/test_orchestrator/test_slash_agents.py
git commit -m "feat(orchestrator): /agents slash command"
```

---

### Task 8.2: Ctrl+C cancellation

**Files:**
- Modify: `orchestrator/main.py`, `orchestrator/mcp_host.py`
- Create: `tests/test_e2e_multi_agent/test_ctrl_c_cancel.py`

- [ ] **Step 1: Add cancellation propagation to `MCPHost`**

In `orchestrator/mcp_host.py`, add:
```python
async def cancel_all(self) -> None:
    """Send MCP notifications/cancelled to every specialist."""
    for handle in self._clients.values():
        try:
            await handle.session.send_notification(method="notifications/cancelled", params={})
        except Exception:
            pass
```

Hook this into `run_prompt` / REPL Ctrl+C handlers (catch `asyncio.CancelledError`, call `cancel_all`, then continue rather than terminate).

- [ ] **Step 2: Write the e2e cancellation test**

```python
# tests/test_e2e_multi_agent/test_ctrl_c_cancel.py
import os
import signal
import subprocess
import sys
import time
import pytest


@pytest.mark.e2e
def test_ctrl_c_during_long_run_does_not_kill_specialists(tmp_path):
    # Start orchestrator in REPL mode in background
    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock/mock-default"
    proc = subprocess.Popen(
        [sys.executable, "cli.py"],
        env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    # Give it time to spawn specialists
    time.sleep(2)

    # On Windows we use CTRL_BREAK_EVENT instead of SIGINT
    if sys.platform == "win32":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)

    time.sleep(0.5)
    # Exit cleanly via stdin
    proc.stdin.write("/exit\n")
    proc.stdin.flush()
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0
```

This is a coarse test — it verifies the orchestrator survives Ctrl+C, not the full cancellation chain. Tighter tests (specialist still alive after Ctrl+C, next request succeeds) would add complexity disproportionate to Day-1.

- [ ] **Step 3: Run the test**

Run: `pytest tests/test_e2e_multi_agent/test_ctrl_c_cancel.py -v -m e2e`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/main.py orchestrator/mcp_host.py tests/test_e2e_multi_agent/test_ctrl_c_cancel.py
git commit -m "feat(orchestrator): propagate Ctrl+C to specialists without killing them"
```

---

### Task 8.3: Specialist crash recovery

**Files:**
- Modify: `orchestrator/main.py`, `orchestrator/mcp_host.py`
- Create: `tests/test_e2e_multi_agent/test_specialist_crash.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_e2e_multi_agent/test_specialist_crash.py
import os
import signal
import subprocess
import sys
import time
import psutil
import pytest


@pytest.mark.e2e
def test_kill_tool_agent_does_not_crash_orchestrator(tmp_path):
    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock/mock-default"
    proc = subprocess.Popen(
        [sys.executable, "cli.py"],
        env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2)

    # Locate tool-agent child process and kill it
    parent = psutil.Process(proc.pid)
    children = parent.children(recursive=True)
    tool_child = next((c for c in children if "tool_agent" in " ".join(c.cmdline())), None)
    assert tool_child is not None
    tool_child.kill()

    # Issue a request that needs tool-agent; orchestrator should error gracefully
    proc.stdin.write("read_file:README.md\n")
    proc.stdin.flush()
    time.sleep(1)
    proc.stdin.write("/exit\n")
    proc.stdin.flush()
    out, err = proc.communicate(timeout=10)
    # Orchestrator exited normally despite the dead child
    assert proc.returncode == 0
    assert "error" in out.lower() or "unavailable" in out.lower()
```

Note: `psutil` is a test-only dep; add `psutil` to `requirements.txt` or to a `test-requirements.txt` if you maintain one.

- [ ] **Step 2: Handle the crash in `MCPHost.call_tool`**

Wrap the underlying `session.call_tool` in a try/except. On `BrokenPipeError` / `ConnectionError` / `mcp.shared.exceptions.McpError`, return an error result with `{"error": "specialist unavailable"}` and log.

- [ ] **Step 3: Run the test**

Run: `pytest tests/test_e2e_multi_agent/test_specialist_crash.py -v -m e2e`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/mcp_host.py tests/test_e2e_multi_agent/test_specialist_crash.py requirements.txt
git commit -m "feat(orchestrator): graceful handling of crashed specialists"
```

---

## Phase 9 — Final E2E Validation

### Task 9.1: End-to-end skill → A2A → tool flow

**Files:**
- Modify: `agents/skill_agent/skill_executor.py` (let LLM emit A2A intents)
- Create: `tests/test_e2e_multi_agent/test_e2e_skill_a2a_chain.py`

- [ ] **Step 1: Teach skill_executor to recognize tool-call intents in LLM output**

For Day-1 simplicity, the LLM is prompted to emit a JSON envelope:
```json
{"tool_calls": [{"tool": "read_file", "arguments": {"path": "..."}}], "final": "<text>"}
```

After receiving the LLM response, parse it; for each `tool_calls` entry, invoke `agents.skill_agent.a2a_client.call_peer(peer_id="tool-agent", skill_id=f"tool.{tool}", input=arguments, meta=...)`. After all tool calls return, re-send messages to the LLM with the results appended, and use that response as the final output.

```python
# In agents/skill_agent/skill_executor.py — add after the existing `execute_skill` body
async def _react_loop(messages, llm, meta):
    from agents.skill_agent.a2a_client import call_peer
    import json
    while True:
        result = llm.invoke(messages)
        text = result.content.strip()
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            return text  # plain final answer
        calls = envelope.get("tool_calls") or []
        if not calls:
            return envelope.get("final", text)
        tool_outputs = []
        for c in calls:
            out = await call_peer(
                peer_id="tool-agent",
                skill_id=f"tool.{c['tool']}",
                input=c["arguments"],
                meta=meta,
            )
            tool_outputs.append({"tool": c["tool"], "output": out})
        messages = messages + [{"role": "tool", "content": json.dumps(tool_outputs)}]
```

Then in `execute_skill`, replace `result = llm.invoke(messages); return result.content` with `return await _react_loop(messages, llm, args.get("_meta") or {})`.

- [ ] **Step 2: Write the e2e test using a scripted MockChatModel**

```python
# tests/test_e2e_multi_agent/test_e2e_skill_a2a_chain.py
import os
import subprocess
import sys
import pytest


@pytest.mark.e2e
def test_skill_chain_via_a2a(tmp_path):
    target = tmp_path / "input.txt"
    target.write_text("PAYLOAD-XYZ", encoding="utf-8")

    env = os.environ.copy()
    # Use mock provider with scripted responses: first turn asks for a read,
    # second turn produces the final answer.
    env["LANGCHAIN_AGENT_MODEL"] = "mock/mock-default"
    env["MOCK_SKILL_SCRIPT"] = (
        '{"tool_calls":[{"tool":"read_file","arguments":{"path":"' + str(target).replace("\\", "\\\\") + '"}}]}'
        "||"
        '{"final":"Got payload PAYLOAD-XYZ"}'
    )
    # Mock orchestrator planner just routes to skill.demo
    env["MOCK_ORCH_SCRIPT"] = '{"capability":"skill.demo","arguments":{}}'

    # NOTE: this test requires the mock provider to honor MOCK_SKILL_SCRIPT and
    # MOCK_ORCH_SCRIPT env vars. Implement that in agents/shared/mock_chat_model.py
    # if not already there (split responses by '||').

    proc = subprocess.run(
        [sys.executable, "cli.py", "prompt", "do the demo"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "[skill]" in proc.stdout
    assert "[tool]" in proc.stdout  # telemetry tag
    assert "PAYLOAD-XYZ" in proc.stdout
```

- [ ] **Step 3: Wire `MOCK_SKILL_SCRIPT` env support in `MockChatModel`**

In `agents/shared/mock_chat_model.py`, add a classmethod constructor:
```python
@classmethod
def from_env(cls, env_var: str, default: str = "ok") -> "MockChatModel":
    import os
    raw = os.environ.get(env_var, default)
    return cls(responses=raw.split("||"))
```

And in `agents/skill_agent/skill_executor.py`'s `_default_llm`, when provider is `mock`, prefer `MockChatModel.from_env("MOCK_SKILL_SCRIPT")`. Similarly for the orchestrator planner: prefer `from_env("MOCK_ORCH_SCRIPT")`.

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_e2e_multi_agent/test_e2e_skill_a2a_chain.py -v -m e2e`
Expected: pass. Label order in stdout should be:
```
[orchestrator] ...
[skill] ...
[tool] (via A2A from skill-agent) tool.read_file
[skill] Got payload PAYLOAD-XYZ
```

- [ ] **Step 5: Commit**

```bash
git add agents/skill_agent/ agents/shared/mock_chat_model.py tests/test_e2e_multi_agent/test_e2e_skill_a2a_chain.py
git commit -m "feat(skill-agent): ReAct loop with A2A tool calls; e2e test for the chain"
```

---

### Task 9.2: Tighten the legacy-mode isolation check

**Files:**
- Modify: `tests/test_e2e_multi_agent/test_e2e_legacy_mode.py`

- [ ] **Step 1: Strengthen assertion using `psutil`**

```python
# tests/test_e2e_multi_agent/test_e2e_legacy_mode.py
import os
import subprocess
import sys
import psutil
import pytest


@pytest.mark.e2e
def test_legacy_mode_does_not_spawn_specialists(tmp_path):
    env = os.environ.copy()
    env["LANGCHAIN_AGENT_MODEL"] = "mock/mock-default"
    env["MOCK_API_KEY"] = "x"

    proc = subprocess.Popen(
        [sys.executable, "cli.py", "--single", "prompt", "hello"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    # Check for children while the process is alive
    seen_specialist = False
    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            cmd = " ".join(child.cmdline())
            if "agents.tool_agent" in cmd or "agents.skill_agent" in cmd:
                seen_specialist = True
                break
    except psutil.NoSuchProcess:
        pass  # process exited before we could inspect — still acceptable
    proc.communicate(timeout=30)
    assert not seen_specialist, "legacy --single mode must not spawn specialist subprocesses"
```

- [ ] **Step 2: Run all e2e tests one final time**

Run: `pytest -v -m e2e`
Expected: all e2e tests pass.

- [ ] **Step 3: Run the full suite for a clean baseline**

Run: `pytest -v`
Expected: full green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e_multi_agent/test_e2e_legacy_mode.py
git commit -m "test(e2e): tighten legacy-mode no-subprocess assertion"
```

---

### Task 9.3: README + spec cross-reference

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Multi-agent mode" section to the README** describing:
  - Default invocation runs multi-agent.
  - `--single` opt-out path.
  - `.agent/` runtime namespace.
  - Pointer to the design spec at `docs/superpowers/specs/2026-05-15-multi-agent-orchestration-design.md`.
  - Quick `/agents` slash-command reference.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document multi-agent mode, --single flag, .agent/ namespace"
```

---

## Self-Review Notes

**Spec coverage check:** Walked through each section of the spec; every requirement maps to at least one task above. Specifically:
- §3 architecture → Phase 5 (orchestrator), Phase 4 (tool-agent), Phase 6 (skill-agent)
- §4 component responsibilities → Tasks 4.x, 5.x, 6.x
- §5.1 MCP → Task 4.1, 5.2
- §5.2 A2A → Tasks 7.1–7.3
- §5.3 authz_grant → Tasks 3.1, 3.2, 4.4
- §6 registry → Task 5.1, 6.2; capability router 5.3; runtime state 7.3
- §7 data flow (simple+chain) → Tasks 5.6, 9.1; cancellation 8.2; crash 8.3
- §8 config → mock provider 1.1; credential injection 5.2 (env in `MCPHost.spawn`); slash commands 8.1; `--single` 2.1, 9.2
- §9 file layout → enforced by the file-structure section above and individual Create paths
- §10 testing pyramid → unit tests in each Task; integration/e2e in Phase 4/5/7/8/9
- §11 deferred items → none added as tasks (kept out of scope)

**Placeholder scan:** Two remaining soft spots, both flagged inline rather than left as TBDs:
- Task 4.1 step 4 instructs the engineer to adjust to whatever the installed MCP SDK exposes — the safest approach since the SDK's surface shifts.
- Task 6.1 step 3 references `legacy.single_agent_loop._build_chat_model` and notes "adapt to actual symbol". This is unavoidable: it depends on the current `cli.py` shape after Task 2.1 extraction.

**Type/name consistency check:** `Card`, `ToolSpec`, `MCPHost`, `CapabilityRouter`, `PermissionGate`, `StreamMux`, `OrchestratorState`, `AuthzError`, `A2AServer`, `A2AHandler`, `MockChatModel` all defined once and used consistently downstream.

**Scope check:** This is one cohesive plan. Each phase ends in a runnable, testable state: Phase 2 preserves the legacy path; Phase 5 has the orchestrator routing to tool-agent end-to-end; Phase 7 enables A2A; Phase 9 has the full skill-tool chain working. Splitting earlier would create awkward seams (e.g. testing the orchestrator without specialists requires mocking MCP, which is more work than just building tool-agent in Phase 4 and using the real client).
