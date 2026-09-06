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
        AgentHttpClient, ClientError, DistributionDownloadEvidence, DistributionProgress,
        ExactRecipeRunObservation,
    },
    health::{HealthEvidence, wait_ready, wait_ready_until},
    host_runtime::{HostRuntimeBoundary, HostRuntimeOutcome},
    image_importer::ImageImporter,
    oci::{OciRuntime, RecipeRunStartIdentity},
    process::ProcessRunner,
    recipe_builder::RecipeBuilder,
    state::{BeginDecision, StateError, StateStore},
    workloads::{CompiledExecutionPlan, Placement},
};
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, ArtifactDistributionRequest,
    HostRuntimeAction, RecipeJobEvidence, RecipeJobFile, RecipeJobOutputLimits,
    RecipeJobOutputManifest, RecipeJobOutputMapping, RecipeJobRunResult, RecipeOperationRequest,
    RecipeStartPhase, canonical_json, hex_sha256,
};

const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
const JOB_CANCEL_EXIT_CODE: i32 = 130;
const JOB_CANCEL_STOP_TIMEOUT_SECONDS: u16 = 5;
const JOB_CANCEL_DRAIN_TIMEOUT: Duration = Duration::from_secs(20);

fn parse_compiled_execution_plan(value: &Value) -> Result<CompiledExecutionPlan, ()> {
    let plan: CompiledExecutionPlan = serde_json::from_value(value.clone()).map_err(|_| ())?;
    plan.validate().map_err(|_| ())?;
    Ok(plan)
}

fn same_installed_workload(
    installed: &CompiledExecutionPlan,
    requested: &CompiledExecutionPlan,
) -> bool {
    installed.identity == requested.identity
        && installed.artifacts == requested.artifacts
        && installed.runtime.executable == requested.runtime.executable
        && installed.runtime.argv == requested.runtime.argv
        && installed.runtime.env == requested.runtime.env
        && installed.runtime.image_digest == requested.runtime.image_digest
        && installed.runtime_image == requested.runtime_image
        && installed.security.devices == requested.security.devices
        && installed.security.capabilities == requested.security.capabilities
        && installed.security.host_network == requested.security.host_network
        && installed.security.privileged == requested.security.privileged
        && installed.security.user == requested.security.user
        && installed.security.mounts == requested.security.mounts
        && installed.security.read_only_root == requested.security.read_only_root
        && installed.security.no_new_privileges == requested.security.no_new_privileges
        && installed.lifecycle == requested.lifecycle
        && installed.endpoint == requested.endpoint
        && installed.job == requested.job
        && installed.topology.name == requested.topology.name
        && installed.topology.mode == requested.topology.mode
        && installed.topology.backend == requested.topology.backend
        && installed.topology.node_count == requested.topology.node_count
}

fn readiness_identity(spec: &CompiledExecutionPlan) -> (String, String) {
    let image_digest = spec
        .runtime
        .image_digest
        .strip_prefix("sha256:")
        .unwrap_or_default()
        .to_owned();
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
    #[error("managed recipe run identity is invalid")]
    Runtime(#[from] crate::oci::OciError),
    #[error("exact recipe run inspection was not authorized")]
    Inspection(#[from] crate::host_runtime::HostRuntimeError),
    #[error("exact recipe run observation could not be reported")]
    Report(#[from] ClientError),
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
                    body: json!({"reason": "agent upgrade did not restart the service"}),
                },
                Err(error) => {
                    let mut body = json!({"reason": error.to_string()});
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

impl<R> RecipeExecutor<'_, R> {
    pub async fn report_exact_recipe_run_observations(
        &self,
    ) -> Result<usize, RecipeObservationError>
    where
        R: ProcessRunner,
    {
        let plans = match self.runtime.recipe_run_inspection_plans() {
            Ok(plans) => plans,
            Err(error) => {
                // A missing/corrupt canonical lifecycle must fail every exact
                // assignment on this node. Returning before the explicit empty
                // v2 snapshot would let an omitted rank retain stale health.
                let _ = self.client.report_exact_recipe_run_observations(&[]).await;
                return Err(RecipeObservationError::Runtime(error));
            }
        };
        if plans.is_empty() {
            self.client
                .report_exact_recipe_run_observations(&[])
                .await?;
            return Ok(0);
        }
        let observation_count = plans.len();
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
                    .await?;
                // This timestamp is part of the signed-grant freshness proof.
                // Capture it immediately after the local privileged inspection;
                // an owner-only HTTP probe follows and remains independently
                // bounded to five seconds.
                let observed_at = DateTime::from_timestamp(outcome.receipt.claims.observed_at, 0)
                    .ok_or(crate::host_runtime::HostRuntimeError::Protocol)?;
                let endpoint_ready = endpoint.map(|address| {
                    outcome.process_running
                        && self.runtime.readiness_request(
                            address,
                            plan.endpoint_port,
                            &plan.health_path,
                        )
                });
                let observation = ExactRecipeRunObservation {
                    schema_version: 1,
                    node_id: self.client.node_id().to_owned(),
                    observed_at,
                    binding: plan.binding,
                    endpoint_ready,
                    grant: outcome.grant,
                    observation_identity_sha256: outcome.observation_identity_sha256,
                    helper_receipt: outcome.receipt,
                };
                self.client
                    .report_exact_recipe_run_observations(std::slice::from_ref(&observation))
                    .await?;
                Ok::<(), RecipeObservationError>(())
            })
            .buffer_unordered(8)
            .collect::<Vec<_>>()
            .await;
        let errors = results
            .into_iter()
            .filter_map(Result::err)
            .collect::<Vec<_>>();
        if errors.iter().any(|error| !error.not_ready()) {
            // An incomplete exact snapshot is never allowed to preserve a
            // distributed route.  The explicit empty v2 report marks every
            // assigned exact-observation rank failed, but reporting failure is
            // still not allowed to terminate the claim lane.
            let _ = self.client.report_exact_recipe_run_observations(&[]).await;
            return Err(errors.into_iter().find(|error| !error.not_ready()).unwrap());
        }
        if let Some(error) = errors.into_iter().next() {
            return Err(error);
        }
        Ok(observation_count)
    }

    async fn execute_host_runtime_outcome(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
    ) -> Result<HostRuntimeOutcome, crate::host_runtime::HostRuntimeError> {
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
                    Err(crate::host_runtime::HostRuntimeError::Protocol)
                } else {
                    Ok(())
                }
            })
    }
}

async fn wait_ready_with_runtime_guard<R, G>(readiness: R, runtime_guard: G) -> bool
where
    R: Future<Output = Result<(), crate::health::HealthError>>,
    G: Future<Output = bool>,
{
    tokio::select! {
        result = readiness => result.is_ok(),
        running = runtime_guard => running,
    }
}

async fn wait_ready_with_runtime_guard_and_cancellation<R, G>(
    readiness: R,
    runtime_guard: G,
    mut cancellation: tokio::sync::watch::Receiver<bool>,
) -> bool
where
    R: Future<Output = Result<(), crate::health::HealthError>>,
    G: Future<Output = bool>,
{
    if *cancellation.borrow() {
        return false;
    }
    tokio::select! {
        result = readiness => result.is_ok(),
        running = runtime_guard => running,
        _ = cancellation.changed() => false,
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

fn distribution_success_evidence(evidence: DistributionDownloadEvidence) -> Value {
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
    let lease = lease_deadline.borrow().with_timezone(&Utc);
    let effective = start_deadline
        .map(|value| value.with_timezone(&Utc).min(lease))
        .unwrap_or(lease);
    Utc::now() < effective
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
        let lease = lease_deadline.borrow().with_timezone(&Utc);
        let effective = start_deadline
            .as_ref()
            .map(|value| value.with_timezone(&Utc).min(lease))
            .unwrap_or(lease);
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
            let request: ArtifactDistributionRequest =
                match serde_json::from_value(claim.payload.clone()) {
                    Ok(request) => request,
                    Err(_) => return failed("artifact distribution request is invalid"),
                };
            if request.validate().is_err() || request.plan_digest != claim.authority_revision {
                return failed("artifact distribution plan identity is invalid");
            }
            let destination = self
                .runtime
                .data_root
                .join("distribution")
                .join(&request.plan_digest);
            let (progress_sender, mut progress_receiver) =
                tokio::sync::watch::channel::<Option<DistributionProgress>>(None);
            let progress_client = self.client.clone();
            let progress_claim = claim.clone();
            let progress_deadline = lease_deadline.clone();
            let progress_task = tokio::spawn(async move {
                // Progress is a snapshot, not an event log. Coalesce fast
                // transfer updates instead of accumulating an unbounded queue
                // of heartbeat requests before image import can begin.
                let mut cadence = tokio::time::interval(Duration::from_secs(1));
                cadence.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
                while progress_receiver.changed().await.is_ok() {
                    cadence.tick().await;
                    let Some(item) = progress_receiver.borrow_and_update().clone() else {
                        continue;
                    };
                    let progress = AgentProgress {
                        attempt: progress_claim.attempt,
                        deadline: *progress_deadline.borrow(),
                        fence: progress_claim.fence,
                        job_id: progress_claim.job_id,
                        node_id: progress_claim.node_id.clone(),
                        operation_id: progress_claim.operation_id,
                        progress: json!({
                            "phase": "copying",
                            "object_sha256": item.object_sha256,
                            "kind": item.kind,
                            "bytes": item.bytes,
                            "total_bytes": item.total_bytes,
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
                        Err(error) if error.retryable() && attempt < 2 => {
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
                Err(_) => failed("Controller distribution could not be verified and retained"),
            };
        }
        let request = match RecipeOperationRequest::parse(claim) {
            Ok(request) => request,
            Err(_) => return failed("recipe operation payload is invalid"),
        };
        match request {
            RecipeOperationRequest::Build(request) => {
                let archive = match self
                    .client
                    .source_bundle(&request.source_bundle_sha256, request.source_bundle_bytes)
                    .await
                {
                    Ok(archive) => archive,
                    Err(_) => return failed("authorized source bundle is unavailable"),
                };
                let builder = RecipeBuilder {
                    runner: self.runtime.runner,
                    data_root: self.runtime.data_root,
                    runtime_root: self.runtime_root,
                    egress_binary: Path::new("/usr/lib/vonk-forge/vonk-build-egress"),
                };
                let cancelled = || *cancellation.borrow();
                match builder.build_cancellable(&request, claim.operation_id, &archive, &cancelled)
                {
                    Ok(evidence) => {
                        if self
                            .client
                            .upload_recipe_image(
                                request.build_id,
                                &evidence.image_digest,
                                &evidence.oci_layout_sha256,
                                evidence.image_bytes,
                                &builder.layout_path(claim.operation_id),
                            )
                            .await
                            .is_err()
                        {
                            return failed("built OCI image could not be stored by the controller");
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
                            let code = match error {
                                crate::host_runtime::HostRuntimeError::HelperRejected { code } => {
                                    code
                                }
                                crate::host_runtime::HostRuntimeError::Io(_) => {
                                    "runtime_helper_unavailable".to_owned()
                                }
                                crate::host_runtime::HostRuntimeError::Controller(_) => {
                                    "runtime_authority_unavailable".to_owned()
                                }
                                crate::host_runtime::HostRuntimeError::Protocol => {
                                    "runtime_helper_protocol_invalid".to_owned()
                                }
                            };
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
                let Some(job) = spec.job.as_ref() else {
                    return failed_job(
                        &request,
                        1,
                        started,
                        "installed recipe is not a one-shot job",
                    );
                };
                if job.interface != request.interface
                    || request.timeout_seconds == 0
                    || request.timeout_seconds > job.timeout_seconds
                    || spec.endpoint.is_some()
                    || spec.runtime.image_digest != request.image_digest
                {
                    return failed_job(
                        &request,
                        1,
                        started,
                        "job request does not match the installed workload",
                    );
                }
                if self
                    .runtime
                    .ensure_memory_available(
                        request.reserved_memory_bytes,
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
                            input.size_bytes,
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
                let input_manifest = json!({
                    "schema_version": 1,
                    "total_bytes": request.input_total_bytes,
                    "files": request.inputs,
                });
                let input_manifest = match canonical_json(&input_manifest) {
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
                let placement = Placement {
                    endpoint_address: None,
                    rank: 0,
                    role: request.role.clone(),
                    world_size: 1,
                    local_address: None,
                    master_address: None,
                    master_port: None,
                    port: 1024,
                    reserved_memory_bytes: request.reserved_memory_bytes,
                };
                let plan = match self.runtime.prepare_job_start(
                    &spec,
                    &installation_id,
                    &job_scope,
                    &placement,
                    &request.parameters,
                    request.timeout_seconds,
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
                for hook in plan.pre_start {
                    let mut arguments = vec![
                        plan.archive_sha256.clone(),
                        plan.registry_index_digest.clone(),
                        plan.platform_manifest_digest.clone(),
                        plan.image_reference.clone(),
                    ];
                    arguments.extend(hook);
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
                        Some(code) => (code, Some("job adapter exited unsuccessfully")),
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
                            output.size_bytes,
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
                let inline_spec =
                    match parse_compiled_execution_plan(&request.compiled_execution_plan) {
                        Ok(spec) => spec,
                        Err(_) => return failed("compiled execution plan is invalid"),
                    };
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
                if self
                    .runtime
                    .ensure_disk_available(request.expected_bytes)
                    .is_err()
                {
                    return failed("local disk capacity changed after install admission");
                }
                if self
                    .runtime
                    .install(
                        &spec,
                        &request.installation_id.to_string(),
                        &spec.identity.recipe_revision_sha256,
                    )
                    .is_err()
                {
                    return failed("recipe artifacts or container image could not be installed");
                }
                let installed_bytes = self
                    .runtime
                    .installed_bytes(&request.installation_id.to_string())
                    .unwrap_or(request.expected_bytes);
                ExecutionResult {
                    state: "succeeded",
                    body: json!({"installed_bytes": installed_bytes}),
                }
            }
            RecipeOperationRequest::Start(request) => {
                let installation_id = request.installation_id.to_string();
                let spec = match parse_compiled_execution_plan(&request.compiled_execution_plan) {
                    Ok(spec) => spec,
                    Err(_) => return failed("compiled execution plan is invalid"),
                };
                if spec.identity.recipe_revision_sha256 != request.recipe_content_sha256
                    || spec.runtime.image_digest != request.image_digest
                    || spec.topology.rank != request.rank
                    || spec.topology.role != request.role
                    || spec.topology.world_size != request.world_size
                    || spec.runtime.placement.rank != request.rank
                    || spec.runtime.placement.role != request.role
                    || spec.runtime.placement.world_size != request.world_size
                    || spec.runtime.placement.port != request.port
                    || spec.runtime.placement.reserved_memory_bytes != request.reserved_memory_bytes
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
                let placement = Placement {
                    endpoint_address: Some(request.endpoint_address),
                    rank: request.rank,
                    role: request.role.clone(),
                    world_size: request.world_size,
                    local_address: request.local_address,
                    master_address: request.master_address,
                    master_port: request.master_port,
                    port: request.port,
                    reserved_memory_bytes: request.reserved_memory_bytes,
                };
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
                    && !before_phase_deadline(&lease_deadline, request.start_deadline.as_ref())
                {
                    return failed("distributed start deadline elapsed before execution");
                }
                let plan = if collective_readiness {
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
                        Err(_) => {
                            return failed("container runtime could not prepare the workload");
                        }
                    }
                };
                if collective_readiness && !plan.pre_start.is_empty() {
                    return failed("retained workload unexpectedly contains start hooks");
                }
                for hook in plan.pre_start {
                    let mut arguments = vec![
                        plan.archive_sha256.clone(),
                        plan.registry_index_digest.clone(),
                        plan.platform_manifest_digest.clone(),
                        plan.image_reference.clone(),
                    ];
                    arguments.extend(hook);
                    if self
                        .execute_host_runtime(claim, HostRuntimeAction::Start, arguments)
                        .await
                        .is_err()
                    {
                        let _ = self.runtime.complete_stop(&run_id);
                        return failed("container runtime pre-start hook failed");
                    }
                }
                let mut arguments = vec![
                    plan.archive_sha256.clone(),
                    plan.registry_index_digest.clone(),
                    plan.platform_manifest_digest.clone(),
                    plan.image_reference.clone(),
                ];
                arguments.extend(plan.main);
                let runtime_guard_arguments = arguments.clone();
                if collective_readiness {
                    if self
                        .execute_host_runtime(
                            claim,
                            HostRuntimeAction::RunInspect,
                            runtime_guard_arguments.clone(),
                        )
                        .await
                        .is_err()
                    {
                        return failed("collective workload is not running with exact identity");
                    }
                } else if self
                    .execute_host_runtime(claim, HostRuntimeAction::Start, arguments)
                    .await
                    .is_err()
                {
                    let _ = self.runtime.complete_stop(&run_id);
                    return failed("container runtime could not start the workload");
                }
                if rank_launch {
                    let first_inspect = self
                        .execute_host_runtime(
                            claim,
                            HostRuntimeAction::RunInspect,
                            runtime_guard_arguments.clone(),
                        )
                        .await;
                    let stable = if first_inspect.is_err()
                        || *cancellation.borrow()
                        || !before_phase_deadline(&lease_deadline, request.start_deadline.as_ref())
                    {
                        false
                    } else {
                        wait_for_launch_stability(
                            lease_deadline.clone(),
                            cancellation.clone(),
                            request.start_deadline,
                            Duration::from_secs(2),
                        )
                        .await
                            && self
                                .execute_host_runtime(
                                    claim,
                                    HostRuntimeAction::RunInspect,
                                    runtime_guard_arguments.clone(),
                                )
                                .await
                                .is_ok()
                            && before_phase_deadline(
                                &lease_deadline,
                                request.start_deadline.as_ref(),
                            )
                    };
                    if !stable {
                        let _ = self
                            .execute_host_runtime(
                                claim,
                                HostRuntimeAction::Stop,
                                vec![
                                    run_id.clone(),
                                    spec.lifecycle.stop_timeout_seconds.to_string(),
                                ],
                            )
                            .await;
                        let _ = self.runtime.complete_stop(&run_id);
                        return failed("rank process did not remain stable after launch");
                    }
                    let artifact_set_digest =
                        match self.runtime.artifact_set_digest(&installation_id) {
                            Ok(digest) => digest,
                            Err(_) => {
                                let _ = self
                                    .execute_host_runtime(
                                        claim,
                                        HostRuntimeAction::Stop,
                                        vec![
                                            run_id.clone(),
                                            spec.lifecycle.stop_timeout_seconds.to_string(),
                                        ],
                                    )
                                    .await;
                                let _ = self.runtime.complete_stop(&run_id);
                                return failed("rank launch evidence is unavailable");
                            }
                        };
                    let runtime_arguments_sha256 = match canonical_json(&runtime_guard_arguments) {
                        Ok(arguments) => hex_sha256(&arguments),
                        Err(_) => return failed("rank launch evidence is unavailable"),
                    };
                    let (evidence_image_digest, evidence_model_identity) =
                        readiness_identity(&spec);
                    let evidence = json!({
                        "phase": "rank-launch",
                        "run_id": run_id,
                        "run_generation": request.run_generation,
                        "recipe_revision_id": request.recipe_revision_id.to_string(),
                        "recipe_content_sha256": request.recipe_content_sha256,
                        "image_digest": evidence_image_digest,
                        "artifact_set_digest": artifact_set_digest,
                        "runtime_arguments_sha256": runtime_arguments_sha256,
                        "model_identity": evidence_model_identity,
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
                    });
                    let (evidence, evidence_digest) = evidence_with_digest(evidence);
                    return ExecutionResult {
                        state: "succeeded",
                        body: json!({
                            "evidence": evidence,
                            "evidence_digest": evidence_digest,
                        }),
                    };
                }
                let runtime_guard = async {
                    loop {
                        tokio::time::sleep(Duration::from_secs(10)).await;
                        if self
                            .execute_host_runtime(
                                claim,
                                HostRuntimeAction::RunInspect,
                                runtime_guard_arguments.clone(),
                            )
                            .await
                            .is_err()
                        {
                            return false;
                        }
                    }
                };
                let ready = if collective_readiness {
                    wait_ready_with_runtime_guard_and_cancellation(
                        wait_ready_until(
                            request.endpoint_address,
                            request.port,
                            &endpoint.health_path,
                            lease_deadline,
                            request.start_deadline,
                        ),
                        runtime_guard,
                        cancellation.clone(),
                    )
                    .await
                } else {
                    wait_ready_with_runtime_guard(
                        wait_ready(
                            request.endpoint_address,
                            request.port,
                            &endpoint.health_path,
                            lease_deadline,
                        ),
                        runtime_guard,
                    )
                    .await
                };
                if !ready {
                    if !collective_readiness {
                        let _ = self
                            .execute_host_runtime(
                                claim,
                                HostRuntimeAction::Stop,
                                vec![
                                    run_id.clone(),
                                    spec.lifecycle.stop_timeout_seconds.to_string(),
                                ],
                            )
                            .await;
                        let _ = self.runtime.complete_stop(&run_id);
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
                    let endpoint_url = format!(
                        "http://{}:{}",
                        match request.endpoint_address {
                            std::net::IpAddr::V4(address) => address.to_string(),
                            std::net::IpAddr::V6(address) => format!("[{address}]"),
                        },
                        request.port
                    );
                    let runtime_arguments_sha256 = match canonical_json(&runtime_guard_arguments) {
                        Ok(arguments) => hex_sha256(&arguments),
                        Err(_) => return failed("collective readiness evidence is unavailable"),
                    };
                    let (evidence_image_digest, evidence_model_identity) =
                        readiness_identity(&spec);
                    let evidence = json!({
                        "phase": "collective-readiness",
                        "run_id": run_id,
                        "run_generation": request.run_generation,
                        "recipe_revision_id": request.recipe_revision_id.to_string(),
                        "recipe_content_sha256": request.recipe_content_sha256,
                        "image_digest": evidence_image_digest,
                        "artifact_set_digest": artifact_set_digest,
                        "runtime_arguments_sha256": runtime_arguments_sha256,
                        "model_identity": evidence_model_identity,
                        "rank": request.rank,
                        "role": request.role,
                        "world_size": request.world_size,
                        "local_address": request.local_address,
                        "master_address": request.master_address,
                        "master_port": request.master_port,
                        "endpoint": endpoint_url,
                        "memory_reservation_bytes": request.reserved_memory_bytes,
                        "ready": true,
                    });
                    let (evidence, evidence_digest) = evidence_with_digest(evidence);
                    return ExecutionResult {
                        state: "succeeded",
                        body: json!({
                            "endpoint": endpoint_url,
                            "evidence": evidence,
                            "evidence_digest": evidence_digest,
                        }),
                    };
                }
                let (evidence_image_digest, evidence_model_identity) = readiness_identity(&spec);
                let evidence = HealthEvidence {
                    recipe_revision_id: request.recipe_revision_id.to_string(),
                    recipe_content_sha256: request.recipe_content_sha256,
                    image_digest: evidence_image_digest,
                    artifact_set_digest: self
                        .runtime
                        .artifact_set_digest(&installation_id)
                        .unwrap_or_default(),
                    model_identity: evidence_model_identity,
                    rank: request.rank,
                    world_size: request.world_size,
                    endpoint: format!(
                        "http://{}:{}",
                        match request.endpoint_address {
                            std::net::IpAddr::V4(address) => address.to_string(),
                            std::net::IpAddr::V6(address) => format!("[{address}]"),
                        },
                        request.port
                    ),
                    memory_reservation_bytes: request.reserved_memory_bytes,
                    ready: true,
                };
                let evidence_digest = canonical_json(&evidence)
                    .map(|value| hex_sha256(&value))
                    .unwrap_or_default();
                let mut evidence_value = serde_json::to_value(&evidence).unwrap_or_default();
                if let Some(document) = evidence_value.as_object_mut() {
                    document.insert(
                        "evidence_digest".to_owned(),
                        Value::String(evidence_digest.clone()),
                    );
                }
                ExecutionResult {
                    state: "succeeded",
                    body: json!({
                        "endpoint": evidence.endpoint,
                        "evidence": evidence_value,
                        "evidence_digest": evidence_digest,
                    }),
                }
            }
            RecipeOperationRequest::Stop(request) => {
                let run_id = request.run_id.to_string();
                let plan = match self.runtime.prepare_stop(&run_id) {
                    Ok(plan) => plan,
                    Err(_) => return failed("container runtime could not prepare workload stop"),
                };
                if self
                    .execute_host_runtime(claim, HostRuntimeAction::Stop, plan.remove)
                    .await
                    .is_err()
                {
                    failed("container runtime could not stop the workload")
                } else {
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
                    if self.runtime.complete_stop(&run_id).is_err() {
                        return failed("container runtime stop metadata could not be finalized");
                    }
                    ExecutionResult {
                        state: "succeeded",
                        body: json!({"stopped": true}),
                    }
                }
            }
            RecipeOperationRequest::Uninstall(request) => {
                let installation_id = request.installation_id.to_string();
                match self.runtime.recipe_digest_if_present(&installation_id) {
                    Ok(None) => {
                        return ExecutionResult {
                            state: "succeeded",
                            body: json!({"uninstalled": true, "already_absent": true}),
                        };
                    }
                    Ok(Some(recipe_digest)) if recipe_digest == request.recipe_content_sha256 => {}
                    Ok(Some(_)) | Err(_) => {
                        return failed(
                            "installed recipe identity does not match uninstall request",
                        );
                    }
                }
                if request.cleanup_model_version_sha256.is_some() {
                    return failed("legacy model cleanup authority is not accepted");
                }
                let removed_model_bytes = self
                    .runtime
                    .uninstall(&installation_id, &request.recipe_content_sha256)
                    .map(|()| 0);
                if removed_model_bytes.is_err() {
                    failed("installed recipe could not be safely removed")
                } else {
                    ExecutionResult {
                        state: "succeeded",
                        body: json!({
                            "uninstalled": true,
                            "removed_model_bytes": removed_model_bytes.unwrap_or(0),
                        }),
                    }
                }
            }
            RecipeOperationRequest::ModelUninstall(request) => {
                let _ = request;
                failed("legacy model uninstall authority is not accepted")
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

fn failed_job(
    request: &vonk_agent_protocol::RecipeJobRunRequest,
    exit_code: i32,
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

fn empty_job_output_manifest() -> RecipeJobOutputManifest {
    let value = json!({"schema_version": 1, "total_bytes": 0, "files": []});
    let manifest_sha256 = canonical_json(&value)
        .map(|bytes| hex_sha256(&bytes))
        .unwrap_or_default();
    RecipeJobOutputManifest {
        schema_version: 1,
        manifest_sha256,
        total_bytes: 0,
        files: Vec::new(),
    }
}

fn job_result_body(
    request: &vonk_agent_protocol::RecipeJobRunRequest,
    exit_code: i32,
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
            elapsed_milliseconds: u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX),
            // The helper does not expose a cgroup peak for transient containers yet. Null is
            // honest unavailable evidence; zero would falsely claim a measurement.
            peak_memory_bytes: None,
        },
        reason: reason.map(str::to_owned),
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
    if entries.len() > usize::from(limits.max_files) {
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
        if metadata.len() > limits.max_file_bytes {
            return Err("job output file size exceeded its bound");
        }
        total_bytes = total_bytes
            .checked_add(metadata.len())
            .filter(|total| *total <= limits.max_total_bytes)
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
            size_bytes: observed,
            sha256: hex::encode(hasher.finalize()),
        });
    }
    let manifest = json!({"schema_version": 1, "total_bytes": total_bytes, "files": files});
    let manifest_sha256 = canonical_json(&manifest)
        .map(|bytes| hex_sha256(&bytes))
        .map_err(|_| "job output manifest is invalid")?;
    let files = serde_json::from_value(manifest["files"].clone())
        .map_err(|_| "job output manifest is invalid")?;
    Ok(RecipeJobOutputManifest {
        schema_version: 1,
        manifest_sha256,
        total_bytes,
        files,
    })
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

#[derive(Clone, Copy)]
struct RunOncePolicy<'a> {
    capabilities: &'a [&'a str],
    wait_seconds: u64,
    runtime_identity: Option<&'a AgentRuntimeIdentity>,
    heartbeat_interval: Duration,
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
    for result in state.pending_results()? {
        client.submit_result(&result).await?;
        state.acknowledge(&result)?;
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
            let heartbeat_task = tokio::spawn(run_heartbeats(
                client.clone(),
                heartbeat_state,
                claim.clone(),
                lease_deadline_sender,
                cancellation_sender,
                heartbeat_stop,
                policy.heartbeat_interval,
            ));
            let executed = normalize_execution_result(
                &claim,
                executor.execute(&claim, lease_deadline, cancellation).await,
            );
            let _ = stop_heartbeat.send(());
            let heartbeat_result = heartbeat_task
                .await
                .map_err(|_| LoopError::HeartbeatTask)
                .and_then(|result| result);
            let cancelled = heartbeat_result.as_ref().copied().unwrap_or(false);
            let (result_state, result_body) = if cancelled && claim.operation != "recipe.job.run.v1"
            {
                (
                    "waiting-for-operator",
                    json!({"reason": "controller cancellation was observed during execution"}),
                )
            } else {
                (executed.state, executed.body)
            };
            let result = state.finish(&claim, result_state, result_body)?;
            heartbeat_result?;
            result
        }
        Ok(BeginDecision::Replay(result)) => result,
        Err(StateError::Busy) => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    client.submit_result(&result).await?;
    state.acknowledge(&result)?;
    Ok(())
}

fn normalize_execution_result(claim: &AgentClaim, executed: ExecutionResult) -> ExecutionResult {
    if executed.state != "failed" {
        return executed;
    }
    if claim.operation == "recipe.job.run.v1" {
        return executed;
    }
    let reason = executed
        .body
        .get("reason")
        .and_then(Value::as_str)
        .unwrap_or("agent operation failed");
    let error_code = match claim.operation.as_str() {
        "agent.upgrade.v1" => "agent_upgrade_failed",
        "artifact.distribution.v1" => "artifact_distribution_failed",
        "recipe.build.v1" => "recipe_build_failed",
        "recipe.image.import.v1" => "recipe_image_import_failed",
        "recipe.job.run.v1" => "recipe_job_run_failed",
        "recipe.install" => "recipe_install_failed",
        "recipe.start" => "recipe_start_failed",
        "recipe.stop" => "recipe_stop_failed",
        "recipe.uninstall" => "recipe_uninstall_failed",
        "recipe.model-uninstall.v1" => "recipe_model_uninstall_failed",
        _ => "operation_failed",
    };
    let mut body = json!({
        "error_code": error_code,
        "reason": reason,
        "status": "failed",
    });
    for field in ["stage", "diagnostic"] {
        if let Some(value) = executed.body.get(field).and_then(Value::as_str) {
            body[field] = Value::String(value.to_owned());
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
    )
}

async fn run_heartbeats<C: LoopClient>(
    client: C,
    mut state: StateStore,
    claim: AgentClaim,
    lease_deadline: tokio::sync::watch::Sender<DateTime<FixedOffset>>,
    cancellation: tokio::sync::watch::Sender<bool>,
    mut stop: tokio::sync::oneshot::Receiver<()>,
    interval: Duration,
) -> Result<bool, LoopError> {
    let mut deadline = claim.deadline;
    let mut cancellation_observed = false;
    loop {
        tokio::select! {
            _ = &mut stop => return Ok(cancellation_observed),
            _ = tokio::time::sleep(interval) => {}
        }
        let progress = AgentProgress {
            attempt: claim.attempt,
            deadline,
            fence: claim.fence,
            job_id: claim.job_id,
            node_id: claim.node_id.clone(),
            operation_id: claim.operation_id,
            progress: json!({"phase": "executing"}),
            schema_version: claim.schema_version,
        };
        let directive = client.heartbeat(&progress).await?;
        state.apply_heartbeat(&progress, &directive)?;
        lease_deadline.send_replace(directive.deadline);
        deadline = directive.deadline;
        cancellation_observed |= directive.cancel_requested;
        if directive.cancel_requested {
            cancellation.send_replace(true);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        ExecutionResult, Executor, InterruptibleJob, LoopClient, RecipeExecutor, RunOncePolicy,
        distribution_success_evidence, normalize_execution_result, output_media_type,
        parse_compiled_execution_plan, readiness_identity, run_interruptible_job,
        run_once_with_claim_hook, run_once_with_heartbeat_interval, wait_for_launch_stability,
        wait_ready_with_runtime_guard,
    };
    use crate::{
        client::{AgentHttpClient, ClientError, DistributionDownloadEvidence},
        oci::OciRuntime,
        process::{ProcessError, ProcessOutput, ProcessRunner, Program},
        runtime_identity::AgentRuntimeIdentity,
        state::StateStore,
    };
    use async_trait::async_trait;
    use chrono::{DateTime, Duration as ChronoDuration, FixedOffset, Utc};
    use serde_json::json;
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
    use vonk_agent_protocol::{
        AgentClaim, AgentDirective, AgentProgress, AgentResult, RecipeJobOutputMapping,
        canonical_json, hex_sha256,
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
        assert_eq!(image_digest, &plan.runtime.image_digest[7..]);
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

    #[tokio::test]
    async fn corrupt_exact_lifecycle_emits_an_explicit_empty_v2_report() {
        let data = tempdir().unwrap();
        let runtime = tempdir().unwrap();
        let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";
        fs::create_dir_all(data.path().join("runs").join(run_id)).unwrap();
        let metadata = data.path().join("run-metadata").join(run_id);
        fs::create_dir_all(&metadata).unwrap();
        fs::write(metadata.join("lifecycle.json"), b"not-json").unwrap();

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            let header_end = loop {
                let size = stream.read(&mut buffer).unwrap();
                assert_ne!(size, 0);
                request.extend_from_slice(&buffer[..size]);
                if let Some(index) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
                    break index + 4;
                }
            };
            let headers = std::str::from_utf8(&request[..header_end]).unwrap();
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
            write!(
                stream,
                "HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            .unwrap();
            request
        });
        let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), NODE_ID);
        let runner = NoProcess;
        let executor = RecipeExecutor {
            client: &client,
            runtime: OciRuntime {
                runner: &runner,
                data_root: data.path(),
                huggingface_curl_config: None,
            },
            runtime_root: runtime.path(),
            observation_receipt_public_key: [0; 32],
        };

        assert!(
            executor
                .report_exact_recipe_run_observations()
                .await
                .is_err()
        );
        let request = server.join().unwrap();
        let (headers, body) = request
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| (&request[..index], &request[index + 4..]))
            .unwrap();
        assert!(
            std::str::from_utf8(headers)
                .unwrap()
                .starts_with("POST /agent/v1/recipe-runs/observations HTTP/1.1\r\n")
        );
        let body: serde_json::Value = serde_json::from_slice(body).unwrap();
        assert_eq!(body["schema_version"], 2);
        assert_eq!(body["runs"], serde_json::json!([]));
    }

    #[test]
    fn failed_recipe_build_preserves_only_safe_classified_evidence() {
        let mut build_claim = claim();
        build_claim.operation = "recipe.build.v1".to_owned();
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
            result.body,
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
            result: body,
            schema_version: 1,
            state: "succeeded".to_owned(),
        };
        result.validate().unwrap();
    }

    #[test]
    fn distribution_failure_uses_operation_specific_result_code() {
        let mut distribution_claim = claim();
        distribution_claim.operation = "artifact.distribution.v1".to_owned();
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

        assert!(!wait_ready_with_runtime_guard(readiness, async { false }).await);
    }

    #[tokio::test]
    async fn successful_readiness_ends_a_still_running_runtime_guard() {
        let runtime_guard = std::future::pending::<bool>();

        assert!(wait_ready_with_runtime_guard(async { Ok(()) }, runtime_guard).await);
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
            if self.fail_heartbeat {
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

    struct HeartbeatGatedExecutor {
        heartbeats: Arc<Mutex<Vec<AgentProgress>>>,
        minimum: usize,
        observed_deadline: Arc<Mutex<Option<DateTime<FixedOffset>>>>,
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
                body: json!({"status": "ok"}),
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
            _claim: &AgentClaim,
            _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
            mut cancellation: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            super::wait_for_cancellation(&mut cancellation).await;
            ExecutionResult {
                state: "cancelled",
                body: json!({"exit_code": 130, "reason": "controller cancellation requested"}),
            }
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
                body: json!({"status": "ok"}),
            }
        }
    }

    fn claim() -> AgentClaim {
        let payload = json!({"plan_digest": "a".repeat(64)});
        AgentClaim {
            attempt: 1,
            authority_revision: "b".repeat(64),
            deadline: (Utc::now() + ChronoDuration::seconds(20))
                .with_timezone(&FixedOffset::east_opt(0).unwrap()),
            fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
            job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
            node_id: NODE_ID.to_owned(),
            operation: "recipe.install".to_owned(),
            operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
            payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
            payload,
            schema_version: 1,
        }
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
            },
            || Ok(()),
        )
        .await
        .unwrap();

        let heartbeats = client.heartbeats.lock().unwrap();
        assert!(heartbeats.len() >= 2);
        drop(heartbeats);
        let results = client.results.lock().unwrap();
        assert_eq!(results.len(), 1);
        assert!(results[0].deadline > original.deadline);
        assert!(observed_deadline.lock().unwrap().unwrap() > original.deadline);
        assert!(state.pending_results().unwrap().is_empty());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn heartbeat_failure_leaves_a_durable_terminal_result_not_a_busy_attempt() {
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
            minimum: 1,
            observed_deadline: Arc::new(Mutex::new(None)),
        };
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

        let error = run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &executor,
            RunOncePolicy {
                capabilities: &["recipe.install"],
                wait_seconds: 0,
                runtime_identity: None,
                heartbeat_interval: Duration::from_millis(10),
            },
            || Ok(()),
        )
        .await
        .unwrap_err();

        assert!(matches!(
            error,
            super::LoopError::Client(ClientError::Retryable)
        ));
        assert_eq!(state.pending_results().unwrap().len(), 1);
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
            },
            || Ok(()),
        )
        .await
        .unwrap();

        assert_eq!(
            client.results.lock().unwrap()[0].result,
            json!({
                "error_code": "recipe_install_failed",
                "reason": "rootless image build failed",
                "status": "failed"
            })
        );
    }

    #[test]
    fn agent_upgrade_failure_preserves_only_bounded_helper_diagnostics() {
        let mut upgrade_claim = claim();
        upgrade_claim.operation = "agent.upgrade.v1".to_owned();
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
            result.body,
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
        import_claim.operation = "recipe.image.import.v1".to_owned();
        for code in [
            "runtime_helper_unavailable",
            "runtime_authority_unavailable",
            "runtime_helper_protocol_invalid",
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
        let mut job_claim = claim();
        job_claim.operation = "recipe.job.run.v1".to_owned();
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
            },
            || Ok(()),
        )
        .await
        .unwrap();

        let results = client.results.lock().unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].state, "cancelled");
        assert_eq!(results[0].result["exit_code"], 130);
        assert_eq!(
            results[0].result["reason"],
            "controller cancellation requested"
        );
    }
}
