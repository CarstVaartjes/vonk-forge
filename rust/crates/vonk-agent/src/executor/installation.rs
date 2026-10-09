//! Installation.

use super::*;

// Dropping an observer is not proof that a copy stopped. The owned worker
// observes this signal at each bounded copy checkpoint and retains verified
// sources; its installation lock remains owned until the worker settles.
struct CancelCopyOnDrop(tokio::sync::watch::Sender<bool>);

impl Drop for CancelCopyOnDrop {
    fn drop(&mut self) {
        self.0.send_replace(true);
    }
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_install(
        &self,
        claim: &AgentClaim,
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
        let image_arguments = vec![
            spec.runtime_image.oci_layout_sha256.clone(),
            spec.runtime_image.image_digest.clone(),
            spec.runtime_image.image_digest.clone(),
            spec.runtime_image.local_image_reference(),
            spec.security.user.clone(),
            spec.runtime_image.local_image_config_id.clone(),
        ];
        for attempt in 0..3_u32 {
            if *cancellation.borrow() {
                return cancelled("controller cancelled during image observation");
            }
            match self
                .execute_host_runtime(
                    claim,
                    HostRuntimeAction::ImageInspect,
                    image_arguments.clone(),
                )
                .await
            {
                Ok(()) => break,
                Err(error) if temporary_observation_error(&error) && attempt < 2 => {
                    tokio::time::sleep(Duration::from_millis(100 * (attempt + 1) as u64)).await;
                }
                Err(error) => {
                    return runtime_failure(
                        "accepted container image observation did not complete",
                        &error,
                    );
                }
            }
        }
        if *cancellation.borrow() {
            return cancelled("controller cancelled before model installation began");
        }
        // The image check above leaves "verifying" as the phase; the
        // long step that follows is a local copy of the model files
        // into this installation, measured in bytes. Say so instead
        // of showing a stale phase with no progress for minutes.
        self.report_phase(claim, ProgressPhase::Copying).await;
        let progress_client = self.client.clone();
        let fence = claim.fence;
        let data_root = self.runtime.data_root.to_owned();
        let copy_spec = spec.clone();
        let installation_id = request.installation_id.to_string();
        let expected_bytes = request.expected_bytes;
        let (copy_stop, copy_cancelled) = tokio::sync::watch::channel(false);
        let _copy_stop = CancelCopyOnDrop(copy_stop);
        let copy_end = std::time::Instant::now()
            + Duration::from_secs(u64::from(claim.observation_budget_seconds));
        let copy_cancellation = cancellation.clone();
        let worker = tokio::task::spawn_blocking(move || {
            let runtime = OciRuntime {
                runner: &crate::process::SystemProcessRunner,
                data_root: &data_root,
            };
            runtime.install_with_space_check_observed(
                &copy_spec,
                &installation_id,
                &copy_spec.identity.recipe_revision_sha256,
                expected_bytes,
                &mut |done, total| progress_client.set_progress_bytes(fence, done, total),
                &|| {
                    *copy_cancellation.borrow()
                        || *copy_cancelled.borrow()
                        || std::time::Instant::now() >= copy_end
                },
            )
        });
        let installed =
            match tokio::time::timeout_at(tokio::time::Instant::from_std(copy_end), worker).await {
                Ok(Ok(installed)) => installed,
                Ok(Err(_)) | Err(_) => {
                    return unconfirmed(
                        WaitReason::RuntimeEffectUnconfirmed,
                        "installation worker completion is unavailable",
                        UnknownEvidence::at(FailureStage::ModelMaterialization),
                    );
                }
            };
        match installed {
            Ok(()) => {}
            Err(OciError::Capacity) => {
                return failed("local disk capacity changed after install admission");
            }
            Err(error) if error.is_cancelled() => {
                return cancelled(
                    "controller cancelled model materialization; verified source bytes retained",
                );
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
                return failed_stage(
                    "managed installation does not match the reconciliation authority",
                    FailureStage::InstallationValidation,
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
                return failed_stage(
                    "reconciled installation cleanup could not be completed",
                    FailureStage::InstallationRemoval,
                    error.safe_category(),
                );
            }
        };
        if !completed.complete {
            return failed_stage(
                "installation cleanup did not reach its durable terminal state",
                FailureStage::InstallationReceipt,
                "receipt-incomplete",
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
