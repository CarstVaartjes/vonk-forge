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
from vonk_control.enrollment_contract import EnrollmentGrant
from vonk_control.models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    Base,
)
from vonk_control.pki import IssuedCertificate

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
        self.root = path.parent
        self.material: _Material | None = None
        self.issue_calls = 0
        self.tokens: list[str] = []
        self.lose_response = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        binding = CertificateIssuanceBinding.model_validate_json(
            json.dumps(body["request"])
        )
        self.path = self.root / f"{binding.request_id}.json"
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
    assert isinstance(grant, EnrollmentGrant)
    transport.lose_response = True
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
    journal = CertificateIssuedReply.model_validate_json(transport.path.read_bytes())
    assert issued.certificate_pem.decode() == journal.crt
    assert replay.certificate_pem.decode() == journal.crt
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
    assert transport.issue_calls == 2
    with sessions() as session:
        certificate = session.get(AgentCertificate, source.serial)
        assert certificate is not None and certificate.state == "active"
        certificate = session.get(AgentCertificate, issued.serial)
        assert certificate is not None and certificate.state == "staged"


def test_unbound_enrollment_releases_claim_without_inventing_provider_authority(
    tmp_path, monkeypatch
):
    transport, provider, sessions, clock = setup(tmp_path)
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
        assert transport.issue_calls == 1
    with sessions.begin() as session:
        accepted = session.scalar(select(AgentEnrollment))
        assert accepted is not None
        accepted.provider_request = None
    calls = len(transport.tokens)
    restarted = EnrollmentService(sessions, provider, clock=clock)
    restarted.submit(grant.token, request, evidence(request))
    assert len(transport.tokens) == calls
    with sessions() as session:
        assert session.scalar(select(AgentEnrollment)) is None
        assert session.scalar(select(AgentCertificate)) is None
    monkeypatch.setattr(issuance_module, "_persist_issued_enrollment", original)
    fresh = restarted.create(NODE_ID, "admin", 600)
    assert isinstance(fresh, EnrollmentGrant)
    issued = restarted.submit(fresh.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    assert issued.node_id == NODE_ID
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
    substituted = None
    try:
        substituted = provider.observe_node(request, NOW, request=changed)
    except Exception as error:  # noqa: BLE001 -- no error class decides acceptance
        assert transport.issue_calls == 1, str(error)
    assert substituted is None
    assert transport.issue_calls == 1
    adopted = provider.observe_node(request, NOW, request=binding)
    assert adopted == issued
    assert transport.issue_calls == 1
