from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_root_tests_do_not_import_control_implementation() -> None:
    offenders = []
    for path in (ROOT / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "insert" or not isinstance(node.func.value, ast.Attribute):
                continue
            if node.func.value.attr != "path" or not isinstance(node.func.value.value, ast.Name):
                continue
            if node.func.value.value.id != "sys":
                continue
            if "control/src" in ast.unparse(node):
                offenders.append(path)
                break
    assert offenders == []
