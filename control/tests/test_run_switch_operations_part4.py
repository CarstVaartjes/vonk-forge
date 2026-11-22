from __future__ import annotations

import errno
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import select, update
from vonk_agent_protocol import (
    FailureCode,
    LifecycleState,
)
from vonk_control.auth import CursorCodec
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.failure_classification import is_security_failure
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.operation_api import OperationQuery
from vonk_control.operations.contract import (
    OperationFailureEvidence,
    OperationRecoveryAction,
)
from vonk_control.operations.item_contract import OperationItem, operation_item
from vonk_control.recipe_operations import (
    RecipeOperationService,
)
from vonk_control.run_switch_contract import (
    InvocationMetadata,
    RunSwitchApplyRequest,
    RunSwitchContainerBuildResult,
    RunSwitchOperation,
    RunSwitchOperationResult,
    RunSwitchPlan,
    RunSwitchRuntimeInstallResult,
    RunSwitchTargetTransferEvidenceResult,
)
from vonk_control.run_switch_operations import (
    RecipeLifecyclePhaseExecutor,
    RunSwitchOperationConflict,
    RunSwitchOperationProvider,
    RunSwitchOperationService,
    _failure_code_of,
    _phase_result,
)

from .recipe_operations_support import (
    NOW,
    installed_recipe,
    setup_services,
)
from .run_switch_operations_support import (
    ColdStartPhaseExecutor,
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _child_operation_id,
    _cold_compile_switch,
    _host_changes_during_compile,
    _ObservingLifecycle,
    _parked_start_switch,
    _replace_child,
    _request,
    _result,
    _service,
    _target_copy_evidence,
)


@pytest.mark.parametrize("reported_bytes", [512, 1536])
def test_child_distribution_progress_is_typed_and_restart_safe(
    tmp_path: Path, reported_bytes: int
) -> None:
    """Catches an underestimated child meter failing its parent across restart."""
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    artifact_executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        artifact_executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )

    assert service.tick() is True
    child_id = _child_operation_id(service.get(operation.operation_id))
    _replace_child(artifact_executor, child_id, state="running")
    _replace_child(
        artifact_executor,
        child_id,
        payload={
            "progress": {
                "completed_bytes": reported_bytes,
                "total_bytes": 1024,
                "members": [
                    {
                        "node_id": nodes[0],
                        "phase": "transfer",
                        "state": "running",
                        "completed_bytes": reported_bytes,
                        "total_bytes": 1024,
                    }
                ],
            }
        },
    )
    assert service.tick() is True
    waiting = service.get(operation.operation_id)
    assert waiting.progress.completed_bytes == reported_bytes
    assert waiting.progress.total_bytes == max(1024, reported_bytes)
    assert waiting.progress.total_bytes_known is True
    assert waiting.progress.members[0].node_id == nodes[0]
    assert waiting.progress.members[0].completed_bytes == reported_bytes
    assert waiting.progress.members[0].total_bytes == max(1024, reported_bytes)
    assert waiting.progress.members[0].state == "running"

    # The parent only stores the child ID and JSON checkpoint.  A fresh
    # service instance can project the same durable child progress.
    restarted = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        artifact_executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    resumed = restarted.get(operation.operation_id)
    assert resumed.progress.completed_bytes == reported_bytes
    assert resumed.progress.members[0].completed_bytes == reported_bytes

    _replace_child(artifact_executor, child_id, state="succeeded")
    assert plan.preparation is not None
    runtime_image = plan.preparation.runtime_image
    assert runtime_image is not None
    _replace_child(
        artifact_executor,
        child_id,
        payload={
            "copied_bytes": 1024,
            "evidence": [
                {
                    "node_id": nodes[0],
                    "downloaded_bytes": 1024,
                }
            ],
        },
    )
    assert restarted.tick() is True
    completed_transfer = restarted.get(operation.operation_id)
    assert completed_transfer.progress.completed_bytes == max(1024, reported_bytes)
    assert completed_transfer.progress.members[0].completed_bytes == max(
        1024, reported_bytes
    )
    assert completed_transfer.progress.members[0].state == "succeeded"
    assert any(
        isinstance(item, RunSwitchTargetTransferEvidenceResult)
        and item.node_id == nodes[0]
        for item in _result(completed_transfer).phase_results
    )
    # The next durable tick consumes the persisted transfer receipts and runs
    # the real verify phase; no caller supplied progress is reconstructed.
    assert restarted.tick() is True
    verified = restarted.get(operation.operation_id)
    assert "verify" in _result(verified).completed_phases


def test_typed_transient_child_failure_is_retried_automatically(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle,
        mapping_id,
        build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    artifact_executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        artifact_executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    now = [NOW]
    service._clock = lambda: now[0]
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )

    assert service._advance(operation.operation_id) is True
    child_id = _child_operation_id(service.get(operation.operation_id))
    _replace_child(artifact_executor, child_id, state="failed")
    _replace_child(
        artifact_executor,
        child_id,
        payload={
            "error_code": "agent.copy.timeout",
            "failure_kind": "temporary-dependency",
        },
    )
    assert service._advance(operation.operation_id) is True
    retrying = service.get(operation.operation_id)
    assert retrying.state == "running"
    assert retrying.result is not None
    assert retrying.result.child_operation_id is None
    assert retrying.result.retry_attempt is not None
    assert retrying.result.retry_attempt > 1
    assert retrying.status_reason is not None
    assert retrying.next_attempt_at is not None
    assert retrying.next_attempt_at > now[0]
    now[0] = retrying.next_attempt_at
    assert service._advance(operation.operation_id) is True
    resumed = service.get(operation.operation_id)
    assert resumed.state == "running"
    assert _child_operation_id(resumed) != child_id


def test_run_switch_failure_classification_is_terminal_only_for_authentication() -> (
    None
):
    request = httpx2.Request("GET", "https://example.invalid/artifact")

    def status_error(status: int) -> httpx2.HTTPStatusError:
        response = httpx2.Response(status, request=request)
        return httpx2.HTTPStatusError(
            "request failed", request=request, response=response
        )

    for status in (401, 403):
        assert is_security_failure(_failure_code_of(status_error(status))) is True
    for status in (404, 429, 500, 503):
        assert is_security_failure(_failure_code_of(status_error(status))) is False
    assert is_security_failure(_failure_code_of(PermissionError(errno.EPERM, "x")))
    for number in (errno.ENOSPC, errno.ECONNRESET):
        assert is_security_failure(_failure_code_of(OSError(number, "x"))) is False


class _CountingPreviewLifecycle(RecipeOperationService):
    """Real lifecycle service that counts how often admission is re-derived.

    It replaces no production state: every other call is delegated to the real
    service, so the count observes the executor's actual admission decision.
    """

    def __init__(self, inner: RecipeOperationService) -> None:
        self._inner = inner
        self.previews = 0

    def preview_run(
        self,
        installation_id: str,
        alias: str,
        *,
        profile_application_id: str | None = None,
    ):
        self.previews += 1
        return self._inner.preview_run(
            installation_id, alias, profile_application_id=profile_application_id
        )

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def test_start_phase_adopts_the_child_it_already_queued(tmp_path: Path) -> None:
    """A resumed start phase keeps the durable child it already queued.

    Run admission hashes live inventory observation time and current
    reservations, so re-previewing after the first start is admitted derives a
    different plan digest for the identical child.  Without adoption the
    executor offered the unchanged request key to admission with the changed
    digest, and admission correctly rejected the second attempt instead of
    resuming the child that was already running.
    """

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = RunSwitchOperationService(
        sessions,
        memory_floor_bytes=0,
        lifecycle=lifecycle,
        clock=lambda: lifecycle._clock(),
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    start_phase = next(phase for phase in plan.phases if phase.kind == "start")
    counting = _CountingPreviewLifecycle(lifecycle)
    executor = RecipeLifecyclePhaseExecutor(
        counting, sessions, ClusterMappingService(sessions), lifecycle._clock
    )
    with sessions.begin() as session:
        for node_id in nodes:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.workload_intent_ordinal = 1
    progress = RunSwitchOperationResult(
        workload_intent_ordinal=1,
        phase_results=[
            RunSwitchRuntimeInstallResult(
                phase="prepare",
                subphase="runtime-install",
                installation_id=installation.owner_id,
            )
        ],
    )
    request_key = str(uuid.uuid4())

    first = executor.execute(
        plan,
        start_phase,
        item_index=0,
        actor="admin",
        request_key=request_key,
        progress=progress,
    )
    second = executor.execute(
        plan,
        start_phase,
        item_index=0,
        actor="admin",
        request_key=request_key,
        progress=progress,
    )

    assert first.operation_id is not None
    assert second.operation_id == first.operation_id
    # The digest was never re-derived: adoption bound the durable request key,
    # operation kind, owner kind, installation and alias instead.
    assert counting.previews == 1
    with sessions() as session:
        starts = tuple(session.scalars(select(Job).where(Job.kind == "recipe.start")))
    assert len(starts) == 1


def test_invocation_metadata_does_not_change_plan_digest(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    web_request = _request(sessions, nodes[0])
    cli_request = web_request.model_copy(
        update={"invocation": InvocationMetadata(origin="cli", reason="switch")}
    )
    assert (
        service.preview(web_request, actor="admin").plan_digest
        == service.preview(cli_request, actor="admin").plan_digest
    )


def test_activity_provider_preserves_group_and_canonical_nested_progress(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    provider = RunSwitchOperationProvider(service)
    page = provider.list_operations(
        OperationQuery(after=None, limit=10, state=None, node_id=None)
    )
    assert page.total == 1
    item = page.items[0]
    assert isinstance(item, OperationItem)
    progress = item.progress
    assert progress is not None
    checkpoint = progress.checkpoint
    assert checkpoint is not None
    assert item.created_at is not None
    assert item.id == operation.operation_id
    assert item.job_id == operation.operation_id
    assert item.node_ids == list(nodes)
    assert item.node_id == nodes[0]
    assert item.attempt >= 1
    assert item.supported_actions == [OperationRecoveryAction.CANCEL]
    assert progress.total_bytes_known is True
    assert progress.members[0].member_id == nodes[0]
    assert checkpoint.digest == operation.plan_digest
    assert datetime.fromisoformat(item.created_at).tzinfo == UTC
    assert provider.get_operation(operation.operation_id).id == operation.operation_id


def test_activity_provider_integrates_with_global_cursor_and_detail_projection(
    tmp_path: Path,
) -> None:
    from vonk_control.operation_api import (
        OperationProvider,
        get_operation_from_providers,
        merge_operation_providers,
        operation_detail_response,
    )

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )
    request = _request(sessions, nodes[0])
    operations = [
        service.apply(
            RunSwitchApplyRequest(
                **request.model_dump(),
                request_key=str(uuid.uuid4()),
            ),
            actor="admin",
        )
        for _ in range(3)
    ]
    provider = RunSwitchOperationProvider(service)
    shared = OperationProvider(
        family=provider.family,
        list_operations=provider.list_operations,
        get_operation=provider.get_operation,
    )
    cursors = CursorCodec(hashlib.sha256(b"run-switch-activity").digest())
    first = merge_operation_providers(
        [shared],
        cursor=None,
        limit=2,
        state="queued",
        node_id=nodes[0],
        cursors=cursors,
    )
    assert len(first.items) == 2
    assert first.total == 3
    assert first.next_cursor is not None
    second = merge_operation_providers(
        [shared],
        cursor=first.next_cursor,
        limit=2,
        state="queued",
        node_id=nodes[0],
        cursors=cursors,
    )
    assert len(second.items) == 1
    assert second.total == 3
    detail = operation_detail_response(first.items[0])
    assert detail.node_ids == list(nodes)
    assert detail.progress is not None
    assert detail.progress.members[0].member_id == nodes[0]
    assert detail.recovery is not None
    assert detail.recovery.actions[0].value == "inspect"
    assert get_operation_from_providers([shared], operations[0].operation_id).id == (
        operations[0].operation_id
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_activity_provider_keeps_valid_items_when_one_plan_is_unreadable(
    tmp_path: Path,
) -> None:
    from vonk_control.operation_api import (
        OperationProvider,
        merge_operation_providers,
        operation_detail_response,
    )

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )
    request = _request(sessions, nodes[0])
    operations = [
        service.apply(
            RunSwitchApplyRequest(
                **request.model_dump(), request_key=str(uuid.uuid4())
            ),
            actor="admin",
        )
        for _ in range(2)
    ]
    valid, unreadable = operations
    with sessions.begin() as session:
        job = session.get(Job, unreadable.operation_id)
        assert job is not None and isinstance(job.payload, dict)
        persisted_payload = dict(job.payload)
        persisted_plan = persisted_payload.get("plan")
        assert isinstance(persisted_plan, dict)
        invalid_plan = dict(persisted_plan)
        # A required current-contract field is absent. Reading the durable
        # operation shows it from the identity recorded beside the plan.
        invalid_plan.pop("action")
        persisted_payload["plan"] = invalid_plan
        job.payload = persisted_payload
        job.created_at = job.created_at + timedelta(seconds=1)
        job.updated_at = job.created_at

    # A plan that cannot be read is unknown, not an error: the operation is
    # shown from the identity recorded beside it.
    rebuilt = service.get(unreadable.operation_id)
    assert rebuilt.operation_id == unreadable.operation_id
    assert rebuilt.plan_digest == unreadable.plan_digest
    assert rebuilt.status_reason is not None

    provider = service.activity_provider()
    shared_provider = OperationProvider(
        family=provider.family,
        list_operations=provider.list_operations,
        get_operation=provider.get_operation,
    )
    page = merge_operation_providers(
        [shared_provider],
        cursor=None,
        limit=10,
        state=None,
        node_id=nodes[0],
        cursors=CursorCodec(hashlib.sha256(b"run-switch-unreadable").digest()),
    )

    assert page.total == 2
    assert [operation_item(item).id for item in page.items] == [
        unreadable.operation_id,
        valid.operation_id,
    ]
    damaged_item = operation_item(page.items[0])
    # The unreadable plan does not make the row unavailable: it is listed from
    # the identity recorded beside the plan.
    assert damaged_item.kind != "run-switch-unreadable"
    assert damaged_item.state != "unavailable"
    assert damaged_item.failure is None
    damaged_detail = operation_detail_response(damaged_item)
    assert damaged_detail.id == unreadable.operation_id
    assert damaged_detail.failure is None
    valid_item = operation_item(page.items[1])
    assert valid_item.id == valid.operation_id
    assert valid_item.node_ids == list(nodes)
    assert valid_item.failure is None


@pytest.mark.parametrize("failed", [False, True])
def test_measured_operation_keeps_unknown_totals_and_failure_readable(
    tmp_path: Path, failed: bool
) -> None:
    from vonk_control.operation_api import operation_detail_response

    switch = _cold_compile_switch(tmp_path)
    _host_changes_during_compile(switch)  # held for a refresh: totals unknown
    for _ in range(8):
        if "runtime-plan" in switch.executor.events:
            break
        switch.drive()
    assert "runtime-plan" in switch.executor.events
    reason = (
        "install plan is blocked: " + "missing durable runtime image receipt; " * 10
    )
    if failed:
        switch.service._fail(switch.operation.operation_id, reason)
    operation = switch.service.get(switch.operation.operation_id)
    detail = operation_detail_response(
        switch.service.activity_provider().get_operation(operation.operation_id)
    )
    assert detail.state == ("failed" if failed else "running")
    assert detail.progress is not None
    assert detail.progress.total_bytes is None
    assert detail.progress.total_bytes_known is False
    if failed:
        assert isinstance(detail.failure, OperationFailureEvidence)
        # The typed projection bounds the evidence without losing its cause.
        assert detail.failure.detail == operation.status_reason
        assert len(detail.failure.summary) <= 256
        assert detail.failure.error_code == FailureCode.OPERATION_FAILED
    else:
        assert detail.failure is None


@pytest.mark.parametrize(
    "invalid_result", [[], "broken", {"phase_index": "0"}, {"phase": "old-phase"}]
)
def test_operation_read_survives_malformed_persisted_result(
    tmp_path: Path, invalid_result: object
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    with sessions.begin() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        # A stored result can be any JSON value; persist it the way the driver
        # stored it rather than through the typed ORM attribute.
        session.execute(
            update(Job)
            .where(Job.id == operation.operation_id)
            .values(result=invalid_result)
        )
        previous_state = job.state
    # A damaged stored result is unknown, not an error: the operation is still
    # readable from its plan, and a retry (which needs the result) is refused as
    # not retryable so the person starts a new request.
    assert service.get(operation.operation_id).operation_id == operation.operation_id
    with pytest.raises(Exception) as _ending:
        service.retry(
            operation.operation_id, request_key=str(uuid.uuid4()), actor="admin"
        )
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        assert job.result == invalid_result
        assert job.state == previous_state


def test_terminal_checkpoint_after_retry_clears_failure_and_rejects_missing_evidence(
    tmp_path,
):
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    # Resume the durable checkpoint written after the last phase of a retry.
    with sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        row.state = "running"
        assert row.result is not None
        row.result = row.result | {
            "phase_index": len(plan.phases),
            "completed_phases": [phase.kind for phase in plan.phases],
            "retryable": True,
            "failed_phase": "transfer",
        }
    restarted = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )
    assert restarted.tick() is True
    completed = restarted.get(operation.operation_id)
    assert completed.state == "succeeded"
    assert completed.result is not None
    assert completed.result.retryable is False
    assert completed.result.failed_phase is None
    assert completed.status_reason is None

    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        RunSwitchOperation.model_validate(completed.model_dump() | {"result": None})
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        RunSwitchOperation.model_validate(completed.model_dump() | {"state": "failed"})
    with sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None
        row.result = None
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        restarted.get(operation.operation_id)


def test_nas_transfer_checkpoint_does_not_complete_unstarted_spark_copy(tmp_path):
    sessions, lifecycle, _, _, _, nodes = setup_services(tmp_path)

    class ColdInspector(CompleteArtifactInspector):
        def inspect(self, *args, **kwargs):
            return replace(
                super().inspect(*args, **kwargs),
                missing_nas_bytes=1024,
                nas_coverage="partial",
            )

    executor = ColdStartPhaseExecutor()
    service = _service(
        sessions,
        NOW,
        lifecycle,
        None,
        artifacts=ColdInspector(missing_spark_bytes=1024),
        phase_executor=executor,
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    service.tick()
    progress = service.get(operation.operation_id).progress
    assert executor.events == ["model-download"]
    assert progress.completed_bytes == 1024
    total_bytes = progress.total_bytes
    assert total_bytes is not None
    assert total_bytes > progress.completed_bytes
    assert progress.members[0].completed_bytes == 0


@pytest.mark.parametrize("child_completion", [False, True])
def test_overlapping_ticks_cannot_apply_completion_to_the_next_checkpoint(
    tmp_path, child_completion
):
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )

    class InterleavedExecutor(RecordingArtifactExecutor):
        entered = False
        service: RunSwitchOperationService | None = None

        def execute(self, *args, **kwargs):
            result = super().execute(*args, **kwargs)
            if not child_completion and not self.entered:
                self.entered = True
                assert self.service is not None
                self.service.tick()
            return result

        def get(self, operation_id):
            result = super().get(operation_id)
            assert result is not None
            if child_completion and result.state == "succeeded" and not self.entered:
                self.entered = True
                assert self.service is not None
                self.service.tick()
            return result

    executor = InterleavedExecutor(child_transfer=child_completion)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    executor.service = service
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    service.tick()
    if child_completion:
        child_id = _child_operation_id(service.get(operation.operation_id))
        _replace_child(executor, child_id, state="succeeded")
        _replace_child(
            executor, child_id, result=_target_copy_evidence(plan, plan.phases[0])
        )
        service.tick()
    current = service.get(operation.operation_id)
    assert current.progress.phase_index == 1
    assert current.current_phase == "verify"
    assert current.completed_phases == ["transfer"]


def test_cancel_intent_waits_for_transfer_receipt_and_preserves_shared_copies(tmp_path):
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    service.tick()
    child_id = _child_operation_id(service.get(operation.operation_id))
    key = str(uuid.uuid4())
    pending = service.cancel(
        operation.operation_id,
        actor="admin",
        request_key=key,
        reason="Stop preparation",
    )
    assert pending.state == "running"
    assert (
        service.cancel(
            operation.operation_id,
            actor="admin",
            request_key=key,
            reason="Stop preparation",
        )
        == pending
    )
    service.tick()
    assert service.get(operation.operation_id).state == "running"
    _replace_child(executor, child_id, state="succeeded")
    _replace_child(
        executor, child_id, result=_target_copy_evidence(plan, plan.phases[0])
    )
    restarted = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    restarted.tick()
    cancelled = restarted.get(operation.operation_id)
    assert cancelled.state == "cancelled"
    cancelled_result = _result(cancelled)
    assert cancelled_result.completed_phases == ["transfer"]
    assert cancelled_result.phase_results
    assert cancelled_result.child_operation_id is None
    assert not restarted._advance(operation.operation_id)
    with sessions() as session:
        installed = session.get(RecipeInstallation, plan.installation_id)
        assert installed is not None
        assert installed.state == "installed"


def test_cancel_closes_a_transfer_parked_for_an_operator_instead_of_waiting(tmp_path):
    """Live regression: cancelling a profile load deadlocked on a parked copy.

    The transfer child waited for an operator and offered no resume, so the
    cancellation waited for it forever. Nothing is running, so the cancelled
    order closes the idempotent child and finishes; copied bytes stay.
    """

    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    service.tick()
    child_id = _child_operation_id(service.get(operation.operation_id))
    _replace_child(executor, child_id, state="needs-operator")
    for _ in range(3):
        service.tick()
    assert service.get(operation.operation_id).state == LifecycleState.OBSERVING

    service.cancel(
        operation.operation_id,
        actor="admin",
        request_key=str(uuid.uuid4()),
        reason="Stop preparation",
    )
    for _ in range(3):
        service.tick()

    assert service.get(operation.operation_id).state == "cancelled"
    assert executor.abandoned == [child_id]


def test_cancel_queued_start_is_idempotent_and_active_cancel_starts_stop(tmp_path):
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    key = str(uuid.uuid4())
    cancelled = service.cancel(
        operation.operation_id,
        actor="admin",
        request_key=key,
        reason="Keep the current profile",
    )
    assert cancelled.state == "cancelled"
    assert cancelled.progress.state == "cancelled"
    repeated = service.cancel(
        operation.operation_id,
        actor="admin",
        request_key=key,
        reason="Different intent",
    )
    assert repeated.state == "cancelled"
    assert repeated.result is not None and repeated.result.cancellation is not None
    assert repeated.result.cancellation.reason == "Keep the current profile"
    request_key = str(uuid.uuid4())
    active = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=request_key,
        ),
        actor="admin",
    )
    # A different intent under a reused request key is a conflict, not a replay.
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        service.apply(
            RunSwitchApplyRequest(
                **{**request.model_dump(), "alias": "another-endpoint"},
                request_key=request_key,
            ),
            actor="admin",
        )
    service.tick()
    stop_key = str(uuid.uuid4())
    original_apply_stop = service.apply_stop

    def unavailable_stop(*_args, **_kwargs):
        raise RunSwitchOperationConflict("run-switch.stop_unavailable")

    # Cancellation is committed only together with an accepted Stop: a Stop
    # that cannot be admitted leaves the operation active and cancellable.
    service.apply_stop = unavailable_stop  # type: ignore[method-assign]
    with pytest.raises(Exception) as _ending:
        service.cancel(
            active.operation_id,
            actor="admin",
            request_key=stop_key,
            reason="Stop running",
        )
    assert service.get(active.operation_id).state != "cancelled"
    service.apply_stop = original_apply_stop  # type: ignore[method-assign]
    stopped = service.cancel(
        active.operation_id,
        actor="admin",
        request_key=stop_key,
        reason="Stop running",
    )
    assert stopped.kind == "recipe.stop.v2"
    assert stopped.state == "queued"
    assert service.get(active.operation_id).state == "cancelled"


def test_production_build_queue_receipt_survives_phase_handoff_and_completion(
    tmp_path: Path,
    postgres_engine,
) -> None:
    from .test_run_switch_build_fencing import _direct_parent

    sessions, planner, parent, request, selected = _direct_parent(
        tmp_path, postgres_engine
    )
    lifecycle = planner._lifecycle
    executor = planner._phase_executor
    assert lifecycle is not None and executor is not None
    with sessions() as session:
        job = session.get(Job, parent.operation_id)
        assert job is not None
        plan = RunSwitchPlan.model_validate_json(json.dumps(job.payload["plan"]))
        progress = RunSwitchOperationResult.model_validate_json(
            json.dumps(job.result), strict=True
        )
    phase = plan.phases[0]
    assert phase.subphase == "container-build"
    request_key = request.request_key
    assert request_key is not None

    def execute():
        return executor.execute(
            plan,
            phase,
            item_index=0,
            actor="admin",
            request_key=request_key,
            progress=progress,
        )

    scheduled = execute()
    assert scheduled.operation_id is not None
    assert scheduled.result is not None
    assert lifecycle.get(scheduled.operation_id).state == "running"
    received = _phase_result(scheduled.result, phase=phase)
    assert isinstance(received, RunSwitchContainerBuildResult)
    assert received.state == "building"
    assert received.build_id == selected.build_id
    assert received.build_input_sha256 == selected.build_input_sha256
    assert received.image_digest is None
    replay = execute()
    assert replay.operation_id == scheduled.operation_id
    assert replay.result is not None
    assert _phase_result(replay.result, phase=phase) == received

    with sessions.begin() as session:
        build = session.get(RecipeBuild, selected.build_id)
        assert build is not None
        build.state = "succeeded"
        build.image_digest = "sha256:" + "a" * 64
        build.oci_layout_sha256 = "b" * 64
        build.image_bytes = 123
        job = session.get(Job, scheduled.operation_id)
        assert job is not None
        job.state = "succeeded"
    completed = execute()
    assert completed.operation_id is None
    assert completed.result is not None
    receipt = _phase_result(completed.result, phase=phase)
    assert isinstance(receipt, RunSwitchContainerBuildResult)
    assert receipt.state == "succeeded"
    assert receipt.image_digest == "sha256:" + "a" * 64
    assert receipt.oci_layout_sha256 == "b" * 64
    assert receipt.image_bytes == 123
    # Success never substitutes defaults for incomplete immutable evidence.
    with sessions.begin() as session:
        stale = session.get(RecipeBuild, selected.build_id)
        assert stale is not None
        stale.image_bytes = None
    with pytest.raises(Exception) as _ending:
        execute()


def test_parked_start_with_an_established_run_completes_without_an_operator(
    tmp_path: Path,
) -> None:
    """A lost start acknowledgement must not require manual recovery.

    The Controller never received the start result, but the run is up and
    published.  The lifecycle owner observes that exact run, the phase accepts
    the established effect, and the ordinary final verification confirms it.
    """

    service, operation, start_index = _parked_start_switch(tmp_path, healthy=True)

    for _ in range(4):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        service.tick()

    view = service.get(operation.operation_id)
    assert view.state == "succeeded", view.status_reason
    completed = _result(view).completed_phases
    # The parked start was accepted exactly once, through the ordinary
    # checkpoint, so no effect is repeated by the observation.
    assert completed.count("start") == 1
    assert "final_verify" in completed
    # The start phase was accepted at its own checkpoint, not replayed earlier.
    assert start_index < len(completed)


def test_parked_start_after_observation_deadline_keeps_exact_effect_pending(
    tmp_path: Path,
) -> None:
    """A deadline slows observation but cannot declare an unknown effect failed."""

    service, operation, _start_index = _parked_start_switch(tmp_path, healthy=False)
    now = [NOW]
    service._clock = lambda: now[0]
    assert service.tick() is True
    now[0] += timedelta(seconds=121)
    assert service.tick() is True

    view = service.get(operation.operation_id)
    assert view.state == LifecycleState.OBSERVING
    assert view.progress.state == LifecycleState.OBSERVING
    assert view.result is not None and view.next_attempt_at is not None
    # One retry clock: the lifecycle core's bounded backoff, never past a minute.
    assert now[0] < view.next_attempt_at <= now[0] + timedelta(seconds=60)
    assert service.tick() is False
    assert isinstance(service._lifecycle, _ObservingLifecycle)
    service._lifecycle._healthy = True
    now[0] = view.next_attempt_at
    for _ in range(4):
        service.tick()
    assert service.get(operation.operation_id).state == "succeeded"


def test_legacy_operator_wait_without_a_clock_is_healed_not_left_waiting(
    tmp_path: Path,
) -> None:
    """No Run/Switch action exists, so a wait for an operator can never end.

    A ``needs-operator`` row with no observation clock is
    re-evaluated by its first advance (core rules 1 and 3): it is retried at the
    core's bounded backoff, then observes its child again and completes.
    """

    service, operation, _start_index = _parked_start_switch(tmp_path, healthy=False)
    now = [NOW]
    service._clock = lambda: now[0]
    with service._sessions.begin() as session:
        row = session.get(Job, operation.operation_id)
        assert row is not None and isinstance(row.result, dict)
        row.state = "needs-operator"
        result = dict(row.result)
        result.pop("observation_due_at", None)
        row.result = result

    held = service.get(operation.operation_id)
    assert held.state == LifecycleState.NEEDS_OPERATOR
    assert service.tick() is True  # healed: it is observed again, not parked
    healed = service.get(operation.operation_id)
    assert healed.state != LifecycleState.NEEDS_OPERATOR
    assert healed.result is not None and healed.next_attempt_at is not None
    assert healed.next_attempt_at > now[0]


def test_parked_start_still_progressing_is_observed_before_final_success(
    tmp_path: Path,
) -> None:
    """A lost start result during a real load is not a failed launch."""

    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    now = [NOW]
    service._clock = lambda: now[0]

    assert service.tick() is True
    held = service.get(operation.operation_id)
    assert held.state == "running"
    assert held.result is not None
    assert held.result.child_operation_id is not None
    assert held.next_attempt_at is not None

    assert service.tick() is False
    assert isinstance(service._lifecycle, _ObservingLifecycle)
    service._lifecycle._healthy = True
    now[0] += timedelta(seconds=5)
    for _ in range(4):
        service.tick()
    assert service.get(operation.operation_id).state == "succeeded"


def test_restart_interrupted_start_keeps_exact_child_when_effect_is_uncertain(
    tmp_path: Path,
) -> None:
    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    with service._sessions.begin() as session:
        parent = session.get(Job, operation.operation_id)
        assert parent is not None and isinstance(parent.result, dict)
        child_id = parent.result.get("child_operation_id")
        assert isinstance(child_id, str)
        child = session.get(Job, child_id)
        assert child is not None
        owner_id = child.payload.get("owner_id")
        assert isinstance(owner_id, str)
        run = session.get(RecipeRun, owner_id)
        assert run is not None
        run.state = "lost"
        for node in session.scalars(select(RunNode).where(RunNode.run_id == owner_id)):
            node.state = "failed"

    assert service.tick() is True
    view = service.get(operation.operation_id)
    assert view.state == LifecycleState.OBSERVING
    assert view.progress.state == LifecycleState.OBSERVING
    assert view.result is not None
    assert view.result.child_operation_id == child_id


def test_restart_retry_due_is_projected_without_replacing_the_child(
    tmp_path: Path,
) -> None:
    service, operation, _ = _parked_start_switch(tmp_path, healthy=False)
    due = NOW + timedelta(seconds=7)
    with service._sessions.begin() as session:
        parent = session.get(Job, operation.operation_id)
        assert parent is not None and isinstance(parent.result, dict)
        child_id = parent.result.get("child_operation_id")
        assert isinstance(child_id, str)
        child = session.get(Job, child_id)
        assert child is not None
        child.state = "queued"
        agent_child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == child_id)
        )
        assert agent_child is not None
        agent_child.state = "backoff"
        agent_child.next_action_at = due
        agent_child.status_reason = "exact lifecycle retry scheduled"

    assert service.tick() is True
    view = service.get(operation.operation_id)
    assert view.state == "running"
    assert view.result is not None
    assert view.result.child_operation_id == child_id
    assert view.next_attempt_at == due


def test_scoped_cleanup_is_allowed_without_launch_readiness(tmp_path: Path) -> None:
    """Removing work must not depend on being able to start work.

    The launch planner is made unavailable, so a removal plan that consulted
    launch readiness would fail here exactly as it would on a Spark whose
    admission evidence has aged out.
    """

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("launch admission is unavailable")

    lifecycle.preview_run = unavailable  # type: ignore[method-assign]
    try:
        cleanup = service.preview_cleanup(installation.owner_id, actor="admin")
    finally:
        del lifecycle.preview_run

    assert cleanup.allowed is True, [reason.code for reason in cleanup.blockers]
    assert cleanup.action == "cleanup"
    assert [phase.kind for phase in cleanup.phases] == ["uninstall", "final_verify"]


def test_scoped_cleanup_is_blocked_while_a_run_is_active(tmp_path: Path) -> None:
    """The uninstall assessment stays the authority for whether removal may run."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    run_plan = lifecycle.preview_run(installation.owner_id, "qwen")
    lifecycle.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        RecordingArtifactExecutor(),
    )

    cleanup = service.preview_cleanup(installation.owner_id, actor="admin")

    assert cleanup.allowed is False
