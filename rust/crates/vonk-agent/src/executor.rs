use async_trait::async_trait;
use chrono::{DateTime, FixedOffset, Utc};
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
    client::{AgentHttpClient, ClientError},
    health::{HealthEvidence, wait_ready},
    host_runtime::{HostRuntimeBoundary, HostRuntimeOutcome},
    image_importer::ImageImporter,
    oci::OciRuntime,
    process::ProcessRunner,
    recipe_builder::RecipeBuilder,
    state::{BeginDecision, StateError, StateStore},
    workloads::{Placement, image_digest},
};
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, HostRuntimeAction, RecipeJobEvidence,
    RecipeJobFile, RecipeJobOutputLimits, RecipeJobOutputManifest, RecipeJobOutputMapping,
    RecipeJobRunResult, RecipeOperationRequest, canonical_json, hex_sha256,
};

const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
const JOB_CANCEL_EXIT_CODE: i32 = 130;
const JOB_CANCEL_STOP_TIMEOUT_SECONDS: u16 = 5;
const JOB_CANCEL_DRAIN_TIMEOUT: Duration = Duration::from_secs(20);

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
                Err(error) => ExecutionResult {
                    state: "failed",
                    body: json!({"reason": error.to_string()}),
                },
            };
        }
        self.recipes
            .execute(claim, lease_deadline, cancellation)
            .await
    }
}

impl<R> RecipeExecutor<'_, R> {
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

#[async_trait(?Send)]
impl<R: ProcessRunner> Executor for RecipeExecutor<'_, R> {
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        mut cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
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
                };
                match builder.build(&request, claim.operation_id, &archive) {
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
                let archive = match importer.staging_path(claim.operation_id) {
                    Ok(path) => path,
                    Err(_) => return failed("image import staging is unavailable"),
                };
                if self
                    .client
                    .download_artifact(&request.oci_layout_sha256, request.image_bytes, &archive)
                    .await
                    .is_err()
                {
                    return failed("exact OCI image archive is unavailable");
                }
                match importer.verify(&request, &archive) {
                    Ok(evidence)
                        if self
                            .execute_host_runtime(
                                claim,
                                HostRuntimeAction::ImageImport,
                                importer.runtime_arguments(&request, &archive),
                            )
                            .await
                            .is_ok() =>
                    {
                        ExecutionResult {
                            state: "succeeded",
                            body: serde_json::to_value(evidence).unwrap_or_else(
                                |_| json!({"reason": "image import evidence serialization failed"}),
                            ),
                        }
                    }
                    Ok(_) => failed("host runtime could not import the accepted OCI image"),
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
                    || image_digest(&spec.runtime.image)
                        .is_none_or(|digest| format!("sha256:{digest}") != request.image_digest)
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
                    let mut arguments = vec![plan.image_digest.clone()];
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
                let mut arguments = vec![plan.image_digest];
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
                let spec = match self
                    .client
                    .recipe_spec(&request.installation_id.to_string())
                    .await
                {
                    Ok(spec) => spec,
                    Err(_) => return failed("digest-bound recipe specification is unavailable"),
                };
                if spec.identity.recipe_revision_sha256 != request.recipe_content_sha256
                    || spec.topology.role != request.role
                    || spec.topology.rank != request.rank
                    || image_digest(&spec.runtime.image)
                        .map(|value| format!("sha256:{value}"))
                        .as_deref()
                        != Some(request.image_digest.as_str())
                {
                    return failed("recipe specification does not match the accepted install");
                }
                if self
                    .execute_host_runtime(
                        claim,
                        HostRuntimeAction::ImageInspect,
                        vec![
                            spec.runtime.image.clone(),
                            request.image_digest.clone(),
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
                        &request.recipe_content_sha256,
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
                if self.runtime.recipe_digest(&installation_id).ok().as_deref()
                    != Some(&request.recipe_content_sha256)
                    || self.runtime.verify_installation(&installation_id).is_err()
                {
                    return failed("installed recipe identity or artifact manifest does not match");
                }
                let spec = match self.runtime.load_spec(&installation_id) {
                    Ok(spec) => spec,
                    Err(_) => return failed("installed recipe specification is corrupt"),
                };
                let Some(endpoint) = spec.endpoint.as_ref() else {
                    return failed("installed recipe is not a persistent service");
                };
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
                let plan =
                    match self
                        .runtime
                        .prepare_start(&spec, &installation_id, &run_id, &placement)
                    {
                        Ok(plan) => plan,
                        Err(_) => {
                            return failed("container runtime could not prepare the workload");
                        }
                    };
                for hook in plan.pre_start {
                    let mut arguments = vec![plan.image_digest.clone()];
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
                let mut arguments = vec![plan.image_digest];
                arguments.extend(plan.main);
                let runtime_guard_arguments = arguments.clone();
                if self
                    .execute_host_runtime(claim, HostRuntimeAction::Start, arguments)
                    .await
                    .is_err()
                {
                    let _ = self.runtime.complete_stop(&run_id);
                    return failed("container runtime could not start the workload");
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
                if !wait_ready_with_runtime_guard(
                    wait_ready(
                        request.endpoint_address,
                        request.port,
                        &endpoint.health_path,
                        lease_deadline,
                    ),
                    runtime_guard,
                )
                .await
                {
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
                    return failed("workload did not become ready before its deadline");
                }
                let evidence = HealthEvidence {
                    recipe_revision_id: request.recipe_revision_id.to_string(),
                    recipe_content_sha256: request.recipe_content_sha256,
                    image_digest: image_digest(&spec.runtime.image)
                        .unwrap_or_default()
                        .to_owned(),
                    artifact_set_digest: self
                        .runtime
                        .artifact_set_digest(&installation_id)
                        .unwrap_or_default(),
                    model_identity: spec
                        .artifacts
                        .first()
                        .map(|artifact| format!("{}@{}", artifact.repository, artifact.revision))
                        .unwrap_or_default(),
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
                    if let Some(image_digest) = plan.image_digest {
                        for hook in plan.post_stop {
                            let mut arguments = vec![image_digest.clone()];
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
                if self
                    .runtime
                    .uninstall(&installation_id, &request.recipe_content_sha256)
                    .is_err()
                {
                    failed("installed recipe could not be safely removed")
                } else {
                    ExecutionResult {
                        state: "succeeded",
                        body: json!({"uninstalled": true}),
                    }
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
        "recipe.build.v1" => "recipe_build_failed",
        "recipe.image.import.v1" => "recipe_image_import_failed",
        "recipe.job.run.v1" => "recipe_job_run_failed",
        "recipe.install" => "recipe_install_failed",
        "recipe.start" => "recipe_start_failed",
        "recipe.stop" => "recipe_stop_failed",
        "recipe.uninstall" => "recipe_uninstall_failed",
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
    ExecutionResult {
        state: "failed",
        body,
    }
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
        ExecutionResult, Executor, InterruptibleJob, LoopClient, RunOncePolicy,
        normalize_execution_result, output_media_type, run_interruptible_job,
        run_once_with_claim_hook, run_once_with_heartbeat_interval, wait_ready_with_runtime_guard,
    };
    use crate::{client::ClientError, runtime_identity::AgentRuntimeIdentity, state::StateStore};
    use async_trait::async_trait;
    use chrono::{DateTime, Duration as ChronoDuration, FixedOffset, Utc};
    use serde_json::json;
    use std::{
        sync::{
            Arc, Mutex,
            atomic::{AtomicBool, Ordering},
        },
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
