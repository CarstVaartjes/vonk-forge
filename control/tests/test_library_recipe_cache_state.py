"""A recipe is cached only when its own artifact set and its build both are.

A recipe download is one artifact set covering the selected files of every model
the recipe binds, resolved by ``ModelCacheService.resolve_artifact_set``. The
Library shows the less ready of that set and what was built from it, so a
succeeded build never outranks a set that is still downloading. The guard ties
that state to what admission and load planning observe, for recipes of one and
of several models, over sets the real service resolves.
"""

from __future__ import annotations

import copy
import itertools
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import ModelCacheCode
from vonk_control.auth import TokenCodec
from vonk_control.catalog_revision_contract import (
    rederive_projection,
    write_catalog_projection,
)
from vonk_control.library_contract import LibraryLocalState, LibraryRecipeProjection
from vonk_control.library_projection import LibraryProjection
from vonk_control.library_projection.common import compose_recipe_state
from vonk_control.models import (
    AgentNode,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    ClusterMapping,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
    RecipeInstallation,
)
from vonk_control.run_switch_operations import DatabaseRunSwitchArtifactInspector
from vonk_control.stored_json import write_guard_mode
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .test_library_local_state_history import _cache_set, _operation
from .test_model_cache import (  # noqa: F401  (cache is a pytest fixture)
    _canonical_model,
    _canonical_recipe,
    _write_object_receipt,
    cache,
)

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
LIBRARY_SRC = Path(__file__).parents[1] / "src/vonk_control/library_projection"


PINNED = {
    "reference": "ghcr.io/vonk/resolver@sha256:" + "6" * 64,
    "build_key": "9" * 64,
}


def _catalog_rows(
    kind: str,
    definition,
    revision_id: str,
    digest: str,
    projected: dict[str, object] | None = None,
    execution_key: str = "a" * 64,
) -> list[CatalogDocument | CatalogDocumentRevision | CatalogDocumentHead]:
    document_id = str(uuid.uuid4())
    publisher, slug = definition.identity.publisher, definition.identity.slug
    return [
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
            execution_key=execution_key if kind == "recipe" else None,
            projected=projected or {},
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
            rows += _catalog_rows(kind, definition, revision_id, digest)
        with sessions.begin() as session:
            for row in rows:
                session.add(row)
                session.flush()
            session.add(AgentNode(node_id="builder", state="active"))
        self._document = document
        self._recipe = recipe

    def add_sibling(self) -> None:
        """A second recipe document over the same models and the same pinned image."""

        document = copy.deepcopy(self._document)
        document["identity"] = {"publisher": "vonk-forge", "slug": "resolver-dual"}
        recipe = RecipeDefinition.model_validate(document)
        self.sibling_digest = document_sha256(recipe.model_dump(mode="json"))
        self.sibling_revision_id = str(uuid.uuid4())

        def pinned_projection(definition: RecipeDefinition) -> dict[str, object]:
            return write_catalog_projection(
                rederive_projection({"prebuilt_image": PINNED}, definition),
                kind="recipe",
            )

        pinned = pinned_projection(recipe)
        with self.sessions.begin() as session:
            for row in _catalog_rows(
                "recipe",
                recipe,
                self.sibling_revision_id,
                self.sibling_digest,
                pinned,
                execution_key="b" * 64,
            ):
                session.add(row)
                session.flush()
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == self.recipe_revision_id)
                .values(projected=pinned_projection(self._recipe))
            )

    def recipe_set(
        self, service, state: str, revision_id: str | None = None
    ) -> ModelCacheSet:
        """The set the service resolves for the recipe, as a download stores it.

        ``revision_id`` is the recipe whose request cached it first; the row keeps
        that recipe as provenance.
        """

        manifest = service.resolve_artifact_set(
            recipe_revision_id=revision_id or self.recipe_revision_id
        )
        row = _cache_set(
            manifest.digest,
            manifest.model_content_digests[0]
            if manifest.model_content_sha256 is None
            else manifest.model_content_sha256,
            manifest.recipe_revision_sha256,
            state,
            manifest.document(),
        )
        row.expected_bytes = manifest.expected_bytes
        row.verified_bytes = manifest.expected_bytes if state == "cached" else 0
        if state == "cached":
            for artifact in manifest.artifacts:
                path = service.root / "objects" / artifact.sha256[:2] / artifact.sha256
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"one")
                _write_object_receipt(service, artifact.sha256, artifact.expected_bytes)
        return row

    def build(
        self, state: str = "succeeded", revision_id: str | None = None
    ) -> RecipeBuild:
        return RecipeBuild(
            id=str(uuid.uuid4()),
            recipe_revision_id=revision_id or self.recipe_revision_id,
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


def _recipes(projection: LibraryProjection) -> dict[str, LibraryRecipeProjection]:
    return {
        f"{recipe.identity.publisher}/{recipe.identity.slug}": recipe
        for recipe in projection.recipe_library(limit=10).recipes
    }


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


def _put_image(service, present: bool) -> None:
    image = service.root.parent / "runtime-images" / ("8" * 64)
    if present:
        image.write_bytes(b"x" * 17)
    else:
        image.unlink(missing_ok=True)


@pytest.mark.parametrize("count", [1, 2])
def test_a_recipe_prepares_while_its_set_downloads_then_is_cached(
    cache,  # noqa: F811
    count: int,
):
    service, sessions = cache
    world = _World(sessions, count=count)
    row = world.recipe_set(service, "downloading")
    download = _operation(
        row.artifact_set_sha256, kind="download", state="running", at=NOW, completed=40
    )
    _put_image(service, True)
    with sessions.begin() as session:
        session.add_all([row, world.build()])
        session.flush()
        session.add(download)
    projection = _projection(sessions, archive=service._runtime_archive_available)

    # A succeeded build does not make a recipe whose set is downloading cached.
    recipe = _recipe(projection)
    assert recipe.local.controller == "preparing"
    assert recipe.local.preparation is not None
    assert recipe.local.preparation.operation_id == download.id
    assert recipe.local.preparation.completed_bytes == 40
    detail = projection.recipe_detail("vonk-forge/resolver-recipe")
    assert detail.local == recipe.local

    # The download finishes: the same recipe reads cached, so nothing asks for
    # it again, and admission agrees.
    done = world.recipe_set(service, "cached")
    with sessions.begin() as session:
        session.execute(
            update(ModelCacheSet)
            .where(ModelCacheSet.artifact_set_sha256 == row.artifact_set_sha256)
            .values(state="cached", verified_bytes=done.verified_bytes)
        )
        session.execute(
            update(ModelCacheOperation)
            .where(ModelCacheOperation.id == download.id)
            .values(state="succeeded")
        )
    assert _recipe(projection).local.controller == "cached"
    resolution = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe", model_variant="fp16"
    )
    assert resolution.blockers == []


def test_a_first_model_set_alone_does_not_make_a_two_model_recipe_cached(
    cache,  # noqa: F811
):
    """Admission reads the recipe's own set, not the first model's set alone."""

    service, sessions = cache
    world = _World(sessions, count=2)
    alone = service.resolve_artifact_set(model_content_sha256=world.model_digests[0])
    row = _cache_set(
        alone.digest,
        world.model_digests[0],
        None,
        "cached",
        alone.document(),
    )
    row.expected_bytes = alone.expected_bytes
    row.verified_bytes = alone.expected_bytes
    for artifact in alone.artifacts:
        path = service.root / "objects" / artifact.sha256[:2] / artifact.sha256
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"one")
        _write_object_receipt(service, artifact.sha256, artifact.expected_bytes)
    with sessions.begin() as session:
        session.add(row)
    blocked = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe", model_variant="fp16"
    )
    assert "model-not-cached" in blocked.blockers


@pytest.mark.parametrize(
    ("count", "set_state", "image_state"),
    list(itertools.product([1, 2], ["cached", "downloading"], ["present", "missing"])),
)
def test_library_cached_means_admission_and_load_planning_find_nothing_missing(
    cache,  # noqa: F811
    count: int,
    set_state: str,
    image_state: str,
):
    """Shared fixture states: cached in the Library implies nothing left to fetch."""

    service, sessions = cache
    world = _World(sessions, count=count)
    row = world.recipe_set(service, set_state)
    _put_image(service, image_state == "present")
    with sessions.begin() as session:
        session.add_all([row, world.build()])
        session.flush()
        if set_state == "downloading":
            session.add(
                _operation(
                    row.artifact_set_sha256,
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
            now=NOW,
        )

    if library.local.controller == "cached":
        assert resolution.blockers == []
        assert inspection.missing_nas_bytes == 0
    if set_state == "cached" and image_state == "present":
        assert library.local.controller == "cached"
    else:
        assert library.local.controller != "cached"


@pytest.mark.parametrize(
    ("count", "installation", "expected"),
    [
        (count, state, expected)
        for count in (1, 2)
        for state, expected in (
            ("planned", "preparing"),
            ("installing", "preparing"),
            ("partial", "preparing"),
            ("installed", "cached"),
            ("failed", "cached"),
            ("uninstalled", "cached"),
        )
    ],
)
def test_an_ended_installation_does_not_pull_a_cached_set_down(
    cache,  # noqa: F811
    count: int,
    installation: str,
    expected: str,
):
    """A head that inherited its image has no build row; its set is cached. The
    retention uninstall (or a failed install) leaves the recipe cached."""

    service, sessions = cache
    world = _World(sessions, count=count)
    row = world.recipe_set(service, "cached")
    mapping_id = str(uuid.uuid4())
    with write_guard_mode(strict=False), sessions.begin() as session:
        session.add_all([row])
        session.add(
            ClusterMapping(
                id=mapping_id,
                recipe_revision_id=world.recipe_revision_id,
                topology_name="single",
                generation=1,
                node_count=1,
                state="ready",
                parameters={},
                placement_digest="f" * 64,
                endpoint_owner_node_id="builder",
                created_by="test",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            RecipeInstallation(
                id=str(uuid.uuid4()),
                recipe_revision_id=world.recipe_revision_id,
                model_content_sha256=world.model_digests[0],
                mapping_id=mapping_id,
                mapping_generation=1,
                recipe_build_id=None,
                image_digest="sha256:" + "e" * 64,
                plan_digest="1" * 64,
                plan={"schema_version": 2},
                state=installation,
                actor="test",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    assert _recipe(_projection(sessions)).local.controller == expected


def test_the_weaker_state_wins_in_one_total_order():
    local = LibraryLocalState
    cached, preparing = local(controller="cached"), local(controller="preparing")
    failed, missing = local(controller="failed"), local(controller="not_cached")
    unknown = local(controller="unknown")
    order = [failed, unknown, missing, preparing, cached]
    for lower, higher in itertools.combinations(order, 2):
        for first, second in ((lower, higher), (higher, lower)):
            assert compose_recipe_state(first, second).controller == lower.controller


def test_a_recipes_facts_reach_the_result_only_through_the_fold():
    """Sets and requests are cache facts, builds and runs asset facts; no fact is
    merged into the result directly, so the fold is the one place a recipe's
    state is decided."""

    source = (LIBRARY_SRC / "local_state.py").read_text(encoding="utf-8")
    body = source[source.index("def _database_local_state") :]
    body = body[: body.index("\ndef ")]
    targets = re.findall(r"_merge_local\(\s*(\w+)", body)
    assert targets and set(targets) <= {"cache_facts", "asset_facts"}
    assert "_fold_local_facts(" in body


@pytest.mark.parametrize("first", ["resolver-recipe", "resolver-dual"])
def test_a_sibling_recipe_sharing_the_prebuilt_image_and_models_is_cached(
    cache,  # noqa: F811
    first: str,
):
    """The recipe that reached the build first does not own it: identity is content.

    The second recipe reuses the first one's succeeded build without a build row
    of its own, and the model set names the first as provenance. Both read cached
    in the Library and have nothing missing at admission.
    """

    service, sessions = cache
    world = _World(sessions, count=1)
    world.add_sibling()
    revisions = {
        "resolver-recipe": world.recipe_revision_id,
        "resolver-dual": world.sibling_revision_id,
    }
    row = world.recipe_set(service, "cached", revisions[first])
    _put_image(service, True)
    with sessions.begin() as session:
        session.add_all([row, world.build(revision_id=revisions[first])])

    recipes = _recipes(
        _projection(sessions, archive=service._runtime_archive_available)
    )
    for slug in revisions:
        assert recipes[f"vonk-forge/{slug}"].local.controller == "cached", slug
        resolution = service.resolve_latest_cached(
            recipe_identity=f"vonk-forge/{slug}", model_variant="fp16"
        )
        assert resolution.blockers == [], slug


@pytest.mark.parametrize("first", ["resolver-recipe", "resolver-dual"])
def test_a_sibling_recipe_follows_the_shared_set_while_it_downloads(
    cache,  # noqa: F811
    first: str,
):
    """Cache facts follow the recipe's own artifact set, not the recipe that
    requested it first: both recipes share the download and its progress, and
    the finished build does not make either of them cached before the set is."""

    service, sessions = cache
    world = _World(sessions, count=1)
    world.add_sibling()
    revisions = {
        "resolver-recipe": world.recipe_revision_id,
        "resolver-dual": world.sibling_revision_id,
    }
    row = world.recipe_set(service, "downloading", revisions[first])
    download = _operation(
        row.artifact_set_sha256, kind="download", state="running", at=NOW, completed=40
    )
    _put_image(service, True)
    with sessions.begin() as session:
        session.add_all([row, world.build(revision_id=revisions[first])])
        session.flush()
        session.add(download)

    recipes = _recipes(
        _projection(sessions, archive=service._runtime_archive_available)
    )
    for slug in revisions:
        local = recipes[f"vonk-forge/{slug}"].local
        assert local.controller == "preparing", slug
        assert local.preparation is not None
        assert local.preparation.operation_id == download.id, slug
        assert local.preparation.completed_bytes == 40, slug
        resolution = service.resolve_latest_cached(
            recipe_identity=f"vonk-forge/{slug}", model_variant="fp16"
        )
        assert "model-not-cached" in resolution.blockers, slug


def test_each_recipe_set_is_resolved_once_across_library_reads(
    cache,  # noqa: F811
    monkeypatch,
):
    from vonk_control.library_projection import local_state

    _, sessions = cache
    world = _World(sessions, count=1)
    world.add_sibling()
    monkeypatch.setattr(local_state, "_RESOLVED_RECIPE_SETS", {})
    calls: list[str] = []
    resolve = local_state.resolve_recipe_set
    monkeypatch.setattr(
        local_state,
        "resolve_recipe_set",
        lambda session, revision_id: (
            calls.append(revision_id) or resolve(session, revision_id)
        ),
    )
    projection = _projection(sessions)
    _recipes(projection)
    _recipes(projection)
    assert sorted(calls) == sorted(
        [world.recipe_revision_id, world.sibling_revision_id]
    )


@pytest.mark.parametrize("refused", [False, True])
def test_an_unresolvable_set_reads_unknown_not_cached_from_its_build(
    cache,  # noqa: F811
    monkeypatch,
    refused: bool,
):
    from vonk_control.library_projection import local_state
    from vonk_control.model_cache.errors import (
        ModelCacheResolutionInvalid,
        ModelCacheResolutionRefused,
    )

    service, sessions = cache
    world = _World(sessions, count=1)
    world.add_sibling()
    _put_image(service, True)
    with sessions.begin() as session:
        session.add(world.build(revision_id=world.sibling_revision_id))
    monkeypatch.setattr(local_state, "_RESOLVED_RECIPE_SETS", {})

    def refuse(session, revision_id):
        if refused:
            raise ModelCacheResolutionRefused("source-refused", "source policy refused")
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.RECIPE_MODEL_MISSING,
            "recipe does not bind an exact model definition",
        )

    monkeypatch.setattr(local_state, "resolve_recipe_set", refuse)
    recipes = _recipes(
        _projection(sessions, archive=service._runtime_archive_available)
    )
    assert recipes["vonk-forge/resolver-dual"].local.controller == "unknown"
