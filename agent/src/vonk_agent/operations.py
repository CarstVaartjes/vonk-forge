"""Closed dispatch for fenced outbound-agent operations."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from vonk_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProtocolError,
    AgentResult,
    canonical_message,
)

from .deadlines import MonotonicDeadline
from .nvidia_tools import InstalledToolSecurityError
from .probe import ProbeError
from .releases import (
    ReleaseDisposition,
    ReleaseEvidence,
    ReleaseInspection,
    ReleaseInstallError,
    ReleaseRequest,
    ReleaseValidationError,
)
from .state import AgentAttemptRecord, AgentStateConflict, AgentStateStore
from .update import AgentRollbackCommand, AgentUpdateCommand, AgentUpdateError
from .workloads import (
    WorkloadAction,
    WorkloadDisposition,
    WorkloadEvidence,
    WorkloadExecutionError,
    WorkloadInspection,
    WorkloadRequest,
    WorkloadValidationError,
)


class UnsupportedOperation(AgentProtocolError):
    """The compiled agent has no handler for this operation."""


class NodeProbe(Protocol):
    def collect(
        self,
        deadline: datetime | MonotonicDeadline,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class NodeProbeRequest:
    require_zero_compute: bool


class ReleaseInstallerBoundary(Protocol):
    def install(
        self, request: ReleaseRequest, deadline: MonotonicDeadline
    ) -> ReleaseEvidence: ...

    def inspect(
        self, request: ReleaseRequest, deadline: MonotonicDeadline
    ) -> ReleaseInspection: ...


class WorkloadOperationsBoundary(Protocol):
    def execute(
        self,
        request: WorkloadRequest,
        deadline: MonotonicDeadline,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
    ) -> WorkloadEvidence: ...

    def inspect(
        self,
        request: WorkloadRequest,
        deadline: MonotonicDeadline,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
    ) -> WorkloadInspection: ...


class AgentUpdateOperationsBoundary(Protocol):
    def execute(
        self,
        command: AgentUpdateCommand,
        deadline: MonotonicDeadline,
        operation_id: str,
        fence: str,
    ) -> Mapping[str, object]: ...

    def rollback(
        self,
        command: AgentRollbackCommand,
        deadline: MonotonicDeadline,
        operation_id: str,
        fence: str,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class OperationContext:
    node_id: str
    state: AgentStateStore
    probe: NodeProbe
    releases: ReleaseInstallerBoundary | None = None
    workloads: WorkloadOperationsBoundary | None = None
    updates: AgentUpdateOperationsBoundary | None = None


class InspectionDisposition(StrEnum):
    READY = "ready"
    SAFE_TO_RETRY = "safe-to-retry"
    COMPLETED = "completed"
    COMPENSATE = "compensate"
    OPERATOR_INTERVENTION = "operator-intervention"
    UNSUPPORTED = "unsupported"


_WAITING_REASONS = {
    InspectionDisposition.COMPENSATE: "compensation-required",
    InspectionDisposition.OPERATOR_INTERVENTION: "operator-intervention-required",
}


@dataclass(frozen=True)
class OperationInspection:
    disposition: InspectionDisposition
    result: AgentResult | None = None
    canonical_result: bytes | None = None
    evidence: Mapping[str, object] | None = None


@dataclass(frozen=True)
class OperationExecution(Mapping[str, Any]):
    """Immutable execution record that also exposes the result evidence mapping."""

    result: AgentResult
    canonical_result: bytes
    replayed: bool

    def __getitem__(self, key: str) -> Any:
        return self.result.result[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.result.result)

    def __len__(self) -> int:
        return len(self.result.result)


class OperationRegistry:
    """A source-defined registry; it deliberately has no plugin discovery."""

    def execute(
        self,
        claim: AgentClaim,
        context: OperationContext,
        *,
        on_active: Callable[[], None] | None = None,
        execution_deadline: MonotonicDeadline | None = None,
    ) -> OperationExecution:
        active_reported = False

        def report_active() -> None:
            nonlocal active_reported
            if not active_reported and on_active is not None:
                on_active()
                active_reported = True

        request = self._validate(claim, context)
        exact = context.state.lookup_exact(claim)
        if exact is not None and exact.result is not None:
            assert exact.canonical_result is not None
            return OperationExecution(exact.result, exact.canonical_result, True)
        if exact is not None:
            report_active()
            inspection = _inspect_request(claim, request, context)
            if (
                inspection.disposition is InspectionDisposition.COMPLETED
                and inspection.evidence is not None
            ):
                recovered = _result(
                    claim,
                    "succeeded",
                    {"status": "ok", "evidence": inspection.evidence},
                )
                finished = context.state.finish(recovered)
                assert (
                    finished.result is not None
                    and finished.canonical_result is not None
                )
                return OperationExecution(
                    finished.result, finished.canonical_result, True
                )
            waiting_reason = _WAITING_REASONS.get(inspection.disposition)
            if waiting_reason is not None:
                waiting = _result(
                    claim,
                    "waiting-for-operator",
                    {"reason": waiting_reason},
                )
                finished = context.state.finish(waiting)
                assert (
                    finished.result is not None
                    and finished.canonical_result is not None
                )
                return OperationExecution(
                    finished.result, finished.canonical_result, True
                )
            if inspection.disposition is not InspectionDisposition.SAFE_TO_RETRY:
                raise AgentStateConflict(
                    "interrupted mutation requires explicit disposition"
                )
        pending = context.state.recover_pending()
        if pending is not None:
            _require_exact(pending, claim)
            if pending.result is not None and pending.canonical_result is not None:
                return OperationExecution(
                    pending.result, pending.canonical_result, True
                )
        try:
            if execution_deadline is None:
                execution_deadline = MonotonicDeadline.bind(claim.deadline)
            else:
                execution_deadline.check()
        except Exception as error:
            if exact is None:
                raise AgentProtocolError("claim deadline has expired") from error
            record = context.state.begin(claim)
            if record.result is not None:
                assert record.canonical_result is not None
                return OperationExecution(
                    record.result,
                    record.canonical_result,
                    True,
                )
            report_active()
            expired = _result(
                claim,
                "failed",
                {
                    "status": "failed",
                    "error_code": "claim_deadline_expired",
                },
            )
            finished = context.state.finish(expired)
            assert finished.result is not None
            assert finished.canonical_result is not None
            return OperationExecution(
                finished.result,
                finished.canonical_result,
                exact is not None,
            )
        record = context.state.begin(claim)
        if record.result is not None:
            assert record.canonical_result is not None
            return OperationExecution(record.result, record.canonical_result, True)
        report_active()
        try:
            evidence = _execute_request(
                claim, request, context, execution_deadline
            )
            result = _result(claim, "succeeded", {"status": "ok", "evidence": evidence})
        except Exception as error:  # noqa: BLE001 - closed registry bounds every handler failure
            error_code = _stable_error_code(error, claim.operation)
            result = _result(
                claim,
                "failed",
                {"status": "failed", "error_code": error_code},
            )
        finished = context.state.finish(result)
        assert finished.result is not None and finished.canonical_result is not None
        return OperationExecution(finished.result, finished.canonical_result, False)

    def inspect(
        self, claim: AgentClaim, context: OperationContext
    ) -> OperationInspection:
        request = self._validate(claim, context)
        exact = context.state.lookup_exact(claim)
        if exact is not None:
            if exact.result is None:
                return _inspect_request(claim, request, context)
            return OperationInspection(
                InspectionDisposition.COMPLETED,
                exact.result,
                exact.canonical_result,
            )
        unresolved = context.state.recover_active()
        if unresolved is None:
            unresolved = context.state.recover_pending()
        if unresolved is None:
            return OperationInspection(InspectionDisposition.READY)
        _require_exact(unresolved, claim)
        if unresolved.result is None:
            return _inspect_request(claim, request, context)
        return OperationInspection(
            InspectionDisposition.COMPLETED,
            unresolved.result,
            unresolved.canonical_result,
        )

    @staticmethod
    def _validate(
        claim: AgentClaim, context: OperationContext
    ) -> (
        NodeProbeRequest
        | ReleaseRequest
        | WorkloadRequest
        | AgentUpdateCommand
        | AgentRollbackCommand
    ):
        if type(claim) is not AgentClaim:
            raise UnsupportedOperation("operation is not compiled into this agent")
        if claim.node_id != context.node_id:
            raise AgentProtocolError("claim node does not match this agent")
        if claim.operation is AgentOperation.NODE_PROBE:
            if not claim.payload:
                return NodeProbeRequest(False)
            if claim.payload == {"require_active_nvidia_compute_processes": 0}:
                return NodeProbeRequest(True)
            raise AgentProtocolError("node probe payload is invalid")
        if claim.operation is AgentOperation.RELEASE_INSTALL:
            if context.releases is None:
                raise UnsupportedOperation("release installation is unavailable")
            try:
                return ReleaseRequest.parse(claim.payload)
            except ReleaseValidationError as error:
                raise AgentProtocolError("release payload is invalid") from error
        if claim.operation is AgentOperation.AGENT_UPDATE:
            if context.updates is None:
                raise UnsupportedOperation("agent update is unavailable")
            try:
                return AgentUpdateCommand.parse(claim.payload)
            except AgentUpdateError as error:
                raise AgentProtocolError("agent update payload is invalid") from error
        if claim.operation is AgentOperation.AGENT_ROLLBACK:
            if context.updates is None:
                raise UnsupportedOperation("agent rollback is unavailable")
            try:
                return AgentRollbackCommand.parse(claim.payload)
            except AgentUpdateError as error:
                raise AgentProtocolError("agent rollback payload is invalid") from error
        action = _WORKLOAD_ACTIONS.get(claim.operation)
        if action is not None:
            if context.workloads is None:
                raise UnsupportedOperation("workload operations are unavailable")
            try:
                return WorkloadRequest.parse(action, claim.payload)
            except WorkloadValidationError as error:
                raise AgentProtocolError("workload payload is invalid") from error
        raise UnsupportedOperation("operation is not compiled into this agent")


_WORKLOAD_ACTIONS = {
    AgentOperation.WORKLOAD_PREPARE: WorkloadAction.PREPARE,
    AgentOperation.WORKLOAD_START: WorkloadAction.START,
    AgentOperation.WORKLOAD_STOP: WorkloadAction.STOP,
    AgentOperation.WORKLOAD_HEALTH: WorkloadAction.HEALTH,
    AgentOperation.WORKLOAD_VERIFY: WorkloadAction.VERIFY,
}


def _execute_request(
    claim: AgentClaim,
    request: (
        NodeProbeRequest
        | ReleaseRequest
        | WorkloadRequest
        | AgentUpdateCommand
        | AgentRollbackCommand
    ),
    context: OperationContext,
    deadline: MonotonicDeadline,
) -> Mapping[str, object]:
    if isinstance(request, NodeProbeRequest):
        evidence = context.probe.collect(deadline)
        if request.require_zero_compute:
            health = evidence.get("vonk_forge")
            accelerator = (
                health.get("accelerator") if isinstance(health, Mapping) else None
            )
            count = (
                accelerator.get("active_nvidia_compute_processes")
                if isinstance(accelerator, Mapping)
                else None
            )
            if not isinstance(count, int) or isinstance(count, bool) or count != 0:
                raise ProbeError("node compute occupancy is not clean")
        return evidence
    if isinstance(request, ReleaseRequest):
        assert context.releases is not None
        return context.releases.install(
            request, deadline
        ).to_mapping()
    if isinstance(request, AgentUpdateCommand):
        assert context.updates is not None
        if (
            request.authorization.node_id != claim.node_id
            or request.authorization.attempt != claim.attempt
            or request.authorization.claim_deadline
            != int(claim.deadline.timestamp())
        ):
            raise AgentUpdateError(
                "activation authorization does not match the claimed node lease"
            )
        return context.updates.execute(
            request, deadline, claim.operation_id, claim.fence
        )
    if isinstance(request, AgentRollbackCommand):
        assert context.updates is not None
        if (
            request.authorization.node_id != claim.node_id
            or request.authorization.attempt != claim.attempt
            or request.authorization.claim_deadline
            != int(claim.deadline.timestamp())
        ):
            raise AgentUpdateError(
                "rollback authorization does not match the claimed node lease"
            )
        return context.updates.rollback(
            request, deadline, claim.operation_id, claim.fence
        )
    assert context.workloads is not None
    return context.workloads.execute(
        request,
        deadline,
        claim.job_id,
        claim.operation_id,
        claim.attempt,
        claim.fence,
    ).to_mapping()


def _inspect_request(
    claim: AgentClaim,
    request: (
        NodeProbeRequest
        | ReleaseRequest
        | WorkloadRequest
        | AgentUpdateCommand
        | AgentRollbackCommand
    ),
    context: OperationContext,
) -> OperationInspection:
    if isinstance(request, NodeProbeRequest) or claim.operation in {
        AgentOperation.WORKLOAD_HEALTH,
        AgentOperation.WORKLOAD_VERIFY,
    }:
        return OperationInspection(InspectionDisposition.SAFE_TO_RETRY)
    if isinstance(request, (AgentUpdateCommand, AgentRollbackCommand)):
        return OperationInspection(InspectionDisposition.OPERATOR_INTERVENTION)
    if isinstance(request, ReleaseRequest):
        assert context.releases is not None
        inspection = context.releases.inspect(request, _recovery_deadline())
        mapping = {
            ReleaseDisposition.READY: InspectionDisposition.OPERATOR_INTERVENTION,
            ReleaseDisposition.SAFE_TO_RESUME: InspectionDisposition.SAFE_TO_RETRY,
            ReleaseDisposition.COMPLETED: InspectionDisposition.COMPLETED,
            ReleaseDisposition.OPERATOR_INTERVENTION: InspectionDisposition.OPERATOR_INTERVENTION,
        }
    else:
        assert context.workloads is not None
        inspection = context.workloads.inspect(
            request,
            _recovery_deadline(),
            claim.job_id,
            claim.operation_id,
            claim.attempt,
            claim.fence,
        )
        mapping = {
            WorkloadDisposition.READY: InspectionDisposition.OPERATOR_INTERVENTION,
            WorkloadDisposition.SAFE_TO_RETRY: InspectionDisposition.SAFE_TO_RETRY,
            WorkloadDisposition.COMPLETED: InspectionDisposition.COMPLETED,
            WorkloadDisposition.COMPENSATE: InspectionDisposition.COMPENSATE,
            WorkloadDisposition.OPERATOR_INTERVENTION: InspectionDisposition.OPERATOR_INTERVENTION,
        }
    evidence = None if inspection.evidence is None else inspection.evidence.to_mapping()
    return OperationInspection(mapping[inspection.disposition], evidence=evidence)


def _recovery_deadline() -> MonotonicDeadline:
    return MonotonicDeadline.bind(datetime.now(UTC) + timedelta(seconds=15))


def _require_exact(record: AgentAttemptRecord, claim: AgentClaim) -> None:
    if record.canonical_claim != canonical_message(claim):
        raise AgentStateConflict("claim conflicts with unresolved state")


def _result(
    claim: AgentClaim, state: str, evidence: Mapping[str, Any]
) -> AgentResult:
    return AgentResult(
        schema_version=claim.schema_version,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        state=state,
        result=evidence,
    )


_PROBE_ERROR_CODES = frozenset(
    {
        "probe_failed",
        "probe_timeout",
        "probe_output_limit",
        "probe_result_limit",
        "probe_collector_failed",
        "probe_security_failure",
    }
)


def _stable_error_code(error: Exception, operation: AgentOperation) -> str:
    if operation is AgentOperation.AGENT_UPDATE:
        return "agent_update_failed"
    if operation is AgentOperation.AGENT_ROLLBACK:
        return "agent_rollback_failed"
    if operation is AgentOperation.RELEASE_INSTALL:
        code = getattr(error, "error_code", None)
        return (
            code
            if isinstance(error, ReleaseInstallError)
            and code == "release_install_failed"
            else "release_install_failed"
        )
    if operation in _WORKLOAD_ACTIONS:
        code = getattr(error, "error_code", None)
        return (
            code
            if isinstance(error, WorkloadExecutionError) and code == "workload_failed"
            else "workload_failed"
        )
    if not isinstance(error, (ProbeError, InstalledToolSecurityError)):
        return "probe_failed"
    code = getattr(error, "error_code", None)
    return code if code in _PROBE_ERROR_CODES else "probe_failed"
