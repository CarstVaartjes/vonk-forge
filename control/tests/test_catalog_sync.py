from __future__ import annotations

import json
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import vonk_control.catalog_entities as catalog_entities_module
import vonk_control.catalog_sync as catalog_sync_module
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_api import ManagedCatalogSyncResponse, _managed_sync
from vonk_control.catalog_queries import active_head_revision
from vonk_control.catalog_revision_contract import (
    read_catalog_projection,
)
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import (
    CatalogSyncError,
    ManagedRecipeCatalogSyncService,
    _empty_result,
)
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    Base,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    RecipeLibrarySyncRun,
)
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.recipe_library_types import (
    RecipeLibraryError,
    RecipeLibraryItem,
    RecipeLibrarySnapshot,
)
from vonk_control.source_bundles import (
    SourceBundleStore,
    generate_source_bundle,
    parse_source_bundle,
)
from vonk_forge_contracts import (
    CONTRACT_VERSION,
    ModelDefinition,
    RecipeDefinition,
    document_sha256,
)

from tests.recipe_library_source import recipe_library_root
from tests.signed_recipe_release import SignedRecipeRelease, signed_recipe_releases

ROOT = recipe_library_root()
pytestmark = pytest.mark.usefixtures(signed_recipe_releases.__name__)


def _active_revision(session, document_id: str) -> CatalogDocumentRevision | None:
    return session.scalar(
        select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.document_id == document_id,
            CatalogDocumentRevision.state == "active",
            active_head_revision(),
        )
    )


def _document_section(document: dict[str, object], key: str) -> dict[str, object]:
    """Narrow one decoded JSON object member so a deliberate edit stays typed."""

    section = document[key]
    assert isinstance(section, dict)
    return section


def _selected_model_reference(recipe: dict[str, object]) -> dict[str, object]:
    """Narrow ``models[0].model`` in a decoded recipe document."""

    models = recipe["models"]
    assert isinstance(models, list)
    selection = models[0]
    assert isinstance(selection, dict)
    reference = selection["model"]
    assert isinstance(reference, dict)
    return reference


class Reader:
    def __init__(self, snapshot: RecipeLibrarySnapshot) -> None:
        self.snapshot = snapshot
        self.fetches: list[str] = []

    def list(self) -> RecipeLibrarySnapshot:
        return self.snapshot

    def fetch(self, uri: str) -> RecipeLibraryItem:
        self.fetches.append(uri)
        return next(item for item in self.snapshot.items if item.uri == uri)


def _item_with_document(
    item: RecipeLibraryItem, document: dict[str, object]
) -> RecipeLibraryItem:
    recipe = RecipeDefinition.model_validate(document)
    digest = document_sha256(recipe.model_dump(mode="json"))
    return replace(
        item,
        content_sha256=digest,
        uri=f"vonk://catalog/{item.publisher}/{item.slug}@sha256:{digest}",
        document=recipe.model_dump(mode="json"),
        tags=tuple(recipe.metadata.tags),
        release=None,
        package_handle=None,
        package_sha256=None,
        source_bundle=None,
        source_bundle_sha256=None,
    )


def _fixture(
    tmp_path: Path,
) -> tuple[sessionmaker, CatalogService, Reader, RecipeLibraryItem]:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    index["recipes"] = index["recipes"][:1]
    client = SignedRecipeRelease.from_library(index, ROOT).client(tmp_path / "packages")
    snapshot = client.list()
    item = client.fetch(snapshot.items[0].uri)
    # The snapshot carries one recipe, so it only needs that recipe's Models;
    # importing the whole published Model set on every sync made each case slow.
    recipe = RecipeDefinition.model_validate(item.document)
    selected = {
        (selection.model.publisher, selection.model.slug) for selection in recipe.models
    }
    snapshot = RecipeLibrarySnapshot(
        snapshot.commit,
        (item,),
        snapshot.repository,
        tuple(
            entity
            for entity in snapshot.catalog_entities
            if (
                (model := ModelDefinition.model_validate(entity)).identity.publisher,
                model.identity.slug,
            )
            in selected
        ),
        version=snapshot.version,
        updated_at=snapshot.updated_at,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "bundles"),
    )
    return sessions, service, Reader(snapshot), item


class FailOnceReader(Reader):
    def __init__(self, snapshot: RecipeLibrarySnapshot, failing_uri: str) -> None:
        super().__init__(snapshot)
        self._failing_uri = failing_uri
        self._failed = False

    def fetch(self, uri: str) -> RecipeLibraryItem:
        if uri == self._failing_uri and not self._failed:
            self.fetches.append(uri)
            self._failed = True
            raise RecipeLibraryError(
                "recipe_library.unavailable", "transient recipe fetch failure"
            )
        return super().fetch(uri)


class UntypedFailOnceReader(Reader):
    def __init__(self, snapshot: RecipeLibrarySnapshot, failing_uri: str) -> None:
        super().__init__(snapshot)
        self._failing_uri = failing_uri
        self._failed = False

    def fetch(self, uri: str) -> RecipeLibraryItem:
        if uri == self._failing_uri and not self._failed:
            self.fetches.append(uri)
            self._failed = True
            raise RuntimeError("temporary package transport failure")
        return super().fetch(uri)


class FailingListReader(Reader):
    def list(self) -> RecipeLibrarySnapshot:
        raise RecipeLibraryError(
            "recipe_library.unavailable", "transient recipe index failure"
        )


def _sync(sessions, service, reader) -> ManagedRecipeCatalogSyncService:
    return ManagedRecipeCatalogSyncService(
        sessions,
        catalog=service,
        reader=reader,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )


def test_sync_imports_canonical_models_and_changed_recipe_once(tmp_path: Path) -> None:
    sessions, service, reader, item = _fixture(tmp_path)
    sync = _sync(sessions, service, reader)
    result = sync.sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )
    assert result.state == "current"
    assert result.imported_count == 1
    assert (result.library_version, result.library_updated_at) == (
        reader.snapshot.version,
        reader.snapshot.updated_at,
    )
    assert result.library_version == CONTRACT_VERSION
    assert reader.fetches == [item.uri]
    with sessions() as session:
        revisions = session.scalars(select(CatalogDocumentRevision)).all()
        assert len([row for row in revisions if row.kind == "model"]) == len(
            reader.snapshot.catalog_entities
        )
        assert len([row for row in revisions if row.kind == "recipe"]) == 1


def test_invalid_model_document_does_not_block_other_catalog_items(
    tmp_path: Path,
) -> None:
    sessions, service, reader, _item = _fixture(tmp_path)
    reader.snapshot = replace(
        reader.snapshot,
        catalog_entities=(*reader.snapshot.catalog_entities, {"invalid": True}),
    )

    result = _sync(sessions, service, reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )

    assert result.state == "partial"
    assert result.skipped_count == 1
    with sessions() as session:
        revisions = session.scalars(select(CatalogDocumentRevision)).all()
        assert any(row.kind == "recipe" for row in revisions)
        assert (
            len([row for row in revisions if row.kind == "model"])
            == len(reader.snapshot.catalog_entities) - 1
        )


@pytest.mark.parametrize("malformed", [False, True])
def test_unchanged_catalog_refreshes_build_policy_without_refetching_recipe(
    tmp_path: Path,
    malformed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions, catalog, reader, item = _fixture(tmp_path)
    sync = _sync(sessions, catalog, reader)
    compile_policy = catalog_entities_module.build_policy_projection

    def previous_policy(recipe):
        policy = compile_policy(recipe)
        _document_section(policy, "build_options")["layers"] = True
        return policy

    # Persist the previous compiler output through the real import path.
    with monkeypatch.context() as previous:
        previous.setattr(
            catalog_entities_module, "build_policy_projection", previous_policy
        )
        first = sync.automatic()
    builds = RecipeBuildService(
        sessions, bundles=SourceBundleStore(tmp_path / "bundles")
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
            )
        )
        assert revision is not None
        revision_id, digest = revision.id, revision.content_digest
        original_document = deepcopy(revision.document)
    previous_input = builds.resolve(revision_id).input_intent_sha256
    if malformed:
        with sessions.begin() as session:
            revision = session.get(CatalogDocumentRevision, revision_id)
            assert revision is not None
            stored = deepcopy(revision.projected)
            _document_section(stored, "build_options")["layers"] = "true"
            # Deliberately corrupt persisted JSON outside the typed writer.
            session.execute(
                update(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.id == revision_id,
                )
                .values(projected=stored)
            )
        # An unreadable revision is skipped, never a reason to stop syncing.
        sync.automatic()
        return

    refreshed = sync.automatic()
    current_input = builds.resolve(revision_id).input_intent_sha256
    assert current_input != previous_input
    assert refreshed.id == first.id
    assert reader.fetches == [item.uri]
    with sessions() as session:
        revisions = tuple(
            session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                )
            )
        )
        assert [(row.id, row.content_digest) for row in revisions] == [
            (revision_id, digest)
        ]
        assert revisions[0].document == original_document
    sync.automatic()
    assert builds.resolve(revision_id).input_intent_sha256 == current_input
    assert reader.fetches == [item.uri]


def test_sync_reactivates_retained_recipe_without_replacing_history_or_model_head(
    tmp_path: Path,
) -> None:
    sessions, catalog, reader, original = _fixture(tmp_path)
    sync = _sync(sessions, catalog, reader)
    library = LibraryProjection(
        sessions, cursors=catalog._cursors, clock=catalog._clock
    )

    def apply(item, commit):
        item = replace(item, library_commit=commit)
        reader.snapshot = replace(reader.snapshot, commit=commit, items=(item,))
        return sync.sync(
            request_key=str(uuid.uuid4()),
            trigger="manual",
            actor="test",
            expected_commit=commit,
        )

    first_result = apply(original, "1" * 40)
    assert first_result.state == "current"
    assert first_result.imported_count == 1
    first = library.recipe_library().recipes[0]

    changed = deepcopy(original.document)
    _document_section(changed, "metadata")["title"] = "Accepted recipe successor"
    replacement = _item_with_document(original, changed)
    second_result = apply(replacement, "2" * 40)
    assert second_result.state == "current"
    assert second_result.updated_count == 1
    second = library.authoring_recipe_detail(first.identity.recipe_id).recipe
    assert second.content_sha256 == replacement.content_sha256
    assert second.recipe_revision_id != first.identity.recipe_revision_id

    invalid = deepcopy(changed)
    _selected_model_reference(invalid)["content_sha256"] = "f" * 64
    failed_result = apply(_item_with_document(original, invalid), "3" * 40)
    assert failed_result.state == "partial"
    assert failed_result.problems
    assert (
        library.authoring_recipe_detail(
            first.identity.recipe_id
        ).recipe.recipe_revision_id
        == second.recipe_revision_id
    )

    # A newer Model head is independent of this recipe's immutable dependency.
    reference = RecipeDefinition.model_validate(original.document).models[0].model
    old_model = catalog.entities.resolve_reference(reference)
    newer_model = deepcopy(old_model.document)
    _document_section(newer_model, "metadata")["description"] = "New Model metadata"
    draft = catalog.entities.revise(old_model.document_id, newer_model, actor="test")
    model_head = catalog.entities.resolve(draft.id, actor="test")

    pending_document = deepcopy(changed)
    _document_section(pending_document, "metadata")["title"] = "Pending local candidate"
    pending = catalog.entities.revise(
        first.identity.recipe_id, pending_document, actor="test"
    )

    rollback_result = apply(original, "4" * 40)
    assert rollback_result.state == "current"
    assert rollback_result.updated_count == 1
    assert (
        library.authoring_recipe_detail(
            first.identity.recipe_id
        ).recipe.recipe_revision_id
        == first.identity.recipe_revision_id
    )
    assert (
        catalog.get_recipe(first.identity.recipe_id).id
        == first.identity.recipe_revision_id
    )
    assert catalog.get_recipe(second.recipe_revision_id).id == second.recipe_revision_id
    current = catalog.recipe_catalog_local_revisions(
        [(original.publisher, original.slug)]
    )
    assert (
        current[(original.publisher, original.slug)].content_sha256
        == original.content_sha256
    )
    assert catalog.entities.get_entity(old_model.document_id).id == model_head.id
    assert catalog.entities.resolve_reference(reference).id == old_model.id

    with sessions() as session:
        head = session.scalar(
            select(CatalogDocumentHead).where(
                CatalogDocumentHead.kind == "recipe",
                CatalogDocumentHead.publisher == original.publisher,
                CatalogDocumentHead.slug == original.slug,
            )
        )
        assert head.active_revision_id == first.identity.recipe_revision_id
        assert head.candidate_revision_id is None
        assert head.generation == 3
        recipe_active = _active_revision(session, first.identity.recipe_id)
        assert recipe_active is not None
        assert recipe_active.id == first.identity.recipe_revision_id
        assert (
            ModelCacheService._latest_recipe_digest(session, second.content_sha256)
            == first.identity.content_sha256
        )
        revisions = list(
            session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.document_id == first.identity.recipe_id
                )
            )
        )
        assert {row.id for row in revisions if row.state == "active"} == {
            first.identity.recipe_revision_id,
            second.recipe_revision_id,
        }
        assert {row.id for row in revisions if row.state == "failed"} == {pending.id}
        assert read_catalog_projection(
            session.get(CatalogDocumentRevision, pending.id)
        ).failure_reason == (
            f"Superseded by imported recipe {original.content_sha256}."
        )

    repeated = apply(original, "5" * 40)
    assert repeated.state == "current"
    assert repeated.unchanged_count == 1
    assert repeated.updated_count == 0
    with sessions() as session:
        assert session.get(CatalogDocumentHead, head.id).generation == 3


def test_sync_keys_local_revisions_by_publisher_and_slug(tmp_path: Path) -> None:
    sessions, service, reader, item = _fixture(tmp_path)

    def variant(*, publisher: str, slug: str) -> RecipeLibraryItem:
        document = deepcopy(item.document)
        identity = document["identity"]
        assert isinstance(identity, dict)
        identity["publisher"] = publisher
        identity["slug"] = slug
        return _item_with_document(
            replace(
                item, publisher=publisher, slug=slug, source_path=f"recipes/{slug}.json"
            ),
            document,
        )

    first = _sync(sessions, service, reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )
    assert first.state == "current"

    other_publisher = variant(publisher="other-publisher", slug=item.slug)
    same_publisher_a = variant(
        publisher=item.publisher,
        slug=f"{item.slug}-variant-a",
    )
    same_publisher_b = variant(
        publisher=item.publisher,
        slug=f"{item.slug}-variant-b",
    )
    reader.snapshot = replace(
        reader.snapshot,
        items=(item, other_publisher, same_publisher_a, same_publisher_b),
    )
    result = _sync(sessions, service, reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )

    assert result.state == "current"
    assert result.imported_count == 3
    assert result.updated_count == 0
    assert result.skipped_count == 0
    with sessions() as session:
        revisions = session.scalars(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        ).all()
        assert {(row.publisher, row.slug) for row in revisions} == {
            (item.publisher, item.slug),
            (other_publisher.publisher, other_publisher.slug),
            (same_publisher_a.publisher, same_publisher_a.slug),
            (same_publisher_b.publisher, same_publisher_b.slug),
        }
        assert len({row.content_digest for row in revisions}) == 4


def test_local_revision_lookup_accepts_more_than_256_identities(tmp_path: Path) -> None:
    _sessions, service, _reader, _item = _fixture(tmp_path)
    identities = [(f"publisher-{index}", f"recipe-{index}") for index in range(257)]

    assert service.recipe_catalog_local_revisions(identities) == {}


def test_sync_imports_canonical_recipe_without_readiness_tags(tmp_path: Path) -> None:
    sessions, service, reader, item = _fixture(tmp_path)
    document = deepcopy(item.document)
    document["metadata"]["tags"] = []  # type: ignore[index]
    replacement = _item_with_document(item, document)
    reader.snapshot = RecipeLibrarySnapshot(
        reader.snapshot.commit,
        (replacement,),
        reader.snapshot.repository,
        reader.snapshot.catalog_entities,
    )

    result = _sync(sessions, service, reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )

    assert result.state == "current"
    assert result.imported_count == 1
    assert result.problems == ()


def test_sync_fails_closed_for_unresolvable_canonical_recipe(tmp_path: Path) -> None:
    sessions, service, reader, item = _fixture(tmp_path)
    document = deepcopy(item.document)
    document["models"][0]["model"]["content_sha256"] = "0" * 64  # type: ignore[index]
    replacement = _item_with_document(item, document)
    reader.snapshot = RecipeLibrarySnapshot(
        reader.snapshot.commit,
        (replacement,),
        reader.snapshot.repository,
        reader.snapshot.catalog_entities,
    )

    result = _sync(sessions, service, reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )

    assert result.state == "partial"
    assert result.skipped_count == 1
    assert result.problems[0]["code"] == "catalog.model_reference_missing"
    with sessions() as session:
        assert (
            session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe"
                )
            ).all()
            == []
        )


def test_recipe_metadata_tags_do_not_change_execution_identity(tmp_path: Path) -> None:
    sessions, service, _reader, item = _fixture(tmp_path)
    service.import_recipe_library(
        "test",
        library_commit=item.library_commit,
        source_path=item.source_path,
        document=item.document,
        expected_content_sha256=item.content_sha256,
        dependency_documents=item.dependencies,
    )
    document = deepcopy(item.document)
    document["metadata"]["tags"] = ["editorial-only"]  # type: ignore[index]
    replacement = _item_with_document(item, document)
    service.import_recipe_library(
        "test",
        library_commit=replacement.library_commit,
        source_path=replacement.source_path,
        document=replacement.document,
        expected_content_sha256=replacement.content_sha256,
        dependency_documents=replacement.dependencies,
    )

    with sessions() as session:
        revisions = session.scalars(
            select(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.kind == "recipe")
            .order_by(CatalogDocumentRevision.revision_number)
        ).all()
        assert len(revisions) == 2
        assert revisions[0].content_digest != revisions[1].content_digest
        assert revisions[0].execution_key == revisions[1].execution_key


def test_automatic_sync_reuses_same_commit_without_refetch(tmp_path: Path) -> None:
    sessions, service, reader, _item_value = _fixture(tmp_path)
    sync = _sync(sessions, service, reader)
    first = sync.automatic()
    repeated = sync.automatic()
    assert repeated.id == first.id
    assert reader.fetches == [reader.snapshot.items[0].uri]


def test_automatic_sync_retries_partial_same_commit_without_refetching_successes(
    tmp_path: Path,
) -> None:
    sessions, service, reader, item = _fixture(tmp_path)
    second_document = deepcopy(item.document)
    second_slug = f"{item.slug}-retry"
    second_document["identity"]["slug"] = second_slug  # type: ignore[index]
    second = _item_with_document(
        replace(item, slug=second_slug, source_path=f"recipes/{second_slug}.json"),
        second_document,
    )
    reader = FailOnceReader(
        replace(
            reader.snapshot,
            items=(reader.snapshot.items[0], second),
        ),
        second.uri,
    )
    sync = _sync(sessions, service, reader)

    partial = sync.automatic()
    assert partial.state == "partial"
    assert partial.skipped_count == 1
    assert reader.fetches == [reader.snapshot.items[0].uri, second.uri]

    recovered = sync.automatic()
    assert recovered.state == "current"
    assert recovered.id != partial.id
    assert recovered.imported_count == 1
    assert recovered.unchanged_count == 1
    assert reader.fetches == [reader.snapshot.items[0].uri, second.uri, second.uri]


def test_automatic_sync_retries_untyped_fetch_failure_and_keeps_other_items(
    tmp_path: Path,
) -> None:
    sessions, service, reader, item = _fixture(tmp_path)
    second_document = deepcopy(item.document)
    second_slug = f"{item.slug}-untyped-retry"
    second_document["identity"]["slug"] = second_slug  # type: ignore[index]
    second = _item_with_document(
        replace(item, slug=second_slug, source_path=f"recipes/{second_slug}.json"),
        second_document,
    )
    flaky_reader = UntypedFailOnceReader(
        replace(reader.snapshot, items=(reader.snapshot.items[0], second)),
        second.uri,
    )
    sync = _sync(sessions, service, flaky_reader)

    partial = sync.automatic()
    assert partial.state == "partial"
    assert partial.skipped_count == 1
    assert partial.imported_count == 1

    recovered = sync.automatic()
    assert recovered.state == "current"
    assert recovered.unchanged_count == 1
    assert recovered.imported_count == 1
    assert flaky_reader.fetches.count(second.uri) == 2


def test_sync_rejects_preview_commit_mismatch_without_catalog_mutation(
    tmp_path: Path,
) -> None:
    sessions, service, reader, _item_value = _fixture(tmp_path)
    sync = _sync(sessions, service, reader)
    with pytest.raises(CatalogSyncError, match="changed since"):
        sync.sync(
            request_key=str(uuid.uuid4()),
            trigger="manual",
            actor="test",
            expected_commit="b" * 40,
        )
    with sessions() as session:
        assert session.scalars(select(CatalogDocumentRevision)).all() == []


def test_sync_marks_reader_failure_failed_and_releases_active_slot(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "bundles"),
    )
    failing_reader = FailingListReader(RecipeLibrarySnapshot("a" * 40, ()))
    sync = _sync(sessions, service, failing_reader)
    request_key = str(uuid.uuid4())

    with pytest.raises(RecipeLibraryError, match="transient recipe index failure"):
        sync.sync(request_key=request_key, trigger="manual", actor="test")

    latest = sync.latest()
    assert latest is not None
    assert latest.state == "failed"
    assert latest.problems[0]["code"] == "recipe_library.unavailable"
    with sessions() as session:
        run = session.scalar(
            select(RecipeLibrarySyncRun).where(
                RecipeLibrarySyncRun.request_key == request_key
            )
        )
        assert run is not None
        assert run.state == "failed"
        assert run.active_slot is None
        assert run.error_code == "recipe_library.unavailable"


@pytest.mark.parametrize(
    "damage", ["missing-problems", "string-count", "null", "invalid-problem", "extra"]
)
def test_sync_round_trip_rejects_malformed_persisted_result(tmp_path, damage):
    sessions, service, reader, _item = _fixture(tmp_path)
    sync = _sync(sessions, service, reader)
    result = sync.sync(request_key=str(uuid.uuid4()), trigger="manual", actor="test")
    assert sync.get(result.id) == result
    with sessions.begin() as session:
        row = session.get(RecipeLibrarySyncRun, result.id)
        damaged = dict(row.result)
        if damage == "missing-problems":
            del damaged["problems"]
        elif damage == "string-count":
            damaged["withdrawn_count"] = "7"
        elif damage == "null":
            damaged = None
        elif damage == "invalid-problem":
            damaged["problems"] = [{"detail": "missing code"}]
        else:
            damaged["undeclared"] = None
        row.result = damaged
    with pytest.raises(CatalogSyncError, match="stored catalog sync result is invalid"):
        sync.get(result.id)
    with pytest.raises(CatalogSyncError, match="stored catalog sync result is invalid"):
        sync.automatic()


def test_reader_skips_unreadable_index_documents_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    index["recipes"] = index["recipes"][:2]
    models, recipes = index["catalog_entities"], index["recipes"]
    # A field a newer minor contract added is ignored; a document that omits
    # one this Controller still requires is skipped on its own.
    models[1]["document"]["future_field"] = "added by a newer contract"
    del models[0]["document"]["files"]
    del recipes[0]["document"]["metadata"]
    skipped_model = models[0]["document"]["identity"]
    skipped_recipe = recipes[0]["document"]["identity"]

    snapshot = (
        SignedRecipeRelease.from_library(index, ROOT)
        .client(tmp_path / "packages")
        .list()
    )

    assert len(snapshot.catalog_entities) == len(models) - 1
    assert [(item.publisher, item.slug) for item in snapshot.items] == [
        (
            recipes[1]["document"]["identity"]["publisher"],
            recipes[1]["document"]["identity"]["slug"],
        )
    ]
    codes = {problem["code"] for problem in snapshot.problems}
    assert codes == {"recipe_package.document_incompatible"}
    details = [str(problem["detail"]) for problem in snapshot.problems]
    assert any(
        f"{skipped_model['publisher']}/{skipped_model['slug']}" in detail
        and "files" in detail
        for detail in details
    )
    assert any(
        f"{skipped_recipe['publisher']}/{skipped_recipe['slug']}" in detail
        and "metadata" in detail
        for detail in details
    )
    recipe_problem = next(
        problem for problem in snapshot.problems if problem["recipe_uri"] is not None
    )
    assert recipe_problem["recipe_uri"] == (
        f"vonk://catalog/{skipped_recipe['publisher']}/{skipped_recipe['slug']}"
        f"@sha256:{recipes[0]['content_sha256']}"
    )


def test_sync_reports_skipped_index_documents_as_partial(tmp_path: Path) -> None:
    sessions, service, reader, _item = _fixture(tmp_path)
    problem = {
        "recipe_uri": None,
        "code": "recipe_package.document_incompatible",
        "detail": "catalog model document is invalid example/future: future_field",
    }
    reader.snapshot = replace(reader.snapshot, problems=(problem,))

    result = _sync(sessions, service, reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )

    assert result.state == "partial"
    assert result.imported_count == 1
    assert result.skipped_count == 1
    assert [(item["code"], item["detail"]) for item in result.problems] == [
        (problem["code"], problem["detail"])
    ]
    assert result.processed_count == result.total_count


def test_automatic_read_failure_is_visible_until_a_sync_succeeds(
    tmp_path: Path,
) -> None:
    sessions, service, reader, _item = _fixture(tmp_path)
    moments = iter(
        datetime(2026, 9, 5, 0, minute, tzinfo=UTC) for minute in range(1, 60)
    )
    sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=service, reader=reader, clock=lambda: next(moments)
    )
    applied = sync.automatic()
    assert applied.state == "current"
    assert sync.latest().last_error is None  # type: ignore[union-attr]

    failing = ManagedRecipeCatalogSyncService(
        sessions,
        catalog=service,
        reader=FailingListReader(reader.snapshot),
        clock=lambda: next(moments),
    )
    for _attempt in range(3):
        with pytest.raises(RecipeLibraryError):
            failing.automatic()

    status = failing.latest()
    assert status is not None
    # The applied catalog stays the reported run; the failure rides along.
    assert (status.id, status.state, status.commit) == (
        applied.id,
        "current",
        reader.snapshot.commit,
    )
    assert status.last_error is not None
    assert status.last_error.code == "recipe_library.unavailable"
    assert status.last_error.detail == "transient recipe index failure"
    assert status.last_error.occurred_at > applied.completed_at  # type: ignore[operator]
    response = ManagedCatalogSyncResponse.model_validate(_managed_sync(status))
    assert response.last_error is not None
    assert response.last_error.code == "recipe_library.unavailable"
    assert response.last_error.occurred_at == status.last_error.occurred_at.isoformat()
    with sessions() as session:
        failures = session.scalars(
            select(RecipeLibrarySyncRun).where(RecipeLibrarySyncRun.state == "failed")
        ).all()
    # Repeating the same failure refreshes one record instead of adding rows.
    assert len(failures) == 1

    reader.snapshot = replace(reader.snapshot, commit="c" * 40)
    recovered = sync.automatic()
    assert recovered.state == "current"
    status = sync.latest()
    assert status is not None
    assert status.id == recovered.id
    assert status.last_error is None


def test_sync_reruns_for_the_same_commit_after_a_controller_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, service, reader, _item = _fixture(tmp_path)
    sync = _sync(sessions, service, reader)
    first = sync.automatic()
    assert sync.automatic().id == first.id

    # Documents this Controller could not read before are read after an upgrade.
    monkeypatch.setattr(
        catalog_sync_module, "_controller_marker", lambda: "vonk-control=next"
    )
    again = sync.automatic()
    assert again.id != first.id
    assert again.commit == first.commit


def test_stale_running_sync_never_blocks_a_new_sync(tmp_path: Path) -> None:
    sessions, service, reader, _item = _fixture(tmp_path)
    sync = _sync(sessions, service, reader)
    now = datetime(2026, 9, 5, tzinfo=UTC)

    def running(started: datetime) -> str:
        with sessions.begin() as session:
            row = RecipeLibrarySyncRun(
                request_key=str(uuid.uuid4()),
                trigger="automatic",
                state="running",
                active_slot="managed-recipes",
                repository="CarstVaartjes/vonk-forge-recipes",
                total_count=0,
                processed_count=0,
                imported_count=0,
                updated_count=0,
                current_count=0,
                conflict_count=0,
                missing_count=0,
                result=_empty_result(),
                actor="test",
                created_at=started,
                started_at=started,
            )
            session.add(row)
            session.flush()
            return row.id

    live = running(now - timedelta(minutes=1))
    with pytest.raises(CatalogSyncError, match="already running"):
        sync.automatic()
    with sessions.begin() as session:
        session.delete(session.get(RecipeLibrarySyncRun, live))

    dead = running(now - timedelta(hours=1))
    assert sync.automatic().state == "current"
    with sessions() as session:
        row = session.get(RecipeLibrarySyncRun, dead)
        assert row is not None
        assert (row.state, row.error_code) == ("failed", "catalog.sync_lease_expired")


def test_sync_reimports_a_republished_package_with_an_unchanged_recipe_document(
    tmp_path: Path,
) -> None:
    """A repaired build source keeps the recipe digest; the stored bundle must follow.

    A package fix that touches only the Dockerfile or its context leaves the
    recipe document, and so its content digest, unchanged.  Sync used to count
    that recipe as unchanged and keep the superseded source bundle, which the
    Controller then kept refusing at download time.
    """

    sessions, service, reader, item = _fixture(tmp_path)
    first = _sync(sessions, service, reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )
    assert first.state == "current"
    assert item.package_handle is not None and item.source_bundle is not None
    with sessions() as session:
        stored = read_catalog_projection(
            session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                    active_head_revision(),
                )
            )
        )
    assert stored.source_bundle_sha256 == item.source_bundle_sha256
    assert stored.package_sha256 == item.package_sha256

    files = dict(parse_source_bundle(item.source_bundle).files)
    files["vonk-patches/repaired.py"] = b"print('repaired')\n"
    repaired_bundle = generate_source_bundle(files)
    republished = replace(
        item,
        library_commit="6" * 40,
        package_sha256="7" * 64,
        package_handle=replace(item.package_handle, package_sha256="7" * 64),
        source_bundle=repaired_bundle.archive,
        source_bundle_sha256=repaired_bundle.sha256,
    )
    assert republished.content_sha256 == item.content_sha256
    assert republished.source_bundle_sha256 != item.source_bundle_sha256
    republished_reader = Reader(
        replace(reader.snapshot, commit="6" * 40, items=(republished,))
    )

    second = _sync(sessions, service, republished_reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit="6" * 40,
    )

    assert second.state == "current"
    assert second.unchanged_count == 0
    assert second.updated_count == 1
    assert republished_reader.fetches == [republished.uri]
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
                active_head_revision(),
            )
        )
        projected = read_catalog_projection(revision)
    assert projected.source_bundle_sha256 == repaired_bundle.sha256
    assert projected.package_sha256 == "7" * 64

    third = _sync(sessions, service, republished_reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit="6" * 40,
    )
    assert third.unchanged_count == 1
    assert republished_reader.fetches == [republished.uri]


def test_sync_retracts_recipes_absent_from_the_published_library(
    tmp_path: Path,
) -> None:
    sessions, catalog, reader, original = _fixture(tmp_path)
    sync = _sync(sessions, catalog, reader)
    library = LibraryProjection(
        sessions, cursors=catalog._cursors, clock=catalog._clock
    )
    other_document = deepcopy(original.document)
    _document_section(other_document, "identity")["slug"] = "deleted-upstream"
    other = replace(
        _item_with_document(original, other_document), slug="deleted-upstream"
    )

    def apply(items, commit):
        items = tuple(replace(item, library_commit=commit) for item in items)
        reader.snapshot = replace(reader.snapshot, commit=commit, items=items)
        return sync.sync(
            request_key=str(uuid.uuid4()),
            trigger="manual",
            actor="test",
            expected_commit=commit,
        )

    def offered() -> set[str]:
        return {recipe.identity.slug for recipe in library.recipe_library().recipes}

    both = apply((original, other), "1" * 40)
    assert both.imported_count == 2
    assert both.withdrawn_count == 0
    assert offered() == {original.slug, other.slug}
    other_revision = catalog.recipe_catalog_local_revisions(
        [(other.publisher, other.slug)]
    )[(other.publisher, other.slug)]

    # A snapshot with no recipes or with skipped documents may be incomplete.
    assert apply((), "2" * 40).withdrawn_count == 0
    assert offered() == {original.slug, other.slug}

    retracted = apply((original,), "3" * 40)
    assert retracted.state == "current"
    assert retracted.withdrawn_count == 1
    assert [item["recipe_id"] for item in retracted.withdrawn_recipes] == [
        other_revision.recipe_id
    ]
    assert offered() == {original.slug}
    # The retained revision still resolves by id: running work is unaffected.
    kept_id = _revision_id(sessions, other_revision.recipe_id)
    assert catalog.get_recipe(kept_id).id == kept_id

    # Retraction is repeatable and publishing the recipe again restores it.
    assert apply((original,), "4" * 40).withdrawn_count == 0
    back = apply((original, other), "5" * 40)
    assert back.state == "current"
    assert offered() == {original.slug, other.slug}


def _revision_id(sessions, document_id: str) -> str:
    with sessions() as session:
        return session.scalars(
            select(CatalogDocumentRevision.id).where(
                CatalogDocumentRevision.document_id == document_id,
                CatalogDocumentRevision.state == "active",
            )
        ).one()
