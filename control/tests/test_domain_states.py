from enum import StrEnum

import pytest

from vonk_control.domain_states import (
    AgentNodeState,
    CertificateState,
    EnrollmentState,
    InvalidStateTransitionError,
    OperationState,
    is_active_fleet_member,
    revoke_certificate,
    transition_enrollment,
    transition_operation,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (EnrollmentState.CREATED, EnrollmentState.WAITING_FOR_REGISTRATION),
        (EnrollmentState.WAITING_FOR_REGISTRATION, EnrollmentState.PENDING_REVIEW),
        (EnrollmentState.PENDING_REVIEW, EnrollmentState.APPROVED),
        (EnrollmentState.PENDING_REVIEW, EnrollmentState.REJECTED),
        (EnrollmentState.PENDING_REVIEW, EnrollmentState.EXPIRED),
        (EnrollmentState.APPROVED, EnrollmentState.CERTIFICATE_ISSUED),
    ],
)
def test_enrollment_transition_allows_specified_edges(current, target):
    assert transition_enrollment(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (EnrollmentState.CREATED, EnrollmentState.APPROVED),
        (EnrollmentState.REJECTED, EnrollmentState.CREATED),
        (EnrollmentState.CERTIFICATE_ISSUED, EnrollmentState.APPROVED),
    ],
)
def test_enrollment_transition_rejects_invalid_edges(current, target):
    with pytest.raises(InvalidStateTransitionError):
        transition_enrollment(current, target)


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
    assert isinstance(EnrollmentState.CREATED, StrEnum)
    assert EnrollmentState.CERTIFICATE_ISSUED.value == "certificate_issued"
    assert OperationState.CANCELLED.value == "cancelled"
