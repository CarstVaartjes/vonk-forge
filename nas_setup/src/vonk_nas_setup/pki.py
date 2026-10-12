"""Generate and validate NAS CA material without changing retained identities."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from asn1crypto import core, keys, pem
from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, padding, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from vonk_agent_protocol.installer_setup import NasStepCaControllerRequest

_VALIDITY = timedelta(days=397)
_RENEWAL_THRESHOLD = timedelta(days=30)
_PBKDF2_ITERATIONS = 600_000


@dataclass(frozen=True)
class ValidatedPkiMaterial:
    controller_needs_renewal: bool


def _invalid(message: str) -> ValueError:
    return ValueError(f"Step CA/controller PKI {message}")


def pki_hostnames(
    request: NasStepCaControllerRequest, environment: Mapping[str, str]
) -> list[str]:
    hostname = environment.get(request.hostname_env, "")
    if len(hostname) > 253 or not all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in hostname.split(".")
    ):
        raise _invalid(f"hostname value for {request.hostname_env} is invalid")
    hostname = hostname.lower()
    return [
        hostname,
        *(f"{prefix}.{hostname}" for prefix in ("enroll", "agents", "registry")),
    ]


def _private_pem(key: ed25519.Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _private_der(key: ed25519.Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _encrypt_intermediate(key: ed25519.Ed25519PrivateKey, password: bytes) -> bytes:
    # BestAvailableEncryption deliberately has no fixed KDF policy. Encode the
    # PKCS#8 envelope with ASN1crypto; all crypto operations use cryptography.
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(16)
    derived = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_PBKDF2_ITERATIONS
    ).derive(password)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(_private_der(key)) + padder.finalize()
    encryptor = Cipher(algorithms.AES(derived), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    envelope = keys.EncryptedPrivateKeyInfo(
        {
            "encryption_algorithm": {
                "algorithm": "pbes2",
                "parameters": {
                    "key_derivation_func": {
                        "algorithm": "pbkdf2",
                        "parameters": {
                            "salt": {"specified": salt},
                            "iteration_count": _PBKDF2_ITERATIONS,
                            "prf": {"algorithm": "sha256"},
                        },
                    },
                    "encryption_scheme": {"algorithm": "aes256_cbc", "parameters": iv},
                },
            },
            "encrypted_data": ciphertext,
        }
    )
    return pem.armor("ENCRYPTED PRIVATE KEY", envelope.dump())


def _pem_der(value: bytes, label: str) -> bytes:
    expression = (
        rb"\s*-----BEGIN " + label.encode() + rb"-----\s+[A-Za-z0-9+/=\s]+"
        rb"-----END " + label.encode() + rb"-----\s*"
    )
    if re.fullmatch(expression, value) is None:
        raise _invalid(f"{label.lower()} is not a single valid PEM block")
    actual_label, _, der = pem.unarmor(value)
    if actual_label != label or not isinstance(der, bytes):
        raise _invalid("private key PEM label is invalid")
    return der


def _decrypt_intermediate(value: bytes, password: bytes) -> bytes:
    try:
        envelope = keys.EncryptedPrivateKeyInfo.load(
            _pem_der(value, "ENCRYPTED PRIVATE KEY"), strict=True
        )
        algorithm = envelope["encryption_algorithm"]
        if algorithm["algorithm"].native != "pbes2":
            raise _invalid("intermediate key encryption is not PBES2")
        parameters = algorithm["parameters"]
        kdf, cipher = parameters["key_derivation_func"], parameters["encryption_scheme"]
        params = kdf["parameters"]
        if (
            kdf["algorithm"].native != "pbkdf2"
            or params["prf"]["algorithm"].native != "sha256"
            or params["iteration_count"].native != _PBKDF2_ITERATIONS
            or cipher["algorithm"].native != "aes256_cbc"
            or params["salt"].name != "specified"
            or params["key_length"].native not in (None, 32)
        ):
            raise _invalid("intermediate key encryption policy is invalid")
        salt, iv = params["salt"].native, cipher["parameters"].native
        if len(salt) < 16 or len(iv) != 16:
            raise _invalid("intermediate key encryption parameters are invalid")
        derived = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=_PBKDF2_ITERATIONS,
        ).derive(password)
        decryptor = Cipher(algorithms.AES(derived), modes.CBC(iv)).decryptor()
        padded = (
            decryptor.update(envelope["encrypted_data"].native) + decryptor.finalize()
        )
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except (ValueError, TypeError, KeyError, UnsupportedAlgorithm):
        raise _invalid("intermediate private key cannot be decrypted") from None


class _Ed25519KeyInfo(keys.PrivateKeyInfo):
    # PKCS#8's current OneAsymmetricKey form used by the Rust producer includes
    # the optional [1] public key. ASN1crypto owns all field decoding.
    _fields: ClassVar = [
        *keys.PrivateKeyInfo._fields,
        ("public_key", core.OctetBitString, {"implicit": 1, "optional": True}),
    ]


def _load_ed25519_der(plaintext: bytes) -> ed25519.Ed25519PrivateKey:
    try:
        info = _Ed25519KeyInfo.load(plaintext, strict=True)
        if (
            info["version"].native not in (0, 1)
            or info["private_key_algorithm"]["algorithm"].native != "ed25519"
        ):
            raise TypeError("wrong key algorithm")
        canonical = keys.PrivateKeyInfo(
            {
                "version": 0,
                "private_key_algorithm": info["private_key_algorithm"],
                "private_key": info["private_key"].native,
            }
        ).dump()
        key = serialization.load_der_private_key(canonical, password=None)
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise TypeError("wrong key algorithm")
        embedded_public = info["public_key"].native
        if embedded_public is not None and (
            info["version"].native != 1
            or embedded_public
            != key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ):
            raise _invalid("private key embedded public key does not match its signer")
        return key
    except (ValueError, TypeError, KeyError, UnsupportedAlgorithm):
        raise _invalid("private key is not valid Ed25519 PKCS#8") from None


def load_ed25519_private_key(content: bytes) -> ed25519.Ed25519PrivateKey:
    """Read the producer's PKCS#8 PEM while validating any embedded public key."""
    return _load_ed25519_der(_pem_der(content, "PRIVATE KEY"))


def _intermediate_key(
    value: bytes, password: bytes
) -> tuple[ed25519.Ed25519PrivateKey, bytes]:
    plaintext = _decrypt_intermediate(value, password)
    return _load_ed25519_der(plaintext), plaintext


def _password(value: bytes) -> bytes:
    password = value.rstrip(b"\r\n")
    if not password or any(
        character < 32 or character == 127 for character in password
    ):
        raise _invalid("intermediate password must be nonempty single-line text")
    try:
        password.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid("intermediate password must be UTF-8 text") from None
    return password


def _builder(
    key: ed25519.Ed25519PrivateKey,
    name: str,
    issuer: x509.Certificate | None,
    issuer_key: ed25519.Ed25519PrivateKey,
    now: datetime,
    validity: timedelta,
) -> x509.CertificateBuilder:
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject if issuer is not None else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + validity)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            False,
        )
    )


def _ca(
    key: ed25519.Ed25519PrivateKey,
    name: str,
    path_length: int,
    validity_days: int,
    issuer: x509.Certificate | None,
    issuer_key: ed25519.Ed25519PrivateKey,
    now: datetime,
) -> x509.Certificate:
    return (
        _builder(key, name, issuer, issuer_key, now, timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=path_length), True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, True, True, False, False),
            True,
        )
        .sign(issuer_key, algorithm=None)
    )


def _leaf(
    key: ed25519.Ed25519PrivateKey,
    hostnames: list[str],
    intermediate: x509.Certificate,
    intermediate_key: ed25519.Ed25519PrivateKey,
    now: datetime,
) -> x509.Certificate:
    return (
        _builder(key, hostnames[0], intermediate, intermediate_key, now, _VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in hostnames]),
            False,
        )
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(intermediate_key, algorithm=None)
    )


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _thumbprint(x: str, y: str) -> str:
    canonical = json.dumps(
        {"crv": "P-256", "kty": "EC", "x": x, "y": y},
        separators=(",", ":"),
        sort_keys=True,
    )
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def _public_jwk() -> bytes:
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    x, y = _b64url(point.x.to_bytes(32, "big")), _b64url(point.y.to_bytes(32, "big"))
    return json.dumps(
        {
            "alg": "ES256",
            "crv": "P-256",
            "kid": _thumbprint(x, y),
            "kty": "EC",
            "use": "sig",
            "x": x,
            "y": y,
        },
        separators=(",", ":"),
    ).encode()


def generate_pki(
    request: NasStepCaControllerRequest,
    environment: Mapping[str, str],
    *,
    now: datetime | None = None,
) -> dict[str, bytes]:
    now = now or datetime.now(UTC)
    hostnames = pki_hostnames(request, environment)
    if request.password_bytes < 1:
        raise _invalid("password entropy byte count must be positive")
    password = secrets.token_hex(request.password_bytes).encode("ascii")
    root_key, intermediate_key, server_key = (
        ed25519.Ed25519PrivateKey.generate() for _ in range(3)
    )
    root = _ca(root_key, "Vonk Forge Root CA", 1, 3650, None, root_key, now)
    intermediate = _ca(
        intermediate_key,
        "Vonk Forge Agent Intermediate CA",
        0,
        1825,
        root,
        root_key,
        now,
    )
    server = _leaf(server_key, hostnames, intermediate, intermediate_key, now)
    paths = request.files
    files = {
        paths.root_certificate: root.public_bytes(serialization.Encoding.PEM),
        paths.intermediate_certificate: intermediate.public_bytes(
            serialization.Encoding.PEM
        ),
        paths.intermediate_private_key: _encrypt_intermediate(
            intermediate_key, password
        ),
        paths.controller_server_certificate: server.public_bytes(
            serialization.Encoding.PEM
        )
        + intermediate.public_bytes(serialization.Encoding.PEM),
        paths.controller_server_private_key: _private_pem(server_key),
        paths.provisioner_public_jwk: _public_jwk(),
        paths.password: password,
    }
    validate_pki_material(request, environment, files, now=now)
    return files


def _certificates(value: bytes, count: int) -> list[x509.Certificate]:
    expression = (
        rb"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----"
    )
    blocks = re.findall(expression, value)
    if len(blocks) != count or re.sub(expression, b"", value).strip():
        raise _invalid("certificate chain is inconsistent or not valid PEM")
    return [x509.load_pem_x509_certificate(block) for block in blocks]


def _validate_jwk(value: bytes) -> None:
    try:
        public = json.loads(value)
        if (
            public["alg"] != "ES256"
            or public["crv"] != "P-256"
            or public["kty"] != "EC"
            or public["use"] != "sig"
            or "d" in public
            or public["kid"] != _thumbprint(public["x"], public["y"])
        ):
            raise ValueError("invalid metadata")
        coordinates = []
        for member in (public["x"], public["y"]):
            coordinate = base64.b64decode(member + "=", altchars=b"-_", validate=True)
            if len(coordinate) != 32 or _b64url(coordinate) != member:
                raise ValueError("invalid coordinate")
            coordinates.append(int.from_bytes(coordinate, "big"))
        ec.EllipticCurvePublicNumbers(
            coordinates[0], coordinates[1], ec.SECP256R1()
        ).public_key()
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        raise _invalid("public provisioner JWK is invalid") from None


def _check_authority(child: x509.Certificate, issuer: x509.Certificate) -> None:
    child.verify_directly_issued_by(issuer)
    public_key = child.public_key()
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise _invalid("certificate key is not Ed25519")
    if child != issuer:
        aki = child.extensions.get_extension_for_class(
            x509.AuthorityKeyIdentifier
        ).value
        ski = issuer.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        if aki.key_identifier != ski.digest:
            raise _invalid("certificate authority key identifier does not match issuer")


def validate_pki_material(
    request: NasStepCaControllerRequest,
    environment: Mapping[str, str],
    files: Mapping[str, bytes],
    *,
    now: datetime | None = None,
    allow_expired_controller: bool = False,
) -> ValidatedPkiMaterial:
    now = now or datetime.now(UTC)
    paths = request.files
    try:
        root = _certificates(files[paths.root_certificate], 1)[0]
        intermediate = _certificates(files[paths.intermediate_certificate], 1)[0]
        server, bundled = _certificates(files[paths.controller_server_certificate], 2)
        if bundled != intermediate:
            raise _invalid("certificate chain is inconsistent")
        for child, issuer in (
            (root, root),
            (intermediate, root),
            (server, intermediate),
        ):
            _check_authority(child, issuer)
        for certificate, length in ((root, 1), (intermediate, 0)):
            constraints = certificate.extensions.get_extension_for_class(
                x509.BasicConstraints
            )
            usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)
            if (
                not constraints.critical
                or not constraints.value.ca
                or constraints.value.path_length != length
                or not usage.value.key_cert_sign
            ):
                raise _invalid("CA certificate constraints are invalid")
            if (
                not certificate.not_valid_before_utc
                <= now
                <= certificate.not_valid_after_utc
            ):
                raise _invalid("CA certificate is outside its validity period")
        sans = server.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        hostnames_changed = list(sans) != [
            x509.DNSName(name) for name in pki_hostnames(request, environment)
        ]
        if hostnames_changed and not allow_expired_controller:
            raise _invalid(
                "controller certificate hostnames do not match configured hostnames"
            )
        try:
            server_is_ca = server.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value.ca
        except x509.ExtensionNotFound:
            server_is_ca = False
        if (
            server_is_ca
            or ExtendedKeyUsageOID.SERVER_AUTH
            not in server.extensions.get_extension_for_class(
                x509.ExtendedKeyUsage
            ).value
            or not server.extensions.get_extension_for_class(
                x509.KeyUsage
            ).value.digital_signature
        ):
            raise _invalid("controller certificate server constraints are invalid")
        if server.not_valid_before_utc > now:
            raise _invalid("controller certificate is not valid yet")
        if server.not_valid_after_utc < now and not allow_expired_controller:
            raise _invalid("controller certificate is outside its validity period")
        server_key = load_ed25519_private_key(
            files[paths.controller_server_private_key]
        )
        if (
            not isinstance(server_key, ed25519.Ed25519PrivateKey)
            or server_key.public_key() != server.public_key()
        ):
            raise _invalid(
                "controller private key does not match controller certificate"
            )
        intermediate_key, _ = _intermediate_key(
            files[paths.intermediate_private_key], _password(files[paths.password])
        )
        if intermediate_key.public_key() != intermediate.public_key():
            raise _invalid(
                "intermediate private key does not match intermediate certificate"
            )
        _validate_jwk(files[paths.provisioner_public_jwk])
    except KeyError:
        raise _invalid("a required PKI member is missing") from None
    except (InvalidSignature, x509.ExtensionNotFound, UnsupportedAlgorithm):
        raise _invalid(
            "certificate signature or required extension is invalid"
        ) from None
    except ValueError as error:
        if str(error).startswith("Step CA/controller PKI "):
            raise
        raise _invalid("certificate or key encoding is invalid") from None
    return ValidatedPkiMaterial(
        controller_needs_renewal=hostnames_changed
        or server.not_valid_after_utc <= now + _RENEWAL_THRESHOLD
    )


def normalize_intermediate_key(
    request: NasStepCaControllerRequest, files: Mapping[str, bytes]
) -> dict[str, bytes]:
    paths = request.files
    password = _password(files[paths.password])
    key, plaintext = _intermediate_key(files[paths.intermediate_private_key], password)
    certificate = _certificates(files[paths.intermediate_certificate], 1)[0]
    if key.public_key() != certificate.public_key():
        raise _invalid(
            "intermediate private key does not match intermediate certificate"
        )
    if plaintext == _private_der(key):
        return {}
    return {paths.intermediate_private_key: _encrypt_intermediate(key, password)}


def normalize_controller_key(
    request: NasStepCaControllerRequest, files: Mapping[str, bytes]
) -> dict[str, bytes]:
    """Make the retained controller signer consumable by OpenSSL exactly once."""
    paths = request.files
    existing = files[paths.controller_server_private_key]
    key = load_ed25519_private_key(existing)
    certificate = _certificates(files[paths.controller_server_certificate], 2)[0]
    if key.public_key() != certificate.public_key():
        raise _invalid("controller private key does not match controller certificate")
    canonical = _private_pem(key)
    return (
        {}
        if existing == canonical
        else {paths.controller_server_private_key: canonical}
    )


def renew_controller_leaf(
    request: NasStepCaControllerRequest,
    environment: Mapping[str, str],
    files: Mapping[str, bytes],
    *,
    now: datetime | None = None,
) -> dict[str, bytes]:
    now = now or datetime.now(UTC)
    validated = validate_pki_material(
        request, environment, files, now=now, allow_expired_controller=True
    )
    if not validated.controller_needs_renewal:
        return {}
    paths = request.files
    intermediate_key, _ = _intermediate_key(
        files[paths.intermediate_private_key], _password(files[paths.password])
    )
    intermediate = _certificates(files[paths.intermediate_certificate], 1)[0]
    key = ed25519.Ed25519PrivateKey.generate()
    certificate = _leaf(
        key, pki_hostnames(request, environment), intermediate, intermediate_key, now
    )
    replacement = {
        paths.controller_server_certificate: certificate.public_bytes(
            serialization.Encoding.PEM
        )
        + files[paths.intermediate_certificate],
        paths.controller_server_private_key: _private_pem(key),
    }
    validate_pki_material(request, environment, {**files, **replacement}, now=now)
    return replacement
