"""Every console script names a module that defines its callable."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECTS = (
    "pyproject.toml",
    "control/pyproject.toml",
    "agent_protocol/pyproject.toml",
    "nas_setup/pyproject.toml",
)


def _module_file(project: Path, module: str) -> Path | None:
    parts = module.split(".")
    for source in (project / "src", project):
        base = source.joinpath(*parts)
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file():
                return candidate
    return None


def _defines(path: Path, name: str) -> bool:
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.ImportFrom | ast.Import):
            targets = (
                [alias.asname or alias.name.split(".")[0] for alias in node.names]
                if isinstance(node, ast.ImportFrom | ast.Import)
                else [
                    t.id
                    for t in (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    if isinstance(t, ast.Name)
                ]
            )
            if name in targets:
                return True
    return False


def test_console_scripts_resolve_to_a_defined_callable() -> None:
    broken = []
    for pyproject in PYPROJECTS:
        project = (ROOT / pyproject).parent
        scripts = tomllib.loads((ROOT / pyproject).read_text(encoding="utf-8"))
        for script, target in scripts.get("project", {}).get("scripts", {}).items():
            module, _, attr = target.partition(":")
            found = _module_file(project, module)
            if found is None or not _defines(found, attr):
                broken.append(f"{pyproject}: {script} = {target}")
    assert broken == []
