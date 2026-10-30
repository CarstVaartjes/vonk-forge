"""A question about one item costs what that item uses, not what else exists.

Principle P17 ("ask the target, not the world"): a review or a per-event
handler that asks about one installation, model, image or Spark answers with
lookups keyed by it. Every registered check below runs against the same target
twice, once beside ``SMALL`` units of unrelated history and once beside
``LARGE`` units, and the statements it executes, the rows it loads and the parses it makes
must be equal. There is no stored number: a target-keyed read passes at any size, and a
read that walks the collection fails as soon as the collection grows.

A check that still walks the collection is registered with the brief finding it
breaks (``KNOWN_VIOLATIONS``) and runs ``xfail(strict=True)``: the pull request
that fixes it deletes its entry, and a fix that lands silently turns the entry
into a failure. A new request-path review is added to ``TARGET_CHECKS``.
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pydantic
import pytest
from sqlalchemy import event
from vonk_control.artifact_reference_scan.image_references import (
    runtime_image_reference_findings,
)
from vonk_control.fleet_projection import FleetProjection
from vonk_control.models import Base
from vonk_control.operator_projection_api.errors import _node
from vonk_control.spark_store_retention import store_retention_request
from vonk_control.unused_storage_collection.common import spark_eviction_capacity

from .target_budget_world import World, build_world

SMALL = 1
LARGE = 10
# Functions that parse or re-hash a stored row. Each is counted wherever it was
# imported, so a new caller of one of them is counted too.
PARSE_POINTS = (
    "parse_stored_installation_plan",
    "read_recipe",
    "read_model",
    "read_stored_model",
    "document_sha256",
)


@dataclass(frozen=True)
class TargetCheck:
    """One question about one item, asked the way a request asks it."""

    run: Callable[[World], object]
    # The finding in request-path-scan-audit it breaks, while it does.
    finding: str | None = None
    reason: str = ""


def _retention(world: World) -> object:
    with world.sessions() as session:
        return store_retention_request(session, world.node, world.now)


def _eviction_capacity(world: World) -> object:
    with world.sessions() as session:
        return spark_eviction_capacity(session, world.node, world.now)


def _recipe_removal_review(world: World) -> object:
    with world.sessions() as session:
        return runtime_image_reference_findings(session, {world.target_archive})


def _uninstall_dependents(world: World) -> object:
    with world.sessions.begin() as session:
        return world.operations._model_dependents_on_nodes(
            session,
            world.target_model,
            {world.node},
            exclude_installation_ids=(world.target_installation,),
            lock=True,
        )


def _fleet_node_selector(world: World) -> object:
    """``GET /api/fleet/{selector}``: resolve one node the way the route does."""
    projection = FleetProjection(world.sessions, clock=lambda: world.now)
    return _node(projection.read(), world.node)


TARGET_CHECKS: dict[str, TargetCheck] = {
    "inventory_retention": TargetCheck(
        _retention,
        "A1",
        "every inventory POST reads all recipe heads, profiles and pointed documents",
    ),
    "load_review_eviction_capacity": TargetCheck(
        _eviction_capacity,
        "A2",
        "a load review rebuilds the whole evidence per node, then reads per installation",
    ),
    "recipe_removal_review": TargetCheck(
        _recipe_removal_review,
        "A3",
        "the image reference scan walks every profile, installation and run",
    ),
    "uninstall_dependents": TargetCheck(
        _uninstall_dependents,
        "A6",
        "the uninstall review loads and locks every live installation",
    ),
    "fleet_node_selector": TargetCheck(
        _fleet_node_selector,
        "A7",
        "a node route builds the whole fleet projection to find one node",
    ),
    "model_removal_review": TargetCheck(
        lambda world: world.cache.review_model_removal(world.target_model),
        "#1965",
        "fixed by PR #1965 (fix/removal-review-bounded); delete this finding there",
    ),
    "fleet_model_removal_review": TargetCheck(
        lambda world: world.cache.review_fleet_model_removal(world.target_model),
        "#1965",
        "fixed by PR #1965 (fix/removal-review-bounded); delete this finding there",
    ),
}


@dataclass(frozen=True)
class Cost:
    statements: int
    # Entities the ORM materialised: a read that selects whole rows to use two
    # columns, or to find the one that matches, shows here and not in the SQL.
    rows: int
    parses: dict[str, int]


def _count_parses(monkeypatch: pytest.MonkeyPatch, parses: Counter[str]) -> None:
    for name, module in list(sys.modules.items()):
        if not name.startswith("vonk_control"):
            continue
        for point in PARSE_POINTS:
            original = getattr(module, point, None)
            if original is None or not callable(original):
                continue
            monkeypatch.setattr(module, point, _counted(original, point, parses))
    original_validate = pydantic.TypeAdapter.validate_json

    def validate_json(self: object, *args: object, **kwargs: object) -> object:
        parses["TypeAdapter.validate_json"] += 1
        return original_validate(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pydantic.TypeAdapter, "validate_json", validate_json)


def _counted(
    original: Callable[..., object], point: str, parses: Counter[str]
) -> Callable[..., object]:
    def counted(*args: object, **kwargs: object) -> object:
        parses[point] += 1
        return original(*args, **kwargs)

    return counted


def measure(world: World, monkeypatch: pytest.MonkeyPatch, check: TargetCheck) -> Cost:
    """Run the check once for warm-up, then once counting statements and parses."""
    check.run(world)
    statements = [0]
    rows = [0]
    parses: Counter[str] = Counter()

    def count(*_args: object) -> None:
        statements[0] += 1

    def loaded(*_args: object) -> None:
        rows[0] += 1

    _count_parses(monkeypatch, parses)
    event.listen(world.engine, "before_cursor_execute", count)
    event.listen(Base, "load", loaded, propagate=True)
    try:
        check.run(world)
    finally:
        event.remove(world.engine, "before_cursor_execute", count)
        event.remove(Base, "load", loaded)
    return Cost(statements[0], rows[0], dict(parses))


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[int, World]]:
    root: Path = tmp_path_factory.mktemp("target-budget")
    built = {
        scale: build_world(root, f"x{scale}", scale=scale) for scale in (SMALL, LARGE)
    }
    try:
        yield built
    finally:
        for world in built.values():
            world.close()


def _entries() -> list[object]:
    return [
        pytest.param(
            name,
            marks=(
                [
                    pytest.mark.xfail(
                        strict=True, reason=f"{check.finding}: {check.reason}"
                    )
                ]
                if check.finding
                else []
            ),
            id=name,
        )
        for name, check in TARGET_CHECKS.items()
    ]


# Slow by design: it seeds two databases once and runs each check four times.
@pytest.mark.slow(30)
@pytest.mark.parametrize("name", _entries())
def test_cost_of_a_target_does_not_grow_with_unrelated_history(
    name: str, worlds: dict[int, World], monkeypatch: pytest.MonkeyPatch
) -> None:
    check = TARGET_CHECKS[name]
    small = measure(worlds[SMALL], monkeypatch, check)
    large = measure(worlds[LARGE], monkeypatch, check)
    assert large == small, (
        f"{name}: {SMALL}x history -> {small}; {LARGE}x history -> {large}"
    )
