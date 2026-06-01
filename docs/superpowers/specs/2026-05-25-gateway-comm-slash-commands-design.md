# Gateway comm slash commands (`/chat` / `/task` in QQ & Feishu)

**Date:** 2026-05-25
**Status:** Approved (design)

## Problem

In the terminal REPL, `/chat` and `/task` delegate to remote A2A peers via the
comm-agent (see `orchestrator/repl_commands.py`). In the chat-platform gateways
(QQ / Feishu) these commands do nothing special: `gateway/runner.run_turn`
sends the raw text straight to the planner, so a user typing `/chat 你好` just
gets the LLM improvising a reply about A2A peers. We want `/chat` and `/task`
(plus discovery helpers) to actually work from QQ / Feishu chat.

The REPL handler can't be reused directly: it is ~1300 lines tightly coupled to
Rich terminal rendering (`ReplUI`) and keeps a current-peer in instance memory,
while the gateway runs **one isolated turn per message** (a fresh `MCPHost` is
spawned and torn down each turn in `gateway/runner._run_turn_locked`) and must
return a plain-text reply.

## Goals

- `/task <peer_id> <task>` and `/chat <peer_id> <message>` work from QQ / Feishu.
- `/peers` (list registered peer_ids) and `/help` for discovery.
- Gated to an operator-controlled allowlist of user ids (fail-safe: empty = deny).
- `/chat` maintains multi-turn context with a remote peer across messages.
- Plain natural-language chat is unchanged (non-commands fall through to the planner).

## Non-goals

- No interactive peer registration from chat (`/comm add` needs a TTY + HMAC
  secret prompt). Peers are registered once in the REPL via `/comm add`; chat
  only *uses* them.
- No per-chat "current peer" state — the peer is written inline on every command.
- No exposure of operator/REPL commands (`/model`, `/permissions`, `/gateway`,
  `/comm add/use/rm`, …) to chat.
- No new comm-agent tools — we reuse the existing `comm.delegate`,
  `comm.chat`, `comm.list_peers` already registered on the per-turn host.

## Approach

A dedicated, UI-free gateway slash module (`gateway/slash.py`). Rejected
alternatives: refactoring the Rich-coupled `ReplCommandHandler` behind a
text/terminal backend (large, risky, touches the REPL for 3 commands); having
the planner LLM interpret slashes (unreliable — this is exactly what fails in
the current behaviour).

## Components

### 1. `gateway/slash.py` — command parsing & dispatch

Single entry point:

```python
async def handle_slash(
    line: str, *, host, session_key: str, user_id: str,
) -> str | None
```

- Returns a **string** → the message was a recognized slash command; the string
  is the reply to send back to the user (planner is skipped).
- Returns **`None`** → not a recognized slash command (or not a slash at all);
  the caller falls through to the normal planner path. This preserves today's
  behaviour for ordinary messages and for unknown `/whatever` typos (they reach
  the planner, which can respond conversationally).

Commands:

| Command | comm tool | Reply |
|---|---|---|
| `/task <peer_id> <task>` | `comm.delegate(peer_id, task, stream=False)` | `final_result` rendered as text |
| `/chat <peer_id> <message>` | `comm.chat(peer_id, message, context_id)` | `reply` text |
| `/peers` | `comm.list_peers()` | lines of `peer_id — display_name` |
| `/help` | — | usage for the above |

Parsing: split into `command`, `peer_id`, `rest`. Missing `peer_id` / `rest`
for `/task` and `/chat` returns a one-line usage hint (a string, so it is sent
back to the user — not `None`). comm-tool errors (unknown peer, peer
unreachable) are surfaced as a short text reply, never raised.

`host` is the per-turn `MCPHost` that `_run_turn_locked` already builds and that
has the comm-agent spawned (`_bootstrap` spawns every card in `.agent/agents/`).
Tools are invoked with the same `_unwrap` / `comm.*` JSON-envelope convention
the REPL handler uses.

### 2. Authorization allowlist

- gateways.json gains an optional per-platform `allowed_users: [user_id, ...]`
  field (Feishu `open_id` / QQ `openid`), editable via the `/gateway setup`
  wizard (a new optional field in `_gw_field_specs`).
- `handle_slash` checks `user_id ∈ allowed_users` **before** doing anything.
  - Not authorized → return a short refusal string (so the user knows it is
    gated, not broken); do **not** fall through to the planner.
  - `allowed_users` missing/empty → **deny all** slash commands (fail-safe; an
    unconfigured allowlist must not silently expose remote peers). Ordinary
    natural-language messages are unaffected.
- The platform is derived from the `session_key` prefix (`qq:` / `feishu:`),
  which the adapters already set, so no adapter call-signature change is needed.
  The allowlist is loaded from gateways.json for that platform.

### 3. `/chat` multi-turn context

- `comm.chat` returns a `context_id` that the remote peer uses to thread a
  conversation. The gateway is per-turn stateless, so we persist `context_id`
  keyed by `(session_key, peer_id)` in a small JSON file
  `<config_dir>/comm_chat_contexts.json` (alongside other runtime state).
- Next `/chat` to the same peer in the same chat loads and reuses it; the
  returned (possibly new) `context_id` is written back.
- `/task` is one-shot and needs no context.
- Slash-command round-trips are **not** written to the 25-turn local
  conversation history (`gateway.session_store`): they are commands / remote
  conversations and must not pollute the local planner context.

### 4. Wiring in `gateway/runner.py`

In `_run_turn_locked`, after `await _bootstrap(host, router)` and before the
planner call:

```python
slash_reply = await handle_slash(
    prompt, host=host, session_key=session_key, user_id=user_id,
)
if slash_reply is not None:
    reply_text = slash_reply
    return reply_text
```

The existing `finally` (host shutdown, peers.json restore, memory-env restore)
still runs. Because slash replies are returned before the planner, they skip the
session-history append (which is gated on the normal path).

## Data flow

```
QQ/Feishu user: "/task openclaw-home summarize ~/notes.md"
  → adapter _handle_dispatch → run_turn(prompt, session_key="qq:123", user_id="openid_X")
    → _run_turn_locked: _bootstrap(host)            # comm-agent spawned, comm.* available
      → handle_slash(...)
        → allowed? user_id in gateways.json[qq].allowed_users   # else refusal string
        → parse: cmd=/task peer=openclaw-home rest="summarize ~/notes.md"
        → host.call_tool("comm-agent", "comm.delegate", {...})
        → return final_result text
    → reply sent back through the adapter's reply API
```

## Error handling

- Unknown peer / unreachable peer / comm-agent down → short text reply quoting
  the comm error (never raises out of `handle_slash`).
- Missing args → usage hint string.
- Unauthorized user → refusal string.
- Unknown `/command` → return `None` (fall through to planner, same as today).

## Testing

`tests/test_gateway/test_slash.py`, pure unit tests with a fake host injecting
`comm.*` responses (no subprocesses):

- authorized `/task` → calls `comm.delegate` with parsed peer/task, returns text.
- authorized `/chat` → calls `comm.chat`, persists `context_id`, second call
  reuses it for the same `(session_key, peer_id)`.
- `/peers` → lists registered peers.
- `/help` → usage text.
- unauthorized user → refusal string, no comm tool called.
- empty/missing allowlist → deny (refusal), no comm tool called.
- missing args (`/task openclaw-home` with no task) → usage hint.
- non-slash input and unknown `/command` → returns `None` (planner fall-through).
- platform derived from `session_key` prefix selects the right allowlist.

## Files touched

- **new** `gateway/slash.py` — parsing, allowlist check, comm dispatch, context store.
- `gateway/runner.py` — call `handle_slash` after bootstrap in `_run_turn_locked`.
- `gateway/credentials.py` and/or `orchestrator/repl_commands.py` `_gw_field_specs`
  — add optional `allowed_users` field to the setup wizard / schema.
- **new** `tests/test_gateway/test_slash.py`.
- README — document the chat-gateway slash commands and the allowlist.
