//! Preflight.

use super::*;

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_runtime_preflight(
        &self,
        claim: &AgentClaim,
        cancellation: tokio::sync::watch::Receiver<bool>,
        request: vonk_agent_protocol::runtime_preflight::RuntimePreflightRequest,
    ) -> ExecutionResult {
        use crate::runtime_preflight::{
            PROBE_BINARY, RuntimePreflight, finding, host_fingerprint_until,
        };
        let worker_owner = super::build::BuildWorkerOwner::new();
        let deadline = Instant::now()
            + Duration::from_secs(60.min(u64::from(claim.observation_budget_seconds)));
        let unavailable = || {
            ExecutionResult::unknown(
                WaitReason::ObservationUnavailable,
                "runtime preflight observation is unavailable",
                UnknownEvidence::at(FailureStage::BoundedBuildProcess)
                    .because("bounded preflight re-observation ended"),
            )
        };
        let mut fingerprint = None;
        for attempt in 0..3 {
            if *cancellation.borrow() || Instant::now() >= deadline {
                break;
            }
            fingerprint = if let Some(runner) = self.runtime.runner.worker() {
                let data_root = self.runtime.data_root.to_path_buf();
                let runtime_root = self.runtime_root.to_path_buf();
                let cancellation = cancellation.clone();
                let abandoned = worker_owner.flag();
                tokio::task::spawn_blocking(move || {
                    host_fingerprint_until(
                        runner.as_ref(),
                        env!("VONK_AGENT_BUILD_DIGEST"),
                        &data_root,
                        &runtime_root,
                        deadline,
                        &|| {
                            *cancellation.borrow()
                                || abandoned.load(std::sync::atomic::Ordering::Acquire)
                        },
                    )
                })
                .await
                .ok()
                .and_then(Result::ok)
            } else {
                host_fingerprint_until(
                    self.runtime.runner,
                    env!("VONK_AGENT_BUILD_DIGEST"),
                    self.runtime.data_root,
                    self.runtime_root,
                    deadline,
                    &|| *cancellation.borrow(),
                )
                .ok()
            };
            if fingerprint.is_some() {
                break;
            }
            tokio::time::sleep(
                Duration::from_millis(50 * (attempt + 1))
                    .min(deadline.saturating_duration_since(Instant::now())),
            )
            .await;
        }
        let Some(fingerprint) = fingerprint else {
            return unavailable();
        };
        // Fabric bandwidth is declared inventory, not measured NCCL acceptance.
        // The configured fabric address must actually be bindable on this host.
        let fabric =
            crate::config::AgentConfig::load(Path::new(crate::config::DEFAULT_CONFIG_PATH))
                .ok()
                .and_then(|config| {
                    let address = config.fabric_address?;
                    let speed = config.fabric_bandwidth_mbps?;
                    std::net::TcpListener::bind((address, 0))
                        .ok()
                        .map(|_| ("connected", speed))
                });
        let probe = RuntimePreflight {
            runner: self.runtime.runner,
            data_root: self.runtime.data_root,
            runtime_root: self.runtime_root,
            probe_binary: Path::new(PROBE_BINARY),
        };
        let mut result = None;
        for attempt in 0..3 {
            if *cancellation.borrow() || Instant::now() >= deadline {
                break;
            }
            let observed = if let Some(runner) = self.runtime.runner.worker() {
                let request = request.clone();
                let fingerprint = fingerprint.clone();
                let data_root = self.runtime.data_root.to_path_buf();
                let runtime_root = self.runtime_root.to_path_buf();
                let cancellation = cancellation.clone();
                let abandoned = worker_owner.flag();
                tokio::task::spawn_blocking(move || {
                    RuntimePreflight {
                        runner: runner.as_ref(),
                        data_root: &data_root,
                        runtime_root: &runtime_root,
                        probe_binary: Path::new(PROBE_BINARY),
                    }
                    .run_until(
                        &request,
                        fingerprint,
                        fabric,
                        deadline,
                        &|| {
                            *cancellation.borrow()
                                || abandoned.load(std::sync::atomic::Ordering::Acquire)
                        },
                    )
                })
                .await
                .ok()
                .and_then(Result::ok)
            } else {
                probe
                    .run_until(&request, fingerprint.clone(), fabric, deadline, &|| {
                        *cancellation.borrow()
                    })
                    .ok()
            };
            if let Some(observed) = observed {
                if observed.findings.iter().all(|value| {
                    value.status
                        != vonk_agent_protocol::runtime_preflight::RuntimePreflightStatus::Unknown
                        || value.capability == "signed_helper_run"
                }) {
                    result = Some(observed);
                    break;
                }
            }
            tokio::time::sleep(
                Duration::from_millis(50 * (attempt + 1))
                    .min(deadline.saturating_duration_since(Instant::now())),
            )
            .await;
        }
        let Some(mut result) = result else {
            return unavailable();
        };
        let mut outcome = None;
        for attempt in 0..3 {
            if *cancellation.borrow() || Instant::now() >= deadline {
                break;
            }
            match tokio::time::timeout(
                deadline.saturating_duration_since(Instant::now()),
                self.execute_host_runtime_outcome(
                    claim,
                    HostRuntimeAction::RuntimePreflight,
                    vec![],
                ),
            )
            .await
            {
                Ok(Ok(value))
                    if value
                        .exit_code
                        .is_some_and(|code| matches!(code, 0 | 21..=25 | 30..=32)) =>
                {
                    outcome = Some(value);
                    break;
                }
                Ok(Err(error)) => {
                    let denied = matches!(
                        &error,
                        crate::host_runtime::HostRuntimeError::HelperRejected {
                            code: HelperErrorCode::GrantUnauthorized
                                | HelperErrorCode::GrantInvalid
                                | HelperErrorCode::PeerIdentityInvalid
                                | HelperErrorCode::GrantNodeMismatch,
                            ..
                        }
                    ) || matches!(&error, crate::host_runtime::HostRuntimeError::Controller(ClientError::Controller(value)) if matches!(value.status, 401 | 403))
                        || matches!(
                            &error,
                            crate::host_runtime::HostRuntimeError::Controller(
                                ClientError::Identity | ClientError::Pin
                            )
                        );
                    if denied {
                        return runtime_failure(
                            "authenticated authority denied the preflight",
                            &error,
                        );
                    }
                }
                _ => {}
            }
            tokio::time::sleep(
                Duration::from_millis(50 * (attempt + 1))
                    .min(deadline.saturating_duration_since(Instant::now())),
            )
            .await;
        }
        let Some(outcome) = outcome else {
            return unavailable();
        };
        use RuntimePreflightFindingCode as Finding;
        let (passed, code) = match outcome.exit_code {
            Some(0) => (true, Finding::PreflightFindingAvailable),
            Some(21) => (false, Finding::PreflightFindingHelperProcUnavailable),
            Some(22) => (false, Finding::PreflightFindingHelperCapabilitiesNotZero),
            Some(23) => (
                false,
                Finding::PreflightFindingHelperNoNewPrivilegesUnavailable,
            ),
            Some(24) => (
                false,
                Finding::PreflightFindingHelperMountNamespaceUnavailable,
            ),
            Some(25) => (
                false,
                Finding::PreflightFindingHelperTemporaryDirectoryUnavailable,
            ),
            Some(30) => (false, Finding::PreflightFindingHelperImageImportFailed),
            Some(31) => (false, Finding::PreflightFindingHelperSandboxRunFailed),
            Some(32) => (false, Finding::PreflightFindingHelperProbeCleanupFailed),
            _ => (false, Finding::PreflightFindingHelperProbeInvalidResult),
        };
        result
            .findings
            .retain(|value| value.capability != "signed_helper_run");
        result
            .findings
            .push(finding("signed_helper_run", passed, code));
        if Instant::now() >= deadline || result.validate().is_err() {
            return unavailable();
        }
        if result.findings.iter().any(|value| {
            value.status == vonk_agent_protocol::runtime_preflight::RuntimePreflightStatus::Unknown
        }) {
            return unavailable();
        }
        ExecutionResult::done(result)
    }
}
