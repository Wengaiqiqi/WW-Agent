from __future__ import annotations

from orchestrator.repl_types import LoopAction


def test_loop_action_enum_values():
    assert LoopAction.CONTINUE is not LoopAction.EXIT
    assert LoopAction.CONTINUE.name == "CONTINUE"
    assert LoopAction.EXIT.name == "EXIT"


def test_loop_action_no_deps():
    import ast, inspect
    mod = __import__("orchestrator.repl_types", fromlist=["LoopAction"])
    source = inspect.getsource(mod)
    tree = ast.parse(source)
    imports = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module is not None
        and (n.module == "orchestrator" or n.module.startswith("orchestrator."))
    ]
    assert len(imports) == 0, "LoopAction module should have zero intra-orchestrator imports"
