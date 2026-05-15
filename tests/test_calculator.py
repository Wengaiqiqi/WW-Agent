"""Tests for the calculator tool."""

import pytest
from tool.tools import calculator


class TestCalculator:
    """Test suite for calculator tool."""

    def test_basic_arithmetic(self):
        """Test basic arithmetic operations."""
        assert calculator.invoke({"expression": "2 + 3"}) == "5"
        assert calculator.invoke({"expression": "10 - 4"}) == "6"
        assert calculator.invoke({"expression": "3 * 4"}) == "12"
        assert calculator.invoke({"expression": "15 / 3"}) == "5.0"

    def test_power_operation(self):
        """Test power operations."""
        assert calculator.invoke({"expression": "2 ** 3"}) == "8"
        assert calculator.invoke({"expression": "pow(2, 3)"}) == "8"

    def test_math_functions(self):
        """Test mathematical functions."""
        result = calculator.invoke({"expression": "sqrt(144)"})
        assert result == "12.0"

        result = calculator.invoke({"expression": "abs(-5)"})
        assert result == "5"

        result = calculator.invoke({"expression": "round(3.7)"})
        assert result == "4"

    def test_constants(self):
        """Test mathematical constants."""
        result = calculator.invoke({"expression": "pi"})
        assert result.startswith("3.14")

        result = calculator.invoke({"expression": "e"})
        assert result.startswith("2.71")

    def test_complex_expression(self):
        """Test complex expressions."""
        result = calculator.invoke({"expression": "2 + 3 * 4"})
        assert result == "14"

        result = calculator.invoke({"expression": "(2 + 3) * 4"})
        assert result == "20"

    def test_invalid_expression(self):
        """Test error handling for invalid expressions."""
        result = calculator.invoke({"expression": "invalid"})
        assert "error" in result.lower()

        result = calculator.invoke({"expression": "1 / 0"})
        assert "error" in result.lower()

    def test_unsafe_operations_blocked(self):
        """Test that unsafe operations are blocked."""
        # Should not allow arbitrary code execution
        result = calculator.invoke({"expression": "__import__('os').system('ls')"})
        assert "error" in result.lower()

        # Should not allow attribute access
        result = calculator.invoke({"expression": "(1).__class__"})
        assert "error" in result.lower()
