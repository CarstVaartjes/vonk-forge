//! Fabric.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    /// Current signed placement owns admission. A stale firewall observation
    /// triggers one bounded reapplication of the kit's existing isolation rules;
    /// unavailable local tooling never vetoes the Controller's decision.
    pub(super) fn require_authorised_published_endpoint(
        &self,
        run: &ValidatedDockerRun,
    ) -> Result<(), OperationError> {
        let Some(port) = run.published_endpoint_port else {
            return Ok(());
        };
        let arguments = [
            "--config".to_owned(),
            DOCKER_FIREWALL_CONFIG.to_owned(),
            "check-endpoint-port".to_owned(),
            port.to_string(),
        ];
        if !matches!(
            self.runner.run_with_timeout(Path::new(DOCKER_FIREWALL), &arguments, Duration::from_secs(10)),
            Ok(output) if output.success
        ) {
            if !self.reconcile_firewall(Duration::from_secs(10)) {
                eprintln!(
                    "vonk-agent-helper: run {} endpoint firewall reconciliation unavailable; using signed placement",
                    run.run_id
                );
            }
            // Re-observe without turning generated configuration into authority.
            let _ = self.runner.run_with_timeout(
                Path::new(DOCKER_FIREWALL),
                &arguments,
                Duration::from_secs(10),
            );
        }
        Ok(())
    }

    fn reconcile_firewall(&self, timeout: Duration) -> bool {
        matches!(
            self.runner.run_with_timeout(
                Path::new(DOCKER_FIREWALL),
                &[
                    "--config".to_owned(),
                    DOCKER_FIREWALL_CONFIG.to_owned(),
                    "apply".to_owned(),
                ],
                timeout,
            ),
            Ok(output) if output.success
        )
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn bind_native_fabric(
        &self,
        run: &mut ValidatedDockerRun,
        sysfs_root: &Path,
    ) -> Result<(), OperationError> {
        self.bind_native_fabric_inner(run, sysfs_root, true)
    }

    pub(super) fn observe_native_fabric(
        &self,
        run: &mut ValidatedDockerRun,
        sysfs_root: &Path,
    ) -> Result<(), OperationError> {
        self.bind_native_fabric_inner(run, sysfs_root, false)
    }

    fn bind_native_fabric_inner(
        &self,
        run: &mut ValidatedDockerRun,
        sysfs_root: &Path,
        reconcile: bool,
    ) -> Result<(), OperationError> {
        let Some(fabric) = &run.native_fabric else {
            return Ok(());
        };
        let host_endpoint = run
            .host_endpoint_port
            .map_or_else(|| "none".to_owned(), |port| port.to_string());
        let arguments = [
            "--config".to_owned(),
            DOCKER_FIREWALL_CONFIG.to_owned(),
            "check-fabric-run".to_owned(),
            fabric.local.to_string(),
            fabric.master.to_string(),
            fabric.port.to_string(),
            host_endpoint,
        ];
        let deadline = Instant::now() + Duration::from_secs(30);
        let mut observed = None;
        // Observation has no host effect and requires no firewall mutation.
        let mut isolated = !reconcile;
        for attempt in 0..3 {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
            let output = self.runner.run_with_timeout(
                Path::new(DOCKER_FIREWALL),
                &arguments,
                Duration::from_secs(5).min(remaining),
            );
            isolated |= matches!(&output, Ok(output) if output.success);
            observed = match output {
                Ok(output) if output.success => std::str::from_utf8(&output.stdout)
                    .ok()
                    .map(str::trim)
                    .filter(|interface| crate::runtime_fabric::valid_interface(interface))
                    .and_then(|interface| {
                        crate::runtime_fabric::resolve(sysfs_root, interface, fabric.local).ok()
                    }),
                _ => None,
            };
            if observed.is_none() {
                // Reapply only the existing kit envelope. Admission uses the
                // signed plan; the actual network sandbox still must exist.
                if !isolated {
                    isolated = self.reconcile_firewall(
                        Duration::from_secs(5)
                            .min(deadline.saturating_duration_since(Instant::now())),
                    );
                }
                if isolated {
                    observed =
                        crate::runtime_fabric::resolve_address(sysfs_root, fabric.local).ok();
                }
            }
            if observed.is_some() {
                break;
            }
            thread::sleep(
                Duration::from_millis(50 * (attempt + 1))
                    .min(deadline.saturating_duration_since(Instant::now())),
            );
        }
        // Kernel observation is unknown after this bounded pass. The owning
        // operation re-observes through a fresh grant; no launch or hold remains.
        let environment = observed.ok_or(OperationError::CommandFailed)?;
        let run_id = run.run_id.clone();
        for note in &environment.notes {
            eprintln!("vonk-agent-helper: run {run_id} fabric rail: {note}");
        }
        if environment.rails() > 1 {
            eprintln!(
                "vonk-agent-helper: run {run_id} fabric rails: NCCL_IB_HCA={}",
                environment.launch_hca
            );
            run.launch_hca = Some((
                environment.identity_hca().to_owned(),
                format!("NCCL_IB_HCA={}", environment.launch_hca),
            ));
        }
        let arguments = environment
            .environment
            .into_iter()
            .flat_map(|value| ["--env".to_owned(), value])
            .collect::<Vec<_>>();
        let added = arguments.len();
        run.arguments
            .splice(run.image_index..run.image_index, arguments);
        run.image_index += added;
        Ok(())
    }
}

#[cfg(test)]
mod tests;
