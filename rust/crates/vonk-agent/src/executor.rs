use async_trait::async_trait;
use chrono::{DateTime, FixedOffset, Utc};
use serde_json::{Value, json};
use std::{future::Future, path::Path, time::Duration};

use crate::supervisor_readiness::AgentRuntimeIdentity;
use crate::{
    client::{AgentHttpClient, ClientError},
    health::{HealthEvidence, wait_ready},
    host_runtime::HostRuntimeBoundary,
    image_importer::ImageImporter,
    oci::OciRuntime,
    process::ProcessRunner,
    recipe_builder::RecipeBuilder,
    state::{BeginDecision, StateError, StateStore},
    workloads::{Placement, image_digest},
};
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, HostRuntimeAction,
    RecipeOperationRequest, canonical_json, hex_sha256,
};

const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);

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
    ) -> ExecutionResult;
}

pub struct RejectingExecutor;

#[async_trait(?Send)]
impl Executor for RejectingExecutor {
    async fn execute(
        &self,
        claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
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

impl<R> RecipeExecutor<'_, R> {
    async fn execute_host_runtime(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
    ) -> Result<(), crate::host_runtime::HostRuntimeError> {
        let request_root = self.runtime_root.join("runtime-requests");
        HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
        }
        .execute(claim, action, arguments)
        .await
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
                        body: json!({"reason": error.to_string()}),
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
            RecipeOperationRequest::Install(request) => {
                let spec = match self
                    .client
                    .recipe_spec(&request.installation_id.to_string())
                    .await
                {
                    Ok(spec) => spec,
                    Err(_) => return failed("digest-bound recipe specification is unavailable"),
                };
                if image_digest(&spec.runtime.image)
                    .map(|value| format!("sha256:{value}"))
                    .as_deref()
                    != Some(request.image_digest.as_str())
                {
                    return failed("installed image digest does not match the accepted build");
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
                        &spec.endpoint.health_path,
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
                if self.runtime.recipe_digest(&installation_id).ok().as_deref()
                    != Some(&request.recipe_content_sha256)
                {
                    return failed("installed recipe identity does not match uninstall request");
                }
                if self.runtime.uninstall(&installation_id).is_err() {
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

#[derive(Debug, thiserror::Error)]
pub enum LoopError {
    #[error(transparent)]
    Client(#[from] ClientError),
    #[error(transparent)]
    State(#[from] StateError),
    #[error("agent heartbeat task failed")]
    HeartbeatTask,
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
        capabilities,
        wait_seconds,
        runtime_identity,
        HEARTBEAT_INTERVAL,
    )
    .await
}

async fn run_once_with_heartbeat_interval<C: LoopClient, E: Executor>(
    client: &C,
    state: &mut StateStore,
    executor: &E,
    capabilities: &[&str],
    wait_seconds: u64,
    runtime_identity: Option<&AgentRuntimeIdentity>,
    heartbeat_interval: Duration,
) -> Result<(), LoopError> {
    for result in state.pending_results()? {
        client.submit_result(&result).await?;
        state.acknowledge(&result)?;
    }
    let Some(claim) = client
        .claim(capabilities, wait_seconds, runtime_identity)
        .await?
    else {
        return Ok(());
    };
    let result = match state.begin(&claim, Utc::now()) {
        Ok(BeginDecision::Execute) => {
            let heartbeat_state = state.reopen()?;
            let (stop_heartbeat, heartbeat_stop) = tokio::sync::oneshot::channel();
            let (lease_deadline_sender, lease_deadline) =
                tokio::sync::watch::channel(claim.deadline);
            let heartbeat_task = tokio::spawn(run_heartbeats(
                client.clone(),
                heartbeat_state,
                claim.clone(),
                lease_deadline_sender,
                heartbeat_stop,
                heartbeat_interval,
            ));
            let executed =
                normalize_execution_result(&claim, executor.execute(&claim, lease_deadline).await);
            let _ = stop_heartbeat.send(());
            let heartbeat_result = heartbeat_task
                .await
                .map_err(|_| LoopError::HeartbeatTask)
                .and_then(|result| result);
            let cancelled = heartbeat_result.as_ref().copied().unwrap_or(false);
            let (result_state, result_body) = if cancelled {
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
    let reason = executed
        .body
        .get("reason")
        .and_then(Value::as_str)
        .unwrap_or("agent operation failed");
    let error_code = match claim.operation.as_str() {
        "recipe.build.v1" => "recipe_build_failed",
        "recipe.image.import.v1" => "recipe_image_import_failed",
        "recipe.install" => "recipe_install_failed",
        "recipe.start" => "recipe_start_failed",
        "recipe.stop" => "recipe_stop_failed",
        "recipe.uninstall" => "recipe_uninstall_failed",
        _ => "operation_failed",
    };
    ExecutionResult {
        state: "failed",
        body: json!({
            "error_code": error_code,
            "reason": reason,
            "status": "failed",
        }),
    }
}

async fn run_heartbeats<C: LoopClient>(
    client: C,
    mut state: StateStore,
    claim: AgentClaim,
    lease_deadline: tokio::sync::watch::Sender<DateTime<FixedOffset>>,
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
    }
}

#[cfg(test)]
mod tests {
    use super::{
        ExecutionResult, Executor, LoopClient, run_once_with_heartbeat_interval,
        wait_ready_with_runtime_guard,
    };
    use crate::{
        client::ClientError, state::StateStore, supervisor_readiness::AgentRuntimeIdentity,
    };
    use async_trait::async_trait;
    use chrono::{DateTime, Duration as ChronoDuration, FixedOffset, Utc};
    use serde_json::json;
    use std::{
        sync::{Arc, Mutex},
        time::Duration,
    };
    use tempfile::tempdir;
    use uuid::Uuid;
    use vonk_agent_protocol::{
        AgentClaim, AgentDirective, AgentProgress, AgentResult, canonical_json, hex_sha256,
    };

    const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

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
        ) -> ExecutionResult {
            ExecutionResult {
                state: "failed",
                body: json!({"reason": "rootless image build failed"}),
            }
        }
    }

    fn claim() -> AgentClaim {
        let payload = json!({"plan_digest": "a".repeat(64)});
        AgentClaim {
            attempt: 1,
            base_commit: "b".repeat(40),
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

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn long_execution_renews_and_persists_its_lease_before_result() {
        let directory = tempdir().unwrap();
        let original = claim();
        let heartbeats = Arc::new(Mutex::new(Vec::new()));
        let client = RecordingClient {
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
            &["recipe.install"],
            0,
            None,
            Duration::from_millis(10),
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
            &["recipe.install"],
            0,
            None,
            Duration::from_millis(10),
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
            &["recipe.install"],
            0,
            None,
            Duration::from_secs(10),
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
}
