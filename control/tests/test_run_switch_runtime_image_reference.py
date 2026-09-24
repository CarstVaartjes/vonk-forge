"""The RunSwitch claim protects exact OCI bytes before publication."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.artifact_lifecycle import (
    ArtifactIdentity,
    clear_removal,
    reserve_removal,
)
from vonk_control.artifact_reference_scan import runtime_image_reference_reasons
from vonk_control.models import AgentNode, ArtifactLifecycleGate, Job
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchPhase,
    RunSwitchPlan,
)
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImageReceipt,
)

from .test_direct_run_switch_production_path import (
    ARCHIVE_DIGEST,
    NOW,
    _direct_request,
    _make_service,
)


def _apply(service: Any, revision_id: str):
    request = _direct_request(revision_id)
    return service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(mode="json"), request_key=str(uuid.uuid4())
        ),
        actor="test",
    )


def _reference_intent(job: Job) -> Mapping[str, object]:
    result = job.result
    assert isinstance(result, Mapping)
    intent = result.get("runtime_image_reference_intent")
    assert isinstance(intent, Mapping)
    return intent


def _advance_to_runtime_image(
    service: Any,
    sessions: sessionmaker[Session],
    operation_id: str,
) -> tuple[RunSwitchPlan, RunSwitchPhase, dict[str, object]]:
    for _ in range(20):
        with sessions() as session:
            job = session.get(Job, operation_id)
            assert job is not None
            assert job.state in {"queued", "running"}, job.status_reason
            progress = dict(job.result or {})
            plan = RunSwitchPlan.model_validate_json(
                json.dumps(job.payload["plan"]), strict=True
            )
            phase_index = progress.get("phase_index")
            assert type(phase_index) is int
            phase = plan.phases[phase_index]
            if phase.kind == "prepare" and phase.subphase == "runtime-image":
                return plan, phase, progress
        service._advance(operation_id)
    raise AssertionError("RunSwitch did not reach runtime-image preparation")


def _observe_prepublication(
    executor: Any,
    sessions: sessionmaker[Session],
    storage: FilesystemRuntimeImageStorage,
    *,
    before_publish_hook: Callable[
        [RuntimeImageReceipt, Callable[[RuntimeImageReceipt], object]], None
    ]
    | None = None,
) -> list[str]:
    preparer = executor._runtime_image_preparer
    assert callable(preparer)
    observed: list[str] = []

    def wrapped_preparer(
        document: Mapping[str, object],
        runtime_spec: Mapping[str, object],
        build: object,
        *,
        before_publish: Callable[[RuntimeImageReceipt], object] | None = None,
    ) -> object:
        assert before_publish is not None

        def check_reference(receipt: RuntimeImageReceipt) -> None:
            if before_publish_hook is None:
                before_publish(receipt)
            else:
                before_publish_hook(receipt, before_publish)
            with sessions() as session:
                reasons = runtime_image_reference_reasons(
                    session, (receipt.oci_archive_sha256,)
                )
            assert any(
                reason.startswith("run/switch operation ")
                for reason in reasons[receipt.oci_archive_sha256]
            )
            assert not (storage.root / receipt.oci_archive_sha256).exists()
            observed.append(receipt.oci_archive_sha256)

        return preparer(
            document,
            runtime_spec,
            build,
            before_publish=check_reference,
        )

    executor._runtime_image_preparer = wrapped_preparer
    return observed


def test_postgres_claim_is_durable_before_image_commit(
    tmp_path: Path, postgres_engine
) -> None:
    service, sessions, revision_id, _digest, _mapping, executor, _events = (
        _make_service(tmp_path, engine=postgres_engine)
    )
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    observed = _observe_prepublication(executor, sessions, storage)
    operation = _apply(service, revision_id)
    plan, phase, _progress = _advance_to_runtime_image(
        service, sessions, operation.operation_id
    )

    service._advance(operation.operation_id)

    assert observed == [ARCHIVE_DIGEST]
    assert (storage.root / ARCHIVE_DIGEST).is_file()
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None and job.result is not None
        intent = _reference_intent(job)
        assert intent["operation_id"] == operation.operation_id
        assert intent["request_key"] == operation.request_key
        assert intent["plan_digest"] == plan.plan_digest
        assert intent["phase_index"] == phase.index
        assert intent["archive_sha256"] == ARCHIVE_DIGEST
        reasons = runtime_image_reference_reasons(session, (ARCHIVE_DIGEST,))
    assert any("runtime image preparation" in item for item in reasons[ARCHIVE_DIGEST])


def test_postgres_reference_survives_worker_death_and_restart(
    tmp_path: Path, postgres_engine
) -> None:
    service, sessions, revision_id, _digest, _mapping, executor, _events = (
        _make_service(tmp_path, engine=postgres_engine)
    )
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    original_preparer = executor._runtime_image_preparer
    operation = _apply(service, revision_id)
    _plan, phase, _progress = _advance_to_runtime_image(
        service, sessions, operation.operation_id
    )

    def die_after_reference(
        receipt: RuntimeImageReceipt,
        before_publish: Callable[[RuntimeImageReceipt], object],
    ) -> None:
        before_publish(receipt)
        raise SystemExit("simulated worker death after reference commit")

    observed = _observe_prepublication(
        executor,
        sessions,
        storage,
        before_publish_hook=die_after_reference,
    )
    with pytest.raises(SystemExit, match="simulated worker death"):
        service._advance(operation.operation_id)

    assert observed == []
    assert not (storage.root / ARCHIVE_DIGEST).exists()
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None and job.state == "running"
        intent_before_restart = dict(_reference_intent(job))
        assert intent_before_restart["phase_index"] == phase.index
        reasons = runtime_image_reference_reasons(session, (ARCHIVE_DIGEST,))
    assert any(
        "runtime image preparation" in reason for reason in reasons[ARCHIVE_DIGEST]
    )

    executor._runtime_image_preparer = original_preparer
    restarted_service = RunSwitchOperationService(
        sessions,
        lifecycle=service._lifecycle,
        clock=service._clock,
        mappings=service._mappings,
        artifacts=service._artifacts,
        artifact_phase_executor=executor,
        published_image_receipt=service._published_image_receipt,
        memory_floor_bytes=service._memory_floor,
    )
    restarted_service._advance(operation.operation_id)

    assert (storage.root / ARCHIVE_DIGEST).is_file()
    with sessions() as session:
        resumed_job = session.get(Job, operation.operation_id)
        assert (
            resumed_job is not None
            and resumed_job.state == "running"
            and isinstance(resumed_job.result, Mapping)
        )
        assert dict(_reference_intent(resumed_job)) == intent_before_restart
        assert resumed_job.result["phase_index"] == phase.index + 1


def test_postgres_retry_rebinds_prepared_image_reference_to_new_job(
    tmp_path: Path, postgres_engine
) -> None:
    service, sessions, revision_id, _digest, _mapping, executor, _events = (
        _make_service(tmp_path, engine=postgres_engine)
    )
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    _observe_prepublication(executor, sessions, storage)
    operation = _apply(service, revision_id)
    _advance_to_runtime_image(service, sessions, operation.operation_id)
    service._advance(operation.operation_id)

    retry_key = str(uuid.uuid4())
    with sessions.begin() as session:
        previous = session.get(Job, operation.operation_id)
        assert previous is not None and isinstance(previous.result, Mapping)
        failed_progress = dict(previous.result)
        assert isinstance(
            failed_progress.get("runtime_image_reference_intent"), Mapping
        )
        failed_progress["retryable"] = True
        previous.state = "failed"
        previous.status_reason = "simulated recoverable failure after image publication"
        previous.result = failed_progress

    retried = service.retry(
        operation.operation_id,
        actor="retrying-operator",
        request_key=retry_key,
    )

    with sessions() as session:
        retry_job = session.get(Job, retried.operation_id)
        assert retry_job is not None
        intent = _reference_intent(retry_job)
        assert intent["operation_id"] == retry_job.id
        assert intent["request_key"] == retry_key
        assert intent["actor"] == "retrying-operator"
        assert (
            intent["workload_intent_ordinal"]
            == retry_job.payload["workload_intent_ordinal"]
        )
        reasons = runtime_image_reference_reasons(session, (ARCHIVE_DIGEST,))
    assert any(
        reason.startswith("run/switch operation ") for reason in reasons[ARCHIVE_DIGEST]
    )


def test_postgres_cancel_winning_before_owner_commit_refuses_publication(
    tmp_path: Path, postgres_engine
) -> None:
    service, sessions, revision_id, _digest, _mapping, executor, _events = (
        _make_service(tmp_path, engine=postgres_engine)
    )
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    operation = _apply(service, revision_id)

    def cancel_first(
        receipt: RuntimeImageReceipt,
        before_publish: Callable[[RuntimeImageReceipt], object],
    ) -> None:
        service.cancel(
            operation.operation_id,
            actor="test",
            request_key=str(uuid.uuid4()),
            reason="cancel before image publication",
        )
        before_publish(receipt)

    _observe_prepublication(
        executor,
        sessions,
        storage,
        before_publish_hook=cancel_first,
    )
    _advance_to_runtime_image(service, sessions, operation.operation_id)

    service._advance(operation.operation_id)

    assert not (storage.root / ARCHIVE_DIGEST).exists()
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None and job.result is not None
        assert job.result.get("runtime_image_reference_intent") is None
        assert job.result.get("cancellation") is not None
    service._advance(operation.operation_id)
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None and job.state == "cancelled"


def test_postgres_cancel_after_publication_keeps_reference_in_terminal_record(
    tmp_path: Path, postgres_engine
) -> None:
    service, sessions, revision_id, _digest, _mapping, executor, _events = (
        _make_service(tmp_path, engine=postgres_engine)
    )
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    _observe_prepublication(executor, sessions, storage)
    operation = _apply(service, revision_id)
    _advance_to_runtime_image(service, sessions, operation.operation_id)
    service._advance(operation.operation_id)

    service.cancel(
        operation.operation_id,
        actor="test",
        request_key=str(uuid.uuid4()),
        reason="cancel after image publication",
    )
    service._advance(operation.operation_id)

    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None and job.state == "cancelled"
        intent = _reference_intent(job)
        assert intent["archive_sha256"] == ARCHIVE_DIGEST
        assert job.result is not None
        assert job.result.get("cancellation") is not None


def test_postgres_replaced_workload_claim_refuses_image_publication(
    tmp_path: Path, postgres_engine
) -> None:
    service, sessions, revision_id, _digest, _mapping, executor, _events = (
        _make_service(tmp_path, engine=postgres_engine)
    )
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    operation = _apply(service, revision_id)
    plan, _phase, _progress = _advance_to_runtime_image(
        service, sessions, operation.operation_id
    )

    def replace_claim(
        receipt: RuntimeImageReceipt,
        before_publish: Callable[[RuntimeImageReceipt], object],
    ) -> None:
        with sessions.begin() as session:
            node = session.get(AgentNode, plan.spark_group.nodes[0].node_id)
            assert node is not None and node.workload_intent_ordinal is not None
            node.workload_intent_ordinal += 1
        before_publish(receipt)

    _observe_prepublication(
        executor,
        sessions,
        storage,
        before_publish_hook=replace_claim,
    )

    service._advance(operation.operation_id)

    assert not (storage.root / ARCHIVE_DIGEST).exists()
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None and job.result is not None
        assert job.result.get("runtime_image_reference_intent") is None


def test_postgres_deletion_fence_defers_exact_image_then_current_retry_publishes(
    tmp_path: Path, postgres_engine
) -> None:
    service, sessions, revision_id, _digest, _mapping, executor, _events = (
        _make_service(tmp_path, engine=postgres_engine)
    )
    storage = FilesystemRuntimeImageStorage(tmp_path / "runtime")
    observed = _observe_prepublication(executor, sessions, storage)
    operation = _apply(service, revision_id)
    _advance_to_runtime_image(service, sessions, operation.operation_id)
    identity = ArtifactIdentity("runtime-image", ARCHIVE_DIGEST)
    fence = str(uuid.uuid4())
    removal_id = str(uuid.uuid4())
    with sessions.begin() as session:
        reserve_removal(
            session,
            (identity,),
            owner_kind="recipe-image-job",
            owner_id=removal_id,
            fence=fence,
            now=NOW,
        )

    service._advance(operation.operation_id)

    assert observed == []
    assert not (storage.root / ARCHIVE_DIGEST).exists()
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        gate = session.get(
            ArtifactLifecycleGate,
            {"artifact_kind": "runtime-image", "artifact_sha256": ARCHIVE_DIGEST},
        )
        assert job is not None and job.result is not None
        assert job.result.get("runtime_image_reference_intent") is None
        assert job.result.get("retry_reason") == "artifact.deletion_in_progress"
        assert gate is not None and gate.removal_fence == fence

    with sessions.begin() as session:
        clear_removal(
            session,
            (identity,),
            owner_kind="recipe-image-job",
            owner_id=removal_id,
            fence=fence,
            now=NOW,
        )
    later = NOW + timedelta(seconds=6)
    service._clock = lambda: later
    executor._clock = lambda: later
    service._advance(operation.operation_id)

    assert observed == [ARCHIVE_DIGEST]
    assert (storage.root / ARCHIVE_DIGEST).is_file()
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None and job.result is not None
        assert _reference_intent(job)["archive_sha256"] == ARCHIVE_DIGEST
