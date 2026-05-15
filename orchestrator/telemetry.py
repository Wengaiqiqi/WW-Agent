# orchestrator/telemetry.py
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from orchestrator.stream_mux import StreamMux

_PATH = Path(".agent/runtime/telemetry.ndjson")


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
    will pick it up and surface it via the unified stream."""
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with _PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "agent_id": agent_id,
            "trace_id": trace_id,
            "message": message,
        }) + "\n")
