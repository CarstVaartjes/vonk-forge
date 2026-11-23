"""A Controller package ``__init__`` re-exports nothing: import from the owning module."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "control/src/vonk_control"


def reexports(source: str) -> list[int]:
    """Lines of an ``__init__`` body beyond the allowed few.

    Allowed: a docstring, ``from __future__``, module-only ``from . import x`` /
    ``from .. import x`` (``models/`` registers every mapped table this way) and
    ``__version__``. Anything else, such as a name import, ``__all__``, a lazy
    ``__getattr__`` or ``Name = x.Name``, re-exports. ``if TYPE_CHECKING:`` blocks
    are ignored."""
    lines = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.ImportFrom) and (
            node.module == "__future__" or (node.module is None and node.level >= 1)
        ):
            continue
        if isinstance(node, ast.If) and ast.unparse(node.test) in {
            "TYPE_CHECKING",
            "typing.TYPE_CHECKING",
        }:
            continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__version__"
        ):
            continue
        lines.append(node.lineno)
    return lines


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
    assert reexports("def __getattr__(name):\n    return name\n") == [1]
    assert reexports("from . import x\nName = x.Name\n") == [2]
    assert reexports("import x\n") == [1]
    assert not reexports(
        '"""Doc."""\nfrom __future__ import annotations\nfrom . import x\n'
        "from .. import y\n__version__ = '1'\n"
        "if TYPE_CHECKING:\n    from .x import Y\n"
    )
