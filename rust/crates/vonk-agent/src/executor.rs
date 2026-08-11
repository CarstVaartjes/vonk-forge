use async_trait::async_trait;
use chrono::Utc;
use serde_json::{Value, json};
use std::path::Path;

use crate::supervisor_readiness::AgentRuntimeIdentity;
use crate::{
    client::{AgentHttpClient, ClientError},
    health::{HealthEvidence, wait_ready},
    image_importer::ImageImporter,
    oci::OciRuntime,
    process::ProcessRunner,
    recipe_builder::RecipeBuilder,
    state::{BeginDecision, StateError, StateStore},
    workloads::{Placement, image_digest},
};
use vonk_agent_protocol::{AgentClaim, RecipeOperationRequest, canonical_json, hex_sha256};

pub struct ExecutionResult {
    pub state: &'static str,
    pub body: Value,
}

#[async_trait(?Send)]
pub trait Executor {
    async fn execute(&self, claim: &AgentClaim) -> ExecutionResult;
}

pub struct RejectingExecutor;

#[async_trait(?Send)]
impl Executor for RejectingExecutor {
    async fn execute(&self, claim: &AgentClaim) -> ExecutionResult {
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

#[async_trait(?Send)]
impl<R: ProcessRunner> Executor for RecipeExecutor<'_, R> {
    async fn execute(&self, claim: &AgentClaim) -> ExecutionResult {
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
                    runner: self.runtime.runner,
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
                match importer.import(&request, &archive) {
                    Ok(evidence) => ExecutionResult {
                        state: "succeeded",
                        body: serde_json::to_value(evidence).unwrap_or_else(
                            |_| json!({"reason": "image import evidence serialization failed"}),
                        ),
                    },
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
                if self
                    .runtime
                    .start(&spec, &installation_id, &run_id, &placement)
                    .is_err()
                {
                    return failed("container runtime could not start the workload");
                }
                if wait_ready(
                    request.port,
                    &spec.endpoint.health_path,
                    claim.deadline.with_timezone(&Utc),
                )
                .await
                .is_err()
                {
                    let _ = self.runtime.stop(&run_id);
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
                if self.runtime.stop(&request.run_id.to_string()).is_err() {
                    failed("container runtime could not stop the workload")
                } else {
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
}

pub async fn run_once<E: Executor>(
    client: &AgentHttpClient,
    state: &mut StateStore,
    executor: &E,
    capabilities: &[&str],
    wait_seconds: u64,
    runtime_identity: Option<&AgentRuntimeIdentity>,
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
            let executed = executor.execute(&claim).await;
            state.finish(&claim, executed.state, executed.body)?
        }
        Ok(BeginDecision::Replay(result)) => result,
        Err(StateError::Busy) => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    client.submit_result(&result).await?;
    state.acknowledge(&result)?;
    Ok(())
}
