"""Carry reviewed allowances through package splits using registry content only."""

from __future__ import annotations

import ast
import copy
import hashlib
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
CONTENT_IDENTITIES = TypeAdapter(dict[str, dict[str, str]])


def record_identities(document: dict, root: Path = ROOT) -> dict:
    """Snapshot only working-tree paths referenced by this registry."""
    paths: set[str] = set()

    def visit(value) -> None:
        if isinstance(value, str) and value.endswith(".py"):
            paths.add(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(
        {key: value for key, value in document.items() if key != "content_identities"}
    )
    return {
        **document,
        "content_identities": {
            path: identities((root / path).read_text())
            for path in sorted(paths)
            if (root / path).is_file()
        },
    }


def identities(source: str) -> dict[str, str]:
    """Function scope and normalized body digest; imports and positions are irrelevant."""
    tree = ast.parse(source)
    result: dict[str, str] = {}

    def digest(nodes: list[ast.stmt]) -> str:
        return hashlib.sha256(
            ast.dump(ast.Module(body=nodes, type_ignores=[])).encode()
        ).hexdigest()

    def visit(nodes: list[ast.stmt], scope: str = "") -> None:
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = scope + node.name
                result[name] = digest([node])
                visit(node.body, name + ".")

    visit(tree.body)
    statements = [
        node
        for node in tree.body
        if not isinstance(
            node, ast.Import | ast.ImportFrom | ast.FunctionDef | ast.AsyncFunctionDef
        )
        and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    if statements:
        result["<module>"] = digest(statements)
    return result


class PackageMoves:
    def __init__(
        self,
        root: Path = ROOT,
        recorded: object = None,
    ) -> None:
        self.root = root
        self.recorded = CONTENT_IDENTITIES.validate_python(
            {} if recorded is None else recorded, strict=True
        )
        self._targets: dict[str, dict[str, dict[str, str]]] = {}
        self._candidates: dict[str, list] = {}
        self._candidate_stamps: dict[str, tuple] = {}

    def targets(self, path: str) -> dict[str, dict[str, str]]:
        if path in self._targets:
            return self._targets[path]
        self._targets[path] = {}
        old = self.root / path
        package = old.with_suffix("")
        if old.exists() or old.suffix != ".py" or not package.is_dir():
            return {}
        previous = self.recorded.get(path, {})
        candidates = {
            file.relative_to(self.root).as_posix(): identities(file.read_text())
            for file in sorted(package.rglob("*.py"))
        }
        occurrences = Counter(
            (name, digest)
            for functions in candidates.values()
            for name, digest in functions.items()
        )
        self._targets[path] = {
            file: {
                name: digest
                for name, digest in functions.items()
                if previous.get(name) == digest and occurrences[(name, digest)] == 1
            }
            for file, functions in candidates.items()
        }
        return self._targets[path]

    def function(
        self,
        path: str,
        function: str,
        matches_site: Callable[[str, str], bool] | None = None,
    ) -> str:
        old = self.root / path
        package = old.with_suffix("")
        if old.suffix == ".rs":
            if old.exists():
                source = old.read_text()
                if re.search(r"\bfn\s+" + re.escape(function) + r"\b", source):
                    return path
                module_root = old.parent if old.name == "lib.rs" else package
                candidates = [
                    module_root / (name + ".rs")
                    for name in re.findall(r"(?m)^(?:pub\s+)?mod\s+(\w+)\s*;", source)
                ]
            elif package.is_dir():
                candidates = sorted(package.rglob("*.rs"))
            else:
                return path
            # Rust inventories predate body snapshots. Carry only a uniquely
            # named function with the same scanner finding, never a pooled
            # allowance or an ambiguous method shared by operation families.
            matches = [
                file.relative_to(self.root).as_posix()
                for file in candidates
                if file.is_file()
                and re.search(
                    r"\bfn\s+" + re.escape(function) + r"\b", file.read_text()
                )
                and matches_site is not None
                and matches_site(file.read_text(), function)
            ]
            return matches[0] if len(matches) == 1 else path
        if old.exists() or old.suffix != ".py" or not package.is_dir():
            return path
        return self._python_function(path, function, matches_site)[0]

    def scope(self, path: str, function: str) -> str:
        """Keep registry scopes attached to the concrete callable after extraction."""
        if "." not in function:
            # Some scanners deliberately register both a leaf and a qualified scope.
            return function
        return self._python_function(path, function, lambda source, name: True)[1]

    def _python_function(self, path, function, matches_site):
        old = self.root / path
        package = old.with_suffix("")
        if old.exists() or old.suffix != ".py" or not package.is_dir():
            return path, function
        digest = self.recorded.get(path, {}).get(function)
        matches = []
        files = sorted(package.rglob("*.py"))
        stamp = tuple(
            (file, file.stat().st_mtime_ns, file.stat().st_size) for file in files
        )
        if self._candidate_stamps.get(path) != stamp:
            self._candidate_stamps[path] = stamp
            self._candidates[path] = [
                (
                    file,
                    source,
                    identities(source),
                    extracted_identities(source, package),
                )
                for file in files
                for source in [file.read_text()]
            ]
        for file, source, current, extracted in self._candidates[path]:
            for name, value in current.items():
                if name.rsplit(".", 1)[-1] != function.rsplit(".", 1)[-1]:
                    continue
                if digest is not None:
                    eligible = digest in {value, extracted.get(name)}
                else:
                    eligible = matches_site is not None and (
                        matches_site(source, name) or matches_site(source, function)
                    )
                if eligible:
                    matches.append((file.relative_to(self.root).as_posix(), name))
        return matches[0] if len(matches) == 1 else (path, function)

    def paths(self, path: str) -> list[str]:
        return list(self.targets(path)) or [path]

    def counts(
        self, recorded: dict[str, int], current: dict[str, int]
    ) -> dict[str, int]:
        """Split per-file budgets only over unchanged, unique content; never pool debt."""
        result = dict(recorded)
        for path, budget in recorded.items():
            targets = self.targets(path)
            eligible = {
                file: current.get(file, 0)
                for file, matching in targets.items()
                if matching
                and matching == identities((self.root / file).read_text())
                and file not in recorded
            }
            if eligible and sum(eligible.values()) <= budget:
                result.pop(path)
                result.update(
                    {file: count for file, count in eligible.items() if count}
                )
                # Unspent allowance remains stale until the normal lowering writer runs.
                if remainder := budget - sum(eligible.values()):
                    result[path] = remainder
        return result


def extracted_identities(source: str, package: Path) -> dict[str, str]:
    """Recognize a method extraction by its unchanged body, not by Git history.

    An explicit self type replaces the class namespace's implicit receiver type.
    Relative imports gain one level when their owner becomes a package. New local
    imports of extracted siblings replace former same-module globals. These are
    the only structural normalizations; changed effects keep a different digest.
    """
    siblings = {file.stem for file in package.glob("*.py")}

    class RestoreExtraction(ast.NodeTransformer):
        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
            self.generic_visit(node)
            if node.args.args and node.args.args[0].arg == "self":
                if node.args.args[0].annotation is not None and node.body:
                    first = node.body[0]
                    if (
                        isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)
                    ):
                        lines = first.value.value.split("\n")
                        first.value.value = "\n".join(
                            [
                                lines[0],
                                *[
                                    "    " + line if line else line
                                    for line in lines[1:]
                                ],
                            ]
                        )
                node.args.args[0].annotation = None
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ImportFrom(self, node):
            if node.level == 1 and node.module in siblings:
                return None
            if node.level > 1:
                node.level -= 1
            return node

    tree = RestoreExtraction().visit(copy.deepcopy(ast.parse(source)))
    result: dict[str, str] = {}

    def visit(nodes, scope=""):
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = scope + node.name
                result[name] = hashlib.sha256(
                    ast.dump(ast.Module(body=[node], type_ignores=[])).encode()
                ).hexdigest()
                visit(node.body, name + ".")

    visit(tree.body)
    return result
