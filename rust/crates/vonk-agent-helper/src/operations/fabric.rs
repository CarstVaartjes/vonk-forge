//! Fabric.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    /// A published endpoint port outside the firewall's authorised set is
    /// dropped by the managed chain, so the workload would run and never be
    /// reachable. Refuse the start and say which port and which set.
    pub(super) fn require_authorised_published_endpoint(
        &self,
        run: &ValidatedDockerRun,
    ) -> Result<(), OperationError> {
        let Some(port) = run.published_endpoint_port else {
            return Ok(());
        };
        let request = format!("check-endpoint-port {port}");
        let rejected = |reason: String| {
            eprintln!(
                "vonk-agent-helper: run {} endpoint firewall rejected: {reason}",
                run.run_id
            );
            OperationError::RuntimeEndpointFirewallRejected { reason }
        };
        let output = match self.runner.run_with_timeout(
            Path::new(DOCKER_FIREWALL),
            &[
                "--config".to_owned(),
                DOCKER_FIREWALL_CONFIG.to_owned(),
                "check-endpoint-port".to_owned(),
                port.to_string(),
            ],
            Duration::from_secs(10),
        ) {
            Ok(output) => output,
            Err(error) => {
                eprintln!(
                    "vonk-agent-helper: run {} endpoint firewall check did not run, starting without it: {request}: {error}",
                    run.run_id
                );
                return Ok(());
            }
        };
        if output.success {
            return Ok(());
        }
        let reason = firewall_rejection_reason(&request, &output);
        if output.exit_code == Some(FIREWALL_PORT_REFUSED) {
            return Err(rejected(reason));
        }
        // A firewall that is absent or not applied cannot say which ports it
        // authorises, and a development or acceptance host has none. Only a
        // positive refusal stops the start.
        eprintln!(
            "vonk-agent-helper: run {} endpoint firewall check inconclusive, starting without it: {reason}",
            run.run_id
        );
        Ok(())
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn bind_native_fabric(
        &self,
        run: &mut ValidatedDockerRun,
        sysfs_root: &Path,
    ) -> Result<(), OperationError> {
        let Some(fabric) = &run.native_fabric else {
            return Ok(());
        };
        let host_endpoint = run
            .host_endpoint_port
            .map_or_else(|| "none".to_owned(), |port| port.to_string());
        let request = format!(
            "check-fabric-run local={} master={} rendezvous={} endpoint={host_endpoint}",
            fabric.local, fabric.master, fabric.port
        );
        let rejected = |reason: String| {
            // The journal keeps the same reason the operation reports, so the
            // refused argument is attributable without the Controller.
            eprintln!(
                "vonk-agent-helper: run {} native fabric firewall rejected: {reason}",
                run.run_id
            );
            OperationError::RuntimeFabricFirewallRejected { reason }
        };
        let output = self
            .runner
            .run_with_timeout(
                Path::new(DOCKER_FIREWALL),
                &[
                    "--config".to_owned(),
                    DOCKER_FIREWALL_CONFIG.to_owned(),
                    "check-fabric-run".to_owned(),
                    fabric.local.to_string(),
                    fabric.master.to_string(),
                    fabric.port.to_string(),
                    host_endpoint,
                ],
                Duration::from_secs(10),
            )
            .map_err(|error| rejected(format!("{request}: firewall check did not run: {error}")))?;
        if !output.success {
            return Err(rejected(firewall_rejection_reason(&request, &output)));
        }
        let interface = std::str::from_utf8(&output.stdout)
            .ok()
            .map(str::trim)
            .filter(|value| crate::runtime_fabric::valid_interface(value))
            .ok_or(OperationError::RuntimeFabricUnavailable)?;
        let environment = crate::runtime_fabric::resolve(sysfs_root, interface, fabric.local)
            .map_err(|error| match error {
                crate::runtime_fabric::FabricError::Io(error) => OperationError::Io(error),
                crate::runtime_fabric::FabricError::Unavailable => {
                    OperationError::RuntimeFabricUnavailable
                }
            })?;
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
