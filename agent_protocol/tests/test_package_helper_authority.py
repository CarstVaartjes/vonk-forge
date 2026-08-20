from __future__ import annotations

from dataclasses import replace

import pytest
from vonk_agent_protocol import AgentProtocolError
from vonk_agent_protocol.host_helper import (
    HOST_HELPER_GRANT_DOMAIN,
    HostHelperGrantClaims,
    HostHelperOperation,
    HostOperationKind,
    host_helper_grant_signing_bytes,
)
from vonk_agent_protocol.workload_packages import (
    PackageHelperGrantClaims,
    PackageHelperOperation,
    PackageHelperSignature,
    PackageObjectReceiptClaims,
    SignedPackageHelperGrant,
    SignedPackageObjectReceipt,
    package_helper_grant_signing_bytes,
    package_object_receipt_signing_bytes,
)

KEY_ID = "a" * 64
SIGNATURE = "b" * 128
REQUEST_ID = "10000000-0000-4000-8000-000000000001"
JOB_ID = "20000000-0000-4000-8000-000000000002"
OPERATION_ID = "30000000-0000-4000-8000-000000000003"
FENCE = "40000000-0000-4000-8000-000000000004"
RELEASE = "c" * 64
REQUEST_DIGEST = "d" * 64


def grant_claims() -> PackageHelperGrantClaims:
    return PackageHelperGrantClaims(
        schema_version=1,
        authority="vonk.workload-package-helper",
        request_id=REQUEST_ID,
        node_id="spk_" + "1" * 32,
        job_id=JOB_ID,
        operation_id=OPERATION_ID,
        attempt=2,
        fence=FENCE,
        release_digest=RELEASE,
        generation="gen-future-stack-001",
        operation=PackageHelperOperation.VERIFY_RELEASE,
        request_digest=REQUEST_DIGEST,
        issued_at=2_000_000_000,
        expires_at=2_000_000_900,
    )


def receipt_claims() -> PackageObjectReceiptClaims:
    return PackageObjectReceiptClaims(
        schema_version=1,
        authority="vonk.workload-package-helper",
        object_digest="e" * 64,
        size=4096,
        relative_name=f"objects/sha256/{'e' * 64}",
    )


def test_helper_grant_round_trips_exact_typed_claims() -> None:
    grant = SignedPackageHelperGrant(
        claims=grant_claims(),
        signature=PackageHelperSignature("ed25519", KEY_ID, SIGNATURE),
    )

    parsed = SignedPackageHelperGrant.parse(grant.to_mapping())

    assert parsed == grant
    assert parsed.claims.operation is PackageHelperOperation.VERIFY_RELEASE
    assert parsed.claims.request_digest == REQUEST_DIGEST


def test_helper_grant_signing_bytes_are_domain_separated_and_canonical() -> None:
    encoded = package_helper_grant_signing_bytes(grant_claims())

    assert encoded == (
        b"Vonk Forge-WORKLOAD-PACKAGE-HELPER-GRANT-V1\x00"
        b'{"attempt":2,"authority":"vonk.workload-package-helper",'
        b'"expires_at":2000000900,"fence":"40000000-0000-4000-8000-000000000004",'
        b'"generation":"gen-future-stack-001",'
        b'"issued_at":2000000000,"job_id":"20000000-0000-4000-8000-000000000002",'
        b'"node_id":"spk_11111111111111111111111111111111",'
        b'"operation":"verify-release",'
        b'"operation_id":"30000000-0000-4000-8000-000000000003",'
        b'"release_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"request_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        b'"request_id":"10000000-0000-4000-8000-000000000001",'
        b'"schema_version":1}'
    )


@pytest.mark.parametrize(
    "operation", ("agent.update", "agent.rollback", "platform.update")
)
def test_helper_grant_cannot_represent_agent_or_platform_updates(
    operation: str,
) -> None:
    document = grant_claims().to_mapping()
    document["operation"] = operation

    with pytest.raises(AgentProtocolError, match="helper operation"):
        PackageHelperGrantClaims.parse(document)


def test_helper_grant_requires_a_bounded_fifteen_minute_lifetime() -> None:
    with pytest.raises(AgentProtocolError, match="expiry"):
        replace(grant_claims(), expires_at=2_000_000_901)
    with pytest.raises(AgentProtocolError, match="expiry"):
        replace(grant_claims(), expires_at=2_000_000_000)


def test_object_receipt_has_explicit_same_key_envelope_but_distinct_domain() -> None:
    receipt = SignedPackageObjectReceipt(
        claims=receipt_claims(),
        signature=PackageHelperSignature("ed25519", KEY_ID, SIGNATURE),
    )

    parsed = SignedPackageObjectReceipt.parse(receipt.to_mapping())

    assert parsed == receipt
    assert parsed.claims.relative_name == "objects/sha256/" + "e" * 64
    assert package_object_receipt_signing_bytes(parsed.claims).startswith(
        b"Vonk Forge-WORKLOAD-PACKAGE-OBJECT-RECEIPT-V1\x00"
    )
    assert package_object_receipt_signing_bytes(
        parsed.claims
    ) != package_helper_grant_signing_bytes(grant_claims())


def test_object_receipt_rejects_a_noncanonical_object_location() -> None:
    with pytest.raises(AgentProtocolError, match="relative name"):
        replace(receipt_claims(), relative_name="objects/sha256/" + "f" * 64)


def test_host_helper_grant_has_a_distinct_narrow_authority_domain() -> None:
    claims = HostHelperGrantClaims(
        schema_version=1,
        authority="vonk.host-maintenance-helper",
        request_id=REQUEST_ID,
        node_id="spk_" + "1" * 32,
        issued_at=2_000_000_000,
        expires_at=2_000_000_060,
        operation=HostHelperOperation(
            HostOperationKind.RESTART_VONK_UNIT, {"unit": "agent"}
        ),
    )

    encoded = host_helper_grant_signing_bytes(claims)

    assert encoded.startswith(HOST_HELPER_GRANT_DOMAIN)
    assert not encoded.startswith(b"Vonk Forge-WORKLOAD-PACKAGE-HELPER-GRANT-V1\x00")


@pytest.mark.parametrize(
    "document",
    (
        {
            "type": "create-managed-directory",
            "area": "models",
            "relative_path": "../etc",
        },
        {"type": "restart-vonk-unit", "unit": "sshd"},
        {
            "type": "restart-vonk-unit",
            "unit": "agent",
            "executable": "/bin/sh",
        },
        {
            "type": "install-vonk-deb",
            "package_sha256": "a" * 64,
            "package_signature": "b" * 128,
            "arguments": ["--force"],
        },
        {"type": "schedule-reboot", "delay_seconds": 5},
    ),
)
def test_host_helper_protocol_rejects_paths_and_untyped_process_control(
    document: dict[str, object],
) -> None:
    with pytest.raises(AgentProtocolError):
        HostHelperOperation.parse(document)


@pytest.mark.parametrize(
    "document",
    (
        {
            "type": "activate-agent-slot",
            "slot": "a",
            "artifact_sha256": "a" * 64,
            "artifact_signature": "b" * 128,
        },
        {"type": "restart-vonk-unit", "unit": "supervisor"},
    ),
)
def test_host_helper_protocol_rejects_removed_agent_lifecycle_operations(
    document: dict[str, object],
) -> None:
    with pytest.raises(AgentProtocolError, match="operation|unit"):
        HostHelperOperation.parse(document)
