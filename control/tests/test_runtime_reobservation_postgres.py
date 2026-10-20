"""Runtime custody adopts only the exact surviving owner on PostgreSQL."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentClaim,
    AgentResult,
    AgentResultState,
    LifecycleState,
    ObservationCause,
    RecipeJobEvidence,
    RecipeJobOutputManifest,
    RecipeJobRunRequest,
    RecipeJobRunResult,
    RecipeStartResult,
    RecipeStopResult,
    canonical_message,
    recipe_job_manifest_sha256,
)
from vonk_agent_protocol import (
    AgentOperation as Kind,
)
from vonk_agent_protocol.contracts import AgentPayload, AgentResultPayload
from vonk_control.agent_jobs import AgentJobService, StaleAgentFence
from vonk_control.lifecycle.agent_operation import AgentOperationAdapter
from vonk_control.models import AgentNode, AgentOperation, AgentOperationAttempt, Job

from . import test_agent_jobs_postgres as fixtures
from .runtime_identity_support import claim_agent
from .test_agent_jobs_postgres import COMMIT, NODE_A, parent


@pytest.fixture
def service(postgres_engine):
    return fixtures.agent_queue_world(postgres_engine)


RUNTIME_KINDS = (Kind.RECIPE_START, Kind.RECIPE_STOP, Kind.RECIPE_JOB_RUN)


def _runtime(kind: Kind) -> tuple[AgentPayload, AgentResultPayload]:
    raw = (
        Path(__file__).parents[2]
        / "agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ).read_bytes()
    claim = AgentClaim.model_validate_json(raw)
    if kind is Kind.RECIPE_START:
        from vonk_agent_protocol import RecipeStartPayload

        from .test_agent_jobs import canonical_start_payload
        from .test_agent_jobs_postgres import Clock

        return RecipeStartPayload.model_validate_json(
            canonical_message(
                canonical_start_payload(
                    start_deadline=Clock().now + timedelta(minutes=5)
                )
            )
        ), RecipeStartResult()
    if kind is Kind.RECIPE_STOP:
        assert isinstance(claim.payload, RecipeJobRunRequest)
        from .agent_grant_requests import own_stop_payload

        return own_stop_payload(
            claim.payload, cancel_pending_start=True
        ), RecipeStopResult()
    if kind is Kind.RECIPE_JOB_RUN:
        assert isinstance(claim.payload, RecipeJobRunRequest)
        return claim.payload, RecipeJobRunResult(
            job_id=claim.payload.job_id,
            run_id=claim.payload.run_id,
            exit_code=0,
            evidence=RecipeJobEvidence(elapsed_milliseconds=0, peak_memory_bytes=None),
            output_manifest=RecipeJobOutputManifest(
                schema_version=1,
                files=(),
                total_bytes=0,
                manifest_sha256=recipe_job_manifest_sha256(()),
            ),
        )
    if kind is Kind.RECIPE_START:
        return claim.payload, RecipeStartResult()
    return claim.payload, RecipeStopResult()


def _accepted(service, kind: Kind = Kind.RECIPE_START):
    sessions, clock = service
    queue = AgentJobService(sessions, clock=clock)
    owner = parent(sessions, clock)
    payload, result = _runtime(kind)
    operation = queue.enqueue(owner.id, NODE_A, kind, COMMIT, payload)
    if kind in RUNTIME_KINDS:
        # Inject a crash after the real lifecycle claim owner committed.
        # Full runtime admission is covered by the cancellation workflow below.
        claim = AgentClaim(
            fence=str(uuid4()),
            operation=kind,
            payload=payload,
            deadline=clock.now + timedelta(seconds=30),
        )
        with sessions.begin() as session:
            row = session.get(AgentOperation, operation.id)
            job = session.get(Job, owner.id)
            assert row is not None and job is not None
            attempt = AgentOperationAdapter(session).start_attempt(
                row,
                job,
                "serial-a",
                claim.fence,
                claim.deadline,
                clock.now,
                progress=None,
            )
            assert attempt is not None
    else:
        claim = claim_agent(queue, NODE_A, "serial-a")
        assert claim is not None
    receipt = AgentResult(
        fence=claim.fence,
        state=AgentResultState.SUCCEEDED,
        result=result,
    )
    clock.advance(seconds=60)
    return queue, sessions, clock, owner, operation, claim, receipt


@pytest.mark.parametrize("kind", RUNTIME_KINDS)
def test_lost_runtime_report_adopts_after_lease_without_reexecution_or_renewal(
    service, kind
):
    # Wrong implementation: only distribution could adopt a completed effect;
    # an installed child was reissued after its report was lost.
    queue, sessions, _clock, _owner, operation, claim, receipt = _accepted(
        service, kind
    )
    with sessions() as session:
        before = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert before is not None
        deadline = before.lease_deadline
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        contact = node.last_seen_at
    assert queue.record_late_result(receipt)
    with sessions() as session:
        row = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert row is not None and row.state == LifecycleState.SUCCEEDED
        assert attempt is not None
        assert attempt.lease_deadline == deadline
        node = session.get(AgentNode, NODE_A)
        assert node is not None and node.last_seen_at == contact
    assert claim_agent(queue, NODE_A, "serial-a") is None


@pytest.mark.parametrize("kind", RUNTIME_KINDS)
@pytest.mark.parametrize(
    "change", ["identity", "revoked", "attempt", "cancel", "owner"]
)
def test_late_runtime_receipt_rejects_changed_authority(service, kind, change):
    # Wrong implementation: a syntactically valid receipt overwrote a newer
    # attempt, cancelled parent, revoked node or changed effect identity.
    queue, sessions, clock, owner, operation, _claim, receipt = _accepted(service, kind)
    with sessions.begin() as session:
        row = session.get(AgentOperation, operation.id)
        node = session.get(AgentNode, NODE_A)
        job = session.get(Job, owner.id)
        assert row is not None and node is not None and job is not None
        if change == "identity":
            row.authority_revision = "f" * 64
        elif change == "revoked":
            node.revoked_at = clock.now
        elif change == "attempt":
            row.current_attempt += 1
        elif change == "cancel":
            job.state = LifecycleState.CANCELLED
        else:
            node.workload_intent_ordinal += 1
    try:
        queue.record_late_result(receipt)
    except StaleAgentFence as error:
        assert str(error)
    with sessions() as session:
        row = session.get(AgentOperation, operation.id)
        assert row is not None and row.state != LifecycleState.SUCCEEDED


@pytest.mark.parametrize("kind", RUNTIME_KINDS)
@pytest.mark.parametrize("parked", [False, True])
def test_worker_adopts_retained_runtime_receipt_without_contact(service, kind, parked):
    # Wrong implementation: adoption ran only at ingress, so a retained
    # authenticated receipt on a completely idle fleet stayed parked forever.
    queue, sessions, clock, _owner, operation, claim, receipt = _accepted(service, kind)
    with sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None
        if parked:
            row = session.get(AgentOperation, operation.id)
            assert row is not None
            row.state = LifecycleState.OBSERVING
        attempt.state = LifecycleState.OBSERVING
        attempt.observation_cause = ObservationCause.LEASE_LAPSED
        attempt.result = receipt.result.model_dump(mode="json")
    queue = AgentJobService(sessions, clock=clock)
    assert queue.reconcile_orders()
    with sessions() as session:
        row = session.get(AgentOperation, operation.id)
        assert row is not None and row.state == LifecycleState.SUCCEEDED


def test_shared_observer_covers_every_kind_except_package_handoff():
    # Wrong implementation: a newly added enum member silently takes the
    # generic restart-interrupted path instead of an owner probe.
    from vonk_control.agent_jobs.observation_receipts import OBSERVATION_KINDS

    assert OBSERVATION_KINDS == set(Kind) - {Kind.AGENT_UPGRADE}


def _cancelled_job(tmp_path, postgres_engine):
    from vonk_agent_protocol import OutcomeKind, OutcomeUnknown, WaitReason

    from .test_artifact_jobs import (
        NOW,
        MutableClock,
        running_artifact_service,
        submitted_artifact_job,
    )

    sessions, operations, _queue, artifacts, run_id, node_id = running_artifact_service(
        tmp_path, engine=postgres_engine
    )
    clock = MutableClock(NOW)
    queue = AgentJobService(sessions, clock=clock)
    operations._agent_jobs = queue
    operations._clock = artifacts._clock = clock

    def consume(session, operation, attempt, message):
        artifacts.consume_agent_result(session, operation, attempt, message)
        operations.consume_agent_result(session, operation, attempt, message)

    queue.set_result_consumer(consume)
    assert claim_agent(queue, node_id, "serial-0") is None
    job = submitted_artifact_job(artifacts, run_id, request_suffix=880)
    claim = claim_agent(queue, node_id, "serial-0")
    assert claim is not None
    artifacts.cancel(
        job.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000301",
        reason="stop unknown work",
    )
    queue.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.OBSERVING,
            result=OutcomeUnknown(
                kind=OutcomeKind.UNKNOWN,
                wait_reason=WaitReason.JOB_STOP_UNCONFIRMED,
                reason="exact job stop is unread",
            ),
        )
    )
    for _ in range(40):
        if artifacts.get(job.id).state == LifecycleState.CANCELLED:
            break
        clock.advance(seconds=120)
        queue.reconcile_orders()
    assert artifacts.get(job.id).state == LifecycleState.CANCELLED
    return sessions, operations, artifacts, queue, clock, job, run_id, node_id


def test_terminal_uncertain_cancel_independently_drives_only_its_exact_stop(
    tmp_path, postgres_engine
):
    # Wrong implementation: terminal cancellation is excluded from idle worker
    # discovery, so its runtime is stopped only after another operator request.
    from vonk_agent_protocol import RecipeStopPayload, RunState
    from vonk_control.models import ArtifactJob, RecipeRun

    sessions, operations, artifacts, queue, _clock, job, run_id, node_id = (
        _cancelled_job(tmp_path, postgres_engine)
    )
    with sessions() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        ordinal = node.workload_intent_ordinal
    assert operations.reconcile_pending_service_stops()
    assert not operations.reconcile_pending_service_stops()
    stop = claim_agent(queue, node_id, "serial-0")
    with sessions() as session:
        debug = [
            (row.kind, row.state, row.status_reason)
            for row in session.scalars(select(AgentOperation))
        ]
    assert stop is not None and stop.operation == Kind.RECIPE_STOP, debug
    assert isinstance(stop.payload, RecipeStopPayload)
    assert stop.payload.target_runtime_id == job.id and stop.payload.run_id == run_id
    queue.record_result(
        AgentResult(
            fence=stop.fence,
            state=AgentResultState.SUCCEEDED,
            result=RecipeStopResult(),
        )
    )
    with sessions() as session:
        artifact = session.get(ArtifactJob, job.id)
        run = session.get(RecipeRun, run_id)
        node = session.get(AgentNode, node_id)
        assert artifact is not None and artifact.state == LifecycleState.CANCELLED
        assert run is not None and run.state == RunState.RUNNING
        assert node is not None and node.workload_intent_ordinal == ordinal
    evidence = artifacts.get(job.id).result_evidence
    assert evidence is not None and evidence.active_scope_may_remain is False
    assert claim_agent(queue, node_id, "serial-0") is None


@pytest.mark.parametrize("change", ["owner", "revoked"])
def test_cancel_cleanup_cannot_adopt_newer_or_revoked_node(
    tmp_path, postgres_engine, change
):
    sessions, operations, _artifacts, _queue, clock, _job, _run, node_id = (
        _cancelled_job(tmp_path, postgres_engine)
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        if change == "owner":
            node.workload_intent_ordinal += 1
        else:
            node.revoked_at = clock.now
    assert not operations.reconcile_pending_service_stops()
    with sessions() as session:
        assert (
            session.scalar(
                select(AgentOperation.id).where(AgentOperation.kind == Kind.RECIPE_STOP)
            )
            is None
        )


@pytest.mark.parametrize("exit_code", [0, 3])
def test_worker_recovers_retained_output_receipt_as_delivery_only(service, exit_code):
    # Wrong implementation: only live ingress redrove completed output custody,
    # so a retained receipt needed another report or repeated inference.
    from vonk_agent_protocol import FailureStage
    from vonk_agent_protocol.recipe_jobs import RecipeJobEnding

    queue, sessions, clock, _owner, operation, claim, receipt = _accepted(
        service, Kind.RECIPE_JOB_RUN
    )
    assert isinstance(receipt.result, RecipeJobRunResult)
    completed = receipt.result.model_copy(
        update={
            "exit_code": exit_code,
            "ending": RecipeJobEnding(stage=FailureStage.JOB_OUTPUT_DELIVERY),
        }
    )
    with sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None
        attempt.state = LifecycleState.OBSERVING
        attempt.observation_cause = ObservationCause.LEASE_LAPSED
        attempt.result = completed.model_dump(mode="json")
        deadline = attempt.lease_deadline
    queue = AgentJobService(sessions, clock=clock)
    assert queue.reconcile_orders()
    with sessions() as session:
        row = session.get(AgentOperation, operation.id)
        assert row is not None
        from vonk_control.agent_jobs.stored import column_value

        request = column_value(row, "payload")
        assert isinstance(request, RecipeJobRunRequest) and request.output_delivery_only
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None and attempt.lease_deadline == deadline


@pytest.mark.parametrize("change", ["owner", "revoked", "identity"])
def test_accepted_cancel_stop_rechecks_authority_before_grant_and_receipt(
    tmp_path, postgres_engine, change
):
    # Wrong implementation: ordinary own-effect cleanup may outlive newer
    # intent; an inherited cancellation must not capture that newer owner.
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from vonk_agent_protocol import ContainerRuntimeAction, RecipeStopPayload
    from vonk_control.host_helper_authority import (
        HostHelperAuthorityError,
        HostHelperGrantIssuer,
        HostRuntimeAuthorityService,
    )

    from .agent_grant_requests import stop_grant_request

    sessions, operations, artifacts, queue, clock, job, run_id, node_id = (
        _cancelled_job(tmp_path, postgres_engine)
    )
    assert operations.reconcile_pending_service_stops()
    stop = claim_agent(queue, node_id, "serial-0")
    assert stop is not None and isinstance(stop.payload, RecipeStopPayload)
    request = stop_grant_request(stop.payload, fence=stop.fence)
    authority = HostRuntimeAuthorityService(
        sessions,
        HostHelperGrantIssuer(
            ed25519.Ed25519PrivateKey.from_private_bytes(bytes([19]) * 32), clock=clock
        ),
        clock=clock,
    )
    arguments = request.model_dump(mode="python", exclude={"action"})
    # Positive witness: this exact accepted cancellation reaches signed Stop.
    grant = authority.issue_grant(
        node_id=node_id,
        certificate_serial="serial-0",
        action=ContainerRuntimeAction.STOP,
        **arguments,
    )
    from vonk_agent_protocol.host_helper import ExecuteContainerRuntimeRequestOperation

    assert isinstance(grant.claims.operation, ExecuteContainerRuntimeRequestOperation)
    assert grant.claims.operation.runtime_target_id == stop.payload.target_runtime_id
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        if change == "owner":
            node.workload_intent_ordinal += 1
        elif change == "revoked":
            node.revoked_at = clock.now
        else:
            operation = session.scalar(
                select(AgentOperation).where(AgentOperation.kind == Kind.RECIPE_STOP)
            )
            assert operation is not None
            operation.payload = stop.payload.model_copy(
                update={"target_runtime_id": str(uuid4())}
            ).model_dump(mode="json")
    with pytest.raises(HostHelperAuthorityError):
        authority.issue_grant(
            node_id=node_id,
            certificate_serial="serial-0",
            action=ContainerRuntimeAction.STOP,
            **arguments,
        )
    with pytest.raises(StaleAgentFence):
        queue.record_result(
            AgentResult(
                fence=stop.fence,
                state=AgentResultState.SUCCEEDED,
                result=RecipeStopResult(),
            )
        )

    # A changed accepted owner ends the inherited cleanup through the normal
    # bounded lifecycle, retaining the original job's unproven physical scope.
    for _ in range(40):
        clock.advance(seconds=120)
        queue.reconcile_orders()
        with sessions() as session:
            operation = session.scalar(
                select(AgentOperation).where(AgentOperation.kind == Kind.RECIPE_STOP)
            )
            assert operation is not None
            if operation.state == LifecycleState.CANCELLED:
                break
    assert operation.state == LifecycleState.CANCELLED
    evidence = artifacts.get(job.id).result_evidence
    assert evidence is not None and evidence.active_scope_may_remain

    # A refused historical cleanup does not poison fresh job authoring.
    from .test_artifact_jobs import artifact_create_request, create_artifact_job

    fresh = create_artifact_job(
        artifacts,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000999"),
    )
    assert fresh.id != job.id and fresh.operation_id is None


@pytest.mark.parametrize("kind", [Kind.RECIPE_START, Kind.RECIPE_STOP])
@pytest.mark.parametrize("retained", [False, True])
def test_definite_runtime_exit_or_refusal_survives_lost_report(service, kind, retained):
    # Wrong implementation: the observer's definite exit/refusal becomes an
    # unending observation after the lease, or another effect attempt.
    from vonk_agent_protocol import AgentFailureKind, AgentFailureResult, FailureCode

    queue, sessions, clock, _owner, operation, claim, _receipt = _accepted(
        service, kind
    )
    receipt = AgentResult(
        fence=claim.fence,
        state=AgentResultState.FAILED,
        result=AgentFailureResult(
            status=LifecycleState.FAILED.value,
            reason="exact runtime evidence refused completion",
            error_code=FailureCode.RECIPE_START_FAILED
            if kind is Kind.RECIPE_START
            else FailureCode.RECIPE_STOP_FAILED,
            failure_kind=AgentFailureKind.INVALID_CONTRACT,
        ),
    )
    if retained:
        with sessions.begin() as session:
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.fence == claim.fence
                )
            )
            assert attempt is not None
            attempt.state = LifecycleState.OBSERVING
            attempt.observation_cause = ObservationCause.LEASE_LAPSED
            attempt.result = receipt.result.model_dump(mode="json")
        queue = AgentJobService(sessions, clock=clock)
        assert queue.reconcile_orders()
    else:
        assert queue.record_late_result(receipt)
    with sessions() as session:
        row = session.get(AgentOperation, operation.id)
        assert (
            row is not None
            and row.state == LifecycleState.FAILED
            and row.current_attempt == 1
        )

        from vonk_control.agent_jobs.stored import column_value

        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None
        stored = column_value(attempt, "result")
        assert isinstance(stored, AgentFailureResult)
        reason_code = stored.error_code
        expected_reason = (
            FailureCode.RECIPE_START_FAILED
            if kind is Kind.RECIPE_START
            else FailureCode.RECIPE_STOP_FAILED
        )
        assert reason_code == expected_reason
