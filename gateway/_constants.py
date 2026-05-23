"""Shared gateway constants.

Single source of truth for things that used to be duplicated string
literals across the package — most importantly the logging formatter
(used by both writers and the log_tail parser).
"""
from __future__ import annotations

from typing import Literal

# Formatter shared by every place that writes to gateway.log (and by
# log_tail._logger_name, which depends on the column layout).
# Changing this format means updating log_tail._logger_name's split index
# in lockstep — that's why both live here together.
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"

# Ordered tuple of platform slugs the gateway package supports. Iteration
# order matches the /gateway picker for parity.
PLATFORMS: tuple[str, ...] = ("feishu", "qq")

# Literal alias for typed APIs (e.g. log_tail.read_tail). Keep in sync
# with PLATFORMS — mypy doesn't enforce that, so the test suite hits both
# branches as a guard.
GatewayPlatform = Literal["qq", "feishu"]
