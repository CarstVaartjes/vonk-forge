//! One observation dispatcher for preparation and runtime custody on startup
//! and every idle tick, including the signed package handoff.
use super::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ObservationKind {
    Preflight,
    Distribution,
    Build,
    BuildCleanup,
    Install,
    Reconcile,
    Uninstall,
    Start,
    Stop,
    JobRun,
    Upgrade,
}
impl ObservationKind {
    pub fn of(operation: &AgentOperation) -> Self {
        match operation {
            AgentOperation::RuntimePreflightV1 => Self::Preflight,
            AgentOperation::ArtifactDistributionV1 => Self::Distribution,
            AgentOperation::RecipeBuildV1 => Self::Build,
            AgentOperation::RecipeBuildCleanupV1 => Self::BuildCleanup,
            AgentOperation::RecipeInstall => Self::Install,
            AgentOperation::RecipeReconcile => Self::Reconcile,
            AgentOperation::RecipeUninstall => Self::Uninstall,
            AgentOperation::RecipeStart => Self::Start,
            AgentOperation::RecipeStop => Self::Stop,
            AgentOperation::RecipeJobRunV1 => Self::JobRun,
            AgentOperation::AgentUpgradeV1 => Self::Upgrade,
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
pub enum OperationObservation {
    Confirmed(Box<ExecutionResult>),
    Progressing(PendingObservation),
    Blocked(PendingObservation),
    Unknown(PendingObservation),
}
impl OperationObservation {
    pub(super) fn confirmed(result: ExecutionResult) -> Self {
        Self::Confirmed(Box::new(result))
    }
}
impl PendingObservation {
    pub(super) fn new(claim: &AgentClaim, cause: WaitReason, reason: impl Into<String>) -> Self {
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

enum ProbeError {
    Uncertain(String),
    Definite(Box<ExecutionResult>),
}
impl From<String> for ProbeError {
    fn from(cause: String) -> Self {
        Self::Uncertain(cause)
    }
}
impl From<&str> for ProbeError {
    fn from(cause: &str) -> Self {
        Self::Uncertain(cause.to_owned())
    }
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn observe_operation(
        &self,
        claim: &AgentClaim,
        upgrade: Option<super::upgrade_observation::UpgradeObservation<'_>>,
    ) -> OperationObservation {
        let kind = ObservationKind::of(&claim.operation);
        let pending = |cause, reason| {
            OperationObservation::Unknown(PendingObservation::new(claim, cause, reason))
        };
        let observed = self.probe_effect(claim, kind, upgrade).await;
        match observed {
            Ok(result) => result,
            Err(ProbeError::Uncertain(cause)) => pending(WaitReason::ObservationUnavailable, cause),
            Err(ProbeError::Definite(result)) => OperationObservation::confirmed(*result),
        }
    }

    async fn probe_effect(
        &self,
        claim: &AgentClaim,
        kind: ObservationKind,
        upgrade: Option<super::upgrade_observation::UpgradeObservation<'_>>,
    ) -> Result<OperationObservation, ProbeError> {
        use vonk_agent_protocol::generated::AgentClaimPayload as Payload;
        let unknown = |reason| {
            OperationObservation::Unknown(PendingObservation::new(
                claim,
                WaitReason::RuntimeEffectUnconfirmed,
                reason,
            ))
        };
        let blocked = |cause, reason| {
            OperationObservation::Blocked(PendingObservation::new(claim, cause, reason))
        };
        match kind {
            ObservationKind::Upgrade => {
                Ok(super::upgrade_observation::observe_upgrade(claim, upgrade))
            }
            ObservationKind::Start | ObservationKind::Stop | ObservationKind::JobRun => {
                Ok(self.observe_runtime(claim).await)
            }
            ObservationKind::Install => {
                let Payload::RecipeInstallPayload(request) = &claim.payload else {
                    return Err("claim payload does not match its content observer".into());
                };
                let id = request.installation_id.to_string();
                let installed = self.runtime.load_spec(&id).map_err(|e| e.to_string())?;
                if !same_installed_workload(&installed, &request.compiled_execution_plan) {
                    return Ok(OperationObservation::confirmed(ExecutionResult::Failed(
                        Failure::new("installed content differs from the accepted plan")
                            .stage(FailureStage::InstallationValidation),
                    )));
                }
                self.runtime
                    .observe_installation(&id)
                    .map_err(|e| e.to_string())?;
                let bytes = self
                    .runtime
                    .installed_bytes(&id)
                    .map_err(|e| e.to_string())?;
                Ok(OperationObservation::confirmed(recipe_install_success(
                    bytes,
                )))
            }
            ObservationKind::Reconcile => {
                let Payload::RecipeReconcilePayload(request) = &claim.payload else {
                    return Err("claim payload does not match its content observer".into());
                };
                let identity = RecipeReconciliationIdentity::from(request);
                self.observe_removal(claim, &identity).await.map(|done| {
                    if done {
                        OperationObservation::confirmed(ExecutionResult::done(
                            RecipeReconcileResult::default(),
                        ))
                    } else {
                        unknown("exact installation removal checkpoint is incomplete".to_owned())
                    }
                })
            }
            ObservationKind::Uninstall => {
                let Payload::RecipeUninstallPayload(request) = &claim.payload else {
                    return Err("claim payload does not match its content observer".into());
                };
                let identity = RecipeReconciliationIdentity {
                    installation_id: request.installation_id,
                    plan_digest: request.plan_digest.clone(),
                };
                self.observe_removal(claim, &identity).await.map(|done| {
                    if done {
                        OperationObservation::confirmed(ExecutionResult::done(
                            RecipeUninstallResult::default(),
                        ))
                    } else {
                        unknown("exact uninstallation checkpoint is incomplete".to_owned())
                    }
                })
            }
            ObservationKind::BuildCleanup => {
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
                    OperationObservation::confirmed(ExecutionResult::done(
                        vonk_agent_protocol::RecipeBuildCleanupEvidence {},
                    ))
                } else {
                    blocked(
                        WaitReason::CleanupUnconfirmed,
                        format!(
                            "build {} still owns units or private staging",
                            request.build_id
                        ),
                    )
                })
            }
            ObservationKind::Build => {
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
                        OperationObservation::Progressing(PendingObservation::new(
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
                    OperationObservation::confirmed(ExecutionResult::done(evidence))
                } else {
                    blocked(
                        WaitReason::ReceiptMissing,
                        format!(
                            "Controller upload for build {} is incomplete",
                            request.build_id
                        ),
                    )
                })
            }
            ObservationKind::Preflight => {
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
                    Some(result) => OperationObservation::confirmed(ExecutionResult::done(result)),
                    None => unknown(
                        "current host fingerprint has no retained preflight result".to_owned(),
                    ),
                })
            }
            ObservationKind::Distribution => {
                let Payload::ArtifactDistributionPayload(request) = &claim.payload else {
                    return Err("claim payload does not match its content observer".into());
                };
                let assignment = &request.assignment;
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
                        return Ok(blocked(
                            WaitReason::ModelCustodyUnconfirmed,
                            format!(
                                "managed object {} ({} bytes) is not yet present",
                                object.sha256, object.bytes
                            ),
                        ));
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
                    return Ok(blocked(
                        WaitReason::RuntimeEffectUnconfirmed,
                        format!(
                            "runtime image {} is not yet present",
                            assignment.oci_image_digest
                        ),
                    ));
                }
                Ok(OperationObservation::confirmed(distribution_success(
                    DistributionDownloadEvidence {
                        model_digests: digests,
                        model_paths: paths,
                        oci_image_digest: assignment.oci_image_digest.clone(),
                        oci_image_config_digest: assignment.oci_image_config_digest.clone(),
                        downloaded_bytes: bytes,
                    },
                )))
            }
        }
    }

    async fn observe_helper(
        &self,
        request: &vonk_agent_protocol::HostRuntimeRequest,
    ) -> Result<bool, ProbeError> {
        let root = self.runtime_root.join("runtime-requests");
        HostRuntimeBoundary {
            client: self.client,
            request_root: &root,
            helper_socket: self.helper_socket,
        }
        .observe_preparation(request)
        .await
        .map_err(|error| {
            use crate::host_runtime::HostRuntimeError;
            if matches!(
                &error,
                HostRuntimeError::HelperRejected {
                    code: HelperErrorCode::RuntimeImageIdentityInvalid
                        | HelperErrorCode::OperationInvalid
                        | HelperErrorCode::OperationInvalidArtifact
                        | HelperErrorCode::PeerIdentityInvalid
                        | HelperErrorCode::GrantUnauthorized,
                    ..
                }
            ) {
                ProbeError::Definite(Box::new(runtime_failure(
                    "operation observation refused the exact identity",
                    &error,
                )))
            } else {
                ProbeError::Uncertain(error.to_string())
            }
        })
    }
    async fn observe_removal(
        &self,
        claim: &AgentClaim,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<bool, ProbeError> {
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
pub async fn reobserve_operations<E: Executor>(
    state: &mut StateStore,
    executor: &E,
    executing: Option<uuid::Uuid>,
    now: DateTime<Utc>,
) -> Result<(), StateError> {
    for claim in state.observation_claims()? {
        if executing == Some(claim.fence) {
            continue;
        }
        let Some(observation) = executor.observe(&claim).await else {
            continue;
        };
        match observation {
            OperationObservation::Confirmed(result) => {
                state.finish(&claim, *result)?;
            }
            OperationObservation::Progressing(pending)
            | OperationObservation::Blocked(pending)
            | OperationObservation::Unknown(pending) => {
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
                if now >= pending.deadline {
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

/// Keep the same owner probe and spool delivery moving through long polls and
/// unrelated long-running effects. The current executor owns its own fence.
pub(super) async fn observing_during<F: Future, C: LoopClient, E: Executor>(
    work: F,
    client: &C,
    state: Option<StateStore>,
    executor: &E,
    executing: Option<uuid::Uuid>,
) -> F::Output {
    let observations = stream::unfold(state, |state| async move {
        let mut state = state?;
        tokio::time::sleep(HEARTBEAT_INTERVAL).await;
        if let Err(error) = reobserve_operations(&mut state, executor, executing, Utc::now()).await
        {
            eprintln!("vonk-agent: periodic operation observation deferred: {error}");
        }
        if let Err(error) = super::agent_loop::deliver_retained(client, &mut state).await {
            eprintln!("vonk-agent: periodic receipt delivery deferred: {error}");
        }
        Some(((), Some(state)))
    })
    .for_each(|()| futures_util::future::ready(()));
    match futures_util::future::select(std::pin::pin!(work), std::pin::pin!(observations)).await {
        futures_util::future::Either::Left((result, _)) => result,
        futures_util::future::Either::Right(((), work)) => work.await,
    }
}
