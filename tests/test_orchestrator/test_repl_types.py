from __future__ import annotations

from orchestrator.repl_types import LoopAction


def test_loop_action_enum_values():
    assert LoopAction.CONTINUE is not LoopAction.EXIT
    assert LoopAction.CONTINUE.name == "CONTINUE"
    assert LoopAction.EXIT.name == "EXIT"


def test_loop_action_no_deps():
    import ast, inspect
    source = inspect.getsource(LoopAction)
    tree = ast.parse(source)
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert len(imports) == 0, "LoopAction module should have zero imports"
