"""Accepted profile cleanup must reach the signed exact-target helper boundary."""

import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from vonk_agent_protocol import (
    ContainerRuntimeAction,
    canonical_message,
    host_helper_grant_signing_bytes,
)
from vonk_agent_protocol.host_helper import (
    ExecuteContainerRuntimeRequestOperation,
    HostRuntimeRequest,
)
from vonk_agent_protocol.recipe_operations import RecipeStopPayload
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.host_helper_authority import (
    HostHelperAuthorityError,
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.models import AgentOperation, Job
from vonk_control.run_switch_operations import RunSwitchOperationService

from .runtime_identity_support import claim_agent
from .test_artifact_job_lifecycle import _issued_job
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


def _selected_profile_stop_grant(
    tmp_path, reason="superseded by newer workload intent"
):
    """Cross real profile acceptance, issued child claim and signed helper grant."""
    sessions, lifecycle, _artifacts, agent_jobs, clock, artifact, _claim, run_id = (
        _issued_job(tmp_path, 890)
    )
    lifecycle._clock = clock
    switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    profiles = build_production_fleet_profile_service(
        sessions, clock=clock, run_switch_operations=switch
    )
    profile = profiles.create(
        FleetProfileInput(name="Stop issued artifact", assignments=[]), actor="admin"
    )
    accepted = profiles.load(
        profile.number,
        actor="admin",
        request_key="00000000-0000-4000-8000-000000000893",
    )
    stop = None
    for _ in range(8):
        profiles.tick()
        switch.tick()
        with sessions() as session:
            stop = session.scalar(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["execution_mode"].as_string() == "profile-jobrun-stop",
                )
            )
        if stop is not None:
            break
    assert stop is not None, profiles.application(accepted.id)
    assert stop.payload["profile_application_id"] == accepted.id
    # Human diagnostic wording is never part of exact cancellation authority.
    from vonk_control.stored_json import write_guard_mode

    with write_guard_mode(strict=False), sessions.begin() as session:
        source = session.get(Job, artifact.operation_id)
        assert source is not None and isinstance(source.result, dict)
        source.result = {**source.result, "reason": reason}
    node_id = stop.targets[0]
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == stop.id)
        )
        assert child is not None
        payload = RecipeStopPayload.model_validate_json(
            canonical_message(child.payload)
        )
    assert payload.target_runtime_id == artifact.id
    assert payload.run_id == run_id
    request = HostRuntimeRequest(
        action="stop",
        fence=claim.fence,
        arguments=[],
        run_generation=payload.run_generation,
        stop_plan=payload,
    )
    issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(bytes([19]) * 32), clock=clock
    )
    authority = HostRuntimeAuthorityService(sessions, issuer, clock=clock)
    grant = authority.issue_grant(
        node_id=node_id,
        certificate_serial="serial-0",
        fence=claim.fence,
        action=ContainerRuntimeAction.STOP,
        request_sha256=hashlib.sha256(canonical_message(request)).hexdigest(),
        stop_plan_sha256=hashlib.sha256(canonical_message(payload)).hexdigest(),
        run_generation=payload.run_generation,
        runtime_run_id=payload.run_id,
        runtime_target_id=payload.target_runtime_id,
        runtime_installation_id=payload.installation_id,
    )
    signed_operation = grant.claims.operation
    assert isinstance(signed_operation, ExecuteContainerRuntimeRequestOperation)
    assert signed_operation.runtime_target_id == artifact.id
    issuer.public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )
    # Changing only the proposed runtime target cannot broaden the exact Stop.
    with pytest.raises(HostHelperAuthorityError):
        authority.issue_grant(
            node_id=node_id,
            certificate_serial="serial-0",
            fence=claim.fence,
            action=ContainerRuntimeAction.STOP,
            request_sha256=hashlib.sha256(canonical_message(request)).hexdigest(),
            stop_plan_sha256=hashlib.sha256(canonical_message(payload)).hexdigest(),
            run_generation=payload.run_generation,
            runtime_run_id=payload.run_id,
            runtime_target_id=payload.run_id,
            runtime_installation_id=payload.installation_id,
        )
    return grant, clock()


@pytest.mark.parametrize("reason", [None, "translated cancellation explanation"])
def test_selected_profile_stop_issues_signed_exact_job_target_grant(tmp_path, reason):
    """A valid issued profile Stop must not be rejected as a service-run Stop."""
    _selected_profile_stop_grant(tmp_path, reason)
