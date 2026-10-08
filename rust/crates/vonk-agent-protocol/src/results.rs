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
            _ => None,
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
            _ => false,
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
mod agent_result_binding_tests {
    use super::*;
    use crate::generated::{AgentOperation, AgentResultState};

    fn result(state: AgentResultState, body: Value) -> AgentResult {
        AgentResult {
            fence: Uuid::new_v4(),
            result: serde_json::from_value(body).unwrap(),
            state,
        }
    }

    #[test]
    fn retired_unknown_result_state_is_adopted_without_being_written() {
        let old = serde_json::json!({
            "fence": "00000000-0000-4000-8000-000000000001",
            "state": "waiting-for-operator",
            "result": {"reason": "legacy receipt"}
        });
        let adopted: AgentResult = serde_json::from_value(old).unwrap();
        assert_eq!(adopted.state, AgentResultState::Observing);
        assert_eq!(serde_json::to_value(adopted).unwrap()["state"], "observing");
    }

    fn typed(state: AgentResultState, body: Value) -> AgentResult {
        result(state, body)
    }

    #[test]
    fn typed_outcome_is_bound_to_its_state_and_operation() {
        let done = typed(
            AgentResultState::Succeeded,
            serde_json::json!({"kind": "done", "result": {"installed_bytes": 3}}),
        );
        done.validate_for_operation(&AgentOperation::RecipeInstall)
            .unwrap();
        assert!(
            done.validate_for_operation(&AgentOperation::RecipeStop)
                .is_err()
        );

        let unknown = serde_json::json!({
            "kind": generated::OutcomeUnknownKind::Unknown,
            "wait_reason": "stop-unconfirmed",
            "reason": "workload stop remains unconfirmed",
        });
        typed(AgentResultState::Observing, unknown.clone())
            .validate_for_operation(&AgentOperation::RecipeStop)
            .unwrap();
        for wrong in [
            AgentResultState::Failed,
            AgentResultState::Cancelled,
            AgentResultState::Succeeded,
        ] {
            assert!(
                typed(wrong, unknown.clone())
                    .validate_for_operation(&AgentOperation::RecipeStop)
                    .is_err()
            );
        }

        let cancelled = serde_json::json!({
            "kind": generated::OutcomeFailedKind::Failed,
            "code": "operation_cancelled",
            "reason": "controller cancellation confirmed after exact workload stop",
        });
        typed(AgentResultState::Cancelled, cancelled.clone())
            .validate_for_operation(&AgentOperation::RecipeStart)
            .unwrap();
        assert!(
            typed(AgentResultState::Failed, cancelled)
                .validate_for_operation(&AgentOperation::RecipeStart)
                .is_err()
        );
    }

    #[test]
    fn a_job_receipt_belongs_to_the_recipe_job_operation_only() {
        let receipt: Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
        ))
        .unwrap();
        let mut receipt = receipt["result"].clone();
        receipt["exit_code"] = serde_json::json!(1);
        let failed = typed(
            AgentResultState::Failed,
            serde_json::json!({
                "kind": generated::OutcomeFailedKind::Failed,
                "code": "recipe_job_run_failed",
                "reason": "runtime failed",
                "receipt": receipt,
            }),
        );
        failed
            .validate_for_operation(&AgentOperation::RecipeJobRunV1)
            .unwrap();
        assert!(
            failed
                .validate_for_operation(&AgentOperation::RecipeStop)
                .is_err()
        );
    }

    #[test]
    fn succeeded_result_is_bound_to_its_current_operation() {
        let stop = result(AgentResultState::Succeeded, serde_json::json!({}));
        stop.validate_for_operation(&AgentOperation::RecipeStop)
            .unwrap();
        assert!(
            stop.validate_for_operation(&AgentOperation::RecipeInstall)
                .is_err()
        );

        let install = result(
            AgentResultState::Succeeded,
            serde_json::json!({"installed_bytes": 0}),
        );
        install
            .validate_for_operation(&AgentOperation::RecipeInstall)
            .unwrap();
        assert!(
            install
                .validate_for_operation(&AgentOperation::RecipeStop)
                .is_err()
        );
    }

    #[test]
    fn failure_result_retains_optional_fields_but_requires_failure_identity() {
        let failure = result(
            AgentResultState::Observing,
            serde_json::json!({"reason": "operator review required"}),
        );
        failure
            .validate_for_operation(&AgentOperation::RecipeStop)
            .unwrap();

        let empty = result(AgentResultState::Failed, serde_json::json!({}));
        assert!(
            empty
                .validate_for_operation(&AgentOperation::RecipeStop)
                .is_err()
        );
        assert!(
            failure
                .validate_for_operation(&AgentOperation::RecipeJobRunV1)
                .is_ok()
        );
        assert!(
            failure
                .validate_for_operation(&AgentOperation::RecipeStop)
                .is_ok()
        );
    }

    #[test]
    fn process_result_is_valid_only_for_the_recipe_job_operation() {
        let mut job: AgentResult = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
        ))
        .unwrap();
        job.state = AgentResultState::Failed;
        let generated::AgentResultResult::RecipeJobRunResult(body) = &mut job.result else {
            panic!("expected typed job result")
        };
        body.exit_code = 1;
        body.reason = Some("runtime failed".to_owned());

        job.validate_for_operation(&AgentOperation::RecipeJobRunV1)
            .unwrap();
        assert!(
            job.validate_for_operation(&AgentOperation::RecipeStop)
                .is_err()
        );
        let generated::AgentResultResult::RecipeJobRunResult(body) = &mut job.result else {
            panic!("expected typed job result")
        };
        body.exit_code = 0;
        assert!(
            job.validate_for_operation(&AgentOperation::RecipeJobRunV1)
                .is_err()
        );
    }
}
