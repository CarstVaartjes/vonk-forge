"""NAS model files are removed only by an explicit removal or the space-pressure
collector, never by a background retention pass (owner rule: models stay until
space is short, then least recently used first).

Accepting a model removal is allowed from the explicit-removal modules only. The
unused-storage collector reaches it through ``removal_review``.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "control/src/vonk_control"
ALLOWED = {
    "model_cache/fleet_removal.py",
    "model_cache/removal_review.py",
}


def _callers() -> set[str]:
    found: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_accept_model_removal"
            for node in ast.walk(tree)
        ):
            found.add(path.relative_to(SOURCE).as_posix())
    return found


def test_model_removal_is_accepted_only_by_explicit_removal_modules() -> None:
    """Catches a retention pass that removes cached model files with space to spare."""
    assert _callers() <= ALLOWED, sorted(_callers() - ALLOWED)


def test_the_guard_still_sees_the_explicit_callers() -> None:
    assert _callers() == ALLOWED
