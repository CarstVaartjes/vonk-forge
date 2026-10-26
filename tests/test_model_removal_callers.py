"""NAS model files are removed only by an explicit removal or the space-pressure
collector, never by a background retention pass (owner rule: models stay until
space is short, then least recently used first).

Accepting a model removal is allowed from the explicit-removal modules only, and
each public wrapper has one caller module: the unused-storage collector reaches
the internal acceptance only through ``accept_unused_*``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "control/src/vonk_control"

#: call name (regular expression) -> the only modules that may call it
CALLERS = {
    r"_accept_model_removal": {
        "model_cache/fleet_removal.py",
        "model_cache/removal_review.py",
    },
    r"accept_unused_\w+": {"unused_storage_collection/removal.py"},
    r"remove_model_selector|remove_fleet_model_selector": {"model_cache_api.py"},
    r"accept_recipe_removal_child_in_session": {
        "recipe_image_availability/removal_acceptance.py"
    },
}


def _callers(pattern: str) -> set[str]:
    name = re.compile(pattern)
    found: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and name.fullmatch(node.func.attr)
            for node in ast.walk(tree)
        ):
            found.add(path.relative_to(SOURCE).as_posix())
    return found


def test_model_removal_is_accepted_only_by_its_owning_modules() -> None:
    """Catches a retention pass that removes cached model files with space to spare."""
    for pattern, allowed in CALLERS.items():
        callers = _callers(pattern)
        assert callers <= allowed, (pattern, sorted(callers - allowed))


def test_the_guard_still_sees_the_owning_callers() -> None:
    for pattern, allowed in CALLERS.items():
        assert _callers(pattern) == allowed, pattern
