"""A model removal's review and acceptance cost what the target uses, nothing else.

Production regression: ``GET /api/model/<d>/remove-review?fleet=true`` took 33 s
to 73 s with 312 cached models, 30 installations, 6 saved profiles and about a
thousand jobs from the last day, while the client waits 15 s. Whether a model is
used is answered by its own rows (cached sets, installations bound to it, the
profiles that name its recipes, live operations), so the cost must not depend on
the rows of other models.

The world is seeded by inserting rows directly (downloading 300 models takes
minutes). Each operation runs once with one unrelated row of each kind and once with
the full unrelated load: the statements it makes must be exactly the same, no
unrelated job or installation row may be loaded, no installation that does not
name the target is parsed, and the 24 hour live scan is never replayed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.inventory import ModelStoreObject
from vonk_control import catalog_revision_collection
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_contract import CacheManifest
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    FleetProfile,
    Job,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeInstallation,
)
from vonk_control.spark_store_retention import store_retention_request
from vonk_control.unused_storage_collection import evidence as collector_evidence
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

from .test_fleet_model_removal_postgres import inventory
from .test_recipe_image_availability import _availability_payload
from .test_recipe_operations import NOW, installed_recipe, setup_services


@dataclass(frozen=True)
class Load:
    """Rows that name none of the target's models."""

    profiles: int
    selectors: int
    installations: int
    jobs: int


# One row of each kind, so every read that exists once such rows exist runs.
ONE_OF_EACH = Load(profiles=1, selectors=1, installations=1, jobs=1)
PRODUCTION = Load(profiles=6, selectors=55, installations=29, jobs=1_000)

MODELS = 300
FILES = 40
CACHED_SETS = 130
SELECTORS = 55
SPARK_OBJECTS = 500
# The client waits 15 seconds; a review has to answer far inside that.
WALL_SECONDS = 3.0
# Lookups that only run once such rows exist (the profile, binding and recipe
# reads), each a single batched statement; no read is added per row.
FIXED_READS = 6
PUBLISHER = "vonk-forge"


def _hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _example(name: str) -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts").joinpath("examples", name).read_text("utf-8")
    )


def _noise_models(session: Session, now: datetime) -> list[tuple[str, str]]:
    """``MODELS`` catalog models of ``FILES`` files each; returns (revision id, digest)."""
    found: list[tuple[str, str]] = []
    for index in range(MODELS):
        document = _example("model-definition.json")
        identity = document["identity"]
        assert isinstance(identity, dict)
        identity["publisher"] = PUBLISHER
        identity["slug"] = f"noise-model-{index:04d}"
        model = identity["model"]
        assert isinstance(model, dict)
        model["publisher"] = PUBLISHER
        model["slug"] = f"noise-model-{index:04d}"
        document["files"] = [
            {
                "id": f"w{n}",
                "path": f"model-{n:05d}-of-{FILES:05d}.safetensors",
                "sha256": _hex(f"{index}/{n}"),
                "size_bytes": 3,
                "roles": ["weights"],
            }
            for n in range(FILES)
        ]
        document["dependencies"] = []
        clean = ModelDefinition.model_validate(document).model_dump(mode="json")
        digest = document_sha256(clean)
        document_id, revision_id = str(uuid.uuid4()), str(uuid.uuid4())
        session.add(
            CatalogDocument(
                id=document_id,
                kind="model",
                publisher=PUBLISHER,
                slug=f"noise-model-{index:04d}",
                title="noise",
                created_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            CatalogDocumentRevision(
                id=revision_id,
                document_id=document_id,
                kind="model",
                publisher=PUBLISHER,
                slug=f"noise-model-{index:04d}",
                revision_number=1,
                schema_version=2,
                state="active",
                document=clean,
                content_digest=digest,
                projected={},
                created_by="test",
                created_at=now,
            )
        )
        session.flush()
        session.add(
            CatalogDocumentHead(
                kind="model",
                publisher=PUBLISHER,
                slug=f"noise-model-{index:04d}",
                active_revision_id=revision_id,
            )
        )
        found.append((revision_id, digest))
    return found


def _cached_sets(
    session: Session, models: list[tuple[str, str]], target: str, now: datetime
) -> None:
    named = [
        *(
            (str(index), digest)
            for index, (_id, digest) in enumerate(models[:CACHED_SETS])
        ),
        ("target", target),
    ]
    for key, digest in named:
        set_digest = _hex(f"set/{key}")
        artifacts = [
            {
                "key": f"w{n}",
                "id": f"w{n}",
                "path": f"model-{n:05d}.safetensors",
                "kind": "file",
                "repository": None,
                "source": "file:///source",
                "revision": None,
                "sha256": _hex(f"{key}/{n}"),
                "download_bytes": 3,
                "roles": ["model"],
                "model_content_sha256": digest,
            }
            for n in range(FILES)
        ]
        manifest = CacheManifest.model_validate(
            {
                "schema_version": 2,
                "source_policy": "nas-first",
                "model_content_sha256": digest,
                "recipe_revision_sha256": None,
                "model_definition_ref": None,
                "model_content_digests": [digest],
                "artifacts": artifacts,
            }
        ).model_dump(mode="json")
        session.add(
            ModelCacheSet(
                artifact_set_sha256=set_digest,
                schema_version=2,
                model_content_sha256=digest,
                recipe_revision_sha256=None,
                manifest=manifest,
                expected_bytes=3 * FILES,
                verified_bytes=3 * FILES,
                state="cached",
                protected=False,
                protected_reasons=[],
                created_at=now,
                updated_at=now,
                last_accessed_at=now,
            )
        )
        session.flush()
        session.add_all(
            ModelCacheSetArtifact(
                artifact_set_sha256=set_digest,
                artifact_key=item["key"],
                artifact_sha256=item["sha256"],
                path=item["path"],
            )
            for item in artifacts
        )


def _noise_recipes(
    session: Session, models: list[tuple[str, str]], now: datetime
) -> list[str]:
    """One recipe per noise model, bound to it as the catalog binds a recipe."""
    revisions: list[str] = []
    for index, (model_revision, model_digest) in enumerate(models[:SELECTORS]):
        document = _example("recipe-source-build.json")
        identity = document["identity"]
        assert isinstance(identity, dict)
        identity["publisher"] = PUBLISHER
        identity["slug"] = f"noise-recipe-{index:04d}"
        selection = document["models"][0]  # type: ignore[index]
        selection["model"].update(
            slug=f"noise-model-{index:04d}", content_sha256=model_digest
        )
        clean = RecipeDefinition.model_validate(document).model_dump(mode="json")
        document_id, revision_id = str(uuid.uuid4()), str(uuid.uuid4())
        session.add(
            CatalogDocument(
                id=document_id,
                kind="recipe",
                publisher=PUBLISHER,
                slug=f"noise-recipe-{index:04d}",
                title="noise",
                created_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            CatalogDocumentRevision(
                id=revision_id,
                document_id=document_id,
                kind="recipe",
                publisher=PUBLISHER,
                slug=f"noise-recipe-{index:04d}",
                revision_number=1,
                schema_version=2,
                state="active",
                document=clean,
                content_digest=document_sha256(clean),
                projected={},
                created_by="test",
                created_at=now,
            )
        )
        session.flush()
        session.add_all(
            [
                CatalogDocumentHead(
                    kind="recipe",
                    publisher=PUBLISHER,
                    slug=f"noise-recipe-{index:04d}",
                    active_revision_id=revision_id,
                ),
                CatalogRecipeModelReference(
                    recipe_revision_id=revision_id,
                    recipe_kind="recipe",
                    selection_id=str(selection["id"]),
                    model_revision_id=model_revision,
                    model_kind="model",
                    model_publisher=PUBLISHER,
                    model_slug=f"noise-model-{index:04d}",
                    model_content_digest=model_digest,
                ),
            ]
        )
        revisions.append(revision_id)
    return revisions


def _unrelated_load(
    sessions: sessionmaker[Session],
    target_installation: str,
    recipes: list[str],
    models: list[tuple[str, str]],
    spark: str,
    now: datetime,
    load: Load,
) -> None:
    """Profiles, installations and a day of finished jobs that do not name the target."""
    with sessions.begin() as session:
        for number in range(load.profiles):
            chosen = range(number, load.selectors, load.profiles)
            session.add(
                FleetProfile(
                    number=number + 1,
                    name=f"Noise {number}",
                    installation_policy="keep-cached",
                    assignments=[
                        {
                            "recipe_selector": f"{PUBLISHER}/noise-recipe-{index:04d}",
                            "spark_ids": [spark],
                            "desired_state": "running",
                            "option_choices": {},
                        }
                        for index in chosen
                    ],
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        real = session.get(RecipeInstallation, target_installation)
        assert real is not None
        real_model = real.model_content_sha256
        assert real_model is not None
        for index in range(load.installations):
            digest = models[index][1]
            session.add(
                RecipeInstallation(
                    id=str(uuid.uuid4()),
                    recipe_revision_id=recipes[index],
                    model_content_sha256=digest,
                    mapping_id=real.mapping_id,
                    mapping_generation=real.mapping_generation,
                    recipe_build_id=real.recipe_build_id,
                    image_digest=real.image_digest,
                    plan_digest=_hex(f"plan/{index}"),
                    plan=json.loads(json.dumps(real.plan).replace(real_model, digest)),
                    state=real.state,
                    actor="test",
                    created_at=now,
                    updated_at=now,
                )
            )
        payload = _availability_payload(recipes[0]).model_dump(mode="json")
        recent = now - timedelta(hours=1)
        for index in range(load.jobs):
            session.add(
                Job(
                    request_id=str(uuid.uuid4()),
                    kind="recipe.image.availability.v2",
                    state="succeeded",
                    actor="test",
                    authority_revision="r",
                    targets=[],
                    payload_digest=_hex(f"job/{index}"),
                    payload=copy.deepcopy(payload),
                    current_attempt=1,
                    created_at=recent,
                    updated_at=recent,
                )
            )


@dataclass
class World:
    sessions: sessionmaker[Session]
    cache: ModelCacheService
    lifecycle: object
    engine: object
    target: str
    nodes: tuple[str, ...]
    now: datetime


def _world(tmp_path: Path, name: str, *, unrelated: Load | None) -> World:
    engine = create_engine(
        f"sqlite:///{tmp_path / f'{name}.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=engine, nodes=2, mapping_node_count=1
    )
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes[:1], request_id=str(uuid.uuid4())
    )
    with sessions() as session:
        row = session.get(RecipeInstallation, installed.owner_id)
        assert row is not None and row.model_content_sha256 is not None
        target = row.model_content_sha256
    with sessions.begin() as session:
        models = _noise_models(session, NOW)
        _cached_sets(session, models, target, NOW)
        recipes = _noise_recipes(session, models, NOW)
    now = NOW + timedelta(minutes=1)
    if unrelated is not None:
        _unrelated_load(
            sessions, installed.owner_id, recipes, models, nodes[0], now, unrelated
        )
    objects = [
        ModelStoreObject(sha256=_hex(f"spark/{n}"), bytes=3)
        for n in range(SPARK_OBJECTS)
    ]
    for node in nodes:
        inventory(sessions, node, now, objects)
    cache = ModelCacheService(
        sessions, tmp_path / f"nas-{name}", reserve_bytes=0, clock=lambda: now
    )
    cache._fleet_lifecycle = lifecycle
    return World(sessions, cache, lifecycle, engine, target, nodes, now)


@dataclass
class Cost:
    statements: list[str]
    parses: int
    loaded: Counter[str]


# Tables a request must only touch through a filter: reading them whole is the
# cost this guards.
FILTERED_TABLES = ("jobs", "recipe_installations")


def _measure(
    world: World, monkeypatch: pytest.MonkeyPatch, call: Callable[[], object]
) -> Cost:
    """Run ``call``; record its statements, installation plan parses and row loads."""
    statements: list[str] = []
    loaded: Counter[str] = Counter()
    parses = [0]

    def record(_conn, _cursor, statement, *_args: object) -> None:
        statements.append(statement)

    def count_load(target: object, _context: object) -> None:
        loaded[type(target).__name__] += 1

    def forbid(*_args: object, **_kwargs: object) -> frozenset[str]:
        raise AssertionError("a removal replayed the 24 hour live scan")

    monkeypatch.setattr(catalog_revision_collection, "live_tokens", forbid)
    monkeypatch.setattr(collector_evidence, "live_tokens", forbid)
    for name, module in list(sys.modules.items()):
        if name.startswith("vonk_control") and hasattr(
            module, "parse_stored_installation_plan"
        ):
            _count_parses(monkeypatch, module, parses)
    event.listen(world.engine, "before_cursor_execute", record)
    event.listen(Base, "load", count_load, propagate=True)
    try:
        call()
    finally:
        event.remove(world.engine, "before_cursor_execute", record)
        event.remove(Base, "load", count_load)
    return Cost(statements, parses[0], loaded)


def _count_parses(
    monkeypatch: pytest.MonkeyPatch, target: ModuleType, counter: list[int]
) -> None:
    original = target.parse_stored_installation_plan

    def counted(*args: object, **kwargs: object) -> object:
        counter[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(target, "parse_stored_installation_plan", counted)


def _operations(world: World) -> dict[str, Callable[[], object]]:
    cache, target = world.cache, world.target

    def accept() -> object:
        review = cache.review_fleet_model_removal(target)
        return cache.remove_fleet_model_selector(
            target,
            actor="operator",
            request_key=str(uuid.uuid4()),
            review_digest=review.review_digest,
        )

    def retention() -> object:
        with world.sessions() as session:
            return store_retention_request(session, world.nodes[0], world.now)

    return {
        "review_model_removal": lambda: cache.review_model_removal(target),
        "review_fleet_model_removal": lambda: cache.review_fleet_model_removal(target),
        "remove_fleet_model_selector": accept,
        "store_retention_request": retention,
    }


@pytest.mark.slow(30)
@pytest.mark.parametrize(
    "operation",
    [
        "review_model_removal",
        "review_fleet_model_removal",
        "remove_fleet_model_selector",
        "store_retention_request",
    ],
)
def test_removal_cost_does_not_grow_with_unrelated_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Catches parsing every installation and walking every profile and job per request.

    Slow by design: it seeds 300 models of 40 files, 130 cached sets and the
    production load, twice per operation.
    """
    alone = _world(tmp_path, "alone", unrelated=ONE_OF_EACH)
    loaded = _world(tmp_path, "loaded", unrelated=PRODUCTION)
    # The uninstall plan the fleet review asks of a named installation lists every
    # other installation and parses each recipe to find dependents of the model
    # (recipe_operations/action_plans.py::_model_dependents_on_nodes). That is the
    # same class of cost in another owner and has its own follow-up; it is left
    # out here so this guard covers the removal's own reads.
    monkeypatch.setattr(
        type(alone.lifecycle),
        "_model_dependents_on_nodes",
        lambda _self, _session, _model, node_ids, **_kw: {n: () for n in node_ids},
    )
    try:
        if operation != "remove_fleet_model_selector":
            # Warm the process the way a running Controller is, then measure.
            _operations(alone)[operation]()
            _operations(loaded)[operation]()
        base = _measure(alone, monkeypatch, _operations(alone)[operation])
        full = _measure(loaded, monkeypatch, _operations(loaded)[operation])
        print(  # the budget numbers are the report
            f"{operation}: one-of-each statements={len(base.statements)} "
            f"parses={base.parses}; production statements={len(full.statements)} "
            f"parses={full.parses} job_rows={full.loaded['Job']} "
            f"installation_rows={full.loaded['RecipeInstallation']}"
        )
        assert len(full.statements) == len(base.statements)
        assert full.parses == base.parses
        # One statement returning every row would keep the count equal.
        for table in ("Job", "RecipeInstallation"):
            assert full.loaded[table] == base.loaded[table]
        for statement in full.statements:
            if any(f"FROM {table}" in statement for table in FILTERED_TABLES):
                assert re.search(r"\bWHERE\b", statement), statement
    finally:
        alone.cache.close()
        loaded.cache.close()
