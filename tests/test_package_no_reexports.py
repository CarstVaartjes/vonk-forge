"""A Controller package ``__init__`` re-exports nothing: import from the owning module."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "control/src/vonk_control"


def reexports(source: str) -> list[int]:
    """Lines that bind imported names or declare ``__all__``. Module-only imports (``from . import x``) are fine:
    ``models/`` uses them to register every mapped table."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module not in (None, "__future__"):
            lines.append(node.lineno)
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                lines.append(node.lineno)
    return sorted(lines)


def test_no_package_init_re_exports_names() -> None:
    failures = {
        relative: lines
        for path in PACKAGE.rglob("__init__.py")
        if (relative := str(path.relative_to(ROOT)))
        if (lines := reexports(path.read_text(encoding="utf-8")))
    }
    assert not failures, f"Import from the module that defines the name: {failures}"


def test_guard_sees_re_exports() -> None:
    assert reexports("from .x import Y\n") == [1]
    assert reexports("from vonk_control.x import Y\n") == [1]
    assert reexports("__all__ = ['Y']\n") == [1]
    assert not reexports("from . import x\nfrom .. import y\n")
