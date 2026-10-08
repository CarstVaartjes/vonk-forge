"""Ratchet direct startup construction: new optional dependencies need a guard."""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "tools/controller-startup-allowlist.json"


class DirectConstruction(ast.NodeVisitor):
    def __init__(self):
        self.calls: set[str] = set()

    def visit_FunctionDef(self, node):
        # Factory bodies are invoked by the recovery owner, not by wiring.
        return

    def visit_AsyncFunctionDef(self, node):
        return

    def visit_Lambda(self, node):
        return

    def visit_Call(self, node):
        name = ast.unparse(node.func)
        leaf = name.rsplit(".", 1)[-1]
        class_factory = (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id[:1].isupper()
        )
        if (
            leaf[:1].isupper()
            or class_factory
            or leaf.startswith("build_")
            or leaf == "from_env_and_secrets"
        ):
            self.calls.add(name)
        self.generic_visit(node)


def test_new_startup_construction_requires_explicit_isolation():
    for entry in json.loads(REGISTRY.read_text())["boundaries"]:
        tree = ast.parse((ROOT / entry["path"]).read_text())
        if entry["scope"] == "__main__":
            scope = next(node for node in tree.body if isinstance(node, ast.If))
        else:
            scope = next(
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == entry["scope"]
            )
        collector = DirectConstruction()
        for statement in scope.body:
            collector.visit(statement)
        assert collector.calls <= set(entry["pure_or_core_calls"]), entry["path"]


def test_guard_rejects_new_constructor_but_accepts_deferred_factory():
    collector = DirectConstruction()
    collector.visit(ast.parse("new = BrokenStorage()\n"))
    assert collector.calls == {"BrokenStorage"}
    collector = DirectConstruction()
    collector.visit(
        ast.parse(
            "new = registry.guard(capability, BrokenStorage, lambda: BrokenStorage())\n"
        )
    )
    assert collector.calls == set()
