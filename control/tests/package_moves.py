"""Carry reviewed allowances through module-to-package splits by AST content.

The baseline ref supplies *source*, never identity: no revision or build ID is
compared. Writers must run before the split and its registry update land together.
Missing source, changed bodies and ambiguous copies receive no move credit.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
from collections import Counter
from collections.abc import Callable
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@cache
def baseline_source(path: str, reference: str = "origin/main") -> str | None:
    result = subprocess.run(
        ["git", "show", f"{reference}:{path}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


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
        source: Callable[[str], str | None] = baseline_source,
    ) -> None:
        self.root = root
        self.source = source
        self._targets: dict[str, dict[str, dict[str, str]]] = {}

    def targets(self, path: str) -> dict[str, dict[str, str]]:
        if path in self._targets:
            return self._targets[path]
        self._targets[path] = {}
        old = self.root / path
        package = old.with_suffix("")
        if old.exists() or old.suffix != ".py" or not package.is_dir():
            return {}
        source = self.source(path)
        if source is None:
            return {}
        previous = identities(source)
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

    def function(self, path: str, function: str) -> str:
        matches = [
            file for file, names in self.targets(path).items() if function in names
        ]
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
