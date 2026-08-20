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

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID

from .pki import (
    CertificateAuthority,
    IssuedCertificate,
    _load_node_csr,
    _read_regular_secret_file,
    _utc_timestamp,
    _validate_provider_request_id,
)

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}\Z")
_SERIAL = re.compile(r"[1-9][0-9]{0,127}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")
_KID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DEFAULT_CERTIFICATE_LIFETIME_SECONDS = 86400
_MAX_CRL_WINDOW = timedelta(hours=1)


class StepCAError(RuntimeError):
    """A bounded provider operation failed without exposing authorization."""


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
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(ca_url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
        ):
            raise ValueError("CA URL must be a fixed HTTPS origin without credentials, path, query, or fragment")
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
            or not 90 <= certificate_lifetime_seconds <= _DEFAULT_CERTIFICATE_LIFETIME_SECONDS
        ):
            raise ValueError("certificate lifetime must be an integer between 90 and 86400 seconds")
        if not 0 <= clock_skew_seconds <= 60:
            raise ValueError("CA clock skew must be between zero and 60 seconds")

        root_pem = _read_regular_secret_file(root_certificate_path)
        intermediate_pem = _read_regular_secret_file(intermediate_certificate_path)
        credential_pem = _read_regular_secret_file(credential_path)
        public_jwk_bytes = _read_regular_secret_file(provisioner_public_jwk_path)
        self._root = _one_certificate(root_pem, "root")
        self._intermediate = _one_certificate(intermediate_pem, "intermediate")
        self._certificate_lifetime = timedelta(seconds=certificate_lifetime_seconds)
        _verify_ca_chain(self._root, self._intermediate, self._certificate_lifetime)
        try:
            credential_jwk = jwt.PyJWK.from_json(credential_pem.decode("ascii"))
            credential = credential_jwk.key
        except (UnicodeDecodeError, ValueError, jwt.PyJWTError) as error:
            raise ValueError("provisioner credential must be a private EC P-256 JWK") from error
        if not isinstance(credential, ec.EllipticCurvePrivateKey) or not isinstance(credential.curve, ec.SECP256R1):
            raise ValueError(  # noqa: TRY004 - all invalid provider configuration is ValueError
                "provisioner credential must be a private EC P-256 JWK"
            )
        if credential_jwk.algorithm_name != "ES256" or credential_jwk.key_id != provisioner_kid:
            raise ValueError("provisioner credential metadata does not match configured key ID")
        try:
            public_mapping = json.loads(public_jwk_bytes)
            public_jwk = jwt.PyJWK.from_dict(public_mapping)
        except (ValueError, TypeError, jwt.PyJWTError) as error:
            raise ValueError("provisioner public metadata must be an EC P-256 JWK") from error
        if not isinstance(public_mapping, dict) or "d" in public_mapping:
            raise ValueError("provisioner public metadata must not contain private key material")
        if (
            not isinstance(public_jwk.key, ec.EllipticCurvePublicKey)
            or not isinstance(public_jwk.key.curve, ec.SECP256R1)
            or public_jwk.algorithm_name != "ES256"
            or public_jwk.key_id != provisioner_kid
            or public_jwk.key.public_numbers() != credential.public_key().public_numbers()
        ):
            raise ValueError("provisioner public metadata does not match private credential")
        if _jwk_thumbprint(public_mapping) != provisioner_kid:
            raise ValueError("provisioner key ID must equal the RFC 7638 public JWK thumbprint")

        context = ssl.create_default_context(cadata=root_pem.decode("ascii"))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._ca_url = ca_url.rstrip("/")
        self._provisioner_name = provisioner_name
        self._provisioner_kid = provisioner_kid
        self._credential = credential
        self._max_response_bytes = max_response_bytes
        self._clock_skew = timedelta(seconds=clock_skew_seconds)
        timeout = httpx.Timeout(timeout_seconds, connect=timeout_seconds)
        self._client = httpx.Client(
            verify=context,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"accept": "application/json", "user-agent": "vonk-forge-control/1"},
        )

    def issue_node(self, node_id: str, csr_pem: bytes, now: datetime) -> IssuedCertificate:
        return self._sign(node_id, csr_pem, now)

    def check_health(self) -> None:
        if self._json_request("GET", "/health", None) != {"status": "ok"}:
            raise StepCAError("step-ca health response is invalid")

    def renew_node(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request_id: str,
    ) -> IssuedCertificate:
        # Vonk Forge authorizes renewal with the currently active mTLS identity;
        # step-ca receives a fresh, fixed-policy sign authorization and CSR.
        _validate_provider_request_id(request_id)
        return self._sign(node_id, csr_pem, now, request_id=request_id)

    def revoke_node(self, serial: str, now: datetime) -> None:
        timestamp = _utc_timestamp(now)
        if _SERIAL.fullmatch(serial) is None:
            raise ValueError("certificate serial must be a positive decimal integer")
        body = {
            "serial": serial,
            "ott": self._token(serial, f"{self._ca_url}/1.0/revoke", timestamp, sans=None),
            "reasonCode": 4,
            "reason": "superseded by Vonk Forge",
            "passive": True,
        }
        response = self._json_request("POST", "/1.0/revoke", body)
        if response != {"status": "ok"}:
            raise StepCAError("step-ca returned an invalid revocation response")

    def revocation_bundle(self, now: datetime) -> bytes:
        timestamp = _utc_timestamp(now)
        raw = self._request("GET", "/1.0/crl?pem=true", None, accept="application/x-pem-file")
        try:
            crl = x509.load_pem_x509_crl(raw)
        except ValueError as error:
            raise StepCAError("step-ca returned an invalid revocation bundle") from error
        if crl.issuer != self._intermediate.subject:
            raise StepCAError("step-ca revocation bundle issuer is invalid")
        try:
            self._intermediate.public_key().verify(crl.signature, crl.tbs_certlist_bytes)
        except Exception as error:
            raise StepCAError("step-ca revocation bundle signature is invalid") from error
        _validate_crl_freshness(crl, timestamp, self._clock_skew)
        return crl.public_bytes(serialization.Encoding.PEM)

    def _sign(
        self,
        node_id: str,
        csr_pem: bytes,
        now: datetime,
        *,
        request_id: str | None = None,
    ) -> IssuedCertificate:
        timestamp = _utc_timestamp(now)
        if _NODE_ID.fullmatch(node_id) is None:
            raise ValueError("node ID must be canonical")
        request = _load_node_csr(node_id, csr_pem)
        normalized_csr = request.public_bytes(serialization.Encoding.PEM)
        endpoint = f"{self._ca_url}/1.0/sign"
        response = self._json_request("POST", "/1.0/sign", {
            "csr": normalized_csr.decode("ascii"),
            "ott": self._token(
                node_id, endpoint, timestamp,
                sans=[f"spiffe://vonk-forge.local/node/{node_id}"],
                request_id=request_id,
            ),
            "notBefore": _rfc3339(timestamp),
            "notAfter": _rfc3339(timestamp + self._certificate_lifetime),
        })
        if not isinstance(response, dict) or not {"crt", "ca", "certChain"} <= set(response):
            raise StepCAError("step-ca returned an invalid sign response")
        if set(response) - {"crt", "ca", "certChain", "tlsOptions"}:
            raise StepCAError("step-ca returned unexpected sign response fields")
        if not isinstance(response["crt"], str) or not isinstance(response["ca"], str):
            raise StepCAError("step-ca returned invalid certificate PEM")
        chain = response["certChain"]
        if not isinstance(chain, list) or len(chain) != 2 or not all(isinstance(value, str) for value in chain):
            raise StepCAError("step-ca returned an invalid certificate chain")
        if chain != [response["crt"], response["ca"]]:
            raise StepCAError("step-ca returned inconsistent certificate chain fields")
        leaf = _one_certificate(response["crt"].encode("ascii"), "leaf", provider_error=True)
        intermediate = _one_certificate(response["ca"].encode("ascii"), "intermediate", provider_error=True)
        if intermediate.fingerprint(hashes.SHA256()) != self._intermediate.fingerprint(hashes.SHA256()):
            raise StepCAError("step-ca returned an unexpected intermediate")
        self._validate_leaf(node_id, request, leaf, timestamp)
        return IssuedCertificate(
            node_id=node_id,
            certificate_pem=leaf.public_bytes(serialization.Encoding.PEM),
            chain_pem=intermediate.public_bytes(serialization.Encoding.PEM),
            serial=str(leaf.serial_number),
            fingerprint=leaf.fingerprint(hashes.SHA256()).hex(),
            not_before=leaf.not_valid_before_utc,
            not_after=leaf.not_valid_after_utc,
        )

    def _validate_leaf(
        self,
        node_id: str,
        request: x509.CertificateSigningRequest,
        leaf: x509.Certificate,
        requested_at: datetime,
    ) -> None:
        if leaf.subject != x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)]):
            raise StepCAError("step-ca returned a mismatched certificate subject")
        if leaf.issuer != self._intermediate.subject:
            raise StepCAError("step-ca returned a mismatched certificate issuer")
        try:
            self._intermediate.public_key().verify(leaf.signature, leaf.tbs_certificate_bytes)
        except Exception as error:
            raise StepCAError("step-ca returned a certificate with an invalid signature") from error
        request_key = request.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if not isinstance(leaf.public_key(), ed25519.Ed25519PublicKey):
            raise StepCAError("step-ca returned a certificate with the wrong key type")
        leaf_key = leaf.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
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
        if not required_extensions <= extension_oids or not extension_oids <= allowed_extensions:
            raise StepCAError("step-ca returned an unexpected certificate extension profile")
        criticality = {value.oid: value.critical for value in leaf.extensions}
        if criticality[ExtensionOID.KEY_USAGE] is not True or criticality[ExtensionOID.EXTENDED_KEY_USAGE] is not False or criticality[ExtensionOID.SUBJECT_ALTERNATIVE_NAME] is not False:
            raise StepCAError("step-ca returned invalid certificate extension criticality")
        if ExtensionOID.BASIC_CONSTRAINTS in extension_oids:
            basic_constraints = leaf.extensions.get_extension_for_class(x509.BasicConstraints)
            if basic_constraints.critical is not True or basic_constraints.value != x509.BasicConstraints(False, None):
                raise StepCAError("step-ca returned a CA certificate")
        expected_usage = x509.KeyUsage(True, False, False, False, False, False, False, False, False)
        if leaf.extensions.get_extension_for_class(x509.KeyUsage).value != expected_usage:
            raise StepCAError("step-ca returned invalid key usage")
        expected_eku = x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH])
        if leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value != expected_eku:
            raise StepCAError("step-ca returned invalid extended key usage")
        expected_san = x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier(f"spiffe://vonk-forge.local/node/{node_id}")
        ])
        if leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value != expected_san:
            raise StepCAError("step-ca returned a mismatched node URI SAN")
        if ExtensionOID.AUTHORITY_KEY_IDENTIFIER in extension_oids:
            try:
                intermediate_skid = self._intermediate.extensions.get_extension_for_class(
                    x509.SubjectKeyIdentifier
                ).value.digest
            except x509.ExtensionNotFound as error:
                raise StepCAError("step-ca returned an unverifiable authority key identifier") from error
            authority_id = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
            if authority_id.key_identifier != intermediate_skid:
                raise StepCAError("step-ca returned a mismatched authority key identifier")
        if ExtensionOID.SUBJECT_KEY_IDENTIFIER in extension_oids:
            expected_skid = x509.SubjectKeyIdentifier.from_public_key(leaf.public_key())
            if leaf.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value != expected_skid:
                raise StepCAError("step-ca returned a mismatched subject key identifier")
        if leaf.not_valid_after_utc - leaf.not_valid_before_utc != self._certificate_lifetime:
            raise StepCAError("step-ca returned an invalid certificate lifetime")
        if abs(leaf.not_valid_before_utc - requested_at) > self._clock_skew:
            raise StepCAError("step-ca returned a certificate outside the allowed clock skew")

    def _token(
        self,
        subject: str,
        audience: str,
        now: datetime,
        *,
        sans: list[str] | None,
        request_id: str | None = None,
    ) -> str:
        timestamp = int(now.timestamp())
        claims: dict[str, object] = {
            "iss": self._provisioner_name,
            "sub": subject,
            "aud": audience,
            "iat": timestamp,
            "nbf": timestamp - int(self._clock_skew.total_seconds()),
            "exp": timestamp + 60,
            "jti": request_id or secrets.token_urlsafe(32),
        }
        if sans is not None:
            claims["sans"] = sans
        return jwt.encode(
            claims, self._credential, algorithm="ES256",
            headers={"kid": self._provisioner_kid, "typ": "JWT"},
        )

    def _json_request(self, method: str, path: str, body: dict[str, object] | None) -> object:
        raw = self._request(method, path, body, accept="application/json")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StepCAError("step-ca returned malformed JSON") from error

    def _request(self, method: str, path: str, body: dict[str, object] | None, *, accept: str) -> bytes:
        try:
            with self._client.stream(method, f"{self._ca_url}{path}", json=body, headers={"accept": accept}) as response:
                if response.is_redirect:
                    raise StepCAError("step-ca redirects are forbidden")
                if not response.is_success:
                    raise StepCAError(f"step-ca request failed with status {response.status_code}")
                output = bytearray()
                for chunk in response.iter_bytes():
                    if len(output) + len(chunk) > self._max_response_bytes:
                        raise StepCAError("step-ca response is too large")
                    output.extend(chunk)
                return bytes(output)
        except StepCAError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise StepCAError("step-ca request failed") from error


def _one_certificate(pem: bytes, label: str, *, provider_error: bool = False) -> x509.Certificate:
    try:
        certificate = x509.load_pem_x509_certificate(pem)
    except ValueError as error:
        exception = StepCAError if provider_error else ValueError
        raise exception(f"{label} certificate must be exactly one valid PEM certificate") from error
    normalized = certificate.public_bytes(serialization.Encoding.PEM)
    if pem.strip() != normalized.strip():
        exception = StepCAError if provider_error else ValueError
        raise exception(f"{label} certificate must be exactly one valid PEM certificate")
    return certificate


def _verify_ca_chain(
    root: x509.Certificate,
    intermediate: x509.Certificate,
    certificate_lifetime: timedelta,
) -> None:
    if root.subject != root.issuer:
        raise ValueError("root certificate must be self-issued")
    try:
        root.public_key().verify(root.signature, root.tbs_certificate_bytes)
    except Exception as error:
        raise ValueError("root certificate self-signature is invalid") from error
    try:
        root_constraints = root.extensions.get_extension_for_class(x509.BasicConstraints).value
        root_usage = root.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound as error:
        raise ValueError("root certificate must contain CA constraints and key usage") from error
    if not root_constraints.ca or root_constraints.path_length is not None and root_constraints.path_length < 1:
        raise ValueError("root certificate cannot issue the configured intermediate")
    if not root_usage.key_cert_sign or not root_usage.crl_sign:
        raise ValueError("root certificate must permit certificate and CRL signing")
    if intermediate.issuer != root.subject:
        raise ValueError("intermediate certificate is not issued by configured root")
    try:
        root.public_key().verify(intermediate.signature, intermediate.tbs_certificate_bytes)
    except Exception as error:
        raise ValueError("intermediate certificate signature is invalid") from error
    try:
        constraints = intermediate.extensions.get_extension_for_class(x509.BasicConstraints).value
        usage = intermediate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound as error:
        raise ValueError("intermediate certificate must contain CA constraints and key usage") from error
    if constraints != x509.BasicConstraints(True, 0) or not usage.key_cert_sign or not usage.crl_sign:
        raise ValueError("intermediate certificate is not a path-length-zero signing CA")
    now = datetime.now(UTC)
    if root.not_valid_before_utc > now or root.not_valid_after_utc <= now:
        raise ValueError("root certificate is not currently valid")
    if intermediate.not_valid_before_utc > now or intermediate.not_valid_after_utc <= now:
        raise ValueError("intermediate certificate is not currently valid")
    if intermediate.not_valid_after_utc <= now + certificate_lifetime:
        raise ValueError("intermediate certificate cannot cover configured leaf lifetime")
    if intermediate.not_valid_after_utc > root.not_valid_after_utc:
        raise ValueError("intermediate certificate outlives configured root")


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jwk_thumbprint(value: dict[str, object]) -> str:
    try:
        canonical = json.dumps(
            {name: value[name] for name in ("crv", "kty", "x", "y")},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")
    except (KeyError, UnicodeEncodeError) as error:
        raise ValueError("provisioner public metadata is missing thumbprint fields") from error
    return base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode("ascii")


def _validate_crl_freshness(
    crl: x509.CertificateRevocationList, timestamp: datetime, clock_skew: timedelta,
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
