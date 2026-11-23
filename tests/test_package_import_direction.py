"""The four first-party packages import each other in one direction only.

The Controller (``vonk_control``) and the NAS helper (``vonk_nas_setup``) may
import the agent protocol; nothing else imports a sibling package. In
particular the server never imports vonkctl (``cluster_profiles``): facts flow
to the CLI through the generated client. Function-level imports count.

Within and across the four packages the runtime import graph has no cycle:
every import outside ``if TYPE_CHECKING:`` is an edge, and so is each ancestor
package ``__init__`` of an imported module that the importer is not inside.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "cluster_profiles": "src/cluster_profiles",
    "vonk_control": "control/src/vonk_control",
    "vonk_agent_protocol": "agent_protocol/src/vonk_agent_protocol",
    "vonk_nas_setup": "nas_setup/src/vonk_nas_setup",
}
ALLOWED = {
    ("vonk_control", "vonk_agent_protocol"),
    # Vendored into the portable wheel by its build hook.
    ("vonk_nas_setup", "vonk_agent_protocol"),
}


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def imported_packages(source: str) -> set[str]:
    """First-party packages a module imports, at any depth, outside TYPE_CHECKING."""
    found: set[str] = set()
    pending: list[ast.AST] = [ast.parse(source)]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            pending.extend(node.orelse)
            continue
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
        pending.extend(ast.iter_child_nodes(node))
    return found & PACKAGES.keys()


def violations(sources: dict[str, dict[str, str]]) -> list[str]:
    """``{package: {file: source}}`` -> the imports the direction rule forbids."""
    return sorted(
        f"{package}: {file} imports {target}"
        for package, files in sources.items()
        for file, source in files.items()
        for target in imported_packages(source) - {package}
        if (package, target) not in ALLOWED
    )


def test_packages_import_only_in_the_allowed_direction() -> None:
    sources = {
        package: {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted((ROOT / directory).rglob("*.py"))
        }
        for package, directory in PACKAGES.items()
    }
    assert all(sources.values()), "a package directory moved; update PACKAGES"
    assert not violations(sources)


def test_guard_sees_function_level_imports_and_frees_type_checking() -> None:
    server = {
        "vonk_control": {
            "a.py": "def late():\n    from cluster_profiles.runtime_identity import x\n",
            "b.py": "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    import cluster_profiles\n",
            "c.py": "from vonk_agent_protocol import y\nimport vonk_control.z\n",
        },
        "vonk_agent_protocol": {"d.py": "import vonk_control\n"},
    }
    assert violations(server) == [
        "vonk_agent_protocol: d.py imports vonk_control",
        "vonk_control: a.py imports cluster_profiles",
    ]


def test_the_controller_does_not_install_vonkctl() -> None:
    """vonkctl is a test client in the dev group, never a runtime dependency."""
    project = tomllib.loads((ROOT / "control/pyproject.toml").read_text())
    assert not [
        dependency
        for dependency in project["project"]["dependencies"]
        if dependency.lower().startswith("vonk-cluster-profiles")
    ]


def _runtime_import_targets(module: str, is_package: bool, source: str) -> set[str]:
    """Dotted names a module may load at runtime, with their ancestor packages."""
    own_package = module if is_package else module.rpartition(".")[0]
    named: set[str] = set()
    pending: list[ast.AST] = [ast.parse(source)]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            pending.extend(node.orelse)
            continue
        if isinstance(node, ast.Import):
            named.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = own_package.split(".")
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parts, *([node.module] if node.module else [])])
            named.add(base)
            named.update(f"{base}.{alias.name}" for alias in node.names)
        pending.extend(ast.iter_child_nodes(node))
    parts = module.split(".")
    inside = {".".join(parts[:end]) for end in range(1, len(parts))}
    if is_package:
        inside.add(module)
    targets: set[str] = set()
    for name in named:
        pieces = name.split(".")
        targets.update(".".join(pieces[:end]) for end in range(1, len(pieces) + 1))
    return targets - inside - {module}


def import_cycles(modules: dict[str, tuple[bool, str]]) -> list[list[str]]:
    """``{module: (is_package, source)}`` -> its strongly connected components."""
    graph = {
        module: sorted(_runtime_import_targets(module, *entry) & modules.keys())
        for module, entry in modules.items()
    }
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, position = work.pop()
            if position == 0:
                index[node] = low[node] = len(index)
                stack.append(node)
                on_stack.add(node)
            neighbours = graph[node]
            if position < len(neighbours):
                work.append((node, position + 1))
                child = neighbours[position]
                if child not in index:
                    work.append((child, 0))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    cycles.append(sorted(component))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return cycles


def test_the_packages_import_one_another_without_a_cycle() -> None:
    modules: dict[str, tuple[bool, str]] = {}
    for package, directory in PACKAGES.items():
        root = (ROOT / directory).parent
        for path in sorted((ROOT / directory).rglob("*.py")):
            if "generated_control" in path.parts:
                continue
            parts = path.relative_to(root).with_suffix("").parts
            is_package = parts[-1] == "__init__"
            name = ".".join(parts[:-1] if is_package else parts)
            modules[name] = (is_package, path.read_text(encoding="utf-8"))
        assert package in modules, "a package directory moved; update PACKAGES"
    assert not import_cycles(modules)


def test_cycle_guard_reports_a_two_module_cycle_and_frees_type_checking() -> None:
    modules = {
        "p": (True, ""),
        "p.a": (False, "from . import b\n"),
        "p.b": (False, "def late():\n    from .a import x\n"),
        "p.c": (False, "from p import a\n"),
        "p.d": (
            False,
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import p.c\n",
        ),
    }
    assert import_cycles(modules) == [["p.a", "p.b"]]
