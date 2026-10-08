"""Fixture-tested syntax detection; no allowances or historical counts."""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (
    REPO_ROOT / "control" / "src",
    REPO_ROOT / "agent_protocol" / "src",
    REPO_ROOT / "src" / "cluster_profiles",
    REPO_ROOT / "scripts",
)

_MAPPING_NAMES = frozenset({"Mapping", "MutableMapping", "dict", "Dict"})
_UNTYPED_VALUES = frozenset({"object", "Any", "JsonValue"})


@dataclass(frozen=True)
class Site:
    path: str
    function: str
    annotation: str
    line: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.function, self.annotation)

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.annotation} in {self.function}"


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def is_untyped_mapping(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    if _name(node.value) not in _MAPPING_NAMES:
        return False
    slice_ = node.slice
    if not isinstance(slice_, ast.Tuple) or len(slice_.elts) != 2:
        return False
    key, value = slice_.elts
    return _name(key) == "str" and _has_untyped_leaf(value)


def _has_untyped_leaf(node: ast.AST) -> bool:
    """True when ``object``, ``Any`` or ``JsonValue`` occurs anywhere in the value type.

    ``dict[str, object | None]`` and ``dict[str, list[object]]`` are as untyped as
    ``dict[str, object]``; adding a ``| None`` or a container does not type the data.
    """

    return any(
        _name(child) in _UNTYPED_VALUES
        for child in ast.walk(node)
        if isinstance(child, ast.Name | ast.Attribute)
    )


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.sites: list[Site] = []

    def _enter(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, node.name)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if is_untyped_mapping(node):
            self.sites.append(
                Site(
                    path=self.path,
                    function=".".join(self.scope) or "<module>",
                    annotation=ast.unparse(node),
                    line=node.lineno,
                )
            )
            return
        self.generic_visit(node)


_CANDIDATE = re.compile(r"\[\s*str\s*,.*(?:object|Any|JsonValue)", re.DOTALL)


def scan_source(source: str, *, path: str) -> list[Site]:
    if _CANDIDATE.search(source) is None:
        return []
    collector = _Collector(path)
    collector.visit(ast.parse(source))
    return collector.sites


def scan_sites(roots: Sequence[Path] = SOURCE_ROOTS) -> list[Site]:
    sites: list[Site] = []
    for root in roots:
        for module in sorted(root.rglob("*")):
            if "generated_control" in module.parts:
                # Generated extension dictionaries are owned by the API generator.
                continue
            if not module.is_file() or not (
                module.suffix == ".py"
                or (
                    not module.suffix
                    and module.read_bytes().startswith(b"#!")
                    and b"python" in module.read_bytes().splitlines()[0]
                )
            ):
                continue
            relative = module.relative_to(REPO_ROOT).as_posix()
            sites.extend(scan_source(module.read_text(encoding="utf-8"), path=relative))
    return sites
