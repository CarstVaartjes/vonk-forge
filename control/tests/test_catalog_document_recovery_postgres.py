"""Whole-table stored-document reads and recovery through the catalog owner."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import CatalogSyncState
from vonk_control.auth import CursorCodec
from vonk_control.catalog_queries import active_head_revision
from vonk_control.catalog_revision_contract import read_catalog_document
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.lifecycle.evidence import Residue, read_or_rebuild
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache.fleet_removal import _model_objects
from vonk_control.models import Base, CatalogDocumentRevision
from vonk_control.recipe_library_types import RecipeLibraryItem, RecipeLibrarySnapshot
from vonk_forge_contracts import ModelDefinition

from .test_model_removal_reference_lifecycle import _one_model

NOW = datetime(2026, 10, 20, tzinfo=UTC)


@dataclass(frozen=True)
class CatalogReadFailure:
    revision_id: str
    publisher: str
    slug: str
    content_digest: str
    current_head: bool
    residue: Residue


def audit_catalog_documents(session: Session) -> list[CatalogReadFailure]:
    """Read every row without effects; collect all damage instead of stopping early.

    Only use a disposable test database. Neither this harness nor its tests
    connect to the production database.
    """
    failures = []
    for row, current in session.execute(
        select(CatalogDocumentRevision, active_head_revision())
    ):
        parsed = read_or_rebuild(
            kind=CatalogDocumentRevision.__tablename__,
            subject=row.id,
            read=partial(read_catalog_document, row),
        )
        if isinstance(parsed, Residue):
            failures.append(
                CatalogReadFailure(
                    row.id, row.publisher, row.slug, row.content_digest, current, parsed
                )
            )
    assert not session.new and not session.dirty and not session.deleted
    return failures


@dataclass
class Source:
    snapshot: RecipeLibrarySnapshot

    def list(self) -> RecipeLibrarySnapshot:
        return self.snapshot

    def fetch(self, uri: str) -> RecipeLibraryItem:
        raise AssertionError(f"this snapshot has no recipes: {uri}")


@pytest.fixture
def catalog(postgres_engine: Engine) -> CatalogService:
    Base.metadata.create_all(postgres_engine)
    return CatalogService(
        sessionmaker(postgres_engine, expire_on_commit=False),
        clock=lambda: NOW,
        cursors=CursorCodec(b"c" * 32),
    )


def _damage(
    session: Session, revision_id: str, *, field: str = "files.0.size_bytes"
) -> None:
    row = session.get(CatalogDocumentRevision, revision_id)
    assert row is not None
    document = row.document.copy()
    if field == "modalities.0":
        document["modalities"] = ["private-input"]
    else:
        files = document["files"]
        assert isinstance(files, list)
        first = files[0]
        assert isinstance(first, dict)
        document["files"] = [{**first, "size_bytes": "private-input"}]
    session.execute(
        update(CatalogDocumentRevision)
        .where(CatalogDocumentRevision.id == revision_id)
        .values(document=document)
        .execution_options(synchronize_session=False)
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_historical_damage_is_retired_without_blocking_current_reads(
    catalog: CatalogService, tmp_path: Path
) -> None:
    """Catches treating every retained revision as required live evidence.

    Healthy history still protects shared bytes; superseded malformed history
    cannot gate a review, while a broken current head remains visibly unknown.
    """
    model = _one_model(tmp_path, "target")
    sibling = _one_model(tmp_path, "sibling")
    for value in (model, sibling):
        catalog.import_catalog_models("test", [value.model_dump(mode="json")])
    with catalog._sessions() as session:
        old = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.slug == sibling.identity.slug
            )
        )
        assert old is not None
    changed = _one_model(tmp_path, "sibling", data=b"new weights")
    candidate = catalog.entities.revise(
        old.document_id, changed.model_dump(mode="json"), actor="test"
    )
    catalog.entities.resolve(candidate.id, actor="test")
    with catalog._sessions() as session:
        target = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.slug == model.identity.slug
            )
        )
        assert target is not None
        # Even superseded healthy documents still count as shared content.
        objects, shared = _model_objects(session, target.content_digest)
        assert objects == {model.files[0].sha256}
        assert shared == objects | {changed.files[0].sha256}
    with catalog._sessions.begin() as session:
        _damage(session, old.id)
    with catalog._sessions() as session:
        failures = audit_catalog_documents(session)
        assert [failure.revision_id for failure in failures] == [old.id]
        assert not failures[0].current_head
        assert failures[0].slug == sibling.identity.slug
        # A mutation consumer uses the same scan, without an exception or gate.
        assert _model_objects(session, target.content_digest) == (
            objects,
            {changed.files[0].sha256},
        )
        assert "files.0.size_bytes" in failures[0].residue.note
        assert "private-input" not in failures[0].residue.note
    cache = ModelCacheService(catalog._sessions, tmp_path / "nas", clock=lambda: NOW)
    try:
        review = cache.review_fleet_model_removal(target.content_digest)
        assert review.fleet is not None
        assert not review.fleet.observation_issues and not review.blockers
    finally:
        cache.close()
    with catalog._sessions() as session:
        # Retirement is an unknown read, not a mutation of retained evidence.
        assert len(audit_catalog_documents(session)) == 1


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("field", ["files.0.size_bytes", "modalities.0"])
def test_current_head_reports_field_and_normal_sync_rebuilds_exact_revision(
    catalog: CatalogService, tmp_path: Path, field: str
) -> None:
    """Catches lost field evidence, false unchanged sync, and reader-side writes."""
    model = _one_model(tmp_path, "current")
    source = Source(
        RecipeLibrarySnapshot(
            commit="a" * 40, items=(), catalog_entities=(model.model_dump(mode="json"),)
        )
    )
    sync = ManagedRecipeCatalogSyncService(
        catalog._sessions, catalog=catalog, reader=source, clock=lambda: NOW
    )
    assert sync.automatic().state == CatalogSyncState.CURRENT
    with catalog._sessions.begin() as session:
        row = session.scalar(select(CatalogDocumentRevision))
        assert row is not None
        revision_id, digest = row.id, row.content_digest
        _damage(session, revision_id, field=field)
    cache = ModelCacheService(catalog._sessions, tmp_path / "nas", clock=lambda: NOW)
    try:
        review = cache.review_fleet_model_removal(digest)
        assert review.fleet is not None
        issue = review.fleet.observation_issues[0]
        assert issue.subject == revision_id
        assert field in issue.detail
        assert model.identity.slug in issue.detail
        assert "private-input" not in issue.detail
        assert review.blockers
        with catalog._sessions() as session:
            failures = audit_catalog_documents(session)
            assert len(failures) == 1 and failures[0].current_head
        # The next ordinary sync, at the same source commit, owns repair.
        assert sync.automatic().state == CatalogSyncState.CURRENT
        assert sync.automatic().state == CatalogSyncState.CURRENT
        with catalog._sessions() as session:
            assert not audit_catalog_documents(session)
            repaired = session.get(CatalogDocumentRevision, revision_id)
            assert repaired is not None and repaired.content_digest == digest
            assert isinstance(read_catalog_document(repaired), ModelDefinition)
            assert len(session.scalars(select(CatalogDocumentRevision)).all()) == 1
        healed = cache.review_fleet_model_removal(digest)
        assert healed.fleet is not None and not healed.fleet.observation_issues
        assert not healed.blockers
    finally:
        cache.close()


@pytest.mark.usefixtures("damaged_json_rows")
def test_whole_table_audit_collects_every_bad_row_without_effects(
    catalog: CatalogService, tmp_path: Path
) -> None:
    """Catches a fail-fast or head-only audit hiding other damaged documents."""
    for slug in ("healthy", "bad-size", "bad-enum"):
        model = _one_model(tmp_path, slug)
        catalog.import_catalog_models("test", [model.model_dump(mode="json")])
    with catalog._sessions.begin() as session:
        rows = session.scalars(select(CatalogDocumentRevision)).all()
        for row in rows:
            if row.slug != "healthy":
                _damage(
                    session,
                    row.id,
                    field="modalities.0"
                    if row.slug == "bad-enum"
                    else "files.0.size_bytes",
                )
    with catalog._sessions() as session:
        # Enforce a real read-only transaction, independently of ORM tracking.
        session.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
        failures = audit_catalog_documents(session)
        assert {failure.slug for failure in failures} == {"bad-size", "bad-enum"}
        assert all(failure.current_head for failure in failures)
        assert all("private-input" not in failure.residue.note for failure in failures)


@pytest.mark.usefixtures("damaged_json_rows")
def test_sync_replaces_a_damaged_head_when_source_revision_is_already_local(
    catalog: CatalogService, tmp_path: Path
) -> None:
    """Catches validating only the source's historical row and keeping a bad head."""
    model = _one_model(tmp_path, "source-head")
    source = Source(
        RecipeLibrarySnapshot(
            commit="a" * 40, items=(), catalog_entities=(model.model_dump(mode="json"),)
        )
    )
    now = [NOW]
    sync = ManagedRecipeCatalogSyncService(
        catalog._sessions, catalog=catalog, reader=source, clock=lambda: now[0]
    )
    sync.automatic()
    with catalog._sessions() as session:
        original = session.scalar(select(CatalogDocumentRevision))
        assert original is not None
    newer = _one_model(tmp_path, "source-head", data=b"newer weights")
    candidate = catalog.entities.revise(
        original.document_id, newer.model_dump(mode="json"), actor="test"
    )
    catalog.entities.resolve(candidate.id, actor="test")
    with catalog._sessions.begin() as session:
        _damage(session, candidate.id, field="modalities.0")
    # Distinct completions keep the latest successful episode unambiguous.
    now[0] += timedelta(seconds=1)
    healed = sync.automatic()
    assert healed.state == CatalogSyncState.CURRENT
    with catalog._sessions() as session:
        selected = session.scalar(
            select(CatalogDocumentRevision).where(active_head_revision())
        )
        assert selected is not None and selected.id == original.id
        assert read_catalog_document(selected) == model
        failures = audit_catalog_documents(session)
        assert len(failures) == 1 and not failures[0].current_head
        assert failures[0].revision_id == candidate.id
        assert len(session.scalars(select(CatalogDocumentRevision)).all()) == 2
    assert sync.automatic().id == healed.id
