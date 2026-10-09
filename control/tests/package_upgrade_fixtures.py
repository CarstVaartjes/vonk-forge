"""Current source-bound upgrade and acknowledgement fixtures."""

from vonk_agent_protocol.package_upgrade import (
    PackageActivationOutcome,
    PackageActivationPhase,
    PackageActivationReceipt,
    PackageRollbackAuthority,
)


def rollback_authority(*, source_binary="f" * 64, deadline=2_000_000_000):
    return PackageRollbackAuthority.model_validate(
        {
            "source": {
                "package_sha256": "7" * 64,
                "package_signature": "8" * 128,
                "package_version": "0.1.0",
                "binary_sha256": source_binary,
                "helper_sha256": "6" * 64,
            },
            "attempt_nonce": "9" * 64,
            "activation_deadline": deadline,
        }
    ).model_dump(mode="json")


def source_transport(*, source_binary="f" * 64):
    return {
        "rollback": rollback_authority(source_binary=source_binary),
        "source_package_bytes": 1000,
        "source_package_url": "https://install.vonkforge.ai/artifacts/agent-packages/"
        + "7" * 64
        + "/vonk-forge-agent.deb",
    }


def activation_receipt(
    payload, node_id, *, now=100, phase=PackageActivationPhase.ACKNOWLEDGED
):
    source = payload["rollback"]["source"]
    return PackageActivationReceipt.model_validate(
        {
            "schema_version": 2,
            "node_id": node_id,
            "source_package_sha256": source["package_sha256"],
            "source_version": source["package_version"],
            "source_binary_sha256": source["binary_sha256"],
            "candidate_package_sha256": payload["package_sha256"],
            "candidate_version": payload["package_version"],
            "candidate_binary_sha256": payload["target_binary_digest"],
            "attempt_nonce": payload["rollback"]["attempt_nonce"],
            "phase": phase,
            "created_at": now,
            "updated_at": now,
            "outcome": PackageActivationOutcome.CONTROLLER_CONFIRMED_ACTIVATION
            if phase == PackageActivationPhase.ACKNOWLEDGED
            else PackageActivationOutcome.AWAITING_CONTROLLER_ACTIVATION,
        }
    ).model_dump(mode="json")
