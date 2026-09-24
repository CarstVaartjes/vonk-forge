"""Real PostgreSQL owner boundaries for model-cache removal references."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Engine, Table, and_, or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.artifact_lifecycle import ArtifactLifecycleGate
from vonk_control.cache_removal_review import CacheRemovalReview
from vonk_control.model_cache import (
    CacheOperationView,
    ModelCacheConflict,
    ModelCacheService,
)
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
)
from vonk_forge_contracts import ModelDefinition, content_sha256

from .test_model_cache import _artifact, _canonical_model, _download, _remove_model


def _removal_service(
    postgres_engine: Engine, tmp_path: Path
) -> tuple[ModelCacheService, sessionmaker[Session]]:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    service = ModelCacheService(
        sessions,
        tmp_path / "model-cache",
        reserve_bytes=0,
        fixture_sources=True,
    )
    return service, sessions


def _register_model(sessions: sessionmaker[Session], model: ModelDefinition) -> str:
    now = datetime.now(UTC)
    identity = model.identity
    digest = content_sha256(model)
    with sessions.begin() as session:
        document = CatalogDocument(
            kind="model",
            publisher=identity.publisher,
            slug=identity.slug,
            title=identity.model.title,
            created_by="operator",
            created_at=now,
            updated_at=now,
        )
        session.add(document)
        session.flush()
        session.add(
            CatalogDocumentRevision(
                document_id=document.id,
                kind="model",
                publisher=identity.publisher,
                slug=identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=model.model_dump(mode="json"),
                content_digest=digest,
                projected={},
                created_by="operator",
                created_at=now,
            )
        )
    return f"{identity.publisher}/{identity.slug}"


def _seed_model(
    service: ModelCacheService,
    tmp_path: Path,
    model: ModelDefinition,
    request_key: str,
) -> tuple[str, dict[str, object], str]:
    data = b"abc"
    digest = content_sha256(model)
    artifact = _artifact(
        tmp_path,
        data,
        model_content_sha256=digest,
    )
    operation = _download(
        service,
        [artifact],
        model_content_sha256=digest,
        request_key=request_key,
    )
    assert operation.state == "succeeded", operation.last_error
    assert operation.artifact_set_sha256 is not None
    assert service._object_path(hashlib.sha256(data).hexdigest()).read_bytes() == data
    return digest, artifact, operation.artifact_set_sha256


def _force_model_request(
    service: ModelCacheService,
    *,
    selector: str,
    digest: str,
    artifact: dict[str, object],
    request_key: str,
) -> CacheOperationView:
    preview = service.download_preview(
        model_content_sha256=digest,
        artifacts=[artifact],
    )
    assert preview["blockers"] == []
    return service.start_download(
        actor="operator",
        request_key=request_key,
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=digest,
        artifacts=[artifact],
        force=True,
        selector=selector,
    )


def _settle_removal(
    service: ModelCacheService, operation: CacheOperationView
) -> CacheOperationView:
    for _ in range(16):
        current = service.get_operation(operation.id)
        if current.state in {"succeeded", "failed", "cancelled"}:
            return current
        service.advance_removals(limit=1)
    raise AssertionError("model removal did not settle under its durable owner")


def _one_model(tmp_path: Path, slug: str, data: bytes = b"abc") -> ModelDefinition:
    return _canonical_model(
        publisher="vonk-forge",
        slug=slug,
        file_id="weights",
        file_digest=hashlib.sha256(data).hexdigest(),
    )


def test_model_removal_reference_scan_failure_rolls_back_and_same_key_recovers(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    model = _one_model(tmp_path, "scan-recovery")
    selector = _register_model(sessions, model)
    digest, _artifact_doc, set_digest = _seed_model(
        service,
        tmp_path,
        model,
        "00000000-0000-4000-8000-000000000101",
    )
    object_digest = hashlib.sha256(b"abc").hexdigest()
    request_key = "00000000-0000-4000-8000-000000000102"
    object_path = service._object_path(object_digest)

    try:
        # Remove the actual owner tables queried by the production scanner.
        # The owner must turn this incomplete reference observation into a
        # typed blocker; it may not treat the unavailable scan as an empty set.
        cast(Table, FleetProfileApplication.__table__).drop(postgres_engine)
        cast(Table, FleetProfile.__table__).drop(postgres_engine)
        with pytest.raises(ModelCacheConflict) as unavailable:
            _remove_model(
                service,
                selector,
                actor="operator",
                request_key=request_key,
                model_content_sha256=digest,
            )
        assert unavailable.value.code == "artifact.reference_scan_failed"
        scan_review = service.review_model_removal(selector)
        assert any(
            blocker.code == "artifact.reference_scan_failed"
            and blocker.retryable
            and "retry" in blocker.recovery_actions
            for blocker in scan_review.blockers
        )

        with sessions() as session:
            assert (
                session.scalar(
                    select(ModelCacheOperation).where(
                        ModelCacheOperation.request_key == request_key
                    )
                )
                is None
            )
            assert session.get(ModelCacheSet, set_digest) is not None
            gates = list(
                session.scalars(
                    select(ArtifactLifecycleGate).where(
                        or_(
                            and_(
                                ArtifactLifecycleGate.artifact_kind == "model-set",
                                ArtifactLifecycleGate.artifact_sha256 == set_digest,
                            ),
                            and_(
                                ArtifactLifecycleGate.artifact_kind == "model-object",
                                ArtifactLifecycleGate.artifact_sha256 == object_digest,
                            ),
                        )
                    )
                )
            )
        assert all(gate.removal_owner_id is None for gate in gates)
        assert object_path.read_bytes() == b"abc"

        Base.metadata.create_all(postgres_engine)
        accepted = _remove_model(
            service,
            selector,
            actor="operator",
            request_key=request_key,
            model_content_sha256=digest,
        )
        settled = _settle_removal(service, accepted)
        assert settled.state == "succeeded"
        assert not object_path.exists()
    finally:
        Base.metadata.create_all(postgres_engine)
        service.close()


def test_accepted_model_request_prevents_removal_before_any_bytes_change(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    model = _one_model(tmp_path, "accepted-reference")
    selector = _register_model(sessions, model)
    digest, artifact, set_digest = _seed_model(
        service,
        tmp_path,
        model,
        "00000000-0000-4000-8000-000000000111",
    )
    object_digest = hashlib.sha256(b"abc").hexdigest()
    object_path = service._object_path(object_digest)
    accepted = _force_model_request(
        service,
        selector=selector,
        digest=digest,
        artifact=artifact,
        request_key="00000000-0000-4000-8000-000000000112",
    )
    assert accepted.state == "queued"

    try:
        with pytest.raises(ModelCacheConflict) as refused:
            _remove_model(
                service,
                selector,
                actor="operator",
                request_key="00000000-0000-4000-8000-000000000113",
                model_content_sha256=digest,
            )
        assert refused.value.code == "model_cache.removal_referenced"
        assert service.get_operation(accepted.id).state == "queued"
        with sessions() as session:
            assert session.get(ModelCacheSet, set_digest) is not None
            assert (
                session.scalar(
                    select(ModelCacheOperation).where(
                        ModelCacheOperation.kind == "remove"
                    )
                )
                is None
            )
        assert object_path.read_bytes() == b"abc"
    finally:
        service.close()


def test_removing_one_set_leaves_shared_object_open_for_second_model(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    model_a = _one_model(tmp_path, "shared-a")
    model_b = _one_model(tmp_path, "shared-b")
    selector_a = _register_model(sessions, model_a)
    selector_b = _register_model(sessions, model_b)
    digest_a, _artifact_a, set_a = _seed_model(
        service,
        tmp_path,
        model_a,
        "00000000-0000-4000-8000-000000000121",
    )
    digest_b, artifact_b, set_b = _seed_model(
        service,
        tmp_path,
        model_b,
        "00000000-0000-4000-8000-000000000122",
    )
    assert digest_a != digest_b
    object_digest = hashlib.sha256(b"abc").hexdigest()
    object_path = service._object_path(object_digest)
    with sessions() as session:
        memberships = list(
            session.scalars(
                select(ModelCacheSetArtifact).where(
                    ModelCacheSetArtifact.artifact_set_sha256.in_((set_a, set_b))
                )
            )
        )
        assert {item.artifact_set_sha256 for item in memberships} == {set_a, set_b}
        assert {item.artifact_sha256 for item in memberships} == {object_digest}

        review = service.review_model_removal(selector_a)
        shared_findings = [
            finding
            for finding in review.references
            if finding.asset_kind == "model-object"
            and finding.asset_sha256 == object_digest
        ]
        assert [
            (finding.owner_kind, finding.owner_id, finding.state)
            for finding in shared_findings
        ] == [("model-cache-set-membership", set_b, "cached")]
        assert review.blockers == []
        assert any(
            asset.kind == "model-object"
            and asset.sha256 == object_digest
            and asset.disposition == "retain-shared"
            for asset in review.assets
        )

    try:
        removing_a = service.remove_model_selector(
            selector_a,
            actor="operator",
            request_key="00000000-0000-4000-8000-000000000123",
            model_content_sha256=digest_a,
            review_digest=review.review_digest,
        )
        assert removing_a.state == "queued"

        # This is a new accepted B operation while A's deletion owner is live.
        # A's plan must not fence a shared object it will retain for B.
        b_request = _force_model_request(
            service,
            selector=selector_b,
            digest=digest_b,
            artifact=artifact_b,
            request_key="00000000-0000-4000-8000-000000000124",
        )
        assert b_request.state == "queued"

        settled_a = _settle_removal(service, removing_a)
        assert settled_a.state == "succeeded"
        service.run_pending()
        assert service.get_operation(b_request.id).state == "succeeded"
        with sessions() as session:
            assert session.get(ModelCacheSet, set_a) is None
            assert session.get(ModelCacheSet, set_b) is not None
            assert (
                session.scalar(
                    select(ModelCacheSetArtifact).where(
                        ModelCacheSetArtifact.artifact_set_sha256 == set_b,
                        ModelCacheSetArtifact.artifact_sha256 == object_digest,
                    )
                )
                is not None
            )
        assert object_path.read_bytes() == b"abc"
    finally:
        service.close()


def test_sibling_membership_change_after_review_refuses_model_removal(
    postgres_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    model_a = _one_model(tmp_path, "shared-review-a")
    model_b = _one_model(tmp_path, "shared-review-b")
    selector_a = _register_model(sessions, model_a)
    _register_model(sessions, model_b)
    digest_a, _artifact_a, set_a = _seed_model(
        service,
        tmp_path,
        model_a,
        "00000000-0000-4000-8000-000000000125",
    )
    _digest_b, _artifact_b, set_b = _seed_model(
        service,
        tmp_path,
        model_b,
        "00000000-0000-4000-8000-000000000126",
    )
    review = service.review_model_removal(selector_a)
    object_digest = hashlib.sha256(b"abc").hexdigest()
    object_path = service._object_path(object_digest)
    original_review = service.review_model_removal

    def review_then_change_sibling(selector: str) -> CacheRemovalReview:
        current = original_review(selector)
        with sessions.begin() as session:
            sibling = session.get(ModelCacheSet, set_b)
            assert sibling is not None
            sibling.state = "failed"
        return current

    monkeypatch.setattr(service, "review_model_removal", review_then_change_sibling)
    try:
        with pytest.raises(ModelCacheConflict) as stale:
            service.remove_model_selector(
                selector_a,
                actor="operator",
                request_key="00000000-0000-4000-8000-000000000127",
                model_content_sha256=digest_a,
                review_digest=review.review_digest,
            )
        assert stale.value.code == "model_cache.removal_review_stale"
        with sessions() as session:
            assert session.get(ModelCacheSet, set_a) is not None
            sibling = session.get(ModelCacheSet, set_b)
            assert sibling is not None and sibling.state == "failed"
            assert (
                session.scalar(
                    select(ModelCacheOperation).where(
                        ModelCacheOperation.request_key
                        == "00000000-0000-4000-8000-000000000127"
                    )
                )
                is None
            )
            gates = tuple(session.scalars(select(ArtifactLifecycleGate)))
        assert all(gate.removal_owner_id is None for gate in gates)
        assert object_path.read_bytes() == b"abc"
    finally:
        service.close()
