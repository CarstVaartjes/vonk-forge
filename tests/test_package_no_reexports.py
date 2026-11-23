"""A package ``__init__`` re-exports nothing: import from the owning module.

No module is an import path for a name it only imports either: every
``from module import name`` names the module that defines ``name``.

Covers the Controller, the platform packages (``cluster_profiles`` and its
sub-packages; the gitignored ``generated_control`` is generated code) and the
agent protocol."""

import ast
import re
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


FROM_IMPORT = re.compile(
    r"^[ \t]*from[ \t]+(?P<module>(?:vonk_control|cluster_profiles|vonk_agent_protocol)[\w.]*)"
    r"[ \t]+import[ \t]+(?P<names>\([^)]*\)|[^\n]+)",
    re.MULTILINE,
)
TEST_ROOTS = (ROOT / "tests", ROOT / "control/tests", ROOT / "agent_protocol/tests")


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


def _module_names(
    current: str, files: dict[str, tuple[str, bool]], tree: ast.Module
) -> dict[str, str]:
    """Local names (or dotted paths) a file binds to a module of ``files``."""
    local: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = _target(current, files[current][1], node)
            local.update(
                {
                    alias.asname or alias.name: f"{target}.{alias.name}"
                    for alias in node.names
                    if f"{target}.{alias.name}" in files
                }
            )
        elif isinstance(node, ast.Import):
            local.update(
                {
                    alias.asname or alias.name: alias.name
                    for alias in node.names
                    if alias.name in files
                }
            )
    return local


def _bindings(tree: ast.Module, modules: dict[str, str]) -> set[str]:
    """Names a module only imports at module level, ``Name = module.Name`` included."""
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
                names = {
                    name.id
                    for target in targets
                    for name in ast.walk(target)
                    if isinstance(name, ast.Name)
                }
                value = getattr(node, "value", None)
                if (
                    isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id in modules
                    and names == {value.attr}
                ):
                    imported.update(names)
                else:
                    defined.update(names)

    visit(tree.body)
    return imported - defined


def imports_through_re_importers(
    files: dict[str, tuple[str, bool]], importers: dict[str, str] | None = None
) -> list[str]:
    """``from module import name`` or ``module.name`` where ``module`` only imports ``name``.

    ``files`` maps a dotted module name to its source and whether it is an
    ``__init__``; they are checked for both forms. ``importers`` maps a label to the
    source of a file (a test), scanned by text for the from-import form only. Importing a
    submodule by name (``from . import x``) is allowed."""
    trees = {name: ast.parse(source) for name, (source, _) in files.items()}
    bound = {name: _module_names(name, files, tree) for name, tree in trees.items()}
    only_imported = {name: _bindings(tree, bound[name]) for name, tree in trees.items()}

    def through_facade(module: str | None, name: str) -> bool:
        return (
            module in files
            and name in only_imported[module]
            and f"{module}.{name}" not in files
        )

    found = []
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = _target(name, files[name][1], node)
                found.extend(
                    f"{name}:{node.lineno} {target}.{alias.name}"
                    for alias in node.names
                    if through_facade(target, alias.name)
                )
            elif isinstance(node, ast.Attribute):
                module = (
                    bound[name].get(node.value.id)
                    if isinstance(node.value, ast.Name)
                    else None
                )
                if module and through_facade(module, node.attr):
                    found.append(f"{name}:{node.lineno} {module}.{node.attr}")
    for label, source in (importers or {}).items():
        for match in FROM_IMPORT.finditer(source):
            names = re.sub(r"#[^\n]*", "", match["names"]).strip("() \n")
            found.extend(
                f"{label}:{source.count(chr(10), 0, match.start()) + 1} {match['module']}.{name}"
                for item in names.split(",")
                if (name := item.split(" as ")[0].strip())
                and through_facade(match["module"], name)
            )
    return found


def test_no_import_takes_a_name_from_a_module_that_only_re_imports_it() -> None:
    files = {
        name: (path.read_text(encoding="utf-8"), is_init)
        for name, (path, is_init) in _module_table().items()
    }
    importers = {
        str(path.relative_to(ROOT)): text
        for root in TEST_ROOTS
        for path in root.rglob("*.py")
        if any(
            package.name in (text := path.read_text(encoding="utf-8"))
            for package in PACKAGES
        )
    }
    assert not (found := imports_through_re_importers(files, importers)), (
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


def test_import_guard_sees_attribute_access_and_alias_definitions() -> None:
    files = {
        "pkg.a": ("class Thing: ...\n", False),
        "pkg.b": ("from . import a\nThing = a.Thing\nOWN = 1\n", False),
        "pkg.c": ("import pkg.b as z\nz.Thing\nz.OWN\n", False),
        "pkg.d": ("from pkg import b\nb.Thing\n", False),
        "pkg.e": ("from . import b\nb.Thing\n", False),
        "pkg.f": ("from . import b\nb.OWN\n", False),
    }
    assert imports_through_re_importers(files) == [
        "pkg.c:2 pkg.b.Thing",
        "pkg.d:2 pkg.b.Thing",
        "pkg.e:2 pkg.b.Thing",
    ]


def test_import_guard_checks_test_files_as_importers() -> None:
    files = {
        "vonk_control.a": ("class Thing: ...\n", False),
        "vonk_control.b": ("from .a import Thing\n", False),
    }
    importers = {
        "tests/test_x.py": "from vonk_control.b import (\n    Thing,  # c\n)\n"
        "from vonk_control.a import Thing\n"
    }
    assert imports_through_re_importers(files, importers) == [
        "tests/test_x.py:1 vonk_control.b.Thing"
    ]


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
