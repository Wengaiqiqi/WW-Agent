"""Shared REPL flow-control types. Keep free of intra-orchestrator imports."""

from enum import Enum, auto


class LoopAction(Enum):
    CONTINUE = auto()
    EXIT = auto()
