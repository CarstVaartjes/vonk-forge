"""Connected enrollment recovery: exact effects, real ending and fresh admission."""

import json
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select
from vonk_agent_protocol.state_machines import (
    CertificateRecordState,
    CertificateRotationState,
    EnrollmentRecordState,
)
from vonk_control.enrollment_contract import EnrollmentGrant
from vonk_control.models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    AgentEnrollmentGrant,
    AgentIssuedCertificateRevocation,
)
from vonk_control.pki import IssuedCertificate

from .test_enrollment import NODE_ID, OTHER_NODE_ID, csr, enroll, evidence
from .test_enrollment import service as service  # noqa: PLC0414 -- fixture export


@pytest.mark.parametrize("generation", [2**31, 2**53 + 1, 2**64 - 1])
def test_accepted_generation_round_trips_without_client_or_storage_narrowing(
    service, generation
):
    """Catches int32 response limits and float-rounded relational projections."""
    from vonk_agent_protocol.enrollment import (
        ActivateRequest,
        IssuedCertificateResponse,
    )
    from vonk_control.enrollment.responses import _issued_response

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
    try:
        enrollment.renew(NODE_ID, source.serial, csr())
    except RuntimeError as error:
        assert calls, f"unexpected failure before provider effect: {error}"
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
    from vonk_control.ca_issuance_contract import CertificateIssuanceBinding

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
    from vonk_control.jobs import JobService
    from vonk_control.worker import Worker

    worker = Worker(
        JobService(sessions, clock=enrollment._clock),
        "enrollment-worker",
        {},
        background_services=(enrollment.reconcile_revocations,),
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


def test_consumed_grant_revocation_returns_status_without_revoking_node(service):
    enrollment, sessions, _, authority = service
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    issued = enrollment.submit(grant.token, request, evidence(request))
    first = enrollment.revoke_grant(grant.id, actor="admin")
    assert enrollment.revoke_grant(grant.id, actor="admin") == first
    with sessions() as session:
        assert session.get(AgentCertificate, issued.serial).revoked_at is None
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


@pytest.mark.parametrize(
    "reply", ["unavailable", "missing-fields", "oversized", "malformed-json"]
)
def test_real_ca_reply_unknown_replays_then_fresh_same_node_is_admitted(
    service, tmp_path, reply
):
    """Catches parsed 503/schema/reader failures being treated as bad authority."""
    import httpx2
    from vonk_agent_protocol import CertificateCode
    from vonk_agent_protocol.state_machines import (
        CertificateJournalState,
        CertificateRequestMode,
    )
    from vonk_control.enrollment import EnrollmentService

    from .test_step_ca import NOW, _provider, _success_response

    _, sessions, _, _ = service
    holder = {}
    fault = True
    requests = []

    def responder(request):
        requests.append(request)
        if fault:
            if reply == "unavailable":
                return httpx2.Response(
                    503,
                    json={
                        "reason_code": CertificateCode.ISSUANCE_UNAVAILABLE,
                        "detail": "journal unavailable",
                    },
                )
            if reply == "missing-fields":
                return httpx2.Response(200, json={"crt": "unreadable envelope"})
            if reply == "oversized":
                return httpx2.Response(200, content=b"x" * (64 * 1024 + 1))
            return httpx2.Response(200, content=b"{bad json")
        body = json.loads(request.content)
        if body["mode"] == CertificateRequestMode.OBSERVE:
            return httpx2.Response(
                200,
                json={
                    "state": CertificateJournalState.ABSENT,
                    "request": body["request"],
                },
            )
        return _success_response(request, holder["material"], [])

    provider, holder["material"] = _provider(
        tmp_path, responder, max_response_bytes=1024
    )
    enrollment = EnrollmentService(sessions, provider, clock=lambda: NOW)
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    enrollment.submit(grant.token, request, evidence(request))
    assert len(requests) == 4
    fault = False
    issued = enrollment.submit(grant.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    fresh = enrollment.create_reenrollment(
        NODE_ID, "admin", 600, request_key=str(uuid.uuid4())
    )
    assert isinstance(fresh, EnrollmentGrant)
    next_request = csr()
    assert isinstance(
        enrollment.submit(fresh.token, next_request, evidence(next_request)),
        IssuedCertificate,
    )
    provider.close()


def test_grant_admission_sql_conflicts_end_without_poisoning_request_identity(
    service, monkeypatch
):
    """Catches an exhausted admission keeping a gate or minting unrelated grants."""
    from contextlib import contextmanager

    from sqlalchemy.exc import OperationalError

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


def test_production_capability_does_not_put_health_http_inside_admission(
    service, tmp_path, monkeypatch
):
    """Catches a health preflight vetoing an exact, otherwise working CA request."""
    import httpx2
    from vonk_agent_protocol.state_machines import (
        CertificateJournalState,
        CertificateRequestMode,
    )
    from vonk_control.agent_services import build_agent_services
    from vonk_control.capabilities import CapabilityRegistry

    from .test_step_ca import NOW, _builder_settings, _provider, _success_response

    _, sessions, _, _ = service
    (tmp_path / "settings").mkdir()
    (tmp_path / "provider").mkdir()
    settings = _builder_settings(tmp_path / "settings", direct_fabric_cidrs="")
    health_calls = 0

    def responder(request):
        nonlocal health_calls
        if request.url.path == "/health":
            health_calls += 1
            return httpx2.Response(503, content=b"unreadable diagnostic")
        body = json.loads(request.content)
        if body["mode"] == CertificateRequestMode.OBSERVE:
            return httpx2.Response(
                200,
                json={
                    "state": CertificateJournalState.ABSENT,
                    "request": body["request"],
                },
            )
        return _success_response(request, material, [])

    provider, material = _provider(tmp_path / "provider", responder)
    monkeypatch.setattr(
        "vonk_control.step_ca.StepCertificateAuthority", lambda **kwargs: provider
    )
    registry = CapabilityRegistry(clock=lambda: NOW)
    services = build_agent_services(
        settings, sessions, lambda: NOW, capabilities=registry
    )
    assert services.enrollment is not None
    grant = services.enrollment.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    request = csr()
    issued = services.enrollment.submit(grant.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    assert health_calls == 0
    assert services.enrollment.submit(grant.token, request, evidence(request)) == issued
    provider.close()


def test_canonical_pending_owner_expires_after_restart_and_fresh_same_node_is_admitted(
    service, tmp_path
):
    """Catches a pending journal keeping a same-node owner forever after restart."""
    from datetime import timedelta

    import httpx2
    from vonk_agent_protocol import CertificateCode
    from vonk_agent_protocol.state_machines import (
        CertificateJournalState,
        CertificateRequestMode,
    )
    from vonk_control.enrollment import EnrollmentService

    from .test_step_ca import NOW, _provider, _success_response

    _, sessions, _, _ = service
    holder = {}
    old = None
    observations = []
    timestamp = NOW

    def responder(request):
        nonlocal old
        body = json.loads(request.content)
        binding = body["request"]
        observations.append(binding)
        if old is None:
            old = binding
        if binding == old:
            return httpx2.Response(
                200,
                json={
                    "state": CertificateJournalState.PENDING,
                    "request": binding,
                    "reason_code": CertificateCode.ISSUANCE_IN_PROGRESS,
                },
            )
        if body["mode"] == CertificateRequestMode.OBSERVE:
            return httpx2.Response(
                200, json={"state": CertificateJournalState.ABSENT, "request": binding}
            )
        return _success_response(request, holder["material"], [])

    provider, holder["material"] = _provider(tmp_path, responder)
    enrollment = EnrollmentService(sessions, provider, clock=lambda: timestamp)
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    enrollment.submit(grant.token, request, evidence(request))
    assert len(observations) == 4
    with sessions() as session:
        accepted = session.scalar(select(AgentEnrollment))
        assert accepted.state == EnrollmentRecordState.ISSUING
        original_created = accepted.created_at
        assert session.scalar(select(AgentCertificate)) is None
    timestamp += timedelta(seconds=301)
    restarted = EnrollmentService(sessions, provider, clock=lambda: timestamp)
    restarted.reconcile_revocations()
    with sessions() as session:
        accepted = session.scalar(select(AgentEnrollment))
        assert accepted.state == EnrollmentRecordState.ENDED
        assert accepted.created_at == original_created
    fresh = restarted.create(NODE_ID, "admin", 600)
    assert isinstance(fresh, EnrollmentGrant)
    issued = restarted.submit(fresh.token, request, evidence(request))
    assert isinstance(issued, IssuedCertificate)
    assert old is not None
    assert issued.serial != old["serial"]
    with sessions() as session:
        assert session.get(AgentCertificate, old["serial"]) is None
        assert session.get(AgentCertificate, issued.serial).revoked_at is None
    provider.close()


def test_controller_restart_after_ca_outage_serves_renewal_and_fresh_rotation(
    service, monkeypatch
):
    """Catches a standing renewal gate after an unavailable CA and lost process."""
    from vonk_agent_protocol import UnknownError
    from vonk_control.enrollment import EnrollmentService

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
    from vonk_agent_protocol.enrollment import IssuedCertificateResponse
    from vonk_control.api import create_app
    from vonk_control.enrollment import EnrollmentService

    from .test_agent_api import (
        NODE_A,
        CurrentAgentClient,
        Jobs,
        _csr_for,
        agent_headers,
        make_agent_system,
    )
    from .test_enrollment import RecordingAuthority

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
