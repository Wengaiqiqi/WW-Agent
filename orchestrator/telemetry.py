# orchestrator/telemetry.py
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from orchestrator.stream_mux import StreamMux
# Redaction lives in agents.shared so an agent subprocess can call emit_event
# without depending on orchestrator.* — we just re-use the same function here
# to keep mask behavior consistent across the two write paths.
from agents.shared.telemetry import _redact_secrets

_PATH = Path(".agent/runtime/telemetry.ndjson")
_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB — past this, rotate to .1


def _rotate_if_oversized() -> None:
    """If telemetry.ndjson exceeds _MAX_BYTES, move it to .ndjson.1 (overwriting
    any prior rotation). Avoids unbounded disk growth on long sessions where
    every tool call writes a line. One backup is enough — we're a single-user
    dev tool, not an audit log."""
    try:
        if _PATH.exists() and _PATH.stat().st_size > _MAX_BYTES:
            rotated = _PATH.with_suffix(".ndjson.1")
            if rotated.exists():
                rotated.unlink()
            _PATH.rename(rotated)
    except OSError:
        # Best-effort. If we can't rotate (file in use on Windows, permission
        # denied, etc.), we'd rather keep emitting than crash the agent.
        pass


def reset_log() -> None:
    """Clear the telemetry log at the start of a session."""
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text("", encoding="utf-8")


async def tail(mux: StreamMux, stop_event: asyncio.Event) -> None:
    """Tail telemetry.ndjson and emit each event into the unified stream.

    Polls every 50ms until stop_event is set. Tracks file position so events
    are emitted exactly once even as the file grows."""
    pos = 0
    while not stop_event.is_set():
        if _PATH.exists():
            try:
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
            except OSError:
                pass  # transient — try again next tick
        await asyncio.sleep(0.05)


def emit_event(*, agent_id: str, trace_id: str, message: str) -> None:
    """Called from a specialist process to record a telemetry event.

    Appends one JSON line to telemetry.ndjson. The orchestrator's tail task
    will pick it up and surface it via the unified stream. Free-form
    ``message`` is run through ``_redact_secrets`` before write — agents
    sometimes echo HTTP response bodies or env dumps that carry credentials
    they didn't intend to log."""
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_oversized()
    with _PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "agent_id": agent_id,
            "trace_id": trace_id,
            "message": _redact_secrets(message),
        }) + "\n")
