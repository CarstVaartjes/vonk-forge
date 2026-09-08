"""Match root package activation evidence against one durable upgrade authority."""
from vonk_agent_protocol.contracts import AgentUpgradePayload
from vonk_agent_protocol.package_upgrade import PackageActivationReceipt


def matches_receipt(receipt: PackageActivationReceipt, payload: AgentUpgradePayload, node_id: str) -> bool:
    return (
        receipt.node_id == node_id
        and receipt.attempt_nonce == payload.rollback.attempt_nonce
        and receipt.source_package_sha256 == payload.rollback.source.package_sha256
        and receipt.source_version == payload.rollback.source.package_version
        and receipt.source_binary_sha256 == payload.rollback.source.binary_sha256
        and receipt.candidate_package_sha256 == payload.package_sha256
        and receipt.candidate_version == payload.package_version
        and receipt.candidate_binary_sha256 == payload.target_binary_digest
        and receipt.created_at <= payload.rollback.activation_deadline
        and (receipt.phase != "acknowledged" or receipt.updated_at <= payload.rollback.activation_deadline)
    )
