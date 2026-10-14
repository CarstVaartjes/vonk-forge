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

/// A pull ends when Docker read no byte through the store for this long. The
/// last layer still extracts after its last byte, so the window is generous;
/// a pull that keeps reading has no total.
const PULL_INACTIVITY: Duration = Duration::from_secs(610);

/// Report each byte Docker reads through the store as the pull's progress:
/// to its inactivity window, and to the order's heartbeat. The helper's pull
/// counts as activity (items), never as the order's artifact bytes, which the
/// Controller reconciles against the distribution's own byte evidence.
pub(super) fn pull_progress<'a>(
    client: &'a crate::client::AgentHttpClient,
    fence: uuid::Uuid,
    pulled: &'a Inactivity,
) -> impl Fn(u64) + Sync + 'a {
    move |bytes| {
        pulled.observe_bytes(bytes);
        client.note_process_activity(fence, bytes);
    }
}

/// A failed pull keeps why the store could not serve Docker, which only saw a
/// 502 or a cut body: a denied upstream read is the pull's own refusal, and
/// any other cause leads the helper's diagnostic.
pub(super) fn with_store_failure(
    error: crate::host_runtime::HostRuntimeError,
    failure: Option<crate::image_store::StoreFailure>,
) -> crate::host_runtime::HostRuntimeError {
    use crate::host_runtime::HostRuntimeError;
    use crate::image_store::StoreFailure;
    match (error, failure) {
        (
            _,
            Some(StoreFailure::Upstream {
                error: upstream, ..
            }),
        ) if upstream.fatal() => HostRuntimeError::Controller(upstream),
        (
            HostRuntimeError::HelperRejected {
                code,
                diagnostic,
                process_logs,
            },
            Some(failure),
        ) => HostRuntimeError::HelperRejected {
            code,
            diagnostic: Some(match diagnostic {
                Some(helper) => format!("{failure}; {helper}"),
                None => failure.to_string(),
            }),
            process_logs,
        },
        (error, Some(failure)) => {
            eprintln!("vonk-agent: runtime image pull failed ({error}) after {failure}");
            error
        }
        (error, None) => error,
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
        self.report_phase(claim, ProgressPhase::Pulling).await;
        let pulled = Inactivity::new(PULL_INACTIVITY);
        let progress = pull_progress(self.client, claim.fence, &pulled);
        let request_root = self.runtime_root.join("runtime-requests");
        let boundary = HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
        };
        let pull = boundary.pull_image(claim, arguments, &pulled);
        let (outcome, failure) = store.serve_while(self.client, &progress, pull).await;
        let outcome = outcome.map_err(|error| with_store_failure(error, failure))?;
        if outcome.stop_uncertain {
            return Err(crate::host_runtime::HostRuntimeError::StopUncertain);
        }
        Ok(())
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
