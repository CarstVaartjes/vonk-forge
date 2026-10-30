"""A question about one item costs what that item uses, not what else exists.

Principle P17 ("ask the target, not the world"): a review or a per-event
handler that asks about one installation, model, image or Spark answers with
lookups keyed by it. Every registered check below runs against the same target
twice, once beside ``SMALL`` units of unrelated history and once beside
``LARGE`` units, and the statements it executes, the rows the driver hands
back and the parses it makes must be equal. There is no stored number: a
target-keyed read passes at any size, and a read that walks the collection
fails as soon as the collection grows.

What grows with a unit: finished jobs, saved profiles and the selectors they
name, catalog models and recipes, cached sets, and installations (each with its
own recipe, build, archive and image) on the other Spark. What does not grow:
the three installations on the target's own Spark and its store, which are the
input of a question about that Spark.

A check that still walks the collection is registered with the finding it
breaks (``finding``) and runs ``xfail(strict=True)``: the pull request that
fixes it deletes the finding, and a fix that lands silently turns the entry
into a failure. A check that already asks the target is registered without a
finding as a positive control. A new request-path review is added to
``TARGET_CHECKS``.

Blind spot: rows are counted, not bytes, and SQLite ignores ``FOR UPDATE``. A
read of the same number of rows that loads a 100 KB document where a column
would do, or locks every row, is not seen here.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pydantic
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from vonk_agent_protocol.inventory import ModelStoreObject
from vonk_control.artifact_reference_scan.image_references import (
    runtime_image_reference_findings,
)
from vonk_control.auth import Actor
from vonk_control.fleet_projection import FleetProjection
from vonk_control.models import (
    CatalogDocumentRevision,
    InstallationNode,
    RecipeBuild,
    RecipeInstallation,
)
from vonk_control.operator_projection_api.routes import (
    install_operator_projection_routes,
)
from vonk_control.recipe_execution_contract import parse_stored_installation_plan
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_control.spark_store_retention import store_retention_request
from vonk_control.unused_storage_collection.common import spark_eviction_capacity
from vonk_forge_contracts import read_recipe

from .runtime_image_fixtures import place_test_image
from .target_budget_world import SPARK_OBJECTS, World, _hex, build_world
from .test_fleet_model_removal_postgres import inventory
from .test_recipe_image_availability import _runtime

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
# ``TypeAdapter`` is how a profile's assignments and a plan are validated.
ADAPTER_PARSES = ("validate_json", "validate_python")


@dataclass(frozen=True)
class TargetCheck:
    """One question about one item, asked the way a request asks it."""

    run: Callable[[World], object]
    # The finding in request-path-scan-audit it breaks, while it does.
    finding: str | None = None
    reason: str = ""
    # A request that changes state cannot be repeated: measure its first run.
    once: bool = False


def _retention(world: World) -> object:
    with world.sessions() as session:
        return store_retention_request(session, world.node, world.now)


def _eviction_capacity(world: World) -> object:
    with world.sessions() as session:
        return spark_eviction_capacity(session, world.node, world.now)


def _recipe_service(world: World) -> RecipeImageAvailabilityService:
    """The recipe cache service over the target recipe, its image in storage."""
    storage = FilesystemRuntimeImageStorage(world.root / f"images-{world.scale}")
    place_test_image(storage, world.target_archive, 30)
    with world.sessions() as session:
        installation = session.get(RecipeInstallation, world.target_installation)
        assert installation is not None
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        recipe = read_recipe(revision.document)
    return RecipeImageAvailabilityService(
        world.sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        model_cache=world.cache,
        clock=lambda: world.now,
    )


_SERVICES: dict[int, RecipeImageAvailabilityService] = {}


def _service(world: World) -> RecipeImageAvailabilityService:
    return _SERVICES.setdefault(id(world), _recipe_service(world))


def _recipe_removal_review(world: World) -> object:
    """``GET /api/recipe/{selector}/remove-review?with_model=true``."""
    return _service(world).review_removal(world.target_recipe_slug, with_model=True)


def _recipe_removal_accept(world: World) -> object:
    """``POST /api/recipe/{selector}/remove`` (a new request each time)."""
    return _service(world).remove_selector(
        world.target_recipe_slug,
        actor="operator",
        request_id=str(uuid.uuid4()),
        with_model=True,
    )


def _image_reference_scan(world: World) -> object:
    with world.sessions() as session:
        return runtime_image_reference_findings(session, {world.target_archive})


def _uninstall_review(world: World) -> object:
    """The uninstall preview, which the reconcile preview route and the phase call."""
    return world.operations.preview_uninstall(world.target_installation)


def _fleet_node_selector(world: World) -> object:
    """``GET /api/fleet/{selector}`` through the real route."""
    app = FastAPI()
    install_operator_projection_routes(
        app,
        actor_dependency=Depends(lambda: Actor("viewer", "viewer")),
        fleet_projection=FleetProjection(world.sessions, clock=lambda: world.now),
        library_projection=None,
    )
    response = TestClient(app).get(f"/api/fleet/{world.node}")
    assert response.status_code == 200, response.text
    return response.json()


def _inventory_record(world: World) -> object:
    """``POST /agent/inventory`` recording one Spark's report (audit A14)."""
    return inventory(
        world.sessions,
        world.node,
        world.now,
        [
            ModelStoreObject(sha256=_hex(f"spark/{n}"), bytes=3)
            for n in range(SPARK_OBJECTS)
        ],
    )


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
        "the image and model reference scans walk every profile, installation and run",
    ),
    # Positive control: accepting only queues the removal; the impact review
    # runs in the worker's observe step, not in the request.
    "recipe_removal_accept": TargetCheck(_recipe_removal_accept, once=True),
    "image_reference_scan": TargetCheck(
        _image_reference_scan,
        "A3",
        "the scan walks every profile, installation and run",
    ),
    "uninstall_review": TargetCheck(
        _uninstall_review,
        "A6",
        "the uninstall review loads every live installation",
    ),
    "fleet_node_selector": TargetCheck(
        _fleet_node_selector,
        "A7",
        "a node route builds the whole fleet projection to find one node (and A8's parses)",
    ),
    "model_removal_review": TargetCheck(
        lambda world: world.cache.review_model_removal(world.target_model),
        "#1965",
        "PR #1965 (fix/removal-review-bounded) bounds it; the finding goes when it does",
    ),
    "fleet_model_removal_review": TargetCheck(
        lambda world: world.cache.review_fleet_model_removal(world.target_model),
        "#1965",
        "PR #1965 (fix/removal-review-bounded) bounds it; the finding goes when it does",
    ),
    # Positive control: a Spark's report is keyed by its node (audit A14).
    "inventory_record": TargetCheck(_inventory_record),
}


@dataclass(frozen=True)
class Cost:
    statements: int
    rows: int
    parses: dict[str, int]


def _count_parses(monkeypatch: pytest.MonkeyPatch, parses: Counter[str]) -> None:
    for name, module in list(sys.modules.items()):
        if not name.startswith("vonk_control"):
            continue
        for point in PARSE_POINTS:
            original = getattr(module, point, None)
            if original is not None and callable(original):
                monkeypatch.setattr(module, point, _counted(original, point, parses))
    for method in ADAPTER_PARSES:
        original = getattr(pydantic.TypeAdapter, method)
        monkeypatch.setattr(
            pydantic.TypeAdapter,
            method,
            _counted_method(original, f"TypeAdapter.{method}", parses),
        )


def _counted(
    original: Callable[..., object], point: str, parses: Counter[str]
) -> Callable[..., object]:
    def counted(*args: object, **kwargs: object) -> object:
        parses[point] += 1
        return original(*args, **kwargs)

    return counted


def _counted_method(
    original: Callable[..., object], point: str, parses: Counter[str]
) -> Callable[..., object]:
    def counted(self: object, *args: object, **kwargs: object) -> object:
        parses[point] += 1
        return original(self, *args, **kwargs)

    return counted


def measure(world: World, monkeypatch: pytest.MonkeyPatch, check: TargetCheck) -> Cost:
    """Run the check once for warm-up, then once counting what it reads."""
    if not check.once:
        check.run(world)
    statements = [0]
    parses: Counter[str] = Counter()

    def count(*_args: object) -> None:
        statements[0] += 1

    _count_parses(monkeypatch, parses)
    event.listen(world.engine, "before_cursor_execute", count)
    fetched = world.fetched[0]
    try:
        check.run(world)
    finally:
        event.remove(world.engine, "before_cursor_execute", count)
    return Cost(statements[0], world.fetched[0] - fetched, dict(parses))


@pytest.fixture(scope="module")
def worlds(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[int, World]]:
    root: Path = tmp_path_factory.mktemp("target-budget")
    built = {}
    for scale in (SMALL, LARGE):
        home = root / f"x{scale}"
        home.mkdir()
        built[scale] = build_world(home, f"x{scale}", scale=scale)
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


@pytest.mark.parametrize("scale", [SMALL, LARGE])
def test_unrelated_installations_hold_their_own_archive_and_nodes(
    scale: int, worlds: dict[int, World]
) -> None:
    """The history is not the target's: its own build, image, plan and nodes."""
    world = worlds[scale]
    with world.sessions() as session:
        others = list(
            session.scalars(
                select(RecipeInstallation).where(
                    RecipeInstallation.id != world.target_installation
                )
            )
        )
        assert len(others) > 0
        for installation in others:
            build = session.get(RecipeBuild, installation.recipe_build_id)
            assert build is not None
            assert build.oci_layout_sha256 != world.target_archive
            assert build.id != world.target_build
            assert installation.model_content_sha256 != world.target_model
            plan = parse_stored_installation_plan(installation.plan)
            assert world.target_archive not in json.dumps(installation.plan)
            held = set(
                session.scalars(
                    select(InstallationNode.node_id).where(
                        InstallationNode.installation_id == installation.id
                    )
                )
            )
            assert held == set(plan.compiled_execution_plans)
        on_target_node = [
            installation
            for installation in others
            if world.node
            in set(
                session.scalars(
                    select(InstallationNode.node_id).where(
                        InstallationNode.installation_id == installation.id
                    )
                )
            )
        ]
        assert len(on_target_node) == 3
