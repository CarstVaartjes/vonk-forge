"""New accepted preparation wins while old removal effects remain fenced."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import ArtifactLifecycleGate, Base, Job, ModelCacheOperation
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .recipe_removal_review_support import remove_after_review
from .test_model_cache import _remove_model, threaded_cache  # noqa: F401 - fixture
from .test_model_removal_reference_lifecycle import (
    _force_model_request,
    _one_model,
    _register_model,
    _removal_service,
    _seed_model,
)
from .test_recipe_image_availability import (
    ARCHIVE,
    ARCHIVE_SHA,
    _add_head,
    _add_revision,
    _build_id,
    _recipe,
    _reference_receipt,
    _runtime,
    _service,
    place_test_image,
)


def test_accepted_model_request_supersedes_removal_without_crossing_other_lock(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    service, sessions = _removal_service(postgres_engine, tmp_path)
    model = _one_model(tmp_path, "request-led-removal")
    selector = _register_model(sessions, model)
    digest, artifact, set_digest = _seed_model(
        service, tmp_path, model, str(uuid.uuid4())
    )
    removal = _remove_model(
        service,
        selector,
        actor="operator",
        request_key=str(uuid.uuid4()),
        model_content_sha256=digest,
    )
    assert service._observe_model_removal_scope(removal.id)
    accepted = _force_model_request(
        service,
        selector=selector,
        digest=digest,
        artifact=artifact,
        request_key=str(uuid.uuid4()),
    )
    assert accepted.state == "queued"
    assert service.get_operation(removal.id).state == "queued"
    object_digest = hashlib.sha256(b"abc").hexdigest()
    with service._model_storage_lock(set_digest, model_set=True):
        # Cancelling through the independent object lock cannot clear this gate.
        for _ in range(4):
            if service.reconcile_requested_removals():
                break
        assert service.get_operation(removal.id).state == "cancelled"
        with sessions() as session:
            gate = session.get(
                ArtifactLifecycleGate,
                {
                    "artifact_kind": "model-set",
                    "artifact_sha256": set_digest,
                },
            )
            assert gate is not None and gate.removal_owner_id == removal.id
        assert service._claim_operations(limit=1, respect_backoff=False) == []
        assert service._object_path(object_digest).read_bytes() == b"abc"
    assert service.reconcile_removal_gates() == 1
    service.run_pending()
    assert service.get_operation(accepted.id).state == "succeeded"
    assert service._object_path(object_digest).read_bytes() == b"abc"


def test_accepted_image_request_waits_for_exact_lock_and_recovers_after_restart(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-source-build.json")
    revision_id = str(uuid.uuid4())
    with sessions.begin() as session:
        revision = _add_revision(
            session, revision_id, recipe, document_id=str(uuid.uuid4())
        )
        _add_head(session, revision)
    storage = FilesystemRuntimeImageStorage(tmp_path / "controller-artifacts")
    place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
    receipt_file = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    receipt_file.write_text(
        json.dumps(_reference_receipt(_build_id(revision_id)).model_dump(mode="json"))
    )
    now = datetime.now(UTC)
    options = {
        "storage": storage,
        "authority": lambda *_args, **_kwargs: (recipe, _runtime()),
        "clock": lambda: now,
    }
    service = _service(sessions, **options)
    removal = remove_after_review(
        service, recipe.identity.slug, actor="operator", request_id=str(uuid.uuid4())
    )
    assert service._observe_recipe_removal(str(removal["operation_id"]))
    accepted = service.start(
        revision_id, actor="operator", request_id=str(uuid.uuid4()), force=True
    )
    assert accepted.state == "queued"
    assert any(
        item.code == "artifact.deletion_in_progress" for item in accepted.blockers
    )
    recovered = _service(sessions, **options)
    with storage.publication_lock(ARCHIVE_SHA):
        assert recovered.reconcile_requested_removals() == 0
        assert recovered.claim_pending(limit=1) == ()
        with sessions() as session:
            owner = session.scalar(
                select(Job).where(Job.request_id == removal["request_key"])
            )
            assert owner is not None and owner.state == "queued"
        assert receipt_file.exists()
    for _ in range(4):
        if recovered.reconcile_requested_removals():
            break
    with sessions() as session:
        owner = session.scalar(
            select(Job).where(Job.request_id == removal["request_key"])
        )
        assert owner is not None and owner.state == "cancelled"
        gate = session.get(
            ArtifactLifecycleGate,
            {
                "artifact_kind": "runtime-image",
                "artifact_sha256": ARCHIVE_SHA,
            },
        )
        assert gate is not None and gate.removal_owner_id is None
    assert receipt_file.exists()


def test_persisted_model_manifest_recovers_through_removal_gates_after_restart(
    threaded_cache,  # noqa: F811 - imported pytest fixture
    tmp_path: Path,
) -> None:
    service, sessions = threaded_cache
    model = _one_model(tmp_path, "restart-manifest-gate")
    selector = _register_model(sessions, model)
    digest, artifact, set_digest = _seed_model(
        service, tmp_path, model, str(uuid.uuid4())
    )
    removal = _remove_model(
        service,
        selector,
        actor="operator",
        request_key=str(uuid.uuid4()),
        model_content_sha256=digest,
    )
    assert service._observe_model_removal_scope(removal.id)
    accepted = _force_model_request(
        service,
        selector=selector,
        digest=digest,
        artifact=artifact,
        request_key=str(uuid.uuid4()),
    )
    with sessions() as session:
        stored = session.get(ModelCacheOperation, accepted.id)
        assert stored is not None and isinstance(stored.payload, dict)
        assert stored.artifact_set_sha256 == set_digest
        assert stored.plan_digest == accepted.plan_digest
    object_digest = hashlib.sha256(b"abc").hexdigest()
    object_path = service._object_path(object_digest)
    before_inode = object_path.stat().st_ino
    recovered = ModelCacheService(
        sessions, service.root, reserve_bytes=0, fixture_sources=True
    )
    try:
        with recovered._model_storage_lock(set_digest, model_set=True):
            for _ in range(4):
                if recovered.reconcile_requested_removals():
                    break
            assert recovered.get_operation(removal.id).state == "cancelled"
            assert recovered._claim_operations(limit=1, respect_backoff=False) == []
            assert object_path.read_bytes() == b"abc"
            assert object_path.stat().st_ino == before_inode
        assert recovered.reconcile_removal_gates() == 1
        recovered.run_pending()
        done = recovered.get_operation(accepted.id)
        assert done.state == "succeeded"
        assert done.request_key == accepted.request_key
        assert done.plan_digest == accepted.plan_digest
        assert done.artifact_set_sha256 == set_digest
        assert object_path.read_bytes() == b"abc"
    finally:
        recovered.close()
