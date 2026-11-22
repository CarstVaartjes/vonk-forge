from __future__ import annotations

import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import LifecycleState, OperationProgress, SecurityRefusalReason
from vonk_control.artifact_reference_scan import (
    runtime_image_reference_findings,
    runtime_image_reference_reasons,
)
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.jobs.documents import (
    AvailabilityJobPayload,
    AvailabilityModelChild,
)
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_contract import (
    ModelCacheCounters,
    ModelCacheRepairPreviewResponse,
    ModelCacheSetState,
)
from vonk_control.model_cache_progress import cache_progress, progress_document
from vonk_control.models import (
    Base,
    Job,
    User,
)
from vonk_control.operations.blockers import make_blocker
from vonk_control.operations.contract import (
    AvailabilityOperationFailure,
    AvailabilityRecoveryAction,
)
from vonk_control.persisted_waits import schedule_wait
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
    RecipeImageAvailabilityUnknown,
)
from vonk_control.recipe_image_availability_api import (
    _view_document,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationError,
    RuntimeImageReferenceIntent,
    read_runtime_image_reference_intent,
)
from vonk_control.strict_json import serialize_json_value

from .non_blocking import assert_ended_without_blocking
from .recipe_image_availability_support import (
    ARCHIVE,
    ARCHIVE_SHA,
    IMAGE_DIGEST,
    Transport,
    _add_head,
    _add_revision,
    _availability_payload,
    _build_runtime,
    _builder,
    _download_preview,
    _manifest,
    _persist_fake_model_cache_child,
    _progress_members,
    _recipe,
    _reference_receipt,
    _runtime,
    _service,
    _set_active_head,
    _successor,
)


def _repair_preview(*, artifact_set_sha256: str, plan_digest: str):
    return ModelCacheRepairPreviewResponse(
        artifact_set_sha256=artifact_set_sha256,
        plan_digest=plan_digest,
        artifact_count=0,
        current_state=ModelCacheSetState.FAILED,
        expected_bytes=0,
        verified_bytes=0,
    ).model_dump(mode="json")


def test_recipe_retry_uses_model_access_recheck_for_terminal_auth(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    failed = SimpleNamespace(
        blockers=(),
        id="00000000-0000-4000-8000-000000000501",
        request_key="00000000-0000-4000-8000-000000000502",
        state="failed",
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters.model_validate(
                    {
                        "phase": "downloading",
                        "downloaded_bytes": 4,
                        "expected_bytes": 10,
                        "completed_artifacts": 0,
                        "total_artifacts": 1,
                    }
                ),
                previous=None,
                now=datetime.now(UTC),
            )
        ),
        failure={
            "code": "access_denied",
            "detail": "HF access denied",
            "recovery_actions": ["open_model_access", "check_access_and_resume"],
            "retryable": False,
            "retry_time": None,
            "retry_after_seconds": None,
            "log_excerpt": "denied",
            "required_bytes": None,
            "free_bytes": None,
            "shortfall_bytes": None,
        },
    )

    class ModelCache:
        def __init__(self) -> None:
            self.called: dict[str, object] | None = None

        def get_operation(self, _operation_id: str) -> SimpleNamespace:
            return failed

        def check_access_and_resume(
            self, operation_id: str, **kwargs: object
        ) -> SimpleNamespace:
            self.called = {"operation_id": operation_id, **kwargs}
            return failed

        def reusable_operation(self, **_: object) -> None:
            return None

    cache = ModelCache()
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            _recipe("recipe-source-build.json"),
            _runtime(),
        ),
        model_cache=cache,
        clock=lambda: datetime.now(UTC),
    )
    service._resume_model_child(
        AvailabilityModelChild.model_validate_json(
            json.dumps({"id": failed.id, "state": "failed", "failure": failed.failure})
        ),
        actor="operator",
        parent_request_key="p" * 36,
    )
    assert cache.called is not None
    assert cache.called["artifact_set_sha256"] == "c" * 64
    assert cache.called["plan_digest"] == "d" * 64


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    ("child_code", "repairs"),
    [
        ("integrity_mismatch", True),
        # The ingress digest edge ended the download: no repair is started.
        (SecurityRefusalReason.DIGEST_MISMATCH.value, False),
    ],
)
def test_automatic_reconciliation_repairs_model_child_and_reuses_image(
    tmp_path: Path, child_code: str, repairs: bool
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-integrity-repair", recipe)

    failed = SimpleNamespace(
        blockers=(),
        id="00000000-0000-4000-8000-000000000301",
        request_key="00000000-0000-4000-8000-000000000302",
        state="running",
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters.model_validate(
                    {
                        "phase": "downloading",
                        "downloaded_bytes": 4,
                        "expected_bytes": 10,
                        "completed_artifacts": 0,
                        "total_artifacts": 1,
                    }
                ),
                previous=None,
                now=datetime.now(UTC),
            )
        ),
        failure=None,
    )
    repaired = SimpleNamespace(
        blockers=(),
        id="00000000-0000-4000-8000-000000000303",
        request_key="00000000-0000-4000-8000-000000000304",
        state="succeeded",
        artifact_set_sha256="c" * 64,
        plan_digest="e" * 64,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters.model_validate(
                    {
                        "phase": "downloading",
                        "downloaded_bytes": 10,
                        "expected_bytes": 10,
                        "completed_artifacts": 0,
                        "total_artifacts": 1,
                    }
                ),
                previous=None,
                now=datetime.now(UTC),
            )
        ),
        failure=None,
    )
    now = datetime.now(UTC)
    model_content_sha256 = next(model.model.content_sha256 for model in recipe.models)
    _persist_fake_model_cache_child(
        sessions,
        child=failed,
        model_content_sha256=model_content_sha256,
        now=now,
    )

    class ModelCache:
        def __init__(self) -> None:
            self.failed = False
            self.repair_calls: list[dict[str, object]] = []

        def download_preview(self, **_: object) -> dict[str, object]:
            return _download_preview(
                plan_digest="d" * 64, artifact_set_sha256="c" * 64, new_bytes=0
            )

        def resolve_artifact_set(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                blockers=(),
                digest="c" * 64,
                document=lambda: _manifest(model_content_digests=["d" * 64]),
            )

        def reusable_operation(self, **_: object) -> object:
            return repaired if self.repair_calls else None if self.failed else failed

        def start_download(self, **_: object) -> SimpleNamespace:
            raise AssertionError("the existing ModelCache child should be reused")

        def get_operation(self, operation_id: str) -> SimpleNamespace:
            if operation_id == repaired.id:
                return repaired
            if self.failed:
                failed.state = "failed"
                failed.failure = {
                    "code": child_code,
                    "detail": "downloaded bytes did not match the pinned digest",
                    "recovery_actions": ["download_again"],
                    "retryable": False,
                    "retry_time": None,
                    "retry_after_seconds": None,
                    "log_excerpt": "digest mismatch",
                    "required_bytes": 10,
                    "free_bytes": 100,
                    "shortfall_bytes": 0,
                }
                _persist_fake_model_cache_child(
                    sessions,
                    child=failed,
                    model_content_sha256=model_content_sha256,
                    now=datetime.now(UTC),
                )
            return failed

        def repair_preview(self, artifact_set_sha256: str) -> dict[str, object]:
            assert artifact_set_sha256 == "c" * 64
            return _repair_preview(artifact_set_sha256="c" * 64, plan_digest="e" * 64)

        def start_repair(self, **kwargs: object) -> SimpleNamespace:
            self.repair_calls.append(kwargs)
            _persist_fake_model_cache_child(
                sessions,
                child=repaired,
                model_content_sha256=model_content_sha256,
                now=datetime.now(UTC),
            )
            return repaired

    model_cache = ModelCache()
    transport = Transport()
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        model_cache=model_cache,
        clock=lambda: now,
    )
    parent = service.start(
        "revision-integrity-repair",
        actor="operator",
        request_id="i" * 36,
    )
    assert service.run_pending() == 1
    partial = service.get(parent.id)
    assert partial.state == LifecycleState.BACKOFF
    assert transport.calls == 1

    model_cache.failed = True
    with sessions.begin() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        schedule_wait(row, datetime(2000, 1, 1, tzinfo=UTC))
    assert service.run_pending() == 1
    if not repairs:
        assert model_cache.repair_calls == []
        assert service.get(parent.id).state == LifecycleState.FAILED
        return
    assert len(model_cache.repair_calls) == 1
    assert model_cache.repair_calls[0]["artifact_set_sha256"] == "c" * 64

    completed = service.get(parent.id)
    assert completed.state == "succeeded"
    assert completed.result is not None
    assert (
        require_mapping(completed.result["model_child"], "model child")["id"]
        == repaired.id
    )
    assert transport.calls == 1

    fresh = service.start(
        "revision-integrity-repair", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None
    assert transport.calls == 1


def test_accepted_cancellation_is_idempotent_and_prevents_queued_dispatch(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(f"sqlite:///{tmp_path / 'cancel-queued.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "cancel-queued-revision", recipe)
        session.add(User(subject="operator", role="operator"))
    transport = Transport()
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "image-cache"),
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=transport,
        clock=lambda: datetime.now(UTC),
    )
    operation = service.start(
        "cancel-queued-revision", actor="operator", request_id="cancel-queued"
    )
    cancel_key = "00000000-0000-4000-8000-000000000901"

    accepted = service.cancel(
        operation.id,
        actor="operator",
        request_id=cancel_key,
        reason="stop queued preparation",
    )
    # Rule 4: a preparation that never ran and holds nothing ends at once.
    assert accepted.state == "cancelled"
    assert accepted.cancellation is not None
    replay = service.cancel(
        operation.id,
        actor="operator",
        request_id=cancel_key,
        reason="stop queued preparation",
    )
    assert replay.cancellation == accepted.cancellation
    with pytest.raises(RecipeImageAvailabilityError):
        service.cancel(
            operation.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000902",
            reason="another cancellation intent",
        )

    assert service.run_pending() == 0
    assert service.get(operation.id).state == "cancelled"
    assert transport.calls == 0
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        service.get(operation.id),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "cancel-queued-revision",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )
    engine.dispose()


def test_late_verified_image_result_cannot_publish_after_cancellation(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'cancel-late.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "cancel-late-revision", recipe)
        session.add(User(subject="operator", role="operator"))
    now = datetime.now(UTC)
    entered = threading.Event()
    release = threading.Event()

    class BlockingTransport(Transport):
        def inspect_archive(self, archive: Path, **kwargs):
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test transport was not released")
            return super().inspect_archive(archive, **kwargs)

    transport = BlockingTransport()
    storage = FilesystemRuntimeImageStorage(tmp_path / "image-cache")
    service = _service(
        sessions,
        storage=storage,
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=transport,
        clock=lambda: now,
        claim_lease_seconds=30,
    )
    operation = service.start(
        "cancel-late-revision", actor="operator", request_id="cancel-late"
    )
    claim = service.claim_pending(owner_id="late-image-result")[0]
    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(service.run_claim, claim)
        assert entered.wait(timeout=30), "worker did not reach the issued image pull"
        accepted = service.cancel(
            operation.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000903",
            reason="stop image preparation",
        )
        assert accepted.state == LifecycleState.OBSERVING
        service.reconcile_cancellations()
        assert service.get(operation.id).state == LifecycleState.OBSERVING
        release.set()
        worker.result(timeout=10)

    observed = service.get(operation.id)
    assert observed.state == "cancelled"
    assert observed.result is None
    assert observed.cancellation is not None
    retained = storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE))
    assert retained.is_file()
    with sessions() as session:
        row = session.get(Job, operation.id)
        assert row is not None and row.state == "cancelled"
        assert "image_result" not in row.payload
        assert "image_reference_intent" not in row.payload
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        service.get(operation.id),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "cancel-late-revision",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )
    engine.dispose()


@pytest.mark.usefixtures("damaged_json_rows")
def test_cancelling_image_reference_intent_is_counted_until_claim_release(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'cancel-reference.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        revision = _add_revision(
            session, "cancel-reference-revision", recipe, built=False
        )
        revision_id = revision.id
        session.add(User(subject="operator", role="operator"))
    now = datetime.now(UTC)
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "image-cache"),
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: now,
    )
    operation = service.start(
        revision_id, actor="operator", request_id="cancel-reference"
    )
    claim = service.claim_pending(owner_id="cancel-reference-writer")[0]
    accepted = service.cancel(
        operation.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000904",
        reason="stop image preparation",
    )
    assert accepted.state == LifecycleState.OBSERVING

    service._persist_provisional_image_reference(
        claim,
        receipt=_reference_receipt(),
    )
    with sessions() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        reference = read_runtime_image_reference_intent(
            row.payload["image_reference_intent"]
        )
        assert reference is not None
        assert reference == RuntimeImageReferenceIntent(
            schema_version=2,
            operation_id=operation.id,
            recipe_revision_id=revision_id,
            attempt=claim.execution_attempt,
            claim_owner=claim.claim_owner,
            oci_archive_sha256=ARCHIVE_SHA,
            image_digest=IMAGE_DIGEST,
            image_bytes=len(ARCHIVE),
        )
        assert runtime_image_reference_reasons(session, [ARCHIVE_SHA]) == {
            ARCHIVE_SHA: (f"image publication operation {operation.id}",)
        }
        findings = runtime_image_reference_findings(session, [ARCHIVE_SHA])
        assert [
            (
                item.asset.kind,
                item.asset.sha256,
                item.owner_kind,
                item.owner_id,
                item.state,
                item.classification,
                item.reason,
            )
            for item in findings[ARCHIVE_SHA]
        ] == [
            (
                "runtime-image",
                ARCHIVE_SHA,
                "recipe-image-availability-operation",
                operation.id,
                LifecycleState.OBSERVING.value,
                "active-work",
                f"image publication operation {operation.id}",
            )
        ]
        persisted_reference = reference.model_dump(mode="json")

    with sessions.begin() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        payload = dict(row.payload)
        payload["image_reference_intent"] = persisted_reference | {
            "unrecognized": "must be rejected"
        }
        row.payload = payload
    service._persist_provisional_image_reference(claim, receipt=_reference_receipt())
    with sessions() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        assert "unrecognized" not in require_mapping(
            row.payload["image_reference_intent"], "reference"
        )
        assert runtime_image_reference_reasons(session, [ARCHIVE_SHA])[ARCHIVE_SHA] == (
            f"image publication operation {operation.id}",
        )
    with sessions.begin() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        payload = dict(row.payload)
        payload["image_reference_intent"] = persisted_reference
        row.payload = payload

    with service._storage.publication_lock(ARCHIVE_SHA):
        service.reconcile_cancellations()
        assert service.get(operation.id).state == LifecycleState.OBSERVING
        with sessions() as session:
            row = session.get(Job, operation.id)
            assert row is not None
            assert row.payload["claim_owner"] == claim.claim_owner
            assert "image_reference_intent" in row.payload

    assert service._release_cancelled_claim(claim)
    assert service._reconcile_availability_cancellation(operation.id)
    assert service.get(operation.id).state == "cancelled"
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        service.get(operation.id),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            revision_id,
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )
    engine.dispose()


@pytest.mark.parametrize("mutation", ["stale-owner", "removal-fence"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_cancelling_reference_intent_rejects_stale_or_fenced_claim(
    tmp_path: Path, mutation: str
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / f'cancel-reference-{mutation}.sqlite'}"
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        revision = _add_revision(session, f"cancel-reference-{mutation}", recipe)
        revision_id = revision.id
        session.add(User(subject="operator", role="operator"))
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "image-cache"),
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
    )
    operation = service.start(
        revision_id, actor="operator", request_id=f"cancel-reference-{mutation}"
    )
    claim = service.claim_pending(owner_id="cancel-reference-writer")[0]
    accepted = service.cancel(
        operation.id,
        actor="operator",
        request_id=(
            "00000000-0000-4000-8000-000000000905"
            if mutation == "stale-owner"
            else "00000000-0000-4000-8000-000000000906"
        ),
        reason="stop image preparation",
    )
    assert accepted.state == LifecycleState.OBSERVING
    with sessions.begin() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        payload = dict(row.payload)
        if mutation == "stale-owner":
            payload["claim_owner"] = "replacement-writer"
        else:
            payload["removal_fence"] = "reserved-for-removal"
        row.payload = payload

    with pytest.raises(RuntimeImagePreparationError):
        service._persist_provisional_image_reference(
            claim,
            receipt=_reference_receipt(),
        )
    with sessions() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        assert "image_reference_intent" not in row.payload
    from vonk_control.lifecycle.image_availability import CANCEL_BUDGET

    elapsed = service._clock() + CANCEL_BUDGET + timedelta(seconds=1)
    service._clock = lambda: elapsed
    service.reconcile_cancellations()
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        service.get(operation.id),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            revision_id,
            actor="operator",
            request_id=f"fresh-fenced-cancel-{mutation}",
        ),
    )
    engine.dispose()


@pytest.mark.usefixtures("damaged_json_rows")
def test_cancelling_one_parent_preserves_a_shared_partial_model_transfer(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'cancel-shared-model.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    revision_id = "cancel-shared-revision"
    independent_recipe = recipe.model_copy(
        update={
            "identity": recipe.identity.model_copy(
                update={"slug": "independent-model-consumer"}
            )
        }
    )
    with sessions.begin() as session:
        _add_revision(session, revision_id, recipe)
        _add_revision(session, "independent-model-consumer", independent_recipe)
        session.add(User(subject="operator", role="operator"))
    cache = ModelCacheService(
        sessions,
        tmp_path / "model-cache",
        free_fraction=0,
        fixture_sources=True,
    )
    source = tmp_path / "weights.source"
    source.write_bytes(b"shared model weights")
    model_digest = "a" * 64
    artifact = {
        "id": "weights",
        "path": "weights.bin",
        "kind": "file",
        "source": source.as_uri(),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "download_bytes": source.stat().st_size,
        "roles": ["weights"],
        "model_content_sha256": model_digest,
    }
    parent_request_id = "first-model-consumer"
    child_request_key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:recipe-availability-model:{revision_id}:{parent_request_id}",
        )
    )
    child = cache.start_download(
        actor="operator",
        request_key=child_request_key,
        model_content_sha256=model_digest,
        artifacts=[artifact],
        interrupt_after_bytes=1,
    )
    assert child.state == LifecycleState.BACKOFF
    before_bytes = child.progress["downloaded_bytes"]

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "image-cache"),
        authority=lambda recipe_revision_id, **_: (
            independent_recipe
            if recipe_revision_id == "independent-model-consumer"
            else recipe,
            _runtime(),
        ),
        transport=Transport(),
        model_cache=cache,
        clock=lambda: datetime.now(UTC),
    )
    first = service.start(revision_id, actor="operator", request_id=parent_request_id)
    second = service.start(
        "independent-model-consumer",
        actor="operator",
        request_id="second-model-consumer",
    )
    with sessions.begin() as session:
        for operation_id in (first.id, second.id):
            row = session.get(Job, operation_id)
            assert row is not None
            row.payload = dict(row.payload) | {
                "model_child": {
                    "id": child.id,
                    "artifact_set_sha256": child.artifact_set_sha256,
                }
            }

    service.cancel(
        first.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000904",
        reason="stop one preparation",
    )
    service.reconcile_cancellations()

    assert service.get(first.id).state == "cancelled"
    assert service.get(second.id).state == "queued"
    still_shared = cache.get_operation(child.id)
    assert still_shared.state == LifecycleState.BACKOFF
    assert still_shared.progress["downloaded_bytes"] == before_bytes
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        service.get(first.id),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            revision_id,
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )
    cache.close()
    engine.dispose()


def test_forced_rebuild_is_a_distinct_operation_for_same_revision(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-force", recipe)
    transport = Transport()
    storage = FilesystemRuntimeImageStorage(tmp_path)
    builds: list[bool] = []
    service = _service(
        sessions,
        storage=storage,
        builder=_builder(storage, calls=builds),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        clock=lambda: datetime.now(UTC),
    )
    cached = service.start("revision-force", actor="operator", request_id="a" * 36)
    forced = service.start(
        "revision-force", actor="operator", request_id="b" * 36, force=True
    )
    assert forced.id != cached.id
    for claim in service.claim_pending(limit=2, owner_id="worker-a"):
        service.run_claim(claim)
    assert service.get(cached.id).state == LifecycleState.CANCELLED
    assert service.get(forced.id).state == LifecycleState.SUCCEEDED
    assert builds == [True]
    fresh = service.start(
        "revision-force", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None


@pytest.mark.parametrize("model_state", ["running", "failed"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_parent_progress_retains_ready_image_while_model_is_incomplete(
    tmp_path: Path, model_state: str
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: now,
    )
    payload = _availability_payload("revision-progress").model_copy(
        update={
            "progress": OperationProgress(
                phase="available",
                completed_bytes=20,
                total_bytes=20,
                total_bytes_known=True,
            ),
            "image_result": _reference_receipt().model_copy(update={"image_bytes": 20}),
            "model_child": AvailabilityModelChild.model_validate_json(
                json.dumps(
                    {
                        "id": "00000000-0000-4000-8000-000000000701",
                        "state": model_state,
                        "model_content_digests": ["d" * 64],
                        "progress": OperationProgress(
                            phase="download",
                            completed_bytes=40,
                            total_bytes=100,
                            total_bytes_known=True,
                        ).model_dump(mode="json"),
                    }
                )
            ),
        }
    )
    if model_state == "failed":
        payload = payload.model_copy(
            update={
                "failure": AvailabilityOperationFailure(
                    code="recipe_image.model_cache_failed",
                    detail="model download failed",
                    retryable=True,
                    recovery_actions=[AvailabilityRecoveryAction.RETRY],
                )
            }
        )
    operation = Job(
        id="availability-progress",
        request_id="p" * 36,
        kind="recipe.image.availability.v2",
        state="failed" if model_state == "failed" else "backoff",
        actor="operator",
        authority_revision="revision-progress",
        targets=["revision-progress"],
        payload_digest="a" * 64,
        payload=serialize_json_value(payload),
        result=None,
        current_attempt=1,
        created_at=now,
        updated_at=now,
    )
    with sessions.begin() as session:
        session.add(operation)
    view = service.get("availability-progress")
    assert view.progress is not None
    assert view.progress["completed_bytes"] == 60
    assert view.progress["total_bytes"] == 120
    members = {
        member["member_id"]: member
        for member in _progress_members(view.progress["members"])
    }
    assert members["model-cache"]["completed_bytes"] == 40
    assert members["model-cache"]["total_bytes"] == 100
    assert members["runtime-image"]["completed_bytes"] == 20
    assert members["runtime-image"]["total_bytes"] == 20
    assert members["runtime-image"]["state"] == "succeeded"
    response = _view_document(view)
    children = {child.kind: child for child in response.children}
    assert children["runtime-image"].state == "succeeded"
    assert children["runtime-image"].failure is None
    assert children["model-cache"].state == model_state
    assert view.image_progress is not None
    assert view.image_progress["total_bytes"] == 20
    rows, total, cursor = service.list_page(limit=1)
    assert total == 1
    assert len(rows) == 1
    assert cursor is None


def test_image_preparation_retries_with_capped_backoff_until_it_succeeds(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "revision-flaky", recipe))
    now = [datetime(2026, 9, 6, 12, tzinfo=UTC)]
    failures = 4

    class FlakyTransport(Transport):
        def inspect_archive(self, archive, **kwargs):
            if self.calls < failures * 3:
                self.calls += 1
                raise RuntimeError("registry says permission denied, digest unknown")
            return super().inspect_archive(archive, **kwargs)

    transport = FlakyTransport()
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        transport=transport,
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: now[0],
    )
    accepted = service.start_selector(
        recipe.identity.slug, actor="operator", request_id="2" * 36
    )
    delays = []
    for _ in range(failures):
        assert service.run_pending() == 1
        waiting = service.get(accepted.id)
        assert waiting.state == "queued"
        assert waiting.failure is not None and waiting.failure["retryable"] is True
        delays.append(waiting.failure["retry_after_seconds"])
        next_attempt = service.get(accepted.id).next_attempt_at
        assert next_attempt is not None
        now[0] = datetime.fromisoformat(next_attempt)
    # The core's one bounded, jittered clock: it grows, never past its cap.
    assert min(delays) >= 1 and max(delays) <= 90
    assert delays[-1] > delays[0]
    assert max(delays) <= 900
    assert service.run_pending() == 1
    assert service.get(accepted.id).state == "succeeded"


def test_archive_integrity_failure_builds_the_image_again(tmp_path: Path) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "revision-integrity", recipe))
    now = [datetime(2026, 9, 6, 12, tzinfo=UTC)]
    transport = Transport()
    storage = FilesystemRuntimeImageStorage(tmp_path)
    original = storage.existing_archive
    corrupt = [True]

    def verify_once(archive_sha256: str, expected_bytes: int) -> Path:
        if corrupt[0]:
            corrupt[0] = False
            raise RuntimeImagePreparationError(
                "runtime_image.archive_mismatch", "stored archive failed verification"
            )
        return original(archive_sha256, expected_bytes)

    builds: list[bool] = []
    service = _service(
        sessions,
        storage=storage,
        builder=_builder(storage, calls=builds),
        transport=transport,
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: now[0],
    )
    first = service.start_selector(
        recipe.identity.slug, actor="operator", request_id="3" * 36
    )
    service.run_pending()
    assert service.get(first.id).state == "succeeded"
    storage.existing_archive = verify_once  # type: ignore[method-assign]
    again = service.start_selector(
        recipe.identity.slug, actor="operator", request_id="4" * 36
    )
    for _ in range(5):
        if service.get(again.id).state == "succeeded":
            break
        service.run_pending()
        waiting = service.get(again.id)
        if waiting.state != LifecycleState.SUCCEEDED:
            assert waiting.next_attempt_at is not None
            now[0] = datetime.fromisoformat(waiting.next_attempt_at)
    assert service.get(again.id).state == "succeeded"
    # The mismatch was observed and the image was built again, not reused.
    assert corrupt == [False]
    assert builds[-1] is True
    assert storage.read_receipt(ARCHIVE_SHA).oci_archive_sha256 == ARCHIVE_SHA


def test_a_succeeded_preparation_the_plan_still_misses_is_asked_again(
    tmp_path: Path,
) -> None:
    """A load asks only for what its plan lacks, so an earlier success is stale.

    Replaying the first success (the image was removed afterwards) would park
    the load for ever; the next ask starts a fresh preparation, paced.
    """

    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "revision-evicted", recipe))
    now = [datetime(2026, 9, 6, 12, tzinfo=UTC)]
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        transport=Transport(),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: now[0],
    )

    def preparations() -> list[Job]:
        with sessions() as session:
            return list(
                session.scalars(
                    select(Job).where(Job.kind == "recipe.image.availability.v2")
                )
            )

    service.ensure_preparation("revision-evicted", actor="operator")
    for _ in range(5):
        if service.run_pending() == 0:
            break
        now[0] += timedelta(seconds=1)
    assert [job.state for job in preparations()] == ["succeeded"]

    # Just finished: say so, do not spin another one.
    blockers = service.ensure_preparation("revision-evicted", actor="operator")
    assert [item.severity for item in blockers] == ["info"]
    assert len(preparations()) == 1

    # Still missing after the pause: a new preparation is requested.
    now[0] += timedelta(minutes=3)
    blockers = service.ensure_preparation("revision-evicted", actor="operator")
    assert not any(item.severity == "error" for item in blockers)
    assert len(preparations()) == 2


def test_newer_revision_is_prepared_at_once_after_the_older_build_failed(
    tmp_path: Path,
) -> None:
    """The quiet period after a failed build only guards the SAME revision."""

    recipe = _recipe("recipe-source-build.json")
    successor = _successor(recipe, "Successor recipe revision")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        older = _add_revision(session, "revision-older", recipe, built=False)
        newer = _add_revision(session, "revision-newer", successor, built=False)
        newer.document_id = older.document_id
        newer.revision_number = 2
        _add_head(session, older)

    def builder(*_: object, **__: object) -> dict[str, object]:
        raise RecipeImageAvailabilityError(
            "recipe_image.build_failed",
            "compiler failed at step 4",
            retryable=False,
            recovery_actions=("inspect",),
        )

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            recipe if recipe_revision_id == "revision-older" else successor,
            _build_runtime(),
        ),
        builder=builder,
        clock=lambda: datetime.now(UTC),
    )
    service.ensure_preparation("revision-older", actor="operator")
    assert service.run_pending() == 1
    blockers = service.ensure_preparation("revision-older", actor="operator")
    assert blockers
    with sessions() as session:
        prior = session.scalar(
            select(Job).where(Job.authority_revision == "revision-older")
        )
        assert prior is not None
        assert prior.next_action_at is not None
        assert prior.payload.get("claim_owner") is None

    # The recipe syncs a newer revision; no waiting for the retry pause.
    with sessions.begin() as session:
        _set_active_head(session, "revision-newer")
    blockers = service.ensure_preparation("revision-newer", actor="operator")

    assert not any(item.severity == "error" for item in blockers)
    with sessions() as session:
        started = tuple(
            session.scalars(
                select(Job).where(Job.kind == "recipe.image.availability.v2")
            )
        )
    assert any(
        job.payload.get("recipe_revision_id") == "revision-newer"
        and job.state in {"queued", "running"}
        for job in started
    ), [(job.kind, job.state, job.payload) for job in started]


def _parked_operation(
    operation_id: str, *, now: datetime, model_child: bool, updated: datetime
) -> Job:
    payload = _availability_payload(f"revision-{operation_id}")
    if model_child:
        payload = payload.model_copy(
            update={
                "model_child": AvailabilityModelChild(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id)),
                    state=LifecycleState.RUNNING,
                ),
                "image_result": _reference_receipt(),
            }
        )
    return Job(
        id=operation_id,
        request_id=operation_id.ljust(36, "x"),
        kind="recipe.image.availability.v2",
        state=LifecycleState.BACKOFF.value if model_child else "queued",
        actor="operator",
        authority_revision=f"revision-{operation_id}",
        targets=[f"revision-{operation_id}"],
        payload_digest="a" * 64,
        payload=serialize_json_value(payload),
        result=None,
        current_attempt=1 if model_child else 0,
        created_at=now,
        updated_at=updated,
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_operations_waiting_only_on_a_model_download_take_no_worker_slot(
    tmp_path: Path,
) -> None:
    """Ready work is claimed first; model waiters are parked with a named reason."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime(2026, 10, 2, 1, 10, tzinfo=UTC)
    with sessions.begin() as session:
        # Many waiters are older than the ready operation, as in production.
        for index in range(40):
            session.add(
                _parked_operation(
                    f"waiter{index:03d}",
                    now=now,
                    model_child=True,
                    updated=now - timedelta(minutes=30 - index // 2),
                )
            )
        session.add(_parked_operation("ready", now=now, model_child=False, updated=now))

    class ModelCache:
        def get_operation(self, operation_id: str) -> SimpleNamespace:
            return SimpleNamespace(state="running", blockers=())

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (None, {}),
        model_cache=ModelCache(),
        clock=lambda: now,
    )
    claims = service.claim_pending(limit=4)
    assert [claim.operation_id for claim in claims] == ["ready"]
    with sessions() as session:
        waiter = session.get(Job, "waiter000")
        assert waiter is not None
        assert waiter.state == LifecycleState.BACKOFF
        assert waiter.current_attempt == 1
        blockers = require_sequence(waiter.payload["blockers"], "blockers")
        assert [require_mapping(item, "blocker")["code"] for item in blockers] == [
            "recipe_image.waiting_for_model"
        ]
    # Parked waiters are not re-examined until their poll is due.
    assert service.claim_pending(limit=4) == ()


def test_parked_operation_carries_the_model_childs_typed_wait(tmp_path: Path) -> None:
    """A gated model's access wait reaches the parent operation."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = datetime(2026, 10, 2, 1, 10, tzinfo=UTC)
    url = "https://huggingface.co/org/gated"
    with sessions.begin() as session:
        waiter = _parked_operation("gated", now=now, model_child=True, updated=now)
        session.add(waiter)

    class ModelCache:
        def get_operation(self, operation_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                state="backoff",
                blockers=(
                    make_blocker(
                        "access_denied",
                        f"Hugging Face access is required: accept the terms at {url}",
                    ),
                ),
            )

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (None, {}),
        model_cache=ModelCache(),
        clock=lambda: now,
    )
    service.claim_pending(limit=4)
    with sessions() as session:
        parked = session.get(Job, "gated")
        assert parked is not None
        blockers = require_sequence(parked.payload["blockers"], "blockers")
        assert [require_mapping(item, "blocker")["code"] for item in blockers] == [
            "access_denied"
        ]
        assert url in str(require_mapping(blockers[0], "blocker")["detail"])


def test_live_download_survives_a_parent_observation_gap_and_resumes(
    tmp_path: Path,
    postgres_engine,
) -> None:
    """A stale parent checkpoint cannot cancel a live child after worker restart."""
    from dataclasses import dataclass

    from vonk_control.lifecycle.image_availability import PREPARATION_STALL_WINDOW

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    now = [datetime.now(UTC)]
    parent_id = str(uuid.uuid4())
    child_id = str(uuid.uuid5(uuid.NAMESPACE_URL, parent_id))
    old = now[0] - PREPARATION_STALL_WINDOW * 2
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
        session.add(
            _parked_operation(parent_id, now=old, model_child=True, updated=old)
        )

    @dataclass
    class ModelOwner:
        state: LifecycleState = LifecycleState.RUNNING
        blockers: tuple[()] = ()

        def get_operation(self, operation_id: str) -> ModelOwner:
            assert operation_id == child_id
            return self

    model_owner = ModelOwner()

    def reopen() -> RecipeImageAvailabilityService:
        return _service(
            sessions,
            storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
            authority=lambda *_args, **_kwargs: (None, {}),
            model_cache=model_owner,
            clock=lambda: now[0],
        )

    service = reopen()
    assert service.claim_pending() == ()
    with sessions() as session:
        parent = session.get(Job, parent_id)
        assert parent is not None
        assert parent.state == LifecycleState.BACKOFF
        payload = service._payload(parent)
        assert isinstance(payload, AvailabilityJobPayload)
        assert payload.cancellation is None
    # Another scheduler gap must reconnect to the same persisted parent/child,
    # and completion of the child makes the parent claimable again.
    now[0] += PREPARATION_STALL_WINDOW * 2
    service = reopen()
    assert service.claim_pending() == ()
    model_owner.state = LifecycleState.SUCCEEDED
    now[0] += timedelta(minutes=1)
    (claim,) = service.claim_pending()
    assert claim.operation_id == parent_id
    with sessions() as session:
        parent = session.get(Job, parent_id)
        assert parent is not None
        assert parent.state == LifecycleState.RUNNING
        payload = service._payload(parent)
        assert isinstance(payload, AvailabilityJobPayload)
        assert payload.cancellation is None


@pytest.mark.parametrize("checkpointed", [False, True])
def test_preparation_expiry_preserves_download_but_explicit_cancel_still_owns_it(
    tmp_path: Path, postgres_engine, checkpointed: bool
) -> None:
    from vonk_agent_protocol import RecipeImageCode, WaitReason
    from vonk_control.recipe_lifecycle_contract import RecipeOperationCancellationResult

    from .test_model_removal_worker_postgres import _prepare_removal_target

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    cache = ModelCacheService(
        sessions, tmp_path / "models", free_fraction=0, fixture_sources=True
    )
    try:
        selector, _, _ = _prepare_removal_target(
            cache, sessions, tmp_path, slug="observer-budget", data=b"abc"
        )
        revision_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        parent_id = str(uuid.uuid4())
        child_request = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"vonk:recipe-availability-model:{revision_id}:{request_id}",
            )
        )
        child = cache.download_model_selector(
            selector, actor="operator", request_key=child_request
        )
        assert child.state == LifecycleState.QUEUED
        payload = _availability_payload(revision_id).model_copy(
            update={
                "model_child": (
                    AvailabilityModelChild(
                        id=child.id,
                        state=LifecycleState.QUEUED,
                        request_key=child.request_key,
                    )
                    if checkpointed
                    else None
                ),
                "failure": AvailabilityOperationFailure(
                    code=RecipeImageCode.PREPARATION_EXHAUSTED,
                    detail="Image preparation observation budget ended",
                    retryable=False,
                    recovery_actions=[],
                ),
            }
        )
        cancellation = RecipeOperationCancellationResult(
            cancel_requested=True,
            cancel_request_id=str(uuid.uuid4()),
            cancel_actor="operator",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
        service = _service(
            sessions,
            storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
            authority=lambda *_args, **_kwargs: (None, {}),
            model_cache=cache,
            clock=lambda: datetime.now(UTC),
        )
        assert service._model_child_cancellation_pending(
            parent_id, payload, request_id, cancellation
        ) == (False, False)
        retained = cache.get_operation(child.id)
        assert retained.state == LifecycleState.QUEUED
        assert retained.cancellation is None
        assert retained.request_key == child_request
        # Explicit cancellation still reaches this exact owned download, even
        # if a crash prevented checkpointing its ID on the parent.
        service._model_child_cancellation_pending(
            parent_id,
            payload.model_copy(update={"failure": None}),
            request_id,
            cancellation.model_copy(update={"cancel_request_id": str(uuid.uuid4())}),
        )
        stopped = cache.get_operation(child.id)
        assert stopped.state == LifecycleState.CANCELLED
        assert stopped.cancellation is not None
    finally:
        cache.close()


def test_unknown_outcome_is_raised_not_failed_and_same_key_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch an unaccepted request becoming a synthetic terminal FAILED view."""
    monkeypatch.setattr(
        "vonk_control.recipe_image_availability.requests."
        "REQUEST_OBSERVATION_BUDGET_SECONDS",
        0,
    )
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        revision = _add_revision(session, "unknown-outcome", recipe)
        _add_head(session, revision)
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=None,
        clock=lambda: datetime(2026, 10, 8, tzinfo=UTC),
    )
    request_id = "00000000-0000-4000-8000-000000000077"
    plan = SimpleNamespace(
        recipe_build_id="build", recipe_revision_id="unknown-outcome"
    )

    with pytest.raises(RecipeImageAvailabilityUnknown):
        service.start("unknown-outcome", actor="operator", request_id=request_id)
    with pytest.raises(RecipeImageAvailabilityUnknown):
        service.prepare_install(plan, "operator", "install-1")  # type: ignore[arg-type]

    service._authority = lambda *_a, **_k: (recipe, _runtime())
    view = service.start("unknown-outcome", actor="operator", request_id=request_id)
    assert view.state != LifecycleState.FAILED.value
    assert view.request_id == request_id
