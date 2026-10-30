"""A model removal's review and acceptance cost what the target uses, nothing else.

Production regression: ``GET /api/model/<d>/remove-review?fleet=true`` took 33 s
to 73 s with 312 cached models, 30 installations, 6 saved profiles and about a
thousand jobs from the last day, while the client waits 15 s. Whether a model is
used is answered by its own rows (cached sets, installations bound to it, the
profiles that name its recipes, live operations), so the cost must not depend on
the rows of other models.

The world is seeded by inserting rows directly (downloading 300 models takes
minutes). Each operation runs with one unrelated row of each kind, with a long
history, and with a large protected load. History must not change the statements,
the plan parses or the rows loaded; protected work may grow linearly. The 24 hour
live scan is never replayed.
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
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.inventory import ModelStoreObject
from vonk_control import catalog_revision_collection
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache.errors import ModelCacheConflictInvalid
from vonk_control.model_cache_contract import CacheManifest
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    FleetProfile,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
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
    #: Uninstalled installations, and catalog models with a cached set each:
    #: history that nothing protects.
    uninstalled: int = 0
    models: int = 0


# One row of each kind, so every read that exists once such rows exist runs.
ONE_OF_EACH = Load(profiles=1, selectors=1, installations=1, jobs=1)
# History grows with age: it must cost nothing.
HISTORY = Load(
    profiles=1, selectors=1, installations=1, jobs=1_000, uninstalled=29, models=150
)
# Protected work (saved selectors, active installations) may grow linearly.
PROTECTED = Load(profiles=6, selectors=55, installations=29, jobs=1)

MODELS = 300
FILES = 40
CACHED_SETS = 130
SELECTORS = 55
SPARK_OBJECTS = 500
PUBLISHER = "vonk-forge"


def _hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _example(name: str) -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts").joinpath("examples", name).read_text("utf-8")
    )


def _noise_models(
    session: Session, now: datetime, count: int = MODELS, start: int = 0
) -> list[tuple[str, str]]:
    """``MODELS`` catalog models of ``FILES`` files each; returns (revision id, digest)."""
    found: list[tuple[str, str]] = []
    for index in range(start, start + count):
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
    session: Session,
    models: list[tuple[str, str]],
    target: str | None,
    now: datetime,
    *,
    count: int = CACHED_SETS,
    prefix: str = "",
) -> None:
    named = [
        *(
            (f"{prefix}{index}", digest)
            for index, (_id, digest) in enumerate(models[:count])
        ),
        *(() if target is None else (("target", target),)),
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
    """``SELECTORS`` recipes; each selects the first file of its own model."""
    revisions: list[str] = []
    for index in range(SELECTORS):
        model_revision, model_digest = models[index]
        document = _example("recipe-source-build.json")
        identity = document["identity"]
        assert isinstance(identity, dict)
        identity["publisher"] = PUBLISHER
        identity["slug"] = f"noise-recipe-{index:04d}"
        selection = document["models"][0]  # type: ignore[index]
        selection["model"].update(
            slug=f"noise-model-{index:04d}", content_sha256=model_digest
        )
        selection["files"][0]["file_id"] = "w0"
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
        for index in range(load.uninstalled):
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
                    plan_digest=_hex(f"old-plan/{index}"),
                    plan=json.loads(json.dumps(real.plan).replace(real_model, digest)),
                    state="uninstalled",
                    actor="test",
                    created_at=now,
                    updated_at=now,
                )
            )
        extra = _noise_models(session, NOW, load.models, start=MODELS)
        _cached_sets(session, extra, None, NOW, count=load.models, prefix="h")
        session.flush()
        _revision_history(session, load.models)
        _finished_work(session, load.models, now)
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


def _revision_history(session: Session, count: int) -> None:
    """Older candidate revisions of the noise models and of every noise recipe."""
    old = session.scalars(
        select(CatalogDocumentRevision)
        .where(
            CatalogDocumentRevision.slug.like("noise-%"),
            CatalogDocumentRevision.revision_number == 1,
        )
        .order_by(CatalogDocumentRevision.slug)
    ).all()
    columns = [c.key for c in CatalogDocumentRevision.__table__.columns]
    for index, row in enumerate(old[: count + SELECTORS]):
        values = {key: getattr(row, key) for key in columns}
        values.update(
            id=str(uuid.uuid4()),
            revision_number=2,
            state="candidate",
            content_digest=_hex(f"old-revision/{index}"),
        )
        session.add(CatalogDocumentRevision(**values))


def _finished_work(session: Session, count: int, now: datetime) -> None:
    """Finished profile applications and finished model-cache operations."""
    profile = session.scalars(select(FleetProfile)).first()
    assert profile is not None
    for index in range(count):
        session.add(
            FleetProfileApplication(
                request_key=str(uuid.uuid4()),
                profile_id=profile.id,
                profile_digest=_hex(f"application/{index}"),
                plan_digest=_hex(f"application-plan/{index}"),
                state="succeeded",
                plan={},
                actor="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ModelCacheOperation(
                request_key=str(uuid.uuid4()),
                kind="download",
                state="succeeded",
                payload={},
                progress={},
                actor="test",
                created_at=now,
                updated_at=now,
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
        sessions, tmp_path / f"nas-{name}", free_fraction=0, clock=lambda: now
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


# Owner scans (model_set_reference_findings) per operation: the protected work
# is repeated once per scan.
SCANS = {
    "review_model_removal": 1,
    "review_fleet_model_removal": 1,
    "remove_fleet_model_selector": 2,
    "store_retention_request": 0,
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

    Two axes. HISTORY (finished jobs, uninstalled installations, catalog models
    and cached sets nothing protects) must leave statements, plan parses and
    loaded rows exactly equal. PROTECTED work (saved selectors, active
    installations) is resolved or parsed once per owner scan, so it may grow
    linearly and no faster.

    Slow by design: it seeds 300 models of 40 files and 130 cached sets, three
    times per operation.
    """
    alone = _world(tmp_path, "alone", unrelated=ONE_OF_EACH)
    history = _world(tmp_path, "history", unrelated=HISTORY)
    protected = _world(tmp_path, "protected", unrelated=PROTECTED)
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
        costs = {}
        for name, world in (
            ("one-of-each", alone),
            ("history", history),
            ("protected", protected),
        ):
            if operation != "remove_fleet_model_selector":
                # Warm the process the way a running Controller is, then measure.
                _operations(world)[operation]()
            costs[name] = _measure(world, monkeypatch, _operations(world)[operation])
        base, aged, full = costs["one-of-each"], costs["history"], costs["protected"]
        print(  # the budget numbers are the report
            f"{operation}: one-of-each statements={len(base.statements)} "
            f"parses={base.parses}; history statements={len(aged.statements)} "
            f"parses={aged.parses}; protected statements={len(full.statements)} "
            f"parses={full.parses} installation_rows={full.loaded['RecipeInstallation']}"
        )
        assert len(aged.statements) == len(base.statements)
        assert aged.parses == base.parses
        assert dict(aged.loaded) == dict(base.loaded)
        scans = SCANS[operation]
        recipes = (PROTECTED.selectors - ONE_OF_EACH.selectors) * scans
        running = (PROTECTED.installations - ONE_OF_EACH.installations) * scans
        assert len(full.statements) - len(base.statements) <= 6 * recipes
        # One parse per active installation per owner scan, and one more where
        # the fleet effects read the installation's own plan.
        assert full.parses - base.parses <= 3 * running
        assert full.loaded["Job"] == base.loaded["Job"]
        for world_cost in (aged, full):
            for statement in world_cost.statements:
                if any(f"FROM {table}" in statement for table in FILTERED_TABLES):
                    assert re.search(r"\bWHERE\b", statement), statement
    finally:
        alone.cache.close()
        history.cache.close()
        protected.cache.close()


def test_a_damaged_installation_plan_blocks_the_removal(tmp_path: Path) -> None:
    """Catches skipping an unreadable plan because its columns name another model.

    The plan cannot say which sets it uses, so an installed installation with a
    damaged plan keeps the removal from being accepted, whatever its columns name.
    """
    world = _world(tmp_path, "damaged", unrelated=ONE_OF_EACH)
    try:
        with world.sessions.begin() as session:
            real = session.scalars(select(RecipeInstallation)).first()
            other = session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.slug == "noise-recipe-0000",
                    CatalogDocumentRevision.kind == "recipe",
                )
            ).first()
            assert real is not None and other is not None
            damaged_id = str(uuid.uuid4())
            session.add(
                RecipeInstallation(
                    id=damaged_id,
                    recipe_revision_id=other.id,
                    model_content_sha256=_hex("elsewhere"),
                    mapping_id=real.mapping_id,
                    mapping_generation=real.mapping_generation,
                    recipe_build_id=real.recipe_build_id,
                    image_digest=real.image_digest,
                    plan_digest=_hex("damaged-plan"),
                    plan=real.plan,
                    state="installed",
                    actor="test",
                    created_at=world.now,
                    updated_at=world.now,
                )
            )
            session.flush()
            # Past the write guard, as damage reaches a stored row.
            session.execute(
                text("UPDATE recipe_installations SET plan = :plan WHERE id = :id"),
                {"plan": '{"damaged": true}', "id": damaged_id},
            )
        objects = [
            ModelStoreObject(sha256=_hex(f"target/{n}"), bytes=3) for n in range(FILES)
        ]
        for node in world.nodes:
            inventory(world.sessions, node, world.now + timedelta(seconds=1), objects)
        review = world.cache.review_fleet_model_removal(world.target)
        assert review.fleet is not None and review.fleet.observation_issues
        accept = world.cache.remove_fleet_model_selector
        with pytest.raises(ModelCacheConflictInvalid):
            accept(
                world.target,
                actor="operator",
                request_key=str(uuid.uuid4()),
                review_digest=review.review_digest,
            )
    finally:
        world.cache.close()
