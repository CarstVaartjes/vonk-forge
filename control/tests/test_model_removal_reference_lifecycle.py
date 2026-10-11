"""Real PostgreSQL owner boundaries for model-cache removal references."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import Engine, Table, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.artifact_lifecycle import ArtifactLifecycleGate
from vonk_control.model_cache import (
    CacheOperationView,
    ModelCacheService,
)
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
)
from vonk_forge_contracts import ModelDefinition, document_sha256

from .test_model_cache import _artifact, _canonical_model, _download, _remove_model


@pytest.fixture(autouse=True)
def _freeze_removal_reconcile_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests exercise reference ownership, not the 250 ms work budget.
    # Runner load must not defer a pass beyond the bounded settle loop.
    monkeypatch.setattr(
        "vonk_control.model_cache.removal_reconcile.time",
        SimpleNamespace(monotonic=lambda: 0.0),
    )


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
    digest = document_sha256(model.model_dump(mode="json"))
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
    digest = document_sha256(model.model_dump(mode="json"))
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


def test_model_removal_reference_scan_failure_keeps_bytes_and_same_owner_recovers(
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
        cast(Table, FleetProfileSelection.__table__).drop(postgres_engine)
        cast(Table, FleetProfileApplication.__table__).drop(postgres_engine)
        cast(Table, FleetProfile.__table__).drop(postgres_engine)
        accepted = _remove_model(
            service,
            selector,
            actor="operator",
            request_key=request_key,
            model_content_sha256=digest,
        )
        assert accepted.id
        assert service.advance_removals(limit=1) == 0
        with sessions() as session:
            assert session.get(ModelCacheOperation, accepted.id) is not None
            assert session.get(ModelCacheSet, set_digest) is not None
        assert object_path.read_bytes() == b"abc"

        Base.metadata.create_all(postgres_engine)
        # The same accepted intent resumes automatically when observation heals.
        service._clock = lambda: datetime.now(UTC) + timedelta(minutes=1)
        settled = _settle_removal(service, accepted)
        assert settled.state == "succeeded"
        assert not object_path.exists()
    finally:
        Base.metadata.create_all(postgres_engine)
        service.close()


def test_newer_model_removal_supersedes_an_accepted_download_request(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    """The latest request leads: removal cancels the older queued download."""

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
        removal = _remove_model(
            service,
            selector,
            actor="operator",
            request_key="00000000-0000-4000-8000-000000000113",
            model_content_sha256=digest,
        )
        assert service.get_operation(accepted.id).state in {"cancelling", "cancelled"}
        settled = _settle_removal(service, removal)
        assert settled.state == "succeeded", settled
        assert service.get_operation(accepted.id).state == "cancelled"
        with sessions() as session:
            assert session.get(ModelCacheSet, set_digest) is None
        assert not object_path.exists()
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


def test_sibling_membership_change_after_review_keeps_the_shared_object(
    postgres_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    model_a = _one_model(tmp_path, "shared-review-a")
    model_b = _one_model(tmp_path, "shared-review-b")
    selector_a = _register_model(sessions, model_a)
    _register_model(sessions, model_b)
    _digest_a, _artifact_a, set_a = _seed_model(
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
    object_digest = hashlib.sha256(b"abc").hexdigest()
    object_path = service._object_path(object_digest)
    # Change the sibling after the explicit review. Acceptance owns the new
    # observation; it no longer calls a second implicit client review.
    service.review_model_removal(selector_a)
    with sessions.begin() as session:
        sibling = session.get(ModelCacheSet, set_b)
        assert sibling is not None
        sibling.state = "failed"
    try:
        # The reviewed digest is advisory: the removal applies to the current
        # state, which still shares the object with the changed sibling.
        removing_a = service.remove_model_selector(
            selector_a,
            actor="operator",
            request_key="00000000-0000-4000-8000-000000000127",
        )
        settled = _settle_removal(service, removing_a)
        assert settled.state == "succeeded", settled
        with sessions() as session:
            assert session.get(ModelCacheSet, set_a) is None
            sibling = session.get(ModelCacheSet, set_b)
            assert sibling is not None and sibling.state == "failed"
            assert (
                session.scalar(
                    select(ModelCacheSetArtifact).where(
                        ModelCacheSetArtifact.artifact_set_sha256 == set_b,
                        ModelCacheSetArtifact.artifact_sha256 == object_digest,
                    )
                )
                is not None
            )
            gates = tuple(session.scalars(select(ArtifactLifecycleGate)))
        assert all(gate.removal_owner_id is None for gate in gates)
        assert object_path.read_bytes() == b"abc"
    finally:
        service.close()
