"""A package ``__init__`` re-exports nothing: import from the owning module.

No module is an import path for a name it only imports either: every
``from module import name`` names the module that defines ``name``.

Covers the Controller, the platform packages (``cluster_profiles`` and its
sub-packages; the gitignored ``generated_control`` is generated code) and the
agent protocol."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (
    ROOT / "control/src/vonk_control",
    ROOT / "src/cluster_profiles",
    ROOT / "agent_protocol/src/vonk_agent_protocol",
)
GENERATED = ROOT / "src/cluster_profiles/generated_control"


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
        for package in PACKAGES
        for path in package.rglob("__init__.py")
        if not path.is_relative_to(GENERATED)
        if (relative := str(path.relative_to(ROOT)))
        if (lines := reexports(path.read_text(encoding="utf-8")))
    }
    assert not failures, f"Import from the module that defines the name: {failures}"


def _module_table() -> dict[str, tuple[Path, bool]]:
    return {
        ".".join(
            [package.name, *path.relative_to(package).with_suffix("").parts]
        ).removesuffix(".__init__"): (path, path.name == "__init__.py")
        for package in PACKAGES
        for path in package.rglob("*.py")
        if not path.is_relative_to(GENERATED)
    }


def _target(current: str, is_init: bool, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    package = current.split(".")
    package = package if is_init else package[:-1]
    package = package[: len(package) - (node.level - 1)]
    return ".".join([*package, *([node.module] if node.module else [])])


def _bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names a module defines, and names it only imports (module level)."""
    defined: set[str] = set()
    imported: set[str] = set()

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.ImportFrom | ast.Import):
                imported.update(
                    (alias.asname or alias.name).split(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.If | ast.Try | ast.With):
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(node, field, []))
                for handler in getattr(node, "handlers", []):
                    visit(handler.body)
            elif isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ):
                defined.add(node.name)
            elif isinstance(node, ast.TypeAlias):
                defined.add(node.name.id)
            elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                defined.update(
                    name.id
                    for target in targets
                    for name in ast.walk(target)
                    if isinstance(name, ast.Name)
                )

    visit(tree.body)
    return defined, imported - defined


def imports_through_re_importers(files: dict[str, tuple[str, bool]]) -> list[str]:
    """``from module import name`` where ``module`` only imports ``name``.

    ``files`` maps a dotted module name to its source and whether it is an
    ``__init__``. Importing a submodule by name (``from . import x``) is allowed."""
    trees = {name: ast.parse(source) for name, (source, _) in files.items()}
    only_imported = {name: _bindings(tree)[1] for name, tree in trees.items()}
    found = []
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            target = _target(name, files[name][1], node)
            if target not in files:
                continue
            found.extend(
                f"{name}:{node.lineno} {target}.{alias.name}"
                for alias in node.names
                if alias.name in only_imported[target]
                and f"{target}.{alias.name}" not in files
            )
    return found


def test_no_import_takes_a_name_from_a_module_that_only_re_imports_it() -> None:
    table = _module_table()
    files = {
        name: (path.read_text(encoding="utf-8"), is_init)
        for name, (path, is_init) in table.items()
    }
    assert not (found := imports_through_re_importers(files)), (
        f"Import each name from the module that defines it: {found}"
    )


def test_import_guard_sees_a_re_importing_module() -> None:
    files = {
        "pkg.a": ("class Thing: ...\n", False),
        "pkg.b": ("from .a import Thing\nOWN = 1\n", False),
        "pkg.c": ("from .b import Thing, OWN\n", False),
        "pkg.d": ("from . import a\nfrom pkg.a import Thing\n", False),
    }
    assert imports_through_re_importers(files) == ["pkg.c:1 pkg.b.Thing"]


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
