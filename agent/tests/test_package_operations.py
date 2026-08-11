from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from vonk_agent.operations import (
    InspectionDisposition,
    OperationContext,
    OperationRegistry,
    UnsupportedOperation,
)
from vonk_agent.package_operations import PackageDisposition, PackageInspection
from vonk_agent.state import AgentStateStore
from vonk_agent_protocol import (
    AgentClaim,
    AgentDirective,
    AgentOperation,
    AgentProgress,
    PackageOperationRequest,
    canonical_message,
)

NODE_ID = "spk_0123456789abcdef0123456789abcdef"
PAYLOAD = {
    "schema_version": 1,
    "deployment_id": "future-family-deployment",
    "release_digest": "a" * 64,
    "deployment_digest": "b" * 64,
}


def package_claim(
    operation: AgentOperation = AgentOperation.PACKAGE_PREPARE,
    payload: dict[str, object] | None = None,
) -> AgentClaim:
    body = PAYLOAD if payload is None else payload
    return AgentClaim(
        schema_version=1,
        job_id="11111111-1111-4111-8111-111111111111",
        operation_id="22222222-2222-4222-8222-222222222222",
        attempt=1,
        fence="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        node_id=NODE_ID,
        operation=operation,
        base_commit="c" * 40,
        payload_digest=hashlib.sha256(canonical_message(body)).hexdigest(),
        payload=body,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


class NeverProbe:
    def collect(self, deadline):
        raise AssertionError("package operation reached probe boundary")


class RecordingPackages:
    def __init__(self) -> None:
        self.executions = []
        self.inspections = []

    def execute(self, request, binding, deadline):
        self.executions.append((request, binding, deadline))
        return {
            "deployment_id": request.deployment_id,
            "release_digest": request.release_digest,
            "status": "prepared",
        }

    def inspect(self, request, binding, deadline):
        self.inspections.append((request, binding, deadline))
        return PackageInspection(
            PackageDisposition.SAFE_TO_RETRY,
            {"status": "not-started"},
        )


def context(tmp_path: Path, packages=None) -> OperationContext:
    return OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "state"),
        probe=NeverProbe(),
        packages=packages,
    )


def test_registry_dispatches_generic_package_request_with_exact_binding(
    tmp_path: Path,
) -> None:
    packages = RecordingPackages()
    claim = package_claim()

    execution = OperationRegistry().execute(claim, context(tmp_path, packages))

    request, binding, _deadline = packages.executions[0]
    assert isinstance(request, PackageOperationRequest)
    assert request.deployment_id == "future-family-deployment"
    assert binding.job_id == claim.job_id
    assert binding.operation_id == claim.operation_id
    assert binding.attempt == claim.attempt
    assert binding.fence == claim.fence
    assert binding.node_id == claim.node_id
    assert execution.result.result["evidence"]["status"] == "prepared"


def test_registry_rejects_package_operation_when_boundary_is_unavailable(
    tmp_path: Path,
) -> None:
    with pytest.raises(UnsupportedOperation, match="package operations"):
        OperationRegistry().inspect(package_claim(), context(tmp_path))


def test_registry_uses_package_inspection_for_interrupted_attempt(
    tmp_path: Path,
) -> None:
    packages = RecordingPackages()
    claim = package_claim()
    operation_context = context(tmp_path, packages)
    operation_context.state.begin(claim)

    inspection = OperationRegistry().inspect(claim, operation_context)

    assert inspection.disposition is InspectionDisposition.SAFE_TO_RETRY
    assert inspection.evidence == {"status": "not-started"}
    assert len(packages.inspections) == 1


def test_package_gc_dispatches_without_release_or_family_catalog_fields(
    tmp_path: Path,
) -> None:
    packages = RecordingPackages()
    claim = package_claim(
        AgentOperation.PACKAGE_GC,
        {"schema_version": 1, "dry_run": True, "target_bytes": 4096},
    )

    OperationRegistry().execute(claim, context(tmp_path, packages))

    request = packages.executions[0][0]
    assert request.deployment_id is None
    assert request.target_bytes == 4096


def test_authenticated_cancellation_directive_is_durable_across_restart(
    tmp_path: Path,
) -> None:
    claim = package_claim()
    state_root = tmp_path / "state"
    state = AgentStateStore(state_root)
    state.begin(claim)
    progress = AgentProgress(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        progress={"phase": "downloading"},
    )
    directive = AgentDirective(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline + timedelta(seconds=30),
        cancel_requested=True,
    )

    state.apply_directive(progress, directive)

    restarted = AgentStateStore(state_root)
    assert restarted.cancellation_requested(claim) is True
    active = restarted.lookup_exact(claim)
    assert active is not None and active.progress is not None
    assert active.progress.deadline == directive.deadline


def test_cancellation_directive_must_match_the_active_fence(tmp_path: Path) -> None:
    claim = package_claim()
    state = AgentStateStore(tmp_path / "state")
    state.begin(claim)
    progress = AgentProgress(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        progress={"phase": "downloading"},
    )
    directive = AgentDirective(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        node_id=claim.node_id,
        deadline=claim.deadline + timedelta(seconds=30),
        cancel_requested=True,
    )

    with pytest.raises(Exception, match="directive"):
        state.apply_directive(progress, directive)

    assert state.cancellation_requested(claim) is False
