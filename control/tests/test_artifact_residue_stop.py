"""An expired cancellation budget cannot prove an exact job target stopped."""

import hashlib
from collections.abc import Callable
from datetime import datetime

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import event, select, text
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    ContainerRuntimeAction,
    canonical_message,
    host_helper_grant_signing_bytes,
)
from vonk_agent_protocol.host_helper import (
    ExecuteContainerRuntimeRequestOperation,
    HostRuntimeRequest,
    SignedHostHelperGrant,
)
from vonk_agent_protocol.recipe_operations import RecipeStopPayload, RecipeStopResult
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.artifact_jobs import ArtifactJobInvalid
from vonk_control.host_helper_authority import (
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentOperation,
    Job,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.recipe_operations import RecipeRetryLater

from .runtime_identity_support import claim_agent
from .test_artifact_job_lifecycle import CANCEL_KEY, _drive, _issued_job
from .test_artifact_jobs import cancellation_result, submitted_artifact_job


@pytest.mark.parametrize("damaged_evidence", [False, True, "omitted"])
def test_unknown_cancelled_job_retains_run_claims_until_exact_stop_receipt(
    tmp_path,
    damaged_evidence,
    grant_observer: Callable[[SignedHostHelperGrant, datetime], None] | None = None,
):
    """Logical Stop must dispatch cleanup instead of blessing an unknown effect."""
    sessions, operations, artifacts, agent_jobs, clock, job, claim, run_id = (
        _issued_job(tmp_path, 880)
    )
    operations._clock = clock
    artifacts._clock = clock
    artifacts.cancel(
        job.id, actor="operator", request_id=CANCEL_KEY, reason="stop unknown work"
    )
    agent_jobs.record_result(
        cancellation_result(
            claim,
            job,
            state="waiting-for-operator",
            reason="the exact runtime target could not be confirmed stopped",
        )
    )
    assert (
        _drive(artifacts, agent_jobs, clock, job.id, until="cancelled") == "cancelled"
    )
    ended = artifacts.get(job.id)
    assert ended.result_evidence is not None
    assert ended.result_evidence.active_scope_may_remain is True

    if damaged_evidence:
        document = ended.result_evidence.model_dump(mode="json", exclude_none=True)
        if damaged_evidence == "omitted":
            # The remaining canonical failure cause still reports an unconfirmed
            # physical stop. Omission cannot turn that into observed absence.
            document.pop("active_scope_may_remain")
        else:
            document = {"elapsed_milliseconds": "damaged"}
        with sessions.begin() as session:
            # Damage only the persisted observation. The malformed branch goes
            # below the strict writer guard; omission remains schema-valid.
            session.execute(
                text(
                    "UPDATE artifact_jobs SET result_evidence = :document WHERE id = :id"
                ),
                {
                    "document": canonical_message(document).decode(),
                    "id": job.id,
                },
            )

    with sessions() as session:
        held = tuple(
            session.scalars(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                    ResourceReservation.state.in_(("active", "promised")),
                )
            )
        )
    assert held
    # Logical cancellation has not freed the physical run reservation. A new
    # job on that same run must wait for the exact target receipt too.
    with pytest.raises(ArtifactJobInvalid, match="reservation"):
        submitted_artifact_job(artifacts, run_id, request_suffix=900)
    stop_plan = operations.preview_stop(run_id)
    observations = 0

    def unavailable_once(connection, cursor, statement, parameters, context, many):
        nonlocal observations
        if "FROM artifact_jobs" in statement:
            observations += 1
            if observations == 1:
                raise RecipeRetryLater("exact JobRun source observation is unavailable")

    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", unavailable_once)
    try:
        stopping = operations.stop(
            run_id,
            plan_digest=stop_plan.plan_digest,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000882",
        )
    finally:
        event.remove(engine, "before_cursor_execute", unavailable_once)
    assert observations >= 2
    # No successful job-target Stop receipt has arrived. A terminal cancellation
    # and a newer workload ordinal are decisions, not observations of absence.
    assert stopping.state != "succeeded"
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state != "stopped"
        assert (
            tuple(
                session.scalars(
                    select(ResourceReservation.id).where(
                        ResourceReservation.id.in_(held),
                        ResourceReservation.state.in_(("active", "promised")),
                    )
                )
            )
            == held
        )
        accepted = session.get(Job, stopping.id)
        assert accepted is not None
        manifest = canonical_message(accepted.payload)
        node_id = accepted.targets[0]
        installation_id = run.installation_id

    # A new service owner resumes the persisted accepted Stop. It must adopt
    # the same target manifest instead of replanning or broadening its effects.
    restarted = AgentJobService(sessions, clock=clock)

    def consume(session, operation, attempt, message):
        artifacts.consume_agent_result(session, operation, attempt, message)
        operations.consume_agent_result(session, operation, attempt, message)

    restarted.set_result_consumer(consume)
    operations._agent_jobs = restarted
    replay = operations.stop(
        run_id,
        plan_digest=stop_plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000882",
    )
    assert replay.id == stopping.id
    with sessions() as session:
        accepted = session.get(Job, replay.id)
        assert accepted is not None and canonical_message(accepted.payload) == manifest
    exact = claim_agent(restarted, node_id, "serial-0")
    assert exact is not None and exact.fence != claim.fence
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == stopping.id)
        )
        assert child is not None
        payload = RecipeStopPayload.model_validate_json(
            canonical_message(child.payload)
        )
    assert payload.target_runtime_id == job.id and payload.run_id == run_id
    request = HostRuntimeRequest(
        action="stop",
        fence=exact.fence,
        arguments=[],
        run_generation=payload.run_generation,
        stop_plan=payload,
    )
    signer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(bytes([19]) * 32), clock=clock
    )
    grant = HostRuntimeAuthorityService(sessions, signer, clock=clock).issue_grant(
        node_id=node_id,
        certificate_serial="serial-0",
        fence=exact.fence,
        action=ContainerRuntimeAction.STOP,
        request_sha256=hashlib.sha256(canonical_message(request)).hexdigest(),
        stop_plan_sha256=hashlib.sha256(canonical_message(payload)).hexdigest(),
        run_generation=payload.run_generation,
        runtime_run_id=payload.run_id,
        runtime_target_id=payload.target_runtime_id,
        runtime_installation_id=payload.installation_id,
    )
    assert isinstance(grant.claims.operation, ExecuteContainerRuntimeRequestOperation)
    assert grant.claims.operation.runtime_target_id == job.id
    signer.public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )
    grant_observed_at = clock()
    restarted.record_result(
        AgentResult(
            fence=exact.fence,
            state=AgentResultState.SUCCEEDED,
            result=RecipeStopResult(),
        )
    )
    with pytest.raises(StaleAgentAttempt):
        restarted.record_result(
            cancellation_result(
                claim, job, state="cancelled", reason="late old receipt"
            )
        )
    stopped = operations.stop(
        run_id,
        plan_digest=stop_plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000882",
    )
    assert stopped.id == stopping.id and stopped.state == "succeeded"
    resolved = artifacts.get(job.id)
    assert resolved.result_evidence is not None
    assert resolved.result_evidence.active_scope_may_remain is False
    assert resolved.result_evidence.residue_resolved_by == "exact-stop"
    if damaged_evidence is True:
        assert resolved.result_evidence.elapsed_milliseconds is None
        assert resolved.result_evidence.peak_memory_bytes is None
    elif damaged_evidence == "omitted":
        assert resolved.result_evidence.elapsed_milliseconds == 10
    with sessions() as session:
        assert not tuple(
            session.scalars(
                select(ResourceReservation.id).where(
                    ResourceReservation.id.in_(held),
                    ResourceReservation.state.in_(("active", "promised")),
                )
            )
        )
    InventoryRepository(sessions, clock=clock).record(
        InventorySnapshotInput(
            node_id,
            clock(),
            10_000,
            8_000,
            10_000,
            8_000,
            10_000,
            8_000,
            1,
            False,
            ("runtime.vonk.v1", "recipe.image.pull.v1", "recipe.operations.v1"),
            memory_pool="shared",
        )
    )
    plan = operations.preview_run(installation_id, "image-job")
    assert plan.allowed
    fresh_run = operations.activate_job_run(
        plan,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000883",
    )
    assert fresh_run.owner_id != run_id
    fresh = submitted_artifact_job(artifacts, fresh_run.owner_id, request_suffix=884)
    fresh_claim = claim_agent(restarted, node_id, "serial-0")
    assert fresh_claim is not None and fresh_claim.fence not in {
        claim.fence,
        exact.fence,
    }
    with sessions() as session:
        current = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == fresh.operation_id
            )
        )
        assert current is not None and current.state == "running"

    if grant_observer is not None:
        grant_observer(grant, grant_observed_at)
