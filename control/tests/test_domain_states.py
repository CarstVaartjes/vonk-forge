import pytest
from vonk_control.domain_states import (
    AgentNodeState,
    CertificateState,
    InvalidStateTransitionError,
    OperationState,
    is_active_fleet_member,
    revoke_certificate,
    transition_operation,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OperationState.QUEUED, OperationState.RUNNING),
        (OperationState.QUEUED, OperationState.CANCELLED),
        (OperationState.RUNNING, OperationState.SUCCEEDED),
        (OperationState.RUNNING, OperationState.FAILED),
        (OperationState.RUNNING, OperationState.CANCELLED),
    ],
)
def test_operation_transition_allows_specified_edges(current, target):
    assert transition_operation(current, target) is target


def test_operation_transition_rejects_terminal_and_backward_edges():
    with pytest.raises(InvalidStateTransitionError):
        transition_operation(OperationState.SUCCEEDED, OperationState.RUNNING)
    with pytest.raises(InvalidStateTransitionError):
        transition_operation(OperationState.QUEUED, OperationState.SUCCEEDED)


def test_revocation_is_terminal_and_excluded_from_active_fleet():
    assert revoke_certificate(CertificateState.ACTIVE) is CertificateState.REVOKED
    assert not is_active_fleet_member(AgentNodeState.ACTIVE, CertificateState.REVOKED)
    with pytest.raises(InvalidStateTransitionError):
        revoke_certificate(CertificateState.REVOKED)


def test_states_are_str_enums_for_schema_serialization():
    assert OperationState.CANCELLED.value == "cancelled"
