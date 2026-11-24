"""Connected enrollment recovery: exact effects, real ending and fresh admission."""

import json
import uuid
from contextlib import contextmanager
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from vonk_agent_protocol.enrollment import (
    ActivateRequest,
    IssuedCertificateResponse,
)
from vonk_agent_protocol.lifecycle_vocabulary import (
    ErrorCategory,
    SecurityRefusalReason,
)
from vonk_agent_protocol.outcome import UnknownError
from vonk_agent_protocol.state_machines import (
    CertificateRecordState,
    CertificateRotationState,
    EnrollmentGrantState,
    EnrollmentRecordState,
)
from vonk_control.api.application import create_app
from vonk_control.enrollment.contract import EnrollmentGrant
from vonk_control.enrollment.responses import _issued_response
from vonk_control.enrollment.service import EnrollmentService
from vonk_control.enrollment.types import EnrollmentDenied
from vonk_control.failures.coordinator import CoordinatorFailures
from vonk_control.jobs.queue import JobService
from vonk_control.models.fleet import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    AgentEnrollmentGrant,
    AgentIssuedCertificateRevocation,
)
from vonk_control.pki.issuance import IssuedCertificate
from vonk_control.pki.issuance_contract import CertificateIssuanceBinding
from vonk_control.pki.step_ca import StepCAIssuancePending
from vonk_control.worker.loop import Worker

from .agent_api_support import (
    NODE_A,
    CurrentAgentClient,
    Jobs,
    _csr_for,
    agent_headers,
    make_agent_system,
)
from .ca_material import NOW
from .test_enrollment import (
    NODE_ID,
    OTHER_NODE_ID,
    RecordingAuthority,
    csr,
    enroll,
    evidence,
)
from .test_enrollment import service as service  # noqa: PLC0414 -- fixture export


@pytest.mark.parametrize("generation", [2**31, 2**53 + 1, 2**64 - 1])
def test_accepted_generation_round_trips_without_client_or_storage_narrowing(
    service, generation
):
    """Catches int32 response limits and float-rounded relational projections."""

    enrollment, sessions, _, authority = service
    source = enroll(enrollment)
    with sessions.begin() as session:
        session.get(AgentCertificate, source.serial).generation = generation - 1
    grant = enrollment.create_reenrollment(
        NODE_ID, "admin", 600, request_key=str(uuid.uuid4())
    )
    request = csr()
    issued = enrollment.submit(grant.token, request, evidence(request))
    response = IssuedCertificateResponse.model_validate_json(
        _issued_response(issued).model_dump_json()
    )
    assert response.generation == generation
    with sessions() as session:
        assert session.get(AgentCertificate, issued.serial).generation == generation
        accepted = session.scalar(
            select(AgentEnrollment).where(AgentEnrollment.grant_id == grant.id)
        )
        assert accepted.certificate_generation == generation
    activation = ActivateRequest.model_validate_json(
        ActivateRequest(node_id=NODE_ID, generation=generation).model_dump_json()
    )
    enrollment.activate(NODE_ID, issued.serial, activation.generation)
    replay = enrollment.submit(grant.token, request, evidence(request))
    assert replay == issued
    assert len(authority.calls) == 2


def test_lost_renewal_response_is_observed_in_the_original_request(service):
    """Catches a one-shot handoff instead of automatic exact-effect recovery."""
    enrollment, sessions, _, authority = service
    source = enroll(enrollment)
    authority.renew_error = RuntimeError("lost response")
    issued = enrollment.renew(NODE_ID, source.serial, csr())
    assert issued.serial != source.serial
    assert len(authority.renew_request_ids) == 1
    with sessions() as session:
        assert session.get(AgentCertificateRotation, NODE_ID) is None
        assert session.get(AgentCertificate, issued.serial) is not None


def test_unverified_identity_never_has_a_persisted_effect(service, monkeypatch):
    """Catches provider mismatch adoption rather than asserting an error taxonomy."""
    enrollment, sessions, _, authority = service
    source = enroll(enrollment)
    original = authority.renew_node
    calls = []

    def wrong_identity(*args, **kwargs):
        issued = original(*args, **kwargs)
        calls.append(issued.serial)
        return replace(issued, node_id=OTHER_NODE_ID)

    monkeypatch.setattr(authority, "renew_node", wrong_identity)
    failure: RuntimeError | None = None
    try:
        enrollment.renew(NODE_ID, source.serial, csr())
    except RuntimeError as error:
        failure = error
    assert failure is None or calls, (
        f"unexpected failure before provider effect: {failure}"
    )
    with sessions() as session:
        assert session.get(AgentCertificate, calls[0]) is None
        assert session.get(AgentCertificate, source.serial).revoked_at is None
        assert (
            session.get(
                AgentEnrollmentGrant, session.scalar(select(AgentEnrollment.grant_id))
            ).consumed_at
            is not None
        )


def test_exhausted_submit_replays_exact_binding_after_recovery(service, monkeypatch):
    """Catches a transient provider outage permanently consuming the bearer grant."""
    enrollment, _, _, authority = service
    original = authority.issue_node
    bindings = []

    def unavailable(*args, **kwargs):
        bindings.append(kwargs["request"])
        raise OSError("authority unavailable before response")

    monkeypatch.setattr(authority, "issue_node", unavailable)
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    enrollment.submit(grant.token, request, evidence(request))
    assert len(bindings) == 4
    assert all(binding == bindings[0] for binding in bindings)
    monkeypatch.setattr(authority, "issue_node", original)
    issued = enrollment.submit(grant.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    assert issued.serial == bindings[0].serial
    fresh = enrollment.create_reenrollment(
        NODE_ID, "admin", 600, request_key=str(uuid.uuid4())
    )
    assert isinstance(fresh, EnrollmentGrant)
    next_request = csr()
    assert isinstance(
        enrollment.submit(fresh.token, next_request, evidence(next_request)),
        IssuedCertificate,
    )


def test_exhausted_rotation_releases_gate_and_reconciles_late_effect(
    service, monkeypatch
):
    """Catches terminal response without gate release or late-effect confirmation."""
    enrollment, sessions, clock, authority = service
    source = enroll(enrollment)
    original_observe = authority.observe_node
    original_renew = authority.renew_node
    accepted = []

    def unavailable_observation(*args, **kwargs):
        raise OSError("journal unavailable")

    monkeypatch.setattr(authority, "observe_node", unavailable_observation)
    enrollment.renew(NODE_ID, source.serial, csr())
    with sessions() as session:
        assert session.get(AgentCertificateRotation, NODE_ID) is None
        ended = session.scalar(select(AgentIssuedCertificateRevocation))
        accepted.append(ended.provider_request)
        material = ended.csr_pem
    # A late CA effect belongs only to the ended binding.

    binding = CertificateIssuanceBinding.model_validate_json(json.dumps(accepted[0]))
    original_renew(NODE_ID, material.encode("ascii"), clock.now, request=binding)
    monkeypatch.setattr(authority, "observe_node", original_observe)
    issued = enrollment.renew(NODE_ID, source.serial, csr())
    assert issued.serial != binding.serial
    assert enrollment.reconcile_revocations()
    assert binding.serial in authority.revocations
    with sessions() as session:
        assert session.get(AgentCertificate, binding.serial) is None
        assert session.get(AgentCertificate, issued.serial).revoked_at is None
        assert (
            session.get(AgentIssuedCertificateRevocation, binding.serial).ca_revoked_at
            is not None
        )


def test_revocation_confirmation_recovers_through_worker_callback(service):
    """Catches losing CA uncertainty at the actual service/worker boundary."""
    enrollment, sessions, _, authority = service
    source = enroll(enrollment)
    authority.revoke_failures.add(source.serial)
    enrollment.revoke_node(NODE_ID, "admin")
    status = enrollment.revocation_status(NODE_ID)
    assert status.local_denial_complete
    assert not status.ca_confirmation_complete
    with sessions() as session:
        assert session.get(AgentCertificate, source.serial).revoked_at is not None
    # Retirement is already complete; repeated requests do not reopen a gate.
    enrollment.revoke_node(NODE_ID, "admin")
    authority.revoke_failures.clear()

    worker = Worker(
        JobService(sessions, clock=enrollment._clock),
        "enrollment-worker",
        {},
        background_services=(enrollment.reconcile_revocations,),
        coordinator_failures=CoordinatorFailures(sessions, enrollment._clock, "a" * 64),
    )
    assert worker.run_once()
    assert enrollment.revocation_status(NODE_ID).ca_confirmation_complete


def test_absent_retirement_is_idempotent_and_same_node_can_enroll(service):
    enrollment, _, _, _ = service
    assert enrollment.revoke_node(NODE_ID, "admin") is None
    assert enrollment.revoke_node(NODE_ID, "admin") is None
    assert enroll(enrollment).node_id == NODE_ID


@pytest.mark.parametrize(
    "field", ["boot_id", "agent_digest", "hardware_fingerprint", "host_key_fingerprint"]
)
def test_committed_content_replays_after_observation_changes(service, field):
    """Catches boot/package observations being a second certificate authority."""
    enrollment, _, _, authority = service
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    first = enrollment.submit(grant.token, request, evidence(request))
    observations = evidence(request) | {field: "a" * 64}
    assert enrollment.submit(grant.token, request, observations) == first
    assert len(authority.calls) == 1


def test_consumed_grant_revocation_refuses_without_revoking_node(service):
    """Catches a consumed receipt being presented as successful revocation."""
    enrollment, sessions, _, authority = service
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    issued = enrollment.submit(grant.token, request, evidence(request))
    for _ in range(2):
        with pytest.raises(EnrollmentDenied, match="already consumed") as refused:
            enrollment.revoke_grant(grant.id, actor="admin")
        assert refused.value.category == ErrorCategory.SECURITY_REFUSAL
        assert (
            refused.value.typed_reason
            == SecurityRefusalReason.CONTROLLER_FLEET_ENROLLMENT_DENIED
        )
    status = enrollment.grant_status(grant.id, actor="admin")
    assert status.state == EnrollmentGrantState.CONSUMED
    assert status.revoked_at is None
    with sessions() as session:
        certificate = session.get(AgentCertificate, issued.serial)
        assert certificate is not None
        assert certificate.revoked_at is None
    assert authority.revocations == []


def test_duplicate_grant_identity_observes_original_status(service):
    enrollment, sessions, _, _ = service
    key = str(uuid.uuid4())
    grant = enrollment.create_named("Spark", "admin", 600, request_key=key)
    replay = enrollment.create_named("Spark", "admin", 600, request_key=key)
    assert replay == enrollment.grant_status(grant.id, actor="admin")
    with sessions() as session:
        assert len(list(session.scalars(select(AgentEnrollmentGrant)))) == 1


def test_staged_missing_material_reobserves_exact_content(service):
    """Catches lost staged PEM causing a server error or another issuance."""
    enrollment, sessions, _, authority = service
    source = enroll(enrollment)
    request = csr()
    staged = enrollment.renew(NODE_ID, source.serial, request)
    with sessions.begin() as session:
        session.get(AgentCertificate, staged.serial).certificate_pem = None
    assert enrollment.renew(NODE_ID, source.serial, request) == staged
    assert len(authority.calls) == 2


@pytest.mark.parametrize("damage", ["phase", "material", "binding"])
def test_damaged_rotation_projection_releases_owner(service, damage):
    enrollment, sessions, clock, _ = service
    source = enroll(enrollment)
    request = csr()
    with sessions.begin() as session:
        session.add(
            AgentCertificateRotation(
                node_id=NODE_ID,
                source_serial=source.serial,
                generation=2,
                csr_pem="é" if damage == "material" else request.decode("ascii"),
                csr_public_key_fingerprint=evidence(request)[
                    "csr_public_key_fingerprint"
                ],
                provider_request_id="damaged-projection",
                provider_request=None,
                state="damaged"
                if damage == "phase"
                else CertificateRotationState.ISSUING,
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
    issued = enrollment.renew(NODE_ID, source.serial, request)
    assert issued.serial != source.serial
    with sessions() as session:
        assert session.get(AgentCertificateRotation, NODE_ID) is None
        assert (
            session.get(AgentCertificate, issued.serial).state
            == CertificateRecordState.STAGED
        )


def test_grant_admission_sql_conflicts_end_without_poisoning_request_identity(
    service, monkeypatch
):
    """Catches an exhausted admission keeping a gate or minting unrelated grants."""

    enrollment, sessions, _, authority = service
    transaction = enrollment._transaction
    attempts = 0
    fault = True

    @contextmanager
    def conflicting_transaction():
        nonlocal attempts
        attempts += 1
        if fault:
            raise OperationalError(
                "bounded ownership conflict", None, OSError("lock timeout")
            )
        with transaction() as session:
            yield session

    monkeypatch.setattr(enrollment, "_transaction", conflicting_transaction)
    request_key = str(uuid.uuid4())
    enrollment.create_named("Spark", "admin", 600, request_key=request_key)
    assert attempts == 4
    assert not authority.calls
    with sessions() as session:
        assert session.scalar(select(AgentEnrollmentGrant)) is None
    fault = False
    grant = enrollment.create_named("Spark", "admin", 600, request_key=request_key)
    assert isinstance(grant, EnrollmentGrant)
    assert grant.id == request_key
    request = csr()
    issued = enrollment.submit(grant.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    assert len(authority.calls) == 1


def test_damage_during_ca_observation_repairs_both_committed_projections(
    service, monkeypatch
):
    """Catches an uncaught receipt race or repairing only the response projection."""
    enrollment, sessions, _, authority = service
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    issued = enrollment.submit(grant.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    with sessions.begin() as session:
        accepted = session.scalar(select(AgentEnrollment))
        accepted.certificate_generation = None
    observe = authority.observe_node

    def damaged_observation(*args, **kwargs):
        result = observe(*args, **kwargs)
        with sessions.begin() as session:
            accepted = session.scalar(select(AgentEnrollment))
            accepted.certificate_generation = None
            certificate = session.get(AgentCertificate, issued.serial)
            certificate.certificate_pem = None
            certificate.fingerprint = "f" * 64
        return result

    monkeypatch.setattr(authority, "observe_node", damaged_observation)
    assert enrollment.submit(grant.token, request, evidence(request)) == issued
    with sessions() as session:
        certificate = session.get(AgentCertificate, issued.serial)
        assert certificate.certificate_pem.encode("ascii") == issued.certificate_pem
        assert certificate.fingerprint == issued.fingerprint
    assert len(authority.calls) == 1


def test_busy_ca_observation_heals_on_the_next_attempt(service):
    """Catches a busy CA journal stranding an accepted enrollment."""

    _, sessions, _, _ = service

    class BusyOnceAuthority(RecordingAuthority):
        busy = True

        def observe_node(self, csr_pem, now, *, request):
            if self.busy:
                self.busy = False
                raise StepCAIssuancePending("local CA journal is busy")
            return super().observe_node(csr_pem, now, request=request)

    provider = BusyOnceAuthority()
    enrollment = EnrollmentService(sessions, provider, clock=lambda: NOW)
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    issued = enrollment.submit(grant.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    with sessions() as session:
        accepted = session.scalar(select(AgentEnrollment))
        assert accepted.state == EnrollmentRecordState.CERTIFICATE_ISSUED


def test_controller_restart_after_ca_outage_serves_renewal_and_fresh_rotation(
    service, monkeypatch
):
    """Catches a standing renewal gate after an unavailable CA and lost process."""

    enrollment, sessions, clock, authority = service
    source = enroll(enrollment)
    pending = csr()
    observe = authority.observe_node

    def unavailable(*args, **kwargs):
        raise OSError("CA restarting")

    monkeypatch.setattr(authority, "observe_node", unavailable)
    outcome = enrollment.renew(NODE_ID, source.serial, pending)
    assert isinstance(outcome, UnknownError)
    with sessions() as session:
        assert session.get(AgentCertificate, source.serial).revoked_at is None
        assert session.get(AgentCertificateRotation, NODE_ID) is None
    # A Controller replacement retains SQL authority but no service-local state.
    restarted = EnrollmentService(sessions, authority, clock=clock)
    monkeypatch.setattr(authority, "observe_node", observe)
    replacement = restarted.renew(NODE_ID, source.serial, pending)
    assert isinstance(replacement, IssuedCertificate)
    assert restarted.renew(NODE_ID, source.serial, pending) == replacement
    restarted.activate(NODE_ID, replacement.serial, replacement.generation)
    fresh = restarted.renew(NODE_ID, replacement.serial, csr())
    assert isinstance(fresh, IssuedCertificate)
    assert fresh.serial != replacement.serial


def test_authenticated_renewal_route_recovers_across_controller_replacement(
    tmp_path, monkeypatch
):
    """Catches persistent HTTP rejection rather than merely provider recovery."""

    client, services, codec, clock = make_agent_system(
        tmp_path, authority=RecordingAuthority()
    )
    with services.sessions.begin() as session:
        source = session.get(AgentCertificate, "serial-a")
        assert source is not None
        source.serial = "101"
        source.fingerprint = "fingerprint-101"
    pending = _csr_for(NODE_A)
    body = {"node_id": NODE_A, "csr": pending.decode()}
    assert services.enrollment is not None
    authority = services.enrollment._authority
    observe = authority.observe_node

    def unavailable(*args, **kwargs):
        raise OSError("CA restarting")

    monkeypatch.setattr(authority, "observe_node", unavailable)
    assert (
        client.post(
            "/agent/renew", headers=agent_headers(NODE_A, "101"), json=body
        ).status_code
        == 503
    )
    # Restart the production owner against the existing durable database.
    services = replace(
        services,
        enrollment=EnrollmentService(services.sessions, authority, clock=clock),
    )
    client = CurrentAgentClient(
        create_app(
            jobs=Jobs(),
            tokens=codec,
            now=lambda: 0,
            agent=services,
            trusted_agent_proxy_auth=b"p" * 32,
        )
    )
    monkeypatch.setattr(authority, "observe_node", observe)
    response = client.post(
        "/agent/renew", headers=agent_headers(NODE_A, "101"), json=body
    )
    assert response.status_code == 200
    issued = IssuedCertificateResponse.model_validate_json(response.content)
    headers = agent_headers(NODE_A, issued.serial)
    headers["x-vonk-agent-fingerprint"] = issued.fingerprint
    assert (
        client.post(
            "/agent/renew/activate",
            headers=headers,
            json={
                "node_id": NODE_A,
                "generation": issued.generation,
            },
        ).status_code
        == 204
    )
    assert client.post("/agent/claim", headers=headers).status_code == 204
    assert (
        client.post(
            "/agent/renew",
            headers=headers,
            json={
                "node_id": NODE_A,
                "csr": _csr_for(NODE_A).decode(),
            },
        ).status_code
        == 200
    )
