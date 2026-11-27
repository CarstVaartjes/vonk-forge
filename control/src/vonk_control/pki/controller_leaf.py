"""Choose, or mint, the controller TLS leaf that Caddy serves.

The installer issues the first leaf and stays the authority for its names.
``runtime-init`` renews it on every start. The served pair is the newest
qualifying one, taken from the volume pair and the installer pair. A pair
qualifies when its key matches, it is issued by the current intermediate and
it carries exactly the installer leaf's names. A new leaf is minted when no
qualifying pair has more than the renewal window left. The window equals the
first expiry warning, so the redeploy that the warning asks for also clears it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_LOGGER = logging.getLogger(__name__)
# Renew this long before expiry: the ControllerCertificateExpiring warning.
RENEWAL_WINDOW = timedelta(days=60)
# The installer's leaf lifetime (the CA/Browser 397-day maximum).
_VALIDITY = timedelta(days=397)
_PEM_BLOCK = re.compile(
    rb"-----BEGIN ([A-Z ]+)-----\s+[A-Za-z0-9+/=\s]+?-----END \1-----\s*"
)


@dataclass(frozen=True)
class LeafPair:
    # The leaf followed by the intermediate, as Caddy serves the chain.
    certificate: bytes
    key: bytes

    def served_file(self) -> bytes:
        """The chain then the key in one file: one rename publishes both."""
        return self.certificate + self.key

    @classmethod
    def from_served_file(cls, content: bytes) -> LeafPair:
        certificate, key = b"", b""
        for match in _PEM_BLOCK.finditer(content):
            if match.group(1) == b"CERTIFICATE":
                certificate += match.group(0)
            elif match.group(1).endswith(b"PRIVATE KEY") and not key:
                key = match.group(0)
            else:
                raise ValueError("controller TLS file holds an unexpected block")
        if not certificate or not key:
            raise ValueError("controller TLS file lacks its chain or its key")
        return cls(certificate, key)


def installer_names(installer_certificate: bytes) -> list[str]:
    """The DNS names of the installer's leaf, in order: the name authority."""
    leaf = x509.load_pem_x509_certificates(installer_certificate)[0]
    names = leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)
    if not names:
        raise ValueError("installer controller certificate names no host")
    return names


class Disqualified(StrEnum):
    """Why a pair is not served; the next pair, or a fresh leaf, replaces it."""

    DAMAGED = "damaged"
    OTHER_ISSUER = "other-issuer"
    KEY_MISMATCH = "key-mismatch"
    NAMES_CHANGED = "names-changed"
    NOT_YET_VALID = "not-yet-valid"


def _qualified(
    pair: LeafPair, intermediate: x509.Certificate, names: list[str], now: datetime
) -> x509.Certificate | Disqualified:
    try:
        chain = x509.load_pem_x509_certificates(pair.certificate)
        if len(chain) != 2 or chain[1] != intermediate:
            return Disqualified.OTHER_ISSUER
        leaf = chain[0]
        leaf.verify_directly_issued_by(intermediate)
        key = serialization.load_pem_private_key(pair.key, password=None)
        sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except (
        ValueError,
        TypeError,
        InvalidSignature,
        UnsupportedAlgorithm,
        x509.ExtensionNotFound,
    ):
        return Disqualified.DAMAGED
    if (
        not isinstance(key, ed25519.Ed25519PrivateKey)
        or key.public_key() != leaf.public_key()
    ):
        return Disqualified.KEY_MISMATCH
    if sans.value.get_values_for_type(x509.DNSName) != names:
        return Disqualified.NAMES_CHANGED
    if leaf.not_valid_before_utc > now:
        return Disqualified.NOT_YET_VALID
    return leaf


def mint_leaf(
    names: list[str],
    intermediate: x509.Certificate,
    intermediate_key: ed25519.Ed25519PrivateKey,
    now: datetime,
) -> LeafPair:
    if intermediate_key.public_key() != intermediate.public_key():
        raise ValueError("intermediate key does not match intermediate certificate")
    key = ed25519.Ed25519PrivateKey.generate()
    # The authority identifier is the intermediate's recorded SKI, never one
    # derived from its key: retained CAs from earlier producers use another
    # SKI method.
    authority = intermediate.extensions.get_extension_for_class(
        x509.SubjectKeyIdentifier
    ).value
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(intermediate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + _VALIDITY)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(authority),
            False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in names]), False
        )
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(intermediate_key, algorithm=None)
    )
    return LeafPair(
        certificate=leaf.public_bytes(serialization.Encoding.PEM)
        + intermediate.public_bytes(serialization.Encoding.PEM),
        key=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def served_leaf(
    *,
    installer: LeafPair,
    staged: LeafPair | None,
    intermediate_certificate: bytes,
    intermediate_key: Callable[[], ed25519.Ed25519PrivateKey],
    now: datetime,
) -> LeafPair | None:
    """The pair to stage, or ``None`` when the staged pair stays served.

    ``intermediate_key`` is called only to mint, because decrypting the
    intermediate is deliberately slow.
    """
    intermediate = x509.load_pem_x509_certificate(intermediate_certificate)
    names = installer_names(installer.certificate)
    newest: tuple[LeafPair, x509.Certificate] | None = None
    # Staged first: on a tie (the staged pair is the installer's copy) it stays.
    for pair in (candidate for candidate in (staged, installer) if candidate):
        leaf = _qualified(pair, intermediate, names, now)
        if isinstance(leaf, Disqualified):
            _LOGGER.info("a controller leaf is not served: %s", leaf.value)
        elif newest is None or leaf.not_valid_after_utc > newest[1].not_valid_after_utc:
            newest = (pair, leaf)
    if newest is not None and newest[1].not_valid_after_utc - now > RENEWAL_WINDOW:
        return None if newest[0] is staged else newest[0]
    return mint_leaf(names, intermediate, intermediate_key(), now)
