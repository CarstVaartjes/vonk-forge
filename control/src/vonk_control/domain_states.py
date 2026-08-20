"""Typed lifecycle states and guarded transitions for the control domain."""

from enum import StrEnum


class InvalidStateTransitionError(ValueError):
    """Raised when a domain state transition is not allowed."""


class OperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentNodeState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


class CertificateState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


_OPERATION_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    OperationState.QUEUED: frozenset(
        {OperationState.RUNNING, OperationState.CANCELLED}
    ),
    OperationState.RUNNING: frozenset(
        {
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
}


def transition_operation(
    current: OperationState, target: OperationState
) -> OperationState:
    """Validate and return an operation state transition."""
    if target not in _OPERATION_TRANSITIONS.get(current, frozenset()):
        raise InvalidStateTransitionError(
            f"invalid operation transition: {current.value} -> {target.value}"
        )
    return target


def revoke_certificate(current: CertificateState) -> CertificateState:
    """Move an active certificate to terminal revocation."""
    if current is not CertificateState.ACTIVE:
        raise InvalidStateTransitionError(
            f"invalid certificate transition: {current.value} -> revoked"
        )
    return CertificateState.REVOKED


def is_active_fleet_member(
    node_state: AgentNodeState, certificate_state: CertificateState
) -> bool:
    """Return whether node and certificate qualify for active Fleet membership."""
    return (
        node_state is AgentNodeState.ACTIVE
        and certificate_state is CertificateState.ACTIVE
    )
