"""Fleet model removal removes the cached sets the Library shows for the model."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.fleet_model_removal_contract import FleetRemovalReason
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_contract import ModelCacheRemovalResult
from vonk_control.models import CatalogDocument
from vonk_forge_contracts import document_sha256

from .removal_settle import advance_until_ended
from .test_model_cache import _canonical_model
from .test_model_cache_revisions import _contains, _content_keys, _revision
from .test_model_removal_reference_lifecycle import _removal_service


def _revise_catalog_only(sessions, *, slug: str, data: bytes, note: str) -> str:
    """Publish a metadata-only revision of ``test/slug`` without downloading it."""

    model = _canonical_model(
        publisher="test",
        slug=slug,
        file_id="weights",
        file_digest=hashlib.sha256(data).hexdigest(),
    )
    model = model.model_copy(
        update={"metadata": model.metadata.model_copy(update={"description": note})}
    )
    with sessions() as session:
        existing = session.scalar(
            select(CatalogDocument.id).where(
                CatalogDocument.publisher == "test", CatalogDocument.slug == slug
            )
        )
    assert existing is not None
    catalog = CatalogEntityService(sessions, clock=lambda: datetime.now(UTC))
    revised = catalog.revise(existing, model.model_dump(mode="json"), actor="test")
    catalog.resolve(revised.id, actor="test")
    return document_sha256(model.model_dump(mode="json"))


def _removed(service: ModelCacheService, selector: str, review_digest: str):
    accepted = service.remove_fleet_model_selector(
        selector,
        actor="operator",
        request_key=str(uuid4()),
        review_digest=review_digest,
    )
    advance_until_ended(service, accepted.id)
    removed = service.get_operation(accepted.id)
    assert removed.state == LifecycleState.SUCCEEDED, removed.last_error
    assert isinstance(removed.result, ModelCacheRemovalResult)
    return removed.result


@pytest.mark.parametrize("verified_again", [False, True])
def test_removal_reclaims_a_set_first_downloaded_under_an_earlier_revision(
    postgres_engine, tmp_path: Path, monkeypatch, verified_again: bool
) -> None:
    """Catches removal choosing sets by the head's exact content digest.

    The Library shows a model's bytes by its publisher/slug lineage, so a set
    first downloaded under an earlier revision is listed under the head. In
    production the review then said ``unused`` with no assets and the receipt
    ``succeeded`` with nothing removed while the model stayed listed as cached.
    ``verified_again`` also binds the head through a later verified download
    of the same files, as a refreshed recipe does.
    """
    service, sessions = _removal_service(postgres_engine, tmp_path)
    _content_keys(service, monkeypatch)
    try:
        first, set_digest, _ = _revision(
            service, sessions, tmp_path / "files", slug="lineage"
        )
        if verified_again:
            head, again, _ = _revision(
                service, sessions, tmp_path / "files", slug="lineage", note="new"
            )
            assert again == set_digest
        else:
            head = _revise_catalog_only(
                sessions, slug="lineage", data=b"old", note="new"
            )
        assert head != first
        listed = service.model_storage()[head]
        assert listed.unique_bytes > 0

        review = service.review_fleet_model_removal("test/lineage")
        assert review.target_identity == head
        assert [item.sha256 for item in review.assets if item.kind == "model-set"] == [
            set_digest
        ]
        assert review.fleet is not None
        assert review.fleet.nas_reason == FleetRemovalReason.UNUSED
        assert review.nas_reclaimable_bytes == listed.unique_bytes

        result = _removed(service, "test/lineage", review.review_digest)
        assert result.removed_entries == [set_digest]
        assert result.reclaimed_bytes == listed.unique_bytes
        assert not _contains(sessions, set_digest)
        assert head not in service.model_storage()
        assert not service._object_path(hashlib.sha256(b"old").hexdigest()).exists()
    finally:
        service.close()


def test_listed_bytes_kept_for_another_model_are_never_reviewed_as_unused(
    postgres_engine, tmp_path: Path
) -> None:
    """Catches an ``unused`` review that reclaims nothing.

    Every object of the model is also held by another cached model. The review
    names that typed reason and no reclaimable bytes; removal drops only this
    model's cache entry and the other model keeps its bytes.
    """
    service, sessions = _removal_service(postgres_engine, tmp_path)
    try:
        _, removed_set, _ = _revision(service, sessions, tmp_path / "a", slug="aa")
        _, kept_set, _ = _revision(service, sessions, tmp_path / "b", slug="bb")
        assert removed_set != kept_set
        assert service.model_storage()

        review = service.review_fleet_model_removal("test/aa")
        assert review.fleet is not None and review.fleet.nas_remove
        assert review.fleet.nas_reason == FleetRemovalReason.SHARED
        assert review.nas_reclaimable_bytes == 0

        result = _removed(service, "test/aa", review.review_digest)
        assert result.removed_entries == [removed_set]
        assert result.reclaimed_bytes == 0
        assert _contains(sessions, kept_set)
        for data in (b"old", b"shared"):
            path = service._object_path(hashlib.sha256(data).hexdigest())
            assert path.read_bytes() == data
    finally:
        service.close()
