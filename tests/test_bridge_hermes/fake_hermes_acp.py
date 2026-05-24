#!/usr/bin/env python
"""Minimal fake `hermes acp` for tests.

Speaks the same camelCase ACP JSON-RPC over stdio that real `hermes acp`
speaks (see hermes-agent/agent/copilot_acp_client.py). Behavior:
  - initialize          -> result with protocolVersion
  - session/new         -> result {"sessionId": "sess-N"}
  - session/prompt      -> streams two agent_message_chunk updates that echo
                           the prompt text, then returns {"stopReason":"end_turn"}
  - anything else       -> JSON-RPC error -32601

Env knobs for tests:
  FAKE_ACP_FAIL_PROMPT=1  -> respond to session/prompt with a JSON-RPC error
  FAKE_ACP_ASK_PERMISSION=1 -> emit a session/request_permission before completing
"""
from __future__ import annotations

import json
import os
import sys


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    session_counter = 0
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"protocolVersion": 1,
                             "agentInfo": {"name": "fake-hermes", "version": "0.0.0"}}})
        elif method == "session/new":
            session_counter += 1
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"sessionId": f"sess-{session_counter}"}})
        elif method == "session/prompt":
            sid = params.get("sessionId")
            text = "".join(
                p.get("text", "") for p in params.get("prompt", [])
                if isinstance(p, dict) and p.get("type") == "text"
            )
            if os.environ.get("FAKE_ACP_FAIL_PROMPT") == "1":
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32000, "message": "fake hermes prompt failure"}})
                continue
            if os.environ.get("FAKE_ACP_ASK_PERMISSION") == "1":
                # Server->client request; bridge must answer before we finish.
                send({"jsonrpc": "2.0", "id": 9001, "method": "session/request_permission",
                      "params": {"sessionId": sid,
                                 "options": [{"optionId": "allow-once", "name": "Allow"},
                                             {"optionId": "reject-once", "name": "Reject"}]}})
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": sid,
                             "update": {"sessionUpdate": "agent_message_chunk",
                                        "content": {"type": "text", "text": "echo: "}}}})
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": sid,
                             "update": {"sessionUpdate": "agent_message_chunk",
                                        "content": {"type": "text", "text": text}}}})
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
