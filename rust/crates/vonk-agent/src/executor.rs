use async_trait::async_trait;
use chrono::{DateTime, FixedOffset, Utc};
use futures_util::{StreamExt, stream};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File},
    future::Future,
    io::Read,
    path::Path,
    time::{Duration, Instant},
};

use crate::runtime_identity::AgentRuntimeIdentity;
use crate::{
    agent_upgrade::AgentUpgradeExecutor,
    client::{
        AgentHttpClient, ClientError, ControllerError, DistributionDownloadEvidence,
        DistributionProgress, ExactRecipeRunObservation, HEARTBEAT_LEASE_MARGIN,
    },
    health::{wait_ready, wait_ready_until},
    host_runtime::{HostRuntimeBoundary, HostRuntimeOutcome},
    image_importer::ImageImporter,
    oci::{OciError, OciRuntime, RecipeRunStartIdentity},
    process::ProcessRunner,
    recipe_builder::RecipeBuilder,
    state::{BeginDecision, StateError, StateStore},
    workloads::{
        CompiledExecutionPlan, CompiledRuntimePlacement, WorkloadError, same_installed_workload,
    },
};
use vonk_agent_protocol::generated::AgentFailureKind;
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, HostRuntimeAction, OperationProgress,
    ProtocolError, RecipeJobEvidence, RecipeJobFile, RecipeJobOutputLimits,
    RecipeJobOutputManifest, RecipeJobOutputMapping, RecipeJobRunResult, RecipeOperationRequest,
    RecipeReconcileResult, RecipeReconciliationIdentity, RecipeStartPhase, RecipeStartRequest,
    RecipeStopResult, RecipeUninstallResult, canonical_json, hex_sha256,
};

const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
/// Prompt retry after a transient heartbeat failure.  Sleeping a whole
/// renewal interval here is what let one lost request schedule the next
/// attempt after the accepted lease had already expired.
const HEARTBEAT_RETRY_INTERVAL: Duration = Duration::from_secs(2);
/// Smallest delay between two renewal attempts.  A lease that is nearly spent
/// still gets one bounded attempt instead of a busy loop.
const HEARTBEAT_RETRY_FLOOR: Duration = Duration::from_millis(50);

/// When a renewal loop attempts its next heartbeat.
///
/// ``interval`` is the steady-state cadence once a renewal is accepted and
/// ``retry_interval`` is the prompt delay after a transient failure.
#[derive(Clone, Copy)]
struct HeartbeatSchedule {
    interval: Duration,
    retry_interval: Duration,
}
const JOB_CANCEL_EXIT_CODE: u32 = 130;
const JOB_CANCEL_STOP_TIMEOUT_SECONDS: u16 = 5;
const JOB_CANCEL_DRAIN_TIMEOUT: Duration = Duration::from_secs(20);

pub fn parse_compiled_execution_plan(value: &Value) -> Result<CompiledExecutionPlan, OciError> {
    let plan: CompiledExecutionPlan = serde_json::from_value(value.clone())?;
    plan.validate()?;
    Ok(plan)
}

pub fn readiness_identity(spec: &CompiledExecutionPlan) -> (String, String) {
    let image_digest = spec.runtime.image_digest.clone();
    let model_identity = spec
        .artifacts
        .first()
        .map(|artifact| {
            format!(
                "{}/{}@{}",
                artifact.model.publisher, artifact.model.slug, artifact.model.content_sha256
            )
        })
        .unwrap_or_default();
    (image_digest, model_identity)
}

struct JobScopeCleanup<'runtime, 'data, R: ProcessRunner> {
    runtime: &'runtime OciRuntime<'data, R>,
    job_scope: &'runtime str,
    active: bool,
}

impl<'runtime, 'data, R: ProcessRunner> JobScopeCleanup<'runtime, 'data, R> {
    fn new(runtime: &'runtime OciRuntime<'data, R>, job_scope: &'runtime str) -> Self {
        Self {
            runtime,
            job_scope,
            active: true,
        }
    }

    fn finish(mut self) -> Result<(), crate::oci::OciError> {
        let result = self.runtime.cleanup_job_scope(self.job_scope);
        self.active = result.is_err();
        result
    }

    fn retain(mut self) {
        self.active = false;
    }
}

impl<R: ProcessRunner> Drop for JobScopeCleanup<'_, '_, R> {
    fn drop(&mut self) {
        if self.active {
            let _ = self.runtime.cleanup_job_scope(self.job_scope);
        }
    }
}

#[async_trait]
pub trait LoopClient: Clone + Send + Sync + 'static {
    async fn claim(
        &self,
        capabilities: &[&str],
        wait_seconds: u64,
        runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError>;
    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError>;
    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError>;
}

#[async_trait]
impl LoopClient for AgentHttpClient {
    async fn claim(
        &self,
        capabilities: &[&str],
        wait_seconds: u64,
        runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        AgentHttpClient::claim(self, capabilities, wait_seconds, runtime_identity).await
    }

    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        AgentHttpClient::heartbeat(self, progress).await
    }

    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        AgentHttpClient::submit_result(self, result).await
    }
}

pub struct ExecutionResult {
    pub state: &'static str,
    pub body: Value,
}

#[async_trait(?Send)]
pub trait Executor {
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult;
}

pub struct RejectingExecutor;

#[async_trait(?Send)]
impl Executor for RejectingExecutor {
    async fn execute(
        &self,
        claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        _cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        ExecutionResult {
            state: "waiting-for-operator",
            body: json!({"operation": claim.operation, "reason": "operation is not enabled by this agent build"}),
        }
    }
}

pub struct RecipeExecutor<'a, R> {
    pub client: &'a AgentHttpClient,
    pub runtime: OciRuntime<'a, R>,
    pub runtime_root: &'a Path,
    pub observation_receipt_public_key: [u8; 32],
}

#[derive(Debug, thiserror::Error)]
pub enum RecipeObservationError {
    #[error("managed recipe run observation failed ({})", .0.safe_category())]
    Runtime(#[from] crate::oci::OciError),
    #[error("exact recipe run inspection failed ({})", .0.preflight_code())]
    Inspection(#[from] crate::host_runtime::HostRuntimeError),
    #[error("exact recipe run observation could not be reported: {0}")]
    Report(#[from] ClientError),
    #[error("exact recipe run snapshot expired before reporting")]
    StaleSnapshot,
}

impl RecipeObservationError {
    pub fn not_ready(&self) -> bool {
        matches!(
            self,
            Self::Inspection(crate::host_runtime::HostRuntimeError::Controller(
                ClientError::ObservationNotReady
            ))
        )
    }
}

pub struct ControlExecutor<'a, R> {
    pub recipes: RecipeExecutor<'a, R>,
    pub upgrades: AgentUpgradeExecutor<'a>,
}

#[async_trait(?Send)]
impl<R: ProcessRunner> Executor for ControlExecutor<'_, R> {
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        if claim.operation == "agent.upgrade.v1" {
            return match self.upgrades.execute(claim).await {
                Ok(()) => ExecutionResult {
                    state: "waiting-for-operator",
                    body: json!({
                        "reason": crate::agent_upgrade::UPGRADE_AWAITING_IDENTITY_REASON,
                    }),
                },
                Err(error) => {
                    let reason = match error.diagnostic() {
                        Some(detail) if !detail.is_empty() => format!("{error}: {detail}"),
                        _ => error.to_string(),
                    };
                    let mut body = json!({"reason": reason});
                    if let Some(detail) = error.diagnostic() {
                        body["diagnostic_logs"] = json!({
                            "stdout": crate::failure_evidence::log_tail(&[]),
                            "stderr": crate::failure_evidence::log_tail(detail.as_bytes()),
                        });
                    }
                    if let Some((code, exit_code)) = error.helper_diagnostics() {
                        body["helper_error_code"] = json!(code);
                        if let Some(exit_code) = exit_code {
                            body["helper_exit_code"] = json!(exit_code);
                        }
                    }
                    ExecutionResult {
                        state: "failed",
                        body,
                    }
                }
            };
        }
        self.recipes
            .execute(claim, lease_deadline, cancellation)
            .await
    }
}

/// A collection failure is unknown evidence for its run, not for another
/// successfully inspected run. Nonempty partial reports preserve omitted ranks
/// on the Controller. Only a successfully collected empty set reports absence.
async fn report_complete_recipe_run_observations(
    client: &AgentHttpClient,
    results: Vec<Result<ExactRecipeRunObservation, RecipeObservationError>>,
) -> Result<usize, RecipeObservationError> {
    let mut observations = Vec::with_capacity(results.len());
    let mut failure = None;
    for result in results {
        match result {
            Ok(observation) => observations.push(observation),
            Err(error) => {
                if failure
                    .as_ref()
                    .is_none_or(RecipeObservationError::not_ready)
                {
                    failure = Some(error);
                }
            }
        }
    }
    // Bounded concurrent inspections can finish in different batches. Retain
    // each still-authorized receipt even when another inspection expired.
    // The Controller remains the authority for receipt age and authorization.
    let now = Utc::now().timestamp();
    observations.retain(|observation| {
        if now <= observation.grant.claims.expires_at {
            return true;
        }
        eprintln!(
            "vonk-agent: exact recipe run {} receipt expired during collection",
            observation.run_id
        );
        if failure
            .as_ref()
            .is_none_or(RecipeObservationError::not_ready)
        {
            failure = Some(RecipeObservationError::StaleSnapshot);
        }
        false
    });
    if !observations.is_empty() || failure.is_none() {
        client
            .report_exact_recipe_run_observations(&observations)
            .await?;
    }
    match failure {
        Some(error) => Err(error),
        None => Ok(observations.len()),
    }
}

impl<R> RecipeExecutor<'_, R> {
    async fn report_phase(&self, claim: &AgentClaim, phase: &str) {
        self.client.set_progress_phase(claim.operation_id, phase);
    }

    /// Locally retained managed runs, counted without asking the Controller.
    ///
    /// A refused sweep must not be mistaken for a node with nothing left to
    /// observe: the claim cadence follows this count, so reporting zero after
    /// a transient refusal slowed exact observation six-fold.
    pub fn managed_recipe_run_count(&self) -> Result<usize, RecipeObservationError>
    where
        R: ProcessRunner,
    {
        Ok(self.runtime.recipe_run_inspection_plans()?.len())
    }

    pub async fn report_exact_recipe_run_observations(
        &self,
    ) -> Result<usize, RecipeObservationError>
    where
        R: ProcessRunner,
    {
        let plans = self.runtime.recipe_run_inspection_plans()?;
        let results = stream::iter(plans)
            .map(|plan| async move {
                let request_root = self.runtime_root.join("runtime-requests");
                let boundary = HostRuntimeBoundary {
                    client: self.client,
                    request_root: &request_root,
                    helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
                    observation_receipt_public_key: self.observation_receipt_public_key,
                };
                let endpoint = plan.endpoint_address;
                let outcome = boundary
                    .inspect_recipe_run(plan.binding.clone(), plan.arguments)
                    .await
                    .inspect_err(|error| {
                        if !matches!(
                            error,
                            crate::host_runtime::HostRuntimeError::Controller(
                                ClientError::ObservationNotReady
                            )
                        ) {
                            eprintln!(
                                "vonk-agent: exact recipe run {} inspection failed: {}",
                                plan.binding.run_id,
                                error.preflight_code()
                            );
                        }
                    })?;
                // This timestamp is part of the signed-grant freshness proof.
                // Capture it immediately after the local privileged inspection;
                // an owner-only HTTP probe follows and remains independently
                // bounded to five seconds.
                let observed_at = DateTime::from_timestamp(outcome.receipt.claims.observed_at, 0)
                    .ok_or(crate::host_runtime::HostRuntimeError::HelperProtocol(
                    crate::host_runtime::HelperProtocolCause::ObservationTimestamp,
                ))?;
                let endpoint_ready = endpoint.map(|address| {
                    outcome.process_running
                        && self.runtime.readiness_request(
                            address,
                            plan.endpoint_port,
                            &plan.health_path,
                        )
                });
                Ok::<_, RecipeObservationError>(ExactRecipeRunObservation {
                    schema_version: 1,
                    node_id: self.client.node_id().to_owned(),
                    observed_at: observed_at.into(),
                    artifact_set_digest: plan.binding.artifact_set_digest,
                    image_digest: plan.binding.image_digest,
                    installation_id: plan.binding.installation_id,
                    local_address: plan.binding.local_address,
                    mapping_generation: plan.binding.mapping_generation,
                    mapping_id: plan.binding.mapping_id,
                    master_address: plan.binding.master_address,
                    master_port: plan.binding.master_port,
                    model_identity: plan.binding.model_identity,
                    port: plan.binding.port,
                    rank: plan.binding.rank,
                    recipe_content_sha256: plan.binding.recipe_content_sha256,
                    recipe_revision_id: plan.binding.recipe_revision_id,
                    role: plan.binding.role,
                    run_generation: plan.binding.run_generation,
                    run_id: plan.binding.run_id,
                    runtime_arguments_sha256: plan.binding.runtime_arguments_sha256,
                    world_size: plan.binding.world_size,
                    endpoint_ready,
                    grant: outcome.grant,
                    observation_identity_sha256: outcome.observation_identity_sha256,
                    helper_receipt: outcome.receipt,
                    observation_receipt_public_key: hex::encode(
                        self.observation_receipt_public_key,
                    ),
                })
            })
            .buffer_unordered(8)
            .collect::<Vec<_>>()
            .await;
        report_complete_recipe_run_observations(self.client, results).await
    }

    async fn execute_host_runtime_outcome(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
    ) -> Result<HostRuntimeOutcome, crate::host_runtime::HostRuntimeError> {
        self.report_phase(
            claim,
            match action {
                HostRuntimeAction::ImageImport => "extracting",
                HostRuntimeAction::Start => "starting",
                HostRuntimeAction::Stop => "stopping",
                _ => "verifying",
            },
        )
        .await;
        let request_root = self.runtime_root.join("runtime-requests");
        HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
            observation_receipt_public_key: self.observation_receipt_public_key,
        }
        .execute(claim, action, arguments)
        .await
    }

    async fn execute_host_runtime(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
    ) -> Result<(), crate::host_runtime::HostRuntimeError> {
        self.execute_host_runtime_outcome(claim, action, arguments)
            .await
            .and_then(|outcome| {
                if outcome.stop_uncertain {
                    Err(crate::host_runtime::HostRuntimeError::StopUncertain)
                } else {
                    Ok(())
                }
            })
    }

    async fn cleanup_installation_cache(
        &self,
        claim: &AgentClaim,
        installation_id: uuid::Uuid,
    ) -> Result<(), crate::host_runtime::HostRuntimeError> {
        let request_root = self.runtime_root.join("runtime-requests");
        HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
            observation_receipt_public_key: self.observation_receipt_public_key,
        }
        .cleanup_installation(claim, installation_id)
        .await
        .and_then(|outcome| {
            if outcome.stop_uncertain {
                Err(crate::host_runtime::HostRuntimeError::StopUncertain)
            } else {
                Ok(())
            }
        })
    }

    async fn reconcile_installation_runtime(
        &self,
        claim: &AgentClaim,
        identity: RecipeReconciliationIdentity,
    ) -> Result<(), crate::host_runtime::HostRuntimeError> {
        let request_root = self.runtime_root.join("runtime-requests");
        HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
            observation_receipt_public_key: self.observation_receipt_public_key,
        }
        .reconcile_installation(claim, identity)
        .await
        .and_then(|outcome| {
            if outcome.stop_uncertain {
                Err(crate::host_runtime::HostRuntimeError::StopUncertain)
            } else {
                Ok(())
            }
        })
    }

    async fn stop_start_run(
        &self,
        claim: &AgentClaim,
        run_id: &str,
        stop_timeout_seconds: u32,
        cancel_pending_start: bool,
    ) -> Result<(), ExecutionResult>
    where
        R: ProcessRunner,
    {
        // The helper's exact run fence keeps an in-flight Docker START from
        // creating this run after STOP has observed temporary absence.
        let mut arguments = vec![run_id.to_owned(), stop_timeout_seconds.to_string()];
        if cancel_pending_start {
            arguments.push("job-cancel".to_owned());
        }
        if self
            .execute_host_runtime(claim, HostRuntimeAction::Stop, arguments)
            .await
            .is_err()
        {
            return Err(waiting_for_operator("workload stop remains unconfirmed"));
        }
        if self.runtime.complete_stop(run_id).is_err() {
            return Err(waiting_for_operator(
                "workload local cleanup remains unconfirmed",
            ));
        }
        Ok(())
    }

    async fn cancel_start_run(
        &self,
        claim: &AgentClaim,
        run_id: &str,
        stop_timeout_seconds: u32,
    ) -> ExecutionResult
    where
        R: ProcessRunner,
    {
        if let Err(uncertain) = self
            .stop_start_run(claim, run_id, stop_timeout_seconds, true)
            .await
        {
            return uncertain;
        }
        ExecutionResult {
            state: "cancelled",
            body: json!({"reason": "controller cancellation confirmed after exact workload stop", "error_code": "operation_cancelled"}),
        }
    }
}

/// Why a readiness wait ended.
///
/// The runtime guard exists so a start stops observing a workload the agent can
/// no longer inspect.  Collapsing its error into `false` made that failure
/// indistinguishable from an expired readiness deadline, so a start whose
/// privileged inspection failed was reported as "the workload did not become
/// ready before its deadline" and the Controller's existing
/// `runtime_observation_unavailable` retry could never fire.  Observed live on
/// 2026-09-17, a two-Spark GLM start ended 51 s in -- five ten-second inspection
/// ticks -- with 30 s still on the attempt lease and an hour on the start budget.
enum ReadinessOutcome {
    Ready,
    Deadline,
    Cancelled,
    GuardFailed(crate::host_runtime::HostRuntimeError),
}

async fn wait_ready_with_runtime_guard_and_cancellation<R, G>(
    readiness: R,
    runtime_guard: G,
    mut cancellation: tokio::sync::watch::Receiver<bool>,
) -> ReadinessOutcome
where
    R: Future<Output = Result<(), crate::health::HealthError>>,
    G: Future<Output = Result<std::convert::Infallible, crate::host_runtime::HostRuntimeError>>,
{
    if *cancellation.borrow() {
        return ReadinessOutcome::Cancelled;
    }
    tokio::select! {
        result = readiness => match result {
            Ok(()) => ReadinessOutcome::Ready,
            Err(_) => ReadinessOutcome::Deadline,
        },
        guard = runtime_guard => match guard {
            Ok(never) => match never {},
            Err(error) => ReadinessOutcome::GuardFailed(error),
        },
        _ = cancellation.changed() => ReadinessOutcome::Cancelled,
    }
}

fn evidence_with_digest(mut evidence: Value) -> (Value, String) {
    let evidence_digest = canonical_json(&evidence)
        .map(|value| hex_sha256(&value))
        .unwrap_or_default();
    if let Some(document) = evidence.as_object_mut() {
        document.insert(
            "evidence_digest".to_owned(),
            Value::String(evidence_digest.clone()),
        );
    }
    (evidence, evidence_digest)
}

/// Build the exact runtime argument vector used for start, inspection, and a
/// pre-start hook. The caller supplies the complete command slice because
/// `RuntimeStartPlan::pre_start` already contains a complete hook invocation.
pub fn runtime_arguments_for_plan(
    plan: &crate::oci::RuntimeStartPlan,
    command: &[String],
) -> Vec<String> {
    let mut arguments = vec![
        plan.archive_sha256.clone(),
        plan.registry_index_digest.clone(),
        plan.platform_manifest_digest.clone(),
        plan.image_reference.clone(),
    ];
    arguments.extend(command.iter().cloned());
    arguments
}

pub fn runtime_arguments_digest(arguments: &[String]) -> Result<String, ProtocolError> {
    canonical_json(&arguments.to_vec()).map(|value| hex_sha256(&value))
}

pub fn recipe_install_success_body(installed_bytes: u64) -> Value {
    json!({"installed_bytes": installed_bytes})
}

pub fn recipe_stop_success_body() -> Value {
    let result = RecipeStopResult { stopped: true };
    result.validate().expect("valid stop result");
    serde_json::to_value(result).expect("serializable stop result")
}

pub fn recipe_uninstall_success_body(removed_model_bytes: u64) -> Value {
    let result = RecipeUninstallResult {
        uninstalled: true,
        removed_model_bytes,
    };
    result.validate().expect("valid uninstall result");
    serde_json::to_value(result).expect("serializable uninstall result")
}

fn recipe_reconcile_success_body(
    identity: &RecipeReconciliationIdentity,
    removed_bytes: u64,
    cleanup_receipt_sha256: String,
) -> Value {
    let result = RecipeReconcileResult {
        cleanup_receipt_sha256,
        compiled_spec_canonical_sha256: identity.compiled_spec_canonical_sha256.clone(),
        install_operation_id: identity.install_operation_id,
        install_operation_payload_sha256: identity.install_operation_payload_sha256.clone(),
        installation_id: identity.installation_id,
        node_id: identity.node_id.clone(),
        plan_digest: identity.plan_digest.clone(),
        recipe_content_sha256: identity.recipe_content_sha256.clone(),
        recipe_revision_id: identity.recipe_revision_id,
        reconciled: true,
        removed_bytes,
    };
    result.validate().expect("valid reconciliation result");
    serde_json::to_value(result).expect("serializable reconciliation result")
}

pub fn recipe_start_success_body(
    request: &RecipeStartRequest,
    spec: &CompiledExecutionPlan,
    artifact_set_digest: &str,
    runtime_guard_arguments: &[String],
) -> Result<Value, ProtocolError> {
    let runtime_arguments_sha256 = runtime_arguments_digest(runtime_guard_arguments)?;
    let (image_digest, model_identity) = readiness_identity(spec);
    let endpoint = format!(
        "http://{}:{}",
        match request.endpoint_address {
            std::net::IpAddr::V4(address) => address.to_string(),
            std::net::IpAddr::V6(address) => format!("[{address}]"),
        },
        request.port
    );
    let evidence = match request.phase {
        Some(RecipeStartPhase::RankLaunch) => json!({
            "phase": "rank-launch",
            "run_id": request.run_id.to_string(),
            "run_generation": request.run_generation,
            "recipe_revision_id": request.recipe_revision_id.to_string(),
            "recipe_content_sha256": request.recipe_content_sha256,
            "image_digest": image_digest,
            "artifact_set_digest": artifact_set_digest,
            "runtime_arguments_sha256": runtime_arguments_sha256,
            "model_identity": model_identity,
            "rank": request.rank,
            "role": request.role,
            "world_size": request.world_size,
            "local_address": request.local_address,
            "master_address": request.master_address,
            "master_port": request.master_port,
            "memory_reservation_bytes": request.reserved_memory_bytes,
            "process_running": true,
            "fabric_projection_bound": true,
            "launched": true,
        }),
        Some(RecipeStartPhase::CollectiveReadiness) => json!({
            "phase": "collective-readiness",
            "run_id": request.run_id.to_string(),
            "run_generation": request.run_generation,
            "recipe_revision_id": request.recipe_revision_id.to_string(),
            "recipe_content_sha256": request.recipe_content_sha256,
            "image_digest": image_digest,
            "artifact_set_digest": artifact_set_digest,
            "runtime_arguments_sha256": runtime_arguments_sha256,
            "model_identity": model_identity,
            "rank": request.rank,
            "role": request.role,
            "world_size": request.world_size,
            "local_address": request.local_address,
            "master_address": request.master_address,
            "master_port": request.master_port,
            "endpoint": endpoint,
            "memory_reservation_bytes": request.reserved_memory_bytes,
            "ready": true,
        }),
        None => {
            let evidence = json!({
                "recipe_revision_id": request.recipe_revision_id.to_string(),
                "recipe_content_sha256": request.recipe_content_sha256,
                "image_digest": image_digest,
                "artifact_set_digest": artifact_set_digest,
                "model_identity": model_identity,
                "rank": request.rank,
                "world_size": request.world_size,
                "endpoint": endpoint,
                "memory_reservation_bytes": request.reserved_memory_bytes,
                "ready": true,
                "run_generation": request.run_generation,
                "runtime_arguments_sha256": runtime_arguments_sha256,
                "local_address": request.local_address,
                "master_address": request.master_address,
                "master_port": request.master_port,
            });
            let (evidence, evidence_digest) = evidence_with_digest(evidence);
            return Ok(json!({
                "endpoint": endpoint,
                "evidence": evidence,
                "evidence_digest": evidence_digest,
            }));
        }
    };
    let (evidence, evidence_digest) = evidence_with_digest(evidence);
    Ok(match request.phase {
        Some(RecipeStartPhase::CollectiveReadiness) => json!({
            "endpoint": endpoint,
            "evidence": evidence,
            "evidence_digest": evidence_digest,
        }),
        Some(RecipeStartPhase::RankLaunch) => {
            json!({"evidence": evidence, "evidence_digest": evidence_digest})
        }
        None => unreachable!("single-node result returned above"),
    })
}

pub fn distribution_success_evidence(evidence: DistributionDownloadEvidence) -> Value {
    evidence_with_digest(json!({
        "assignment_id": evidence.assignment_id,
        "model_artifact_set_sha256": evidence.model_artifact_set_sha256,
        "verified": true,
        "verified_digests": evidence.model_digests,
        "verified_image_digest": evidence.oci_image_digest,
        "imported_image_digest": evidence.oci_image_digest,
        "verified_oci_layout_sha256": evidence.oci_archive_sha256,
        "oci_image_digest": evidence.oci_image_digest,
        "downloaded_bytes": evidence.downloaded_bytes,
    }))
    .0
}

fn before_phase_deadline(
    lease_deadline: &tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
    start_deadline: Option<&DateTime<FixedOffset>>,
) -> bool {
    Utc::now() < crate::health::phase_deadline(lease_deadline, start_deadline)
}

async fn wait_for_launch_stability(
    mut lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
    mut cancellation: tokio::sync::watch::Receiver<bool>,
    start_deadline: Option<DateTime<FixedOffset>>,
    duration: Duration,
) -> bool {
    let stable_at = tokio::time::Instant::now() + duration;
    loop {
        if *cancellation.borrow()
            || !before_phase_deadline(&lease_deadline, start_deadline.as_ref())
        {
            return false;
        }
        let effective = crate::health::phase_deadline(&lease_deadline, start_deadline.as_ref());
        let until_deadline = (effective - Utc::now()).to_std().unwrap_or(Duration::ZERO);
        tokio::select! {
            _ = tokio::time::sleep_until(stable_at) => {
                return before_phase_deadline(&lease_deadline, start_deadline.as_ref())
                    && !*cancellation.borrow();
            }
            _ = tokio::time::sleep(until_deadline) => return false,
            changed = lease_deadline.changed() => {
                if changed.is_err() {
                    return false;
                }
            }
            changed = cancellation.changed() => {
                if changed.is_err() || *cancellation.borrow() {
                    return false;
                }
            }
        }
    }
}

#[async_trait(?Send)]
impl<R: ProcessRunner> Executor for RecipeExecutor<'_, R> {
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        mut cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        if claim.operation == "artifact.distribution.v1" {
            if claim.validate().is_err() {
                return failed("artifact distribution claim is invalid");
            }
            let vonk_agent_protocol::generated::AgentClaimPayload::ArtifactDistributionPayload(
                request,
            ) = &claim.payload
            else {
                return failed("artifact distribution request is invalid");
            };
            if request.validate().is_err() || request.plan_digest != claim.authority_revision {
                return failed("artifact distribution plan identity is invalid");
            }
            self.report_phase(claim, "preparing").await;
            let destination = self.runtime.data_root.join("distribution");
            let (progress_sender, mut progress_receiver) =
                tokio::sync::watch::channel::<Option<DistributionProgress>>(None);
            let progress_client = self.client.clone();
            let progress_claim = claim.clone();
            let progress_deadline = lease_deadline.clone();
            let progress_task = tokio::spawn(async move {
                // Progress is a snapshot, not an event log. Coalesce fast
                // transfer updates instead of accumulating an unbounded queue
                // of heartbeat requests before image import can begin.
                let mut completed_bytes = 0_u64;
                let mut completed_items = 0_u64;
                let mut cadence = tokio::time::interval(Duration::from_secs(1));
                cadence.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
                while progress_receiver.changed().await.is_ok() {
                    cadence.tick().await;
                    let Some(item) = progress_receiver.borrow_and_update().clone() else {
                        continue;
                    };
                    // Retries rescan durable objects from the beginning. Keep the
                    // operation-wide high-water mark while those objects replay.
                    progress_client.set_progress_phase(progress_claim.operation_id, item.phase);
                    completed_bytes = completed_bytes.max(item.bytes);
                    completed_items = completed_items.max(item.completed_items);
                    let progress = AgentProgress {
                        attempt: progress_claim.attempt,
                        deadline: *progress_deadline.borrow(),
                        fence: progress_claim.fence,
                        job_id: progress_claim.job_id,
                        node_id: progress_claim.node_id.clone(),
                        operation_id: progress_claim.operation_id,
                        progress: Some(OperationProgress {
                            completed_items: Some(completed_items),
                            total_items: Some(item.total_items),
                            object_sha256: Some(item.object_sha256),
                            kind: Some(item.kind),
                            completed_bytes,
                            total_bytes: item.total_bytes,
                            total_bytes_known: item.total_bytes.is_some(),
                            ..phase_progress(item.phase)
                        }),
                        schema_version: 1,
                    };
                    let _ = progress_client.heartbeat(&progress).await;
                }
            });
            let download = {
                let mut result = None;
                let archive_root = self.runtime.data_root.join("oci-archives");
                for attempt in 0..3_u32 {
                    let progress_sender = progress_sender.clone();
                    let current = self
                        .client
                        .download_distribution_with_progress(
                            &request.plan_digest,
                            &destination,
                            &archive_root,
                            move |item| {
                                progress_sender.send_replace(Some(item));
                            },
                        )
                        .await;
                    match current {
                        Ok(value) => {
                            result = Some(Ok(value));
                            break;
                        }
                        Err(error)
                            if error.retryable()
                                && error.retry_after_seconds().is_none()
                                && attempt < 2 =>
                        {
                            tokio::time::sleep(Duration::from_millis(100 * (attempt + 1) as u64))
                                .await;
                        }
                        Err(error) => {
                            result = Some(Err(error));
                            break;
                        }
                    }
                }
                result.expect("bounded distribution retry always records a result")
            };
            // The reporter exits only when every sender is dropped. Keep it
            // alive through retries, then close it before waiting; otherwise
            // a finished transfer can wait forever before importing its image.
            drop(progress_sender);
            let _ = progress_task.await;
            return match download {
                Ok(evidence) => {
                    let importer = ImageImporter {
                        data_root: self.runtime.data_root,
                    };
                    let archive = match importer.retain_verified_distribution_archive(
                        &evidence.oci_archive_sha256,
                        &evidence.oci_image_digest,
                        evidence.oci_archive_bytes,
                        &evidence.oci_archive_path,
                    ) {
                        Ok(path) => path,
                        Err(_) => return failed("distributed OCI archive could not be retained"),
                    };
                    if let Err(error) = self
                        .execute_host_runtime(
                            claim,
                            HostRuntimeAction::ImageImport,
                            importer.distribution_runtime_arguments(
                                &evidence.oci_archive_sha256,
                                &evidence.oci_image_digest,
                                evidence.oci_archive_bytes,
                                &archive,
                            ),
                        )
                        .await
                    {
                        // HostRuntimeError exposes only bounded, stable
                        // categories, never helper stderr or credentials.
                        return ExecutionResult {
                            state: "failed",
                            body: json!({
                                "reason": format!(
                                    "distributed OCI image could not be imported: {error}"
                                ),
                            }),
                        };
                    }
                    ExecutionResult {
                        state: "succeeded",
                        body: distribution_success_evidence(evidence),
                    }
                }
                Err(error) => distribution_failure_result(&error),
            };
        }
        let request = match RecipeOperationRequest::parse(claim) {
            Ok(request) => request,
            Err(_) => return failed("recipe operation payload is invalid"),
        };
        match request {
            RecipeOperationRequest::RuntimePreflight(request) => {
                use crate::runtime_preflight::{
                    PROBE_BINARY, RuntimePreflight, finding, host_fingerprint,
                };
                let started = std::time::Instant::now();
                let fingerprint = match host_fingerprint(
                    self.runtime.runner,
                    env!("VONK_AGENT_BUILD_DIGEST"),
                    self.runtime.data_root,
                    self.runtime_root,
                ) {
                    Ok(value) => value,
                    Err(_) => {
                        return failed("runtime preflight host policy fingerprint is unavailable");
                    }
                };
                // Fabric bandwidth is declared inventory, not measured NCCL acceptance.
                // The configured fabric address must actually be bindable on this host.
                let fabric =
                    crate::config::AgentConfig::load(Path::new(crate::config::DEFAULT_CONFIG_PATH))
                        .ok()
                        .and_then(|config| {
                            let address = config.fabric_address?;
                            let speed = config.fabric_bandwidth_mbps?;
                            std::net::TcpListener::bind((address, 0))
                                .ok()
                                .map(|_| ("connected", speed))
                        });
                let probe = RuntimePreflight {
                    runner: self.runtime.runner,
                    data_root: self.runtime.data_root,
                    runtime_root: self.runtime_root,
                    probe_binary: Path::new(PROBE_BINARY),
                };
                let mut result =
                    match probe.run(&request, fingerprint, fabric, &|| *cancellation.borrow()) {
                        Ok(result) => result,
                        Err(_) => {
                            return failed(
                                "runtime preflight could not inspect the agent service environment",
                            );
                        }
                    };
                let outcome = self
                    .execute_host_runtime_outcome(
                        claim,
                        HostRuntimeAction::RuntimePreflight,
                        vec![],
                    )
                    .await;
                let (passed, code) = match outcome {
                    Ok(outcome) => {
                        let (passed, code) = match outcome.exit_code {
                            Some(0) => (true, "available"),
                            Some(21) => (false, "helper_proc_unavailable"),
                            Some(22) => (false, "helper_capabilities_not_zero"),
                            Some(23) => (false, "helper_no_new_privileges_unavailable"),
                            Some(24) => (false, "helper_mount_namespace_unavailable"),
                            Some(25) => (false, "helper_temporary_directory_unavailable"),
                            Some(30) => (false, "helper_image_import_failed"),
                            Some(31) => (false, "helper_sandbox_run_failed"),
                            Some(32) => (false, "helper_probe_cleanup_failed"),
                            _ => (false, "helper_probe_invalid_result"),
                        };
                        (passed, code.to_owned())
                    }
                    Err(error) => (false, error.preflight_code()),
                };
                result
                    .findings
                    .retain(|value| value.capability != "signed_helper_run");
                result
                    .findings
                    .push(finding("signed_helper_run", passed, &code));
                result.duration_ms = started.elapsed().as_millis() as u64;
                if result.validate().is_err() {
                    return failed("runtime preflight exceeded the bounded deadline");
                }
                ExecutionResult {
                    state: "succeeded",
                    body: serde_json::to_value(result).expect("typed preflight serializes"),
                }
            }
            RecipeOperationRequest::BuildCleanup(request) => {
                match crate::recipe_builder::cleanup_build(self.runtime.runner, &request) {
                    Ok(evidence) => ExecutionResult {
                        state: "succeeded",
                        body: serde_json::to_value(evidence).expect("typed cleanup evidence"),
                    },
                    Err(_) => failed("recipe build cleanup could not confirm the service stopped"),
                }
            }
            RecipeOperationRequest::Build(request) => {
                self.report_phase(claim, "downloading").await;
                let archive = match self
                    .client
                    .source_bundle(
                        &request.source_bundle_sha256,
                        u64::from(request.source_bundle_bytes),
                    )
                    .await
                {
                    Ok(archive) => archive,
                    Err(error) => {
                        return recipe_build_client_failure_result(
                            &error,
                            "source-bundle-fetch",
                            "authorized source bundle could not be fetched",
                        );
                    }
                };
                let builder = RecipeBuilder {
                    runner: self.runtime.runner,
                    data_root: self.runtime.data_root,
                    runtime_root: self.runtime_root,
                    egress_binary: Path::new("/usr/lib/vonk-forge/vonk-build-egress"),
                };
                self.report_phase(claim, "building").await;
                let cancelled = || *cancellation.borrow();
                match builder.build_cancellable(&request, claim.operation_id, &archive, &cancelled)
                {
                    Ok(evidence) => {
                        self.report_phase(claim, "uploading").await;
                        let (sender, mut receiver) = tokio::sync::watch::channel(0_u64);
                        let progress_client = self.client.clone();
                        let progress_claim = claim.clone();
                        let progress_deadline = lease_deadline.clone();
                        let total_bytes = evidence.image_bytes;
                        let progress_task = tokio::spawn(async move {
                            let mut completed_bytes = 0;
                            let mut cadence = tokio::time::interval(Duration::from_secs(1));
                            cadence.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
                            while receiver.changed().await.is_ok() {
                                cadence.tick().await;
                                completed_bytes =
                                    completed_bytes.max(*receiver.borrow_and_update());
                                let progress = AgentProgress {
                                    attempt: progress_claim.attempt,
                                    deadline: *progress_deadline.borrow(),
                                    fence: progress_claim.fence,
                                    job_id: progress_claim.job_id,
                                    node_id: progress_claim.node_id.clone(),
                                    operation_id: progress_claim.operation_id,
                                    progress: Some(OperationProgress {
                                        completed_bytes,
                                        total_bytes: Some(total_bytes),
                                        total_bytes_known: true,
                                        ..phase_progress("uploading")
                                    }),
                                    schema_version: 1,
                                };
                                let _ = progress_client.heartbeat(&progress).await;
                            }
                        });
                        let transfer_client = self.client.clone();
                        let transfer_operation_id = claim.operation_id;
                        let result = self
                            .client
                            .upload_recipe_image(
                                request.build_id,
                                &evidence.image_digest,
                                &evidence.oci_layout_sha256,
                                evidence.image_bytes,
                                &builder.layout_path(claim.operation_id),
                                move |bytes| {
                                    transfer_client.set_progress_bytes(
                                        transfer_operation_id,
                                        bytes,
                                        total_bytes,
                                    );
                                    sender.send_replace(bytes);
                                },
                            )
                            .await;
                        // The transfer owns the sender; finishing closes the channel.
                        // The reporter drains its last snapshot independently of transfer IO.
                        let _ = progress_task.await;
                        if let Err(error) = result {
                            return recipe_build_client_failure_result(
                                &error,
                                "image-upload",
                                "Controller did not confirm the built OCI image upload",
                            );
                        }
                        ExecutionResult {
                            state: "succeeded",
                            body: serde_json::to_value(evidence).unwrap_or_else(
                                |_| json!({"reason": "build evidence serialization failed"}),
                            ),
                        }
                    }
                    Err(error) => ExecutionResult {
                        state: "failed",
                        body: error.failure_evidence(),
                    },
                }
            }
            RecipeOperationRequest::ImageImport(request) => {
                self.report_phase(claim, "downloading").await;
                if self
                    .runtime
                    .ensure_disk_available(request.image_bytes)
                    .is_err()
                {
                    return failed("local disk capacity changed before image import");
                }
                let importer = ImageImporter {
                    data_root: self.runtime.data_root,
                };
                let archive = match importer.verified_cached_archive(&request) {
                    Ok(Some(path)) => path,
                    Ok(None) => {
                        let staging = match importer.staging_path(claim.operation_id) {
                            Ok(path) => path,
                            Err(_) => return failed("image import staging is unavailable"),
                        };
                        let mut downloaded = false;
                        for attempt in 0..3_u32 {
                            match self
                                .client
                                .download_artifact(
                                    &request.oci_layout_sha256,
                                    request.image_bytes,
                                    &staging,
                                )
                                .await
                            {
                                Ok(()) => {
                                    downloaded = true;
                                    break;
                                }
                                Err(error) if error.retryable() && attempt < 2 => {
                                    tokio::time::sleep(Duration::from_millis(
                                        100 * (attempt + 1) as u64,
                                    ))
                                    .await;
                                }
                                Err(_) => break,
                            }
                        }
                        if !downloaded {
                            return failed("exact OCI image archive is unavailable");
                        }
                        match importer.retain_verified_archive(&request, &staging) {
                            Ok(path) => path,
                            Err(_) => {
                                return failed("verified OCI image archive could not be retained");
                            }
                        }
                    }
                    Err(_) => return failed("OCI image archive cache is invalid"),
                };
                self.report_phase(claim, "verifying").await;
                match importer.verify(&request, &archive) {
                    Ok(evidence) => match self
                        .execute_host_runtime(
                            claim,
                            HostRuntimeAction::ImageImport,
                            importer.runtime_arguments(&request, &archive),
                        )
                        .await
                    {
                        Ok(()) => ExecutionResult {
                            state: "succeeded",
                            body: serde_json::to_value(evidence).unwrap_or_else(
                                |_| json!({"reason": "image import evidence serialization failed"}),
                            ),
                        },
                        Err(error) => {
                            let mut body = json!({
                                "reason": "host runtime could not import the accepted OCI image",
                            });
                            let code = image_import_helper_code(&error);
                            body["helper_error_code"] = Value::String(code);
                            ExecutionResult {
                                state: "failed",
                                body,
                            }
                        }
                    },
                    Err(error) => ExecutionResult {
                        state: "failed",
                        body: json!({"reason": error.to_string()}),
                    },
                }
            }
            RecipeOperationRequest::JobRun(request) => {
                self.report_phase(claim, "preparing").await;
                let started = Instant::now();
                let installation_id = request.installation_id.to_string();
                let job_scope = request.job_id.to_string();
                if self.runtime.recipe_digest(&installation_id).ok().as_deref()
                    != Some(&request.recipe_content_sha256)
                    || self.runtime.verify_installation(&installation_id).is_err()
                {
                    return failed_job(
                        &request,
                        1,
                        started,
                        "installed recipe identity or artifact manifest does not match",
                    );
                }
                let spec = match self.runtime.load_spec(&installation_id) {
                    Ok(spec) => spec,
                    Err(_) => {
                        return failed_job(
                            &request,
                            1,
                            started,
                            "installed recipe specification is corrupt",
                        );
                    }
                };
                let invocation = match prepare_job_invocation(&spec, &request) {
                    Ok(plan) => plan,
                    Err(_) => return failed_job(&request, 1, started, "job invocation is invalid"),
                };
                if self
                    .runtime
                    .ensure_memory_available(
                        request.reserved_memory_bytes,
                        request.memory_floor_bytes,
                        &request.memory_kind.to_string(),
                        Path::new("/proc/meminfo"),
                    )
                    .is_err()
                {
                    return failed_job(
                        &request,
                        1,
                        started,
                        "local memory capacity changed after job admission",
                    );
                }
                if *cancellation.borrow() {
                    return cancelled_job(&request, started, "controller cancellation requested");
                }
                if self.runtime.cleanup_job_scope(&job_scope).is_err() {
                    return failed_job(&request, 1, started, "prior job scope is unsafe");
                }
                // Keep the scope alive through output collection/upload, then guarantee bounded
                // local cleanup for every success, adapter failure, timeout, and transport error.
                let job_scope_cleanup = JobScopeCleanup::new(&self.runtime, &job_scope);
                for input in &request.inputs {
                    let destination = match self
                        .runtime
                        .job_input_destination(&job_scope, &input.name)
                    {
                        Ok(destination) => destination,
                        Err(_) => {
                            let _ = self.runtime.cleanup_job_scope(&job_scope);
                            return failed_job(&request, 1, started, "job input staging failed");
                        }
                    };
                    let download = run_until_cancelled(
                        self.client.download_recipe_job_input(
                            request.job_id,
                            &input.sha256,
                            u64::from(input.size_bytes),
                            &destination,
                        ),
                        &mut cancellation,
                    )
                    .await;
                    if download.is_none() {
                        if job_scope_cleanup.finish().is_err() {
                            return failed_job(
                                &request,
                                JOB_CANCEL_EXIT_CODE,
                                started,
                                "cancelled job scope cleanup failed",
                            );
                        }
                        return cancelled_job(
                            &request,
                            started,
                            "controller cancellation requested",
                        );
                    }
                    if download.is_some_and(|result| result.is_err()) {
                        let _ = self.runtime.cleanup_job_scope(&job_scope);
                        return failed_job(
                            &request,
                            1,
                            started,
                            "authorized job input is unavailable",
                        );
                    }
                }
                let input_names = request
                    .inputs
                    .iter()
                    .map(|input| input.name.clone())
                    .collect::<Vec<_>>();
                let input_manifest = match recipe_job_input_manifest(&request) {
                    Ok(bytes) => bytes,
                    Err(_) => {
                        let _ = self.runtime.cleanup_job_scope(&job_scope);
                        return failed_job(&request, 1, started, "job input manifest is invalid");
                    }
                };
                if self
                    .runtime
                    .write_job_input_manifest(
                        &job_scope,
                        &input_names,
                        &input_manifest,
                        &request.input_manifest_sha256,
                    )
                    .is_err()
                {
                    let _ = self.runtime.cleanup_job_scope(&job_scope);
                    return failed_job(
                        &request,
                        1,
                        started,
                        "job input staging is not same-run exact",
                    );
                }
                let placement = match job_placement(&invocation, &request) {
                    Ok(placement) => placement,
                    Err(_) => {
                        return failed_job(
                            &request,
                            1,
                            started,
                            "job placement does not match the installed workload",
                        );
                    }
                };
                let plan = match self.runtime.prepare_job_start(
                    &spec,
                    &installation_id,
                    &job_scope,
                    &placement,
                    &invocation,
                ) {
                    Ok(plan) => plan,
                    Err(_) => {
                        let _ = self.runtime.cleanup_job_scope(&job_scope);
                        return failed_job(
                            &request,
                            1,
                            started,
                            "container runtime could not prepare the job",
                        );
                    }
                };
                for hook in &plan.pre_start {
                    let arguments = runtime_arguments_for_plan(&plan, hook);
                    if self
                        .execute_host_runtime(claim, HostRuntimeAction::Start, arguments)
                        .await
                        .is_err()
                    {
                        let _ = self.runtime.complete_stop(&job_scope);
                        let _ = self.runtime.cleanup_job_scope(&job_scope);
                        return failed_job(
                            &request,
                            1,
                            started,
                            "container runtime pre-start hook failed",
                        );
                    }
                }
                let mut arguments = vec![
                    plan.archive_sha256,
                    plan.registry_index_digest,
                    plan.platform_manifest_digest,
                    plan.image_reference,
                ];
                arguments.extend(plan.main);
                let outcome = run_interruptible_job(
                    self.execute_host_runtime_outcome(claim, HostRuntimeAction::Start, arguments),
                    &mut cancellation,
                    || {
                        self.execute_host_runtime(
                            claim,
                            HostRuntimeAction::Stop,
                            vec![
                                job_scope.clone(),
                                JOB_CANCEL_STOP_TIMEOUT_SECONDS.to_string(),
                                "job-cancel".to_owned(),
                            ],
                        )
                    },
                )
                .await;
                let outcome = match outcome {
                    InterruptibleJob::Completed(outcome) => outcome,
                    InterruptibleJob::Cancelled { stopped: true } => {
                        let _ = self.runtime.complete_stop(&job_scope);
                        if job_scope_cleanup.finish().is_err() {
                            return failed_job(
                                &request,
                                JOB_CANCEL_EXIT_CODE,
                                started,
                                "cancelled job scope cleanup failed",
                            );
                        }
                        return cancelled_job(
                            &request,
                            started,
                            "controller cancellation requested",
                        );
                    }
                    InterruptibleJob::Cancelled { stopped: false } => {
                        job_scope_cleanup.retain();
                        return ExecutionResult {
                            state: "waiting-for-operator",
                            body: job_result_body(
                                &request,
                                JOB_CANCEL_EXIT_CODE,
                                started,
                                empty_job_output_manifest(),
                                Some("controller cancellation could not stop the active job"),
                            ),
                        };
                    }
                };
                if outcome.as_ref().is_ok_and(|outcome| outcome.stop_uncertain) {
                    job_scope_cleanup.retain();
                    return ExecutionResult {
                        state: "waiting-for-operator",
                        body: job_result_body(
                            &request,
                            JOB_CANCEL_EXIT_CODE,
                            started,
                            empty_job_output_manifest(),
                            Some("job runtime could not be stopped safely"),
                        ),
                    };
                }
                if outcome.is_err() {
                    job_scope_cleanup.retain();
                    return ExecutionResult {
                        state: "waiting-for-operator",
                        body: job_result_body(
                            &request,
                            JOB_CANCEL_EXIT_CODE,
                            started,
                            empty_job_output_manifest(),
                            Some("job runtime execution or cleanup state is uncertain"),
                        ),
                    };
                }
                let (exit_code, exit_reason) = match outcome {
                    Ok(outcome) => match outcome.exit_code {
                        Some(0) => (0, None),
                        Some(124) => (124, Some("job adapter exceeded its deadline")),
                        Some(code) if (0..=255).contains(&code) => (
                            u32::try_from(code).expect("nonnegative process exit status"),
                            Some("job adapter exited unsuccessfully"),
                        ),
                        Some(_) => {
                            return failed_job(
                                &request,
                                1,
                                started,
                                "job adapter reported an invalid exit status",
                            );
                        }
                        None => (1, Some("job adapter did not report an exit status")),
                    },
                    Err(_) => unreachable!("runtime errors return operator-waiting above"),
                };
                let _ = self.runtime.complete_stop(&job_scope);
                if *cancellation.borrow() {
                    if job_scope_cleanup.finish().is_err() {
                        return failed_job(
                            &request,
                            JOB_CANCEL_EXIT_CODE,
                            started,
                            "cancelled job scope cleanup failed",
                        );
                    }
                    return cancelled_job(&request, started, "controller cancellation requested");
                }
                let output_manifest = match collect_job_outputs(
                    self.runtime.job_output_root(&job_scope).ok().as_deref(),
                    &request.output_limits,
                    &request.output_mappings,
                ) {
                    Ok(manifest) => manifest,
                    Err(reason) => {
                        let _ = self.runtime.cleanup_job_scope(&job_scope);
                        return failed_job(&request, exit_code.max(1), started, reason);
                    }
                };
                for output in &output_manifest.files {
                    let path = self
                        .runtime
                        .job_output_root(&job_scope)
                        .unwrap()
                        .join(&output.name);
                    let upload = run_until_cancelled(
                        self.client.upload_recipe_job_output(
                            request.job_id,
                            &output.name,
                            &output.media_type,
                            &output.sha256,
                            u64::from(output.size_bytes),
                            &path,
                        ),
                        &mut cancellation,
                    )
                    .await;
                    if upload.is_none() {
                        if job_scope_cleanup.finish().is_err() {
                            return failed_job(
                                &request,
                                JOB_CANCEL_EXIT_CODE,
                                started,
                                "cancelled job scope cleanup failed",
                            );
                        }
                        return cancelled_job(
                            &request,
                            started,
                            "controller cancellation requested",
                        );
                    }
                    if upload.is_some_and(|result| result.is_err()) {
                        let _ = self.runtime.cleanup_job_scope(&job_scope);
                        return failed_job(
                            &request,
                            exit_code.max(1),
                            started,
                            "job output upload failed",
                        );
                    }
                }
                if *cancellation.borrow() {
                    if job_scope_cleanup.finish().is_err() {
                        return failed_job(
                            &request,
                            JOB_CANCEL_EXIT_CODE,
                            started,
                            "cancelled job scope cleanup failed",
                        );
                    }
                    return cancelled_job(&request, started, "controller cancellation requested");
                }
                let body =
                    job_result_body(&request, exit_code, started, output_manifest, exit_reason);
                if job_scope_cleanup.finish().is_err() {
                    return failed_job(
                        &request,
                        exit_code.max(1),
                        started,
                        "job scope cleanup failed",
                    );
                }
                ExecutionResult {
                    state: if exit_code == 0 {
                        "succeeded"
                    } else {
                        "failed"
                    },
                    body,
                }
            }
            RecipeOperationRequest::Install(request) => {
                self.report_phase(claim, "installing").await;
                if *cancellation.borrow() {
                    return cancelled("controller cancelled before installation began");
                }
                let inline_spec = request.compiled_execution_plan.clone();
                if inline_spec.validate().is_err() {
                    return failed("compiled execution plan is invalid");
                }
                let spec = match self
                    .client
                    .recipe_spec(&request.installation_id.to_string())
                    .await
                {
                    Ok(spec) => spec,
                    Err(_) => return failed("digest-bound recipe specification is unavailable"),
                };
                if spec != inline_spec
                    || spec.topology.role != request.role
                    || spec.topology.rank != request.rank
                {
                    return failed("compiled execution plan does not match the accepted install");
                }
                if spec.identity.recipe_revision_sha256.is_empty()
                    || spec.topology.role != request.role
                {
                    return failed("recipe specification does not match the accepted install");
                }
                if self
                    .execute_host_runtime(
                        claim,
                        HostRuntimeAction::ImageInspect,
                        vec![
                            spec.runtime_image.oci_layout_sha256.clone(),
                            spec.runtime_image
                                .registry_manifest_digest
                                .clone()
                                .unwrap_or_else(|| {
                                    spec.runtime_image.platform_manifest_digest.clone()
                                }),
                            spec.runtime_image.platform_manifest_digest.clone(),
                            spec.runtime_image.local_image_reference(),
                            spec.security.user.clone(),
                        ],
                    )
                    .await
                    .is_err()
                {
                    return failed("accepted container image is unavailable to the host runtime");
                }
                if *cancellation.borrow() {
                    return cancelled("controller cancelled before model installation began");
                }
                match self.runtime.install_with_space_check(
                    &spec,
                    &request.installation_id.to_string(),
                    &spec.identity.recipe_revision_sha256,
                    request.expected_bytes,
                ) {
                    Ok(()) => {}
                    Err(OciError::Capacity) => {
                        return failed("local disk capacity changed after install admission");
                    }
                    Err(error) => {
                        let (stage, category) = error.safe_install_context();
                        return failed_owned(format!(
                            "recipe artifacts or container image could not be installed (stage={stage}; category={category})"
                        ));
                    }
                }
                // A failed measurement is not evidence that the admitted
                // payload is present.  Substituting ``expected_bytes`` (the
                // Controller's disk reservation) would report the reservation
                // as a measured tree and make an unmeasured install look
                // complete, so the operation fails instead.
                let installed_bytes = match self
                    .runtime
                    .installed_bytes(&request.installation_id.to_string())
                {
                    Ok(bytes) => bytes,
                    Err(error) => {
                        let (stage, category) = error.safe_install_context();
                        return failed_owned(format!(
                            "installed payload could not be measured after installation (stage={stage}; category={category})"
                        ));
                    }
                };
                if *cancellation.borrow() {
                    return cancelled(
                        "controller cancellation observed after installation settled",
                    );
                }
                ExecutionResult {
                    state: "succeeded",
                    body: recipe_install_success_body(installed_bytes),
                }
            }
            RecipeOperationRequest::Reconcile(request) => {
                self.report_phase(claim, "reconciling-installation").await;
                if *cancellation.borrow() {
                    return cancelled("controller cancelled before installation reconciliation");
                }
                let identity = RecipeReconciliationIdentity::from(&request);
                let prepared = match self.runtime.prepare_reconciliation(&identity) {
                    Ok(progress) => progress,
                    Err(OciError::ReconciliationBusy) => {
                        return temporary_reconciliation_failure(
                            "installation-reconciliation-lock",
                            "installation_reconciliation_busy",
                            "installation_reconciliation_busy",
                        );
                    }
                    Err(error) if retryable_reconciliation_storage_error(&error) => {
                        return temporary_reconciliation_failure(
                            "installation-checkpoint-storage",
                            "recipe_reconciliation_dependency_unavailable",
                            "installation_storage_temporarily_unavailable",
                        );
                    }
                    Err(error) => {
                        return failed_stage(
                            "managed installation does not match the reconciliation authority",
                            "installation-validation",
                            error.safe_category(),
                        );
                    }
                };
                if prepared.complete && prepared.cleanup_receipt_sha256.is_none() {
                    return failed_stage(
                        "completed installation cleanup has no durable receipt",
                        "installation-receipt",
                        "receipt-missing",
                    );
                }
                // The local receipt is useful after an agent restart, but each
                // attempt still obtains a fresh Controller-signed helper grant.
                // The helper receipt proves there are no exact or unclassified
                // managed containers before the installation tree is removed.
                if *cancellation.borrow() {
                    return cancelled(
                        "controller cancelled after reconciliation checkpoint preparation",
                    );
                }
                if let Err(error) = self
                    .reconcile_installation_runtime(claim, identity.clone())
                    .await
                {
                    if matches!(
                        error,
                        crate::host_runtime::HostRuntimeError::HelperRejected { ref code, .. }
                            if code == "installation_reconciliation_busy"
                    ) {
                        return temporary_reconciliation_failure(
                            "helper-runtime-reconciliation-lock",
                            "installation_reconciliation_busy",
                            "installation_reconciliation_busy",
                        );
                    }
                    if temporary_observation_error(&error) {
                        return temporary_reconciliation_failure(
                            "helper-runtime-reconciliation",
                            "recipe_reconciliation_dependency_unavailable",
                            error.preflight_code(),
                        );
                    }
                    return failed_stage_owned(
                        "managed runtime effects could not be reconciled for installation removal",
                        "helper-runtime-reconciliation",
                        error.preflight_code(),
                    );
                }
                if *cancellation.borrow() {
                    return cancelled(
                        "controller cancelled after runtime reconciliation; removal is resumable",
                    );
                }
                let completed = match self.runtime.finalize_reconciliation(&identity) {
                    Ok(progress) => progress,
                    Err(OciError::ReconciliationBusy) => {
                        return temporary_reconciliation_failure(
                            "installation-reconciliation-lock",
                            "installation_reconciliation_busy",
                            "installation_reconciliation_busy",
                        );
                    }
                    Err(error) if retryable_reconciliation_storage_error(&error) => {
                        return temporary_reconciliation_failure(
                            "installation-checkpoint-storage",
                            "recipe_reconciliation_dependency_unavailable",
                            "installation_storage_temporarily_unavailable",
                        );
                    }
                    Err(error) => {
                        return failed_stage(
                            "reconciled installation cleanup could not be completed",
                            "installation-removal",
                            error.safe_category(),
                        );
                    }
                };
                let Some(cleanup_receipt_sha256) = completed.cleanup_receipt_sha256 else {
                    return failed_stage(
                        "installation cleanup completed without a durable receipt",
                        "installation-receipt",
                        "receipt-missing",
                    );
                };
                if !completed.complete {
                    return failed_stage(
                        "installation cleanup did not reach its durable terminal state",
                        "installation-receipt",
                        "receipt-incomplete",
                    );
                }
                ExecutionResult {
                    state: "succeeded",
                    body: recipe_reconcile_success_body(
                        &identity,
                        completed.removed_bytes,
                        cleanup_receipt_sha256,
                    ),
                }
            }
            RecipeOperationRequest::Start(request) => {
                self.report_phase(claim, "starting").await;
                let installation_id = request.installation_id.to_string();
                let phase_deadline = match request
                    .start_deadline
                    .as_deref()
                    .map(DateTime::parse_from_rfc3339)
                    .transpose()
                {
                    Ok(deadline) => deadline,
                    Err(_) => return failed("recipe start deadline is invalid"),
                };
                let spec = request.compiled_execution_plan.clone();
                if spec.validate().is_err() {
                    return failed("compiled execution plan is invalid");
                }
                if spec.identity.recipe_revision_sha256 != request.recipe_content_sha256
                    || spec.runtime.image_digest != request.image_digest
                    || spec.topology.rank != request.rank
                    || spec.topology.role != request.role
                    || spec.topology.world_size != request.world_size
                    || spec.runtime.placement.rank != request.rank
                    || spec.runtime.placement.role != request.role
                    || spec.runtime.placement.world_size != request.world_size
                    || spec.runtime.placement.port != Some(request.port)
                    || spec.runtime.placement.reserved_memory_bytes != request.reserved_memory_bytes
                    || spec.runtime.placement.memory_floor_bytes != request.memory_floor_bytes
                    || spec.runtime.placement.memory_kind.to_string()
                        != request.memory_kind.to_string()
                    || spec.runtime.placement.local_address != request.local_address
                    || spec.runtime.placement.master_address != request.master_address
                    || spec.runtime.placement.master_port != request.master_port
                    || (spec.runtime.placement.endpoint_address.is_some()
                        && spec.runtime.placement.endpoint_address
                            != Some(request.endpoint_address))
                    || (spec.runtime.placement.endpoint_address.is_none()
                        && request.world_size > 1
                        && request.local_address != Some(request.endpoint_address))
                {
                    return failed("compiled execution plan does not match start identity");
                }
                if self.runtime.recipe_digest(&installation_id).ok().as_deref()
                    != Some(&request.recipe_content_sha256)
                    || self.runtime.verify_installation(&installation_id).is_err()
                {
                    return failed("installed recipe identity or artifact manifest does not match");
                }
                let installed_spec = match self.runtime.load_spec(&installation_id) {
                    Ok(spec) => spec,
                    Err(_) => return failed("installed recipe specification is corrupt"),
                };
                if !same_installed_workload(&installed_spec, &spec) {
                    return failed("start plan does not match installed workload identity");
                }
                let Some(endpoint) = spec.endpoint.as_ref() else {
                    return failed("installed recipe is not a persistent service");
                };
                let placement = spec.runtime.placement.clone();
                let run_id = request.run_id.to_string();
                let inspection_identity =
                    request
                        .run_generation
                        .map(|run_generation| RecipeRunStartIdentity {
                            mapping_generation: request.mapping_generation,
                            mapping_id: request.mapping_id,
                            recipe_content_sha256: request.recipe_content_sha256.clone(),
                            recipe_revision_id: request.recipe_revision_id,
                            run_generation,
                        });
                let collective_readiness =
                    matches!(request.phase, Some(RecipeStartPhase::CollectiveReadiness));
                let rank_launch = matches!(request.phase, Some(RecipeStartPhase::RankLaunch));
                if request.phase.is_some()
                    && !before_phase_deadline(&lease_deadline, phase_deadline.as_ref())
                {
                    return failed("distributed start deadline elapsed before execution");
                }
                // A previous agent may have completed the Docker start before
                // its result was acknowledged. Retained lifecycle identity is
                // read without resetting writable state or replaying hooks.
                let retained_plan = if collective_readiness {
                    None
                } else {
                    match self.runtime.prepare_retained_start_if_present(
                        &spec,
                        &installation_id,
                        &run_id,
                        &placement,
                        inspection_identity.as_ref(),
                    ) {
                        Ok(plan) => plan,
                        Err(crate::oci::OciError::Io(error))
                            if error.kind() != std::io::ErrorKind::PermissionDenied =>
                        {
                            return temporary_runtime_observation_failure();
                        }
                        Err(_) => {
                            return waiting_for_operator(
                                "retained workload identity does not match the authorized start",
                            );
                        }
                    }
                };
                let retained_existing = retained_plan.is_some();
                let plan = if let Some(plan) = retained_plan {
                    plan
                } else if collective_readiness {
                    match inspection_identity.as_ref().map_or_else(
                        || {
                            self.runtime.prepare_retained_start(
                                &spec,
                                &installation_id,
                                &run_id,
                                &placement,
                            )
                        },
                        |identity| {
                            self.runtime
                                .prepare_retained_start_with_inspection_identity(
                                    &spec,
                                    &installation_id,
                                    &run_id,
                                    &placement,
                                    identity,
                                )
                        },
                    ) {
                        Ok(plan) => plan,
                        Err(_) => {
                            return failed(
                                "retained workload identity does not match collective readiness",
                            );
                        }
                    }
                } else {
                    if self
                        .runtime
                        .ensure_memory_available(
                            request.reserved_memory_bytes,
                            request.memory_floor_bytes,
                            &request.memory_kind.to_string(),
                            Path::new("/proc/meminfo"),
                        )
                        .is_err()
                    {
                        return failed("local memory capacity changed after run admission");
                    }
                    match inspection_identity.as_ref().map_or_else(
                        || {
                            self.runtime
                                .prepare_start(&spec, &installation_id, &run_id, &placement)
                        },
                        |identity| {
                            self.runtime.prepare_start_with_inspection_identity(
                                &spec,
                                &installation_id,
                                &run_id,
                                &placement,
                                identity,
                            )
                        },
                    ) {
                        Ok(plan) => plan,
                        Err(error) => {
                            return runtime_preparation_failure(&error);
                        }
                    }
                };
                if collective_readiness && !plan.pre_start.is_empty() {
                    return failed("retained workload unexpectedly contains start hooks");
                }
                if *cancellation.borrow() {
                    return self
                        .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                        .await;
                }
                for hook in &plan.pre_start {
                    let arguments = runtime_arguments_for_plan(&plan, hook);
                    let mut cancellation_observer = cancellation.clone();
                    match run_until_cancelled(
                        self.execute_host_runtime(claim, HostRuntimeAction::Start, arguments),
                        &mut cancellation_observer,
                    )
                    .await
                    {
                        None => {
                            return self
                                .cancel_start_run(
                                    claim,
                                    &run_id,
                                    spec.lifecycle.stop_timeout_seconds,
                                )
                                .await;
                        }
                        Some(Err(error)) => {
                            if *cancellation.borrow() {
                                return self
                                    .cancel_start_run(
                                        claim,
                                        &run_id,
                                        spec.lifecycle.stop_timeout_seconds,
                                    )
                                    .await;
                            }
                            let _ = self.runtime.complete_stop(&run_id);
                            return runtime_failure(
                                "container runtime pre-start hook failed",
                                &error,
                            );
                        }
                        Some(Ok(())) => {}
                    }
                }
                let arguments = runtime_arguments_for_plan(&plan, &plan.main);
                let runtime_guard_arguments = arguments.clone();
                let mut acl_transition = if collective_readiness || retained_existing {
                    None
                } else {
                    match self
                        .runtime
                        .begin_installation_acl_transition(&installation_id)
                    {
                        Ok(transition) => Some(transition),
                        Err(_) => {
                            return failed("installed model custody changed before runtime start");
                        }
                    }
                };
                let mut cancellation_observer = cancellation.clone();
                let runtime_action = if collective_readiness || retained_existing {
                    HostRuntimeAction::RunInspect
                } else {
                    HostRuntimeAction::Start
                };
                let runtime_arguments = if collective_readiness || retained_existing {
                    runtime_guard_arguments.clone()
                } else {
                    arguments
                };
                let mut runtime_result = run_until_cancelled(
                    self.execute_host_runtime(claim, runtime_action, runtime_arguments),
                    &mut cancellation_observer,
                )
                .await;
                if retained_existing
                    && matches!(
                        &runtime_result,
                        Some(Err(crate::host_runtime::HostRuntimeError::HelperRejected { code, .. }))
                            if code == "runtime_run_missing"
                    )
                    && spec.lifecycle.pre_start.is_empty()
                {
                    // The retained plan and an independent Docker listing prove
                    // this exact run never reached a running container. With no
                    // pre-start hooks there is no ambiguous one-shot effect.
                    if self
                        .runtime
                        .ensure_memory_available(
                            request.reserved_memory_bytes,
                            request.memory_floor_bytes,
                            &request.memory_kind.to_string(),
                            Path::new("/proc/meminfo"),
                        )
                        .is_err()
                    {
                        return failed("local memory capacity changed after run admission");
                    }
                    acl_transition = match self
                        .runtime
                        .begin_installation_acl_transition(&installation_id)
                    {
                        Ok(transition) => Some(transition),
                        Err(_) => {
                            return failed(
                                "installed model custody changed before resumed runtime start",
                            );
                        }
                    };
                    runtime_result = run_until_cancelled(
                        self.execute_host_runtime(
                            claim,
                            HostRuntimeAction::Start,
                            runtime_guard_arguments.clone(),
                        ),
                        &mut cancellation_observer,
                    )
                    .await;
                }
                match runtime_result {
                    None => {
                        let stopped = self
                            .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                            .await;
                        if let Some(transition) = acl_transition.take()
                            && self
                                .runtime
                                .finish_installation_acl_transition(&installation_id, transition)
                                .is_err()
                        {
                            return waiting_for_operator(
                                "cancelled workload model custody remains unconfirmed",
                            );
                        }
                        return stopped;
                    }
                    Some(Err(error)) => {
                        if *cancellation.borrow() {
                            let stopped = self
                                .cancel_start_run(
                                    claim,
                                    &run_id,
                                    spec.lifecycle.stop_timeout_seconds,
                                )
                                .await;
                            if let Some(transition) = acl_transition.take()
                                && self
                                    .runtime
                                    .finish_installation_acl_transition(
                                        &installation_id,
                                        transition,
                                    )
                                    .is_err()
                            {
                                return waiting_for_operator(
                                    "cancelled workload model custody remains unconfirmed",
                                );
                            }
                            return stopped;
                        }
                        if retained_existing {
                            if temporary_observation_error(&error) {
                                return temporary_runtime_observation_failure();
                            }
                            // A foreign or uninspectable exact container, or
                            // ambiguous pre-start hook, requires reconciliation.
                            return waiting_for_operator(
                                "retained workload runtime effect could not be confirmed",
                            );
                        }
                        if !collective_readiness
                            && let Err(uncertain) = self
                                .stop_start_run(
                                    claim,
                                    &run_id,
                                    spec.lifecycle.stop_timeout_seconds,
                                    false,
                                )
                                .await
                        {
                            return uncertain;
                        }
                        return runtime_failure(
                            if collective_readiness {
                                "collective workload is not running with exact identity"
                            } else {
                                "container runtime could not start the workload"
                            },
                            &error,
                        );
                    }
                    Some(Ok(())) => {}
                }
                if let Some(transition) = acl_transition.take()
                    && self
                        .runtime
                        .finish_installation_acl_transition(&installation_id, transition)
                        .is_err()
                {
                    if let Err(uncertain) = self
                        .stop_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds, false)
                        .await
                    {
                        return uncertain;
                    }
                    return failed("installed model custody changed during runtime start");
                }
                if *cancellation.borrow() {
                    return self
                        .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                        .await;
                }
                if rank_launch {
                    let first_inspect = self
                        .execute_host_runtime(
                            claim,
                            HostRuntimeAction::RunInspect,
                            runtime_guard_arguments.clone(),
                        )
                        .await;
                    let mut launch_failure = first_inspect.err();
                    let stable = if launch_failure.is_some()
                        || *cancellation.borrow()
                        || !before_phase_deadline(&lease_deadline, phase_deadline.as_ref())
                    {
                        false
                    } else if wait_for_launch_stability(
                        lease_deadline.clone(),
                        cancellation.clone(),
                        phase_deadline,
                        Duration::from_secs(2),
                    )
                    .await
                    {
                        launch_failure = self
                            .execute_host_runtime(
                                claim,
                                HostRuntimeAction::RunInspect,
                                runtime_guard_arguments.clone(),
                            )
                            .await
                            .err();
                        launch_failure.is_none()
                            && before_phase_deadline(&lease_deadline, phase_deadline.as_ref())
                    } else {
                        false
                    };
                    if !stable {
                        if *cancellation.borrow() {
                            return self
                                .cancel_start_run(
                                    claim,
                                    &run_id,
                                    spec.lifecycle.stop_timeout_seconds,
                                )
                                .await;
                        }
                        if let Err(uncertain) = self
                            .stop_start_run(
                                claim,
                                &run_id,
                                spec.lifecycle.stop_timeout_seconds,
                                false,
                            )
                            .await
                        {
                            return uncertain;
                        }
                        return match launch_failure {
                            Some(error) => runtime_failure(
                                "rank process did not remain stable after launch",
                                &error,
                            ),
                            None => failed("rank process did not remain stable after launch"),
                        };
                    }
                    let artifact_set_digest =
                        match self.runtime.artifact_set_digest(&installation_id) {
                            Ok(digest) => digest,
                            Err(_) => {
                                if let Err(uncertain) = self
                                    .stop_start_run(
                                        claim,
                                        &run_id,
                                        spec.lifecycle.stop_timeout_seconds,
                                        false,
                                    )
                                    .await
                                {
                                    return uncertain;
                                }
                                return failed("rank launch evidence is unavailable");
                            }
                        };
                    let body = match recipe_start_success_body(
                        &request,
                        &spec,
                        &artifact_set_digest,
                        &runtime_guard_arguments,
                    ) {
                        Ok(body) => body,
                        Err(_) => {
                            if let Err(uncertain) = self
                                .stop_start_run(
                                    claim,
                                    &run_id,
                                    spec.lifecycle.stop_timeout_seconds,
                                    false,
                                )
                                .await
                            {
                                return uncertain;
                            }
                            return failed("rank launch evidence is unavailable");
                        }
                    };
                    if *cancellation.borrow() {
                        return self
                            .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                            .await;
                    }
                    return ExecutionResult {
                        state: "succeeded",
                        body,
                    };
                }
                let runtime_guard = async {
                    loop {
                        tokio::time::sleep(Duration::from_secs(10)).await;
                        self.execute_host_runtime(
                            claim,
                            HostRuntimeAction::RunInspect,
                            runtime_guard_arguments.clone(),
                        )
                        .await?;
                    }
                };
                let ready = match if collective_readiness {
                    wait_ready_with_runtime_guard_and_cancellation(
                        wait_ready_until(
                            request.endpoint_address,
                            request.port,
                            &endpoint.health_path,
                            lease_deadline,
                            phase_deadline,
                        ),
                        runtime_guard,
                        cancellation.clone(),
                    )
                    .await
                } else {
                    wait_ready_with_runtime_guard_and_cancellation(
                        wait_ready(
                            request.endpoint_address,
                            request.port,
                            &endpoint.health_path,
                            lease_deadline,
                        ),
                        runtime_guard,
                        cancellation.clone(),
                    )
                    .await
                } {
                    ReadinessOutcome::Ready => true,
                    ReadinessOutcome::Cancelled | ReadinessOutcome::Deadline => false,
                    ReadinessOutcome::GuardFailed(error) => {
                        // The runtime could not be inspected, so the effect cannot
                        // be bound.  Name that instead of reporting a readiness
                        // deadline the workload never reached: a temporary
                        // inspection failure is the code the Controller already
                        // retries for a start, so it becomes self-healing, and any
                        // other rejection carries the captured container output
                        // the inspection gate admits.
                        if temporary_observation_error(&error) {
                            return temporary_runtime_observation_failure();
                        }
                        return runtime_observation_failure(&error);
                    }
                };
                if !ready {
                    if *cancellation.borrow() {
                        return self
                            .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                            .await;
                    }
                    if !collective_readiness
                        && let Err(uncertain) = self
                            .stop_start_run(
                                claim,
                                &run_id,
                                spec.lifecycle.stop_timeout_seconds,
                                false,
                            )
                            .await
                    {
                        return uncertain;
                    }
                    return failed("workload did not become ready before its deadline");
                }
                if collective_readiness {
                    let artifact_set_digest =
                        match self.runtime.artifact_set_digest(&installation_id) {
                            Ok(digest) => digest,
                            Err(_) => {
                                return failed("collective readiness evidence is unavailable");
                            }
                        };
                    let body = match recipe_start_success_body(
                        &request,
                        &spec,
                        &artifact_set_digest,
                        &runtime_guard_arguments,
                    ) {
                        Ok(body) => body,
                        Err(_) => return failed("collective readiness evidence is unavailable"),
                    };
                    if *cancellation.borrow() {
                        return self
                            .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                            .await;
                    }
                    return ExecutionResult {
                        state: "succeeded",
                        body,
                    };
                }
                let artifact_set_digest = match self.runtime.artifact_set_digest(&installation_id) {
                    Ok(digest) => digest,
                    Err(_) => return failed("readiness evidence is unavailable"),
                };
                let body = match recipe_start_success_body(
                    &request,
                    &spec,
                    &artifact_set_digest,
                    &runtime_guard_arguments,
                ) {
                    Ok(body) => body,
                    Err(_) => return failed("readiness evidence is unavailable"),
                };
                if *cancellation.borrow() {
                    return self
                        .cancel_start_run(claim, &run_id, spec.lifecycle.stop_timeout_seconds)
                        .await;
                }
                ExecutionResult {
                    state: "succeeded",
                    body,
                }
            }
            RecipeOperationRequest::Stop(request) => {
                self.report_phase(claim, "stopping").await;
                let run_id = request.run_id.to_string();
                let plan = match self.runtime.prepare_stop(&run_id) {
                    Ok(plan) => plan,
                    Err(_) => return failed("container runtime could not prepare workload stop"),
                };
                let mut cancel_remove = plan.remove.clone();
                cancel_remove.push("job-cancel".to_owned());
                let fenced_stop = request.cancel_pending_start || *cancellation.borrow();
                let remove = if fenced_stop {
                    cancel_remove.clone()
                } else {
                    plan.remove
                };
                if self
                    .execute_host_runtime(claim, HostRuntimeAction::Stop, remove)
                    .await
                    .is_err()
                {
                    waiting_for_operator("container runtime stop remains unconfirmed")
                } else {
                    if !plan.post_stop.is_empty() {
                        match self.runtime.begin_post_stop_hooks(&run_id) {
                            Ok(()) => {}
                            Err(crate::oci::OciError::PostStopHooksStarted) => {
                                return waiting_for_operator(
                                    "post-stop hook effect may already have been applied",
                                );
                            }
                            Err(crate::oci::OciError::Io(_)) => {
                                return failed("post-stop hook marker could not be persisted");
                            }
                            Err(_) => return failed("post-stop hook metadata is invalid"),
                        }
                    }
                    if let (
                        Some(archive_sha256),
                        Some(registry_index_digest),
                        Some(platform_manifest_digest),
                        Some(image_reference),
                    ) = (
                        plan.archive_sha256,
                        plan.registry_index_digest,
                        plan.platform_manifest_digest,
                        plan.image_reference,
                    ) {
                        for hook in plan.post_stop {
                            if *cancellation.borrow() {
                                break;
                            }
                            let mut arguments = vec![
                                archive_sha256.clone(),
                                registry_index_digest.clone(),
                                platform_manifest_digest.clone(),
                                image_reference.clone(),
                            ];
                            arguments.extend(hook);
                            if self
                                .execute_host_runtime(claim, HostRuntimeAction::Stop, arguments)
                                .await
                                .is_err()
                            {
                                return failed("container runtime post-stop hook failed");
                            }
                        }
                    }
                    if *cancellation.borrow()
                        && !fenced_stop
                        && self
                            .execute_host_runtime(claim, HostRuntimeAction::Stop, cancel_remove)
                            .await
                            .is_err()
                    {
                        return waiting_for_operator("cancelled workload stop remains unconfirmed");
                    }
                    if self.runtime.complete_stop(&run_id).is_err() {
                        return waiting_for_operator(
                            "container runtime stop metadata remains unconfirmed",
                        );
                    }
                    if *cancellation.borrow() {
                        ExecutionResult {
                            state: "cancelled",
                            body: json!({"reason": "controller cancellation confirmed after exact workload stop", "error_code": "operation_cancelled"}),
                        }
                    } else {
                        ExecutionResult {
                            state: "succeeded",
                            body: recipe_stop_success_body(),
                        }
                    }
                }
            }
            RecipeOperationRequest::Uninstall(request) => {
                self.report_phase(claim, "uninstalling").await;
                if *cancellation.borrow() {
                    return cancelled("controller cancelled before uninstallation began");
                }
                let installation_uuid = request.installation_id;
                let installation_id = installation_uuid.to_string();
                match self.runtime.recipe_digest_if_present(&installation_id) {
                    Ok(None) => {
                        if *cancellation.borrow() {
                            return cancelled(
                                "controller cancellation observed with installation absent",
                            );
                        }
                        return ExecutionResult {
                            state: "succeeded",
                            body: recipe_uninstall_success_body(0),
                        };
                    }
                    Ok(Some(recipe_digest)) if recipe_digest == request.recipe_content_sha256 => {}
                    Ok(Some(_)) | Err(_) => {
                        return failed(
                            "installed recipe identity does not match uninstall request",
                        );
                    }
                }
                let removed_model_bytes = match request.cleanup_model_content_sha256 {
                    Some(model_content_sha256) => {
                        self.runtime.validate_uninstall_with_model_cleanup(
                            &installation_id,
                            &request.recipe_content_sha256,
                            &model_content_sha256,
                        )
                    }
                    None => self
                        .runtime
                        .validate_uninstall(&installation_id, &request.recipe_content_sha256)
                        .map(|()| 0),
                };
                let removed_model_bytes = match removed_model_bytes {
                    Ok(bytes) => bytes,
                    Err(error) => {
                        return failed_stage(
                            "installed recipe could not be safely removed",
                            "installation-validation",
                            error.safe_category(),
                        );
                    }
                };
                if *cancellation.borrow() {
                    return cancelled("controller cancelled before installation cleanup began");
                }
                match self.runtime.runtime_cache_present(&installation_id) {
                    Ok(false) => {}
                    Ok(true) => {
                        if let Err(error) = self
                            .cleanup_installation_cache(claim, installation_uuid)
                            .await
                        {
                            return failed_stage_owned(
                                "installed recipe could not be safely removed",
                                "runtime-cache-cleanup",
                                error.preflight_code(),
                            );
                        }
                    }
                    Err(error) => {
                        return failed_stage(
                            "installed recipe could not be safely removed",
                            "runtime-cache-cleanup",
                            error.safe_category(),
                        );
                    }
                }
                if *cancellation.borrow() {
                    return cancelled(
                        "controller cancellation observed after runtime cache cleanup",
                    );
                }
                if let Err(error) = self
                    .runtime
                    .finalize_uninstall(&installation_id, &request.recipe_content_sha256)
                {
                    return failed_stage(
                        "installed recipe could not be safely removed",
                        "installation-removal",
                        error.safe_category(),
                    );
                }
                if *cancellation.borrow() {
                    return cancelled(
                        "controller cancellation observed after uninstallation settled",
                    );
                }
                ExecutionResult {
                    state: "succeeded",
                    body: recipe_uninstall_success_body(removed_model_bytes),
                }
            }
        }
    }
}

fn failed(reason: &'static str) -> ExecutionResult {
    ExecutionResult {
        state: "failed",
        body: json!({"reason": reason}),
    }
}

fn cancelled(reason: &'static str) -> ExecutionResult {
    ExecutionResult {
        state: "cancelled",
        body: json!({"reason": reason, "error_code": "operation_cancelled"}),
    }
}

fn temporary_reconciliation_failure(
    stage: &'static str,
    error_code: &'static str,
    diagnostic: impl Into<String>,
) -> ExecutionResult {
    ExecutionResult {
        state: "failed",
        body: json!({
            "diagnostic": diagnostic.into(),
            "error_code": error_code,
            "failure_kind": "temporary-dependency",
            "reason": "installation reconciliation is waiting for its local owner",
            "retry_after_seconds": 2,
            "stage": stage,
        }),
    }
}

fn retryable_reconciliation_storage_error(error: &OciError) -> bool {
    let OciError::Io(error) = error else {
        return false;
    };
    matches!(
        error.kind(),
        std::io::ErrorKind::Interrupted
            | std::io::ErrorKind::WouldBlock
            | std::io::ErrorKind::TimedOut
    ) || error.raw_os_error().is_some_and(|code| {
        code == rustix::io::Errno::IO.raw_os_error()
            || code == rustix::io::Errno::NOSPC.raw_os_error()
    })
}

fn waiting_for_operator(reason: &'static str) -> ExecutionResult {
    ExecutionResult {
        state: "waiting-for-operator",
        body: json!({"reason": reason}),
    }
}

fn temporary_observation_error(error: &crate::host_runtime::HostRuntimeError) -> bool {
    use crate::host_runtime::HostRuntimeError;
    match error {
        HostRuntimeError::Io(error) => error.kind() != std::io::ErrorKind::PermissionDenied,
        HostRuntimeError::Controller(ClientError::Protocol) => false,
        HostRuntimeError::Controller(ClientError::Controller(error))
            if matches!(error.status, 401 | 403) =>
        {
            false
        }
        HostRuntimeError::Controller(_) => true,
        HostRuntimeError::HelperRejected { code, .. } => {
            matches!(
                code.as_str(),
                "operation_io" | "installation_reconciliation_storage_unavailable"
            )
        }
        HostRuntimeError::HelperProtocol(_) => false,
        HostRuntimeError::HelperProtocolBound { .. } => false,
        HostRuntimeError::StopUncertain => false,
    }
}

fn temporary_runtime_observation_failure() -> ExecutionResult {
    ExecutionResult {
        state: "failed",
        body: json!({
            "reason": "exact workload runtime observation is temporarily unavailable",
            "error_code": "runtime_observation_unavailable",
            "failure_kind": "temporary-dependency",
            "retry_after_seconds": 5,
        }),
    }
}

/// Refuse a start whose observation failed, carrying what the helper captured.
///
/// A rejection raised on the inspection path can carry the exact container's
/// output -- the one case the diagnostic gate admits for a privileged action,
/// admitted because the inspection already proved the container's identity and
/// sanitized the text.  Reporting only the code left an operator with "the
/// observation failed" when the answer was that the workload process had exited
/// and printed why.
fn runtime_observation_failure(error: &crate::host_runtime::HostRuntimeError) -> ExecutionResult {
    let reason = match error.diagnostic() {
        Some(detail) if !detail.is_empty() => {
            format!("exact workload runtime observation failed: {error}: {detail}")
        }
        _ => format!("exact workload runtime observation failed: {error}"),
    };
    let mut body = json!({"reason": reason});
    if let Some(logs) =
        crate::failure_evidence::diagnostic_logs(error.process_logs(), error.diagnostic())
    {
        body["diagnostic_logs"] = logs;
    }
    if let crate::host_runtime::HostRuntimeError::HelperRejected { code, .. } = error {
        body["helper_error_code"] = json!(code);
    }
    ExecutionResult {
        state: "failed",
        body,
    }
}

fn failed_owned(reason: String) -> ExecutionResult {
    ExecutionResult {
        state: "failed",
        body: json!({"reason": reason}),
    }
}

fn failed_stage(
    reason: &'static str,
    stage: &'static str,
    diagnostic: &'static str,
) -> ExecutionResult {
    ExecutionResult {
        state: "failed",
        body: json!({"reason": reason, "stage": stage, "diagnostic": diagnostic}),
    }
}

fn failed_stage_owned(
    reason: &'static str,
    stage: &'static str,
    diagnostic: String,
) -> ExecutionResult {
    ExecutionResult {
        state: "failed",
        body: json!({"reason": reason, "stage": stage, "diagnostic": diagnostic}),
    }
}

/// Bound the safe control-plane facts of a refused Controller request.
///
/// An authority denial used to report only that the request failed, so the
/// denied path, status and request id were unrecoverable.  Only the URL path,
/// HTTP status, validated error code, request id and transport category are
/// captured; queries, credentials, headers and response bodies stay unread.
fn controller_denial_diagnostic(error: &ClientError) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(status) = error.status() {
        parts.push(format!("http_status={status}"));
    }
    if let Some(code) = error.code() {
        parts.push(format!("error_code={code}"));
    }
    if let Some(endpoint) = error
        .endpoint()
        .map(str::to_owned)
        .or_else(|| error.transport_endpoint())
    {
        parts.push(format!("endpoint={endpoint}"));
    }
    if let Some(request_id) = error.request_id() {
        parts.push(format!("request_id={request_id}"));
    }
    if let Some(kind) = error.transport_kind() {
        parts.push(format!("transport={kind}"));
    }
    // Leave headroom below the 512-character wire field for redaction.
    parts.join(" ").chars().take(400).collect()
}

/// Classify a refused distribution and keep the bounded denial facts.
///
/// The incident's `invalid-authority` outcome reported only that the
/// distribution failed, so the denied request and status were unrecoverable.
fn distribution_failure_result(error: &ClientError) -> ExecutionResult {
    let mut body = json!({
        "reason": "Controller distribution could not be verified and retained",
        "failure_kind": if error.retryable() {
            "temporary-dependency"
        } else if matches!(error.status(), Some(401 | 403)) {
            "invalid-authority"
        } else {
            "integrity-failure"
        },
        "stage": "artifact-distribution",
    });
    if let Some(seconds) = error.retry_after_seconds() {
        body["retry_after_seconds"] = json!(seconds);
    }
    let diagnostic = controller_denial_diagnostic(error);
    if !diagnostic.is_empty() {
        body["diagnostic"] = json!(diagnostic);
    }
    ExecutionResult {
        state: "failed",
        body,
    }
}

fn recipe_build_client_failure_kind(error: &ClientError) -> AgentFailureKind {
    if error.retryable() {
        return AgentFailureKind::TemporaryDependency;
    }
    if matches!(error.status(), Some(401 | 403))
        || matches!(
            error,
            ClientError::CredentialRead(_) | ClientError::Identity | ClientError::Pin
        )
    {
        return AgentFailureKind::InvalidAuthority;
    }
    match error {
        ClientError::Controller(controller) if (400..=499).contains(&controller.status) => {
            if controller.status == 404 {
                AgentFailureKind::ResourcePrerequisite
            } else if controller.status == 409 {
                AgentFailureKind::IntegrityFailure
            } else {
                AgentFailureKind::InvalidContract
            }
        }
        ClientError::ResultRejected(_) | ClientError::Protocol => AgentFailureKind::InvalidContract,
        ClientError::ObservationNotReady => AgentFailureKind::ResourcePrerequisite,
        ClientError::ResultSuperseded => AgentFailureKind::UncertainEffect,
        _ => AgentFailureKind::IntegrityFailure,
    }
}

fn recipe_build_client_failure_result(
    error: &ClientError,
    stage: &'static str,
    reason: &'static str,
) -> ExecutionResult {
    let failure_kind = recipe_build_client_failure_kind(error);
    let mut body = json!({
        "failure_kind": failure_kind,
        "reason": reason,
        "stage": stage,
    });
    if failure_kind == AgentFailureKind::TemporaryDependency
        && let Some(seconds) = error.retry_after_seconds()
    {
        body["retry_after_seconds"] = json!(seconds);
    }
    let diagnostic = controller_denial_diagnostic(error);
    if !diagnostic.is_empty() {
        body["diagnostic"] = json!(diagnostic);
    }
    ExecutionResult {
        state: "failed",
        body,
    }
}

enum InterruptibleJob<T> {
    Completed(T),
    Cancelled { stopped: bool },
}

async fn wait_for_cancellation(cancellation: &mut tokio::sync::watch::Receiver<bool>) {
    loop {
        if *cancellation.borrow() {
            return;
        }
        if cancellation.changed().await.is_err() {
            std::future::pending::<()>().await;
        }
    }
}

async fn run_until_cancelled<T, F>(
    operation: F,
    cancellation: &mut tokio::sync::watch::Receiver<bool>,
) -> Option<T>
where
    F: Future<Output = T>,
{
    tokio::pin!(operation);
    tokio::select! {
        biased;
        result = &mut operation => Some(result),
        () = wait_for_cancellation(cancellation) => None,
    }
}

async fn run_interruptible_job<T, F, S, SF, E>(
    job: F,
    cancellation: &mut tokio::sync::watch::Receiver<bool>,
    stop: S,
) -> InterruptibleJob<T>
where
    F: Future<Output = T>,
    S: FnOnce() -> SF,
    SF: Future<Output = Result<(), E>>,
{
    tokio::pin!(job);
    tokio::select! {
        biased;
        result = &mut job => InterruptibleJob::Completed(result),
        () = wait_for_cancellation(cancellation) => {
            let stopped = stop().await.is_ok();
            if stopped {
                let _ = tokio::time::timeout(JOB_CANCEL_DRAIN_TIMEOUT, &mut job).await;
            }
            InterruptibleJob::Cancelled { stopped }
        }
    }
}

fn job_placement(
    spec: &CompiledExecutionPlan,
    request: &vonk_agent_protocol::RecipeJobRunRequest,
) -> Result<CompiledRuntimePlacement, WorkloadError> {
    let placement = &spec.runtime.placement;
    if placement.rank != u64::from(request.rank)
        || placement.role != request.role
        || placement.world_size != 1
        || placement.port.is_some()
        || placement.reserved_memory_bytes != request.reserved_memory_bytes
        || placement.memory_floor_bytes != request.memory_floor_bytes
        || placement.memory_kind.to_string() != request.memory_kind.to_string()
    {
        return Err(WorkloadError::Invalid("job placement"));
    }
    placement.validate_bound()?;
    Ok(placement.clone())
}

pub fn recipe_job_input_manifest(
    request: &vonk_agent_protocol::RecipeJobRunRequest,
) -> Result<Vec<u8>, vonk_agent_protocol::ProtocolError> {
    canonical_json(&vonk_agent_protocol::generated::RecipeJobInputManifest {
        schema_version: 1,
        total_bytes: request.input_total_bytes,
        files: request.inputs.clone(),
    })
}

pub fn prepare_job_invocation(
    installed: &CompiledExecutionPlan,
    request: &vonk_agent_protocol::RecipeJobRunRequest,
) -> Result<CompiledExecutionPlan, WorkloadError> {
    let plan = request.compiled_execution_plan.clone();
    plan.validate()?;
    let Some(job) = plan.job.as_ref() else {
        return Err(WorkloadError::Invalid("job interface"));
    };
    if job.interface.as_str() != request.interface.as_str()
        || job.timeout_seconds != request.timeout_seconds
        || plan.runtime.image_digest != request.image_digest
        || plan.identity.recipe_revision_sha256 != request.recipe_content_sha256
        || !crate::workloads::same_job_workload(installed, &plan)
    {
        return Err(WorkloadError::Invalid("job invocation authority"));
    }
    job_placement(&plan, request)?;
    Ok(plan)
}

fn failed_job(
    request: &vonk_agent_protocol::RecipeJobRunRequest,
    exit_code: u32,
    started: Instant,
    reason: &'static str,
) -> ExecutionResult {
    ExecutionResult {
        state: "failed",
        body: job_result_body(
            request,
            exit_code,
            started,
            empty_job_output_manifest(),
            Some(reason),
        ),
    }
}

fn cancelled_job(
    request: &vonk_agent_protocol::RecipeJobRunRequest,
    started: Instant,
    reason: &'static str,
) -> ExecutionResult {
    ExecutionResult {
        state: "cancelled",
        body: job_result_body(
            request,
            JOB_CANCEL_EXIT_CODE,
            started,
            empty_job_output_manifest(),
            Some(reason),
        ),
    }
}

fn output_manifest_with_digest(
    content: vonk_agent_protocol::generated::RecipeJobOutputManifestContent,
) -> Result<RecipeJobOutputManifest, ProtocolError> {
    let manifest_sha256 = hex_sha256(&canonical_json(&content)?);
    Ok(RecipeJobOutputManifest {
        schema_version: content.schema_version,
        manifest_sha256,
        total_bytes: content.total_bytes,
        files: content.files,
    })
}

fn empty_job_output_manifest() -> RecipeJobOutputManifest {
    output_manifest_with_digest(
        vonk_agent_protocol::generated::RecipeJobOutputManifestContent {
            schema_version: 1,
            total_bytes: 0,
            files: Vec::new(),
        },
    )
    .expect("canonical empty job manifest")
}

fn job_result_body(
    request: &vonk_agent_protocol::RecipeJobRunRequest,
    exit_code: u32,
    started: Instant,
    output_manifest: RecipeJobOutputManifest,
    reason: Option<&str>,
) -> Value {
    let result = RecipeJobRunResult {
        schema_version: 1,
        job_id: request.job_id,
        run_id: request.run_id,
        exit_code,
        output_manifest,
        evidence: RecipeJobEvidence {
            elapsed_milliseconds: u32::try_from(started.elapsed().as_millis())
                .expect("bounded job elapsed time"),
            // The helper does not expose a cgroup peak for transient containers yet. Null is
            // honest unavailable evidence; zero would falsely claim a measurement.
            peak_memory_bytes: None,
        },
        reason: reason.map(str::to_owned),
        diagnostics: None,
    };
    debug_assert!(result.validate().is_ok());
    serde_json::to_value(result).unwrap_or_default()
}

fn collect_job_outputs(
    root: Option<&Path>,
    limits: &RecipeJobOutputLimits,
    mappings: &[RecipeJobOutputMapping],
) -> Result<RecipeJobOutputManifest, &'static str> {
    let root = root.ok_or("job output directory is unavailable")?;
    let mut entries = fs::read_dir(root)
        .map_err(|_| "job output directory is unavailable")?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| "job output directory is unavailable")?;
    entries.sort_by_key(fs::DirEntry::file_name);
    if entries.len() > usize::try_from(limits.max_files).expect("bounded job output count") {
        return Err("job output file count exceeded its bound");
    }
    let mut files = Vec::with_capacity(entries.len());
    let mut total_bytes = 0_u64;
    for entry in entries {
        let file_type = entry.file_type().map_err(|_| "job output is unsafe")?;
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| "job output name is invalid")?;
        if !file_type.is_file() || file_type.is_symlink() || !valid_job_output_name(&name) {
            return Err("job output is unsafe");
        }
        let metadata = entry.metadata().map_err(|_| "job output is unsafe")?;
        if metadata.len() > u64::from(limits.max_file_bytes) {
            return Err("job output file size exceeded its bound");
        }
        total_bytes = total_bytes
            .checked_add(metadata.len())
            .filter(|total| *total <= u64::from(limits.max_total_bytes))
            .ok_or("job output total size exceeded its bound")?;
        let media_type = output_media_type(&name, mappings)
            .ok_or("job output media type is not declared by its signed slot mapping")?;
        if !limits
            .allowed_media_types
            .iter()
            .any(|allowed| allowed == media_type)
        {
            return Err("job output media type is not allowed");
        }
        let mut file = File::open(entry.path()).map_err(|_| "job output is unsafe")?;
        let mut hasher = Sha256::new();
        let mut observed = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let read = file
                .read(&mut buffer)
                .map_err(|_| "job output could not be read")?;
            if read == 0 {
                break;
            }
            observed += read as u64;
            hasher.update(&buffer[..read]);
        }
        if observed != metadata.len() {
            return Err("job output changed while it was collected");
        }
        files.push(RecipeJobFile {
            name,
            media_type: media_type.to_owned(),
            size_bytes: u32::try_from(observed)
                .map_err(|_| "job output file size exceeded its bound")?,
            sha256: hex::encode(hasher.finalize()),
        });
    }
    output_manifest_with_digest(
        vonk_agent_protocol::generated::RecipeJobOutputManifestContent {
            schema_version: 1,
            total_bytes: u32::try_from(total_bytes)
                .map_err(|_| "job output total size exceeded its bound")?,
            files,
        },
    )
    .map_err(|_| "job output manifest is invalid")
}

fn valid_job_output_name(value: &str) -> bool {
    !value.is_empty()
        && value != "manifest.json"
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn output_media_type<'mapping>(
    name: &str,
    mappings: &'mapping [RecipeJobOutputMapping],
) -> Option<&'mapping str> {
    mappings
        .iter()
        .flat_map(|mapping| {
            mapping
                .extensions
                .iter()
                .map(move |extension| (extension, mapping.media_type.as_str()))
        })
        .filter(|(extension, _)| name.len() > extension.len() && name.ends_with(extension.as_str()))
        .max_by_key(|(extension, _)| extension.len())
        .map(|(_, media_type)| media_type)
}

#[derive(Debug, thiserror::Error)]
pub enum LoopError {
    #[error(transparent)]
    Client(#[from] ClientError),
    #[error(transparent)]
    State(#[from] StateError),
    #[error("agent heartbeat task failed")]
    HeartbeatTask,
    #[error("agent readiness publication failed: {0}")]
    Readiness(String),
}

/// A failed, panicked, or aborted heartbeat lane must stop the synchronous
/// executor too. Its parent cannot poll a select while Podman blocks that
/// thread; cancellation therefore belongs to the independent heartbeat task.
struct CancelExecutionOnDrop(tokio::sync::watch::Sender<bool>);

impl Drop for CancelExecutionOnDrop {
    fn drop(&mut self) {
        self.0.send_replace(true);
    }
}

#[derive(Clone, Copy)]
struct RunOncePolicy<'a> {
    capabilities: &'a [&'a str],
    wait_seconds: u64,
    runtime_identity: Option<&'a AgentRuntimeIdentity>,
    heartbeat_interval: Duration,
    heartbeat_retry_interval: Duration,
}

pub async fn run_once<C: LoopClient, E: Executor>(
    client: &C,
    state: &mut StateStore,
    executor: &E,
    capabilities: &[&str],
    wait_seconds: u64,
    runtime_identity: Option<&AgentRuntimeIdentity>,
) -> Result<(), LoopError> {
    run_once_with_heartbeat_interval(
        client,
        state,
        executor,
        RunOncePolicy {
            capabilities,
            wait_seconds,
            runtime_identity,
            heartbeat_interval: HEARTBEAT_INTERVAL,
            heartbeat_retry_interval: HEARTBEAT_RETRY_INTERVAL,
        },
        || Ok(()),
    )
    .await
}

pub async fn run_once_with_claim_hook<C, E, F>(
    client: &C,
    state: &mut StateStore,
    executor: &E,
    capabilities: &[&str],
    wait_seconds: u64,
    runtime_identity: Option<&AgentRuntimeIdentity>,
    on_claim_accepted: F,
) -> Result<(), LoopError>
where
    C: LoopClient,
    E: Executor,
    F: FnOnce() -> Result<(), LoopError>,
{
    run_once_with_heartbeat_interval(
        client,
        state,
        executor,
        RunOncePolicy {
            capabilities,
            wait_seconds,
            runtime_identity,
            heartbeat_interval: HEARTBEAT_INTERVAL,
            heartbeat_retry_interval: HEARTBEAT_RETRY_INTERVAL,
        },
        on_claim_accepted,
    )
    .await
}

async fn run_once_with_heartbeat_interval<C, E, F>(
    client: &C,
    state: &mut StateStore,
    executor: &E,
    policy: RunOncePolicy<'_>,
    on_claim_accepted: F,
) -> Result<(), LoopError>
where
    C: LoopClient,
    E: Executor,
    F: FnOnce() -> Result<(), LoopError>,
{
    let now = Utc::now();
    for (operation, result) in state.unreconciled_results()? {
        result
            .validate_for_operation(&operation)
            .map_err(StateError::from)?;
        if state.result_rejection(&result, now)?.is_some() {
            // A refused receipt stays in local custody until its bounded
            // cool-down elapses; re-sending the same bytes cannot succeed.
            continue;
        }
        match client.submit_result(&result).await {
            Ok(()) | Err(ClientError::ResultSuperseded) => state.mark_reconciled(&result)?,
            Err(ClientError::ResultRejected(error)) => {
                record_result_rejection(state, &result, &error, now)?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    for (operation, result) in state.pending_results()? {
        result
            .validate_for_operation(&operation)
            .map_err(StateError::from)?;
        if state.result_rejection(&result, now)?.is_some() {
            continue;
        }
        match client.submit_result(&result).await {
            Ok(()) => state.acknowledge(&result)?,
            // The Controller refused this attempt's outcome as no longer
            // current.  The evidence never landed, so keep it in local custody
            // instead of discarding it, and stop re-sending an outcome that
            // already cannot be applied.
            Err(ClientError::ResultSuperseded) => state.supersede(&result)?,
            // The Controller refused these exact bytes at its ingress
            // validation boundary.  Keep the receipt and the bounded reason,
            // suppress the resend for a cool-down, and keep unrelated work and
            // health alive instead of terminating the loop.
            Err(ClientError::ResultRejected(error)) => {
                record_result_rejection(state, &result, &error, now)?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    let claim = client
        .claim(
            policy.capabilities,
            policy.wait_seconds,
            policy.runtime_identity,
        )
        .await?;
    on_claim_accepted()?;
    let Some(claim) = claim else {
        return Ok(());
    };
    let result = match state.begin(&claim, Utc::now()) {
        Ok(BeginDecision::Execute) => {
            let heartbeat_state = state.reopen()?;
            let (stop_heartbeat, heartbeat_stop) = tokio::sync::oneshot::channel();
            let (lease_deadline_sender, lease_deadline) =
                tokio::sync::watch::channel(claim.deadline);
            let (cancellation_sender, cancellation) = tokio::sync::watch::channel(false);
            let cancel_on_exit = CancelExecutionOnDrop(cancellation_sender.clone());
            let heartbeats = run_heartbeats(
                client.clone(),
                heartbeat_state,
                claim.clone(),
                lease_deadline_sender,
                cancellation_sender,
                heartbeat_stop,
                HeartbeatSchedule {
                    interval: policy.heartbeat_interval,
                    retry_interval: policy.heartbeat_retry_interval,
                },
            );
            let heartbeat_task = tokio::spawn(async move {
                let _cancel_on_exit = cancel_on_exit;
                heartbeats.await
            });
            let executed = normalize_execution_result(
                &claim,
                executor.execute(&claim, lease_deadline, cancellation).await,
            );
            let _ = stop_heartbeat.send(());
            let heartbeat_result = heartbeat_task
                .await
                .map_err(|_| LoopError::HeartbeatTask)
                .and_then(|result| result);
            // The executor owns the effect and its quiescence proof. Preserve
            // its exact cancelled or uncertain outcome; a heartbeat alone
            // cannot turn an in-flight runtime effect into a terminal result.
            let result = state.finish(&claim, executed.state, executed.body)?;
            heartbeat_result?;
            result
        }
        Ok(BeginDecision::Replay(result)) => *result,
        Err(StateError::Busy) => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    result
        .validate_for_operation(&claim.operation)
        .map_err(StateError::from)?;
    if state.result_rejection(&result, now)?.is_none() {
        match client.submit_result(&result).await {
            Ok(()) => state.acknowledge(&result)?,
            Err(ClientError::ResultSuperseded) => state.supersede(&result)?,
            Err(ClientError::ResultRejected(error)) => {
                record_result_rejection(state, &result, &error, now)?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

/// Persist one Controller ingress refusal and make it retrievable.
///
/// Only bounded, correlated control-plane facts are recorded: the durable
/// `result_rejections` row carries the HTTP status, the validated error code,
/// the request id and the Controller's bounded summary, while this line makes
/// the same facts retrievable from the agent's log surface.  The rejected
/// values and the request body are never included.
fn record_result_rejection(
    state: &mut StateStore,
    result: &AgentResult,
    error: &ControllerError,
    now: DateTime<Utc>,
) -> Result<(), LoopError> {
    let rejection = state.reject_result(result, error, now)?;
    eprintln!(
        "vonk-agent: controller refused result for operation {} attempt {} \
         (http {} {} request_id={}): {}; retrying the retained result after {}",
        result.operation_id,
        result.attempt,
        rejection.http_status,
        rejection.code,
        rejection.request_id.as_deref().unwrap_or("none"),
        rejection.reason,
        rejection.retry_due_at.to_rfc3339(),
    );
    Ok(())
}

/// The bounded helper error code an OCI image import failure reports. This is
/// the producer for `stable_runtime_helper_error_code`, which decides whether
/// the code survives into the Controller's normalized failure body.
fn image_import_helper_code(error: &crate::host_runtime::HostRuntimeError) -> String {
    use crate::host_runtime::HostRuntimeError;
    match error {
        HostRuntimeError::HelperRejected { code, .. } => code.clone(),
        HostRuntimeError::Io(_) => "runtime_helper_unavailable".to_owned(),
        HostRuntimeError::Controller(_) => "runtime_authority_unavailable".to_owned(),
        HostRuntimeError::HelperProtocol(cause) => {
            format!("runtime_helper_{}", cause.code())
        }
        HostRuntimeError::HelperProtocolBound { cause, .. } => {
            format!("runtime_helper_{}", cause.code())
        }
        HostRuntimeError::StopUncertain => "runtime_helper_stop_uncertain".to_owned(),
    }
}

fn runtime_failure(reason: &str, error: &crate::host_runtime::HostRuntimeError) -> ExecutionResult {
    let mut result = failed_owned(format!("{reason}: {}", error.preflight_code()));
    if let Some((limit, observed)) = error.refusal_bound() {
        // Bounded integers only: the refusing rule and the measured bound. The
        // offending argument itself never crosses this boundary.
        result.body["refusal_bound"] = json!({
            "rule": error.preflight_code(),
            "limit": limit,
            "observed": observed,
        });
    }
    if let Some(logs) =
        crate::failure_evidence::diagnostic_logs(error.process_logs(), error.diagnostic())
    {
        result.body["diagnostic_logs"] = logs;
    }
    result
}

fn runtime_preparation_failure(error: &OciError) -> ExecutionResult {
    let (stage, category) = error.safe_start_context();
    failed_owned(format!(
        "container runtime could not prepare the workload (stage={stage}; category={category})"
    ))
}

fn normalize_execution_result(claim: &AgentClaim, executed: ExecutionResult) -> ExecutionResult {
    if executed.state != "failed" {
        return executed;
    }
    if claim.operation == "recipe.job.run.v1" {
        let diagnostics = crate::failure_evidence::from_failure(&claim.operation, &executed.body);
        let mut executed = executed;
        if let Some(reason) = executed.body.get("reason").and_then(Value::as_str) {
            executed.body["reason"] = Value::String(crate::failure_evidence::sanitize_text(reason));
        }
        if let Ok(value) = serde_json::to_value(diagnostics) {
            executed.body["diagnostics"] = value;
        }
        return executed;
    }
    let reason = executed
        .body
        .get("reason")
        .and_then(Value::as_str)
        .unwrap_or("agent operation failed");
    let reason: String = crate::failure_evidence::sanitize_text(reason)
        .chars()
        .take(1024)
        .collect();
    let error_code = executed
        .body
        .get("error_code")
        .and_then(Value::as_str)
        .filter(|code| {
            (claim.operation == "recipe.start" && *code == "runtime_observation_unavailable")
                || (claim.operation == "recipe.reconcile"
                    && matches!(
                        *code,
                        "installation_reconciliation_busy"
                            | "recipe_reconciliation_dependency_unavailable"
                    ))
        })
        .unwrap_or_else(|| match claim.operation.as_str() {
            "agent.upgrade.v1" => "agent_upgrade_failed",
            "artifact.distribution.v1" => "artifact_distribution_failed",
            "recipe.build.v1" => "recipe_build_failed",
            "recipe.image.import.v1" => "recipe_image_import_failed",
            "recipe.job.run.v1" => "recipe_job_run_failed",
            "recipe.install" => "recipe_install_failed",
            "recipe.start" => "recipe_start_failed",
            "recipe.stop" => "recipe_stop_failed",
            "recipe.uninstall" => "recipe_uninstall_failed",
            _ => "operation_failed",
        });
    let mut body = json!({
        "error_code": error_code,
        "reason": reason,
        "status": "failed",
    });
    if let Some(kind) = executed.body.get("failure_kind").and_then(Value::as_str) {
        body["failure_kind"] = Value::String(kind.to_owned());
    }
    if let Some(seconds) = executed
        .body
        .get("retry_after_seconds")
        .and_then(Value::as_u64)
    {
        body["retry_after_seconds"] = json!(seconds);
    }
    for field in ["stage", "diagnostic"] {
        if let Some(value) = executed.body.get(field).and_then(Value::as_str) {
            body[field] = Value::String(crate::failure_evidence::sanitize_text(value));
        }
    }
    if claim.operation == "agent.upgrade.v1" {
        if let Some(code) = executed
            .body
            .get("helper_error_code")
            .and_then(Value::as_str)
            .filter(|code| {
                matches!(
                    *code,
                    "package_verification_failed"
                        | "package_metadata_failed"
                        | "package_custody_failed"
                        | "package_install_failed"
                )
            })
        {
            body["helper_error_code"] = Value::String(code.to_owned());
        }
        if body.get("helper_error_code").and_then(Value::as_str) == Some("package_install_failed")
            && let Some(exit_code) = executed
                .body
                .get("helper_exit_code")
                .and_then(Value::as_i64)
                .filter(|code| (0..=255).contains(code))
        {
            body["helper_exit_code"] = Value::from(exit_code);
        }
    }
    if claim.operation == "recipe.image.import.v1"
        && let Some(code) = executed
            .body
            .get("helper_error_code")
            .and_then(Value::as_str)
            .filter(|code| stable_runtime_helper_error_code(code))
    {
        body["helper_error_code"] = Value::String(code.to_owned());
    }
    let diagnostics = crate::failure_evidence::from_failure(&claim.operation, &executed.body);
    if let Ok(value) = serde_json::to_value(diagnostics) {
        body["diagnostics"] = value;
    }
    ExecutionResult {
        state: "failed",
        body,
    }
}

fn stable_runtime_helper_error_code(value: &str) -> bool {
    matches!(
        value,
        "operation_failed"
            | "operation_invalid"
            | "operation_unsafe_path"
            | "operation_invalid_artifact"
            | "operation_command_failed"
            | "operation_stop_uncertain"
            | "operation_io"
            | "runtime_image_load_failed"
            | "runtime_image_inspect_failed"
            | "runtime_image_identity_invalid"
            | "runtime_image_receipt_failed"
            | "runtime_helper_unavailable"
            | "runtime_authority_unavailable"
            | "runtime_helper_protocol_invalid"
            // Any host runtime call can be refused before the helper trusts the
            // grant, the image import included. Dropping those codes here left
            // the normalized failure with no cause at all.
            | "grant_invalid"
            | "grant_node_mismatch"
            | "grant_unauthorized"
            | "peer_identity_invalid"
            | "request_invalid"
            | "request_replayed"
            | "request_ledger_failed"
            // An agent-side helper-protocol cause keeps its own code here or
            // normalization silently drops it from the Controller's failure
            // body.
            | "runtime_helper_request_encoding_invalid"
            | "runtime_helper_call_join_failed"
            | "runtime_helper_message_framing_invalid"
            | "runtime_helper_response_unbound"
            | "runtime_helper_rejection_malformed"
            | "runtime_helper_outcome_malformed"
            | "runtime_helper_request_document_invalid"
            | "runtime_helper_request_schema_version_invalid"
            | "runtime_helper_request_attempt_invalid"
            | "runtime_helper_request_arguments_presence_invalid"
            | "runtime_helper_request_installation_identity_invalid"
            | "runtime_helper_request_bytes_invalid"
            | "runtime_helper_request_argument_nul_byte"
            | "runtime_helper_request_storage_invalid"
            | "runtime_helper_system_clock_invalid"
            | "runtime_helper_inspection_receipt_invalid"
            | "runtime_helper_observation_receipt_invalid"
            | "runtime_helper_observation_timestamp_invalid"
            | "runtime_helper_stop_uncertain"
    )
}

fn phase_progress(phase: &str) -> OperationProgress {
    OperationProgress {
        phase: phase.to_owned(),
        completed_bytes: 0,
        total_bytes: None,
        total_bytes_known: false,
        completed_items: None,
        total_items: None,
        object_sha256: None,
        kind: None,
        activity: None,
        observed_at: None,
        last_progress_at: None,
        bytes_per_second: None,
        smoothed_bytes_per_second: None,
        eta_seconds: None,
        elapsed_seconds: None,
        checkpoint: None,
        members: Vec::new(),
    }
}

/// Whether a failed renewal may be attempted again.
///
/// A renewal loop that terminates on an unclassified error is a one-way
/// ratchet: the first failure it does not recognise ends all future renewals,
/// and with them the agent's ability to observe cancellation.  Classifying every
/// `ClientError` variant names the closed set of conditions a renewal can never
/// repair.
#[derive(Debug, PartialEq, Eq)]
enum HeartbeatFailure {
    /// The same request may be attempted again on the retry schedule.
    Retryable,
    /// The Controller cancelled this exact superseded command.
    SupersededCancellation,
    /// Renewal can never succeed again; stop the loop and cancel the work.
    Terminal,
}

/// Classify one failed renewal against the complete `ClientError` set.
///
/// There is deliberately no catch-all arm: adding an error class is a compile
/// error here until someone decides, in writing, whether it is recoverable.
fn classify_heartbeat_failure(error: &ClientError) -> HeartbeatFailure {
    match error {
        ClientError::Transport(_) | ClientError::Retryable => HeartbeatFailure::Retryable,
        ClientError::Controller(controller) => {
            if controller.status == 409 && controller.code == "superseded_operation_cancelled" {
                HeartbeatFailure::SupersededCancellation
            } else if controller.retryable() {
                HeartbeatFailure::Retryable
            } else {
                // A refused renewal: revoked authority, a fence another attempt
                // has taken over, a lease lapsed past its renewal allowance, or
                // an invalid claim.  Sending the same renewal again cannot
                // repair any of them.
                HeartbeatFailure::Terminal
            }
        }
        // None of these is repaired by renewing again: the credential, TLS
        // identity or pinned CA cannot be read; the response cannot be parsed;
        // the result boundary is not the renewal boundary at all.
        ClientError::CredentialRead(_)
        | ClientError::Identity
        | ClientError::Protocol
        | ClientError::ResultSuperseded
        | ClientError::ResultRejected(_)
        | ClientError::ObservationNotReady
        | ClientError::Pin => HeartbeatFailure::Terminal,
    }
}

/// The immutable start deadline a two-phase start bound, if this claim is one.
///
/// The lease is the thing a renewal recovers, so it cannot also be the recovery
/// budget.  A distributed start persists its own budget in the payload the agent
/// executes, and that is the clock the renewal loop retries against.
fn claim_start_deadline(claim: &AgentClaim) -> Option<DateTime<FixedOffset>> {
    let vonk_agent_protocol::generated::AgentClaimPayload::RecipeStartPayload(request) =
        &claim.payload
    else {
        return None;
    };
    request
        .start_deadline
        .as_deref()
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
}

async fn run_heartbeats<C: LoopClient>(
    client: C,
    mut state: StateStore,
    claim: AgentClaim,
    lease_deadline: tokio::sync::watch::Sender<DateTime<FixedOffset>>,
    cancellation: tokio::sync::watch::Sender<bool>,
    mut stop: tokio::sync::oneshot::Receiver<()>,
    schedule: HeartbeatSchedule,
) -> Result<bool, LoopError> {
    let mut deadline = claim.deadline;
    // Both sides of the wire agree on this one budget: the Controller lets a
    // lapsed renewal re-acquire while the start budget is still open, and the
    // loop retries until the same instant.  An operation that binds no start
    // deadline is bounded by its own work, which is what ``stop`` already is.
    let renewal_budget_end = claim_start_deadline(&claim);
    let mut cancellation_observed = false;
    let mut delay = schedule.interval;
    loop {
        tokio::select! {
            _ = &mut stop => return Ok(cancellation_observed),
            _ = tokio::time::sleep(delay) => {}
        }
        let progress = AgentProgress {
            attempt: claim.attempt,
            deadline,
            fence: claim.fence,
            job_id: claim.job_id,
            node_id: claim.node_id.clone(),
            operation_id: claim.operation_id,
            progress: None,
            schema_version: claim.schema_version,
        };
        let directive = match client.heartbeat(&progress).await {
            Ok(directive) => directive,
            Err(error) => match classify_heartbeat_failure(&error) {
                HeartbeatFailure::SupersededCancellation => {
                    // The Controller has already invalidated this exact old
                    // command. It is an expected cancellation, not an agent loop
                    // failure; preserve the executor's eventual stop evidence.
                    eprintln!(
                        "vonk-agent: superseded operation cancellation observed for {}",
                        claim.operation_id
                    );
                    cancellation.send_replace(true);
                    return Ok(true);
                }
                HeartbeatFailure::Terminal => return Err(error.into()),
                HeartbeatFailure::Retryable => {
                    let now = Utc::now();
                    if let Some(budget_end) = renewal_budget_end
                        && now >= budget_end.with_timezone(&Utc)
                    {
                        // The start's own immutable budget is spent, so no
                        // renewal can restore this attempt; the executor's own
                        // phase-deadline failure owns the outcome instead.
                        return Err(error.into());
                    }
                    // Retry promptly while the accepted lease can still be
                    // extended in time, then settle onto the ordinary renewal
                    // cadence: a lapsed lease bounds how often we may ask, not
                    // whether we may ask.  The loop stays alive so a renewal
                    // that lands inside the Controller's allowance still
                    // re-acquires the attempt, and so the agent keeps observing
                    // the Controller's cancellation.
                    delay = if now < deadline.with_timezone(&Utc) {
                        schedule
                            .retry_interval
                            .min(remaining_lease(deadline).saturating_sub(HEARTBEAT_LEASE_MARGIN))
                            .max(HEARTBEAT_RETRY_FLOOR)
                    } else {
                        schedule.interval
                    };
                    continue;
                }
            },
        };
        // The accepted lease advanced, so the ordinary renewal cadence
        // applies again until the next transient failure.
        delay = schedule.interval;
        state.apply_heartbeat(&progress, &directive)?;
        lease_deadline.send_replace(directive.deadline);
        deadline = directive.deadline;
        cancellation_observed |= directive.cancel_requested;
        if directive.cancel_requested {
            cancellation.send_replace(true);
        }
    }
}

/// Time left before the accepted lease stops authorising a renewal.
fn remaining_lease(deadline: DateTime<FixedOffset>) -> Duration {
    (deadline.with_timezone(&Utc) - Utc::now())
        .to_std()
        .unwrap_or(Duration::ZERO)
}

#[cfg(test)]
mod tests {
    use super::{
        ExecutionResult, Executor, HEARTBEAT_RETRY_FLOOR, HeartbeatFailure, InterruptibleJob,
        LoopClient, ReadinessOutcome, RecipeExecutor, RecipeObservationError, RejectingExecutor,
        RunOncePolicy, classify_heartbeat_failure, controller_denial_diagnostic,
        distribution_failure_result, distribution_success_evidence, normalize_execution_result,
        output_media_type, parse_compiled_execution_plan, readiness_identity,
        recipe_build_client_failure_result, recipe_install_success_body,
        report_complete_recipe_run_observations, run_interruptible_job, run_once_with_claim_hook,
        run_once_with_heartbeat_interval, runtime_observation_failure, temporary_observation_error,
        temporary_runtime_observation_failure, wait_for_launch_stability,
        wait_ready_with_runtime_guard_and_cancellation,
    };
    use crate::{
        client::{AgentHttpClient, ClientError, ControllerError, DistributionDownloadEvidence},
        oci::OciRuntime,
        process::{ProcessError, ProcessOutput, ProcessRunner, Program},
        runtime_identity::AgentRuntimeIdentity,
        state::{BeginDecision, StateStore},
    };
    use async_trait::async_trait;
    use chrono::{DateTime, Duration as ChronoDuration, FixedOffset, Utc};
    use serde_json::{Value, json};
    use std::{
        fs,
        io::{Read, Write},
        net::TcpListener,
        sync::{
            Arc, Mutex,
            atomic::{AtomicBool, Ordering},
        },
        thread,
        time::Duration,
    };
    use tempfile::tempdir;
    use uuid::Uuid;
    use vonk_agent_protocol::generated::{AgentFailureKind, AgentFailureResult};
    use vonk_agent_protocol::{
        AgentClaim, AgentDirective, AgentProgress, AgentResult, RecipeJobOutputMapping,
        RecipeOperationRequest, canonical_json, hex_sha256,
    };

    const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

    #[test]
    fn readiness_identity_uses_controller_evidence_digest_forms() {
        let value: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../control/tests/fixtures/compiled_workload_v2.json"
        ))
        .unwrap();
        let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let (image_digest, model_identity) = readiness_identity(&plan);
        assert_eq!(image_digest, plan.runtime.image_digest);
        let artifact = &plan.artifacts[0];
        assert_eq!(
            model_identity,
            format!(
                "{}/{}@{}",
                artifact.model.publisher, artifact.model.slug, artifact.model.content_sha256
            )
        );
    }

    #[test]
    fn compiled_plan_parser_rejects_malformed_or_unsafe_mounts() {
        let mut value: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../control/tests/fixtures/compiled_workload_v2.json"
        ))
        .unwrap();
        assert!(parse_compiled_execution_plan(&value).is_ok());
        value["security"]["mounts"][0]["target"] = json!("/etc");
        assert!(parse_compiled_execution_plan(&value).is_err());
        value["security"]["mounts"][0]["target"] = json!("/models");
        value["runtime"].as_object_mut().unwrap().remove("argv");
        assert!(parse_compiled_execution_plan(&value).is_err());
    }

    struct NoProcess;

    impl ProcessRunner for NoProcess {
        fn run(
            &self,
            _program: Program,
            _arguments: &[String],
            _timeout: Duration,
        ) -> Result<ProcessOutput, ProcessError> {
            panic!("corrupt lifecycle enumeration must not execute a process")
        }
    }

    #[test]
    fn canonical_job_claim_prepares_without_a_serving_port() {
        let claim: Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
        ))
        .unwrap();
        let request: vonk_agent_protocol::RecipeJobRunRequest =
            serde_json::from_value(claim["payload"].clone()).unwrap();
        let spec = request.compiled_execution_plan.clone();
        spec.validate().unwrap();
        let placement = super::job_placement(&spec, &request).unwrap();
        for field in [
            "rank",
            "role",
            "reserved_memory_bytes",
            "memory_floor_bytes",
        ] {
            let mut altered = claim["payload"].clone();
            altered[field] = match field {
                "rank" => json!(1),
                "role" => json!("worker"),
                "reserved_memory_bytes" => json!(request.reserved_memory_bytes + 1024),
                _ => json!(request.memory_floor_bytes + 1024),
            };
            if matches!(field, "rank" | "role") {
                assert!(
                    serde_json::from_value::<vonk_agent_protocol::RecipeJobRunRequest>(altered)
                        .is_err()
                );
            } else {
                let altered = serde_json::from_value(altered).unwrap();
                assert!(super::job_placement(&spec, &altered).is_err());
            }
        }
        let data = tempdir().unwrap();
        let run_id = request.run_id.to_string();
        fs::create_dir_all(data.path().join("runs").join(&run_id).join("inputs")).unwrap();
        let runtime = OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
            huggingface_curl_config: None,
        };
        let start = runtime
            .prepare_job_start(
                &spec,
                &request.installation_id.to_string(),
                &run_id,
                &placement,
                &spec,
            )
            .unwrap();
        assert!(!start.main.iter().any(|argument| argument == "--publish"));
        assert!(
            start
                .main
                .windows(2)
                .any(|pair| pair == ["--network", "none"])
        );
        let metadata = data.path().join("run-metadata").join(run_id);
        for name in ["runtime.json", "lifecycle.json"] {
            let persisted: Value =
                serde_json::from_slice(&fs::read(metadata.join(name)).unwrap()).unwrap();
            let port = if name == "runtime.json" {
                &persisted["runtime"]["placement"]["port"]
            } else {
                &persisted["placement"]["port"]
            };
            assert!(port.is_null());
        }
        let mut serving_placement = placement;
        serving_placement.port = Some(1024);
        assert!(
            runtime
                .start_arguments(
                    &spec,
                    &request.installation_id.to_string(),
                    &request.run_id.to_string(),
                    &serving_placement
                )
                .is_err()
        );
    }

    struct ObservationServer {
        client: AgentHttpClient,
        stop: Arc<AtomicBool>,
        worker: thread::JoinHandle<Vec<Value>>,
    }

    impl ObservationServer {
        fn new(status: Option<u16>) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap();
            listener.set_nonblocking(true).unwrap();
            let stop = Arc::new(AtomicBool::new(false));
            let stopped = stop.clone();
            let worker = thread::spawn(move || {
                let mut reports = Vec::new();
                while !stopped.load(Ordering::SeqCst) {
                    let (mut stream, _) = match listener.accept() {
                        Ok(connection) => connection,
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(1));
                            continue;
                        }
                        Err(error) => panic!("observation listener: {error}"),
                    };
                    stream
                        .set_read_timeout(Some(Duration::from_secs(2)))
                        .unwrap();
                    let mut request = Vec::new();
                    let mut buffer = [0_u8; 4096];
                    let header_end = loop {
                        let size = stream.read(&mut buffer).unwrap();
                        assert_ne!(size, 0);
                        request.extend_from_slice(&buffer[..size]);
                        if let Some(index) =
                            request.windows(4).position(|bytes| bytes == b"\r\n\r\n")
                        {
                            break index + 4;
                        }
                    };
                    let headers = std::str::from_utf8(&request[..header_end]).unwrap();
                    assert!(
                        headers.starts_with("POST /agent/recipe-runs/observations HTTP/1.1\r\n")
                    );
                    let content_length = headers
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().unwrap())
                        })
                        .unwrap();
                    while request.len() - header_end < content_length {
                        let size = stream.read(&mut buffer).unwrap();
                        assert_ne!(size, 0);
                        request.extend_from_slice(&buffer[..size]);
                    }
                    reports.push(serde_json::from_slice(&request[header_end..]).unwrap());
                    if let Some(status) = status {
                        write!(stream, "HTTP/1.1 {status} Response\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
                    }
                }
                reports
            });
            Self {
                client: AgentHttpClient::for_http_test(
                    &format!("http://{address}/"),
                    "spk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ),
                stop,
                worker,
            }
        }

        fn finish(self) -> Vec<Value> {
            self.stop.store(true, Ordering::SeqCst);
            self.worker.join().unwrap()
        }
    }

    fn exact_observation(run_id: Uuid) -> crate::client::ExactRecipeRunObservation {
        let mut value: Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/fixtures/recipe-run-observation.json"
        ))
        .unwrap();
        let now = Utc::now().timestamp();
        value["run_id"] = json!(run_id);
        value["grant"]["claims"]["operation"]["job_id"] = json!(run_id);
        value["grant"]["claims"]["issued_at"] = json!(now - 1);
        value["grant"]["claims"]["expires_at"] = json!(now + 10);
        value["helper_receipt"]["claims"]["observed_at"] = json!(now);
        value["observed_at"] = json!(DateTime::from_timestamp(now, 0).unwrap());
        serde_json::from_value(value).unwrap()
    }

    #[tokio::test]
    async fn exact_snapshot_reports_multiple_runs_together() {
        let server = ObservationServer::new(Some(204));
        let ids = [Uuid::new_v4(), Uuid::new_v4()];
        let results = ids
            .into_iter()
            .map(|id| Ok(exact_observation(id)))
            .collect();
        assert_eq!(
            report_complete_recipe_run_observations(&server.client, results)
                .await
                .unwrap(),
            2
        );
        let reports = server.finish();
        assert_eq!(reports.len(), 1);
        assert_eq!(reports[0]["schema_version"], 2);
        let runs = reports[0]["runs"].as_array().unwrap();
        assert_eq!(runs.len(), 2);
        for id in ids {
            assert!(runs.iter().any(|run| run["run_id"] == id.to_string()));
        }
    }

    #[tokio::test]
    async fn exact_snapshot_report_failure_never_reports_empty() {
        for status in [Some(503), Some(422), None] {
            let server = ObservationServer::new(status);
            let error = report_complete_recipe_run_observations(
                &server.client,
                vec![Ok(exact_observation(Uuid::new_v4()))],
            )
            .await
            .unwrap_err();
            assert!(matches!(error, RecipeObservationError::Report(_)));
            let reports = server.finish();
            assert_eq!(reports.len(), 1);
            assert_eq!(reports[0]["runs"].as_array().unwrap().len(), 1);
        }
    }

    #[tokio::test]
    async fn exact_snapshot_inspection_failure_preserves_other_runs_without_reporting_empty() {
        // Wrong implementation: a denied or unavailable retained run discards
        // current healthy receipts, eventually expiring that unrelated run.
        for error in [
            crate::host_runtime::HostRuntimeError::HelperProtocol(
                crate::host_runtime::HelperProtocolCause::ObservationTimestamp,
            ),
            crate::host_runtime::HostRuntimeError::Controller(ClientError::ObservationNotReady),
            crate::host_runtime::HostRuntimeError::Controller(ClientError::Controller(Box::new(
                crate::client::ControllerError::from_status(403),
            ))),
        ] {
            let server = ObservationServer::new(Some(204));
            let current_run = Uuid::new_v4();
            let result = report_complete_recipe_run_observations(
                &server.client,
                vec![
                    Ok(exact_observation(current_run)),
                    Err(RecipeObservationError::Inspection(error)),
                ],
            )
            .await;
            assert!(matches!(result, Err(RecipeObservationError::Inspection(_))));
            let reports = server.finish();
            assert_eq!(reports.len(), 1);
            let runs = reports[0]["runs"].as_array().unwrap();
            assert_eq!(runs.len(), 1);
            assert_eq!(runs[0]["run_id"], current_run.to_string());
        }
    }

    #[tokio::test]
    async fn exact_snapshot_failed_inspection_is_not_proof_of_an_empty_node() {
        let server = ObservationServer::new(Some(204));
        let result = report_complete_recipe_run_observations(
            &server.client,
            vec![Err(RecipeObservationError::Inspection(
                crate::host_runtime::HostRuntimeError::Controller(ClientError::ObservationNotReady),
            ))],
        )
        .await;
        assert!(matches!(result, Err(RecipeObservationError::Inspection(_))));
        assert!(server.finish().is_empty());
    }

    #[tokio::test]
    async fn exact_snapshot_does_not_submit_receipts_expired_during_collection() {
        let server = ObservationServer::new(Some(204));
        let mut stale = exact_observation(Uuid::new_v4());
        stale.grant.claims.issued_at = Utc::now().timestamp() - 20;
        stale.grant.claims.expires_at = Utc::now().timestamp() - 10;
        stale.helper_receipt.claims.observed_at = stale.grant.claims.issued_at;
        stale.observed_at = DateTime::from_timestamp(stale.grant.claims.issued_at, 0)
            .unwrap()
            .into();
        stale.validate().unwrap();
        let fresh = exact_observation(Uuid::new_v4());
        assert!(matches!(
            report_complete_recipe_run_observations(
                &server.client,
                vec![Ok(stale.clone()), Ok(fresh.clone())]
            )
            .await,
            Err(RecipeObservationError::StaleSnapshot)
        ));
        let reports = server.finish();
        assert_eq!(reports.len(), 1);
        let runs = reports[0]["runs"].as_array().unwrap();
        assert_eq!(runs.len(), 1);
        assert_eq!(runs[0]["run_id"], fresh.run_id.to_string());

        let server = ObservationServer::new(Some(204));
        assert!(matches!(
            report_complete_recipe_run_observations(&server.client, vec![Ok(stale)]).await,
            Err(RecipeObservationError::StaleSnapshot)
        ));
        assert!(server.finish().is_empty());
    }

    #[tokio::test]
    async fn exact_snapshot_reports_empty_only_when_no_managed_runs_exist() {
        let data = tempdir().unwrap();
        let runtime = tempdir().unwrap();
        let server = ObservationServer::new(Some(204));
        let runner = NoProcess;
        let executor = RecipeExecutor {
            client: &server.client,
            runtime: OciRuntime {
                runner: &runner,
                data_root: data.path(),
                huggingface_curl_config: None,
            },
            runtime_root: runtime.path(),
            observation_receipt_public_key: [0; 32],
        };
        assert_eq!(
            executor
                .report_exact_recipe_run_observations()
                .await
                .unwrap(),
            0
        );
        let reports = server.finish();
        assert_eq!(reports.len(), 1);
        assert_eq!(reports[0]["schema_version"], 2);
        assert_eq!(reports[0]["runs"], json!([]));
    }

    #[tokio::test]
    async fn corrupt_exact_lifecycle_does_not_report_false_absence() {
        let data = tempdir().unwrap();
        let runtime = tempdir().unwrap();
        let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";
        fs::create_dir_all(data.path().join("runs").join(run_id)).unwrap();
        let metadata = data.path().join("run-metadata").join(run_id);
        fs::create_dir_all(&metadata).unwrap();
        fs::write(metadata.join("lifecycle.json"), b"not-json").unwrap();
        let server = ObservationServer::new(Some(204));
        let runner = NoProcess;
        let executor = RecipeExecutor {
            client: &server.client,
            runtime: OciRuntime {
                runner: &runner,
                data_root: data.path(),
                huggingface_curl_config: None,
            },
            runtime_root: runtime.path(),
            observation_receipt_public_key: [0; 32],
        };
        assert!(matches!(
            executor.report_exact_recipe_run_observations().await,
            Err(RecipeObservationError::Runtime(_))
        ));
        assert!(server.finish().is_empty());
    }

    #[test]
    fn observation_report_diagnostic_preserves_only_the_safe_client_category() {
        assert_eq!(
            RecipeObservationError::Report(ClientError::Protocol).to_string(),
            "exact recipe run observation could not be reported: controller protocol response is invalid"
        );
        let error = ClientError::CredentialRead(std::io::Error::other("secret/path/token"));
        assert_eq!(
            RecipeObservationError::Report(error).to_string(),
            "exact recipe run observation could not be reported: agent credential could not be read"
        );
    }

    #[test]
    fn failed_recipe_build_preserves_only_safe_classified_evidence() {
        let mut build_claim = claim();
        build_claim.operation = "recipe.build.v1".parse().unwrap();
        let result = normalize_execution_result(
            &build_claim,
            ExecutionResult {
                state: "failed",
                body: json!({
                    "diagnostic": "temporary-storage-exhausted",
                    "host_path": "/private/secret",
                    "reason": "Podman could not import the verified base image (temporary-storage-exhausted)",
                    "stage": "base-image-import",
                }),
            },
        );

        assert_eq!(
            checked_failure_body(result.body),
            json!({
                "diagnostic": "temporary-storage-exhausted",
                "error_code": "recipe_build_failed",
                "reason": "Podman could not import the verified base image (temporary-storage-exhausted)",
                "stage": "base-image-import",
                "status": "failed",
            })
        );
    }

    #[test]
    fn recipe_build_client_failures_keep_typed_retry_and_refusal_evidence() {
        let mut unavailable = ControllerError::from_status(503);
        unavailable.retry_after_seconds = Some(19);
        let denied = ControllerError::from_status(403);
        let invalid_contract = ControllerError::from_status(422);
        let conflict = ControllerError::from_status(409);
        let cases = [
            (
                ClientError::Controller(Box::new(unavailable)),
                "source-bundle-fetch",
                AgentFailureKind::TemporaryDependency,
                Some(19),
            ),
            (
                ClientError::Controller(Box::new(denied)),
                "source-bundle-fetch",
                AgentFailureKind::InvalidAuthority,
                None,
            ),
            (
                ClientError::Controller(Box::new(invalid_contract)),
                "source-bundle-fetch",
                AgentFailureKind::InvalidContract,
                None,
            ),
            (
                ClientError::Protocol,
                "image-upload",
                AgentFailureKind::InvalidContract,
                None,
            ),
            (
                ClientError::Controller(Box::new(conflict)),
                "image-upload",
                AgentFailureKind::IntegrityFailure,
                None,
            ),
            (
                ClientError::Retryable,
                "image-upload",
                AgentFailureKind::TemporaryDependency,
                None,
            ),
        ];
        let mut build_claim = claim();
        build_claim.operation = "recipe.build.v1".parse().unwrap();

        for (error, stage, expected_kind, expected_retry_after) in cases {
            let result = recipe_build_client_failure_result(
                &error,
                stage,
                "build dependency could not be confirmed",
            );
            let result = normalize_execution_result(&build_claim, result);
            let failure: AgentFailureResult = serde_json::from_value(result.body.clone()).unwrap();

            assert_eq!(result.state, "failed");
            assert_eq!(failure.status.as_deref(), Some("failed"));
            assert_eq!(failure.error_code.as_deref(), Some("recipe_build_failed"));
            assert_eq!(failure.stage.as_deref(), Some(stage));
            assert_eq!(failure.failure_kind, Some(expected_kind));
            assert_eq!(failure.retry_after_seconds, expected_retry_after);
            assert_eq!(
                failure.reason.as_deref(),
                Some("build dependency could not be confirmed")
            );
        }
    }

    #[test]
    fn distribution_result_is_digest_bound_and_controller_safe() {
        let archive_digest = "a".repeat(64);
        let image_digest = format!("sha256:{}", "b".repeat(64));
        let body = distribution_success_evidence(DistributionDownloadEvidence {
            assignment_id: Uuid::new_v4(),
            model_artifact_set_sha256: "c".repeat(64),
            model_digests: vec!["d".repeat(64)],
            model_paths: vec![std::path::PathBuf::from("/run/private/model.bin")],
            oci_archive_path: std::path::PathBuf::from("/run/private/image.oci.tar"),
            oci_archive_sha256: archive_digest.clone(),
            oci_archive_bytes: 123,
            oci_image_digest: image_digest.clone(),
            downloaded_bytes: 456,
        });
        let evidence_digest = body["evidence_digest"].as_str().unwrap();
        let mut without_digest = body.clone();
        without_digest
            .as_object_mut()
            .unwrap()
            .remove("evidence_digest");
        assert_eq!(
            evidence_digest,
            hex_sha256(&canonical_json(&without_digest).unwrap())
        );
        assert!(body.get("model_files").is_none());
        assert!(body.get("oci_archive").is_none());
        assert_eq!(body["verified_oci_layout_sha256"], archive_digest);
        assert_eq!(body["verified_image_digest"], image_digest);

        let result = AgentResult {
            attempt: 1,
            deadline: (Utc::now() + ChronoDuration::seconds(20))
                .with_timezone(&FixedOffset::east_opt(0).unwrap()),
            fence: Uuid::new_v4(),
            job_id: Uuid::new_v4(),
            node_id: NODE_ID.to_owned(),
            operation_id: Uuid::new_v4(),
            result: serde_json::from_value(body).unwrap(),
            schema_version: 1,
            state: "succeeded".parse().unwrap(),
        };
        result.validate().unwrap();
    }

    #[test]
    fn distribution_failure_uses_operation_specific_result_code() {
        let mut distribution_claim = claim();
        distribution_claim.operation = "artifact.distribution.v1".parse().unwrap();
        let result = normalize_execution_result(
            &distribution_claim,
            ExecutionResult {
                state: "failed",
                body: json!({"reason": "distribution object digest mismatch"}),
            },
        );
        assert_eq!(result.body["error_code"], "artifact_distribution_failed");
        assert_eq!(result.body["status"], "failed");
    }

    #[test]
    fn a_denied_controller_request_keeps_bounded_denial_facts() {
        // The incident's invalid-authority failure named neither the denied
        // request nor the status, so its cause could not be recovered from the
        // retained evidence.
        let error = ClientError::Controller(Box::new(ControllerError {
            operation: "controller.request /agent/distribution/assignment".to_owned(),
            endpoint: "/agent/distribution/assignment".to_owned(),
            status: 403,
            code: "controller.request_rejected".to_owned(),
            request_id: Some("req-403".to_owned()),
            decision: "exit",
            retry_after_seconds: None,
            summary: None,
        }));

        let diagnostic = controller_denial_diagnostic(&error);

        assert!(diagnostic.contains("http_status=403"));
        assert!(diagnostic.contains("error_code=controller.request_rejected"));
        assert!(diagnostic.contains("endpoint=/agent/distribution/assignment"));
        assert!(diagnostic.contains("request_id=req-403"));
        assert!(diagnostic.len() <= 512);
    }

    #[test]
    fn an_invalid_authority_distribution_failure_preserves_denial_context() {
        let mut distribution_claim = claim();
        distribution_claim.operation = "artifact.distribution.v1".parse().unwrap();
        let error = ClientError::Controller(Box::new(ControllerError {
            operation: "controller.request /agent/distribution/assignment".to_owned(),
            endpoint: "/agent/distribution/assignment".to_owned(),
            status: 401,
            code: "controller.authentication_required".to_owned(),
            request_id: Some("req-401".to_owned()),
            decision: "exit",
            retry_after_seconds: None,
            summary: None,
        }));

        let raw = distribution_failure_result(&error);
        assert_eq!(raw.state, "failed");
        assert_eq!(raw.body["failure_kind"], "invalid-authority");
        assert_eq!(raw.body["stage"], "artifact-distribution");

        let result = normalize_execution_result(&distribution_claim, raw);

        assert_eq!(result.body["error_code"], "artifact_distribution_failed");
        assert_eq!(result.body["failure_kind"], "invalid-authority");
        assert_eq!(result.body["stage"], "artifact-distribution");
        let diagnostic = result.body["diagnostic"].as_str().unwrap();
        assert!(diagnostic.contains("http_status=401"));
        assert!(diagnostic.contains("request_id=req-401"));
    }

    #[tokio::test]
    async fn recipe_uninstall_with_optional_model_cleanup_is_executed() {
        let data = tempdir().unwrap();
        let runtime_root = tempdir().unwrap();
        let installation_id = "00000000-0000-4000-8000-000000000001";
        let plan: Value = serde_json::from_str(include_str!(
            "../../../../control/tests/fixtures/compiled_workload_v2.json"
        ))
        .unwrap();
        let model_content_sha256 = plan["artifacts"][0]["model"]["content_sha256"]
            .as_str()
            .unwrap()
            .to_owned();
        let installation = data.path().join("installations").join(installation_id);
        fs::create_dir_all(&installation).unwrap();
        fs::write(
            installation.join("spec.json"),
            serde_json::to_vec(&plan).unwrap(),
        )
        .unwrap();
        let recipe_content_sha256 = plan["identity"]["recipe_revision_sha256"].as_str().unwrap();
        fs::write(
            installation.join("recipe-content.sha256"),
            recipe_content_sha256,
        )
        .unwrap();

        let claim = AgentClaim {
            attempt: 1,
            authority_revision: "b".repeat(64),
            deadline: (Utc::now() + ChronoDuration::seconds(20))
                .with_timezone(&FixedOffset::east_opt(0).unwrap()),
            fence: Uuid::new_v4(),
            job_id: Uuid::new_v4(),
            node_id: NODE_ID.to_owned(),
            operation: "recipe.uninstall".parse().unwrap(),
            operation_id: Uuid::new_v4(),
            payload_digest: hex_sha256(
                &canonical_json(&serde_json::json!({
                    "schema_version": 1,
                    "installation_id": installation_id,
                    "recipe_content_sha256": recipe_content_sha256,
                    "cleanup_model_content_sha256": model_content_sha256,
                    "plan_digest": "b".repeat(64),
                }))
                .unwrap(),
            ),
            payload: serde_json::from_value(serde_json::json!({
                "schema_version": 1,
                "installation_id": installation_id,
                "recipe_content_sha256": recipe_content_sha256,
                "cleanup_model_content_sha256": model_content_sha256,
                "plan_digest": "b".repeat(64),
            }))
            .unwrap(),
            schema_version: 1,
        };
        let client = AgentHttpClient::for_http_test("http://127.0.0.1/", NODE_ID);
        let runner = NoProcess;
        let executor = RecipeExecutor {
            client: &client,
            runtime: OciRuntime {
                runner: &runner,
                data_root: data.path(),
                huggingface_curl_config: None,
            },
            runtime_root: runtime_root.path(),
            observation_receipt_public_key: [0; 32],
        };
        let (_lease_sender, lease_deadline) = tokio::sync::watch::channel(claim.deadline);
        let (_cancel_sender, cancellation) = tokio::sync::watch::channel(false);

        let result = executor.execute(&claim, lease_deadline, cancellation).await;

        assert_eq!(result.state, "succeeded");
        assert_eq!(result.body["uninstalled"], true);
        assert_eq!(result.body["removed_model_bytes"], 0);
        assert!(!installation.exists());
    }

    #[test]
    fn signed_output_mappings_cover_pdf_avif_and_custom_suffixes() {
        let mappings = vec![
            RecipeJobOutputMapping {
                slot: "custom".to_owned(),
                media_type: "application/vnd.vonk.custom".to_owned(),
                extensions: vec![".vonk.bin".to_owned()],
            },
            RecipeJobOutputMapping {
                slot: "document".to_owned(),
                media_type: "application/pdf".to_owned(),
                extensions: vec![".pdf".to_owned()],
            },
            RecipeJobOutputMapping {
                slot: "fallback".to_owned(),
                media_type: "application/octet-stream".to_owned(),
                extensions: vec![".bin".to_owned()],
            },
            RecipeJobOutputMapping {
                slot: "image".to_owned(),
                media_type: "image/avif".to_owned(),
                extensions: vec![".avif".to_owned()],
            },
        ];

        assert_eq!(
            output_media_type("report.pdf", &mappings),
            Some("application/pdf")
        );
        assert_eq!(
            output_media_type("frame.avif", &mappings),
            Some("image/avif")
        );
        assert_eq!(
            output_media_type("artifact.vonk.bin", &mappings),
            Some("application/vnd.vonk.custom")
        );
        assert_eq!(
            output_media_type("artifact.bin", &mappings),
            Some("application/octet-stream")
        );
        assert_eq!(output_media_type("report.PDF", &mappings), None);
        assert_eq!(
            output_media_type("artifact.VONK.bin", &mappings),
            Some("application/octet-stream")
        );
    }

    #[tokio::test]
    async fn active_job_cancellation_runs_the_stop_path_promptly() {
        let (sender, mut cancellation) = tokio::sync::watch::channel(false);
        let (job_stopped, job_drained) = tokio::sync::oneshot::channel::<()>();
        let stopped = Arc::new(AtomicBool::new(false));
        let observed = Arc::clone(&stopped);
        tokio::spawn(async move {
            tokio::task::yield_now().await;
            sender.send_replace(true);
        });

        let result = tokio::time::timeout(
            Duration::from_secs(1),
            run_interruptible_job(
                async move {
                    let _ = job_drained.await;
                },
                &mut cancellation,
                move || async move {
                    observed.store(true, Ordering::Release);
                    let _ = job_stopped.send(());
                    Ok::<(), ()>(())
                },
            ),
        )
        .await
        .expect("cancellation did not interrupt the active job");

        assert!(matches!(
            result,
            InterruptibleJob::Cancelled { stopped: true }
        ));
        assert!(stopped.load(Ordering::Acquire));
    }

    #[tokio::test]
    async fn exited_runtime_ends_a_still_pending_readiness_probe() {
        let readiness = std::future::pending::<Result<(), crate::health::HealthError>>();

        let (_sender, cancellation) = tokio::sync::watch::channel(false);
        let outcome = wait_ready_with_runtime_guard_and_cancellation(
            readiness,
            async { Err(crate::host_runtime::HostRuntimeError::StopUncertain) },
            cancellation,
        )
        .await;
        assert!(matches!(outcome, ReadinessOutcome::GuardFailed(_)));
    }

    #[tokio::test]
    async fn a_failed_runtime_inspection_names_the_inspection_not_a_readiness_deadline() {
        // Wrong implementation this catches: the guard collapsed every error into
        // `false`, so a failed privileged inspection was reported as "the workload
        // did not become ready before its deadline" and the Controller's existing
        // `runtime_observation_unavailable` retry could never fire.  Observed live
        // on 2026-09-17, a two-Spark GLM start ended 51 s in -- five ten-second
        // inspection ticks -- with an hour of start budget unused.
        let error = crate::host_runtime::HostRuntimeError::Io(std::io::Error::other(
            "privileged inspection failed",
        ));
        assert!(temporary_observation_error(&error));
        let (_sender, cancellation) = tokio::sync::watch::channel(false);
        let outcome = wait_ready_with_runtime_guard_and_cancellation(
            std::future::pending::<Result<(), crate::health::HealthError>>(),
            async { Err(error) },
            cancellation,
        )
        .await;
        assert!(matches!(outcome, ReadinessOutcome::GuardFailed(_)));
    }

    #[tokio::test]
    async fn successful_readiness_ends_a_still_running_runtime_guard() {
        let runtime_guard = std::future::pending::<
            Result<std::convert::Infallible, crate::host_runtime::HostRuntimeError>,
        >();

        let (_sender, cancellation) = tokio::sync::watch::channel(false);
        let outcome = wait_ready_with_runtime_guard_and_cancellation(
            async { Ok(()) },
            runtime_guard,
            cancellation,
        )
        .await;
        assert!(matches!(outcome, ReadinessOutcome::Ready));
    }

    #[test]
    fn an_exited_workload_failure_carries_the_captured_container_output() {
        // Wrong implementation this catches: the guard's rejection was reported as
        // its code alone, so an operator read "the observation failed" when the
        // answer was that the workload process had exited and printed why.  The
        // inspection gate admits that text precisely because the inspection
        // already proved the container's identity and sanitized it.
        let error = crate::host_runtime::HostRuntimeError::HelperRejected {
            code: "runtime_process_exited".to_owned(),
            diagnostic: None,
            process_logs: Some(Box::new(crate::failure_evidence::FailureProcessLogs {
                stdout: crate::failure_evidence::log_tail(b"rank 0 listening on 8888\n"),
                stderr: crate::failure_evidence::log_tail(b"ModuleNotFoundError: runtime module\n"),
            })),
        };
        let result = runtime_observation_failure(&error);
        assert_eq!(result.state, "failed");
        let reason = result.body["reason"].as_str().unwrap_or_default();
        assert!(reason.contains("runtime_process_exited"), "{reason}");
        // Both streams arrive as themselves: merging them into one tail is what
        // discarded the stream that was written first.
        let stdout = result.body["diagnostic_logs"]["stdout"]["text"]
            .as_str()
            .unwrap_or_default();
        assert!(stdout.contains("listening on 8888"), "{stdout}");
        let stderr = result.body["diagnostic_logs"]["stderr"]["text"]
            .as_str()
            .unwrap_or_default();
        assert!(stderr.contains("ModuleNotFoundError"), "{stderr}");
    }
    #[tokio::test]
    async fn collective_readiness_exits_when_the_controller_cancels() {
        let (sender, cancellation) = tokio::sync::watch::channel(false);
        let wait = wait_ready_with_runtime_guard_and_cancellation(
            std::future::pending::<Result<(), crate::health::HealthError>>(),
            std::future::pending::<
                Result<std::convert::Infallible, crate::host_runtime::HostRuntimeError>,
            >(),
            cancellation,
        );
        let trigger = async {
            tokio::task::yield_now().await;
            sender.send_replace(true);
        };
        let (outcome, ()) = tokio::join!(wait, trigger);
        assert!(matches!(outcome, ReadinessOutcome::Cancelled));
    }

    #[tokio::test]
    async fn rank_launch_stability_is_capped_by_the_signed_deadline() {
        let lease = (Utc::now() + ChronoDuration::minutes(5))
            .with_timezone(&FixedOffset::east_opt(0).unwrap());
        let immutable = (Utc::now() - ChronoDuration::milliseconds(1))
            .with_timezone(&FixedOffset::east_opt(0).unwrap());
        let (_lease_sender, lease_receiver) = tokio::sync::watch::channel(lease);
        let (_cancel_sender, cancellation) = tokio::sync::watch::channel(false);

        assert!(
            !wait_for_launch_stability(
                lease_receiver,
                cancellation,
                Some(immutable),
                Duration::from_secs(30),
            )
            .await
        );
    }

    #[derive(Clone)]
    struct RecordingClient {
        cancel_requested: bool,
        claim: Arc<Mutex<Option<AgentClaim>>>,
        fail_heartbeat: bool,
        heartbeats: Arc<Mutex<Vec<AgentProgress>>>,
        results: Arc<Mutex<Vec<AgentResult>>>,
    }

    #[async_trait]
    impl LoopClient for RecordingClient {
        async fn claim(
            &self,
            _capabilities: &[&str],
            _wait_seconds: u64,
            _runtime_identity: Option<&AgentRuntimeIdentity>,
        ) -> Result<Option<AgentClaim>, ClientError> {
            Ok(self.claim.lock().unwrap().take())
        }

        async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
            self.heartbeats.lock().unwrap().push(progress.clone());
            if self.fail_heartbeat && self.heartbeats.lock().unwrap().len() == 1 {
                return Err(ClientError::Retryable);
            }
            Ok(AgentDirective {
                attempt: progress.attempt,
                cancel_requested: self.cancel_requested,
                deadline: progress.deadline + ChronoDuration::seconds(30),
                fence: progress.fence,
                job_id: progress.job_id,
                node_id: progress.node_id.clone(),
                operation_id: progress.operation_id,
                schema_version: progress.schema_version,
            })
        }

        async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
            self.results.lock().unwrap().push(result.clone());
            Ok(())
        }
    }

    #[derive(Clone)]
    struct SupersededCancellationClient(RecordingClient);

    #[async_trait]
    impl LoopClient for SupersededCancellationClient {
        async fn claim(
            &self,
            capabilities: &[&str],
            wait_seconds: u64,
            runtime_identity: Option<&AgentRuntimeIdentity>,
        ) -> Result<Option<AgentClaim>, ClientError> {
            self.0
                .claim(capabilities, wait_seconds, runtime_identity)
                .await
        }

        async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
            self.0.heartbeats.lock().unwrap().push(progress.clone());
            Err(ClientError::Controller(Box::new(ControllerError {
                operation: "controller.request /agent/heartbeat".to_owned(),
                endpoint: "/agent/heartbeat".to_owned(),
                status: 409,
                code: "superseded_operation_cancelled".to_owned(),
                request_id: None,
                decision: "exit",
                retry_after_seconds: None,
                summary: None,
            })))
        }

        async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
            self.0.submit_result(result).await
        }
    }

    #[derive(Clone)]
    struct TerminalHeartbeatClient {
        inner: RecordingClient,
        panic: bool,
    }

    #[async_trait]
    impl LoopClient for TerminalHeartbeatClient {
        async fn claim(
            &self,
            capabilities: &[&str],
            wait_seconds: u64,
            runtime_identity: Option<&AgentRuntimeIdentity>,
        ) -> Result<Option<AgentClaim>, ClientError> {
            self.inner
                .claim(capabilities, wait_seconds, runtime_identity)
                .await
        }

        async fn heartbeat(
            &self,
            _progress: &AgentProgress,
        ) -> Result<AgentDirective, ClientError> {
            assert!(!self.panic, "heartbeat task failed unexpectedly");
            Err(ClientError::Protocol)
        }

        async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
            self.inner.submit_result(result).await
        }
    }

    /// Refuses every renewal until ``lapsed_after`` and accepts them afterwards.
    ///
    /// That is the shape of a Controller restart or a lost round trip: the
    /// accepted lease runs out while the Controller is unreachable, then it
    /// answers again.
    #[derive(Clone)]
    struct LeaseLapseClient {
        inner: RecordingClient,
        lapsed_after: DateTime<Utc>,
        accepted_at: Arc<Mutex<Vec<DateTime<Utc>>>>,
    }
    #[async_trait]
    impl LoopClient for LeaseLapseClient {
        async fn claim(
            &self,
            capabilities: &[&str],
            wait_seconds: u64,
            runtime_identity: Option<&AgentRuntimeIdentity>,
        ) -> Result<Option<AgentClaim>, ClientError> {
            self.inner
                .claim(capabilities, wait_seconds, runtime_identity)
                .await
        }

        async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
            self.inner.heartbeats.lock().unwrap().push(progress.clone());
            if Utc::now() < self.lapsed_after {
                return Err(ClientError::Retryable);
            }
            self.accepted_at.lock().unwrap().push(Utc::now());
            Ok(AgentDirective {
                attempt: progress.attempt,
                cancel_requested: false,
                deadline: progress.deadline + ChronoDuration::seconds(30),
                fence: progress.fence,
                job_id: progress.job_id,
                node_id: progress.node_id.clone(),
                operation_id: progress.operation_id,
                schema_version: progress.schema_version,
            })
        }

        async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
            self.inner.submit_result(result).await
        }
    }

    /// Keeps the work alive until a renewal is accepted.
    ///
    /// The lease under test lapses in real time, so a fixed work duration would
    /// race the executor's own finish against the renewal the test asserts.  It
    /// still honours cancellation, so a loop that gives up early ends the test
    /// promptly instead of waiting out the cap.
    struct RenewalGatedExecutor {
        accepted: Arc<Mutex<Vec<DateTime<Utc>>>>,
        minimum: usize,
        cap: Duration,
        cancelled: Arc<AtomicBool>,
    }

    #[async_trait(?Send)]
    impl Executor for RenewalGatedExecutor {
        async fn execute(
            &self,
            _claim: &AgentClaim,
            _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
            cancellation: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            let deadline = std::time::Instant::now() + self.cap;
            while std::time::Instant::now() < deadline {
                if *cancellation.borrow() {
                    self.cancelled.store(true, Ordering::SeqCst);
                    break;
                }
                if self.accepted.lock().unwrap().len() >= self.minimum {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            ExecutionResult {
                state: "succeeded",
                body: recipe_install_success_body(1),
            }
        }
    }

    struct BlockingCancellationExecutor {
        cancelled: Arc<AtomicBool>,
    }

    #[async_trait(?Send)]
    impl Executor for BlockingCancellationExecutor {
        async fn execute(
            &self,
            _claim: &AgentClaim,
            _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
            cancellation: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            // The real Podman runner is synchronous too. A select in the
            // parent cannot observe a failed heartbeat while this poll blocks.
            let deadline = std::time::Instant::now() + Duration::from_secs(2);
            while std::time::Instant::now() < deadline {
                if *cancellation.borrow() {
                    self.cancelled.store(true, Ordering::SeqCst);
                    break;
                }
                thread::sleep(Duration::from_millis(1));
            }
            ExecutionResult {
                state: "failed",
                body: json!({"reason": "build process ended"}),
            }
        }
    }

    struct HeartbeatGatedExecutor {
        heartbeats: Arc<Mutex<Vec<AgentProgress>>>,
        minimum: usize,
        observed_deadline: Arc<Mutex<Option<DateTime<FixedOffset>>>>,
    }

    struct CancelledHeartbeatExecutor(HeartbeatGatedExecutor);

    #[async_trait(?Send)]
    impl Executor for CancelledHeartbeatExecutor {
        async fn execute(
            &self,
            claim: &AgentClaim,
            lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
            cancellation: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            let mut result = self.0.execute(claim, lease_deadline, cancellation).await;
            result.state = "cancelled";
            result.body = json!({"reason": "exact workload stop confirmed", "error_code": "operation_cancelled"});
            result
        }
    }

    #[async_trait(?Send)]
    impl Executor for HeartbeatGatedExecutor {
        async fn execute(
            &self,
            _claim: &AgentClaim,
            lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
            _cancellation: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            tokio::time::timeout(Duration::from_secs(2), async {
                loop {
                    if self.heartbeats.lock().unwrap().len() >= self.minimum {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(1)).await;
                }
            })
            .await
            .expect("heartbeat task did not make progress");
            *self.observed_deadline.lock().unwrap() = Some(*lease_deadline.borrow());
            ExecutionResult {
                state: "succeeded",
                body: super::recipe_install_success_body(0),
            }
        }
    }

    struct FailedExecutor;

    #[async_trait(?Send)]
    impl Executor for FailedExecutor {
        async fn execute(
            &self,
            _claim: &AgentClaim,
            _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
            _cancellation: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            ExecutionResult {
                state: "failed",
                body: json!({"reason": "rootless image build failed"}),
            }
        }
    }

    struct CancellationExecutor;

    #[async_trait(?Send)]
    impl Executor for CancellationExecutor {
        async fn execute(
            &self,
            claim: &AgentClaim,
            _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
            mut cancellation: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            let RecipeOperationRequest::JobRun(request) =
                RecipeOperationRequest::parse(claim).unwrap()
            else {
                panic!("expected canonical job claim");
            };
            let started = std::time::Instant::now();
            super::wait_for_cancellation(&mut cancellation).await;
            super::cancelled_job(&request, started, "controller cancellation requested")
        }
    }

    struct OrderingExecutor {
        events: Arc<Mutex<Vec<&'static str>>>,
    }

    #[async_trait(?Send)]
    impl Executor for OrderingExecutor {
        async fn execute(
            &self,
            _claim: &AgentClaim,
            _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
            _cancellation: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            self.events.lock().unwrap().push("execute");
            ExecutionResult {
                state: "succeeded",
                body: super::recipe_install_success_body(0),
            }
        }
    }

    fn checked_failure_body(mut body: serde_json::Value) -> serde_json::Value {
        let diagnostics = body.as_object_mut().unwrap().remove("diagnostics").unwrap();
        serde_json::from_value::<crate::failure_evidence::FailureDiagnostics>(diagnostics)
            .unwrap()
            .validate()
            .unwrap();
        body
    }

    fn claim() -> AgentClaim {
        let plan: Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
        ))
        .unwrap();
        let payload = json!({
            "schema_version": 2,
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "plan_digest": "a".repeat(64),
            "rank": 0,
            "role": "entrypoint",
            "expected_bytes": 1,
            "compiled_execution_plan": plan,
        });
        let claim = AgentClaim {
            attempt: 1,
            authority_revision: "b".repeat(64),
            deadline: (Utc::now() + ChronoDuration::seconds(20))
                .with_timezone(&FixedOffset::east_opt(0).unwrap()),
            fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
            job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
            node_id: NODE_ID.to_owned(),
            operation: "recipe.install".parse().unwrap(),
            operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
            payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
            payload: serde_json::from_value(payload).unwrap(),
            schema_version: 1,
        };
        RecipeOperationRequest::parse(&claim).unwrap();
        claim
    }

    #[test]
    fn preparation_failure_keeps_safe_stage_and_permission_boundary_in_controller_result() {
        use crate::oci::OciError;

        let mut start_claim = claim();
        start_claim.operation = "recipe.start".parse().unwrap();
        let error = OciError::Start {
            stage: "output-storage",
            source: Box::new(OciError::Io(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "/private/secret-credential-value",
            ))),
        };
        let failure = super::runtime_preparation_failure(&error);
        let normalized = super::normalize_execution_result(&start_claim, failure);
        let result: vonk_agent_protocol::generated::AgentFailureResult =
            serde_json::from_value(normalized.body).unwrap();
        assert_eq!(
            result.reason.as_deref(),
            Some(
                "container runtime could not prepare the workload (stage=output-storage; category=storage-permission-denied)"
            )
        );
    }

    #[test]
    fn rank_launch_failure_keeps_sanitized_logs_in_the_controller_contract() {
        let mut start_claim = claim();
        start_claim.operation = "recipe.start".parse().unwrap();
        let error = crate::host_runtime::HostRuntimeError::HelperRejected {
            code: "runtime_process_exited".into(),
            diagnostic: None,
            process_logs: Some(Box::new(crate::failure_evidence::FailureProcessLogs {
                stdout: crate::failure_evidence::log_tail(b"starting the engine core\n"),
                stderr: crate::failure_evidence::log_tail(
                    b"ModuleNotFoundError: runtime module\nAPI_TOKEN=private-value\n",
                ),
            })),
        };
        let failed =
            super::runtime_failure("rank process did not remain stable after launch", &error);
        let result = super::normalize_execution_result(&start_claim, failed);
        let body: vonk_agent_protocol::generated::AgentFailureResult =
            serde_json::from_value(result.body).unwrap();
        assert!(
            body.reason
                .as_deref()
                .unwrap()
                .contains("helper_runtime_process_exited")
        );
        let diagnostics = body.diagnostics.as_ref().unwrap();
        diagnostics.validate().unwrap();
        assert!(diagnostics.stdout.text.contains("starting the engine core"));
        assert!(diagnostics.stderr.text.contains("ModuleNotFoundError"));
        assert!(
            !serde_json::to_string(&body)
                .unwrap()
                .contains("private-value")
        );
    }

    #[test]
    fn runtime_failure_names_the_helper_protocol_cause_to_the_controller() {
        // Wrong implementation: the named cause stopped at `preflight_code()`,
        // so the Controller only ever saw the collapsed
        // `helper_protocol_invalid` label on the live blocked-start path.
        let mut start_claim = claim();
        start_claim.operation = "recipe.start".parse().unwrap();
        let error = crate::host_runtime::HostRuntimeError::HelperProtocol(
            crate::host_runtime::HelperProtocolCause::OutcomeMalformed,
        );
        let failed =
            super::runtime_failure("container runtime could not start the workload", &error);
        let result = super::normalize_execution_result(&start_claim, failed);
        let body: vonk_agent_protocol::generated::AgentFailureResult =
            serde_json::from_value(result.body).unwrap();
        let reason = body.reason.as_deref().unwrap();
        assert!(
            reason.contains(
                "container runtime could not start the workload: helper_outcome_malformed"
            ),
            "the failure reason must name the violated contract, got {reason}"
        );
    }

    #[test]
    fn runtime_failure_names_the_agent_built_request_to_the_controller() {
        // Wrong implementation: a Start whose agent-built request failed
        // canonical validation reported the collapsed `helper_protocol_invalid`
        // on the live blocked-start path, with no helper involved and nothing
        // for an operator to act on.
        let mut start_claim = claim();
        start_claim.operation = "recipe.start".parse().unwrap();
        let error = crate::host_runtime::HostRuntimeError::HelperProtocol(
            crate::host_runtime::HelperProtocolCause::RequestDocument,
        );
        let failed =
            super::runtime_failure("container runtime could not start the workload", &error);
        let result = super::normalize_execution_result(&start_claim, failed);
        let body: vonk_agent_protocol::generated::AgentFailureResult =
            serde_json::from_value(result.body).unwrap();
        let reason = body.reason.as_deref().unwrap();
        assert!(
            reason.contains(
                "container runtime could not start the workload: helper_request_document_invalid"
            ),
            "the failure reason must name the agent-built request, got {reason}"
        );
    }

    #[test]
    fn runtime_failure_names_stop_uncertain_without_calling_it_protocol() {
        // Wrong implementation: an ambiguous stop carried the protocol label,
        // so "we could not confirm the stop" read as a corrupt helper reply.
        let mut start_claim = claim();
        start_claim.operation = "recipe.start".parse().unwrap();
        let error = crate::host_runtime::HostRuntimeError::StopUncertain;
        let failed =
            super::runtime_failure("container runtime could not start the workload", &error);
        let result = super::normalize_execution_result(&start_claim, failed);
        let body: vonk_agent_protocol::generated::AgentFailureResult =
            serde_json::from_value(result.body).unwrap();
        let reason = body.reason.as_deref().unwrap();
        assert!(
            reason
                .contains("container runtime could not start the workload: helper_stop_uncertain"),
            "an ambiguous stop must not read as a malformed reply, got {reason}"
        );
    }

    #[test]
    fn a_refused_request_bound_reaches_the_failure_evidence() {
        // Wrong implementation: the refusal named its rule but not the bound, so
        // an operator had to read the constants to tell 518 of 4096 from 5000
        // of 4096.
        let mut start_claim = claim();
        start_claim.operation = "recipe.start".parse().unwrap();
        let limit = vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES as u64;
        let error = crate::host_runtime::HostRuntimeError::HelperProtocolBound {
            cause: crate::host_runtime::HelperProtocolCause::RequestBytes,
            limit: Some(limit),
            observed: limit + 1,
        };
        let failed =
            super::runtime_failure("container runtime could not start the workload", &error);
        let result = super::normalize_execution_result(&start_claim, failed);
        let body: vonk_agent_protocol::generated::AgentFailureResult =
            serde_json::from_value(result.body).unwrap();
        let diagnostics = body.diagnostics.as_ref().unwrap();
        let refusal = diagnostics
            .preflight
            .iter()
            .find(|property| property.name == "request_refusal")
            .expect("the refusal bound must be reported");
        assert!(refusal.value.contains("request_bytes_invalid"));
        assert!(refusal.value.contains(&format!("limit={limit}")));
        assert!(refusal.value.contains(&format!("observed={}", limit + 1)));
        assert!(
            body.reason
                .as_deref()
                .unwrap()
                .contains("helper_request_bytes_invalid")
        );
    }

    #[test]
    fn image_import_helper_protocol_cause_survives_normalization() {
        // Wrong implementation: a new cause's code was absent from
        // `stable_runtime_helper_error_code`, so normalization silently dropped
        // it and the Controller saw no cause at all.
        let mut import_claim = claim();
        import_claim.operation = "recipe.image.import.v1".parse().unwrap();
        let mut errors: Vec<crate::host_runtime::HostRuntimeError> = [
            crate::host_runtime::HelperProtocolCause::RequestEncoding,
            crate::host_runtime::HelperProtocolCause::HelperCallJoin,
            crate::host_runtime::HelperProtocolCause::MessageFraming,
            crate::host_runtime::HelperProtocolCause::ResponseUnbound,
            crate::host_runtime::HelperProtocolCause::RejectionMalformed,
            crate::host_runtime::HelperProtocolCause::OutcomeMalformed,
            crate::host_runtime::HelperProtocolCause::RequestDocument,
            crate::host_runtime::HelperProtocolCause::RequestSchemaVersion,
            crate::host_runtime::HelperProtocolCause::RequestAttempt,
            crate::host_runtime::HelperProtocolCause::RequestArgumentsPresence,
            crate::host_runtime::HelperProtocolCause::RequestInstallationIdentity,
            crate::host_runtime::HelperProtocolCause::RequestBytes,
            crate::host_runtime::HelperProtocolCause::RequestArgumentNulByte,
            crate::host_runtime::HelperProtocolCause::RequestStorage,
            crate::host_runtime::HelperProtocolCause::SystemClock,
            crate::host_runtime::HelperProtocolCause::InspectionReceipt,
            crate::host_runtime::HelperProtocolCause::ObservationReceipt,
            crate::host_runtime::HelperProtocolCause::ObservationTimestamp,
        ]
        .into_iter()
        .map(crate::host_runtime::HostRuntimeError::HelperProtocol)
        .collect();
        errors.push(crate::host_runtime::HostRuntimeError::StopUncertain);
        for error in errors {
            let code = super::image_import_helper_code(&error);
            assert!(
                code.starts_with("runtime_helper_"),
                "an import failure code stays in the runtime_helper_ namespace, got {code}"
            );
            let result = super::normalize_execution_result(
                &import_claim,
                ExecutionResult {
                    state: "failed",
                    body: json!({
                        "reason": "runtime image import failed",
                        "helper_error_code": code,
                    }),
                },
            );
            assert_eq!(
                result.body["helper_error_code"], code,
                "{code} must survive normalization rather than be silently dropped"
            );
        }
    }

    #[test]
    fn artifact_job_failure_keeps_current_result_and_typed_diagnostics() {
        let mut job_claim = claim();
        job_claim.operation = "recipe.job.run.v1".parse().unwrap();
        let envelope: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
        ))
        .unwrap();
        let mut body = envelope["result"].clone();
        body["exit_code"] = json!(1);
        body["reason"] = json!("runtime failed");
        let result = normalize_execution_result(
            &job_claim,
            ExecutionResult {
                state: "failed",
                body,
            },
        );
        let typed: vonk_agent_protocol::RecipeJobRunResult =
            serde_json::from_value(result.body).unwrap();
        typed.validate().unwrap();
        assert_eq!(typed.exit_code, 1);
        assert!(typed.diagnostics.is_some());
    }

    #[tokio::test]
    async fn successful_claim_publishes_readiness_before_job_execution() {
        let directory = tempdir().unwrap();
        let client = RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(claim()))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        let events = Arc::new(Mutex::new(Vec::new()));
        let executor = OrderingExecutor {
            events: events.clone(),
        };
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
        let hook_events = events.clone();

        run_once_with_claim_hook(
            &client,
            &mut state,
            &executor,
            &["recipe.install"],
            0,
            None,
            move || {
                hook_events.lock().unwrap().push("readiness");
                Ok(())
            },
        )
        .await
        .unwrap();

        assert_eq!(*events.lock().unwrap(), ["readiness", "execute"]);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn long_execution_renews_and_persists_its_lease_before_result() {
        let directory = tempdir().unwrap();
        let original = claim();
        let heartbeats = Arc::new(Mutex::new(Vec::new()));
        let client = RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(original.clone()))),
            fail_heartbeat: false,
            heartbeats: heartbeats.clone(),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        let executor = HeartbeatGatedExecutor {
            heartbeats,
            minimum: 2,
            observed_deadline: Arc::new(Mutex::new(None)),
        };
        let observed_deadline = executor.observed_deadline.clone();
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &executor,
            RunOncePolicy {
                capabilities: &["recipe.install"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(10),
                heartbeat_retry_interval: Duration::from_millis(1),
            },
            || Ok(()),
        )
        .await
        .unwrap();

        let heartbeats = client.heartbeats.lock().unwrap();
        assert!(heartbeats.len() >= 2);
        assert!(
            heartbeats
                .iter()
                .all(|heartbeat| heartbeat.progress.is_none())
        );
        drop(heartbeats);
        let results = client.results.lock().unwrap();
        assert_eq!(results.len(), 1);
        assert!(results[0].deadline > original.deadline);
        assert!(observed_deadline.lock().unwrap().unwrap() > original.deadline);
        assert!(state.pending_results().unwrap().is_empty());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_conflicting_result_response_is_not_an_acknowledgement() {
        // The real client has to separate "the Controller already holds this
        // outcome" (204) from "the Controller refused it because the attempt is
        // no longer current" (409).  Treating both as accepted is what let a
        // refused result be deleted locally as though it had landed.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            while !request.windows(4).any(|value| value == b"\r\n\r\n") {
                let read = stream.read(&mut buffer).unwrap();
                assert_ne!(read, 0);
                request.extend_from_slice(&buffer[..read]);
            }
            stream
                .write_all(
                    b"HTTP/1.1 409 Conflict\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .unwrap();
            request
        });
        let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), NODE_ID);

        let directory = tempdir().unwrap();
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
        let claim = claim();
        assert!(matches!(
            state.begin(&claim, Utc::now()).unwrap(),
            BeginDecision::Execute
        ));
        let result = state
            .finish(&claim, "succeeded", recipe_install_success_body(0))
            .unwrap();

        assert!(matches!(
            client.submit_result(&result).await,
            Err(ClientError::ResultSuperseded)
        ));
        let request = String::from_utf8_lossy(&server.join().unwrap()).to_ascii_lowercase();
        assert!(request.starts_with("post /agent/result"));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_validation_rejected_result_response_is_typed_for_local_custody() {
        // The real client has to separate a 422 ingress refusal from the
        // generic "protocol response is invalid" and from a transport failure,
        // so the loop can record the bounded reason and continue.  Treating it
        // as a bare non-retryable controller error is what exited the agent.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            while !request.windows(4).any(|value| value == b"\r\n\r\n") {
                let read = stream.read(&mut buffer).unwrap();
                assert_ne!(read, 0);
                request.extend_from_slice(&buffer[..read]);
            }
            stream
                .write_all(
                    b"HTTP/1.1 422 Unprocessable Entity\r\n\
                      content-type: application/json\r\n\
                      x-vonk-error-code: controller.invalid_request\r\n\
                      x-request-id: req-422\r\n\
                      content-length: 0\r\n\
                      connection: close\r\n\r\n",
                )
                .unwrap();
            request
        });
        let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), NODE_ID);

        let directory = tempdir().unwrap();
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
        let claim = claim();
        assert!(matches!(
            state.begin(&claim, Utc::now()).unwrap(),
            BeginDecision::Execute
        ));
        let result = state
            .finish(&claim, "succeeded", recipe_install_success_body(0))
            .unwrap();

        let Err(ClientError::ResultRejected(error)) = client.submit_result(&result).await else {
            panic!("a 422 must be a typed result rejection");
        };
        assert_eq!(error.status, 422);
        assert_eq!(error.code, "controller.invalid_request");
        assert_eq!(error.request_id.as_deref(), Some("req-422"));
        assert_eq!(error.endpoint, "/agent/result");
        let request = String::from_utf8_lossy(&server.join().unwrap()).to_ascii_lowercase();
        assert!(request.starts_with("post /agent/result"));
    }

    #[derive(Clone)]
    struct RefusingResultClient {
        submitted: Arc<Mutex<Vec<AgentResult>>>,
    }

    #[async_trait]
    impl LoopClient for RefusingResultClient {
        async fn claim(
            &self,
            _capabilities: &[&str],
            _wait_seconds: u64,
            _runtime_identity: Option<&AgentRuntimeIdentity>,
        ) -> Result<Option<AgentClaim>, ClientError> {
            Ok(None)
        }

        async fn heartbeat(
            &self,
            _progress: &AgentProgress,
        ) -> Result<AgentDirective, ClientError> {
            Err(ClientError::Protocol)
        }

        async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
            self.submitted.lock().unwrap().push(result.clone());
            Err(ClientError::ResultSuperseded)
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_refused_result_is_kept_instead_of_discarded() {
        // A result the Controller refuses because the attempt is no longer
        // current never reached durable storage.  Treating that refusal as an
        // acknowledgement discarded the only evidence of the work this agent
        // performed, so the outcome stays in local custody instead.
        let directory = tempdir().unwrap();
        let path = directory.path().join("state.sqlite");
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        let claim = claim();
        assert!(matches!(
            state.begin(&claim, Utc::now()).unwrap(),
            BeginDecision::Execute
        ));
        let result = state
            .finish(&claim, "succeeded", recipe_install_success_body(0))
            .unwrap();
        assert_eq!(state.pending_results().unwrap().len(), 1);

        let client = RefusingResultClient {
            submitted: Arc::new(Mutex::new(Vec::new())),
        };
        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &RejectingExecutor,
            RunOncePolicy {
                capabilities: &["recipe.install"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(10),
                heartbeat_retry_interval: Duration::from_millis(1),
            },
            || Ok(()),
        )
        .await
        .unwrap();

        assert_eq!(client.submitted.lock().unwrap().len(), 1);
        // The execution is not retried. Its recorded outcome remains readable
        // and is offered once more as diagnostic evidence after restart.
        assert!(state.pending_results().unwrap().is_empty());
        assert_eq!(state.unreconciled_results().unwrap().len(), 1);
        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &RejectingExecutor,
            RunOncePolicy {
                capabilities: &["recipe.install"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(10),
                heartbeat_retry_interval: Duration::from_millis(1),
            },
            || Ok(()),
        )
        .await
        .unwrap();
        assert_eq!(client.submitted.lock().unwrap().len(), 2);
        assert!(state.unreconciled_results().unwrap().is_empty());
        let connection = rusqlite::Connection::open(&path).unwrap();
        let stored: Option<Vec<u8>> = connection
            .query_row(
                "SELECT result_json FROM operations WHERE operation_id=?1 AND attempt=?2",
                rusqlite::params![result.operation_id.to_string(), result.attempt],
                |row| row.get(0),
            )
            .unwrap();
        assert!(stored.is_some());
    }

    fn ingress_refusal() -> ControllerError {
        ControllerError {
            operation: "controller.request /agent/result".to_owned(),
            endpoint: "/agent/result".to_owned(),
            status: 422,
            code: "controller.invalid_request".to_owned(),
            request_id: Some("req-422".to_owned()),
            decision: "exit",
            retry_after_seconds: None,
            summary: Some(
                "request is invalid: body.result.AgentFailureResult.failure_kind \
                 (is_instance_of)"
                    .to_owned(),
            ),
        }
    }

    #[derive(Clone)]
    struct IngressRejectingClient {
        accept: Arc<Mutex<bool>>,
        submitted: Arc<Mutex<Vec<AgentResult>>>,
    }

    #[async_trait]
    impl LoopClient for IngressRejectingClient {
        async fn claim(
            &self,
            _capabilities: &[&str],
            _wait_seconds: u64,
            _runtime_identity: Option<&AgentRuntimeIdentity>,
        ) -> Result<Option<AgentClaim>, ClientError> {
            Ok(None)
        }

        async fn heartbeat(
            &self,
            _progress: &AgentProgress,
        ) -> Result<AgentDirective, ClientError> {
            Err(ClientError::Protocol)
        }

        async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
            self.submitted.lock().unwrap().push(result.clone());
            if *self.accept.lock().unwrap() {
                Ok(())
            } else {
                Err(ClientError::ResultRejected(Box::new(ingress_refusal())))
            }
        }
    }

    fn completed_install_result(state: &mut StateStore) -> AgentResult {
        let claim = claim();
        assert!(matches!(
            state.begin(&claim, Utc::now()).unwrap(),
            BeginDecision::Execute
        ));
        state
            .finish(&claim, "succeeded", recipe_install_success_body(0))
            .unwrap()
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn an_ingress_rejected_result_is_recorded_and_the_loop_stays_alive() {
        // A 422 refuses these exact bytes at the Controller's validation
        // boundary.  The general client policy treats a non-retryable 4xx as
        // "exit", so before this the refusal propagated out of the loop and the
        // restarted agent replayed the same durable result.  The receipt now
        // stays in custody, the bounded refusal is durable, and the loop keeps
        // serving claim/heartbeat work.
        let directory = tempdir().unwrap();
        let path = directory.path().join("state.sqlite");
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        let result = completed_install_result(&mut state);
        let client = IngressRejectingClient {
            accept: Arc::new(Mutex::new(false)),
            submitted: Arc::new(Mutex::new(Vec::new())),
        };

        for _ in 0..2 {
            run_once_with_heartbeat_interval(
                &client,
                &mut state,
                &RejectingExecutor,
                RunOncePolicy {
                    capabilities: &["recipe.install"],
                    wait_seconds: 0,
                    runtime_identity: None,
                    heartbeat_interval: Duration::from_millis(10),
                    heartbeat_retry_interval: Duration::from_millis(1),
                },
                || Ok(()),
            )
            .await
            .unwrap();
        }

        // The same bytes were offered once and then not hot-looped, the receipt
        // is still unacknowledged, and the refusal names its boundary.
        assert_eq!(client.submitted.lock().unwrap().len(), 1);
        assert_eq!(state.pending_results().unwrap().len(), 1);
        let rejection = state
            .result_rejection(&result, Utc::now())
            .unwrap()
            .expect("a recorded ingress refusal");
        assert_eq!(rejection.http_status, 422);
        assert_eq!(rejection.code, "controller.invalid_request");
        assert_eq!(rejection.request_id.as_deref(), Some("req-422"));
        // The refusal names the failing field and rule from the Controller's
        // own validation digest, rather than only the endpoint it was refused
        // at, so the durable record is actionable without Controller access.
        assert!(rejection.reason.contains("failure_kind"));
        assert!(rejection.reason.contains("is_instance_of"));
        assert!(rejection.reason.len() <= 256);
        assert!(rejection.retry_due_at > Utc::now());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_corrected_ingress_reconciles_the_retained_result() {
        // Reconciliation after the cause is corrected: once the recorded
        // cool-down has elapsed the retained receipt is offered again, and the
        // Controller's acceptance acknowledges it.
        let directory = tempdir().unwrap();
        let path = directory.path().join("state.sqlite");
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        let result = completed_install_result(&mut state);
        state
            .reject_result(
                &result,
                &ingress_refusal(),
                Utc::now() - ChronoDuration::seconds(1200),
            )
            .unwrap();
        assert!(
            state
                .result_rejection(&result, Utc::now())
                .unwrap()
                .is_none()
        );
        let client = IngressRejectingClient {
            accept: Arc::new(Mutex::new(true)),
            submitted: Arc::new(Mutex::new(Vec::new())),
        };

        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &RejectingExecutor,
            RunOncePolicy {
                capabilities: &["recipe.install"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(10),
                heartbeat_retry_interval: Duration::from_millis(1),
            },
            || Ok(()),
        )
        .await
        .unwrap();

        assert_eq!(client.submitted.lock().unwrap().len(), 1);
        assert!(state.pending_results().unwrap().is_empty());
        assert!(
            state
                .result_rejection(&result, Utc::now())
                .unwrap()
                .is_none()
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_new_attempt_clears_the_previous_attempts_refusal() {
        // A refusal must not leak onto a newer authorised attempt, and it must
        // not survive the Controller accepting or superseding the result.
        let directory = tempdir().unwrap();
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
        let mut first = claim();
        assert!(matches!(
            state.begin(&first, Utc::now()).unwrap(),
            BeginDecision::Execute
        ));
        let result = state
            .finish(&first, "succeeded", recipe_install_success_body(0))
            .unwrap();
        state
            .reject_result(&result, &ingress_refusal(), Utc::now())
            .unwrap();
        assert!(
            state
                .result_rejection(&result, Utc::now())
                .unwrap()
                .is_some()
        );

        first.attempt = 2;
        first.fence = Uuid::new_v4();
        assert!(matches!(
            state.begin(&first, Utc::now()).unwrap(),
            BeginDecision::Execute
        ));
        assert!(
            state
                .result_rejection(&result, Utc::now())
                .unwrap()
                .is_none()
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn transient_heartbeat_failure_does_not_terminate_healthy_execution() {
        let directory = tempdir().unwrap();
        let heartbeats = Arc::new(Mutex::new(Vec::new()));
        let client = RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(claim()))),
            fail_heartbeat: true,
            heartbeats: heartbeats.clone(),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        let executor = HeartbeatGatedExecutor {
            heartbeats,
            minimum: 2,
            observed_deadline: Arc::new(Mutex::new(None)),
        };
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &executor,
            RunOncePolicy {
                capabilities: &["recipe.install"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(10),
                heartbeat_retry_interval: Duration::from_millis(1),
            },
            || Ok(()),
        )
        .await
        .unwrap();

        assert!(client.heartbeats.lock().unwrap().len() >= 2);
        assert_eq!(client.results.lock().unwrap().len(), 1);
        assert!(state.pending_results().unwrap().is_empty());
    }

    #[tokio::test]
    async fn cancelled_heartbeat_preserves_the_executors_confirmed_stop_result() {
        let directory = tempdir().unwrap();
        let heartbeats = Arc::new(Mutex::new(Vec::new()));
        let client = RecordingClient {
            cancel_requested: true,
            claim: Arc::new(Mutex::new(Some(claim()))),
            fail_heartbeat: false,
            heartbeats: heartbeats.clone(),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        let executor = CancelledHeartbeatExecutor(HeartbeatGatedExecutor {
            heartbeats,
            minimum: 1,
            observed_deadline: Arc::new(Mutex::new(None)),
        });
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &executor,
            RunOncePolicy {
                capabilities: &["recipe.install"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(1),
                heartbeat_retry_interval: Duration::from_millis(1),
            },
            || Ok(()),
        )
        .await
        .unwrap();

        let results = client.results.lock().unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].state.as_str(), "cancelled");
        let vonk_agent_protocol::generated::AgentResultResult::AgentFailureResult(body) =
            &results[0].result
        else {
            panic!("cancelled start outcome lost its typed failure result");
        };
        assert_eq!(
            body.reason.as_deref(),
            Some("exact workload stop confirmed")
        );
        assert_eq!(body.error_code.as_deref(), Some("operation_cancelled"));
    }

    #[tokio::test]
    async fn exact_superseded_cancellation_heartbeat_is_an_expected_loop_outcome() {
        let directory = tempdir().unwrap();
        let heartbeats = Arc::new(Mutex::new(Vec::new()));
        let client = SupersededCancellationClient(RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(claim()))),
            fail_heartbeat: false,
            heartbeats: heartbeats.clone(),
            results: Arc::new(Mutex::new(Vec::new())),
        });
        let executor = CancelledHeartbeatExecutor(HeartbeatGatedExecutor {
            heartbeats,
            minimum: 1,
            observed_deadline: Arc::new(Mutex::new(None)),
        });
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &executor,
            RunOncePolicy {
                capabilities: &["recipe.install"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(1),
                heartbeat_retry_interval: Duration::from_millis(1),
            },
            || Ok(()),
        )
        .await
        .unwrap();

        let results = client.0.results.lock().unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].state.as_str(), "cancelled");
    }

    #[tokio::test(start_paused = true)]
    async fn transient_heartbeat_failure_retries_inside_the_accepted_lease() {
        let directory = tempdir().unwrap();
        let heartbeats = Arc::new(Mutex::new(Vec::new()));
        let client = RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(claim()))),
            fail_heartbeat: true,
            heartbeats: heartbeats.clone(),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        let executor = HeartbeatGatedExecutor {
            heartbeats: heartbeats.clone(),
            minimum: 2,
            observed_deadline: Arc::new(Mutex::new(None)),
        };
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
        let run = run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &executor,
            RunOncePolicy {
                capabilities: &["recipe.install"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(800),
                heartbeat_retry_interval: HEARTBEAT_RETRY_FLOOR,
            },
            || Ok(()),
        );
        let drive_clock = async {
            // Claim persistence and heartbeat task startup take an arbitrary
            // number of polls. Advance virtual time in small steps until the
            // first request, then measure the retry against that request.
            for _ in 0..100 {
                if !heartbeats.lock().unwrap().is_empty() {
                    break;
                }
                tokio::time::advance(Duration::from_millis(10)).await;
                tokio::task::yield_now().await;
            }
            assert_eq!(heartbeats.lock().unwrap().len(), 1);
            tokio::task::yield_now().await;
            tokio::time::advance(HEARTBEAT_RETRY_FLOOR).await;
            tokio::task::yield_now().await;
            assert_eq!(heartbeats.lock().unwrap().len(), 2);
        };
        let (result, ()) = tokio::join!(run, drive_clock);
        result.unwrap();
        assert_eq!(client.results.lock().unwrap().len(), 1);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn terminal_heartbeat_failure_cancels_a_blocking_executor() {
        for panic in [false, true] {
            let directory = tempdir().unwrap();
            let client = TerminalHeartbeatClient {
                inner: RecordingClient {
                    cancel_requested: false,
                    claim: Arc::new(Mutex::new(Some(claim()))),
                    fail_heartbeat: false,
                    heartbeats: Arc::new(Mutex::new(Vec::new())),
                    results: Arc::new(Mutex::new(Vec::new())),
                },
                panic,
            };
            let executor = BlockingCancellationExecutor {
                cancelled: Arc::new(AtomicBool::new(false)),
            };
            let mut state =
                StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
            let result = run_once_with_heartbeat_interval(
                &client,
                &mut state,
                &executor,
                RunOncePolicy {
                    capabilities: &["recipe.install"],
                    wait_seconds: 0,
                    runtime_identity: None,
                    heartbeat_interval: Duration::from_millis(10),
                    heartbeat_retry_interval: Duration::from_millis(1),
                },
                || Ok(()),
            )
            .await;
            assert!(result.is_err());
            assert!(
                executor.cancelled.load(Ordering::SeqCst),
                "executor continued after heartbeat failure (panic={panic})"
            );
            assert!(client.inner.results.lock().unwrap().is_empty());
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_retryable_renewal_failure_after_the_lease_lapses_still_renews() {
        // Wrong implementation: the retry arm was guarded by
        // ``Utc::now() < deadline``, so the first renewal that could not be
        // re-sent inside the accepted lease fell through to the catch-all, the
        // heartbeat task returned, and the work was cancelled.  One lost round
        // trip near the expiry therefore ended renewal for a start that was
        // still healthy -- and ended the agent's ability to observe the
        // Controller's cancellation with it.
        let directory = tempdir().unwrap();
        // Open the store before the lease is timed.  `state.begin` refuses an
        // already-expired claim, so anything slow on the path to the loop is
        // inside the lease's margin; a SQLite open plus schema creation is
        // exactly that, and on a loaded two-core runner it was enough to make
        // the claim expire before the loop started.
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
        let heartbeats = Arc::new(Mutex::new(Vec::new()));
        let accepted_at = Arc::new(Mutex::new(Vec::new()));
        let mut lease = claim();
        // The lease lapses in real time, because that is the condition under
        // test.  The margin now covers only loop startup, and the refusal
        // window comfortably outlives the lease.
        let lease_deadline = Utc::now() + ChronoDuration::milliseconds(500);
        lease.deadline = lease_deadline.with_timezone(&FixedOffset::east_opt(0).unwrap());
        let client = LeaseLapseClient {
            inner: RecordingClient {
                cancel_requested: false,
                claim: Arc::new(Mutex::new(Some(lease))),
                fail_heartbeat: false,
                heartbeats: heartbeats.clone(),
                results: Arc::new(Mutex::new(Vec::new())),
            },
            // Comfortably past the accepted lease, so every renewal before this
            // instant is refused and the lease has certainly lapsed.
            lapsed_after: Utc::now() + ChronoDuration::milliseconds(2000),
            accepted_at: accepted_at.clone(),
        };
        let cancelled = Arc::new(AtomicBool::new(false));
        let executor = RenewalGatedExecutor {
            accepted: accepted_at.clone(),
            minimum: 1,
            cap: Duration::from_secs(5),
            cancelled: cancelled.clone(),
        };

        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &executor,
            RunOncePolicy {
                capabilities: &["recipe.install"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(5),
                heartbeat_retry_interval: Duration::from_millis(5),
            },
            || Ok(()),
        )
        .await
        .expect("a lapsed lease that the Controller still accepts must be re-acquired");

        assert!(
            !cancelled.load(Ordering::SeqCst),
            "the work was cancelled although the Controller still accepted renewals"
        );
        let accepted_at = accepted_at.lock().unwrap();
        assert!(
            accepted_at.iter().any(|instant| *instant > lease_deadline),
            "no renewal was accepted after the lease lapsed: {accepted_at:?}"
        );
        assert!(heartbeats.lock().unwrap().len() > accepted_at.len());
        assert_eq!(client.inner.results.lock().unwrap().len(), 1);
        assert!(state.pending_results().unwrap().is_empty());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_retryable_renewal_failure_stops_once_the_start_budget_is_spent() {
        // The lease is what a renewal recovers, so it cannot also be the
        // recovery budget.  Wrong implementation: the loop retried while the
        // accepted lease was live, ignoring the start's own immutable budget, so
        // it kept renewing an attempt whose start deadline had already elapsed.
        let directory = tempdir().unwrap();
        let mut start = claim();
        start.operation = "recipe.start".parse().unwrap();
        let payload = json!({
            "schema_version": 2,
            "run_id": "00000000-0000-4000-8000-0000000000aa",
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "recipe_revision_id": "00000000-0000-4000-8000-0000000000bb",
            "recipe_content_sha256": "c".repeat(64),
            "mapping_id": "00000000-0000-4000-8000-0000000000cc",
            "mapping_generation": 1,
            "run_generation": 1,
            "image_digest": format!("sha256:{}", "d".repeat(64)),
            "plan_digest": "a".repeat(64),
            "alias": "rank-0",
            "rank": 0,
            "role": "entrypoint",
            "port": 29500,
            "reserved_memory_bytes": 1,
            "memory_floor_bytes": 2_000_000_000,
            "memory_kind": "unified",
            "endpoint_address": "10.0.0.1",
            "world_size": 2,
            "compiled_execution_plan": serde_json::from_str::<Value>(include_str!(
                "../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
            ))
            .unwrap(),
            "local_address": "10.0.0.1",
            "master_address": "10.0.0.1",
            "master_port": 29500,
            "phase": "rank-launch",
            "start_deadline": (Utc::now() - ChronoDuration::seconds(1)).to_rfc3339(),
        });
        let typed: vonk_agent_protocol::generated::AgentClaimPayload =
            serde_json::from_value(payload).unwrap();
        start.payload_digest = hex_sha256(&canonical_json(&typed).unwrap());
        start.payload = typed;
        let client = LeaseLapseClient {
            inner: RecordingClient {
                cancel_requested: false,
                claim: Arc::new(Mutex::new(Some(start))),
                fail_heartbeat: false,
                heartbeats: Arc::new(Mutex::new(Vec::new())),
                results: Arc::new(Mutex::new(Vec::new())),
            },
            // Never accepts, so only the start budget can end the loop.
            lapsed_after: Utc::now() + ChronoDuration::hours(1),
            accepted_at: Arc::new(Mutex::new(Vec::new())),
        };
        let executor = BlockingCancellationExecutor {
            cancelled: Arc::new(AtomicBool::new(false)),
        };
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

        let result = tokio::time::timeout(
            Duration::from_millis(500),
            run_once_with_heartbeat_interval(
                &client,
                &mut state,
                &executor,
                RunOncePolicy {
                    capabilities: &["recipe.start"],
                    wait_seconds: 0,
                    runtime_identity: None,
                    heartbeat_interval: Duration::from_millis(5),
                    heartbeat_retry_interval: Duration::from_millis(5),
                },
                || Ok(()),
            ),
        )
        .await;
        assert!(
            result.is_ok(),
            "renewal retried past the start's own immutable budget"
        );
        assert!(result.unwrap().is_err());
        assert!(executor.cancelled.load(Ordering::SeqCst));
    }

    #[test]
    fn heartbeat_failure_classification_is_a_closed_set() {
        // Wrong implementation: ``Err(error) => return Err(...)`` treated every
        // error class it did not recognise as terminal, so a new class added to
        // the client would silently end renewal.  The replacement names each
        // class and is exhaustive, so adding one is a compile error here.
        let refusal = |status: u16, code: &str| {
            ClientError::Controller(Box::new(ControllerError {
                operation: "controller.request /agent/heartbeat".to_owned(),
                endpoint: "/agent/heartbeat".to_owned(),
                status,
                code: code.to_owned(),
                request_id: None,
                decision: "exit",
                retry_after_seconds: None,
                summary: None,
            }))
        };
        // A Controller that is asking for the same request again.
        for status in [408, 429, 500, 503] {
            assert_eq!(
                classify_heartbeat_failure(&refusal(status, "controller_unavailable")),
                HeartbeatFailure::Retryable,
                "status {status}"
            );
        }
        assert_eq!(
            classify_heartbeat_failure(&ClientError::Retryable),
            HeartbeatFailure::Retryable
        );
        // A refused renewal: authority, fence, a lease past its allowance, or an
        // invalid claim.  None of these is repaired by sending it again.
        for status in [400, 401, 403, 404, 409, 410, 422] {
            assert_eq!(
                classify_heartbeat_failure(&refusal(status, "stale_agent_attempt")),
                HeartbeatFailure::Terminal,
                "status {status}"
            );
        }
        assert_eq!(
            classify_heartbeat_failure(&refusal(409, "superseded_operation_cancelled")),
            HeartbeatFailure::SupersededCancellation
        );
        for terminal in [
            ClientError::Identity,
            ClientError::Protocol,
            ClientError::ObservationNotReady,
            ClientError::Pin,
        ] {
            assert_eq!(
                classify_heartbeat_failure(&terminal),
                HeartbeatFailure::Terminal
            );
        }
    }

    #[tokio::test]
    async fn failed_execution_emits_the_controller_failure_contract() {
        let directory = tempdir().unwrap();
        let client = RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(claim()))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &FailedExecutor,
            RunOncePolicy {
                capabilities: &["recipe.install"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_secs(10),
                heartbeat_retry_interval: Duration::from_millis(1),
            },
            || Ok(()),
        )
        .await
        .unwrap();

        assert_eq!(
            checked_failure_body(
                serde_json::to_value(&client.results.lock().unwrap()[0].result).unwrap()
            ),
            json!({
                "error_code": "recipe_install_failed",
                "reason": "rootless image build failed",
                "status": "failed"
            })
        );
    }

    #[test]
    fn exact_start_observation_failure_keeps_retry_contract() {
        let mut start_claim = claim();
        start_claim.operation = "recipe.start".parse().unwrap();
        let result =
            normalize_execution_result(&start_claim, temporary_runtime_observation_failure());
        assert_eq!(result.state, "failed");
        assert_eq!(result.body["error_code"], "runtime_observation_unavailable");
        assert_eq!(result.body["failure_kind"], "temporary-dependency");
        assert_eq!(result.body["retry_after_seconds"], 5);
    }

    #[test]
    fn agent_upgrade_failure_preserves_only_bounded_helper_diagnostics() {
        let mut upgrade_claim = claim();
        upgrade_claim.operation = "agent.upgrade.v1".parse().unwrap();
        let result = normalize_execution_result(
            &upgrade_claim,
            ExecutionResult {
                state: "failed",
                body: json!({
                    "reason": "agent upgrade helper rejected the request: package_install_failed",
                    "helper_error_code": "package_install_failed",
                    "helper_exit_code": 75,
                    "untrusted_detail": "must not cross the controller boundary",
                }),
            },
        );

        assert_eq!(
            checked_failure_body(result.body),
            json!({
                "error_code": "agent_upgrade_failed",
                "reason": "agent upgrade helper rejected the request: package_install_failed",
                "status": "failed",
                "helper_error_code": "package_install_failed",
                "helper_exit_code": 75,
            })
        );

        let rejected = normalize_execution_result(
            &upgrade_claim,
            ExecutionResult {
                state: "failed",
                body: json!({
                    "reason": "agent upgrade failed",
                    "helper_error_code": "arbitrary_host_detail",
                    "helper_exit_code": 512,
                }),
            },
        );
        assert!(rejected.body.get("helper_error_code").is_none());
        assert!(rejected.body.get("helper_exit_code").is_none());
    }

    #[test]
    fn image_import_failure_preserves_only_bounded_helper_diagnostics() {
        let mut import_claim = claim();
        import_claim.operation = "recipe.image.import.v1".parse().unwrap();
        for code in [
            "runtime_helper_unavailable",
            "runtime_authority_unavailable",
            "runtime_helper_protocol_invalid",
            "grant_unauthorized",
            "request_replayed",
        ] {
            let result = normalize_execution_result(
                &import_claim,
                ExecutionResult {
                    state: "failed",
                    body: json!({
                        "reason": "runtime image import failed",
                        "helper_error_code": code,
                        "untrusted_detail": "/root/authority/private-key",
                    }),
                },
            );

            assert_eq!(result.body["helper_error_code"], code);
            assert!(result.body.get("untrusted_detail").is_none());
        }

        let rejected = normalize_execution_result(
            &import_claim,
            ExecutionResult {
                state: "failed",
                body: json!({
                    "reason": "runtime image import failed",
                    "helper_error_code": "arbitrary_host_detail",
                }),
            },
        );
        assert!(rejected.body.get("helper_error_code").is_none());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn artifact_job_heartbeat_cancellation_is_preserved_as_terminal_cancelled() {
        let directory = tempdir().unwrap();
        let mut job_claim: AgentClaim = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
        )).unwrap();
        job_claim.deadline = (Utc::now() + ChronoDuration::seconds(20)).fixed_offset();
        job_claim.node_id = NODE_ID.to_owned();
        job_claim.validate().unwrap();
        let RecipeOperationRequest::JobRun(request) =
            RecipeOperationRequest::parse(&job_claim).unwrap()
        else {
            panic!("expected canonical job claim");
        };
        let client = RecordingClient {
            cancel_requested: true,
            claim: Arc::new(Mutex::new(Some(job_claim))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &CancellationExecutor,
            RunOncePolicy {
                capabilities: &["recipe.job.run.v1"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(1),
                heartbeat_retry_interval: Duration::from_millis(1),
            },
            || Ok(()),
        )
        .await
        .unwrap();

        let results = client.results.lock().unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].state.as_str(), "cancelled");
        let vonk_agent_protocol::generated::AgentResultResult::RecipeJobRunResult(result) =
            &results[0].result
        else {
            panic!("expected canonical job result");
        };
        result.validate().unwrap();
        assert_eq!(result.job_id, request.job_id);
        assert_eq!(result.run_id, request.run_id);
        assert!(result.output_manifest.files.is_empty());
        assert_eq!(result.output_manifest.total_bytes, 0);
        assert_eq!(result.exit_code, 130);
        assert_eq!(
            result.reason.as_deref(),
            Some("controller cancellation requested")
        );
    }
}
