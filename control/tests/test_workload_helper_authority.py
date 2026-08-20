from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.workload_packages import (
    PACKAGE_HELPER_AUTHORITY,
    PackageHelperOperation,
    package_helper_grant_signing_bytes,
    package_object_receipt_signing_bytes,
)
from vonk_control.workload_helper_authority import (
    WorkloadHelperAuthorityError,
    WorkloadHelperGrantIssuer,
    WorkloadObjectReceiptIssuer,
)

NOW = datetime(2033, 5, 18, 12, 0, tzinfo=UTC)
REQUEST_ID = "10000000-0000-4000-8000-000000000001"


def grant_issuer() -> WorkloadHelperGrantIssuer:
    return WorkloadHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(b"h" * 32),
        clock=lambda: NOW,
        request_id_factory=lambda: REQUEST_ID,
    )


def receipt_issuer() -> WorkloadObjectReceiptIssuer:
    return WorkloadObjectReceiptIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(b"r" * 32)
    )


def test_issuer_signs_exact_workload_helper_binding_with_distinct_key_purpose() -> None:
    authority = grant_issuer()

    grant = authority.issue_grant(
        node_id="spk_" + "1" * 32,
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=3,
        fence="40000000-0000-4000-8000-000000000004",
        release_digest="a" * 64,
        generation="gen-future-stack-001",
        operation=PackageHelperOperation.START,
        request_digest="b" * 64,
        expires_in_seconds=900,
    )

    assert grant.claims.request_id == REQUEST_ID
    assert grant.claims.node_id == "spk_" + "1" * 32
    assert grant.claims.operation is PackageHelperOperation.START
    assert grant.claims.issued_at == int(NOW.timestamp())
    assert grant.claims.expires_at == int(NOW.timestamp()) + 900
    assert grant.signature.key_id == authority.key_id
    authority.public_key.verify(
        bytes.fromhex(grant.signature.value),
        package_helper_grant_signing_bytes(grant.claims),
    )
    assert authority.public_key_document() == {
        "algorithm": "ed25519",
        "authority": PACKAGE_HELPER_AUTHORITY,
        "key_id": authority.key_id,
        "public_key": authority.public_key_bytes.hex(),
        "schema_version": 1,
        "usage": "grant",
    }


def test_issuer_accepts_the_request_identity_already_bound_into_the_body_digest() -> None:
    authority = grant_issuer()
    request_id = "50000000-0000-4000-8000-000000000005"

    grant = authority.issue_grant(
        request_id=request_id,
        node_id="spk_" + "1" * 32,
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=1,
        fence="40000000-0000-4000-8000-000000000004",
        release_digest="a" * 64,
        generation="gen-future-stack-001",
        operation=PackageHelperOperation.PREPARE,
        request_digest="b" * 64,
        expires_in_seconds=60,
    )

    assert grant.claims.request_id == request_id


def test_grant_signature_cannot_be_reused_as_an_undomained_or_update_signature() -> None:
    authority = grant_issuer()
    grant = authority.issue_grant(
        node_id="spk_" + "1" * 32,
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=1,
        fence="40000000-0000-4000-8000-000000000004",
        release_digest="a" * 64,
        generation="gen-future-stack-001",
        operation=PackageHelperOperation.HEALTH,
        request_digest="b" * 64,
        expires_in_seconds=30,
    )

    with pytest.raises(InvalidSignature):
        authority.public_key.verify(
            bytes.fromhex(grant.signature.value), canonical_message(grant.claims)
        )


@pytest.mark.parametrize("seconds", (0, 901, True))
def test_issuer_rejects_unbounded_grant_expiry(seconds: object) -> None:
    with pytest.raises(WorkloadHelperAuthorityError, match="expiry"):
        grant_issuer().issue_grant(
            node_id="spk_" + "1" * 32,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=1,
            fence="40000000-0000-4000-8000-000000000004",
            release_digest="a" * 64,
            generation="gen-future-stack-001",
            operation=PackageHelperOperation.VERIFY,
            request_digest="b" * 64,
            expires_in_seconds=seconds,
        )


def test_issuer_refuses_non_workload_operation_even_when_string_shaped() -> None:
    with pytest.raises(WorkloadHelperAuthorityError, match="operation"):
        grant_issuer().issue_grant(
            node_id="spk_" + "1" * 32,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=1,
            fence="40000000-0000-4000-8000-000000000004",
            release_digest="a" * 64,
            generation="gen-future-stack-001",
            operation="node.probe",
            request_digest="b" * 64,
            expires_in_seconds=30,
        )


def test_issuer_defines_object_receipt_authority_with_a_separate_signature_domain() -> None:
    authority = receipt_issuer()

    receipt = authority.issue_object_receipt(object_digest="e" * 64, size=8192)

    assert receipt.claims.relative_name == "objects/sha256/" + "e" * 64
    assert receipt.signature.key_id == authority.key_id
    authority.public_key.verify(
        bytes.fromhex(receipt.signature.value),
        package_object_receipt_signing_bytes(receipt.claims),
    )
    with pytest.raises(InvalidSignature):
        authority.public_key.verify(
            bytes.fromhex(receipt.signature.value),
            b"Vonk Forge-WORKLOAD-PACKAGE-HELPER-GRANT-V1\x00"
            + canonical_message(receipt.claims),
        )
    assert authority.public_key_document() == {
        "algorithm": "ed25519",
        "authority": PACKAGE_HELPER_AUTHORITY,
        "key_id": authority.key_id,
        "public_key": authority.public_key_bytes.hex(),
        "schema_version": 1,
        "usage": "object-receipt",
    }
    assert authority.key_id != grant_issuer().key_id


def test_distinct_issuers_load_only_owner_private_ed25519_key_files(
    tmp_path,
) -> None:
    grant_path = tmp_path / "grant.pem"
    receipt_path = tmp_path / "receipt.pem"
    for path, raw in (
        (grant_path, b"g" * 32),
        (receipt_path, b"r" * 32),
    ):
        path.write_bytes(
            ed25519.Ed25519PrivateKey.from_private_bytes(raw).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        path.chmod(0o600)

    grant = WorkloadHelperGrantIssuer.from_private_key_file(
        grant_path, clock=lambda: NOW, request_id_factory=lambda: REQUEST_ID
    )
    receipt = WorkloadObjectReceiptIssuer.from_private_key_file(receipt_path)

    assert grant.key_id != receipt.key_id
    grant_path.chmod(0o640)
    with pytest.raises(WorkloadHelperAuthorityError, match="private key"):
        WorkloadHelperGrantIssuer.from_private_key_file(grant_path)
