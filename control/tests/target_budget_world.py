"""A seeded Controller database for the target-budget harness.

``build_world`` seeds one target (an installed recipe on one Spark, the model it
uses and that model's cached set) plus ``scale`` units of unrelated history:
finished jobs carrying recipe-sized payloads, saved-profile selectors, recipes
and models in the catalog, cached sets and installations of other recipes.
Rows are inserted directly; downloading a catalog takes minutes.

Two worlds differ only in ``scale``. Whatever a request asks about the target
must therefore cost the same in both.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.inventory import ModelStoreObject
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_contract import CacheManifest
from vonk_control.models import (
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    ClusterMapping,
    ClusterMappingNode,
    FleetProfile,
    InstallationNode,
    Job,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeBuild,
    RecipeInstallation,
)
from vonk_control.recipe_operations import RecipeOperationService
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

from .test_fleet_model_removal_postgres import inventory
from .test_recipe_image_availability import _availability_payload
from .test_recipe_operations import NOW, installed_recipe, setup_services

PUBLISHER = "vonk-forge"
FILES = 4
# One unit of unrelated history. Production holds about 1 000 jobs, 55 selectors,
# 300 models and 30 installations; the harness compares two sizes of the same
# shape, so the unit only has to be large enough that a per-row read shows.
JOBS_PER_UNIT = 100
SELECTORS_PER_UNIT = 5
PROFILES_PER_UNIT = 1
# Installations on the target's own Spark, whatever the unrelated history.
NODE_LOCAL = 3
MODELS_PER_UNIT = 30
INSTALLATIONS_PER_UNIT = 3
CACHED_SETS_PER_UNIT = 13
# The Spark's own store is node-local input and does not grow with history.
SPARK_OBJECTS = 20


def _hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _example(name: str) -> dict[str, object]:
    return json.loads(
        files("vonk_forge_contracts").joinpath("examples", name).read_text("utf-8")
    )


@dataclass
class World:
    """The services and the identity of the target a check asks about."""

    scale: int
    sessions: sessionmaker[Session]
    engine: Engine
    cache: ModelCacheService
    operations: RecipeOperationService
    target_model: str
    target_installation: str
    target_archive: str
    node: str
    other_node: str
    now: datetime
    # Rows the database driver has handed to the Controller, over every
    # statement and every table, so a column select that the code filters in
    # Python counts as much as an entity load.
    fetched: list[int]
    # SQLite virtual-machine steps: work done inside SQL, such as a text scan
    # of JSON, that no row count shows.
    steps: list[int]
    root: Path
    target_recipe_slug: str
    target_build: str

    def close(self) -> None:
        self.cache.close()
        self.engine.dispose()


def _catalog_document(
    session: Session, kind: str, slug: str, document: dict[str, object], now: datetime
) -> tuple[str, str]:
    """Insert one active catalog document, its head and revision; (revision id, digest)."""
    digest = document_sha256(document)
    document_id, revision_id = str(uuid.uuid4()), str(uuid.uuid4())
    session.add(
        CatalogDocument(
            id=document_id,
            kind=kind,
            publisher=PUBLISHER,
            slug=slug,
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
            kind=kind,
            publisher=PUBLISHER,
            slug=slug,
            revision_number=1,
            schema_version=2,
            state="active",
            document=document,
            content_digest=digest,
            projected={},
            created_by="test",
            created_at=now,
        )
    )
    session.flush()
    session.add(
        CatalogDocumentHead(
            kind=kind,
            publisher=PUBLISHER,
            slug=slug,
            active_revision_id=revision_id,
        )
    )
    return revision_id, digest


def _noise_models(session: Session, count: int, now: datetime) -> list[tuple[str, str]]:
    models: list[tuple[str, str]] = []
    for index in range(count):
        slug = f"noise-model-{index:05d}"
        document = _example("model-definition.json")
        identity = document["identity"]
        assert isinstance(identity, dict)
        identity.update(publisher=PUBLISHER, slug=slug)
        model = identity["model"]
        assert isinstance(model, dict)
        model.update(publisher=PUBLISHER, slug=slug)
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
        models.append(_catalog_document(session, "model", slug, clean, now))
    return models


def _cached_sets(
    session: Session, digests: list[str], target: str, now: datetime
) -> None:
    for index, digest in enumerate([*digests, target]):
        set_digest = _hex(f"set/{index}")
        artifacts = [
            {
                "key": f"w{n}",
                "id": f"w{n}",
                "path": f"model-{n:05d}.safetensors",
                "kind": "file",
                "repository": None,
                "source": "file:///source",
                "revision": None,
                "sha256": _hex(f"{index}/{n}"),
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
    session: Session, models: list[tuple[str, str]], count: int, now: datetime
) -> list[str]:
    """One recipe per noise model, bound to it the way the catalog binds a recipe."""
    revisions: list[str] = []
    for index, (model_revision, model_digest) in enumerate(models[:count]):
        slug = f"noise-recipe-{index:05d}"
        document = _example("recipe-source-build.json")
        identity = document["identity"]
        assert isinstance(identity, dict)
        identity.update(publisher=PUBLISHER, slug=slug)
        selection = document["models"][0]  # type: ignore[index]
        selection["model"].update(
            slug=f"noise-model-{index:05d}", content_sha256=model_digest
        )
        clean = RecipeDefinition.model_validate(document).model_dump(mode="json")
        revision_id, _ = _catalog_document(session, "recipe", slug, clean, now)
        session.add(
            CatalogRecipeModelReference(
                recipe_revision_id=revision_id,
                recipe_kind="recipe",
                selection_id=str(selection["id"]),
                model_revision_id=model_revision,
                model_kind="model",
                model_publisher=PUBLISHER,
                model_slug=f"noise-model-{index:05d}",
                model_content_digest=model_digest,
            )
        )
        revisions.append(revision_id)
    return revisions


def _own(value: object, swaps: dict[str, str]) -> object:
    """``value`` with the target's identities replaced by another item's."""
    text = json.dumps(value)
    for old, new in swaps.items():
        if old:
            text = text.replace(old, new)
    return json.loads(text)


def _unrelated_history(
    sessions: sessionmaker[Session],
    scale: int,
    target_installation: str,
    recipes: list[str],
    models: list[tuple[str, str]],
    spark: str,
    other_node: str,
    now: datetime,
) -> None:
    """Saved profiles, installations and finished jobs that do not name the target."""
    with sessions.begin() as session:
        for number in range(scale * PROFILES_PER_UNIT):
            chosen = range(
                number * SELECTORS_PER_UNIT, (number + 1) * SELECTORS_PER_UNIT
            )
            session.add(
                FleetProfile(
                    number=number + 1,
                    name=f"Noise {number}",
                    installation_policy="keep-cached",
                    assignments=[
                        {
                            "recipe_selector": f"{PUBLISHER}/noise-recipe-{index:05d}",
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
        real_build = session.get(RecipeBuild, real.recipe_build_id) if real else None
        assert real is not None and real.model_content_sha256 is not None
        assert real_build is not None
        build_columns = {
            column.name: getattr(real_build, column.name)
            for column in RecipeBuild.__table__.columns
        }
        real_mapping = session.get(ClusterMapping, real.mapping_id)
        assert real_mapping is not None
        mapping_columns = {
            column.name: getattr(real_mapping, column.name)
            for column in ClusterMapping.__table__.columns
        }
        revision_digests = {
            revision.id: revision.content_digest
            for revision in session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id.in_(recipes)
                )
            )
        }
        target_revision = session.get(CatalogDocumentRevision, real.recipe_revision_id)
        assert target_revision is not None and target_revision.content_digest
        for index in range(scale * INSTALLATIONS_PER_UNIT + NODE_LOCAL):
            # The first NODE_LOCAL installations sit on the target's own Spark:
            # what a node holds is the input of a question about that node, so
            # it does not grow with unrelated history. The rest sit on the other.
            local = index < NODE_LOCAL
            node = spark if local else other_node
            digest = models[index][1]
            installation_id, build_id = str(uuid.uuid4()), str(uuid.uuid4())
            mapping_id = str(uuid.uuid4())
            archive = _hex(f"archive/{index}")
            image_digest = f"sha256:{_hex(f'image/{index}')}"
            swaps = {
                spark: node,
                real.model_content_sha256: digest,
                real.recipe_build_id: build_id,
                real.recipe_revision_id: recipes[index],
                target_revision.content_digest: revision_digests[recipes[index]] or "",
                real_build.oci_layout_sha256 or "": archive,
                real.image_digest or "": image_digest,
                real_build.build_input_sha256: _hex(f"input/{index}"),
                real.plan_digest: _hex(f"plan/{index}"),
                real_mapping.id: mapping_id,
                real_mapping.placement_digest: _hex(f"placement/{index}"),
            }

            # Its own placement: a mapping of one rank on its own Spark.
            session.add(
                ClusterMapping(
                    **{
                        **mapping_columns,
                        "id": mapping_id,
                        "recipe_revision_id": recipes[index],
                        "placement_digest": _hex(f"placement/{index}"),
                        "endpoint_owner_node_id": node,
                        "node_count": 1,
                        "parameters": _own(real_mapping.parameters, swaps),
                    }
                )
            )
            session.flush()
            session.add(
                ClusterMappingNode(
                    mapping_id=mapping_id,
                    node_id=node,
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=now,
                )
            )
            session.add(
                RecipeBuild(
                    **{
                        **build_columns,
                        "id": build_id,
                        "recipe_revision_id": recipes[index],
                        "build_input_sha256": _hex(f"input/{index}"),
                        "oci_layout_sha256": archive,
                        "image_digest": image_digest,
                        "plan": _own(real_build.plan, swaps),
                    }
                )
            )
            session.flush()
            plan = _own(real.plan, swaps)
            session.add(
                RecipeInstallation(
                    id=installation_id,
                    recipe_revision_id=recipes[index],
                    model_content_sha256=digest,
                    mapping_id=mapping_id,
                    mapping_generation=real_mapping.generation,
                    recipe_build_id=build_id,
                    image_digest=image_digest,
                    plan_digest=_hex(f"plan/{index}"),
                    plan=plan,
                    state=real.state,
                    actor="test",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            session.add(
                InstallationNode(
                    installation_id=installation_id,
                    node_id=node,
                    rank=0,
                    role="entrypoint",
                    state="installed",
                    required_bytes=120,
                    installed_bytes=120,
                    updated_at=now,
                )
            )
        payload = _availability_payload(recipes[0]).model_dump(mode="json")
        recent = now - timedelta(hours=1)
        for index in range(scale * JOBS_PER_UNIT):
            body = copy.deepcopy(payload)
            session.add(
                Job(
                    request_id=str(uuid.uuid4()),
                    kind="recipe.image.availability.v2",
                    state="succeeded",
                    actor="test",
                    authority_revision="r",
                    targets=[],
                    payload_digest=_hex(f"job/{index}"),
                    payload=body,
                    current_attempt=1,
                    created_at=recent,
                    updated_at=recent,
                )
            )


def build_world(tmp_path: Path, name: str, *, scale: int) -> World:
    """The target plus ``scale`` units of unrelated history."""
    engine = create_engine(
        f"sqlite:///{tmp_path / f'{name}.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    fetched = [0]
    steps = [0]

    def count_step() -> int:
        steps[0] += 1
        return 0

    def count_fetched(_cursor: object, row: tuple[object, ...]) -> tuple[object, ...]:
        fetched[0] += 1
        return row

    @event.listens_for(engine, "connect")
    def _count_rows(dbapi_connection: Any, _record: object) -> None:
        dbapi_connection.row_factory = count_fetched
        dbapi_connection.set_progress_handler(count_step, 1)

    sessions, operations, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=engine, nodes=2, mapping_node_count=1
    )
    installed = installed_recipe(
        operations, mapping_id, build_id, nodes[:1], request_id=str(uuid.uuid4())
    )
    with sessions() as session:
        row = session.get(RecipeInstallation, installed.owner_id)
        build = session.get(RecipeBuild, build_id)
        assert row is not None and row.model_content_sha256 is not None
        assert build is not None and build.oci_layout_sha256 is not None
        target, archive = row.model_content_sha256, build.oci_layout_sha256
        revision = session.get(CatalogDocumentRevision, row.recipe_revision_id)
        assert revision is not None
        slug = revision.slug
        # The target recipe is a published one: a document with an active head,
        # which is what a selector resolves.
        if session.get(CatalogDocument, revision.document_id) is None:
            session.add(
                CatalogDocument(
                    id=revision.document_id,
                    kind=revision.kind,
                    publisher=revision.publisher,
                    slug=revision.slug,
                    title="target",
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.flush()
        if (
            session.scalar(
                select(CatalogDocumentHead).where(
                    CatalogDocumentHead.kind == revision.kind,
                    CatalogDocumentHead.publisher == revision.publisher,
                    CatalogDocumentHead.slug == revision.slug,
                )
            )
            is None
        ):
            session.add(
                CatalogDocumentHead(
                    kind=revision.kind,
                    publisher=revision.publisher,
                    slug=revision.slug,
                    active_revision_id=revision.id,
                )
            )
        session.commit()
    count = scale * MODELS_PER_UNIT
    with sessions.begin() as session:
        models = _noise_models(session, count, NOW)
        _cached_sets(
            session,
            [digest for _, digest in models[: scale * CACHED_SETS_PER_UNIT]],
            target,
            NOW,
        )
        recipes = _noise_recipes(
            session, models, scale * SELECTORS_PER_UNIT + NODE_LOCAL, NOW
        )
    now = NOW + timedelta(minutes=1)
    if scale:
        _unrelated_history(
            sessions,
            scale,
            installed.owner_id,
            recipes,
            models,
            nodes[0],
            nodes[1],
            now,
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
    cache._fleet_lifecycle = operations
    return World(
        scale,
        sessions,
        engine,
        cache,
        operations,
        target,
        installed.owner_id,
        archive,
        nodes[0],
        nodes[1],
        now,
        fetched,
        steps,
        tmp_path,
        slug,
        build_id,
    )
