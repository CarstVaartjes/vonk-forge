"""Carry reviewed allowances through package splits using registry content only."""

from __future__ import annotations

import ast
import hashlib
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
                if not isinstance(node, ast.ClassDef):
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
        if old.exists() or old.suffix != ".py" or not package.is_dir():
            return path
        previous = self.recorded.get(path, {})
        digest = previous.get(function)
        matches = []
        for file in sorted(package.rglob("*.py")):
            source = file.read_text()
            for name, current in identities(source).items():
                if name.rsplit(".", 1)[-1] != function.rsplit(".", 1)[-1]:
                    continue
                if digest is not None:
                    eligible = current == digest
                else:
                    eligible = matches_site is not None and matches_site(source, name)
                if eligible:
                    matches.append(file.relative_to(self.root).as_posix())
        return matches[0] if len(matches) == 1 else path

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
