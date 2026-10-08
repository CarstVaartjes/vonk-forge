//! Runtime.

use super::*;

impl<R> RecipeExecutor<'_, R> {
    pub(super) async fn report_phase(&self, claim: &AgentClaim, phase: ProgressPhase) {
        self.client.set_progress_phase(claim.fence, phase);
    }
}

impl<R> RecipeExecutor<'_, R> {
    pub(super) async fn execute_host_runtime_outcome(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
    ) -> Result<HostRuntimeOutcome, crate::host_runtime::HostRuntimeError> {
        self.report_phase(
            claim,
            match action {
                HostRuntimeAction::ImagePull => ProgressPhase::Pulling,
                HostRuntimeAction::Start => ProgressPhase::Starting,
                HostRuntimeAction::Stop => ProgressPhase::Stopping,
                _ => ProgressPhase::Verifying,
            },
        )
        .await;
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

impl<R> RecipeExecutor<'_, R> {
    /// Pull a pinned runtime image from the Controller's layered image store.
    ///
    /// The store is served to the local Docker daemon on a loopback port only
    /// for the duration of this pull, through this agent's authenticated
    /// Controller client; Docker then fetches only the layers it lacks.
    pub async fn pull_runtime_image(
        &self,
        claim: &AgentClaim,
        manifest_digest: &str,
        config_digest: &str,
    ) -> Result<(), crate::host_runtime::HostRuntimeError> {
        let store = crate::image_store::LoopbackImageStore::bind().await?;
        let arguments = store.pull_arguments(manifest_digest, config_digest);
        store
            .serve_while(
                self.client,
                self.execute_host_runtime(claim, HostRuntimeAction::ImagePull, arguments),
            )
            .await
    }
}

impl<R> RecipeExecutor<'_, R> {
    pub(super) async fn execute_host_runtime(
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
}

impl<R> RecipeExecutor<'_, R> {
    pub(super) async fn execute_host_runtime_plan_outcome(
        &self,
        claim: &AgentClaim,
        arguments: Vec<String>,
        plan: HostRuntimePlan,
    ) -> Result<HostRuntimeOutcome, crate::host_runtime::HostRuntimeError> {
        let phase = match &plan {
            HostRuntimePlan::Start(_) | HostRuntimePlan::JobRun(_) => ProgressPhase::Starting,
            HostRuntimePlan::Stop(_) => ProgressPhase::Stopping,
        };
        self.report_phase(claim, phase).await;
        let request_root = self.runtime_root.join("runtime-requests");
        HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
        }
        .execute_plan(claim, arguments, plan)
        .await
    }
}

impl<R> RecipeExecutor<'_, R> {
    pub(super) async fn execute_host_runtime_plan(
        &self,
        claim: &AgentClaim,
        arguments: Vec<String>,
        plan: HostRuntimePlan,
    ) -> Result<(), crate::host_runtime::HostRuntimeError> {
        self.execute_host_runtime_plan_outcome(claim, arguments, plan)
            .await
            .and_then(|outcome| {
                if outcome.stop_uncertain {
                    Err(crate::host_runtime::HostRuntimeError::StopUncertain)
                } else {
                    Ok(())
                }
            })
    }
}

impl<R> RecipeExecutor<'_, R> {
    pub(super) async fn cleanup_installation_cache(
        &self,
        claim: &AgentClaim,
        installation_id: uuid::Uuid,
    ) -> Result<(), crate::host_runtime::HostRuntimeError> {
        let request_root = self.runtime_root.join("runtime-requests");
        HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
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
}

impl<R> RecipeExecutor<'_, R> {
    pub(super) async fn reconcile_installation_runtime(
        &self,
        claim: &AgentClaim,
        identity: RecipeReconciliationIdentity,
    ) -> Result<(), crate::host_runtime::HostRuntimeError> {
        let request_root = self.runtime_root.join("runtime-requests");
        HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
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
}
