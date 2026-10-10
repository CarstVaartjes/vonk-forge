"""Controller CA provider using the existing installer intermediate.

Use the installer's step-ca-intermediate-key and step-ca-password secrets.
Signing occurs outside SQL transactions. Ed25519 produces identical bytes for
an immutable binding, so any worker can finish a committed PENDING request
without leases or a permanently poisoned owner after process death.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import CertificateCode, ErrorCategory, UnknownError, WaitReason
from vonk_agent_protocol.state_machines import (
    CertificateIssuancePurpose,
    CertificateRequestMode,
)

from .ca_issuance_contract import CertificateIssuanceBinding
from .models.fleet import (
    AgentCertificate,
    AgentIssuedCertificateRevocation,
    LocalCertificateIssuance,
    LocalCertificateRevocation,
)
from .pki import (
    IssuedCertificate,
    _load_node_csr,
    _read_regular_secret_file,
    _utc_timestamp,
)
from .step_ca import (
    _DEFAULT_CERTIFICATE_LIFETIME_SECONDS,
    _KID,
    _MAX_CRL_WINDOW,
    _NAME,
    NodeCertificateAuthority,
    StepCAError,
    StepCAIssuancePending,
    StepCAUnavailable,
    _one_certificate,
    _verify_ca_chain,
)


class LocalCertificateAuthority(NodeCertificateAuthority):
    """The existing CA boundary backed by the same intermediate and PostgreSQL."""

    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        root_certificate_path: Path | str,
        intermediate_certificate_path: Path | str,
        intermediate_key_path: Path | str,
        password_path: Path | str,
        provisioner_name: str,
        provisioner_kid: str,
    ) -> None:
        self._root = _one_certificate(
            _read_regular_secret_file(root_certificate_path), "root"
        )
        self._intermediate = _one_certificate(
            _read_regular_secret_file(intermediate_certificate_path), "intermediate"
        )
        _verify_ca_chain(self._root, self._intermediate)
        key = serialization.load_pem_private_key(
            _read_regular_secret_file(intermediate_key_path),
            password=_read_regular_secret_file(password_path).strip(),
        )
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise ValueError("local intermediate key must be Ed25519")  # noqa: TRY004
        if (
            key.public_key().public_bytes_raw()
            != self._intermediate.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ):
            raise ValueError("local intermediate key does not match certificate")
        if (
            _NAME.fullmatch(provisioner_name) is None
            or _KID.fullmatch(provisioner_kid) is None
        ):
            raise ValueError("local CA provisioner identity is invalid")
        self._key = key
        self._sessions = sessions
        self._provisioner_name = provisioner_name
        self._provisioner_kid = provisioner_kid
        self._certificate_lifetime_seconds = _DEFAULT_CERTIFICATE_LIFETIME_SECONDS
        self._certificate_lifetime = timedelta(
            seconds=self._certificate_lifetime_seconds
        )
        self._import_revocations()

    def _import_revocations(self) -> None:
        """Carry durable pre-cutover revocation intent, including lost replies.

        Enrollment persists intent before calling the CA. Accepted certificates
        and node-independent late effects therefore retain every Controller
        revocation even when step-ca succeeded but its response was lost.
        Repeating this transaction never overwrites an existing revocation.
        """
        with self._session(write=True) as session:
            for serial, revoked_at in session.execute(
                select(
                    AgentCertificate.serial,
                    AgentCertificate.revoked_at,
                ).where(AgentCertificate.revoked_at.is_not(None))
            ):
                session.execute(
                    insert(LocalCertificateRevocation)
                    .values(serial=serial, revoked_at=revoked_at)
                    .on_conflict_do_nothing()
                )
            for serial, revoked_at in session.execute(
                select(
                    AgentCertificate.serial,
                    AgentCertificate.ca_revoked_at,
                ).where(AgentCertificate.ca_revoked_at.is_not(None))
            ):
                session.execute(
                    insert(LocalCertificateRevocation)
                    .values(serial=serial, revoked_at=revoked_at)
                    .on_conflict_do_nothing()
                )
            for serial, revoked_at in session.execute(
                select(
                    AgentIssuedCertificateRevocation.serial,
                    AgentIssuedCertificateRevocation.created_at,
                )
            ):
                session.execute(
                    insert(LocalCertificateRevocation)
                    .values(serial=serial, revoked_at=revoked_at)
                    .on_conflict_do_nothing()
                )

    def close(self) -> None:
        """No transport resources to close."""

    @contextmanager
    def _session(self, *, write: bool = False) -> Iterator[Session]:
        try:
            with self._sessions() as session:
                if write:
                    with session.begin():
                        yield session
                else:
                    yield session
        except SQLAlchemyError as error:
            raise StepCAUnavailable("local CA journal is unavailable") from error

    def check_health(self) -> None | UnknownError:
        try:
            with self._session() as session:
                session.execute(select(LocalCertificateIssuance.request_id).limit(1))
        except StepCAUnavailable:
            return UnknownError(
                category=ErrorCategory.UNKNOWN,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return None

    def issue_node(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request: CertificateIssuanceBinding,
    ) -> IssuedCertificate:
        if node_id != request.node_id:
            raise ValueError("CA request node identity does not match")
        result = self._sign(
            csr_pem, now, request=request, mode=CertificateRequestMode.ISSUE
        )
        assert result is not None
        return result

    def observe_node(
        self, csr_pem: bytes, now: datetime, *, request: CertificateIssuanceBinding
    ) -> IssuedCertificate | None:
        return self._sign(
            csr_pem, now, request=request, mode=CertificateRequestMode.OBSERVE
        )

    def renew_node(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request: CertificateIssuanceBinding,
    ) -> IssuedCertificate:
        if request.purpose != CertificateIssuancePurpose.ROTATION:
            raise ValueError("renewal requires a rotation binding")
        return self.issue_node(node_id, csr_pem, now, request=request)

    def _locks(self, session: Session, *identities: str) -> None:
        # Nonblocking, common numeric order; transactions never span signing.
        keys = sorted(
            {
                int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], signed=True)
                for value in identities
            }
        )
        for key in keys:
            if not session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
            ):
                raise StepCAIssuancePending("local CA journal is busy")

    def _refuse(self, code: CertificateCode) -> None:
        raise StepCAError(code.value, reason_code=code)

    def _source(self, session: Session, request: CertificateIssuanceBinding) -> None:
        serial = request.source_serial
        if serial is None:
            return
        if session.get(LocalCertificateRevocation, serial) is not None:
            self._refuse(CertificateCode.SOURCE_REVOKED)
        source = session.scalar(
            select(LocalCertificateIssuance).where(
                LocalCertificateIssuance.serial == serial
            )
        )
        existing = session.get(AgentCertificate, serial)
        if existing is not None and existing.ca_revoked_at is not None:
            self._refuse(CertificateCode.SOURCE_REVOKED)
        pem = (
            source.certificate_pem
            if source is not None
            else (existing.certificate_pem if existing is not None else None)
        )
        if pem is None:
            self._refuse(CertificateCode.SOURCE_IDENTITY_REFUSED)
        assert pem is not None
        leaf = _one_certificate(pem.encode("ascii"), "source", provider_error=True)
        try:
            leaf.verify_directly_issued_by(self._intermediate)
            if (
                leaf.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
                != request.node_id
            ):
                self._refuse(CertificateCode.SOURCE_IDENTITY_REFUSED)
            if leaf.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value != x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{request.node_id}"
                    )
                ]
            ):
                self._refuse(CertificateCode.SOURCE_IDENTITY_REFUSED)
        except (
            InvalidSignature,
            ValueError,
            IndexError,
            x509.ExtensionNotFound,
        ) as error:
            raise StepCAError(
                "rotation source identity is invalid",
                reason_code=CertificateCode.SOURCE_IDENTITY_REFUSED,
            ) from error

    def _sign(
        self,
        csr_pem: bytes,
        now: datetime,
        *,
        request: CertificateIssuanceBinding,
        mode: CertificateRequestMode,
    ) -> IssuedCertificate | None:
        _utc_timestamp(now)
        # Validate persisted documents even when a caller used model_construct.
        request = CertificateIssuanceBinding.model_validate_json(
            request.model_dump_json()
        )
        csr = _load_node_csr(request.node_id, csr_pem)
        expected = self.prepare_request(
            request.node_id,
            csr_pem,
            datetime.fromisoformat(request.not_before),
            purpose=request.purpose,
            source_serial=request.source_serial,
            generation=request.generation,
        )
        if any(
            getattr(request, field) != getattr(expected, field)
            for field in (
                "csr_sha256",
                "issuer_fingerprint",
                "provisioner_name",
                "provisioner_kid",
                "policy_sha256",
                "not_after",
            )
        ):
            self._refuse(CertificateCode.REQUEST_INVALID)
        identities = ("request:" + request.request_id, "serial:" + request.serial)
        if request.source_serial is not None:
            identities += ("serial:" + request.source_serial,)
        with self._session(write=True) as session:
            self._locks(session, *identities)
            row = session.get(LocalCertificateIssuance, request.request_id)
            if row is not None:
                if (
                    CertificateIssuanceBinding.model_validate_json(
                        json.dumps(row.binding)
                    )
                    != request
                ):
                    self._refuse(CertificateCode.REQUEST_BINDING_MISMATCH)
                if session.get(LocalCertificateRevocation, request.serial) is not None:
                    self._refuse(CertificateCode.ISSUANCE_REVOKED)
                if row.certificate_pem is not None:
                    return self._issued(request, csr, row.certificate_pem)
            elif mode == CertificateRequestMode.ISSUE:
                if (
                    session.scalar(
                        select(LocalCertificateIssuance.request_id).where(
                            LocalCertificateIssuance.serial == request.serial
                        )
                    )
                    is not None
                ):
                    self._refuse(CertificateCode.SERIAL_ALREADY_RESERVED)
                if session.get(AgentCertificate, request.serial) is not None:
                    self._refuse(CertificateCode.SERIAL_ALREADY_ISSUED)
            self._source(session, request)
            if mode == CertificateRequestMode.OBSERVE:
                if row is None:
                    return None
                raise StepCAIssuancePending(CertificateCode.ISSUANCE_IN_PROGRESS)
            if row is None:
                session.add(
                    LocalCertificateIssuance(
                        request_id=request.request_id,
                        serial=request.serial,
                        binding=request.model_dump(mode="json"),
                    )
                )
        # A crash here leaves PENDING. Replaying signs precisely the same bytes.
        pem = (
            self._certificate(request, csr)
            .public_bytes(serialization.Encoding.PEM)
            .decode("ascii")
        )
        with self._session(write=True) as session:
            self._locks(session, *identities)
            row = session.get(LocalCertificateIssuance, request.request_id)
            if row is None:
                self._refuse(CertificateCode.ATTEMPT_SUPERSEDED)
            assert row is not None
            if (
                CertificateIssuanceBinding.model_validate_json(json.dumps(row.binding))
                != request
            ):
                self._refuse(CertificateCode.REQUEST_BINDING_MISMATCH)
            if session.get(LocalCertificateRevocation, request.serial) is not None:
                self._refuse(CertificateCode.ISSUANCE_REVOKED)
            if row.certificate_pem is None:
                try:
                    self._source(session, request)
                except StepCAError as error:
                    if error.reason_code == CertificateCode.SOURCE_REVOKED:
                        self._refuse(CertificateCode.ROTATION_SOURCE_REVOKED)
                    raise
                row.certificate_pem = pem
            elif row.certificate_pem != pem:
                raise StepCAError(
                    "local CA committed certificate differs from exact effect"
                )
        return self._issued(request, csr, pem)

    def _certificate(
        self, request: CertificateIssuanceBinding, csr: x509.CertificateSigningRequest
    ) -> x509.Certificate:
        builder = (
            x509.CertificateBuilder()
            .subject_name(csr.subject)
            .issuer_name(self._intermediate.subject)
            .public_key(csr.public_key())
            .serial_number(int(request.serial))
            .not_valid_before(datetime.fromisoformat(request.not_before))
            .not_valid_after(datetime.fromisoformat(request.not_after))
            .add_extension(
                x509.KeyUsage(
                    True, False, False, False, False, False, False, False, False
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.UniformResourceIdentifier(
                            f"spiffe://vonk-forge.local/node/{request.node_id}"
                        )
                    ]
                ),
                critical=False,
            )
        )
        try:
            skid = self._intermediate.extensions.get_extension_for_class(
                x509.SubjectKeyIdentifier
            ).value.digest
        except x509.ExtensionNotFound:
            pass
        else:
            builder = builder.add_extension(
                x509.AuthorityKeyIdentifier(skid, None, None), critical=False
            )
        return builder.sign(self._key, algorithm=None)

    def _issued(
        self,
        request: CertificateIssuanceBinding,
        csr: x509.CertificateSigningRequest,
        pem: str,
    ) -> IssuedCertificate:
        leaf = _one_certificate(pem.encode("ascii"), "leaf", provider_error=True)
        self._validate_leaf(request.node_id, csr, leaf)
        if (
            str(leaf.serial_number) != request.serial
            or leaf.not_valid_before_utc != datetime.fromisoformat(request.not_before)
            or leaf.not_valid_after_utc != datetime.fromisoformat(request.not_after)
        ):
            raise StepCAError(
                "local CA certificate differs from the accepted exact effect"
            )
        return IssuedCertificate(
            request.node_id,
            leaf.public_bytes(serialization.Encoding.PEM),
            self._intermediate.public_bytes(serialization.Encoding.PEM),
            request.serial,
            leaf.fingerprint(hashes.SHA256()).hex(),
            leaf.not_valid_before_utc,
            leaf.not_valid_after_utc,
            request.generation,
        )

    def revoke_node(self, serial: str, now: datetime) -> None:
        if (
            not serial.isdecimal()
            or not 0 < int(serial) < 2**159
            or str(int(serial)) != serial
        ):
            raise ValueError("certificate serial must be a positive decimal integer")
        with self._session(write=True) as session:
            self._locks(session, "serial:" + serial)
            session.execute(
                insert(LocalCertificateRevocation)
                .values(serial=serial, revoked_at=_utc_timestamp(now))
                .on_conflict_do_nothing()
            )

    def revocation_bundle(self, now: datetime) -> bytes | UnknownError:
        try:
            return self._revocation_bundle_once(now)
        except StepCAUnavailable:
            return UnknownError(
                category=ErrorCategory.UNKNOWN,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )

    def _revocation_bundle_once(self, now: datetime) -> bytes:
        timestamp = _utc_timestamp(now)
        with self._session() as session:
            entries = list(
                session.scalars(
                    select(LocalCertificateRevocation).order_by(
                        LocalCertificateRevocation.serial
                    )
                )
            )
        builder = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(self._intermediate.subject)
            .last_update(timestamp)
            .next_update(timestamp + _MAX_CRL_WINDOW)
        )
        for entry in entries:
            builder = builder.add_revoked_certificate(
                x509.RevokedCertificateBuilder()
                .serial_number(int(entry.serial))
                .revocation_date(entry.revoked_at)
                .add_extension(
                    x509.CRLReason(x509.ReasonFlags.superseded), critical=False
                )
                .build()
            )
        return builder.sign(self._key, algorithm=None).public_bytes(
            serialization.Encoding.PEM
        )
