"""Chat-platform gateways for the W&W agent.

Each adapter receives messages from an external platform (Feishu, QQ Official
Bot, ...), routes the user's text through ``orchestrator.run_prompt`` via the
capture helper in :mod:`gateway.runner`, and replies back through the
platform's API.

Adapters are intentionally thin: no session persistence, no rate limiting, no
pairing. They map one inbound user message to one orchestrator turn and send
the resulting text back.
"""
