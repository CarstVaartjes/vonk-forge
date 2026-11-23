//! Results.

use super::*;

impl AgentResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        // The state is the closed `AgentResultState`, so no other word can reach
        // here. A typed outcome decides its own state word; the legacy state field
        // must say the same thing, so the two cannot drift.
        if let Some(state) = self.result.outcome_state()
            && state != self.state
        {
            return Err(ProtocolError::Identity("result state"));
        }
        Ok(())
    }

    /// Validate the terminal result against the operation carried by its
    /// authoritative claim. The wire envelope intentionally has no duplicate
    /// operation discriminator, so callers must supply the stored operation.
    #[expect(
        clippy::wildcard_enum_match_arm,
        reason = "every other error passes through unchanged"
    )]
    pub fn validate_for_operation(
        &self,
        operation: &generated::AgentOperation,
    ) -> Result<(), ProtocolError> {
        use generated::{AgentOperation, AgentResultResult, AgentResultState};

        self.validate()?;
        let matches = match &self.result {
            AgentResultResult::OutcomeDone(done) => {
                success_matches(operation, &AgentResultResult::from(done.result.clone()))?
            }
            AgentResultResult::OutcomeFailed(failed) => match &failed.receipt {
                None => true,
                Some(receipt) => {
                    *operation == AgentOperation::RecipeJobRunV1 && {
                        receipt.validate()?;
                        self.state == AgentResultState::Cancelled || receipt.exit_code != 0
                    }
                }
            },
            AgentResultResult::OutcomeUnknown(unknown) => match &unknown.receipt {
                None => true,
                Some(receipt) => {
                    *operation == AgentOperation::RecipeJobRunV1 && {
                        receipt.validate()?;
                        true
                    }
                }
            },
            body => match self.state {
                AgentResultState::Succeeded => success_matches(operation, body)?,
                AgentResultState::Failed => match (body, operation) {
                    (AgentResultResult::AgentFailureResult(result), _) => {
                        result.reason.is_some() || result.error_code.is_some()
                    }
                    (
                        AgentResultResult::RecipeJobRunResult(result),
                        AgentOperation::RecipeJobRunV1,
                    ) => {
                        result.validate()?;
                        result.exit_code != 0
                    }
                    _ => false,
                },
                AgentResultState::Cancelled | AgentResultState::Observing => {
                    match (body, operation) {
                        (AgentResultResult::AgentFailureResult(result), _) => {
                            result.reason.is_some() || result.error_code.is_some()
                        }
                        (
                            AgentResultResult::RecipeJobRunResult(result),
                            AgentOperation::RecipeJobRunV1,
                        ) => {
                            result.validate()?;
                            true
                        }
                        _ => false,
                    }
                }
            },
        };
        if matches {
            Ok(())
        } else {
            Err(ProtocolError::Identity("result operation"))
        }
    }
}

/// Whether a success body is the one the operation reports.
#[expect(
    clippy::trivially_copy_pass_by_ref,
    reason = "borrowed to match the sibling signatures that take the operation by reference"
)]
pub(super) fn success_matches(
    operation: &generated::AgentOperation,
    body: &generated::AgentResultResult,
) -> Result<bool, ProtocolError> {
    use generated::{AgentOperation, AgentResultResult};

    Ok(match operation {
        AgentOperation::RuntimePreflightV1 => {
            let AgentResultResult::RuntimePreflightResult(result) = body else {
                return Err(ProtocolError::Identity("result operation"));
            };
            result.validate()?;
            true
        }
        AgentOperation::AgentUpgradeV1 => {
            matches!(body, AgentResultResult::AgentUpgradeResult(_))
        }
        AgentOperation::ArtifactDistributionV1 => {
            matches!(body, AgentResultResult::ArtifactDistributionResult(_))
        }
        // An empty success deserializes into the first empty-capable
        // variant, so it is recognized by content, not by variant.
        AgentOperation::RecipeBuildCleanupV1 => empty_result(body),
        AgentOperation::RecipeBuildV1 => {
            matches!(body, AgentResultResult::RecipeBuildEvidence(_))
        }
        AgentOperation::RecipeInstall => {
            matches!(body, AgentResultResult::AgentInstallResult(_))
        }
        // Stop, uninstall and reconcile succeed with `{}`, which an
        // untagged parse reads as the first empty variant.
        AgentOperation::RecipeStart
        | AgentOperation::RecipeStop
        | AgentOperation::RecipeUninstall
        | AgentOperation::RecipeReconcile => {
            body.is_empty_success()
                || (*operation == AgentOperation::RecipeStart
                    && matches!(body, AgentResultResult::RecipeStartResult(_)))
        }
        AgentOperation::RecipeJobRunV1 => {
            let AgentResultResult::RecipeJobRunResult(result) = body else {
                return Err(ProtocolError::Identity("result operation"));
            };
            result.validate()?;
            true
        }
    })
}

impl generated::AgentResultResult {
    /// The state word a typed outcome reports under, or `None` for a legacy body.
    pub fn outcome_state(&self) -> Option<generated::AgentResultState> {
        use generated::{AgentResultResult, AgentResultState, FailureCode};

        match self {
            AgentResultResult::OutcomeDone(_) => Some(AgentResultState::Succeeded),
            AgentResultResult::OutcomeFailed(failed) => {
                Some(if failed.code == FailureCode::OperationCancelled {
                    AgentResultState::Cancelled
                } else {
                    AgentResultState::Failed
                })
            }
            AgentResultResult::OutcomeUnknown(_) => Some(AgentResultState::Observing),
            AgentResultResult::RuntimePreflightResult(_)
            | AgentResultResult::AgentInstallResult(_)
            | AgentResultResult::RecipeStartResult(_)
            | AgentResultResult::RecipeStopResult(_)
            | AgentResultResult::RecipeReconcileResult(_)
            | AgentResultResult::RecipeUninstallResult(_)
            | AgentResultResult::RecipeBuildEvidence(_)
            | AgentResultResult::RecipeBuildCleanupEvidence(_)
            | AgentResultResult::RecipeJobRunResult(_)
            | AgentResultResult::ArtifactDistributionResult(_)
            | AgentResultResult::AgentFailureResult(_)
            | AgentResultResult::AgentUpgradeResult(_) => None,
        }
    }

    /// Whether this is the empty `{}` success body of a stop, uninstall,
    /// reconcile or non-serving start.
    pub fn is_empty_success(&self) -> bool {
        match self {
            Self::RecipeStartResult(result) => result.endpoint.is_none(),
            Self::RecipeStopResult(_)
            | Self::RecipeUninstallResult(_)
            | Self::RecipeReconcileResult(_) => true,
            Self::RuntimePreflightResult(_)
            | Self::AgentInstallResult(_)
            | Self::RecipeBuildEvidence(_)
            | Self::RecipeBuildCleanupEvidence(_)
            | Self::RecipeJobRunResult(_)
            | Self::ArtifactDistributionResult(_)
            | Self::AgentFailureResult(_)
            | Self::AgentUpgradeResult(_)
            | Self::OutcomeDone(_)
            | Self::OutcomeFailed(_)
            | Self::OutcomeUnknown(_) => false,
        }
    }
}

impl From<generated::OutcomeDoneResult> for generated::AgentResultResult {
    fn from(value: generated::OutcomeDoneResult) -> Self {
        use generated::{AgentResultResult as To, OutcomeDoneResult as From};

        match value {
            From::RuntimePreflightResult(body) => To::RuntimePreflightResult(body),
            From::AgentInstallResult(body) => To::AgentInstallResult(body),
            From::RecipeStartResult(body) => To::RecipeStartResult(body),
            From::RecipeStopResult(body) => To::RecipeStopResult(body),
            From::RecipeReconcileResult(body) => To::RecipeReconcileResult(body),
            From::RecipeUninstallResult(body) => To::RecipeUninstallResult(body),
            From::RecipeBuildEvidence(body) => To::RecipeBuildEvidence(body),
            From::RecipeBuildCleanupEvidence(body) => To::RecipeBuildCleanupEvidence(body),
            From::RecipeJobRunResult(body) => To::RecipeJobRunResult(body),
            From::ArtifactDistributionResult(body) => To::ArtifactDistributionResult(body),
            From::AgentUpgradeResult(body) => To::AgentUpgradeResult(body),
        }
    }
}

#[cfg(test)]
mod agent_result_binding_tests;
