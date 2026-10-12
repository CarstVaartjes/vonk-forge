"""Exercise setup PKI against cryptography and Python's strict TLS consumer."""

from __future__ import annotations

import base64
import hashlib
import json
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from asn1crypto import keys, pem
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from vonk_agent_protocol.installer_setup import NasStepCaControllerRequest
from vonk_nas_setup.pki import (
    generate_pki,
    normalize_intermediate_key,
    pki_hostnames,
    renew_controller_leaf,
    validate_pki_material,
)

ENVIRONMENT = {"VONK_CONTROL_HOSTNAME": "control.example.test"}
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(scope="module")
def request_contract() -> NasStepCaControllerRequest:
    return NasStepCaControllerRequest.model_validate_json(
        json.dumps(
            {
                "hostname_env": "VONK_CONTROL_HOSTNAME",
                "provisioner_name": "vonk-agent",
                "password_bytes": 32,
                "files": {
                    "root_certificate": "step-ca/root-certificate",
                    "intermediate_certificate": "step-ca/intermediate-certificate",
                    "intermediate_private_key": "step-ca/intermediate-key",
                    "controller_server_certificate": "controller-server-certificate",
                    "controller_server_private_key": "controller-server-key",
                    "provisioner_public_jwk": "agent-ca-provisioner-public-jwk",
                    "password": "step-ca-password",
                },
            }
        )
    )


@pytest.fixture(scope="module")
def material(request_contract: NasStepCaControllerRequest) -> dict[str, bytes]:
    return generate_pki(request_contract, ENVIRONMENT, now=NOW)


def _tls_handshake(
    server: ssl.SSLContext, client: ssl.SSLContext, hostname: str
) -> None:
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client_tls = client.wrap_bio(client_in, client_out, server_hostname=hostname)
    server_tls = server.wrap_bio(server_in, server_out, server_side=True)
    client_done = server_done = False
    for _ in range(20):
        for peer, outgoing, incoming, side in (
            (client_tls, client_out, server_in, "client"),
            (server_tls, server_out, client_in, "server"),
        ):
            try:
                peer.do_handshake()
                if side == "client":
                    client_done = True
                else:
                    server_done = True
            except ssl.SSLWantReadError:
                pass
            if outgoing.pending:
                incoming.write(outgoing.read())
        if client_done and server_done:
            return
    pytest.fail("TLS handshake did not complete within the memory transport exchanges")


def test_generated_chain_enrolls_with_strict_tls_and_root_only_trust(
    request_contract: NasStepCaControllerRequest,
    material: dict[str, bytes],
    tmp_path: Path,
) -> None:
    # Catches missing AKI/SKI, unsuitable CA usage, broken signatures, missing
    # intermediate and leaf names that isolated certificate parsing accepts.
    paths = request_contract.files
    certificate, key = tmp_path / "server.pem", tmp_path / "key.pem"
    certificate.write_bytes(material[paths.controller_server_certificate])
    key.write_bytes(material[paths.controller_server_private_key])
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate, key)
    client = ssl.create_default_context(
        cadata=material[paths.root_certificate].decode()
    )
    client.verify_flags |= ssl.VERIFY_X509_STRICT
    for hostname in pki_hostnames(request_contract, ENVIRONMENT):
        _tls_handshake(server, client, hostname)
    with pytest.raises(ssl.SSLCertVerificationError):
        _tls_handshake(server, client, "unrelated.example.test")


def test_generated_intermediate_loads_with_controller_crypto_and_explicit_encryption_policy(
    request_contract: NasStepCaControllerRequest, material: dict[str, bytes]
) -> None:
    # Catches an unreadable OneAsymmetricKey, cleartext key or silently weaker KDF.
    paths = request_contract.files
    encrypted = material[paths.intermediate_private_key]
    key = serialization.load_pem_private_key(encrypted, material[paths.password])
    assert isinstance(key, ed25519.Ed25519PrivateKey)
    intermediate = x509.load_pem_x509_certificate(
        material[paths.intermediate_certificate]
    )
    assert key.public_key() == intermediate.public_key()
    label, _, der = pem.unarmor(encrypted)
    assert label == "ENCRYPTED PRIVATE KEY"
    algorithm = keys.EncryptedPrivateKeyInfo.load(der)["encryption_algorithm"]
    assert algorithm["algorithm"].native == "pbes2"
    parameters = algorithm["parameters"]
    kdf = parameters["key_derivation_func"]
    assert kdf["algorithm"].native == "pbkdf2"
    assert kdf["parameters"]["iteration_count"].native == 600_000
    assert kdf["parameters"]["prf"]["algorithm"].native == "sha256"
    assert parameters["encryption_scheme"]["algorithm"].native == "aes256_cbc"
    assert normalize_intermediate_key(request_contract, material) == {}
    with pytest.raises(ValueError):
        serialization.load_pem_private_key(encrypted, b"wrong password")


@pytest.mark.parametrize(
    "days_remaining,renew", [(31, False), (30, True), (29, True), (-1, True)]
)
def test_renewal_threshold_keeps_authority_and_valid_identity(
    request_contract: NasStepCaControllerRequest,
    material: dict[str, bytes],
    days_remaining: int,
    renew: bool,
) -> None:
    # Catches rotating root/issuer/JWK or every healthy leaf during an upgrade,
    # and failure to recover a coherent expired leaf through the retained issuer.
    now = NOW + timedelta(days=397 - days_remaining)
    before = dict(material)
    result = validate_pki_material(
        request_contract, ENVIRONMENT, material, now=now, allow_expired_controller=True
    )
    assert result.controller_needs_renewal is renew
    replacement = renew_controller_leaf(
        request_contract, ENVIRONMENT, material, now=now
    )
    assert bool(replacement) is renew
    assert material == before
    candidate = {**material, **replacement}
    validate_pki_material(request_contract, ENVIRONMENT, candidate, now=now)
    assert (
        renew_controller_leaf(request_contract, ENVIRONMENT, candidate, now=now) == {}
    )
    if renew:
        paths = request_contract.files
        for path, content in material.items():
            if path not in (
                paths.controller_server_certificate,
                paths.controller_server_private_key,
            ):
                assert candidate[path] == content
        assert (
            candidate[paths.controller_server_certificate]
            != material[paths.controller_server_certificate]
        )
        leaf = x509.load_pem_x509_certificates(
            candidate[paths.controller_server_certificate]
        )[0]
        assert leaf.not_valid_after_utc == now + timedelta(days=397)


def test_hostname_change_renews_leaf_without_rotating_authority(
    request_contract: NasStepCaControllerRequest, material: dict[str, bytes]
) -> None:
    # Catches preserving an obsolete SAN or generating fresh trust anchors.
    environment = {"VONK_CONTROL_HOSTNAME": "New-Control.example.test"}
    with pytest.raises(ValueError, match="hostnames"):
        validate_pki_material(request_contract, environment, material, now=NOW)
    replacement = renew_controller_leaf(
        request_contract, environment, material, now=NOW
    )
    validate_pki_material(
        request_contract, environment, {**material, **replacement}, now=NOW
    )
    assert request_contract.files.root_certificate not in replacement
    assert request_contract.files.intermediate_private_key not in replacement


@pytest.mark.parametrize(
    "damaged", ["server_key", "intermediate_key", "password", "root", "chain", "jwk"]
)
def test_corruption_refuses_before_expired_leaf_renewal_and_recovers_when_repaired(
    request_contract: NasStepCaControllerRequest,
    material: dict[str, bytes],
    damaged: str,
) -> None:
    # Catches bypassing key/issuer/JWK checks solely because the leaf has expired.
    paths = request_contract.files
    candidate = dict(material)
    unrelated_key = ed25519.Ed25519PrivateKey.generate()
    if damaged == "server_key":
        candidate[paths.controller_server_private_key] = unrelated_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    elif damaged == "intermediate_key":
        candidate[paths.intermediate_private_key] = material[
            paths.intermediate_private_key
        ].replace(b"A", b"B", 1)
    elif damaged == "password":
        candidate[paths.password] = b"wrong password"
    elif damaged == "root":
        candidate[paths.root_certificate] = material[paths.intermediate_certificate]
    elif damaged == "chain":
        candidate[paths.controller_server_certificate] += b"unexpected trailing bytes"
    else:
        public = json.loads(material[paths.provisioner_public_jwk])
        public["kid"] = "unrelated"
        candidate[paths.provisioner_public_jwk] = json.dumps(public).encode()
    before = dict(candidate)
    expired = NOW + timedelta(days=398)
    with pytest.raises(ValueError, match="Step CA/controller PKI"):
        renew_controller_leaf(request_contract, ENVIRONMENT, candidate, now=expired)
    assert candidate == before
    replacement = renew_controller_leaf(
        request_contract, ENVIRONMENT, material, now=expired
    )
    validate_pki_material(
        request_contract, ENVIRONMENT, {**material, **replacement}, now=expired
    )


def test_jwk_rejects_invalid_p256_point_even_with_valid_thumbprint(
    request_contract: NasStepCaControllerRequest, material: dict[str, bytes]
) -> None:
    # Catches accepting metadata/thumbprints without validating the curve point.
    public = json.loads(material[request_contract.files.provisioner_public_jwk])
    public["x"] = public["y"] = (
        base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode()
    )
    canonical = json.dumps(
        {name: public[name] for name in ("crv", "kty", "x", "y")},
        separators=(",", ":"),
        sort_keys=True,
    )
    public["kid"] = (
        base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    candidate = {
        **material,
        request_contract.files.provisioner_public_jwk: json.dumps(public).encode(),
    }
    with pytest.raises(ValueError, match="JWK"):
        validate_pki_material(request_contract, ENVIRONMENT, candidate, now=NOW)


def _resign(
    certificate: x509.Certificate,
    issuer_key: ed25519.Ed25519PrivateKey,
    *,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
    wrong_aki: bool = False,
) -> bytes:
    builder = (
        x509.CertificateBuilder()
        .subject_name(certificate.subject)
        .issuer_name(certificate.issuer)
        .public_key(certificate.public_key())
        .serial_number(certificate.serial_number)
        .not_valid_before(not_before or certificate.not_valid_before_utc)
        .not_valid_after(not_after or certificate.not_valid_after_utc)
    )
    for extension in certificate.extensions:
        value = extension.value
        if wrong_aki and isinstance(value, x509.AuthorityKeyIdentifier):
            value = x509.AuthorityKeyIdentifier(bytes(20), None, None)
        builder = builder.add_extension(value, extension.critical)
    return builder.sign(issuer_key, None).public_bytes(serialization.Encoding.PEM)


@pytest.mark.parametrize("damaged", ["future_leaf", "leaf_aki", "expired_intermediate"])
def test_signed_but_invalid_certificates_fail_closed(
    request_contract: NasStepCaControllerRequest,
    material: dict[str, bytes],
    damaged: str,
) -> None:
    # Catches checking signatures while ignoring time or the strict TLS issuer binding.
    paths = request_contract.files
    key = serialization.load_pem_private_key(
        material[paths.intermediate_private_key], material[paths.password]
    )
    assert isinstance(key, ed25519.Ed25519PrivateKey)
    leaf = x509.load_pem_x509_certificates(
        material[paths.controller_server_certificate]
    )[0]
    candidate = dict(material)
    if damaged == "expired_intermediate":
        with pytest.raises(ValueError, match="CA certificate"):
            renew_controller_leaf(
                request_contract, ENVIRONMENT, material, now=NOW + timedelta(days=1826)
            )
        return
    candidate[paths.controller_server_certificate] = (
        _resign(
            leaf,
            key,
            not_before=NOW + timedelta(days=1) if damaged == "future_leaf" else None,
            wrong_aki=damaged == "leaf_aki",
        )
        + material[paths.intermediate_certificate]
    )
    with pytest.raises(ValueError, match="Step CA/controller PKI"):
        renew_controller_leaf(request_contract, ENVIRONMENT, candidate, now=NOW)


def test_expired_leaf_is_invalid_until_upgrade_renewal(
    request_contract: NasStepCaControllerRequest, material: dict[str, bytes]
) -> None:
    # Catches allowing an expired server certificate in ordinary install validation.
    with pytest.raises(ValueError, match="outside its validity"):
        validate_pki_material(
            request_contract, ENVIRONMENT, material, now=NOW + timedelta(days=398)
        )


@pytest.mark.parametrize(
    "hostname",
    ["", "bad.example.", "-bad.example", "bad..example", "bad name", "é.example"],
)
def test_invalid_hostname_cannot_issue_a_controller_identity(
    request_contract: NasStepCaControllerRequest, hostname: str
) -> None:
    # Catches turning malformed operator input into a different certificate identity.
    with pytest.raises(ValueError, match="hostname"):
        generate_pki(request_contract, {"VONK_CONTROL_HOSTNAME": hostname}, now=NOW)


def test_intermediate_authority_key_identifier_must_match_root_subject_key_identifier(
    request_contract: NasStepCaControllerRequest, material: dict[str, bytes]
) -> None:
    # Catches accepting a cryptographically signed intermediate that the strict
    # enrollment TLS consumer rejects because its issuer identifier is wrong.
    paths = request_contract.files
    root_key = ed25519.Ed25519PrivateKey.generate()
    old_root = x509.load_pem_x509_certificate(material[paths.root_certificate])
    root = (
        x509.CertificateBuilder()
        .subject_name(old_root.subject)
        .issuer_name(old_root.subject)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(hours=1))
        .not_valid_after(NOW + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(True, 1), True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, True, True, False, False),
            True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), False
        )
        .sign(root_key, None)
    )
    intermediate = x509.load_pem_x509_certificate(
        material[paths.intermediate_certificate]
    )
    signed_intermediate = _resign(intermediate, root_key, wrong_aki=True)
    leaf = x509.load_pem_x509_certificates(
        material[paths.controller_server_certificate]
    )[0]
    candidate = {
        **material,
        paths.root_certificate: root.public_bytes(serialization.Encoding.PEM),
        paths.intermediate_certificate: signed_intermediate,
        paths.controller_server_certificate: leaf.public_bytes(
            serialization.Encoding.PEM
        )
        + signed_intermediate,
    }
    with pytest.raises(ValueError, match="authority key identifier"):
        validate_pki_material(request_contract, ENVIRONMENT, candidate, now=NOW)


def test_normalizing_previous_pkcs8_encoding_preserves_signer_and_runs_once(
    request_contract: NasStepCaControllerRequest, material: dict[str, bytes]
) -> None:
    # Catches dropping existing encrypted signer authority, retaining the old
    # OpenSSL-unreadable public-key extension, or reencrypting on every upgrade.
    from asn1crypto import core
    from cryptography.hazmat.primitives import hashes, padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    class OneAsymmetricKey(core.Sequence):
        _fields: ClassVar = [
            ("version", core.Integer),
            ("private_key_algorithm", keys.PrivateKeyAlgorithm),
            ("private_key", core.OctetString),
            ("public_key", core.OctetBitString, {"implicit": 1}),
        ]

    paths = request_contract.files
    password = material[paths.password]
    original = material[paths.intermediate_private_key]
    key = serialization.load_pem_private_key(original, password)
    assert isinstance(key, ed25519.Ed25519PrivateKey)
    canonical = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    info = keys.PrivateKeyInfo.load(canonical)
    old_der = OneAsymmetricKey(
        {
            "version": 1,
            "private_key_algorithm": info["private_key_algorithm"],
            "private_key": info["private_key"].contents,
            "public_key": key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            ),
        }
    ).dump()
    _, _, envelope_der = pem.unarmor(original)
    envelope = keys.EncryptedPrivateKeyInfo.load(envelope_der)
    parameters = envelope["encryption_algorithm"]["parameters"]
    kdf = parameters["key_derivation_func"]["parameters"]
    derived = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=kdf["salt"].native,
        iterations=kdf["iteration_count"].native,
    ).derive(password)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(old_der) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(derived),
        modes.CBC(parameters["encryption_scheme"]["parameters"].native),
    ).encryptor()
    envelope["encrypted_data"] = encryptor.update(padded) + encryptor.finalize()
    candidate = {
        **material,
        paths.intermediate_private_key: pem.armor(
            "ENCRYPTED PRIVATE KEY", envelope.dump()
        ),
    }
    validate_pki_material(request_contract, ENVIRONMENT, candidate, now=NOW)
    replacement = normalize_intermediate_key(request_contract, candidate)
    normalized = serialization.load_pem_private_key(
        replacement[paths.intermediate_private_key], password
    )
    assert isinstance(normalized, ed25519.Ed25519PrivateKey)
    assert normalized.public_key() == key.public_key()
    assert (
        normalize_intermediate_key(request_contract, {**candidate, **replacement}) == {}
    )


def test_rust_controller_public_key_encoding_is_valid_and_normalizes_once(
    request_contract: NasStepCaControllerRequest,
    material: dict[str, bytes],
    tmp_path: Path,
) -> None:
    # Catches using OpenSSL's version-0-only loader before validating and
    # preserving the current Rust producer's version-1 controller identity.
    from asn1crypto import core

    class OneAsymmetricKey(core.Sequence):
        _fields: ClassVar = [
            ("version", core.Integer),
            ("private_key_algorithm", keys.PrivateKeyAlgorithm),
            ("private_key", core.OctetString),
            ("public_key", core.OctetBitString, {"implicit": 1}),
        ]

    paths = request_contract.files
    key = serialization.load_pem_private_key(
        material[paths.controller_server_private_key], None
    )
    assert isinstance(key, ed25519.Ed25519PrivateKey)
    canonical = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    info = keys.PrivateKeyInfo.load(canonical)
    rust_der = OneAsymmetricKey(
        {
            "version": 1,
            "private_key_algorithm": info["private_key_algorithm"],
            "private_key": info["private_key"].contents,
            "public_key": key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            ),
        }
    ).dump()
    candidate = {
        **material,
        paths.controller_server_private_key: pem.armor("PRIVATE KEY", rust_der),
    }
    invalid_public = OneAsymmetricKey.load(rust_der)
    invalid_public["public_key"] = bytes(32)
    with pytest.raises(ValueError, match="Ed25519 PKCS#8"):
        validate_pki_material(
            request_contract,
            ENVIRONMENT,
            {
                **candidate,
                paths.controller_server_private_key: pem.armor(
                    "PRIVATE KEY", invalid_public.dump()
                ),
            },
            now=NOW,
        )
    validate_pki_material(request_contract, ENVIRONMENT, candidate, now=NOW)
    from vonk_nas_setup.pki import normalize_controller_key

    replacement = normalize_controller_key(request_contract, candidate)
    loaded = serialization.load_pem_private_key(
        replacement[paths.controller_server_private_key], None
    )
    assert isinstance(loaded, ed25519.Ed25519PrivateKey)
    assert loaded.public_key() == key.public_key()
    normalized = {**candidate, **replacement}
    assert normalize_controller_key(request_contract, normalized) == {}
    assert (
        renew_controller_leaf(request_contract, ENVIRONMENT, normalized, now=NOW) == {}
    )
    assert (
        normalized[paths.controller_server_certificate]
        == material[paths.controller_server_certificate]
    )
    certificate_path, key_path = tmp_path / "server.pem", tmp_path / "key.pem"
    certificate_path.write_bytes(normalized[paths.controller_server_certificate])
    key_path.write_bytes(normalized[paths.controller_server_private_key])
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate_path, key_path)
    client = ssl.create_default_context(
        cadata=normalized[paths.root_certificate].decode()
    )
    client.verify_flags |= ssl.VERIFY_X509_STRICT
    _tls_handshake(server, client, ENVIRONMENT[request_contract.hostname_env])
