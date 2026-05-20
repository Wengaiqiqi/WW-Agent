# gateway/

Chat-platform adapters that bridge external messaging platforms to the
orchestrator. Each inbound user message is converted to a single
`orchestrator.run_prompt` turn and the assistant's reply is sent back
through the platform's API. Adapters are intentionally thin: no session
persistence, no rate limiting, no pairing.

## Run

### From the REPL (recommended)

`python cli.py` and run `/gateway`. The slash command opens a two-step
arrow-key menu (same UX as `/model`):

```
/gateway

   Chat Platform Gateways
   up/down move - enter open - esc cancel

   > * Feishu / Lark            running
         app_id=cli_abc  url=http://0.0.0.0:8765/feishu/webhook
     - QQ Official Bot          not configured
         app_id=?
```

After picking a platform, a second menu offers:

| Action | Effect |
|---|---|
| Setup credentials | Field-by-field wizard. Enter keeps current value. Ctrl+C aborts. |
| Start gateway | Run the adapter as a background task in this REPL |
| Stop gateway | Cancel the running task (shown only when running) |
| View saved credentials | Show all stored fields; secrets are masked |
| Clear credentials | Delete the platform's entry from `gateways.json` |
| Back to platform list | Return to step 1 |

Credentials live in `.langchain-agent/gateways.json` (a sibling
`.gitignore` is created automatically). The wizard masks secrets when
redisplaying them (`cli_xxxx******`). Background tasks share the REPL's
event loop, so the orchestrator can keep handling REPL prompts while a
gateway is live; exiting the REPL stops all running gateways.

### Standalone

```bash
python -m gateway feishu --port 8765    # Feishu / Lark webhook server
python -m gateway qq                    # QQ Official Bot (WebSocket gateway)
```

The standalone entry point reads from environment variables instead of
`gateways.json` (see each section below for the var names) — useful for
running gateways as long-lived services under systemd / docker.

The same orchestrator config (active model, permission mode, workspace, MCP
specialists) applies — gateway processes share the on-disk `.langchain-agent/`
and `.agent/` state with the CLI.

## Feishu / Lark

Set up in the Feishu developer console:

1. Create a Custom App, enable Bot capability, and grant scopes:
   `im:message`, `im:message:send_as_bot`.
2. Open **Event Subscriptions** ("事件订阅"), point the request URL at
   `https://<your-host>/feishu/webhook`, copy the **Verification Token**.
3. Subscribe to `im.message.receive_v1`.
4. (Optional) Enable **Encrypt Mode** and copy the **Encrypt Key**.

Required env vars:

| Var | Purpose |
|---|---|
| `FEISHU_APP_ID` | App ID from "Credentials & Basic Info" |
| `FEISHU_APP_SECRET` | App Secret from the same page |
| `FEISHU_VERIFY_TOKEN` | Verification Token from "Event Subscriptions" |
| `FEISHU_ENCRYPT_KEY` | (Only if Encrypt Mode is on) Encrypt Key |
| `FEISHU_DOMAIN` | `open.feishu.cn` (default) or `open.larksuite.com` |
| `FEISHU_REPLY_IN_THREAD` | `1` to reply in the same thread |

Behavior:

- In **private chats**, the bot replies to every text or post message.
- In **groups**, the bot only replies when `@`-mentioned. The mention key is
  stripped before the prompt is passed to the orchestrator.
- The webhook handler responds in `< 3s` (Feishu's delivery requirement) by
  dispatching the orchestrator turn as a background task and ACKing immediately.

## QQ Official Bot

Uses the official **WebSocket Gateway** (the same protocol the QQ
SDK / hermes-agent use). No public webhook needed — the bot opens an outbound
connection to `wss://api.sgroup.qq.com/websockets`.

Set up in the QQ Open Platform console (`q.qq.com`):

1. Register a Bot application and complete the qualification review.
2. Note the **AppID** and **Client Secret**.
3. In **Functions → Event Subscriptions**, enable the channels your bot needs
   (Group@bot / C2C / Guild@bot).

Required env vars:

| Var | Purpose |
|---|---|
| `QQ_APP_ID` | Bot AppID |
| `QQ_CLIENT_SECRET` | Bot Client Secret |
| `QQ_INTENTS` | (Optional) override intents bitmask. Default = `(1<<25) \| (1<<30)` |
| `QQ_SANDBOX` | `1` to use the sandbox API host |

Default intents:

- `1 << 25` — `PUBLIC_MESSAGES` (C2C + Group `@bot`)
- `1 << 30` — `PUBLIC_GUILD_MESSAGES` (Guild `@bot`)

Add `1 << 12` (DIRECT_MESSAGE) if you need guild DMs.

Event routing:

| Event | Reply endpoint |
|---|---|
| `GROUP_AT_MESSAGE_CREATE` | `POST /v2/groups/{group_openid}/messages` |
| `C2C_MESSAGE_CREATE` | `POST /v2/users/{user_openid}/messages` |
| `AT_MESSAGE_CREATE` / `GUILD_AT_MESSAGE_CREATE` | `POST /channels/{channel_id}/messages` |

The adapter handles session resume (op 6) on reconnects and falls back to
identify (op 2) if the saved seq/session is rejected.

## How it talks to the orchestrator

`gateway.runner.run_turn(prompt)` is the single entry point. Each call:

1. Mints an HMAC key and spawns a fresh `MCPHost` + specialists.
2. Builds the orchestrator planner (LLM if `LANGCHAIN_AGENT_MODEL` is set,
   stub otherwise).
3. Runs one `TurnRunner.run(prompt)` turn.
4. Returns the final assistant text (no `[orchestrator]` line tags).
5. Tears down MCP children.

Turns are serialised process-wide (the orchestrator's `.agent/runtime/` files
are not safe for concurrent writers). For a high-traffic bot, run multiple
gateway processes behind a load balancer rather than enabling concurrency
inside one process.

## Adding another platform

The pattern is fixed:

1. Verify the inbound payload (signature / token).
2. Extract plain-text content + a "where to reply" identifier.
3. `reply = await run_turn(text)`.
4. Send `reply` through the platform's outbound API.

Keep it in one file under `gateway/<platform>.py` and expose a `serve()`
entry point, then register it in `gateway/__main__.py`.
