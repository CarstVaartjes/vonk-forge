from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import PackageActivationReceipt, PackageRollbackAuthority
from vonk_agent_protocol.host_helper import (
    ConfirmPackageActivationOperation,
    InstallVonkDebOperation,
)


def authority() -> dict[str, object]:
    return {
        "source": {
            "package_sha256": "a" * 64,
            "package_signature": "b" * 128,
            "package_version": "0.1.1~acceptance.1",
            "binary_sha256": "c" * 64,
            "helper_sha256": "d" * 64,
        },
        "attempt_nonce": "e" * 64,
        "activation_deadline": 2_000_000_000,
    }


def test_signed_install_requires_captured_current_source_and_fixed_ack() -> None:
    operation = InstallVonkDebOperation.model_validate(
        {
            "type": "install-vonk-deb",
            "package_sha256": "f" * 64,
            "package_signature": "1" * 128,
            "rollback": authority(),
        }
    )
    assert operation.rollback.source.package_sha256 == "a" * 64
    assert (
        InstallVonkDebOperation.model_validate_json(operation.model_dump_json())
        == operation
    )
    with pytest.raises(ValidationError):
        InstallVonkDebOperation.model_validate(
            operation.model_dump(exclude={"rollback"})
        )
    acknowledgement = ConfirmPackageActivationOperation(
        type="confirm-package-activation",
        package_sha256="f" * 64,
        attempt_nonce="e" * 64,
    )
    with pytest.raises(ValidationError):
        ConfirmPackageActivationOperation.model_validate(
            acknowledgement.model_dump() | {"command": "/bin/sh"}
        )


@pytest.mark.parametrize("deadline", [True, 2_000_000_000.0, "2000000000"])
def test_source_authority_rejects_nested_scalar_coercion(deadline: object) -> None:
    payload = authority() | {"activation_deadline": deadline}
    with pytest.raises(ValidationError):
        PackageRollbackAuthority.model_validate(payload)
    with pytest.raises(ValidationError):
        PackageRollbackAuthority.model_validate_json(json.dumps(payload))


def test_source_authority_rejects_arbitrary_path_and_root_receipt_is_current() -> None:
    payload = authority()
    payload["source"]["path"] = "/tmp/older.deb"
    with pytest.raises(ValidationError):
        PackageRollbackAuthority.model_validate(payload)
    receipt = PackageActivationReceipt(
        schema_version=2,
        node_id="spk_" + "1" * 32,
        source_package_sha256="a" * 64,
        source_version="0.1.1~acceptance.1",
        source_binary_sha256="c" * 64,
        candidate_package_sha256="f" * 64,
        candidate_version="0.1.1~acceptance.2",
        candidate_binary_sha256="2" * 64,
        attempt_nonce="e" * 64,
        phase="rolled_back",
        created_at=100,
        updated_at=130,
        outcome="source_restored_and_restarted",
    )
    assert (
        PackageActivationReceipt.model_validate_json(receipt.model_dump_json())
        == receipt
    )
    with pytest.raises(ValidationError):
        PackageActivationReceipt.model_validate(
            receipt.model_dump() | {"schema_version": 1}
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_version", "../old.deb"),
        ("outcome", "shell output secret"),
        ("updated_at", 99),
    ],
)
def test_activation_receipt_rejects_unbounded_or_unordered_evidence(
    field: str, value: object
) -> None:
    receipt = {
        "schema_version": 2,
        "node_id": "spk_" + "1" * 32,
        "source_package_sha256": "a" * 64,
        "source_version": "0.1.1~acceptance.1",
        "source_binary_sha256": "c" * 64,
        "candidate_package_sha256": "f" * 64,
        "candidate_version": "0.1.1~acceptance.2",
        "candidate_binary_sha256": "2" * 64,
        "attempt_nonce": "e" * 64,
        "phase": "rolled_back",
        "created_at": 100,
        "updated_at": 130,
        "outcome": "source_restored_and_restarted",
    }
    receipt[field] = value
    with pytest.raises(ValidationError):
        PackageActivationReceipt.model_validate_json(json.dumps(receipt))
