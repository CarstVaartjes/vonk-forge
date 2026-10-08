"""Strict Smallstep step-ca v0.30.2 provider for GPU node agent certificates."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID
from pydantic import BaseModel, ConfigDict, ValidationError
from vonk_agent_protocol import CertificateCode, InvalidRequestError, canonical_message
from vonk_agent_protocol.enrollment import (
    MAX_ENROLLMENT_RESPONSE_BYTES,
    IssuedCertificateResponse,
)

from .ca_issuance_contract import (
    CertificateAbsentReply,
    CertificateIssuanceBinding,
    CertificateIssuedReply,
    CertificatePendingReply,
    CertificateRefusalReason,
    CertificateRefusalReply,
    CertificateSignRequest,
)
from .pki import (
    CertificateAuthority,
    IssuedCertificate,
    _load_node_csr,
    _read_regular_secret_file,
    _utc_timestamp,
)

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_SERIAL = re.compile(r"[1-9][0-9]{0,127}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")
_KID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DEFAULT_CERTIFICATE_LIFETIME_SECONDS = 30 * 24 * 60 * 60
_MAX_CRL_WINDOW = timedelta(hours=1)


class _StepWire(BaseModel):
    """A document the Controller sends to step-ca, or step-ca's strict answer."""

    model_config = ConfigDict(extra="forbid", strict=True)


class _TokenClaims(_StepWire):
    """The claims of the one-time token that authorizes one step-ca request."""

    iss: str
    sub: str
    aud: str
    iat: int
    nbf: int
    exp: int
    jti: str
    sans: list[str] | None = None
    vonk: CertificateIssuanceBinding | None = None
    cnf: dict[str, str] | None = None


class _RevokeRequest(_StepWire):
    serial: str
    ott: str
    reasonCode: int = 4
    reason: str = "superseded by Vonk Forge"
    passive: bool = True


class _StatusReply(_StepWire):
    status: str


class _PublicJwk(BaseModel):
    """The public EC key fields an RFC 7638 thumbprint is built from."""

    model_config = ConfigDict(extra="ignore")

    crv: str
    kty: str
    x: str
    y: str


class StepCAError(RuntimeError):
    """A bounded provider operation failed without exposing authorization."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: CertificateRefusalReason = "certificate.issuance_unavailable",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class StepCAResponseCapacityRefused(InvalidRequestError, StepCAError):
    """An owned representability policy refuses before issuance transport."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason_code = CertificateCode.RESPONSE_UNREPRESENTABLE


class StepCAIssuancePending(StepCAError):
    """The exact CA journal request is still owned by an issuer epoch."""


class StepCertificateAuthority(CertificateAuthority):
    """Issue through one fixed, privately reachable step-ca JWK provisioner."""

    def __init__(
        self,
        *,
        ca_url: str,
        root_certificate_path: Path | str,
        intermediate_certificate_path: Path | str,
        provisioner_name: str,
        provisioner_kid: str,
        credential_path: Path | str,
        provisioner_public_jwk_path: Path | str,
        timeout_seconds: float = 3.0,
        max_response_bytes: int = 64 * 1024,
        certificate_lifetime_seconds: int = _DEFAULT_CERTIFICATE_LIFETIME_SECONDS,
        clock_skew_seconds: int = 30,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(ca_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "CA URL must be a fixed HTTPS origin without credentials, path, query, or fragment"
            )
        if _NAME.fullmatch(provisioner_name) is None:
            raise ValueError("provisioner name is invalid")
        if _KID.fullmatch(provisioner_kid) is None:
            raise ValueError("provisioner key ID is invalid")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("CA timeout must be between zero and 30 seconds")
        if not 1024 <= max_response_bytes <= 1024 * 1024:
            raise ValueError("CA response limit must be between 1024 bytes and one MiB")
        if (
            isinstance(certificate_lifetime_seconds, bool)
            or not isinstance(certificate_lifetime_seconds, int)
            or not 90
            <= certificate_lifetime_seconds
            <= _DEFAULT_CERTIFICATE_LIFETIME_SECONDS
        ):
            # The NAS step-ca configuration owns the lifetime (trust the kit);
            # only an unrepresentable value is refused.
            raise ValueError(
                "certificate lifetime must be an integer between 90 and 2592000 seconds"
            )
        if not 0 <= clock_skew_seconds <= 60:
            raise ValueError("CA clock skew must be between zero and 60 seconds")

        root_pem = _read_regular_secret_file(root_certificate_path)
        intermediate_pem = _read_regular_secret_file(intermediate_certificate_path)
        credential_pem = _read_regular_secret_file(credential_path)
        public_jwk_bytes = _read_regular_secret_file(provisioner_public_jwk_path)
        self._root = _one_certificate(root_pem, "root")
        self._intermediate = _one_certificate(intermediate_pem, "intermediate")
        self._certificate_lifetime_seconds = certificate_lifetime_seconds
        self._certificate_lifetime = timedelta(seconds=certificate_lifetime_seconds)
        _verify_ca_chain(self._root, self._intermediate, self._certificate_lifetime)
        try:
            credential_jwk = jwt.PyJWK.from_json(credential_pem.decode("ascii"))
            credential = credential_jwk.key
        except (UnicodeDecodeError, ValueError, jwt.PyJWTError) as error:
            raise ValueError(
                "provisioner credential must be a private EC P-256 JWK"
            ) from error
        if not isinstance(credential, ec.EllipticCurvePrivateKey) or not isinstance(
            credential.curve, ec.SECP256R1
        ):
            raise ValueError(  # noqa: TRY004 - all invalid provider configuration is ValueError
                "provisioner credential must be a private EC P-256 JWK"
            )
        if (
            credential_jwk.algorithm_name != "ES256"
            or credential_jwk.key_id != provisioner_kid
        ):
            raise ValueError(
                "provisioner credential metadata does not match configured key ID"
            )
        try:
            public_mapping = json.loads(public_jwk_bytes)
            public_jwk = jwt.PyJWK.from_dict(public_mapping)
        except (ValueError, TypeError, jwt.PyJWTError) as error:
            raise ValueError(
                "provisioner public metadata must be an EC P-256 JWK"
            ) from error
        if not isinstance(public_mapping, dict) or "d" in public_mapping:
            raise ValueError(
                "provisioner public metadata must not contain private key material"
            )
        if (
            not isinstance(public_jwk.key, ec.EllipticCurvePublicKey)
            or not isinstance(public_jwk.key.curve, ec.SECP256R1)
            or public_jwk.algorithm_name != "ES256"
            or public_jwk.key_id != provisioner_kid
            or public_jwk.key.public_numbers()
            != credential.public_key().public_numbers()
        ):
            raise ValueError(
                "provisioner public metadata does not match private credential"
            )
        if _jwk_thumbprint(public_mapping) != provisioner_kid:
            raise ValueError(
                "provisioner key ID must equal the RFC 7638 public JWK thumbprint"
            )

        context = ssl.create_default_context(cadata=root_pem.decode("ascii"))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._ca_url = ca_url.rstrip("/")
        self._provisioner_name = provisioner_name
        self._provisioner_kid = provisioner_kid
        self._credential = credential
        self._max_response_bytes = max_response_bytes
        self._clock_skew = timedelta(seconds=clock_skew_seconds)
        timeout = httpx2.Timeout(timeout_seconds, connect=timeout_seconds)
        self._client = httpx2.Client(
            verify=context,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "accept": "application/json",
                "user-agent": "vonk-forge-control/1",
            },
        )

    def prepare_request(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        purpose: str,
        source_serial: str | None,
        generation: int,
    ) -> CertificateIssuanceBinding:
        timestamp = _utc_timestamp(now)
        csr = _load_node_csr(node_id, csr_pem)
        issuer = self._intermediate.fingerprint(hashes.SHA256()).hex()
        policy = json.dumps(
            {
                "certificate_lifetime_seconds": self._certificate_lifetime_seconds,
                "issuer_fingerprint": issuer,
                "profile": "vonk-node-client-v1",
                "provisioner_kid": self._provisioner_kid,
                "provisioner_name": self._provisioner_name,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return CertificateIssuanceBinding.model_validate(
            {
                "request_id": secrets.token_urlsafe(32),
                "node_id": node_id,
                "csr_sha256": hashlib.sha256(
                    csr.public_bytes(serialization.Encoding.DER)
                ).hexdigest(),
                "serial": str(x509.random_serial_number()),
                "not_before": _rfc3339(timestamp),
                "not_after": _rfc3339(timestamp + self._certificate_lifetime),
                "issuer_fingerprint": issuer,
                "provisioner_name": self._provisioner_name,
                "provisioner_kid": self._provisioner_kid,
                "policy_sha256": hashlib.sha256(policy).hexdigest(),
                "purpose": purpose,
                "source_serial": source_serial,
                "generation": generation,
            }
        )

    def issue_node(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request: CertificateIssuanceBinding,
    ) -> IssuedCertificate:
        if request.node_id != node_id:
            raise ValueError("CA request node identity does not match")
        issued = self._sign(csr_pem, now, request=request, mode="issue")
        if issued is None:
            raise StepCAError("CA issue returned absence")
        return issued

    def observe_node(
        self,
        csr_pem: bytes,
        now: datetime,
        *,
        request: CertificateIssuanceBinding,
    ) -> IssuedCertificate | None:
        return self._sign(csr_pem, now, request=request, mode="observe")

    def check_health(self) -> None:
        if not _is_ok(self._json_request("GET", "/health", None)):
            raise StepCAError("step-ca health response is invalid")

    def renew_node(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request: CertificateIssuanceBinding,
    ) -> IssuedCertificate:
        if request.purpose != "rotation":
            raise ValueError("renewal requires a rotation binding")
        return self.issue_node(node_id, csr_pem, now, request=request)

    def revoke_node(self, serial: str, now: datetime) -> None:
        timestamp = _utc_timestamp(now)
        if _SERIAL.fullmatch(serial) is None:
            raise ValueError("certificate serial must be a positive decimal integer")
        body = _RevokeRequest(
            serial=serial,
            ott=self._token(serial, f"{self._ca_url}/1.0/revoke", timestamp, sans=None),
        )
        response = self._json_request("POST", "/1.0/revoke", body)
        if not _is_ok(response):
            raise StepCAError("step-ca returned an invalid revocation response")

    def revocation_bundle(self, now: datetime) -> bytes:
        timestamp = _utc_timestamp(now)
        raw = self._request(
            "GET", "/1.0/crl?pem=true", None, accept="application/x-pem-file"
        )
        try:
            crl = x509.load_pem_x509_crl(raw)
        except ValueError as error:
            raise StepCAError(
                "step-ca returned an invalid revocation bundle"
            ) from error
        if crl.issuer != self._intermediate.subject:
            raise StepCAError("step-ca revocation bundle issuer is invalid")
        try:
            _signature_verifying_key(self._intermediate).verify(
                crl.signature, crl.tbs_certlist_bytes
            )
        except Exception as error:
            raise StepCAError(
                "step-ca revocation bundle signature is invalid"
            ) from error
        _validate_crl_freshness(crl, timestamp, self._clock_skew)
        return crl.public_bytes(serialization.Encoding.PEM)

    def _sign(
        self,
        csr_pem: bytes,
        now: datetime,
        *,
        request: CertificateIssuanceBinding,
        mode: str,
    ) -> IssuedCertificate | None:
        timestamp = _utc_timestamp(now)
        node_id = request.node_id
        csr = _load_node_csr(node_id, csr_pem)
        csr_digest = hashlib.sha256(
            csr.public_bytes(serialization.Encoding.DER)
        ).hexdigest()
        # Configuration is still authoritative on every replay; a durable request
        # cannot authorize a different issuer, provisioner, policy or CSR.
        expected = self.prepare_request(
            node_id,
            csr_pem,
            datetime.fromisoformat(request.not_before),
            purpose=request.purpose,
            source_serial=request.source_serial,
            generation=request.generation,
        )
        if (
            request.csr_sha256 != csr_digest
            or request.issuer_fingerprint != expected.issuer_fingerprint
            or request.provisioner_name != expected.provisioner_name
            or request.provisioner_kid != expected.provisioner_kid
            or request.policy_sha256 != expected.policy_sha256
        ):
            raise ValueError("CA request no longer matches exact issuer policy or CSR")
        self._validate_sign_response_capacity(request)
        raw_response = self._json_request(
            "POST",
            "/1.0/vonk/sign",
            CertificateSignRequest.model_validate(
                {
                    "csr": csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
                    "ott": self._token(
                        node_id,
                        f"{self._ca_url}/1.0/sign",
                        timestamp,
                        sans=[f"spiffe://vonk-forge.local/node/{node_id}"],
                        request=request,
                    ),
                    "request": request,
                    "mode": mode,
                }
            ),
        )
        try:
            if (
                isinstance(raw_response, dict)
                and raw_response.get("state") == "pending"
            ):
                pending = CertificatePendingReply.model_validate(raw_response)
                if pending.request != request:
                    raise ValueError("CA returned another journal binding")
                raise StepCAIssuancePending("certificate.issuance_in_progress")
            if isinstance(raw_response, dict) and raw_response.get("state") == "absent":
                absent = CertificateAbsentReply.model_validate(raw_response)
                if mode != "observe" or absent.request != request:
                    raise ValueError("CA returned invalid absence evidence")
                return None
            response = CertificateIssuedReply.model_validate(raw_response)
            if response.request != request:
                raise ValueError("CA returned another journal binding")
        except (ValidationError, ValueError) as error:
            raise StepCAError("step-ca returned an invalid sign response") from error
        if len(response.certChain) != 2:
            raise StepCAError("step-ca returned an invalid certificate chain")
        if response.certChain != [response.crt, response.ca]:
            raise StepCAError("step-ca returned inconsistent certificate chain fields")
        leaf = _one_certificate(
            response.crt.encode("ascii"), "leaf", provider_error=True
        )
        intermediate = _one_certificate(
            response.ca.encode("ascii"), "intermediate", provider_error=True
        )
        if intermediate.fingerprint(hashes.SHA256()) != self._intermediate.fingerprint(
            hashes.SHA256()
        ):
            raise StepCAError("step-ca returned an unexpected intermediate")
        self._validate_leaf(
            node_id, csr, leaf, datetime.fromisoformat(request.not_before)
        )
        if (
            str(leaf.serial_number) != request.serial
            or _rfc3339(leaf.not_valid_before_utc) != request.not_before
            or _rfc3339(leaf.not_valid_after_utc) != request.not_after
        ):
            raise StepCAError("CA certificate differs from the accepted exact effect")
        return IssuedCertificate(
            node_id=node_id,
            certificate_pem=leaf.public_bytes(serialization.Encoding.PEM),
            chain_pem=intermediate.public_bytes(serialization.Encoding.PEM),
            serial=str(leaf.serial_number),
            fingerprint=leaf.fingerprint(hashes.SHA256()).hex(),
            not_before=leaf.not_valid_before_utc,
            not_after=leaf.not_valid_after_utc,
            generation=request.generation,
        )

    def _validate_leaf(
        self,
        node_id: str,
        request: x509.CertificateSigningRequest,
        leaf: x509.Certificate,
        requested_at: datetime,
    ) -> None:
        if leaf.subject != x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, node_id)]
        ):
            raise StepCAError("step-ca returned a mismatched certificate subject")
        if leaf.issuer != self._intermediate.subject:
            raise StepCAError("step-ca returned a mismatched certificate issuer")
        try:
            _signature_verifying_key(self._intermediate).verify(
                leaf.signature, leaf.tbs_certificate_bytes
            )
        except Exception as error:
            raise StepCAError(
                "step-ca returned a certificate with an invalid signature"
            ) from error
        request_key = request.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        if not isinstance(leaf.public_key(), ed25519.Ed25519PublicKey):
            raise StepCAError("step-ca returned a certificate with the wrong key type")
        leaf_key = leaf.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        if leaf_key != request_key:
            raise StepCAError("step-ca returned a certificate for another public key")
        required_extensions = {
            ExtensionOID.KEY_USAGE,
            ExtensionOID.EXTENDED_KEY_USAGE,
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME,
        }
        allowed_extensions = required_extensions | {
            ExtensionOID.BASIC_CONSTRAINTS,
            ExtensionOID.SUBJECT_KEY_IDENTIFIER,
            ExtensionOID.AUTHORITY_KEY_IDENTIFIER,
        }
        extension_oids = {value.oid for value in leaf.extensions}
        if (
            not required_extensions <= extension_oids
            or not extension_oids <= allowed_extensions
        ):
            raise StepCAError(
                "step-ca returned an unexpected certificate extension profile"
            )
        criticality = {value.oid: value.critical for value in leaf.extensions}
        if (
            criticality[ExtensionOID.KEY_USAGE] is not True
            or criticality[ExtensionOID.EXTENDED_KEY_USAGE] is not False
            or criticality[ExtensionOID.SUBJECT_ALTERNATIVE_NAME] is not False
        ):
            raise StepCAError(
                "step-ca returned invalid certificate extension criticality"
            )
        if ExtensionOID.BASIC_CONSTRAINTS in extension_oids:
            basic_constraints = leaf.extensions.get_extension_for_class(
                x509.BasicConstraints
            )
            if (
                basic_constraints.critical is not True
                or basic_constraints.value != x509.BasicConstraints(False, None)
            ):
                raise StepCAError("step-ca returned a CA certificate")
        expected_usage = x509.KeyUsage(
            True, False, False, False, False, False, False, False, False
        )
        if (
            leaf.extensions.get_extension_for_class(x509.KeyUsage).value
            != expected_usage
        ):
            raise StepCAError("step-ca returned invalid key usage")
        expected_eku = x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH])
        if (
            leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            != expected_eku
        ):
            raise StepCAError("step-ca returned invalid extended key usage")
        expected_san = x509.SubjectAlternativeName(
            [
                x509.UniformResourceIdentifier(
                    f"spiffe://vonk-forge.local/node/{node_id}"
                )
            ]
        )
        if (
            leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            != expected_san
        ):
            raise StepCAError("step-ca returned a mismatched node URI SAN")
        if ExtensionOID.AUTHORITY_KEY_IDENTIFIER in extension_oids:
            try:
                intermediate_skid = (
                    self._intermediate.extensions.get_extension_for_class(
                        x509.SubjectKeyIdentifier
                    ).value.digest
                )
            except x509.ExtensionNotFound as error:
                raise StepCAError(
                    "step-ca returned an unverifiable authority key identifier"
                ) from error
            authority_id = leaf.extensions.get_extension_for_class(
                x509.AuthorityKeyIdentifier
            ).value
            if authority_id.key_identifier != intermediate_skid:
                raise StepCAError(
                    "step-ca returned a mismatched authority key identifier"
                )
        if ExtensionOID.SUBJECT_KEY_IDENTIFIER in extension_oids:
            expected_skid = x509.SubjectKeyIdentifier.from_public_key(leaf.public_key())
            if (
                leaf.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
                != expected_skid
            ):
                raise StepCAError(
                    "step-ca returned a mismatched subject key identifier"
                )
        if (
            leaf.not_valid_after_utc - leaf.not_valid_before_utc
            != self._certificate_lifetime
        ):
            raise StepCAError("step-ca returned an invalid certificate lifetime")
        if abs(leaf.not_valid_before_utc - requested_at) > self._clock_skew:
            raise StepCAError(
                "step-ca returned a certificate outside the allowed clock skew"
            )

    def _token(
        self,
        subject: str,
        audience: str,
        now: datetime,
        *,
        sans: list[str] | None,
        request: CertificateIssuanceBinding | None = None,
    ) -> str:
        timestamp = int(now.timestamp())
        claims = _TokenClaims(
            iss=self._provisioner_name,
            sub=subject,
            aud=audience,
            iat=timestamp,
            nbf=timestamp - int(self._clock_skew.total_seconds()),
            exp=timestamp + 60,
            jti=secrets.token_urlsafe(32),
            vonk=request,
            cnf=None
            if request is None
            else {
                "x5rt#S256": base64.urlsafe_b64encode(bytes.fromhex(request.csr_sha256))
                .rstrip(b"=")
                .decode("ascii")
            },
            sans=sans,
        )
        return jwt.encode(
            {
                key: value
                for key, value in claims.model_dump(mode="json").items()
                if value is not None
            },
            self._credential,
            algorithm="ES256",
            headers={"kid": self._provisioner_kid, "typ": "JWT"},
        )

    def _json_request(
        self, method: str, path: str, body: _StepWire | CertificateSignRequest | None
    ) -> object:
        raw = self._request(method, path, body, accept="application/json")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StepCAError("step-ca returned malformed JSON") from error

    def _validate_sign_response_capacity(
        self, request: CertificateIssuanceBinding
    ) -> None:
        # The private issuer owns a fixed 64 KiB complete-reply contract.
        # A smaller configured reader cannot accept every valid issued effect;
        # refuse before observe/issue HTTP rather than lose its committed reply.
        # Other endpoints (including CRL) retain their independent reader limit.
        if self._max_response_bytes < MAX_ENROLLMENT_RESPONSE_BYTES:
            raise StepCAResponseCapacityRefused(
                "configured CA sign response reader cannot accept the "
                f"{MAX_ENROLLMENT_RESPONSE_BYTES}-byte issuance contract "
                f"(configured {self._max_response_bytes} bytes)",
            )
        # The CA's committed reply includes each PEM twice (crt/ca and
        # certChain). A complete sign reply bounded to 64 KiB therefore spends
        # at most half that budget on the two PEMs in the agent response.
        # Charge the actual outgoing metadata independently BEFORE CA effects;
        # do not assume the CA transport bound covers the agent envelope.
        try:
            metadata = IssuedCertificateResponse(
                node_id=request.node_id,
                certificate_pem="x",
                chain_pem="x",
                serial=request.serial,
                fingerprint="0" * 64,
                not_before=datetime.fromisoformat(request.not_before).isoformat(),
                not_after=datetime.fromisoformat(request.not_after).isoformat(),
                generation=request.generation,
            )
        except ValidationError as error:
            raise StepCAResponseCapacityRefused(
                "issued response metadata cannot fit the enrollment contract",
            ) from error
        maximum_response = (
            len(canonical_message(metadata)) - 2 + MAX_ENROLLMENT_RESPONSE_BYTES // 2
        )
        if maximum_response > MAX_ENROLLMENT_RESPONSE_BYTES:
            raise StepCAResponseCapacityRefused(
                f"issued response cannot fit {MAX_ENROLLMENT_RESPONSE_BYTES} bytes "
                f"(upper bound {maximum_response})",
            )

    def _request(
        self,
        method: str,
        path: str,
        body: _StepWire | CertificateSignRequest | None,
        *,
        accept: str,
    ) -> bytes:
        try:
            with self._client.stream(
                method,
                f"{self._ca_url}{path}",
                json=None if body is None else body.model_dump(mode="json"),
                headers={"accept": accept},
            ) as response:
                if response.is_redirect:
                    raise StepCAError("step-ca redirects are forbidden")
                output = bytearray()
                for chunk in response.iter_bytes():
                    observed = len(output) + len(chunk)
                    if observed > self._max_response_bytes or (
                        path == "/1.0/vonk/sign"
                        and observed > MAX_ENROLLMENT_RESPONSE_BYTES
                    ):
                        raise StepCAError("step-ca response is too large")
                    output.extend(chunk)
                if not response.is_success:
                    if path == "/1.0/vonk/sign" and response.status_code != 404:
                        try:
                            refusal = CertificateRefusalReply.model_validate_json(
                                bytes(output)
                            )
                        except ValidationError as error:
                            raise StepCAError(
                                "CA returned an invalid refusal response"
                            ) from error
                        raise StepCAError(
                            f"CA refused exact certificate request: {refusal.detail}",
                            reason_code=refusal.reason_code,
                        )
                    raise StepCAError(
                        f"step-ca request failed with status {response.status_code}"
                    )
                return bytes(output)
        except StepCAError:
            raise
        except (httpx2.HTTPError, OSError) as error:
            raise StepCAError("step-ca request failed") from error


def _signature_verifying_key(
    certificate: x509.Certificate,
) -> ed25519.Ed25519PublicKey | ed448.Ed448PublicKey:
    """Return the certificate key that verifies with only signature and data.

    step-ca signs with Ed25519 keys, whose ``verify`` takes exactly the
    signature and the signed bytes. A key that cannot verify that way is
    rejected here instead of reaching an unsupported call.
    """

    key = certificate.public_key()
    if isinstance(key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
        return key
    raise ValueError("certificate public key cannot verify a signature")


def _one_certificate(
    pem: bytes, label: str, *, provider_error: bool = False
) -> x509.Certificate:
    try:
        certificate = x509.load_pem_x509_certificate(pem)
    except ValueError as error:
        exception = StepCAError if provider_error else ValueError
        raise exception(
            f"{label} certificate must be exactly one valid PEM certificate"
        ) from error
    normalized = certificate.public_bytes(serialization.Encoding.PEM)
    if pem.strip() != normalized.strip():
        exception = StepCAError if provider_error else ValueError
        raise exception(
            f"{label} certificate must be exactly one valid PEM certificate"
        )
    return certificate


def _verify_ca_chain(
    root: x509.Certificate,
    intermediate: x509.Certificate,
    certificate_lifetime: timedelta,
) -> None:
    if root.subject != root.issuer:
        raise ValueError("root certificate must be self-issued")
    try:
        _signature_verifying_key(root).verify(
            root.signature, root.tbs_certificate_bytes
        )
    except Exception as error:
        raise ValueError("root certificate self-signature is invalid") from error
    try:
        root_constraints = root.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        root_usage = root.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound as error:
        raise ValueError(
            "root certificate must contain CA constraints and key usage"
        ) from error
    if (
        not root_constraints.ca
        or root_constraints.path_length is not None
        and root_constraints.path_length < 1
    ):
        raise ValueError("root certificate cannot issue the configured intermediate")
    if not root_usage.key_cert_sign or not root_usage.crl_sign:
        raise ValueError("root certificate must permit certificate and CRL signing")
    if intermediate.issuer != root.subject:
        raise ValueError("intermediate certificate is not issued by configured root")
    try:
        _signature_verifying_key(root).verify(
            intermediate.signature, intermediate.tbs_certificate_bytes
        )
    except Exception as error:
        raise ValueError("intermediate certificate signature is invalid") from error
    try:
        constraints = intermediate.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        usage = intermediate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound as error:
        raise ValueError(
            "intermediate certificate must contain CA constraints and key usage"
        ) from error
    if (
        constraints != x509.BasicConstraints(True, 0)
        or not usage.key_cert_sign
        or not usage.crl_sign
    ):
        raise ValueError(
            "intermediate certificate is not a path-length-zero signing CA"
        )
    now = datetime.now(UTC)
    if root.not_valid_before_utc > now or root.not_valid_after_utc <= now:
        raise ValueError("root certificate is not currently valid")
    if (
        intermediate.not_valid_before_utc > now
        or intermediate.not_valid_after_utc <= now
    ):
        raise ValueError("intermediate certificate is not currently valid")
    if intermediate.not_valid_after_utc <= now + certificate_lifetime:
        raise ValueError(
            "intermediate certificate cannot cover configured leaf lifetime"
        )
    if intermediate.not_valid_after_utc > root.not_valid_after_utc:
        raise ValueError("intermediate certificate outlives configured root")


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_ok(value: object) -> bool:
    try:
        return _StatusReply.model_validate(value).status == "ok"
    except ValidationError:
        return False


def _jwk_thumbprint(value: object) -> str:
    try:
        jwk = _PublicJwk.model_validate(value)
        canonical = json.dumps(
            {"crv": jwk.crv, "kty": jwk.kty, "x": jwk.x, "y": jwk.y},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    except (ValidationError, UnicodeEncodeError) as error:
        raise ValueError(
            "provisioner public metadata is missing thumbprint fields"
        ) from error
    return (
        base64.urlsafe_b64encode(hashlib.sha256(canonical).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def _validate_crl_freshness(
    crl: x509.CertificateRevocationList,
    timestamp: datetime,
    clock_skew: timedelta,
) -> None:
    last_update = crl.last_update_utc
    next_update = crl.next_update_utc
    if (
        next_update is None
        or last_update > timestamp + clock_skew
        or last_update < timestamp - _MAX_CRL_WINDOW - clock_skew
        or next_update <= timestamp - clock_skew
        or next_update - last_update > _MAX_CRL_WINDOW + clock_skew
    ):
        raise StepCAError("step-ca revocation bundle freshness window is invalid")
