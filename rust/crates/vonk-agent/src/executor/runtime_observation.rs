//! Runtime custody probes never start, upload, or renew an effect. The one
//! stop is a job's own deadline: with no execution waiting for it, the probe
//! holds that bound (`observe_running_job`). A measured job exit enters the
//! existing output ledger; the Controller can authorize delivery-only
//! recovery under a fresh fence.
use super::operation_observation::{OperationObservation, PendingObservation};
use super::*;
use crate::host_runtime::RuntimeReobservation;

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn observe_runtime(&self, claim: &AgentClaim) -> OperationObservation {
        let pending = |cause, reason| {
            OperationObservation::Unknown(PendingObservation::new(claim, cause, reason))
        };
        let root = self.runtime_root.join("runtime-requests");
        let boundary = HostRuntimeBoundary {
            client: self.client,
            request_root: &root,
            helper_socket: self.helper_socket,
        };
        let request = match RecipeOperationRequest::parse(claim) {
            Ok(request) => request,
            Err(error) => {
                return OperationObservation::confirmed(failed_stage_owned(
                    "runtime observation claim is invalid",
                    FailureStage::AgentRestart,
                    error.to_string(),
                ));
            }
        };
        let seen = match &request {
            RecipeOperationRequest::Start(start) => {
                let Some(stop) =
                    exact_stop_plan_from_claim(claim, &start.run_id.to_string(), false)
                else {
                    return OperationObservation::confirmed(failed(
                        "start observation identity is invalid",
                    ));
                };
                let seen = boundary.reobserve_runtime(&stop).await;
                if start.phase == Some(RecipeStartPhase::RankLaunch)
                    && matches!(&seen, Ok(RuntimeReobservation::Running { .. }))
                {
                    tokio::time::sleep(RANK_STABILITY_WINDOW).await;
                    boundary.reobserve_runtime(&stop).await
                } else {
                    seen
                }
            }
            RecipeOperationRequest::Stop(stop) => boundary.reobserve_stop(stop).await,
            RecipeOperationRequest::JobRun(job) => {
                if let Ok(Some(receipt)) = self.runtime.retained_job_completion(job) {
                    return self.observe_job_outputs(claim, job, receipt);
                }
                boundary.reobserve_job(job).await
            }
            _ => {
                return OperationObservation::confirmed(failed(
                    "claim payload does not match runtime observer",
                ));
            }
        };
        let seen = match seen {
            Ok(seen) => seen,
            Err(error) if refused_before_effect(&error) => {
                return OperationObservation::confirmed(runtime_failure(
                    "runtime observation refused the exact identity",
                    &error,
                ));
            }
            Err(error) => return pending(WaitReason::ObservationUnavailable, error.to_string()),
        };
        match (request, seen) {
            (RecipeOperationRequest::Stop(_), RuntimeReobservation::Absent) => {
                OperationObservation::confirmed(ExecutionResult::done(RecipeStopResult::default()))
            }
            (RecipeOperationRequest::Start(start), RuntimeReobservation::Running { .. }) => {
                if let Err(error) = self
                    .runtime
                    .observe_installation(&start.installation_id.to_string())
                {
                    return pending(WaitReason::ModelCustodyUnconfirmed, error.to_string());
                }
                if start.phase == Some(RecipeStartPhase::RankLaunch)
                    || match (
                        start.endpoint_address(),
                        start.port(),
                        start.compiled_execution_plan.endpoint.as_ref(),
                    ) {
                        (Some(address), Some(port), Some(endpoint)) => {
                            crate::health::readiness_once(address, port, &endpoint.health_path)
                                .await
                        }
                        _ => false,
                    }
                {
                    OperationObservation::confirmed(recipe_start_success(&start))
                } else {
                    OperationObservation::Progressing(PendingObservation::new(
                        claim,
                        WaitReason::RuntimeEffectUnconfirmed,
                        "exact runtime is running; readiness remains unconfirmed",
                    ))
                }
            }
            (RecipeOperationRequest::Start(_), RuntimeReobservation::Exited(outcome)) => {
                OperationObservation::confirmed(ExecutionResult::Failed(
                    Failure::new("exact start runtime exited")
                        .code(FailureCode::RecipeStartFailed)
                        .stage(FailureStage::RuntimeAdapter)
                        .diagnostic(
                            outcome
                                .diagnostic
                                .unwrap_or_else(|| format!("exit_code={:?}", outcome.exit_code)),
                        )
                        .process_logs(outcome.process_logs.map(|logs| *logs)),
                ))
            }
            (RecipeOperationRequest::JobRun(job), RuntimeReobservation::Exited(outcome)) => {
                self.observe_job_exit(claim, &job, outcome)
            }
            (
                RecipeOperationRequest::JobRun(job),
                RuntimeReobservation::Running { progressed_at },
            ) => self.observe_running_job(claim, &job, progressed_at).await,
            (_, RuntimeReobservation::Running { .. }) => {
                OperationObservation::Progressing(PendingObservation::new(
                    claim,
                    WaitReason::RuntimeEffectUnconfirmed,
                    "exact runtime is still running",
                ))
            }
            (_, RuntimeReobservation::Absent) => pending(
                WaitReason::RuntimeEffectUnconfirmed,
                "exact runtime is absent; no completion receipt was observed".to_owned(),
            ),
            (_, RuntimeReobservation::Exited(_)) => pending(
                WaitReason::StopUnconfirmed,
                "exact runtime exited but its removal remains unconfirmed".to_owned(),
            ),
        }
    }
}
