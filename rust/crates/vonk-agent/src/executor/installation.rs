//! Installation.

use super::*;

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn prepare_installation(
        &self,
        claim: &AgentClaim,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        lease_deadline: &tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: &tokio::sync::watch::Receiver<bool>,
    ) -> Result<(), ExecutionResult> {
        let plan_digest = match &claim.payload {
            vonk_agent_protocol::generated::AgentClaimPayload::RecipeInstallPayload(request) => {
                &request.plan_digest
            }
            vonk_agent_protocol::generated::AgentClaimPayload::RecipeStartPayload(request) => {
                &request.plan_digest
            }
            vonk_agent_protocol::generated::AgentClaimPayload::RecipeJobRunRequest(request) => {
                &request.plan_digest
            }
            _ => return Err(failed("installation preparation request is invalid")),
        };
        let bytes = spec
            .artifacts
            .iter()
            .map(|artifact| artifact.size_bytes)
            .sum::<u64>();
        let transfer_budget = Duration::from_secs(75 + bytes.div_ceil(1024 * 1024));
        let assets = run_with_authority(
            self.client.download_distribution_with_progress(
                plan_digest,
                &self.runtime.data_root.join("distribution"),
                |item| {
                    self.client.set_progress_phase(claim.fence, item.phase);
                    self.client.set_progress_bytes(
                        claim.fence,
                        item.bytes,
                        item.total_bytes.unwrap_or(bytes),
                    );
                },
            ),
            lease_deadline.clone(),
            cancellation.clone(),
            transfer_budget,
        )
        .await;
        let evidence = match assets {
            Some(Ok(evidence)) => evidence,
            Some(Err(error)) => return Err(distribution_failure_result(&error)),
            None if *cancellation.borrow() => {
                return Err(cancelled("controller cancelled preparation"));
            }
            None => return Err(temporary_runtime_observation_failure()),
        };
        if evidence.oci_image_digest != spec.runtime_image.image_digest {
            // The authenticated delivery assignment contradicts the accepted
            // exact image identity; no image or model projection is published.
            return Err(failed(
                "delivery assignment does not bind the accepted image",
            ));
        }
        let pulled = run_with_authority(
            self.pull_runtime_image(
                claim,
                &evidence.oci_image_digest,
                &evidence.oci_image_config_digest,
            ),
            lease_deadline.clone(),
            cancellation.clone(),
            Duration::from_secs(3 * 60 * 60),
        )
        .await;
        match pulled {
            Some(Ok(())) => {}
            Some(Err(error)) if temporary_observation_error(&error) => {
                return Err(temporary_runtime_observation_failure());
            }
            Some(Err(error)) => {
                return Err(runtime_failure(
                    "runtime image preparation was denied",
                    &error,
                ));
            }
            None if *cancellation.borrow() => {
                return Err(cancelled("controller cancelled image preparation"));
            }
            None => return Err(temporary_runtime_observation_failure()),
        }

        let data_root = self.runtime.data_root.to_path_buf();
        let copy_cancellation = cancellation.clone();
        let copy_lease = lease_deadline.clone();
        let progress_client = self.client.clone();
        let fence = claim.fence;
        let spec = spec.clone();
        let installation_id = installation_id.to_owned();
        // Bound storage work by authorized bytes at a 1 MiB/s floor plus
        // setup time. A renewable lease cannot extend this work deadline.
        let materialized_bytes = {
            let mut paths = std::collections::BTreeSet::new();
            spec.artifacts
                .iter()
                .filter(|artifact| paths.insert((&artifact.selection_id, &artifact.path)))
                .map(|artifact| artifact.size_bytes)
                .sum::<u64>()
        };
        let deadline =
            Instant::now() + Duration::from_secs(75 + materialized_bytes.div_ceil(1024 * 1024));
        tokio::task::spawn_blocking(move || {
            let runtime = OciRuntime {
                runner: &crate::process::SystemProcessRunner,
                data_root: &data_root,
            };
            runtime.install_with_space_check_controlled(
                &spec,
                &installation_id,
                &spec.identity.recipe_revision_sha256,
                bytes,
                &mut |done, total| progress_client.set_progress_bytes(fence, done, total),
                &|| {
                    *copy_cancellation.borrow()
                        || remaining_lease(*copy_lease.borrow()).is_zero()
                        || Instant::now() >= deadline
                },
            )
        })
        .await
        .map_err(|_| temporary_runtime_observation_failure())?
        .map_err(|_| temporary_runtime_observation_failure())
    }

    pub(super) async fn execute_install(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
        request: vonk_agent_protocol::RecipeInstallRequest,
    ) -> ExecutionResult {
        self.report_phase(claim, ProgressPhase::Installing).await;
        if *cancellation.borrow() {
            return cancelled("controller cancelled before installation began");
        }
        let spec = request.compiled_execution_plan.clone();
        if spec.validate().is_err() {
            return failed("compiled execution plan is invalid");
        }
        if *cancellation.borrow() {
            return cancelled("controller cancelled before model installation began");
        }
        // Request-led delivery and local materialization report their own
        // phases and measured bytes through the ordinary heartbeat owner.
        self.report_phase(claim, ProgressPhase::Copying).await;
        let installed = self
            .prepare_installation(
                claim,
                &spec,
                &request.installation_id.to_string(),
                &lease_deadline,
                &cancellation,
            )
            .await;
        if *cancellation.borrow() {
            return cancelled("controller cancelled during model preparation");
        }
        match installed {
            Ok(()) => {}
            Err(result) => return result,
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
            Err(_) => return temporary_runtime_observation_failure(),
        };
        if *cancellation.borrow() {
            return cancelled("controller cancellation observed after installation settled");
        }
        recipe_install_success(installed_bytes)
    }
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_reconcile(
        &self,
        claim: &AgentClaim,
        cancellation: tokio::sync::watch::Receiver<bool>,
        request: vonk_agent_protocol::RecipeReconcileRequest,
    ) -> ExecutionResult {
        self.report_phase(claim, ProgressPhase::ReconcilingInstallation)
            .await;
        if *cancellation.borrow() {
            return cancelled("controller cancelled before installation reconciliation");
        }
        let identity = RecipeReconciliationIdentity::from(&request);
        let prepared = match self.runtime.prepare_reconciliation(&identity) {
            Ok(progress) => progress,
            Err(OciError::ReconciliationBusy) => {
                return temporary_reconciliation_failure(
                    FailureStage::InstallationReconciliationLock,
                    FailureCode::InstallationReconciliationBusy,
                    FailureCode::InstallationReconciliationBusy.to_string(),
                );
            }
            Err(error) if retryable_reconciliation_storage_error(&error) => {
                return temporary_reconciliation_failure(
                    FailureStage::InstallationCheckpointStorage,
                    FailureCode::RecipeReconciliationDependencyUnavailable,
                    "installation_storage_temporarily_unavailable",
                );
            }
            Err(error) => {
                return temporary_reconciliation_failure(
                    FailureStage::InstallationValidation,
                    FailureCode::RecipeReconciliationDependencyUnavailable,
                    error.safe_category(),
                );
            }
        };
        let _ = prepared;
        // The local checkpoint resumes removal after an agent restart;
        // each attempt still obtains a fresh Controller-signed helper
        // grant, and the helper refuses while managed containers of
        // this installation remain.
        if *cancellation.borrow() {
            return cancelled("controller cancelled after reconciliation checkpoint preparation");
        }
        if let Err(error) = self
            .reconcile_installation_runtime(claim, identity.clone())
            .await
        {
            if matches!(
                error,
                crate::host_runtime::HostRuntimeError::HelperRejected { ref code, .. }
                    if *code == HelperErrorCode::InstallationReconciliationBusy
            ) {
                return temporary_reconciliation_failure(
                    FailureStage::HelperRuntimeReconciliationLock,
                    FailureCode::InstallationReconciliationBusy,
                    FailureCode::InstallationReconciliationBusy.to_string(),
                );
            }
            if temporary_observation_error(&error) {
                return temporary_reconciliation_failure(
                    FailureStage::HelperRuntimeReconciliation,
                    FailureCode::RecipeReconciliationDependencyUnavailable,
                    error.preflight_code(),
                );
            }
            return failed_stage_owned(
                "managed runtime effects could not be reconciled for installation removal",
                FailureStage::HelperRuntimeReconciliation,
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
                    FailureStage::InstallationReconciliationLock,
                    FailureCode::InstallationReconciliationBusy,
                    FailureCode::InstallationReconciliationBusy.to_string(),
                );
            }
            Err(error) if retryable_reconciliation_storage_error(&error) => {
                return temporary_reconciliation_failure(
                    FailureStage::InstallationCheckpointStorage,
                    FailureCode::RecipeReconciliationDependencyUnavailable,
                    "installation_storage_temporarily_unavailable",
                );
            }
            Err(error) => {
                return temporary_reconciliation_failure(
                    FailureStage::InstallationRemoval,
                    FailureCode::RecipeReconciliationDependencyUnavailable,
                    error.safe_category(),
                );
            }
        };
        if !completed.complete {
            return temporary_reconciliation_failure(
                FailureStage::InstallationReceipt,
                FailureCode::RecipeReconciliationDependencyUnavailable,
                "installation completion remains unobserved",
            );
        }
        ExecutionResult::done(RecipeReconcileResult::default())
    }
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_uninstall(
        &self,
        claim: &AgentClaim,
        cancellation: tokio::sync::watch::Receiver<bool>,
        request: vonk_agent_protocol::RecipeUninstallRequest,
    ) -> ExecutionResult {
        self.report_phase(claim, ProgressPhase::Uninstalling).await;
        if *cancellation.borrow() {
            return cancelled("controller cancelled before uninstallation began");
        }
        let installation_uuid = request.installation_id;
        let installation_id = installation_uuid.to_string();
        match self.runtime.recipe_digest_if_present(&installation_id) {
            Ok(None) => {
                if *cancellation.borrow() {
                    return cancelled("controller cancellation observed with installation absent");
                }
                return ExecutionResult::done(RecipeUninstallResult::default());
            }
            Ok(Some(recipe_digest)) if recipe_digest == request.recipe_content_sha256 => {}
            Ok(Some(_)) | Err(_) => {
                return failed("installed recipe identity does not match uninstall request");
            }
        }
        let validated = match request.cleanup_model_content_sha256.as_deref() {
            Some(model_content_sha256) => self.runtime.validate_uninstall_with_model_cleanup(
                &installation_id,
                &request.recipe_content_sha256,
                model_content_sha256,
            ),
            None => self
                .runtime
                .validate_uninstall(&installation_id, &request.recipe_content_sha256)
                .map(|()| 0),
        };
        if let Err(error) = validated {
            return failed_stage(
                "installed recipe could not be safely removed",
                FailureStage::InstallationValidation,
                error.safe_category(),
            );
        }
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
                        FailureStage::RuntimeCacheCleanup,
                        error.preflight_code(),
                    );
                }
            }
            Err(error) => {
                return failed_stage(
                    "installed recipe could not be safely removed",
                    FailureStage::RuntimeCacheCleanup,
                    error.safe_category(),
                );
            }
        }
        if *cancellation.borrow() {
            return cancelled("controller cancellation observed after runtime cache cleanup");
        }
        // The Controller authorizes model cleanup only when no other
        // installation of this model is left on the Spark. Name the
        // store objects before the installation's own record is gone.
        let store_objects = request
            .cleanup_model_content_sha256
            .as_deref()
            .and_then(|model_content_sha256| {
                self.runtime
                    .model_store_objects(
                        &installation_id,
                        &request.recipe_content_sha256,
                        model_content_sha256,
                    )
                    .ok()
            })
            .unwrap_or_default();
        if let Err(error) = self
            .runtime
            .finalize_uninstall(&installation_id, &request.recipe_content_sha256)
        {
            return failed_stage(
                "installed recipe could not be safely removed",
                FailureStage::InstallationRemoval,
                error.safe_category(),
            );
        }
        // Freeing space is best effort and never fails the uninstall.
        self.runtime.reclaim_unshared_model_objects(&store_objects);
        if *cancellation.borrow() {
            return cancelled("controller cancellation observed after uninstallation settled");
        }
        ExecutionResult::done(RecipeUninstallResult::default())
    }
}

#[cfg(test)]
mod tests;
