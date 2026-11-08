"""A value the public contracts export has one owner: the contracts.

Restating ``MAX_RUNTIME_ARGUMENTS = 128`` beside the contract lets the two drift
(one contract, one path). The scan matches the NAME of every public UPPERCASE
module-level constant of ``vonk_forge_contracts``, not its value, so a copy
with a different number is caught too. Import the constant instead.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from functools import cache
from pathlib import Path

KIND = "constant restated from vonk_forge_contracts; import it from the contract"
SCOPES = ("src/", "control/src/", "agent_protocol/src/")
_UPPER = re.compile(r"[A-Z][A-Z0-9_]*")


def _module_files() -> list[Path]:
    spec = importlib.util.find_spec("vonk_forge_contracts")
    if spec is None or not spec.submodule_search_locations:
        return []
    return [
        path
        for root in spec.submodule_search_locations
        for path in Path(root).rglob("*.py")
    ]


@cache
def contract_constants() -> frozenset[str]:
    names: set[str] = set()
    for path in _module_files():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            names.update(
                target.id
                for target in targets
                if isinstance(target, ast.Name) and _UPPER.fullmatch(target.id)
            )
    return frozenset(names)


def scan_source(
    path: str, source: str, names: frozenset[str] | None = None
) -> list[int]:
    if not path.startswith(SCOPES):
        return []
    names = contract_constants() if names is None else names
    if not names:
        return []
    pattern = re.compile(
        r"^_?(?:" + "|".join(sorted(names)) + r")\s*(?::[^=\n]+)?=(?!=)"
    )
    return [
        line for line, text in enumerate(source.splitlines(), 1) if pattern.match(text)
    ]
