"""Recipe image availability: model image children."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vonk_agent_protocol.lifecycle_vocabulary import (
    LifecycleState,
    SecurityRefusalReason,
)
from vonk_agent_protocol.wire_model import OperationProgress
from vonk_control.artifact_reference_scan.image_references import (
    runtime_image_reference_findings,
    runtime_image_reference_reasons,
)
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.jobs.documents import (
    AvailabilityModelChild,
)
from vonk_control.model_cache.contract import (
    ModelCacheCounters,
    ModelCacheRepairPreviewResponse,
    ModelCacheSetState,
)
from vonk_control.model_cache.progress import cache_progress, progress_document
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.model_primitives import Base
from vonk_control.models.fleet import (
    User,
)
from vonk_control.models.jobs import Job
from vonk_control.operations.contract import (
    AvailabilityOperationFailure,
    AvailabilityRecoveryAction,
)
from vonk_control.persisted_waits import schedule_wait
from vonk_control.recipe_image_availability.api import (
    _view_document,
)
from vonk_control.recipe_image_availability.contracts import (
    RecipeImageAvailabilityError,
)
from vonk_control.runtime_image_preparation.contracts import (
    RuntimeImagePreparationError,
    RuntimeImageReferenceIntent,
    read_runtime_image_reference_intent,
)
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from vonk_control.strict_json import serialize_json_value

from .non_blocking import assert_ended_without_blocking
from .recipe_image_availability_support import (
    ARCHIVE,
    ARCHIVE_SHA,
    IMAGE_DIGEST,
    Transport,
    _add_revision,
    _availability_payload,
    _builder,
    _download_preview,
    _manifest,
    _persist_fake_model_cache_child,
    _recipe,
    _reference_receipt,
    _runtime,
    _service,
)
from .runtime_image_fixtures import remove_test_image


def _repair_preview(*, artifact_set_sha256: str, plan_digest: str):
    return ModelCacheRepairPreviewResponse(
        artifact_set_sha256=artifact_set_sha256,
        plan_digest=plan_digest,
        artifact_count=0,
        current_state=ModelCacheSetState.FAILED,
        expected_bytes=0,
        verified_bytes=0,
    ).model_dump(mode="json")


def _progress_members(value: object) -> list[Mapping[str, object]]:
    """Read the decoded progress member array as mappings, in order."""

    return [
        require_mapping(member, "progress member")
        for member in require_sequence(value, "progress members")
    ]


@pytest.mark.usefixtures("damaged_json_rows")
def test_model_and_image_children_advance_independently_and_reuse_image(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-overlap", recipe)
    now = datetime.now(UTC)
    child = SimpleNamespace(
        blockers=(),
        id="00000000-0000-4000-8000-000000000201",
        request_key="00000000-0000-4000-8000-000000000202",
        state="running",
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters.model_validate(
                    {
                        "phase": "downloading",
                        "downloaded_bytes": 40,
                        "expected_bytes": 100,
                        "completed_artifacts": 0,
                        "total_artifacts": 1,
                    }
                ),
                previous=None,
                now=now,
            )
        ),
        failure=None,
    )
    _persist_fake_model_cache_child(
        sessions,
        child=child,
        model_content_sha256=next(
            model.model.content_sha256 for model in recipe.models
        ),
        now=now,
    )

    class ModelCache:
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

        def reusable_operation(self, **_: object) -> None:
            return None

        def start_download(self, **_: object) -> SimpleNamespace:
            return child

        def get_operation(self, _operation_id: str) -> SimpleNamespace:
            return child

    transport = Transport()
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        model_cache=ModelCache(),
        clock=lambda: now,
    )
    queued = service.start("revision-overlap", actor="operator", request_id="q" * 36)
    assert service.run_pending() == 1
    partial = service.get(queued.id)
    assert partial.state == LifecycleState.BACKOFF
    assert partial.result is None
    assert transport.calls == 1
    assert partial.image_state == "succeeded"
    assert partial.image_failure is None
    assert partial.progress is not None
    assert partial.progress["completed_bytes"] == 40 + len(ARCHIVE)
    image_child = next(
        item
        for item in _view_document(partial).children
        if item.kind == "runtime-image"
    )
    assert image_child.state == "succeeded"
    assert image_child.progress.completed_bytes == len(ARCHIVE)
    assert (
        _progress_members(partial.progress["members"])[-1]["member_id"] == "model-cache"
    )
    assert isinstance(service._storage, FilesystemRuntimeImageStorage)
    remove_test_image(service._storage, ARCHIVE_SHA)
    child.state = "succeeded"
    with sessions.begin() as session:
        row = session.get(Job, queued.id)
        assert row is not None
        schedule_wait(row, datetime(2000, 1, 1, tzinfo=UTC))
    builds: list[bool] = []
    restarted = _service(
        sessions,
        storage=service._storage,
        builder=_builder(service._storage, calls=builds),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        model_cache=ModelCache(),
        clock=lambda: datetime.now(UTC),
    )
    assert restarted.run_pending() == 1
    completed = restarted.get(queued.id)
    assert completed.state == "succeeded", completed.failure
    # The removed archive is built again rather than reused.
    assert builds == [False]
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        completed,
        end=lambda receipt: receipt,
        fresh=lambda _: restarted.start(
            "revision-overlap",
            actor="operator",
            request_id="prepare-after-rehydration",
        ),
    )


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
        assert row is not None
        assert row.state == "cancelled"
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
