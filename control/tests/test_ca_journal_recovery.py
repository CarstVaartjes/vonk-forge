"""Producer/store/restart/provider-observer recovery under exact CA identity."""

import json

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import vonk_control.enrollment.issuance as issuance_module
from vonk_control.ca_issuance_contract import CertificateIssuanceBinding
from vonk_control.enrollment.contract import EnrollmentGrant
from vonk_control.enrollment.service import EnrollmentService
from vonk_control.model_primitives import Base
from vonk_control.models.fleet import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
)
from vonk_control.pki import IssuedCertificate
from vonk_control.step_ca import StepCAUnavailable

from .ca_material import NODE_ID, NOW
from .test_enrollment import RecordingAuthority, csr, evidence


class LossyAuthority(RecordingAuthority):
    """Issues and journals the certificate, then loses the reply once."""

    def __init__(self) -> None:
        super().__init__()
        self.lose_response = False

    def issue_node(self, node_id, public_key_pem, now, *, request):
        issued = super().issue_node(node_id, public_key_pem, now, request=request)
        if self.lose_response:
            self.lose_response = False
            raise StepCAUnavailable("response deliberately lost")
        return issued


def setup(tmp_path):
    provider = LossyAuthority()
    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    return provider, sessions, lambda: NOW


def test_lost_enrollment_response_restarts_and_observes_identical_certificate(tmp_path):
    provider, sessions, clock = setup(tmp_path)
    service = EnrollmentService(sessions, provider, clock=clock)
    request = csr()
    grant = service.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    provider.lose_response = True
    service.submit(grant.token, request, evidence(request))
    with sessions() as session:
        accepted = session.scalar(select(AgentEnrollment))
        assert accepted is not None
        binding = CertificateIssuanceBinding.model_validate(accepted.provider_request)
        assert accepted.certificate_serial == binding.serial
    restarted = EnrollmentService(sessions, provider, clock=clock)
    issued = restarted.submit(grant.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    replay = restarted.submit(grant.token, request, evidence(request))
    assert issued == replay
    assert isinstance(replay, IssuedCertificate)
    assert issued.serial == binding.serial
    assert len(provider.calls) == 1


def test_enrollment_sql_persistence_failure_adopts_provider_result_after_restart(
    tmp_path, monkeypatch
):
    provider, sessions, clock = setup(tmp_path)
    service = EnrollmentService(sessions, provider, clock=clock)
    request = csr()
    grant = service.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    persist = issuance_module._persist_issued_enrollment

    failures = 0

    def fail(*args, **kwargs):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise IntegrityError(
                "injected commit conflict", None, RuntimeError("lost SQL result")
            )
        return persist(*args, **kwargs)

    monkeypatch.setattr(issuance_module, "_persist_issued_enrollment", fail)
    service.submit(grant.token, request, evidence(request))
    monkeypatch.setattr(issuance_module, "_persist_issued_enrollment", persist)
    restarted = EnrollmentService(sessions, provider, clock=clock)
    issued = restarted.submit(grant.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    assert len(provider.calls) == 1


def test_rotation_sql_failure_keeps_source_active_and_adopts_exact_result(
    tmp_path, monkeypatch
):
    provider, sessions, clock = setup(tmp_path)
    service = EnrollmentService(sessions, provider, clock=clock)
    first_csr = csr()
    grant = service.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    source = service.submit(grant.token, first_csr, evidence(first_csr))
    assert isinstance(source, IssuedCertificate)
    # Independent exact requests have independent journal entries.
    rotation_csr = csr()

    persist = service._persist_rotation
    failures = 0

    def fail(*args, **kwargs):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise IntegrityError(
                "injected commit conflict", None, RuntimeError("lost SQL result")
            )
        return persist(*args, **kwargs)

    monkeypatch.setattr(service, "_persist_rotation", fail)
    recovered = service.renew(NODE_ID, source.serial, rotation_csr)
    assert isinstance(recovered, IssuedCertificate)
    with sessions() as session:
        certificate = session.get(AgentCertificate, source.serial)
        assert certificate is not None and certificate.state == "active"
        assert session.get(AgentCertificateRotation, NODE_ID) is None
        accepted = session.get(AgentCertificate, recovered.serial)
        assert accepted is not None
        binding = CertificateIssuanceBinding.model_validate_json(
            json.dumps(accepted.provider_request)
        )
    restarted = EnrollmentService(sessions, provider, clock=clock)
    issued = restarted.renew(NODE_ID, source.serial, rotation_csr)
    assert isinstance(issued, IssuedCertificate)
    assert issued.serial == binding.serial
    assert issued.generation == 2
    assert len(provider.calls) == 2
    with sessions() as session:
        certificate = session.get(AgentCertificate, source.serial)
        assert certificate is not None and certificate.state == "active"
        certificate = session.get(AgentCertificate, issued.serial)
        assert certificate is not None and certificate.state == "staged"


def test_unbound_enrollment_releases_claim_without_inventing_provider_authority(
    tmp_path, monkeypatch
):
    provider, sessions, clock = setup(tmp_path)
    service = EnrollmentService(sessions, provider, clock=clock)
    original = issuance_module._persist_issued_enrollment

    def process_death(*args, **kwargs):
        raise SystemExit("process ended before result persistence")

    monkeypatch.setattr(issuance_module, "_persist_issued_enrollment", process_death)
    request = csr()
    grant = service.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    try:
        service.submit(grant.token, request, evidence(request))
    except SystemExit:
        assert len(provider.calls) == 1
    with sessions.begin() as session:
        accepted = session.scalar(select(AgentEnrollment))
        assert accepted is not None
        accepted.provider_request = None
    calls = len(provider.calls)
    restarted = EnrollmentService(sessions, provider, clock=clock)
    restarted.submit(grant.token, request, evidence(request))
    assert len(provider.calls) == calls
    with sessions() as session:
        assert session.scalar(select(AgentEnrollment)) is None
        assert session.scalar(select(AgentCertificate)) is None
    monkeypatch.setattr(issuance_module, "_persist_issued_enrollment", original)
    fresh = restarted.create(NODE_ID, "admin", 600)
    assert isinstance(fresh, EnrollmentGrant)
    issued = restarted.submit(fresh.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    assert issued.node_id == NODE_ID
    assert len(provider.calls) == 2
