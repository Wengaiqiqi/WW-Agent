from __future__ import annotations
import sys
from typing import TextIO


# Cosmetic short tags for the three built-in agents. Any agent not listed
# here falls back to ``[{agent_id}]`` (see ``_AGENT_TAG.get`` below), so
# adding a new specialist is functionally complete without touching this
# dict — entries here are purely a "shorter on screen" UX choice for the
# names users see most often.
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
