"""Small, dependency-free compute helpers.

These hold logic that used to live inline in ``tool/tools.py`` (the removed
single-agent @tool surface). They are pure functions — no permission/authz
layer — so the multi-agent ``tool_executor`` wrappers and unit tests can call
them directly. Authorization is enforced at the tool_executor / JWT-grant
boundary, not here.
"""
from __future__ import annotations

import ast
import math
import operator
from datetime import datetime

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_FUNCS = {
    "abs": abs,
    "round": round,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "pow": pow,
}

_CONSTS = {
    "pi": math.pi,
    "e": math.e,
}


def _safe_eval(node: ast.AST):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op(_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        return op(_safe_eval(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only simple function calls are allowed")
        func_name = node.func.id
        if func_name not in _FUNCS:
            raise ValueError(f"Unknown function: {func_name}")
        return _FUNCS[func_name](*[_safe_eval(arg) for arg in node.args])
    if isinstance(node, ast.Name):
        if node.id not in _CONSTS:
            raise ValueError(f"Unknown constant: {node.id}")
        return _CONSTS[node.id]
    raise ValueError(f"Unsupported expression type: {type(node).__name__}")


def evaluate_expression(expression: str) -> str:
    """Evaluate a math expression with a restricted AST walker.

    Supports arithmetic, powers, a small set of math functions, and the
    constants ``pi``/``e``. Anything else (attribute access, imports, unknown
    names) raises inside ``_safe_eval`` and is returned as an error string so
    the caller never executes arbitrary code.
    """
    try:
        tree = ast.parse(expression, mode="eval")
        return str(_safe_eval(tree.body))
    except Exception as exc:
        return f"Calculation error: {exc}"


def current_datetime_str() -> str:
    """Return the current local date and time, e.g. ``2026-06-07 22:55:01 (Sunday)``."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S (%A)")
