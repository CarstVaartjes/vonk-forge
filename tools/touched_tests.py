"""Select the test files a patch touches: ``scripts/test changed``.

Reads a unified diff (stdin or a file) and prints ``<suite> <test file>`` lines,
suite being ``control`` (control/tests) or ``repository`` (tests/ and
agent_protocol/tests). A test file is selected when the patch edits it, or when
it imports, directly or through test support modules and conftests, a module the
patch touches. Importing a package or submodule executes the package
``__init__`` files above it, so a touched ``__init__.py`` selects every
importer of anything inside the package. The graph is static (``ast``) and
covers only the test trees; source modules are not followed.
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_TREES = ("tests", "control/tests", "agent_protocol/tests")
SUITE_OF = {
    "control/tests": "control",
    "tests": "repository",
    "agent_protocol/tests": "repository",
}
# Directories dotted imports resolve against, besides the importing file's own.
IMPORT_BASES = (
    "",
    "src",
    "control",
    "control/src",
    "agent_protocol/src",
    "nas_setup/src",
)


def _suite(path: str) -> str | None:
    for tree in TEST_TREES:
        if path.startswith(tree + "/"):
            return SUITE_OF[tree]
    return None


def is_test_file(path: str) -> bool:
    return (
        _suite(path) is not None
        and Path(path).name.startswith("test_")
        and path.endswith(".py")
    )


def _imports(source: str, package: list[str]) -> set[str]:
    """Dotted names a module may import, relative ones made absolute."""
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                prefix = ".".join([*base, node.module] if node.module else base)
            else:
                prefix = node.module or ""
            if prefix:
                names.add(prefix)
                names.update(f"{prefix}.{alias.name}" for alias in node.names)
    return names


def _resolve(name: str, own_dir: str, root: Path) -> set[str]:
    """Repository files executed by importing ``name``: the module, or the longest
    existing prefix of it, plus every package ``__init__`` above it."""
    parts = name.split(".")
    for base in (own_dir, *IMPORT_BASES):
        for end in range(len(parts), 0, -1):
            relative = "/".join(parts[:end])
            for candidate in (f"{relative}.py", f"{relative}/__init__.py"):
                path = f"{base}/{candidate}" if base else candidate
                if (root / path).is_file():
                    found = {path}
                    for above in range(1, end):
                        init = "/".join(parts[:above]) + "/__init__.py"
                        init = f"{base}/{init}" if base else init
                        if (root / init).is_file():
                            found.add(init)
                    return found
    return set()


def _tree_files(root: Path) -> list[str]:
    files = []
    for tree in TEST_TREES:
        for path in sorted((root / tree).rglob("*.py")):
            if "__pycache__" not in path.parts:
                files.append(path.relative_to(root).as_posix())
    return files


def select(touched: list[str], root: Path = ROOT) -> list[tuple[str, str]]:
    """``(suite, test file)`` for the touched repository paths, sorted."""
    dependents: dict[str, set[str]] = defaultdict(set)
    for path in _tree_files(root):
        own_dir = str(Path(path).parent)
        package = Path(path).with_suffix("").parts[:-1]
        source = (root / path).read_text(encoding="utf-8", errors="ignore")
        for name in _imports(source, list(package)):
            for target in _resolve(name, own_dir, root):
                if target != path:
                    dependents[target].add(path)
        if Path(path).name == "conftest.py":
            for other in _tree_files_under(root, own_dir):
                dependents[path].add(other)
    selected: set[str] = set()
    seen: set[str] = set()
    pending = [path for path in touched if (root / path).is_file()]
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        if is_test_file(path):
            selected.add(path)
        pending.extend(dependents.get(path, ()))
    return sorted((_suite(path) or "", path) for path in selected)


def _tree_files_under(root: Path, directory: str) -> list[str]:
    return [
        path.relative_to(root).as_posix()
        for path in sorted((root / directory).rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def touched_paths(patch: str) -> list[str]:
    sys.path.insert(0, str(ROOT))
    from control.tests.added_line_guards.patch import added_lines

    return sorted(added_lines(patch))


def main() -> int:
    arguments = sys.argv[1:]
    patch = (
        Path(arguments[0]).read_text(encoding="utf-8")
        if arguments and arguments[0] != "-"
        else sys.stdin.read()
    )
    extra = arguments[1:]
    for suite, path in select([*touched_paths(patch), *extra]):
        print(suite, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
