"""Tests for orchestrator/ui_input helpers.

The full ``ask_boxed_input`` flow needs a running prompt_toolkit Application
to test end-to-end and is exercised manually in the REPL. The width
calculation that drives the input box's auto-grow behavior IS unit-testable,
and a regression here is what previously made long lines appear to "slide
right" off the terminal instead of wrapping into a taller input frame.
"""
from __future__ import annotations

from orchestrator.ui_input import _visual_width


def test_ascii_chars_are_width_one():
    assert _visual_width("hello") == 5
    assert _visual_width("") == 0


def test_cjk_chars_are_width_two():
    # Common Chinese: each glyph occupies two terminal columns.
    assert _visual_width("你好") == 4
    assert _visual_width("中文测试") == 8


def test_mixed_ascii_and_cjk():
    # "hi你好" → 2 + 4 = 6
    assert _visual_width("hi你好") == 6


def test_fullwidth_punctuation_is_width_two():
    # Fullwidth comma U+FF0C — counts as 2.
    assert _visual_width("，") == 2
    assert _visual_width("你好，世界") == 10


def test_hangul_syllables_are_width_two():
    # 안녕 ("hello" in Korean) — both syllables are wide.
    assert _visual_width("안녕") == 4
