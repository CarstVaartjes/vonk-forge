"""Exact CA observation heals bookkeeping; verification never retries into success."""

from dataclasses import replace

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from vonk_agent_protocol import (
    LifecycleState,
    SecurityRefusalReason,
    UnknownError,
    WaitReason,
)
from vonk_agent_protocol.state_machines import EnrollmentRecordState
from vonk_control.enrollment import EnrollmentDenied, EnrollmentService
from vonk_control.models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    AgentNode,
)
from vonk_control.pki import IssuedCertificate

from .non_blocking import assert_ended_without_blocking
from .test_enrollment import NODE_ID, OTHER_NODE_ID, csr, enroll, evidence
from .test_enrollment import (
    service as service,  # noqa: PLC0414 -- pytest fixture export
)


class Receipt(BaseModel):
    request_key: str
    state: LifecycleState
    reason_code: WaitReason | SecurityRefusalReason | None = None


def test_lost_renewal_response_is_observed_in_the_original_request(service):
    """Catches returning an uncertain refusal before observing a committed effect."""
    enrollment, sessions, _clock, authority = service
    source = enroll(enrollment)
    authority.renew_error = RuntimeError("lost response")
    issued = enrollment.renew(NODE_ID, source.serial, csr())
    assert isinstance(issued, IssuedCertificate)
    assert len(authority.renew_request_ids) == 1
    with sessions() as session:
        assert session.get(AgentCertificateRotation, NODE_ID) is None
        assert session.get(AgentCertificate, issued.serial) is not None


def test_verification_failure_is_never_converted_into_unknown(service, monkeypatch):
    """Catches the broad provider handler swallowing a verified identity refusal."""
    enrollment, _sessions, _clock, authority = service
    source = enroll(enrollment)
    original = authority.renew_node
    calls = []

    def wrong_identity(*args, **kwargs):
        issued = original(*args, **kwargs)
        calls.append(issued.serial)
        return replace(issued, node_id=OTHER_NODE_ID)

    monkeypatch.setattr(authority, "renew_node", wrong_identity)
    with pytest.raises(EnrollmentDenied, match="accepted issuance binding"):
        enrollment.renew(NODE_ID, source.serial, csr())
    assert len(calls) == 1


def test_exhausted_revocation_keeps_local_denial_and_admits_fresh_operation(service):
    """Catches a remote bookkeeping error blocking a locally completed retirement."""
    enrollment, sessions, _clock, authority = service
    source = enroll(enrollment)
    authority.revoke_failures.add(source.serial)
    original = Receipt(request_key="retirement", state=LifecycleState.RUNNING)

    def end(_receipt):
        observed = enrollment.revoke_node(NODE_ID, "admin")
        assert isinstance(observed, UnknownError)
        assert observed.reason is WaitReason.OBSERVATION_UNAVAILABLE
        return Receipt(request_key=original.request_key, state=LifecycleState.SUCCEEDED)

    def released():
        with sessions() as session:
            node = session.get(AgentNode, NODE_ID)
            certificate = session.get(AgentCertificate, source.serial)
            assert node is not None and node.revoked_at is not None
            assert certificate is not None and certificate.revoked_at is not None

    def fresh(_world):
        grant = enrollment.create(OTHER_NODE_ID, "admin", 600)
        request = csr(OTHER_NODE_ID)
        issued = enrollment.submit(
            grant.token, request, evidence(request, node_id=OTHER_NODE_ID)
        )
        assert isinstance(issued, IssuedCertificate)
        return Receipt(request_key=grant.id, state=LifecycleState.SUCCEEDED)

    assert_ended_without_blocking(
        sessions, original, end=end, fresh=fresh, assert_released=released
    )
    assert len(authority.revocations) == 4
    authority.revoke_failures.clear()
    assert enrollment.revoke_node(NODE_ID, "admin") is None
    with sessions() as session:
        certificate = session.get(AgentCertificate, source.serial)
        assert certificate is not None and certificate.ca_revoked_at is not None


def test_unknown_submit_has_bounded_attempts_and_reuses_exact_binding(
    service, monkeypatch
):
    """Catches terminal provider failure and duplicate issuance after response loss."""
    enrollment, sessions, clock, authority = service
    attempts = []
    original = authority.issue_node

    def unavailable(*args, **kwargs):
        attempts.append(kwargs["request"].request_id)
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(authority, "issue_node", unavailable)
    grant = enrollment.create(NODE_ID, "admin", 600)
    request = csr()
    outcome = enrollment.submit(grant.token, request, evidence(request))
    assert isinstance(outcome, UnknownError)
    assert len(attempts) == 4 and len(set(attempts)) == 1
    with sessions() as session:
        row = session.scalar(select(AgentEnrollment))
        assert row is not None and row.provider_request is not None
    monkeypatch.setattr(authority, "issue_node", original)
    restarted = EnrollmentService(sessions, authority, clock=clock)
    issued = restarted.submit(grant.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    assert len(authority.calls) == 1


def test_unbound_rotation_ends_fail_closed_and_admits_fresh_same_node(service):
    """Catches an unverified historical issuance gate permanently poisoning renewal."""
    enrollment, sessions, clock, authority = service
    source = enroll(enrollment)
    request = csr()
    from .test_enrollment import public_key_fingerprint

    claim = enrollment._claim_rotation(
        NODE_ID, source.serial, request, public_key_fingerprint(request), clock.now
    )
    assert not isinstance(claim, IssuedCertificate)
    with sessions.begin() as session:
        intent = session.get(AgentCertificateRotation, NODE_ID)
        assert intent is not None
        intent.provider_request = None
    original = Receipt(
        request_key=claim.provider_request_id, state=LifecycleState.RUNNING
    )

    def end(_receipt):
        with pytest.raises(
            EnrollmentDenied, match="no exact journal binding"
        ) as refused:
            enrollment.renew(NODE_ID, source.serial, request)
        assert authority.renew_request_ids == []
        return Receipt(
            request_key=original.request_key,
            state=LifecycleState.FAILED,
            reason_code=refused.value.typed_reason,
        )

    def released():
        with sessions() as session:
            assert session.get(AgentCertificateRotation, NODE_ID) is None

    def fresh(_world):
        issued = enrollment.renew(NODE_ID, source.serial, csr())
        assert isinstance(issued, IssuedCertificate)
        return Receipt(
            request_key=authority.renew_request_ids[-1], state=LifecycleState.SUCCEEDED
        )

    assert_ended_without_blocking(
        sessions, original, end=end, fresh=fresh, assert_released=released
    )


def test_new_csr_reconciles_competing_exact_rotation_before_replacement(service):
    """Catches a fresh authorized recovery stuck behind an older unknown outcome."""
    enrollment, sessions, clock, authority = service
    source = enroll(enrollment)
    old_csr = csr()
    from .test_enrollment import public_key_fingerprint

    claim = enrollment._claim_rotation(
        NODE_ID, source.serial, old_csr, public_key_fingerprint(old_csr), clock.now
    )
    assert not isinstance(claim, IssuedCertificate)
    assert claim.provider_request is not None
    obsolete = authority.renew_node(
        NODE_ID, old_csr, clock.now, request=claim.provider_request
    )
    recovered = enrollment.recover_rotation(NODE_ID, source.serial, csr())
    assert isinstance(recovered, IssuedCertificate)
    assert recovered.generation == 3
    assert authority.revocations == [obsolete.serial]
    assert len(authority.renew_request_ids) == 2
    with sessions() as session:
        assert session.get(AgentCertificateRotation, NODE_ID) is None


def test_unbound_enrollment_releases_old_gate_for_fresh_same_node(service, monkeypatch):
    """Catches a denied historical grant leaving the node's issuance gate poisoned."""
    enrollment, sessions, _clock, authority = service
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    issue = authority.issue_node

    def unavailable(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(authority, "issue_node", unavailable)
    assert isinstance(
        enrollment.submit(grant.token, request, evidence(request)), UnknownError
    )
    with sessions.begin() as session:
        accepted = session.scalar(select(AgentEnrollment))
        assert accepted is not None
        accepted.provider_request = None
    monkeypatch.setattr(authority, "issue_node", issue)
    original = Receipt(request_key=grant.id, state=LifecycleState.RUNNING)

    def end(_receipt):
        with pytest.raises(
            EnrollmentDenied, match="no exact journal binding"
        ) as refused:
            enrollment.submit(grant.token, request, evidence(request))
        assert authority.calls == []
        return Receipt(
            request_key=grant.id,
            state=LifecycleState.FAILED,
            reason_code=refused.value.typed_reason,
        )

    def released():
        with sessions() as session:
            assert (
                session.scalar(
                    select(AgentEnrollment.id).where(
                        AgentEnrollment.state == EnrollmentRecordState.ISSUING
                    )
                )
                is None
            )
            rows = list(session.scalars(select(AgentEnrollment)))
            assert all(row.provider_request is not None for row in rows)

    def fresh(_world):
        new_grant = enrollment.create(NODE_ID, "admin", 600)
        new_csr = csr()
        issued = enrollment.submit(new_grant.token, new_csr, evidence(new_csr))
        assert isinstance(issued, IssuedCertificate)
        return Receipt(request_key=new_grant.id, state=LifecycleState.SUCCEEDED)

    assert_ended_without_blocking(
        sessions, original, end=end, fresh=fresh, assert_released=released
    )


def test_fresh_csr_reconciles_older_pending_retirement(service):
    """Catches a new CSR refused behind an older recoverable revocation intent."""
    from .test_enrollment import public_key_fingerprint

    enrollment, sessions, _clock, authority = service
    source = enroll(enrollment)
    obsolete = enrollment.renew(NODE_ID, source.serial, csr())
    assert isinstance(obsolete, IssuedCertificate)
    authority.revoke_failures.add(obsolete.serial)
    assert isinstance(
        enrollment.recover_rotation(NODE_ID, source.serial, csr()), UnknownError
    )
    authority.revoke_failures.clear()
    request = csr()
    replacement = enrollment.recover_rotation(NODE_ID, source.serial, request)
    assert isinstance(replacement, IssuedCertificate)
    with sessions() as session:
        stored = session.get(AgentCertificate, replacement.serial)
        assert stored is not None
        assert stored.csr_public_key_fingerprint == public_key_fingerprint(request)
        assert session.get(AgentCertificateRotation, NODE_ID) is None


def test_missing_retirement_target_releases_gate_for_fresh_rotation(service):
    """Catches an absent locally denied target permanently poisoning new work."""
    enrollment, sessions, _clock, authority = service
    source = enroll(enrollment)
    obsolete = enrollment.renew(NODE_ID, source.serial, csr())
    assert isinstance(obsolete, IssuedCertificate)
    authority.revoke_failures.add(obsolete.serial)
    assert isinstance(
        enrollment.recover_rotation(NODE_ID, source.serial, csr()), UnknownError
    )
    with sessions.begin() as session:
        target = session.get(AgentCertificate, obsolete.serial)
        assert target is not None
        session.delete(target)
    original = Receipt(request_key=obsolete.serial, state=LifecycleState.RUNNING)

    def end(_receipt):
        replacement = enrollment.recover_rotation(NODE_ID, source.serial, csr())
        assert isinstance(replacement, IssuedCertificate)
        return Receipt(request_key=obsolete.serial, state=LifecycleState.SUCCEEDED)

    def released():
        with sessions() as session:
            assert session.get(AgentCertificateRotation, NODE_ID) is None

    def fresh(_world):
        issued = enrollment.recover_rotation(NODE_ID, source.serial, csr())
        assert isinstance(issued, IssuedCertificate)
        return Receipt(request_key=issued.serial, state=LifecycleState.SUCCEEDED)

    assert_ended_without_blocking(
        sessions, original, end=end, fresh=fresh, assert_released=released
    )
