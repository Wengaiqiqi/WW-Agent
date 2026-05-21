"""Tail + filter helper for the gateway action-menu log panel.

Reads ``<config_dir>/gateway.log`` from the end, keeps only lines that
match the requested platform, and returns at most ``max_lines`` already-
trimmed strings (chronological order). Designed for repeated calls from a
prompt_toolkit render loop, so it must be fast on a small file and never
raise.
"""
from __future__ import annotations
