"""Tests for the basic compute helpers in ``tool.tool_basic``.

These functions hold logic that used to live inline in the now-removed
``tool/tools.py`` @tool surface. They are pure (no permission/authz layer);
the authz gate lives at the tool_executor / grant boundary.
"""

import re

from tool.tool_basic import current_datetime_str, evaluate_expression


class TestEvaluateExpression:
    def test_basic_arithmetic(self):
        assert evaluate_expression("2 + 3") == "5"
        assert evaluate_expression("10 - 4") == "6"
        assert evaluate_expression("3 * 4") == "12"
        assert evaluate_expression("15 / 3") == "5.0"

    def test_power_operation(self):
        assert evaluate_expression("2 ** 3") == "8"
        assert evaluate_expression("pow(2, 3)") == "8"

    def test_math_functions(self):
        assert evaluate_expression("sqrt(144)") == "12.0"
        assert evaluate_expression("abs(-5)") == "5"
        assert evaluate_expression("round(3.7)") == "4"

    def test_constants(self):
        assert evaluate_expression("pi").startswith("3.14")
        assert evaluate_expression("e").startswith("2.71")

    def test_precedence(self):
        assert evaluate_expression("2 + 3 * 4") == "14"
        assert evaluate_expression("(2 + 3) * 4") == "20"

    def test_invalid_expression_returns_error_string(self):
        assert "error" in evaluate_expression("invalid").lower()
        assert "error" in evaluate_expression("1 / 0").lower()

    def test_unsafe_operations_blocked(self):
        assert "error" in evaluate_expression("__import__('os').system('ls')").lower()
        assert "error" in evaluate_expression("(1).__class__").lower()


class TestCurrentDatetimeStr:
    def test_format_matches_yyyy_mm_dd_hms_weekday(self):
        # e.g. "2026-06-07 22:55:01 (Sunday)"
        out = current_datetime_str()
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \([A-Za-z]+\)$", out)
