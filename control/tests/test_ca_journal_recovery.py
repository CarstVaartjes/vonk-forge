"""Producer/store/restart/provider-observer recovery under exact CA identity."""

import json
from pathlib import Path

import httpx2
import jwt
import pytest
import vonk_control.enrollment.issuance as issuance_module
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from vonk_control.ca_issuance_contract import (
    CertificateIssuanceBinding,
    CertificateIssuedReply,
)
from vonk_control.enrollment.service import EnrollmentService
from vonk_control.enrollment.types import (
    EnrollmentDenied,
    EnrollmentIssuanceUncertain,
    RenewalIssuanceUncertain,
)
from vonk_control.models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    Base,
)
from vonk_control.pki import IssuedCertificate
from vonk_control.step_ca import StepCAError

from .test_enrollment import csr, evidence
from .test_step_ca import NODE_ID, NOW, _Material, _provider, _success_response


class StoredProvider:
    """Fault transport storing canonical provider output across Controller restarts.

    Actual signer process death and concurrent issuer epochs use the hosted CA
    lane. This transport catches Controller reissuance after lost responses or
    failed SQL persistence while preserving real CSR/JWT/leaf verification.
    """

    def __init__(self, path: Path):
        self.path = path
        self.material: _Material | None = None
        self.issue_calls = 0
        self.tokens: list[str] = []
        self.lose_response = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        binding = CertificateIssuanceBinding.model_validate(body["request"])
        claims = jwt.decode(body["ott"], options={"verify_signature": False})
        assert claims["vonk"] == binding.model_dump(mode="json")
        self.tokens.append(claims["jti"])
        if self.path.exists():
            stored = CertificateIssuedReply.model_validate_json(self.path.read_bytes())
            if stored.request != binding:
                return httpx2.Response(
                    409,
                    json={
                        "reason_code": "certificate.request_binding_mismatch",
                        "detail": "exact binding differs",
                    },
                )
            return httpx2.Response(201, json=stored.model_dump(mode="json"))
        if body["mode"] == "observe":
            return httpx2.Response(
                200,
                json={"state": "absent", "request": binding.model_dump(mode="json")},
            )
        self.issue_calls += 1
        assert self.material is not None
        response = _success_response(request, self.material, [])
        stored = CertificateIssuedReply.model_validate(response.json())
        self.path.write_text(stored.model_dump_json())
        if self.lose_response:
            self.lose_response = False
            raise httpx2.ReadError("response deliberately lost", request=request)
        return response


def setup(tmp_path: Path):
    transport = StoredProvider(tmp_path / "committed.json")
    provider, material = _provider(tmp_path, transport)
    transport.material = material
    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: NOW
    return transport, provider, sessions, clock


def test_lost_enrollment_response_restarts_and_observes_identical_certificate(tmp_path):
    transport, provider, sessions, clock = setup(tmp_path)
    service = EnrollmentService(sessions, provider, clock=clock)
    request = csr()
    grant = service.create(NODE_ID, "admin", 600)
    transport.lose_response = True
    with pytest.raises(EnrollmentIssuanceUncertain):
        service.submit(grant.token, request, evidence(request))
    with sessions() as session:
        accepted = session.scalar(select(AgentEnrollment))
        assert accepted is not None
        binding = CertificateIssuanceBinding.model_validate(accepted.provider_request)
        assert accepted.state == "issuing"
    restarted = EnrollmentService(sessions, provider, clock=clock)
    issued = restarted.submit(grant.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    replay = restarted.submit(grant.token, request, evidence(request))
    assert issued == replay
    assert issued.serial == binding.serial
    assert transport.issue_calls == 1
    assert len(transport.tokens) == len(set(transport.tokens))
    assert binding.request_id not in transport.tokens


def test_enrollment_sql_persistence_failure_adopts_provider_result_after_restart(
    tmp_path, monkeypatch
):
    transport, provider, sessions, clock = setup(tmp_path)
    service = EnrollmentService(sessions, provider, clock=clock)
    request = csr()
    grant = service.create(NODE_ID, "admin", 600)
    persist = issuance_module._persist_issued_enrollment

    def fail(*args, **kwargs):
        raise IntegrityError(
            "injected controller commit failure", None, RuntimeError("lost SQL result")
        )

    monkeypatch.setattr(issuance_module, "_persist_issued_enrollment", fail)
    with pytest.raises(EnrollmentIssuanceUncertain):
        service.submit(grant.token, request, evidence(request))
    monkeypatch.setattr(issuance_module, "_persist_issued_enrollment", persist)
    restarted = EnrollmentService(sessions, provider, clock=clock)
    issued = restarted.submit(grant.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    stored = CertificateIssuedReply.model_validate_json(transport.path.read_bytes())
    assert issued.certificate_pem.decode() == stored.crt
    assert transport.issue_calls == 1


def test_rotation_sql_failure_keeps_source_active_and_adopts_exact_result(
    tmp_path, monkeypatch
):
    transport, provider, sessions, clock = setup(tmp_path)
    service = EnrollmentService(sessions, provider, clock=clock)
    first_csr = csr()
    grant = service.create(NODE_ID, "admin", 600)
    source = service.submit(grant.token, first_csr, evidence(first_csr))
    assert isinstance(source, IssuedCertificate)
    # Independent exact requests have independent journal entries.
    transport.path = tmp_path / "rotation.json"
    rotation_csr = csr()

    def fail(*args, **kwargs):
        raise IntegrityError(
            "injected controller commit failure", None, RuntimeError("lost SQL result")
        )

    monkeypatch.setattr(service, "_persist_rotation", fail)
    with pytest.raises(RenewalIssuanceUncertain):
        service.renew(NODE_ID, source.serial, rotation_csr)
    with sessions() as session:
        certificate = session.get(AgentCertificate, source.serial)
        assert certificate is not None and certificate.state == "active"
        accepted = session.get(AgentCertificateRotation, NODE_ID)
        assert accepted is not None
        binding = CertificateIssuanceBinding.model_validate(accepted.provider_request)
    restarted = EnrollmentService(sessions, provider, clock=clock)
    issued = restarted.renew(NODE_ID, source.serial, rotation_csr)
    assert isinstance(issued, IssuedCertificate)
    assert issued.serial == binding.serial
    assert issued.generation == 2
    assert transport.issue_calls == 2
    with sessions() as session:
        certificate = session.get(AgentCertificate, source.serial)
        assert certificate is not None and certificate.state == "active"
        certificate = session.get(AgentCertificate, issued.serial)
        assert certificate is not None and certificate.state == "staged"


def test_unbound_enrollment_releases_claim_without_inventing_provider_authority(
    tmp_path,
):
    transport, provider, sessions, clock = setup(tmp_path)
    service = EnrollmentService(sessions, provider, clock=clock)
    request = csr()
    grant = service.create(NODE_ID, "admin", 600)
    transport.lose_response = True
    with pytest.raises(EnrollmentIssuanceUncertain):
        service.submit(grant.token, request, evidence(request))
    with sessions.begin() as session:
        accepted = session.scalar(select(AgentEnrollment))
        assert accepted is not None
        accepted.provider_request = None
    calls = len(transport.tokens)
    with pytest.raises(EnrollmentDenied, match="historical"):
        EnrollmentService(sessions, provider, clock=clock).submit(
            grant.token, request, evidence(request)
        )
    assert len(transport.tokens) == calls
    assert transport.issue_calls == 1
    with sessions() as session:
        assert session.scalar(select(AgentEnrollment)) is None
    # Missing journal authority cannot poison the next authorized same-node grant.
    transport.path = tmp_path / "fresh.json"
    restarted = EnrollmentService(sessions, provider, clock=clock)
    fresh_grant = restarted.create(NODE_ID, "admin", 600)
    fresh_request = csr()
    issued = restarted.submit(fresh_grant.token, fresh_request, evidence(fresh_request))
    assert isinstance(issued, IssuedCertificate)
    assert transport.issue_calls == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("serial", "42"),
        ("generation", 2),
        ("csr_sha256", "f" * 64),
        ("policy_sha256", "f" * 64),
    ],
)
def test_same_request_mutation_cannot_adopt_or_issue_other_effect(
    tmp_path, field, value
):
    transport, provider, _, _ = setup(tmp_path)
    request = csr()
    binding = provider.prepare_request(
        NODE_ID, request, NOW, purpose="enrollment", source_serial=None, generation=1
    )
    issued = provider.issue_node(NODE_ID, request, NOW, request=binding)
    changed = CertificateIssuanceBinding.model_validate(
        {**binding.model_dump(mode="json"), field: value}
    )
    with pytest.raises((StepCAError, ValueError)):
        provider.observe_node(request, NOW, request=changed)
    adopted = provider.observe_node(request, NOW, request=binding)
    assert adopted == issued
    assert transport.issue_calls == 1
