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
from functools import cache
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
    # Public, but no production caller may accept a removal through it.
    r"accept_removal_for_sets_in_session": set(),
}

#: modules that define the wrappers around the internal acceptance
ACCEPTING_MODULES = ("model_cache/fleet_removal.py", "model_cache/removal_review.py")


@cache
def _attribute_calls() -> dict[str, frozenset[str]]:
    """Attribute call names per module, parsed once for every pattern."""
    return {
        path.relative_to(SOURCE).as_posix(): frozenset(
            node.func.attr
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        )
        for path in SOURCE.rglob("*.py")
    }


def _callers(pattern: str) -> set[str]:
    name = re.compile(pattern)
    return {
        module
        for module, calls in _attribute_calls().items()
        if any(name.fullmatch(call) for call in calls)
    }


def test_model_removal_is_accepted_only_by_its_owning_modules() -> None:
    """Catches a retention pass that removes cached model files with space to spare."""
    for pattern, allowed in CALLERS.items():
        callers = _callers(pattern)
        assert callers <= allowed, (pattern, sorted(callers - allowed))


def test_the_guard_still_sees_the_owning_callers() -> None:
    for pattern, allowed in CALLERS.items():
        assert _callers(pattern) == allowed, pattern


def _accepting_wrappers() -> set[str]:
    wrappers: set[str] = set()
    for relative in ACCEPTING_MODULES:
        tree = ast.parse((SOURCE / relative).read_text())
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", getattr(node.func, "id", None))
                == "_accept_model_removal"
                for node in ast.walk(function)
            ):
                wrappers.add(function.name)
    return wrappers


def test_every_wrapper_of_the_acceptance_has_pinned_callers() -> None:
    """A new public wrapper around the acceptance cannot bypass the caller pins."""
    unpinned = {
        name
        for name in _accepting_wrappers()
        if not any(re.fullmatch(pattern, name) for pattern in CALLERS)
    }
    assert not unpinned, sorted(unpinned)
