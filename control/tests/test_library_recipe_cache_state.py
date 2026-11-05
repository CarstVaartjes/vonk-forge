"""A recipe is cached only when everything it needs is cached.

The Library shows a recipe's controller state as the lowest of its own digest's
state and the state of every model it binds. A succeeded build, a draft model
that is cached, or an installation never outranks a main model that is still
downloading. The guard ties that state to what admission and load planning
observe from the same stored facts.
"""

from __future__ import annotations

import copy
import itertools
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.library_contract import LibraryRecipeProjection
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ArtifactSetManifest, ArtifactSpec
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
)
from vonk_control.run_switch_operations import DatabaseRunSwitchArtifactInspector
from vonk_forge_contracts import RecipeDefinition, document_sha256
from vonk_forge_contracts.model import ModelReference

from .test_library_local_state_history import _cache_set, _operation
from .test_model_cache import (  # noqa: F401  (cache is a pytest fixture)
    _canonical_model,
    _canonical_recipe,
    _write_object_receipt,
    cache,
)

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
LIBRARY_SRC = Path(__file__).parents[1] / "src/vonk_control/library_projection"


class _World:
    """One recipe over ``count`` models, stored the way the catalog stores them."""

    def __init__(self, sessions: sessionmaker[Session], *, count: int) -> None:
        self.sessions = sessions
        models = [
            _canonical_model(
                publisher="vonk-forge",
                slug="resolver-model" if index == 0 else f"draft-model-{index}",
                file_id="weights",
                file_digest=str(index + 1) * 64,
            )
            for index in range(count)
        ]
        self.model_digests = [
            document_sha256(model.model_dump(mode="json")) for model in models
        ]
        document = _canonical_recipe(self.model_digests[0])
        document["identity"] = {
            "publisher": "vonk-forge",
            "slug": "resolver-recipe",
        }
        bound = document["models"]
        assert isinstance(bound, list)
        for index in range(1, count):
            extra = copy.deepcopy(bound[0])
            extra["id"] = f"draft-{index}"
            extra["files"][0]["id"] = f"draft-weights-{index}"
            extra["files"][0]["mount"]["target"] = f"/draft-{index}"
            extra["model"]["slug"] = models[index].identity.slug
            extra["model"]["content_sha256"] = self.model_digests[index]
            bound.append(extra)
        recipe = RecipeDefinition.model_validate(document)
        self.recipe_digest = document_sha256(recipe.model_dump(mode="json"))
        self.recipe_revision_id = str(uuid.uuid4())
        rows: list[CatalogDocument | CatalogDocumentRevision | CatalogDocumentHead] = []
        for kind, definition, revision_id, digest in [
            *[
                ("model", model, str(uuid.uuid4()), digest)
                for model, digest in zip(models, self.model_digests, strict=True)
            ],
            ("recipe", recipe, self.recipe_revision_id, self.recipe_digest),
        ]:
            document_id = str(uuid.uuid4())
            publisher, slug = definition.identity.publisher, definition.identity.slug
            rows += [
                CatalogDocument(
                    id=document_id,
                    kind=kind,
                    publisher=publisher,
                    slug=slug,
                    title=slug,
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CatalogDocumentRevision(
                    id=revision_id,
                    document_id=document_id,
                    kind=kind,
                    publisher=publisher,
                    slug=slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=definition.model_dump(mode="json"),
                    content_digest=digest,
                    execution_key="a" * 64 if kind == "recipe" else None,
                    projected={},
                    created_by="test",
                    created_at=NOW,
                ),
                CatalogDocumentHead(
                    kind=kind,
                    publisher=publisher,
                    slug=slug,
                    active_revision_id=revision_id,
                ),
            ]
        with sessions.begin() as session:
            for row in rows:
                session.add(row)
                session.flush()
            session.add(AgentNode(node_id="builder", state="active"))

    def set_id(self, index: int) -> str:
        return f"{index + 1:x}" * 64

    def model_set(self, index: int, state: str, *, size: int = 3) -> ModelCacheSet:
        digest = self.model_digests[index]
        artifact = ArtifactSpec(
            key=f"artifact-{index}-weights",
            artifact_id="weights",
            path="weights.safetensors",
            kind="huggingface.file",
            repository="https://huggingface.co/vonk-forge/resolver-model",
            source="https://huggingface.co/vonk-forge/resolver-model/resolve/"
            + "0" * 40
            + "/weights.safetensors",
            revision="0" * 40,
            sha256=str(index + 1) * 64,
            expected_bytes=size,
            roles=("weights",),
            model_content_sha256=digest,
        )
        manifest = ArtifactSetManifest(
            model_content_sha256=digest,
            recipe_revision_sha256=None,
            model_content_digests=(digest,),
            artifacts=(artifact,),
            model_definition_ref=ModelReference(
                publisher="vonk-forge",
                slug="resolver-model" if index == 0 else f"draft-model-{index}",
                content_sha256=digest,
            ),
        )
        row = _cache_set(manifest.digest, digest, None, state, manifest.document())
        row.expected_bytes = size
        row.verified_bytes = size if state == "cached" else 0
        return row

    def build(self, state: str = "succeeded") -> RecipeBuild:
        return RecipeBuild(
            id=str(uuid.uuid4()),
            recipe_revision_id=self.recipe_revision_id,
            builder_node_id="builder",
            source_bundle_sha256="2" * 64,
            build_input_sha256="3" * 64,
            state=state,
            policy_report={},
            plan={},
            image_digest="sha256:" + "6" * 64,
            oci_layout_sha256="8" * 64,
            image_bytes=17,
            error=None,
            created_at=NOW,
            updated_at=NOW,
        )


def _projection(sessions: sessionmaker[Session], *, archive=None) -> LibraryProjection:
    return LibraryProjection(
        sessions,
        cursors=TokenCodec(b"q" * 32).cursor_codec(),
        clock=lambda: NOW,
        runtime_archive_available=archive,
    )


def _recipe(projection: LibraryProjection) -> LibraryRecipeProjection:
    (recipe,) = projection.recipe_library(limit=10).recipes
    return recipe


def test_a_recipe_prepares_while_its_main_model_downloads_then_is_cached(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recipe-state.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    world = _World(sessions, count=2)
    main, draft = world.model_set(0, "downloading"), world.model_set(1, "cached")
    download = _operation(
        main.artifact_set_sha256,
        kind="download",
        state="running",
        at=NOW,
        completed=40,
    )
    with sessions.begin() as session:
        session.add_all([main, draft])
        session.flush()
        session.add_all([download, world.build()])

    recipe = _recipe(_projection(sessions))
    assert recipe.local.controller == "preparing"
    assert recipe.local.preparation is not None
    assert recipe.local.preparation.operation_id == download.id
    assert recipe.local.preparation.state == "running"
    assert recipe.local.preparation.completed_bytes == 40
    # The detail view reads the same state from the same bound models.
    detail = _projection(sessions).recipe_detail("vonk-forge/resolver-recipe")
    assert detail.local.controller == "preparing"
    assert detail.local.preparation == recipe.local.preparation

    with sessions.begin() as session:
        session.execute(
            update(ModelCacheSet)
            .where(ModelCacheSet.artifact_set_sha256 == main.artifact_set_sha256)
            .values(state="cached")
        )
        session.execute(
            update(ModelCacheOperation)
            .where(ModelCacheOperation.id == download.id)
            .values(state="succeeded")
        )
    assert _recipe(_projection(sessions)).local.controller == "cached"


def test_a_bound_model_without_cache_state_keeps_the_recipe_from_reading_cached(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'recipe-failed.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    world = _World(sessions, count=2)
    with sessions.begin() as session:
        session.add_all([world.model_set(0, "cached"), world.build()])

    # The draft model has no cache state at all.
    assert _recipe(_projection(sessions)).local.controller == "not_cached"


_MODEL_STATES = ("cached", "downloading", "absent")
_IMAGE_STATES = ("present", "missing", "unbuilt")


@pytest.mark.parametrize(
    ("model_state", "image_state"),
    list(itertools.product(_MODEL_STATES, _IMAGE_STATES)),
)
def test_library_cached_means_admission_and_load_planning_find_nothing_missing(
    cache,  # noqa: F811
    model_state: str,
    image_state: str,
):
    """Shared fixture states: cached in the Library implies nothing left to fetch."""

    service, sessions = cache
    world = _World(sessions, count=1)
    model_object = service.root / "objects" / "11" / ("1" * 64)
    image_object = service.root.parent / "runtime-images" / ("8" * 64)
    rows: list[object] = []
    if model_state != "absent":
        rows.append(
            world.model_set(0, "cached" if model_state == "cached" else "downloading")
        )
    if model_state == "cached":
        model_object.parent.mkdir(parents=True)
        model_object.write_bytes(b"one")
        _write_object_receipt(service, "1" * 64, 3)
    if image_state != "unbuilt":
        rows.append(world.build())
    if image_state == "present":
        image_object.write_bytes(b"x" * 17)
    with sessions.begin() as session:
        session.add_all(rows)
        session.flush()
        if model_state == "downloading":
            session.add(
                _operation(
                    world.model_set(0, "downloading").artifact_set_sha256,
                    kind="download",
                    state="running",
                    at=NOW,
                    completed=1,
                )
            )

    library = _recipe(_projection(sessions, archive=service._runtime_archive_available))
    resolution = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe", model_variant="fp16"
    )
    with sessions() as session:
        inspection = DatabaseRunSwitchArtifactInspector(service).inspect(
            session,
            model_content_sha256=world.model_digests[0],
            recipe_revision_id=world.recipe_revision_id,
            node_ids=(),
            retention="retain",
            now=NOW,
        )

    if library.local.controller == "cached":
        assert resolution.blockers == []
        assert inspection.missing_nas_bytes == 0
    if model_state == "cached" and image_state == "present":
        assert library.local.controller == "cached"
    if model_state != "cached":
        assert library.local.controller != "cached"


def test_a_recipes_local_state_is_built_only_by_the_composing_helper():
    """Only ``_recipe_local`` reads a recipe's own state through ``self._local``."""

    offenders = []
    for path in sorted(LIBRARY_SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"self\._local\(", text):
            call = text[match.start() : match.start() + 240]
            if "ReceiptOwner.RECIPE" not in call:
                continue
            before = text[: match.start()]
            owner = re.findall(r"^def (\w+)", before, flags=re.MULTILINE)[-1]
            if owner != "_recipe_local":
                offenders.append(f"{path.name}:{before.count(chr(10)) + 1}")
    assert offenders == []
    for name in ("catalog.py", "views.py"):
        text = (LIBRARY_SRC / name).read_text(encoding="utf-8")
        assert "self._recipe_local(" in text
