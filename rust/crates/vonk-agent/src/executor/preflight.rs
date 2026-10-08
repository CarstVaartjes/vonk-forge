//! Preflight.

use super::*;

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_runtime_preflight(
        &self,
        claim: &AgentClaim,
        cancellation: tokio::sync::watch::Receiver<bool>,
        request: vonk_agent_protocol::runtime_preflight::RuntimePreflightRequest,
    ) -> ExecutionResult {
        use crate::runtime_preflight::{PROBE_BINARY, RuntimePreflight, finding, host_fingerprint};
        let started = std::time::Instant::now();
        let fingerprint = match host_fingerprint(
            self.runtime.runner,
            env!("VONK_AGENT_BUILD_DIGEST"),
            self.runtime.data_root,
            self.runtime_root,
        ) {
            Ok(value) => value,
            Err(_) => {
                return failed("runtime preflight host policy fingerprint is unavailable");
            }
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
        let mut result = match probe.run(&request, fingerprint, fabric, &|| *cancellation.borrow())
        {
            Ok(result) => result,
            Err(_) => {
                return failed("runtime preflight could not inspect the agent service environment");
            }
        };
        let outcome = self
            .execute_host_runtime_outcome(claim, HostRuntimeAction::RuntimePreflight, vec![])
            .await;
        use RuntimePreflightFindingCode as Finding;
        let (passed, code) = match outcome {
            Ok(outcome) => match outcome.exit_code {
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
            },
            Err(error) => (false, error.finding_code()),
        };
        result
            .findings
            .retain(|value| value.capability != "signed_helper_run");
        result
            .findings
            .push(finding("signed_helper_run", passed, code));
        if started.elapsed() >= Duration::from_secs(60) || result.validate().is_err() {
            return failed("runtime preflight exceeded the bounded deadline");
        }
        ExecutionResult::done(result)
    }
}
