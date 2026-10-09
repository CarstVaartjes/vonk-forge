//! Dispatch.

use super::*;

#[async_trait(?Send)]
impl<R: ProcessRunner> Executor for RecipeExecutor<'_, R> {
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        if claim.operation == AgentOperation::ArtifactDistributionV1 {
            return self
                .execute_distribution(claim, lease_deadline, cancellation)
                .await;
        }
        let request = match RecipeOperationRequest::parse(claim) {
            Ok(request) => request,
            Err(_) => return failed("recipe operation payload is invalid"),
        };
        match request {
            RecipeOperationRequest::RuntimePreflight(request) => {
                self.execute_runtime_preflight(claim, cancellation, request)
                    .await
            }
            RecipeOperationRequest::BuildCleanup(request) => {
                self.execute_build_cleanup(request).await
            }
            RecipeOperationRequest::Build(request) => {
                self.execute_build(claim, cancellation, request).await
            }
            RecipeOperationRequest::JobRun(request) => {
                self.execute_job_run(claim, lease_deadline, cancellation, request)
                    .await
            }
            RecipeOperationRequest::Install(request) => {
                self.execute_install(claim, lease_deadline, cancellation, request)
                    .await
            }
            RecipeOperationRequest::Reconcile(request) => {
                self.execute_reconcile(claim, cancellation, request).await
            }
            RecipeOperationRequest::Start(request) => {
                self.execute_start(claim, lease_deadline, cancellation, request)
                    .await
            }
            RecipeOperationRequest::Stop(request) => {
                self.execute_stop(claim, cancellation, request).await
            }
            RecipeOperationRequest::Uninstall(request) => {
                self.execute_uninstall(claim, cancellation, request).await
            }
        }
    }
}

#[cfg(test)]
#[async_trait(?Send)]
impl Executor for RejectingExecutor {
    async fn execute(
        &self,
        claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        _cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        let _ = claim;
        failed("operation is not enabled by this agent build")
    }
}

#[async_trait(?Send)]
impl<R: ProcessRunner> Executor for ControlExecutor<'_, R> {
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        if claim.operation == AgentOperation::AgentUpgradeV1 {
            return upgrade_outcome(self.upgrades.execute(claim).await);
        }

        self.recipes
            .execute(claim, lease_deadline, cancellation)
            .await
    }
}

pub(crate) fn upgrade_outcome(
    result: Result<(), crate::agent_upgrade::AgentUpgradeError>,
) -> ExecutionResult {
    match result {
        Ok(()) => ExecutionResult::unknown(
            WaitReason::AgentUpgradeAwaitingIdentity,
            crate::agent_upgrade::UPGRADE_AWAITING_IDENTITY_REASON,
            UnknownEvidence::at(FailureStage::AgentUpgradeInstalled)
                .because("the package is installed; the new agent's identity is not yet confirmed"),
        ),
        Err(error)
            if !error.security_edge()
                && !matches!(error, crate::agent_upgrade::AgentUpgradeError::InvalidClaim) =>
        {
            ExecutionResult::unknown(
                WaitReason::RuntimeEffectUnconfirmed,
                error.to_string(),
                UnknownEvidence::at(FailureStage::Unknown),
            )
        }
        Err(error) => {
            let reason = match error.diagnostic() {
                Some(detail) if !detail.is_empty() => format!("{error}: {detail}"),
                _ => error.to_string(),
            };
            let mut failure = Failure::new(reason)
                .process_logs(error.diagnostic().map(crate::failure_evidence::detail_logs));
            if let Some((code, exit_code)) = error.helper_diagnostics() {
                failure = failure.helper(code, exit_code.and_then(|code| u32::try_from(code).ok()));
            }
            ExecutionResult::Failed(failure)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::executor::test_support::{NoProcess, claim};
    use uuid::Uuid;

    #[tokio::test]
    async fn expired_delivery_releases_the_claim_lane_without_acknowledging_old_custody() {
        use crate::executor::test_support::{FailedExecutor, NODE_ID, RecordingClient};
        use std::sync::{Arc, Mutex};
        let root = tempfile::tempdir().unwrap();
        let mut state = StateStore::open(&root.path().join("state.sqlite"), NODE_ID).unwrap();
        let old = claim();
        state.begin(&old, Utc::now()).unwrap();
        let retained = state
            .finish(&old, super::super::failures::recipe_install_success(0))
            .unwrap();
        state
            .reject_result(
                &retained,
                &ControllerError::from_status(422),
                Utc::now() - chrono::Duration::seconds(901),
            )
            .unwrap();
        let mut fresh = old.clone();
        fresh.fence = uuid::Uuid::new_v4();
        let client = RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(fresh.clone()))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        tokio::time::timeout(
            Duration::from_secs(1),
            run_once(&client, &mut state, &FailedExecutor, None, 0, None),
        )
        .await
        .unwrap()
        .unwrap();
        assert!(
            client
                .results
                .lock()
                .unwrap()
                .iter()
                .all(|result| result.fence != old.fence)
        );
        assert!(
            client
                .results
                .lock()
                .unwrap()
                .iter()
                .any(|result| result.fence == fresh.fence)
        );
        assert!(
            state
                .pending_results()
                .unwrap()
                .iter()
                .any(|(_, result)| result == &retained)
        );
    }

    #[test]
    fn unavailable_package_preparation_allows_a_fresh_upgrade_handoff() {
        use crate::agent_upgrade::AgentUpgradeError;
        use vonk_agent_protocol::generated::{AgentResultResult, HelperErrorCode};

        // Preparation loss is recoverable bookkeeping, not a permanent refusal.
        let unavailable = upgrade_outcome(Err(AgentUpgradeError::HelperRejectedWithCode {
            code: HelperErrorCode::PackagePreparationUnavailable,
            exit_code: None,
            diagnostic: None,
        }))
        .finish_for(&AgentOperation::AgentUpgradeV1);
        assert!(matches!(
            unavailable.result,
            AgentResultResult::OutcomeUnknown(_)
        ));
        let fresh = upgrade_outcome(Ok(())).finish_for(&AgentOperation::AgentUpgradeV1);
        assert!(matches!(fresh.result, AgentResultResult::OutcomeUnknown(_)));

        // The same dispatch still refuses unverified package bytes at ingress.
        let unverified = upgrade_outcome(Err(AgentUpgradeError::HelperRejectedWithCode {
            code: HelperErrorCode::PackageVerificationFailed,
            exit_code: None,
            diagnostic: None,
        }));
        assert!(matches!(unverified, ExecutionResult::Failed(_)));
        assert!(matches!(
            upgrade_outcome(Ok(())),
            ExecutionResult::Unknown(_)
        ));
    }

    #[test]
    fn helper_install_and_response_loss_reach_unknown_wire_then_accept_a_fresh_handoff() {
        use std::io::{Read, Write};
        use std::os::unix::net::UnixListener;
        use vonk_agent_protocol::generated::{
            AgentResultResult, HostHelperResponse, HostHelperResponseStatus,
        };
        use vonk_agent_protocol::{AgentResult, canonical_json, parse_strict};
        let root = tempfile::tempdir().unwrap();
        let socket = root.path().join("helper.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let request_id = uuid::Uuid::new_v4();
        listener.set_nonblocking(true).unwrap();
        let peer = std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(5);
            for valid in [false, true, true] {
                let (mut stream, _) = loop {
                    assert!(Instant::now() < deadline);
                    match listener.accept() {
                        Ok(connection) => break connection,
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            std::thread::sleep(Duration::from_millis(1))
                        }
                        Err(error) => panic!("helper fixture unavailable: {error}"),
                    }
                };
                stream
                    .set_read_timeout(Some(Duration::from_secs(1)))
                    .unwrap();
                let mut prefix = [0; 4];
                stream.read_exact(&mut prefix).unwrap();
                let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
                stream.read_exact(&mut body).unwrap();
                if !valid {
                    continue;
                }
                let response = HostHelperResponse {
                    schema_version: 1,
                    request_id: Some(request_id),
                    installation_intent_nonce: None,
                    status: HostHelperResponseStatus::PackageInstalled,
                    diagnostic: None,
                    process_logs: None,
                    error_code: None,
                    exit_code: None,
                    process_running: None,
                };
                let bytes = canonical_json(&response).unwrap();
                stream
                    .write_all(&(bytes.len() as u32).to_be_bytes())
                    .unwrap();
                stream.write_all(&bytes).unwrap();
            }
        });
        for expected_install in [false, true, true] {
            let observed = crate::agent_upgrade::call_helper_until(
                &socket,
                b"signed request",
                Instant::now() + Duration::from_secs(1),
            )
            .and_then(|response| {
                crate::agent_upgrade::installed_handoff(&response, &request_id.to_string())
            });
            assert_eq!(observed.is_ok(), expected_install);
            let finished = upgrade_outcome(observed).finish_for(&AgentOperation::AgentUpgradeV1);
            let result = AgentResult {
                fence: request_id,
                result: finished.result,
                state: finished.state,
            };
            let consumed: AgentResult = parse_strict(&canonical_json(&result).unwrap()).unwrap();
            consumed
                .validate_for_operation(&AgentOperation::AgentUpgradeV1)
                .unwrap();
            assert!(matches!(
                consumed.result,
                AgentResultResult::OutcomeUnknown(_)
            ));
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        while !peer.is_finished() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(1));
        }
        assert!(peer.is_finished());
        drop(peer);
    }

    #[tokio::test]
    async fn superseded_distribution_yields_without_network_or_host_effects() {
        let data = tempfile::tempdir().unwrap();
        let runtime = tempfile::tempdir().unwrap();
        let client = AgentHttpClient::for_http_test(
            "http://127.0.0.1:1/",
            "spk_0123456789abcdef0123456789abcdef",
        );
        let executor = RecipeExecutor {
            client: &client,
            runtime_root: runtime.path(),
            runtime: OciRuntime {
                runner: &NoProcess,
                data_root: data.path(),
            },
        };
        let mut obsolete = claim();
        obsolete.operation = AgentOperation::ArtifactDistributionV1;
        obsolete.payload =
            vonk_agent_protocol::generated::AgentClaimPayload::ArtifactDistributionPayload(
                vonk_agent_protocol::generated::ArtifactDistributionPayload {
                    plan_digest: "a".repeat(64),
                },
            );
        let (_lease, deadline) = tokio::sync::watch::channel(obsolete.deadline);
        let (cancel, cancellation) = tokio::sync::watch::channel(true);
        let result = tokio::time::timeout(
            Duration::from_secs(1),
            executor.execute(&obsolete, deadline, cancellation),
        )
        .await
        .unwrap();
        // Before any transfer or helper effect, cancellation is confirmed;
        // there is no unknown effect for the Controller to reconcile.
        let ExecutionResult::Failed(failure) = result else {
            panic!("expected confirmed cancellation before distribution effects");
        };
        assert_eq!(failure.code, Some(FailureCode::OperationCancelled));
        assert!(std::fs::read_dir(data.path()).unwrap().next().is_none());
        assert!(std::fs::read_dir(runtime.path()).unwrap().next().is_none());
        // Cancellation ownership is per request; ending an obsolete transfer
        // leaves a new operation free to execute on the normal dispatch path.
        drop(cancel);
        let mut current = obsolete.clone();
        current.fence = Uuid::new_v4();
        let (_lease, deadline) = tokio::sync::watch::channel(current.deadline);
        let (_cancel, cancellation) = tokio::sync::watch::channel(false);
        let result = tokio::time::timeout(
            Duration::from_secs(1),
            executor.execute(&current, deadline, cancellation),
        )
        .await
        .unwrap();
        // The fresh transfer reaches the unavailable Controller, rather than
        // inheriting cancellation or being refused by stale local ownership.
        let ExecutionResult::Failed(failure) = &result else {
            panic!("expected the fresh distribution to reach its network dependency");
        };
        assert_eq!(failure.code, None);
        assert_eq!(failure.stage, Some(FailureStage::ArtifactDistribution));
        assert_eq!(
            failure.failure_kind,
            Some(AgentFailureKind::TemporaryDependency)
        );
        let finished = result.finish(&current);
        AgentResult {
            fence: current.fence,
            result: finished.result,
            state: finished.state,
        }
        .validate_for_operation(&current.operation)
        .unwrap();
    }
}
