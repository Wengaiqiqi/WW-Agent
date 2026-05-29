# Web Frontend Reskin (Hermes Teal) + Custom Endpoints — Design

Date: 2026-05-29
Status: Approved (design); implementation plan pending

## Goal

Two changes to the web UI, kept in one effort because they touch the same files:

1. **Reskin** the existing web frontend to match the visual aesthetic of the
   `hermes-agent` web dashboard (the "Hermes Teal" terminal look).
2. **Custom endpoints**: let a logged-in web user enter a Base URL + API Key
   (+ model name + protocol) directly in the web UI and switch the agent to
   that model — without the operator pre-configuring server-side keys.

## Constraints / decisions (locked)

- **Stack**: keep the current vanilla-JS single-page app served as static
  files by FastAPI. **No build step, no React, no `@nous-research/ui`.** We
  reproduce the *look*, not the reference's architecture.
- **Custom-endpoint UX**: add a "custom endpoint" entry alongside the existing
  server-preset providers. Presets remain. The user fills Label + Base URL +
  API Key + Model + Protocol; once saved it is selectable in the model picker.
- **Storage**: custom endpoints are stored **server-side, per user, in the
  existing SQLite store** (`web/store.py`). The API key is stored in plaintext,
  consistent with the existing `credentials.json` threat model.

## Background: how model selection works today

- `web/models.py:available_models()` lists only providers whose API key is
  discoverable **server-side** (env var or `credentials.json`). The chosen
  `provider/model` id is what the frontend sends.
- `web/app.py` `MessageReq{content, model}` → `web/bridge.py:run_turn_streaming(..., model_id=...)`.
- `web/bridge.py:_web_turn_env()` sets `LANGCHAIN_AGENT_MODEL` for the turn
  (serialized on `_CONCURRENCY_GUARD`, restored on exit).
- `config/_settings.load_active_config()` resolves `LANGCHAIN_AGENT_MODEL`
  ("provider" or "provider/model") → `make_config(provider, model)`, pulling
  `base_url` / `api_key_env` / `protocol` from the `PROVIDERS` registry.
- `config/_credentials.get_api_key(cfg)` reads `os.getenv(cfg.api_key_env)` or
  `credentials.json`. `config/_llm.build_llm()` builds `ChatAnthropic` or
  `ReasoningChatOpenAI` from the resolved `ActiveConfig`.
- **The planner runs in-process** in the web worker. **Specialists are spawned
  subprocesses** (`orchestrator/mcp_host.py`) that receive a *whitelisted* env
  (`_OS_PASSTHROUGH`) and bootstrap their own credentials from
  `credentials.json`. They do **not** inherit arbitrary process env.

The whitelist is the crux: a user-supplied URL/key reaches the in-process
planner via env, but a **delegated specialist** won't see it unless we
explicitly forward the relevant vars.

## Part A — Visual reskin (Hermes Teal terminal aesthetic)

Files: `web/static/index.html`, `web/static/styles.css`, `web/static/app.js`
(markup for the new dialog), plus `web/static/fonts/` for a bundled monospace.

- **Color tokens** in `styles.css :root` (mirroring `hermes-agent/web/src/index.css`):
  - `--background: #041c1c` (deep teal canvas)
  - `--foreground / --midground: #ffe6cb` (cream)
  - `--border: color-mix(in srgb, #ffe6cb 15%, transparent)`
  - `--destructive: #fb2c36`, `--success: #4ade80`, `--warning: #ffbd38`
  - `--warm-glow: rgba(255, 189, 56, 0.35)`
- **Fonts**:
  - Bundle **JetBrains Mono** (Apache-2.0) woff2 from `hermes-agent/web/public/fonts-terminal/`
    into `web/static/fonts/` for the terminal/data monospace.
  - Headers: system font + `text-transform: uppercase` + `letter-spacing`
    to evoke the Hermes "display" feel.
  - **Do NOT copy** the proprietary Hermes fonts (Collapse / Mondwest / Rules).
- **Texture**: border-based cards, uppercase wide-tracking section headers, a
  subtle grain/warm-glow backdrop. Keep the existing sidebar + chat layout;
  this is a re-paint, not a re-layout.
- Existing behavior preserved: auth view, conversation list + caching, SSE
  token streaming, the `过程(n)` process disclosure, markdown + syntax
  highlight, copy buttons.

### Layout sketch (text mockup, approved)

```
┌──────────────┬─────────────────────────────────────────┐
│ + NEW CHAT   │  [ MODEL ▾  mimo-v2.5-pro · xiaomi ]  ⚙  │
│              │─────────────────────────────────────────│
│ ▸ conv one   │  user: ...                              │
│   conv two   │  assistant: ...  ▸过程(3)               │
│──────────────│                                         │
│ user  ⏻logout│  [ 发消息…                    ] [发送] │
└──────────────┴─────────────────────────────────────────┘

  ⚙ / "+ custom endpoint" opens a Hermes-style modal:
  ┌── ADD CUSTOM ENDPOINT ──────────── X ┐
  │ Label    [ My LLM            ]       │
  │ Base URL [ https://.../v1    ]       │
  │ API Key  [ ••••••••••        ]       │
  │ Model    [ gpt-5.4           ]       │
  │ Protocol ( openai ▾ )                │
  │              [Cancel] [Save & Use]   │
  └──────────────────────────────────────┘
```

## Part B — Custom endpoints (server-side, per user)

### B1. Storage — `web/store.py`

New table:

```sql
CREATE TABLE IF NOT EXISTS endpoints (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL,
  label      TEXT NOT NULL,
  base_url   TEXT NOT NULL,
  api_key    TEXT NOT NULL,
  model      TEXT NOT NULL,
  protocol   TEXT NOT NULL DEFAULT 'openai',  -- 'openai' | 'anthropic'
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_endpoints_user ON endpoints(user_id);
```

Functions (mirroring existing store patterns):
`create_endpoint(db, user_id, label, base_url, api_key, model, protocol) -> dict`,
`list_endpoints(db, user_id) -> list[dict]`,
`get_endpoint(db, endpoint_id) -> dict | None`,
`delete_endpoint(db, endpoint_id) -> None`.

`init_db` creates the table (additive migration — safe on existing DBs).

### B2. Routes — `web/app.py`

- `GET /api/endpoints` → `list_endpoints(db, user["id"])`. **Never return
  `api_key`** in list responses; return a `has_key: true` flag instead.
- `POST /api/endpoints` `{label, base_url, api_key, model, protocol}` →
  validate non-empty + `protocol in {openai, anthropic}`, create, return the
  row (sans key).
- `DELETE /api/endpoints/{id}` → ownership check (404 if not owned), delete.
- `MessageReq` gains optional `endpoint_id: str | None`. In `send_message`,
  if `endpoint_id` is present: load the owned endpoint (404 otherwise) and pass
  its `base_url / api_key / model / protocol` into `bridge_fn`; otherwise behave
  as today with `model`.

Ownership helper analogous to `_owned_conversation`.

### B3. Per-turn config injection — `web/bridge.py`

`run_turn_streaming` and `_web_turn_env` extended to optionally carry a custom
endpoint. When one is supplied, `_web_turn_env` sets (and restores) for the turn:

- `LANGCHAIN_AGENT_MODEL = "custom/<model>"`
- `LANGCHAIN_AGENT_BASE_URL = <base_url>`
- `LANGCHAIN_AGENT_PROTOCOL = <protocol>`  (`openai` | `anthropic`)
- `LANGCHAIN_AGENT_API_KEY = <api_key>`

These are added to the set already saved/restored, so the prior values are
preserved exactly. The turn holds `_CONCURRENCY_GUARD`, so no concurrent turn
sees these.

### B4. Config — honor the env overrides

- `config/_settings.load_active_config()`: after resolving the provider from
  `LANGCHAIN_AGENT_MODEL`, if `LANGCHAIN_AGENT_BASE_URL` and/or
  `LANGCHAIN_AGENT_PROTOCOL` are set, apply them as overrides on the resulting
  `ActiveConfig`. Gated on the env vars being present — **default behavior
  unchanged when unset.** (The `custom` provider already exists in the registry
  with `protocol: openai`, `base_url: ""`; the overrides fill the blanks.)
- `config/_credentials.get_api_key(cfg)`: prefer `LANGCHAIN_AGENT_API_KEY` when
  set, else fall back to the existing `os.getenv(cfg.api_key_env)` /
  `credentials.json` path.

### B5. Specialist propagation — `orchestrator/mcp_host.py`

Add to `_OS_PASSTHROUGH` so a *delegated* specialist building the same custom
endpoint can resolve URL/protocol and authenticate:

- `LANGCHAIN_AGENT_BASE_URL`
- `LANGCHAIN_AGENT_PROTOCOL`
- `LANGCHAIN_AGENT_API_KEY`

Rationale: `LANGCHAIN_AGENT_API_KEY` is a secret, which the whitelist normally
fails-closed on. Forwarding it here is a deliberate, minimal exception: it is
the one key the user chose for *this* turn, the turn is serialized, and the
value is set/restored around the turn. It is only present in the env when a
custom endpoint is active.

### B6. Frontend — `web/static/app.js`

- `loadModels()` merges server presets (`GET /api/models`) with the user's
  custom endpoints (`GET /api/endpoints`) into the model picker.
- A "+ custom endpoint" action opens the modal (B / sketch). Save →
  `POST /api/endpoints` → refresh list → select the new endpoint.
- Custom endpoints are deletable from the picker/manage UI.
- `sendMessage()` sends `endpoint_id` when a custom endpoint is selected;
  otherwise sends `model` as today.

## Testing

- `web/store.py`: endpoints CRUD + per-user isolation.
- `web/app.py`: `/api/endpoints` auth + ownership (list omits `api_key`;
  POST validation; DELETE 404 for non-owner); `send_message` with `endpoint_id`
  routes the endpoint fields into a fake `bridge_fn`.
- `web/bridge.py`: a custom-endpoint turn sets the four env vars and restores
  prior values afterward.
- `config`: `load_active_config()` honors `LANGCHAIN_AGENT_BASE_URL` /
  `LANGCHAIN_AGENT_PROTOCOL`; `get_api_key()` prefers `LANGCHAIN_AGENT_API_KEY`.
- Reskin is visual; verify manually in a browser (golden path + the new dialog).

## Out of scope (YAGNI)

- Editing an existing endpoint (add + delete only for now).
- "List models available at this endpoint" discovery call.
- Porting the multi-page Hermes dashboard (Config / Cron / Logs / etc.).
- Theme switching / i18n / the Nous design system.
- Encrypting the stored API key at rest (matches existing `credentials.json`).
