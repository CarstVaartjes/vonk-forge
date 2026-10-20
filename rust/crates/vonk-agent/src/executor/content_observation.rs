//! One read-only observation path for content/preparation custody on startup
//! and every idle tick. Runtime and package children remain separate tracks.
use super::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ContentKind {
    Preflight,
    Distribution,
    Build,
    BuildCleanup,
    Install,
    Reconcile,
    Uninstall,
}
impl ContentKind {
    pub fn of(operation: &AgentOperation) -> Option<Self> {
        match operation {
            AgentOperation::RuntimePreflightV1 => Some(Self::Preflight),
            AgentOperation::ArtifactDistributionV1 => Some(Self::Distribution),
            AgentOperation::RecipeBuildV1 => Some(Self::Build),
            AgentOperation::RecipeBuildCleanupV1 => Some(Self::BuildCleanup),
            AgentOperation::RecipeInstall => Some(Self::Install),
            AgentOperation::RecipeReconcile => Some(Self::Reconcile),
            AgentOperation::RecipeUninstall => Some(Self::Uninstall),
            AgentOperation::RecipeStart
            | AgentOperation::RecipeStop
            | AgentOperation::RecipeJobRunV1
            | AgentOperation::AgentUpgradeV1 => None,
        }
    }
}

pub struct PendingObservation {
    pub cause: WaitReason,
    pub reason: String,
    pub owner: AgentOperation,
    pub dependency: uuid::Uuid,
    pub next_check: DateTime<Utc>,
    pub deadline: DateTime<FixedOffset>,
}
pub enum ContentObservation {
    Confirmed(ExecutionResult),
    Progressing(PendingObservation),
    Blocked(PendingObservation),
    Unknown(PendingObservation),
}
impl PendingObservation {
    fn new(claim: &AgentClaim, cause: WaitReason, reason: impl Into<String>) -> Self {
        Self {
            cause,
            reason: reason.into(),
            owner: claim.operation,
            dependency: claim.fence,
            next_check: Utc::now() + chrono::Duration::seconds(15),
            deadline: claim.deadline,
        }
    }
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn observe_content(&self, claim: &AgentClaim) -> Option<ContentObservation> {
        let kind = ContentKind::of(&claim.operation)?;
        let pending = |cause, reason| {
            ContentObservation::Unknown(PendingObservation::new(claim, cause, reason))
        };
        let observed = self.probe_content(claim, kind).await;
        Some(match observed {
            Ok(result) => result,
            Err(error) => pending(WaitReason::ObservationUnavailable, error),
        })
    }

    async fn probe_content(
        &self,
        claim: &AgentClaim,
        kind: ContentKind,
    ) -> Result<ContentObservation, String> {
        use vonk_agent_protocol::generated::AgentClaimPayload as Payload;
        let unknown = |reason| {
            ContentObservation::Unknown(PendingObservation::new(
                claim,
                WaitReason::RuntimeEffectUnconfirmed,
                reason,
            ))
        };
        let blocked = |reason| {
            ContentObservation::Blocked(PendingObservation::new(
                claim,
                WaitReason::CleanupUnconfirmed,
                reason,
            ))
        };
        match kind {
            ContentKind::Install => {
                let Payload::RecipeInstallPayload(request) = &claim.payload else {
                    return Err("claim payload does not match its content observer".into());
                };
                let id = request.installation_id.to_string();
                let installed = self.runtime.load_spec(&id).map_err(|e| e.to_string())?;
                if !same_installed_workload(&installed, &request.compiled_execution_plan) {
                    return Err("installed content differs from the accepted plan".into());
                }
                self.runtime
                    .observe_installation(&id)
                    .map_err(|e| e.to_string())?;
                let bytes = self
                    .runtime
                    .installed_bytes(&id)
                    .map_err(|e| e.to_string())?;
                Ok(ContentObservation::Confirmed(recipe_install_success(bytes)))
            }
            ContentKind::Reconcile => {
                let Payload::RecipeReconcilePayload(request) = &claim.payload else {
                    return Err("claim payload does not match its content observer".into());
                };
                let identity = RecipeReconciliationIdentity::from(request);
                self.observe_removal(claim, &identity).await.map(|done| {
                    if done {
                        ContentObservation::Confirmed(ExecutionResult::done(
                            RecipeReconcileResult::default(),
                        ))
                    } else {
                        unknown("exact installation removal checkpoint is incomplete".to_owned())
                    }
                })
            }
            ContentKind::Uninstall => {
                let Payload::RecipeUninstallPayload(request) = &claim.payload else {
                    return Err("claim payload does not match its content observer".into());
                };
                let identity = RecipeReconciliationIdentity {
                    installation_id: request.installation_id,
                    plan_digest: request.plan_digest.clone(),
                };
                self.observe_removal(claim, &identity).await.map(|done| {
                    if done {
                        ContentObservation::Confirmed(ExecutionResult::done(
                            RecipeUninstallResult::default(),
                        ))
                    } else {
                        unknown("exact uninstallation checkpoint is incomplete".to_owned())
                    }
                })
            }
            ContentKind::BuildCleanup => {
                let Payload::RecipeBuildCleanupRequest(request) = &claim.payload else {
                    return Err("claim payload does not match its content observer".into());
                };
                let done = crate::recipe_builder::observe_build_cleanup(
                    self.runtime.runner,
                    self.runtime.data_root,
                    request,
                    Instant::now() + Duration::from_secs(15),
                )
                .map_err(|e| e.to_string())?;
                Ok(if done {
                    ContentObservation::Confirmed(ExecutionResult::done(
                        vonk_agent_protocol::RecipeBuildCleanupEvidence {},
                    ))
                } else {
                    blocked(format!(
                        "build {} still owns units or private staging",
                        request.build_id
                    ))
                })
            }
            ContentKind::Build => {
                let Payload::RecipeBuildRequest(request) = &claim.payload else {
                    return Err("claim payload does not match its content observer".into());
                };
                let builder = RecipeBuilder {
                    runner: self.runtime.runner,
                    data_root: self.runtime.data_root,
                    runtime_root: self.runtime_root,
                    egress_binary: Path::new("/usr/lib/vonk-forge/vonk-build-egress"),
                };
                let Some(evidence) = builder.retained_export(
                    request,
                    request.build_id,
                    Instant::now() + Duration::from_secs(15),
                    &|| false,
                ) else {
                    let cleanup = vonk_agent_protocol::RecipeBuildCleanupRequest {
                        build_id: request.build_id,
                        operation_id: request.build_id,
                    };
                    let running = crate::recipe_builder::observe_build_running(
                        self.runtime.runner,
                        &cleanup,
                        Instant::now() + Duration::from_secs(15),
                    )
                    .map_err(|e| e.to_string())?;
                    return Ok(if running {
                        ContentObservation::Progressing(PendingObservation::new(
                            claim,
                            WaitReason::RuntimeEffectUnconfirmed,
                            format!("build {} still has a live owned unit", request.build_id),
                        ))
                    } else {
                        unknown(format!(
                            "build {} has no retained export or live owned unit; completion remains unknown",
                            request.build_id
                        ))
                    });
                };
                let complete = self
                    .client
                    .observe_recipe_image(request.build_id, &evidence)
                    .await
                    .map_err(|e| e.to_string())?;
                Ok(if complete {
                    ContentObservation::Confirmed(ExecutionResult::done(evidence))
                } else {
                    blocked(format!(
                        "Controller upload for build {} is incomplete",
                        request.build_id
                    ))
                })
            }
            ContentKind::Preflight => {
                let Payload::RuntimePreflightRequest(_request) = &claim.payload else {
                    return Err("claim payload does not match its content observer".into());
                };
                let fingerprint = crate::runtime_preflight::host_fingerprint(
                    self.runtime.runner,
                    env!("VONK_AGENT_BUILD_DIGEST"),
                    self.runtime.data_root,
                    self.runtime_root,
                )
                .map_err(|e| e.to_string())?;
                let result = crate::runtime_preflight::observe_completed(
                    self.runtime.data_root,
                    claim.fence,
                    &fingerprint,
                )
                .map_err(|e| e.to_string())?;
                Ok(match result {
                    Some(result) => ContentObservation::Confirmed(ExecutionResult::done(result)),
                    None => unknown(
                        "current host fingerprint has no retained signed preflight result"
                            .to_owned(),
                    ),
                })
            }
            ContentKind::Distribution => {
                let Payload::ArtifactDistributionPayload(request) = &claim.payload else {
                    return Err("claim payload does not match its content observer".into());
                };
                let assignment = self
                    .client
                    .distribution_manifest(&request.plan_digest)
                    .await
                    .map_err(|e| e.to_string())?;
                let mut paths = Vec::new();
                let mut digests = Vec::new();
                let mut bytes = 0;
                for object in &assignment.objects {
                    let path = self
                        .runtime
                        .data_root
                        .join("distribution/models")
                        .join(&object.sha256);
                    if !self
                        .client
                        .observe_distribution_object(&path, object.bytes)
                        .await
                        .map_err(|e| e.to_string())?
                    {
                        return Ok(blocked(format!(
                            "managed object {} ({} bytes) is not yet present",
                            object.sha256, object.bytes
                        )));
                    }
                    paths.push(path);
                    digests.push(object.sha256.clone());
                    bytes += object.bytes;
                }
                let reference = format!(
                    "localhost/vonk/compiled-runtime-{}@{}",
                    assignment.oci_image_digest.trim_start_matches("sha256:"),
                    assignment.oci_image_digest
                );
                let request = crate::host_runtime::runtime_request(
                    claim,
                    HostRuntimeAction::ImagePull,
                    vec![
                        "127.0.0.1:1".into(),
                        assignment.oci_image_digest.clone(),
                        assignment.oci_image_config_digest.clone(),
                        reference,
                    ],
                    None,
                    None,
                    None,
                );
                if !self.observe_helper(&request).await? {
                    return Ok(blocked(format!(
                        "runtime image {} is not yet present",
                        assignment.oci_image_digest
                    )));
                }
                Ok(ContentObservation::Confirmed(distribution_success(
                    DistributionDownloadEvidence {
                        model_digests: digests,
                        model_paths: paths,
                        oci_image_digest: assignment.oci_image_digest,
                        oci_image_config_digest: assignment.oci_image_config_digest,
                        downloaded_bytes: bytes,
                    },
                )))
            }
        }
    }

    async fn observe_helper(
        &self,
        request: &vonk_agent_protocol::HostRuntimeRequest,
    ) -> Result<bool, String> {
        let root = self.runtime_root.join("runtime-requests");
        HostRuntimeBoundary {
            client: self.client,
            request_root: &root,
            helper_socket: self.helper_socket,
        }
        .observe_preparation(request)
        .await
        .map_err(|e| e.to_string())
    }
    async fn observe_removal(
        &self,
        claim: &AgentClaim,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<bool, String> {
        if !self
            .runtime
            .observe_reconciliation(identity)
            .map_err(|e| e.to_string())?
        {
            return Ok(false);
        }
        let request = crate::host_runtime::runtime_request(
            claim,
            HostRuntimeAction::InstallationCleanup,
            vec![],
            Some(identity.installation_id),
            Some(identity.clone()),
            None,
        );
        self.observe_helper(&request).await
    }
}

/// Runs before receipt delivery and claims, including an entirely idle fleet.
/// Startup reaches this same pass through run_once; it never dispatches effects.
pub(super) async fn reobserve_content<E: Executor>(
    state: &mut StateStore,
    executor: &E,
) -> Result<(), StateError> {
    for claim in state.observation_claims()? {
        let Some(observation) = executor.observe(&claim).await else {
            continue;
        };
        match observation {
            ContentObservation::Confirmed(result) => {
                state.finish(&claim, result)?;
            }
            ContentObservation::Progressing(pending)
            | ContentObservation::Blocked(pending)
            | ContentObservation::Unknown(pending) => {
                eprintln!(
                    "vonk-agent: observing fence={} owner={} dependency={} cause={} next_check={} deadline={}: {}",
                    claim.fence,
                    pending.owner,
                    pending.dependency,
                    pending.cause,
                    pending.next_check,
                    pending.deadline,
                    pending.reason
                );
                if Utc::now() >= pending.deadline {
                    state.finish(
                        &claim,
                        ExecutionResult::unknown(
                            pending.cause,
                            pending.reason,
                            UnknownEvidence::at(FailureStage::AgentRestart),
                        ),
                    )?;
                }
            }
        }
    }
    Ok(())
}
