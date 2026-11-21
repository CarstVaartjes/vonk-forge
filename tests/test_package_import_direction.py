"""The four first-party packages import each other in one direction only.

The Controller (``vonk_control``) and the NAS helper (``vonk_nas_setup``) may
import the agent protocol; nothing else imports a sibling package. In
particular the server never imports vonkctl (``cluster_profiles``): facts flow
to the CLI through the generated client. Function-level imports count.
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
