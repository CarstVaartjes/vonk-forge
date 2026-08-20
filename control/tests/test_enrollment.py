import base64
import hashlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from vonk_control.enrollment import (
    EnrollmentDenied,
    EnrollmentService,
    RenewalInProgress,
    RenewalIssuanceUncertain,
)
from vonk_control.models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    AgentEnrollmentGrant,
    AgentIssuedCertificateRevocation,
    AgentNode,
    Base,
)
from vonk_control.pki import CertificateAuthority, IssuedCertificate

NODE_ID = "spk_0123456789abcdef0123456789abcdef"
OTHER_NODE_ID = "spk_fedcba9876543210fedcba9876543210"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingAuthority(CertificateAuthority):
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, datetime]] = []
        self._serial = 0
        self.revocations: list[str] = []
        self.revoke_failures: set[str] = set()
        self.renew_error: BaseException | None = None
        self.renew_request_ids: list[str] = []
        self.observe_renewal: Callable[[], None] | None = None

    def issue_node(
        self, node_id: str, public_key_pem: bytes, now: datetime
    ) -> IssuedCertificate:
        self.calls.append((node_id, public_key_pem, now))
        self._serial += 1
        return IssuedCertificate(
            node_id=node_id,
            certificate_pem=f"certificate-{self._serial}".encode(),
            chain_pem=b"intermediate-chain",
            serial=f"serial-{self._serial}",
            fingerprint=f"fingerprint-{self._serial}",
            not_before=now,
            not_after=now + timedelta(hours=24),
        )

    def renew_node(
        self,
        node_id: str,
        public_key_pem: bytes,
        now: datetime,
        *,
        request_id: str,
    ) -> IssuedCertificate:
        self.renew_request_ids.append(request_id)
        if self.observe_renewal is not None:
            self.observe_renewal()
        issued = self.issue_node(node_id, public_key_pem, now)
        if self.renew_error is not None:
            raise self.renew_error
        return issued

    def revocation_bundle(self, now: datetime) -> bytes:
        return b"revocation-bundle"

    def revoke_node(self, serial: str, now: datetime) -> None:
        self.revocations.append(serial)
        if serial in self.revoke_failures:
            raise RuntimeError("provider response deliberately lost")


class FailingIssuanceAuthority(RecordingAuthority):
    def issue_node(
        self, node_id: str, public_key_pem: bytes, now: datetime
    ) -> IssuedCertificate:
        self.calls.append((node_id, public_key_pem, now))
        raise RuntimeError("provider response deliberately lost")


class CompletedRenewalAuthority(RecordingAuthority):
    def __init__(self, *, crash_new_revocation: bool) -> None:
        super().__init__()
        self.completed = threading.Event()
        self.release = threading.Event()
        self.crash_new_revocation = crash_new_revocation
        self.crashed_serials: set[str] = set()

    def renew_node(
        self,
        node_id: str,
        public_key_pem: bytes,
        now: datetime,
        *,
        request_id: str,
    ) -> IssuedCertificate:
        issued = super().renew_node(
            node_id,
            public_key_pem,
            now,
            request_id=request_id,
        )
        self.completed.set()
        assert self.release.wait(timeout=5)
        return issued

    def revoke_node(self, serial: str, now: datetime) -> None:
        super().revoke_node(serial, now)
        if (
            self.crash_new_revocation
            and serial == "serial-2"
            and serial not in self.crashed_serials
        ):
            self.crashed_serials.add(serial)
            raise SystemExit("simulated crash during issued-certificate revocation")


def csr(node_id: str = NODE_ID) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, node_id)])
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{node_id}"
                    )
                ]
            ),
            critical=False,
        )
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )


def invalid_signature_csr() -> bytes:
    request = x509.load_pem_x509_csr(csr())
    der = bytearray(request.public_bytes(serialization.Encoding.DER))
    der[-1] ^= 1
    encoded = base64.b64encode(der)
    return (
        b"-----BEGIN CERTIFICATE REQUEST-----\n"
        + encoded
        + b"\n-----END CERTIFICATE REQUEST-----\n"
    )


def rsa_csr() -> bytes:
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_ID)])
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{NODE_ID}"
                    )
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )


def public_key_fingerprint(csr_pem: bytes) -> str:
    request = x509.load_pem_x509_csr(csr_pem)
    public_key = request.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(public_key).hexdigest()


def evidence(
    csr_pem: bytes, *, node_id: str = NODE_ID, **overrides: str
) -> dict[str, str]:
    result = {
        "node_id": node_id,
        "csr_public_key_fingerprint": public_key_fingerprint(csr_pem),
        "host_key_fingerprint": "SHA256:host-key",
        "hardware_fingerprint": "hardware-fingerprint",
        "agent_digest": "a" * 64,
        "boot_id": "b9e9b12a-63e4-4cb5-83f3-4d963d321ec8",
    }
    result.update(overrides)
    return result


@pytest.fixture
def service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'enrollment.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    clock = Clock()
    authority = RecordingAuthority()
    sessions = sessionmaker(engine, expire_on_commit=False)
    return (
        EnrollmentService(sessions, authority, clock=clock),
        sessions,
        clock,
        authority,
    )


def enroll(
    service: EnrollmentService, *, node_id: str = NODE_ID, request: bytes | None = None
):
    request = request or csr(node_id)
    grant = service.create(node_id, "admin", 600)
    return service.submit(grant.token, request, evidence(request, node_id=node_id))


@pytest.mark.parametrize("ttl_seconds", (0, 901))
def test_grant_creation_rejects_ttl_outside_bounded_contract(
    service, ttl_seconds: int
) -> None:
    enrollment, _, _, _ = service

    with pytest.raises(ValueError, match="between one and 900 seconds"):
        enrollment.create(NODE_ID, "admin", ttl_seconds)


def test_grant_creation_accepts_maximum_contract_ttl(service) -> None:
    enrollment, _, clock, _ = service

    grant = enrollment.create(NODE_ID, "admin", 900)

    assert grant.expires_at == clock.now + timedelta(seconds=900)


def test_grant_is_single_use_and_immediately_issues_authorized_certificate(
    service,
) -> None:
    enrollment, sessions, _, authority = service
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)

    assert len(base64.urlsafe_b64decode(grant.token + "=")) == 32
    assert "token" not in repr(grant)
    issued = enrollment.submit(grant.token, request, evidence(request))
    assert enrollment.submit(grant.token, request, evidence(request)) == issued

    assert issued.node_id == NODE_ID
    assert len(authority.calls) == 1
    with sessions() as session:
        stored_grant = session.scalar(select(AgentEnrollmentGrant))
        stored = session.scalar(select(AgentEnrollment))
        certificate = session.get(AgentCertificate, issued.serial)
        node = session.get(AgentNode, NODE_ID)
        assert (
            stored_grant is not None
            and stored_grant.token_digest
            == hashlib.sha256(base64.urlsafe_b64decode(grant.token + "=")).hexdigest()
        )
        assert stored_grant.purpose == "new-node"
        assert not hasattr(stored_grant, "token")
        assert stored is not None and stored.state == "approved"
        assert stored.decision_actor == "admin"
        assert stored.csr_public_key_fingerprint == public_key_fingerprint(request)
        assert certificate is not None and certificate.node_id == NODE_ID
        assert node is not None and node.state == "active"


def test_identity_free_grant_binds_node_from_submitted_csr(service) -> None:
    enrollment, _, _, _ = service
    grant = enrollment.create(None, "admin", 600)
    request = csr(NODE_ID)
    result = enrollment.submit(grant.token, request, evidence(request))
    assert result.node_id == NODE_ID


def test_submit_rejects_expired_malformed_and_evidence_mismatched_grants_without_leaking_token(
    service,
) -> None:
    enrollment, _, clock, _ = service
    request = csr()
    expired = enrollment.create(NODE_ID, "admin", 1)
    clock.advance(seconds=1)
    with pytest.raises(EnrollmentDenied, match="expired"):
        enrollment.submit(expired.token, request, evidence(request))
    with pytest.raises(EnrollmentDenied, match="invalid enrollment grant") as malformed:
        enrollment.submit("not a token", request, evidence(request))
    assert expired.token not in str(malformed.value)

    mismatched = enrollment.create(NODE_ID, "admin", 600)
    with pytest.raises(EnrollmentDenied, match="evidence"):
        enrollment.submit(
            mismatched.token, request, evidence(request, node_id=OTHER_NODE_ID)
        )
    with pytest.raises(EnrollmentDenied, match="consumed"):
        enrollment.submit(mismatched.token, request, evidence(request))


def test_submit_rejects_malformed_csr_and_csr_fingerprint_mismatch(service) -> None:
    enrollment, _, _, _ = service
    grant = enrollment.create(NODE_ID, "admin", 600)
    with pytest.raises(EnrollmentDenied, match="CSR"):
        enrollment.submit(grant.token, b"not a csr", {})

    request = csr()
    mismatch = enrollment.create(NODE_ID, "admin", 600)
    with pytest.raises(EnrollmentDenied, match="CSR public-key fingerprint"):
        enrollment.submit(
            mismatch.token,
            request,
            evidence(request, csr_public_key_fingerprint="0" * 64),
        )


@pytest.mark.parametrize(
    ("invalid_request", "message"),
    (
        (b"not a csr", "CSR must be valid PEM"),
        (invalid_signature_csr(), "CSR signature is invalid"),
        (rsa_csr(), "CSR public key must be Ed25519"),
    ),
    ids=("malformed", "invalid-signature", "unsupported-key"),
)
def test_identifiable_grant_is_consumed_when_csr_validation_fails(
    service, invalid_request: bytes, message: str
) -> None:
    enrollment, sessions, _, _ = service
    grant = enrollment.create(NODE_ID, "admin", 600)

    with pytest.raises(EnrollmentDenied, match=message):
        enrollment.submit(grant.token, invalid_request, {})
    with pytest.raises(EnrollmentDenied, match="consumed"):
        enrollment.submit(grant.token, csr(), evidence(csr()))
    with sessions() as session:
        stored_grant = session.get(AgentEnrollmentGrant, grant.id)
        assert stored_grant is not None and stored_grant.consumed_at is not None
        assert session.scalar(select(func.count()).select_from(AgentEnrollment)) == 0


def test_sqlite_simultaneous_exact_replay_is_idempotent(service) -> None:
    enrollment, _, _, _ = service
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    barrier = threading.Barrier(4)

    def submit() -> object:
        barrier.wait()
        try:
            return enrollment.submit(grant.token, request, evidence(request))
        except EnrollmentDenied as error:
            return error

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(), range(4)))

    assert all(result == results[0] for result in results)
    assert not isinstance(results[0], Exception)


def test_consumed_grant_denies_mismatched_replay_without_revealing_certificate(
    service,
) -> None:
    enrollment, _, _, _ = service
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)
    issued = enrollment.submit(grant.token, request, evidence(request))

    changed_evidence = evidence(request) | {"boot_id": "different-boot"}
    with pytest.raises(EnrollmentDenied, match="does not match") as mismatch:
        enrollment.submit(grant.token, request, changed_evidence)
    assert "certificate-" not in str(mismatch.value)
    with pytest.raises(EnrollmentDenied, match="does not match"):
        alternate = csr()
        enrollment.submit(grant.token, alternate, evidence(alternate))

    assert enrollment.submit(grant.token, request, evidence(request)) == issued
    with pytest.raises(EnrollmentDenied, match="does not match") as approved_mismatch:
        enrollment.submit(grant.token, request, changed_evidence)
    assert issued.certificate_pem.decode() not in str(approved_mismatch.value)


def test_renewal_stages_once_then_activation_atomically_retires_older_identity(
    service,
) -> None:
    enrollment, sessions, _clock, authority = service
    issued = enroll(enrollment)
    renewed_csr = csr()

    renewed = enrollment.renew(NODE_ID, issued.serial, renewed_csr)
    repeated = enrollment.renew(NODE_ID, issued.serial, renewed_csr)

    assert renewed.node_id == NODE_ID
    assert renewed.serial != issued.serial
    assert repeated == renewed
    assert len(authority.calls) == 2
    assert authority.calls[-1][1] == x509.load_pem_x509_csr(renewed_csr).public_bytes(
        serialization.Encoding.PEM
    )
    with sessions() as session:
        original = session.get(AgentCertificate, issued.serial)
        staged = session.get(AgentCertificate, renewed.serial)
        assert (
            original is not None
            and original.revoked_at is None
            and original.state == "active"
        )
        assert original.generation == 1
        assert (
            staged is not None
            and staged.revoked_at is None
            and staged.state == "staged"
        )
        assert staged.generation == 2
    with pytest.raises(EnrollmentDenied, match="staged|rotation"):
        enrollment.renew(NODE_ID, issued.serial, csr())
    with pytest.raises(EnrollmentDenied, match="active"):
        enrollment.renew(NODE_ID, renewed.serial, csr())

    enrollment.activate(NODE_ID, renewed.serial, renewed.generation)
    enrollment.activate(NODE_ID, renewed.serial, renewed.generation)

    with sessions() as session:
        original = session.get(AgentCertificate, issued.serial)
        active = session.get(AgentCertificate, renewed.serial)
        assert (
            original is not None
            and original.revoked_at is not None
            and original.state == "revoked"
        )
        assert (
            active is not None
            and active.revoked_at is None
            and active.state == "active"
        )
    with pytest.raises(EnrollmentDenied, match="serial"):
        enrollment.renew(OTHER_NODE_ID, renewed.serial, csr(OTHER_NODE_ID))
    with pytest.raises(EnrollmentDenied, match="CSR"):
        enrollment.renew(NODE_ID, renewed.serial, b"not a csr")
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_ID)
        assert node is not None
        node.state = "retired"
    with pytest.raises(EnrollmentDenied, match="retired|revoked"):
        enrollment.renew(NODE_ID, renewed.serial, csr())


def test_renewal_intent_is_committed_before_provider_call(service) -> None:
    enrollment, sessions, _clock, authority = service
    issued = enroll(enrollment)
    request = csr()

    def observe() -> None:
        with sessions() as session:
            intent = session.get(AgentCertificateRotation, NODE_ID)
            assert intent is not None
            assert intent.source_serial == issued.serial
            assert intent.state == "issuing"
            assert intent.csr_public_key_fingerprint == public_key_fingerprint(request)
            assert intent.provider_request_id == authority.renew_request_ids[-1]
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AgentCertificate)
                    .where(AgentCertificate.state == "staged")
                )
                == 0
            )

    authority.observe_renewal = observe

    renewed = enrollment.renew(NODE_ID, issued.serial, request)

    assert renewed.generation == 2


def test_renewal_provider_exception_is_durable_manual_recovery_without_reissue(
    service,
) -> None:
    enrollment, sessions, _clock, authority = service
    issued = enroll(enrollment)
    request = csr()
    authority.renew_error = RuntimeError("provider response deliberately lost")

    with pytest.raises(RenewalIssuanceUncertain, match="manual recovery"):
        enrollment.renew(NODE_ID, issued.serial, request)
    with pytest.raises(RenewalIssuanceUncertain, match="manual recovery"):
        enrollment.renew(NODE_ID, issued.serial, request)

    assert len(authority.calls) == 2
    assert len(authority.renew_request_ids) == 1
    with sessions() as session:
        intent = session.get(AgentCertificateRotation, NODE_ID)
        assert intent is not None and intent.state == "manual-recovery"
        assert intent.provider_request_id == authority.renew_request_ids[0]
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentCertificate)
                .where(AgentCertificate.state == "staged")
            )
            == 0
        )


def test_process_death_leaves_inspectable_intent_then_becomes_terminal_without_reissue(
    service,
) -> None:
    enrollment, sessions, clock, authority = service
    issued = enroll(enrollment)
    request = csr()
    authority.renew_error = SystemExit("simulated process death after provider request")

    with pytest.raises(SystemExit, match="simulated process death"):
        enrollment.renew(NODE_ID, issued.serial, request)
    authority.renew_error = None
    restarted = EnrollmentService(sessions, authority, clock=clock)

    with pytest.raises(RenewalInProgress, match="in progress"):
        restarted.renew(NODE_ID, issued.serial, request)
    clock.advance(seconds=301)
    with pytest.raises(RenewalIssuanceUncertain, match="manual recovery"):
        restarted.renew(NODE_ID, issued.serial, request)

    assert len(authority.calls) == 2
    assert len(authority.renew_request_ids) == 1
    with sessions() as session:
        intent = session.get(AgentCertificateRotation, NODE_ID)
        assert intent is not None and intent.state == "manual-recovery"


def test_renewal_persistence_ambiguity_is_terminal_without_reissue(service) -> None:
    enrollment, sessions, clock, authority = service
    issued = enroll(enrollment)
    request = csr()
    with sessions.begin() as session:
        session.add(AgentNode(node_id=OTHER_NODE_ID, state="active", capabilities=[]))
        session.add(
            AgentCertificate(
                serial="serial-2",
                node_id=OTHER_NODE_ID,
                not_before=clock.now,
                not_after=clock.now + timedelta(hours=1),
                fingerprint="fingerprint-2",
            )
        )

    with pytest.raises(RenewalIssuanceUncertain, match="manual recovery"):
        enrollment.renew(NODE_ID, issued.serial, request)
    with pytest.raises(RenewalIssuanceUncertain, match="manual recovery"):
        enrollment.renew(NODE_ID, issued.serial, request)

    assert len(authority.calls) == 2
    assert len(authority.renew_request_ids) == 1
    with sessions() as session:
        intent = session.get(AgentCertificateRotation, NODE_ID)
        assert intent is not None and intent.state == "manual-recovery"


def test_sqlite_simultaneous_exact_renewal_issues_one_staged_generation(
    service,
) -> None:
    enrollment, sessions, clock, _ = service
    issued = enroll(enrollment)
    renewed_csr = csr()
    authority = PausingAuthority()
    authority._serial = 1
    enrollment._authority = authority
    follower = EnrollmentService(sessions, authority, clock=clock)
    results: list[object] = []

    def renew() -> None:
        try:
            results.append(enrollment.renew(NODE_ID, issued.serial, renewed_csr))
        except (EnrollmentDenied, SQLAlchemyError) as error:
            results.append(error)

    first = threading.Thread(target=renew)
    first.start()
    assert authority.entered.wait(timeout=5)
    with pytest.raises(RenewalInProgress, match="in progress"):
        follower.renew(NODE_ID, issued.serial, renewed_csr)
    authority.release.set()
    first.join(timeout=5)

    assert len(results) == 1
    assert isinstance(results[0], IssuedCertificate)
    assert follower.renew(NODE_ID, issued.serial, renewed_csr) == results[0]
    assert len(authority.calls) == 1


def test_revoked_identity_denies_renewal_immediately(service) -> None:
    enrollment, sessions, clock, _ = service
    issued = enroll(enrollment)
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, issued.serial)
        assert certificate is not None
        certificate.revoked_at = clock.now

    with pytest.raises(EnrollmentDenied, match="retired|revoked"):
        enrollment.renew(NODE_ID, issued.serial, csr())


def test_local_revocation_precedes_remote_and_retry_calls_only_unconfirmed_serials(
    service,
) -> None:
    enrollment, sessions, clock, authority = service
    issued = enroll(enrollment)
    with sessions.begin() as session:
        session.add(
            AgentCertificate(
                serial="serial-2",
                node_id=NODE_ID,
                fingerprint="fingerprint-2",
                not_before=clock.now,
                not_after=clock.now + timedelta(hours=24),
                generation=2,
            )
        )
    authority.revoke_failures.add("serial-2")

    with pytest.raises(EnrollmentDenied, match="remote CA revocation is uncertain"):
        enrollment.revoke_node(NODE_ID, "admin")

    with sessions() as session:
        node = session.get(AgentNode, NODE_ID)
        first = session.get(AgentCertificate, issued.serial)
        second = session.get(AgentCertificate, "serial-2")
        assert (
            node is not None and node.state == "retired" and node.revoked_at is not None
        )
        assert (
            first is not None
            and first.revoked_at is not None
            and first.ca_revoked_at is not None
        )
        assert (
            second is not None
            and second.revoked_at is not None
            and second.ca_revoked_at is None
        )
    assert authority.revocations == [issued.serial, "serial-2"]

    authority.revoke_failures.clear()
    enrollment.revoke_node(NODE_ID, "admin")
    assert authority.revocations == [issued.serial, "serial-2", "serial-2"]
    with sessions() as session:
        assert session.get(AgentCertificate, "serial-2").ca_revoked_at is not None  # type: ignore[union-attr]


@pytest.mark.parametrize("crash_new_revocation", (False, True))
def test_postgres_retirement_wins_completed_rotation_and_reconciles_issued_serial(
    postgres_engine: Engine,
    crash_new_revocation: bool,
) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    initial = EnrollmentService(sessions, RecordingAuthority(), clock=clock)
    source = enroll(initial)
    authority = CompletedRenewalAuthority(crash_new_revocation=crash_new_revocation)
    authority._serial = 1
    rotating = EnrollmentService(sessions, authority, clock=clock)
    revoking = EnrollmentService(sessions, authority, clock=clock)
    request = csr()
    revocation_locked = threading.Event()
    release_revocation = threading.Event()
    renewal_results: list[object] = []
    revocation_errors: list[BaseException] = []

    def pause_after_revocation_node_lock(
        _conn, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if (
            threading.current_thread().name == "revoker"
            and "FROM agent_nodes" in statement
            and "FOR UPDATE OF agent_nodes" in statement
        ):
            revocation_locked.set()
            assert release_revocation.wait(timeout=5)

    def renew() -> None:
        try:
            renewal_results.append(rotating.renew(NODE_ID, source.serial, request))
        except BaseException as error:  # noqa: BLE001 - deliberate process-death regression
            renewal_results.append(error)

    def revoke() -> None:
        try:
            revoking.revoke_node(NODE_ID, "admin")
        except BaseException as error:  # noqa: BLE001 - thread must report SystemExit
            revocation_errors.append(error)

    event.listen(
        postgres_engine, "after_cursor_execute", pause_after_revocation_node_lock
    )
    try:
        renewer = threading.Thread(target=renew, name="renewer")
        revoker = threading.Thread(target=revoke, name="revoker")
        renewer.start()
        assert authority.completed.wait(timeout=5)
        revoker.start()
        assert revocation_locked.wait(timeout=5)
        authority.release.set()
        time.sleep(0.25)
        assert renewer.is_alive(), (
            "rotation persistence must wait for revocation's node lock"
        )
        release_revocation.set()
        revoker.join(timeout=5)
        renewer.join(timeout=5)
    finally:
        authority.release.set()
        release_revocation.set()
        event.remove(
            postgres_engine, "after_cursor_execute", pause_after_revocation_node_lock
        )

    assert not renewer.is_alive() and not revoker.is_alive()
    assert not revocation_errors
    assert len(renewal_results) == 1
    if crash_new_revocation:
        assert isinstance(renewal_results[0], SystemExit)
    else:
        assert isinstance(renewal_results[0], EnrollmentDenied)
    with sessions() as session:
        node = session.get(AgentNode, NODE_ID)
        original = session.get(AgentCertificate, source.serial)
        issued = session.get(AgentCertificate, "serial-2")
        intent = session.get(AgentCertificateRotation, NODE_ID)
        assert node is not None and node.state == "retired"
        assert original is not None and original.state == "revoked"
        assert issued is not None and issued.state == "revoked"
        assert issued.revoked_at is not None
        assert intent is not None
        if crash_new_revocation:
            assert issued.ca_revoked_at is None
            assert intent.state == "revocation-pending"
        else:
            assert issued.ca_revoked_at is not None
            assert intent.state == "revoked"
    assert authority.revocations == [source.serial, "serial-2"]

    if crash_new_revocation:
        revoking.revoke_node(NODE_ID, "admin")
        revoking.revoke_node(NODE_ID, "admin")
        assert authority.revocations == [
            source.serial,
            "serial-2",
            "serial-2",
        ]
        with sessions() as session:
            issued = session.get(AgentCertificate, "serial-2")
            intent = session.get(AgentCertificateRotation, NODE_ID)
            assert issued is not None and issued.ca_revoked_at is not None
            assert intent is not None and intent.state == "revoked"


def test_postgres_approved_node_cannot_be_deleted_while_certificate_exists(
    postgres_engine: Engine,
) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    enrollment = EnrollmentService(sessions, RecordingAuthority(), clock=clock)
    enroll(enrollment)

    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.execute(delete(AgentNode).where(AgentNode.node_id == NODE_ID))

    with sessions() as session:
        assert session.get(AgentNode, NODE_ID) is not None


@pytest.mark.parametrize("failure_mode", ("success", "runtime", "system-exit"))
def test_postgres_missing_node_after_completed_rotation_retains_recovery_evidence(
    postgres_engine: Engine,
    failure_mode: str,
) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    initial = EnrollmentService(sessions, RecordingAuthority(), clock=clock)
    source = enroll(initial)
    authority = CompletedRenewalAuthority(
        crash_new_revocation=failure_mode == "system-exit"
    )
    authority._serial = 1
    if failure_mode == "runtime":
        authority.revoke_failures.add("serial-2")
    rotating = EnrollmentService(sessions, authority, clock=clock)
    reconciling = EnrollmentService(sessions, authority, clock=clock)
    results: list[object] = []

    def renew() -> None:
        try:
            results.append(rotating.renew(NODE_ID, source.serial, csr()))
        except BaseException as error:  # noqa: BLE001 - deliberate process-death regression
            results.append(error)

    renewer = threading.Thread(target=renew, name="renewer")
    renewer.start()
    assert authority.completed.wait(timeout=5)
    with sessions.begin() as session:
        session.execute(
            delete(AgentCertificate).where(AgentCertificate.node_id == NODE_ID)
        )
        session.execute(delete(AgentNode).where(AgentNode.node_id == NODE_ID))
    authority.release.set()
    renewer.join(timeout=5)

    assert not renewer.is_alive()
    assert len(results) == 1
    if failure_mode == "success":
        assert isinstance(results[0], EnrollmentDenied)
    elif failure_mode == "runtime":
        assert isinstance(results[0], RenewalIssuanceUncertain)
    else:
        assert isinstance(results[0], SystemExit)
    assert authority.revocations == ["serial-2"]
    with sessions() as session:
        evidence = session.get(AgentIssuedCertificateRevocation, "serial-2")
        assert evidence is not None
        assert evidence.node_id == NODE_ID
        assert evidence.provider_request_id == authority.renew_request_ids[0]
        assert evidence.fingerprint == "fingerprint-2"
        assert evidence.generation == 2
        assert evidence.state == (
            "revoked" if failure_mode == "success" else "revocation-pending"
        )
        if failure_mode == "success":
            assert evidence.ca_revoked_at is not None
        else:
            assert evidence.ca_revoked_at is None

    if failure_mode != "success":
        authority.revoke_failures.clear()
        reconciling.revoke_node(NODE_ID, "admin")
        reconciling.revoke_node(NODE_ID, "admin")
        assert authority.revocations == ["serial-2", "serial-2"]
        with sessions() as session:
            evidence = session.get(AgentIssuedCertificateRevocation, "serial-2")
            assert evidence is not None and evidence.state == "revoked"
            assert evidence.ca_revoked_at is not None


def test_postgres_separate_services_return_one_idempotent_exact_replay(
    postgres_engine: Engine,
) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    authority = RecordingAuthority()
    first = EnrollmentService(sessions, authority, clock=clock)
    second = EnrollmentService(sessions, authority, clock=clock)
    request = csr()
    grant = first.create(NODE_ID, "admin", 600)
    barrier = threading.Barrier(2)

    def submit(service: EnrollmentService) -> object:
        barrier.wait()
        try:
            return service.submit(grant.token, request, evidence(request))
        except EnrollmentDenied as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (first, second)))

    assert all(result == results[0] for result in results)
    assert not isinstance(results[0], Exception)


def test_postgres_enrollment_persists_node_before_certificate(
    postgres_engine: Engine,
) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    authority = RecordingAuthority()
    enrollment = EnrollmentService(sessions, authority, clock=clock)
    issued = enroll(enrollment)

    with sessions() as session:
        assert session.get(AgentNode, NODE_ID) is not None
        assert session.get(AgentCertificate, issued.serial) is not None
        stored = session.scalar(select(AgentEnrollment))
        assert (
            stored is not None
            and stored.state == "approved"
            and stored.certificate_serial == issued.serial
        )


class PausingAuthority(RecordingAuthority):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def issue_node(
        self, node_id: str, public_key_pem: bytes, now: datetime
    ) -> IssuedCertificate:
        with self._lock:
            self.calls.append((node_id, public_key_pem, now))
        self.entered.set()
        assert self.release.wait(timeout=5)
        with self._lock:
            self._serial += 1
            serial = self._serial
        return IssuedCertificate(
            node_id=node_id,
            certificate_pem=f"certificate-{serial}".encode(),
            chain_pem=b"intermediate-chain",
            serial=f"serial-{serial}",
            fingerprint=f"fingerprint-{serial}",
            not_before=now,
            not_after=now + timedelta(hours=24),
        )


def test_postgres_same_node_enrollment_race_issues_exactly_once(
    postgres_engine: Engine,
) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    authority = PausingAuthority()
    first = EnrollmentService(sessions, authority, clock=clock)
    second = EnrollmentService(sessions, authority, clock=clock)
    first_request = csr()
    second_request = csr()
    first_grant = first.create(NODE_ID, "admin", 600)
    second_grant = second.create(NODE_ID, "admin", 600)
    results: list[object] = []

    def submit(service: EnrollmentService, token: str, request: bytes) -> None:
        try:
            results.append(service.submit(token, request, evidence(request)))
        except EnrollmentDenied as error:
            results.append(error)

    first_thread = threading.Thread(
        target=submit, args=(first, first_grant.token, first_request)
    )
    second_thread = threading.Thread(
        target=submit, args=(second, second_grant.token, second_request)
    )
    first_thread.start()
    assert authority.entered.wait(timeout=5)
    second_thread.start()
    time.sleep(0.25)
    authority.release.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert len(authority.calls) == 1
    assert sum(isinstance(result, IssuedCertificate) for result in results) == 1
    assert sum(isinstance(result, EnrollmentDenied) for result in results) == 1
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentNode)) == 1
        assert session.scalar(select(func.count()).select_from(AgentCertificate)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AgentEnrollment)
                .where(AgentEnrollment.state == "approved")
            )
            == 1
        )


def test_postgres_separate_services_never_duplicate_in_progress_renewal(
    postgres_engine: Engine,
) -> None:
    Base.metadata.drop_all(postgres_engine)
    Base.metadata.create_all(postgres_engine)
    clock = Clock()
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    initial = EnrollmentService(sessions, RecordingAuthority(), clock=clock)
    issued = enroll(initial)
    request = csr()
    authority = PausingAuthority()
    authority._serial = 1
    owner = EnrollmentService(sessions, authority, clock=clock)
    follower = EnrollmentService(sessions, authority, clock=clock)
    results: list[IssuedCertificate] = []

    thread = threading.Thread(
        target=lambda: results.append(owner.renew(NODE_ID, issued.serial, request))
    )
    thread.start()
    assert authority.entered.wait(timeout=5)

    with pytest.raises(RenewalInProgress, match="in progress"):
        follower.renew(NODE_ID, issued.serial, request)
    assert len(authority.calls) == 1

    authority.release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(results) == 1
    assert follower.renew(NODE_ID, issued.serial, request) == results[0]
    assert len(authority.calls) == 1


def test_enrollment_persistence_failure_stays_recoverable_without_reissuing(
    service,
) -> None:
    enrollment, sessions, clock, authority = service
    with sessions.begin() as session:
        session.add(AgentNode(node_id=OTHER_NODE_ID, state="active", capabilities=[]))
        session.add(
            AgentCertificate(
                serial="serial-1",
                node_id=OTHER_NODE_ID,
                not_before=clock.now,
                not_after=clock.now + timedelta(hours=1),
                fingerprint="existing-fingerprint",
            )
        )
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)

    with pytest.raises(EnrollmentDenied, match="manual recovery"):
        enrollment.submit(grant.token, request, evidence(request))

    assert len(authority.calls) == 1
    with sessions() as session:
        stored = session.scalar(select(AgentEnrollment))
        assert stored is not None and stored.state == "issuing"
        assert session.get(AgentNode, NODE_ID) is None


def test_provider_failure_is_durable_uncertain_and_exact_replay_never_reissues(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'uncertain.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    authority = FailingIssuanceAuthority()
    enrollment = EnrollmentService(
        sessions,
        authority,
        clock=Clock(),
        issuance_replay_wait_seconds=0.01,
    )
    request = csr()
    grant = enrollment.create(NODE_ID, "admin", 600)

    with pytest.raises(EnrollmentDenied, match="uncertain"):
        enrollment.submit(grant.token, request, evidence(request))
    with pytest.raises(EnrollmentDenied, match="uncertain"):
        enrollment.submit(grant.token, request, evidence(request))

    assert len(authority.calls) == 1
    with sessions() as session:
        stored = session.scalar(select(AgentEnrollment))
        assert stored is not None and stored.state == "issuing"
