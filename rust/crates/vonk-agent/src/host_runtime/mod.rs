//! Narrow client for controller-authorized host container-runtime operations.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use thiserror::Error;
use vonk_agent_protocol::generated::HostHelperResponse as HelperResponse;
use vonk_agent_protocol::generated::{
    HelperErrorCode, HostHelperResponseStatus, RuntimePreflightFindingCode,
};
use vonk_agent_protocol::{
    AgentClaim, HostRuntimeAction, HostRuntimeRequest, HostRuntimeRequestRule, RecipeJobRunRequest,
    RecipeReconciliationIdentity, RecipeRunInspectionRequest, RecipeStartRequest,
    RecipeStopRequest, canonical_generated_json, canonical_json, hex_sha256, parse_strict,
};

use crate::client::{AgentHttpClient, ClientError};
use crate::failure_evidence::{FailureProcessLogs, sanitize_tail};

/// The frame ceiling is owned by the wire contract so the agent, the upgrade
/// channel and the privileged helper cannot drift.
const MAX_HELPER_MESSAGE_BYTES: usize = vonk_agent_protocol::MAX_HELPER_FRAME_BYTES;

/// The existing background observation concurrency wave belongs to native
/// helper work, not to futures that may be cancelled before that work finishes.
/// Foreground lifecycle/diagnostic requests do not wait on this pool.
pub(crate) const BACKGROUND_RUN_INSPECTION_CONCURRENCY: usize = 8;
fn background_inspection_slots() -> std::sync::Arc<tokio::sync::Semaphore> {
    static SLOTS: std::sync::OnceLock<std::sync::Arc<tokio::sync::Semaphore>> =
        std::sync::OnceLock::new();
    SLOTS
        .get_or_init(|| {
            std::sync::Arc::new(tokio::sync::Semaphore::new(
                BACKGROUND_RUN_INSPECTION_CONCURRENCY,
            ))
        })
        .clone()
}

mod errors;
mod responses;
mod transport;

pub use errors::{HelperProtocolCause, HostRuntimeError};
pub use responses::finding_word;
use responses::{require_bound_response, require_executed_outcome, runtime_rejection};
use transport::{call_helper, write_request};

pub struct HostRuntimeOutcome {
    pub exit_code: Option<i32>,
    pub stop_uncertain: bool,
    /// A one-shot job that did not exit cleanly: its exit account and the
    /// container's own output, captured by the helper before removal.
    pub diagnostic: Option<String>,
    pub process_logs: Option<Box<FailureProcessLogs>>,
}

/// What one read-only inspection of a managed run reported.
pub struct RunInspectionReport {
    pub running: bool,
    /// The running container's bounded, sanitized tail when it was requested.
    pub process_logs: Option<Box<FailureProcessLogs>>,
    /// Why a requested tail is missing.
    pub log_error: Option<String>,
}

/// The exact signed lifecycle plan carried beside a privileged runtime request.
/// Job runs use the parent's logical run identity while targeting the exact job
/// container; the typed plan keeps those identities distinct.
#[derive(Clone, Debug)]
pub enum HostRuntimePlan {
    Start(RecipeStartRequest),
    JobRun(RecipeJobRunRequest),
    Stop(RecipeStopRequest),
}

impl HostRuntimePlan {
    fn action(&self) -> HostRuntimeAction {
        match self {
            Self::Start(_) | Self::JobRun(_) => HostRuntimeAction::Start,
            Self::Stop(_) => HostRuntimeAction::Stop,
        }
    }
}

pub struct HostRuntimeBoundary<'a> {
    pub client: &'a AgentHttpClient,
    pub request_root: &'a Path,
    pub helper_socket: &'a Path,
}

struct RequestFileCleanup(PathBuf);

impl Drop for RequestFileCleanup {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

impl HostRuntimeBoundary<'_> {
    /// Ask the helper whether the exact container of one managed run is
    /// running.  The inspection is read-only, so it needs no Controller grant.
    pub async fn inspect_recipe_run(
        &self,
        arguments: Vec<String>,
    ) -> Result<bool, HostRuntimeError> {
        self.inspect_recipe_run_report(arguments, false)
            .await
            .map(|report| report.running)
    }

    pub async fn inspect_recipe_run_evidence_for_observation(
        &self,
        arguments: Vec<String>,
    ) -> Result<RunInspectionReport, HostRuntimeError> {
        let permit = background_inspection_slots()
            .acquire_owned()
            .await
            .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::HelperCallJoin))?;
        self.inspect_recipe_run_report_with_permit(arguments, false, Some(permit))
            .await
    }

    /// Like `inspect_recipe_run`, and when `include_logs` is set also reads the
    /// running container's bounded output without stopping it.
    pub async fn inspect_recipe_run_report(
        &self,
        arguments: Vec<String>,
        include_logs: bool,
    ) -> Result<RunInspectionReport, HostRuntimeError> {
        self.inspect_recipe_run_report_with_permit(arguments, include_logs, None)
            .await
    }

    async fn inspect_recipe_run_report_with_permit(
        &self,
        arguments: Vec<String>,
        include_logs: bool,
        permit: Option<tokio::sync::OwnedSemaphorePermit>,
    ) -> Result<RunInspectionReport, HostRuntimeError> {
        let request = HostRuntimeRequest {
            action: HostRuntimeAction::RunInspect,
            fence: uuid::Uuid::new_v4(),
            arguments,
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: None,
            start_plan: None,
            stop_plan: None,
        };
        request
            .validate()
            .map_err(HostRuntimeError::request_refusal)?;
        let body = canonical_json(&request)
            .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestEncoding))?;
        let digest = hex_sha256(&body);
        let request_path = write_request(self.request_root, &digest, &body)?;
        let request_cleanup = RequestFileCleanup(request_path);
        let request_id = uuid::Uuid::new_v4();
        let frame = canonical_generated_json(&RecipeRunInspectionRequest {
            request_id,
            request_sha256: digest,
            include_logs: include_logs.then_some(true),
        })
        .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestEncoding))?;
        let helper_socket = self.helper_socket.to_path_buf();
        let response = tokio::task::spawn_blocking(move || {
            // Both native ownership and its request stay alive until actual
            // socket work returns, even when the awaiting page is cancelled.
            let _permit = permit;
            let _request_cleanup = request_cleanup;
            call_helper(&helper_socket, &frame, Duration::from_secs(15))
        })
        .await
        .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::HelperCallJoin))??;
        require_bound_response(&response, &request_id.to_string())?;
        if response.error_code.is_some() {
            return Err(runtime_rejection(&response, HostRuntimeAction::RunInspect));
        }
        if response.status != HostHelperResponseStatus::ContainerRuntimeRequestExecuted
            || response.exit_code.is_some()
        {
            return Err(HostRuntimeError::HelperProtocol(
                HelperProtocolCause::InspectionOutcome,
            ));
        }
        let running = response
            .process_running
            .ok_or(HostRuntimeError::HelperProtocol(
                HelperProtocolCause::InspectionOutcome,
            ))?;
        // A tail was never volunteered: only a request that asked for it may
        // receive one, and the reason it is missing is typed.
        let safety_stop = !running
            && response.diagnostic.as_deref().is_some_and(|detail| {
                detail
                    .split_whitespace()
                    .any(|token| token == "exit_cause=host_memory_exhausted")
            });
        if !include_logs
            && (response.process_logs.is_some() || response.diagnostic.is_some() && !safety_stop)
        {
            return Err(HostRuntimeError::HelperProtocol(
                HelperProtocolCause::InspectionOutcome,
            ));
        }
        Ok(RunInspectionReport {
            running,
            process_logs: response.process_logs.as_ref().map(|logs| {
                Box::new(FailureProcessLogs {
                    stdout: sanitize_tail(&logs.stdout),
                    stderr: sanitize_tail(&logs.stderr),
                })
            }),
            log_error: response
                .diagnostic
                .as_deref()
                .map(crate::failure_evidence::sanitize_text),
        })
    }

    pub async fn execute(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        self.execute_bound(claim, action, arguments, None, None, None)
            .await
    }

    pub async fn execute_plan(
        &self,
        claim: &AgentClaim,
        arguments: Vec<String>,
        plan: HostRuntimePlan,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        self.execute_bound(claim, plan.action(), arguments, None, None, Some(plan))
            .await
    }

    pub async fn cleanup_installation(
        &self,
        claim: &AgentClaim,
        installation_id: uuid::Uuid,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        self.execute_bound(
            claim,
            HostRuntimeAction::InstallationCleanup,
            Vec::new(),
            Some(installation_id),
            None,
            None,
        )
        .await
    }

    pub async fn reconcile_installation(
        &self,
        claim: &AgentClaim,
        identity: RecipeReconciliationIdentity,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        self.execute_bound(
            claim,
            HostRuntimeAction::InstallationCleanup,
            Vec::new(),
            Some(identity.installation_id),
            Some(identity),
            None,
        )
        .await
    }

    async fn execute_bound(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
        installation_id: Option<uuid::Uuid>,
        reconciliation_identity: Option<RecipeReconciliationIdentity>,
        lifecycle_plan: Option<HostRuntimePlan>,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        let helper_timeout = match action {
            HostRuntimeAction::RuntimePreflight => Duration::from_secs(14),
            HostRuntimeAction::Start => lifecycle_plan
                .as_ref()
                .and_then(|plan| match plan {
                    HostRuntimePlan::JobRun(job) => job
                        .compiled_execution_plan
                        .job
                        .as_ref()
                        .map(|job| u64::from(job.timeout_seconds)),
                    _ => None,
                })
                .filter(|value| (1..=3600).contains(value))
                .map_or(Duration::from_secs(610), |value| {
                    // Leave room after the adapter deadline for bounded inspect/stop/remove and
                    // a truthful uncertainty response from the privileged helper.
                    Duration::from_secs(value + 120)
                }),
            HostRuntimeAction::Stop => lifecycle_plan
                .as_ref()
                .and_then(|plan| match plan {
                    HostRuntimePlan::Stop(stop) => Some(u64::from(stop.stop_timeout_seconds)),
                    _ => None,
                })
                .filter(|value| (1..=600).contains(value))
                .map_or(Duration::from_secs(45), |value| {
                    Duration::from_secs(value + 45)
                }),
            _ => Duration::from_secs(610),
        };
        let (start_plan, job_plan, stop_plan, run_generation) = match &lifecycle_plan {
            Some(HostRuntimePlan::Start(plan)) => {
                (Some(plan.clone()), None, None, Some(plan.run_generation))
            }
            Some(HostRuntimePlan::JobRun(plan)) => {
                (None, Some(plan.clone()), None, Some(plan.run_generation))
            }
            Some(HostRuntimePlan::Stop(plan)) => {
                (None, None, Some(plan.clone()), Some(plan.run_generation))
            }
            None => (None, None, None, None),
        };
        let request = HostRuntimeRequest {
            action,
            fence: claim.fence,
            arguments,
            job_plan,
            installation_id,
            reconciliation_identity: reconciliation_identity.clone(),
            run_generation,
            start_plan,
            stop_plan,
        };
        request
            .validate()
            .map_err(HostRuntimeError::request_refusal)?;
        let body = canonical_json(&request)
            .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestEncoding))?;
        let digest = hex_sha256(&body);
        let request_path = write_request(self.request_root, &digest, &body)?;
        // The attached helper call is deliberately run on a blocking worker so a cancellation
        // heartbeat can issue a concurrent STOP. If that cancellation drops this future, still
        // remove the signed request file; the helper already received its canonical body.
        let _request_cleanup = RequestFileCleanup(request_path);
        async {
            let mut nonce = None;
            let mut accepted_response = None;
            // Challenge repair is one bounded observation, not a local planner.
            // Each nonce returns to the authority for a fresh current decision.
            for _ in 0..3 {
                let grant = self
                    .client
                    .host_runtime_grant_with_intent_nonce(claim, &request, &digest, nonce.take())
                    .await?;
                let request_id = grant.claims.request_id.to_string();
                let grant = canonical_json(&grant).map_err(|_| {
                    HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestEncoding)
                })?;
                let helper_socket = self.helper_socket.to_path_buf();
                let response = tokio::task::spawn_blocking(move || {
                    call_helper(&helper_socket, &grant, helper_timeout)
                })
                .await
                .map_err(|_| {
                    HostRuntimeError::HelperProtocol(HelperProtocolCause::HelperCallJoin)
                })??;
                require_bound_response(&response, &request_id)?;
                if response.status == HostHelperResponseStatus::Rejected
                    && response.error_code.as_deref()
                        == Some(HelperErrorCode::InstallationIntentObservationRequired.as_str())
                    && matches!(
                        action,
                        HostRuntimeAction::Start | HostRuntimeAction::InstallationCleanup
                    )
                {
                    nonce = response.installation_intent_nonce.clone();
                    if nonce.as_deref().is_none_or(|value| {
                        value.len() != 64
                            || !value
                                .bytes()
                                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
                    }) {
                        return Err(runtime_rejection(&response, action));
                    }
                    continue;
                }
                accepted_response = Some((response, request_id));
                break;
            }
            let Some((response, request_id)) = accepted_response else {
                return Err(HostRuntimeError::HelperRejected {
                    code: HelperErrorCode::InstallationReconciliationStorageUnavailable,
                    diagnostic: None,
                    process_logs: None,
                });
            };
            let stop_uncertain =
                response.status == HostHelperResponseStatus::ContainerRuntimeStopUncertain;
            require_bound_response(&response, &request_id)?;
            if response.error_code.is_some() {
                return Err(runtime_rejection(&response, action));
            }
            require_executed_outcome(&response, stop_uncertain)?;
            Ok(HostRuntimeOutcome {
                exit_code: response.exit_code.map(|code| code as i32),
                stop_uncertain,
                diagnostic: response
                    .diagnostic
                    .as_deref()
                    .map(crate::failure_evidence::sanitize_text),
                process_logs: response.process_logs.as_ref().map(|logs| {
                    Box::new(FailureProcessLogs {
                        stdout: sanitize_tail(&logs.stdout),
                        stderr: sanitize_tail(&logs.stderr),
                    })
                }),
            })
        }
        .await
    }
}

/// Bind the helper's reply to the request this agent sent before anything in it
/// is trusted.
///
/// The helper attaches the request identity only once it has authorized the
/// grant, so a rejection raised by an earlier check arrives with
/// `request_id: null`. That is the normal shape of the helper saying which check
/// refused -- `grant_node_mismatch` and `grant_unauthorized` cannot be reported
/// any other way -- and demanding an identity that the helper deliberately
/// withholds reported each of them as a malformed reply instead. The code set
/// keeps acceptance closed to the rejections the helper can only raise before it
/// trusts the grant, so an unbound reply can never be mistaken for a bound one.
#[cfg(test)]
mod tests;
