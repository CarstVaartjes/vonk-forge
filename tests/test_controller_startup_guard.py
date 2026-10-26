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


FILESYSTEM_EFFECTS = frozenset(
    {
        "chmod",
        "makedirs",
        "mkdir",
        "mkfifo",
        "mkstemp",
        "open",
        "rename",
        "replace",
        "rmdir",
        "symlink_to",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
)
SOURCE = ROOT / "control/src/vonk_control"


def constructors(sources: list[str]) -> dict[str, list[ast.FunctionDef]]:
    """Every ``__init__`` in the sources, by class name."""
    result: dict[str, list[ast.FunctionDef]] = {}
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ClassDef):
                result.setdefault(node.name, []).extend(
                    init
                    for init in node.body
                    if isinstance(init, ast.FunctionDef) and init.name == "__init__"
                )
    return result


def nested_filesystem_effects(
    inits: dict[str, list[ast.FunctionDef]], name: str, depth: int = 1
) -> set[str]:
    """Filesystem effects building ``name`` can run, one constructor level deep.

    An allowlisted constructor is only "pure" if building it cannot fail on a
    filesystem fault, so the scan reads its ``__init__`` and the ``__init__`` of
    each class that body constructs."""
    effects: set[str] = set()
    for init in inits.get(name, []):
        for call in ast.walk(init):
            if not isinstance(call, ast.Call):
                continue
            leaf = ast.unparse(call.func).rsplit(".", 1)[-1]
            if leaf in FILESYSTEM_EFFECTS:
                effects.add(f"{name}.__init__ calls {leaf}")
            elif depth and leaf[:1].isupper():
                effects |= {
                    f"{name} -> {nested}"
                    for nested in nested_filesystem_effects(inits, leaf, depth - 1)
                }
    return effects


def test_allowlisted_constructors_have_no_nested_filesystem_effects():
    inits = constructors([path.read_text() for path in SOURCE.rglob("*.py")])
    found: set[str] = set()
    for entry in json.loads(REGISTRY.read_text())["boundaries"]:
        for call in entry["pure_or_core_calls"]:
            found |= nested_filesystem_effects(inits, call.rsplit(".", 1)[-1])
    assert not found, sorted(found)


def test_nested_scan_rejects_a_constructor_that_builds_a_touching_one():
    inits = constructors(
        [
            (
                "class Store:\n"
                "    def __init__(self, root):\n"
                "        root.mkdir(parents=True, exist_ok=True)\n"
            ),
            "class Owner:\n    def __init__(self, root):\n        Store(root)\n",
            "class Pure:\n    def __init__(self, root):\n        self.root = root\n",
        ]
    )
    assert nested_filesystem_effects(inits, "Owner") == {
        "Owner -> Store.__init__ calls mkdir"
    }
    assert nested_filesystem_effects(inits, "Pure") == set()


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
