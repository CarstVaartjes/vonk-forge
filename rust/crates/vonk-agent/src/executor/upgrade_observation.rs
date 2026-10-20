//! Observe the existing privileged activation transaction; never install or
//! start a second rollback. The helper's durable watcher owns that transaction.
use super::operation_observation::{OperationObservation, PendingObservation};
use super::*;
use vonk_agent_protocol::generated::{
    AgentRuntimeIdentityArchitecture, AgentUpgradeResult, AgentUpgradeStatus,
};
use vonk_agent_protocol::{AgentUpgradeRequest, PackageActivationPhase};

pub(super) struct UpgradeObservation<'a> {
    pub node_id: &'a str,
    /// Collected from the running executable and admitted after its self-test.
    pub identity: &'a AgentRuntimeIdentity,
    pub receipt: Result<
        Option<vonk_agent_protocol::PackageActivationReceipt>,
        crate::agent_upgrade::AgentUpgradeError,
    >,
}

pub(super) fn observe_upgrade(
    claim: &AgentClaim,
    context: Option<UpgradeObservation<'_>>,
) -> OperationObservation {
    let pending = |reason| {
        OperationObservation::Unknown(PendingObservation::new(
            claim,
            WaitReason::AgentUpgradeAwaitingIdentity,
            reason,
        ))
    };
    let request = match AgentUpgradeRequest::parse(claim) {
        Ok(request) => request,
        Err(error) => {
            return OperationObservation::confirmed(ExecutionResult::Failed(
                Failure::new("upgrade observation claim is invalid")
                    .stage(FailureStage::AgentRestart)
                    .diagnostic(error.to_string()),
            ));
        }
    };
    let Some(context) = context else {
        return pending("running agent identity is unavailable to the package observer".to_owned());
    };
    let receipt = match context.receipt {
        Ok(Some(receipt)) => receipt,
        Ok(None) => {
            return pending(
                "signed package activation receipt is absent; no installation is inferred"
                    .to_owned(),
            );
        }
        Err(error) => {
            let cause = match &error {
                crate::agent_upgrade::AgentUpgradeError::Io(cause) => {
                    format!("{error}: {:?}", cause.kind())
                }
                _ => error.to_string(),
            };
            return OperationObservation::Unknown(PendingObservation::new(
                claim,
                WaitReason::ObservationUnavailable,
                format!("package activation receipt observation unavailable: {cause}"),
            ));
        }
    };
    // Bind the complete captured transaction, including its deadline. A stale
    // receipt is neither this fence's success nor permission to reinstall.
    if receipt.node_id != context.node_id
        || receipt.attempt_nonce != request.rollback.attempt_nonce
        || receipt.source_package_sha256 != request.rollback.source.package_sha256
        || receipt.source_version != request.rollback.source.package_version
        || receipt.source_binary_sha256 != request.rollback.source.binary_sha256
        || receipt.candidate_package_sha256 != request.package_sha256
        || receipt.candidate_version != request.package_version
        || receipt.candidate_binary_sha256 != request.target_binary_digest
        || receipt.created_at > request.rollback.activation_deadline
        || (receipt.phase == PackageActivationPhase::Acknowledged
            && receipt.updated_at > request.rollback.activation_deadline)
    {
        return pending(
            "activation receipt does not identify this exact accepted package transaction"
                .to_owned(),
        );
    }
    match receipt.phase {
        PackageActivationPhase::Acknowledged => {
            if context.identity.architecture != AgentRuntimeIdentityArchitecture::LinuxArm64
                || context.identity.build_digest != request.target_build_digest
                || context.identity.binary_digest != request.target_binary_digest
            {
                return pending(
                    "acknowledged package has not been observed as this running build and binary"
                        .to_owned(),
                );
            }
            OperationObservation::confirmed(ExecutionResult::done(AgentUpgradeResult {
                architecture: request.architecture,
                binary_digest: context.identity.binary_digest.clone(),
                build_digest: context.identity.build_digest.clone(),
                package_sha256: request.package_sha256,
                package_version: request.package_version,
                status: AgentUpgradeStatus::Upgraded,
                activation_receipt: receipt,
            }))
        }
        PackageActivationPhase::RolledBack => {
            if context.identity.binary_digest != request.rollback.source.binary_sha256 {
                return pending(
                    "rollback receipt exists but the captured source binary is not running"
                        .to_owned(),
                );
            }
            OperationObservation::confirmed(ExecutionResult::Failed(
                Failure::new("package activation rolled back to the captured source")
                    .stage(FailureStage::AgentUpgradeInstalled)
                    .package_activation(receipt),
            ))
        }
        PackageActivationPhase::Armed
        | PackageActivationPhase::ActivationFailed
        | PackageActivationPhase::RollingBack
        | PackageActivationPhase::RollbackFailed => pending(format!(
            "package transaction phase={} outcome={}; the existing activation/rollback transaction owns recovery",
            receipt.phase, receipt.outcome,
        )),
    }
}
