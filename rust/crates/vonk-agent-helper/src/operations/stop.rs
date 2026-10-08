//! Stop.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_stop_authorized(
        &self,
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
        timeout: u16,
        cancel_pending_start: bool,
    ) -> Result<(), OperationError> {
        let installation_id = identity.installation_id.to_string();
        let deadline = Instant::now() + Duration::from_secs(u64::from(timeout) + 15);
        {
            let _installation_guard = self.lock_installation_runtime(&installation_id)?;
            self.refuse_reconciled_runtime(&installation_id)?;
            // Advance the durable generation fence before even inspecting for
            // absence. A delayed old Start therefore cannot recreate a run
            // after this Stop has been acknowledged or the helper restarts.
            self.update_runtime_generation_fence(
                &identity,
                RuntimeGenerationFenceUse::Stop {
                    cancel_pending_start,
                },
            )?;
            if cancel_pending_start {
                self.job_cancellation.cancel(identity)?;
            }
            self.runtime_stop_once(identity, logical_run_id, plan_digest, timeout)?;
        }
        if cancel_pending_start {
            self.job_cancellation
                .wait_for_active_start(identity, deadline)?;
        }
        Ok(())
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_stop_once(
        &self,
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
        timeout: u16,
    ) -> Result<(), OperationError> {
        if !lower_hex(plan_digest, 64)
            || identity.run_generation == 0
            || identity.run_generation > i64::MAX as u64
        {
            return Err(OperationError::InvalidOperation);
        }
        let name = format!("vonk-{}", identity.runtime_id);
        let existing = self.run_docker_with_timeout(
            &[
            "container".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}\t{{index .Config.Labels \"ai.vonkforge.target-id\"}}\t{{index .Config.Labels \"ai.vonkforge.installation-id\"}}\t{{index .Config.Labels \"ai.vonkforge.run-generation\"}}\t{{index .Config.Labels \"ai.vonkforge.plan-digest\"}}".to_owned(),
            name.clone(),
            ],
            Duration::from_secs(15),
        )?;
        if !existing.success {
            // A failed inspect is not proof of absence. Docker access and
            // daemon errors can share its exit code, so require an independent
            // exact empty listing before acknowledging a repeated stop.
            return if self.prove_container_absent(&name, &existing)? {
                Ok(())
            } else {
                Err(OperationError::CommandFailed)
            };
        }
        let expected = format!(
            "true\t{logical_run_id}\t{}\t{}\t{}\t{plan_digest}",
            identity.runtime_id, identity.installation_id, identity.run_generation
        );
        if std::str::from_utf8(&existing.stdout).ok().map(str::trim) != Some(expected.as_str()) {
            return Err(OperationError::InvalidArtifact);
        }
        let stopped = self.run_docker_with_timeout(
            &[
                "stop".to_owned(),
                "--timeout".to_owned(),
                timeout.to_string(),
                name.clone(),
            ],
            Duration::from_secs(u64::from(timeout) + 15),
        )?;
        if !stopped.success {
            return Err(OperationError::CommandFailed);
        }
        let removed =
            self.run_docker_with_timeout(&["rm".to_owned(), name], Duration::from_secs(15))?;
        if !removed.success {
            return Err(OperationError::CommandFailed);
        }
        Ok(())
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn prove_container_absent(
        &self,
        name: &str,
        inspected: &CommandOutput,
    ) -> Result<bool, OperationError> {
        if inspected.exit_code != Some(1) || !inspected.stdout.iter().all(u8::is_ascii_whitespace) {
            return Ok(false);
        }
        let listing = self.run_docker_with_timeout(
            &[
                "container".to_owned(),
                "ls".to_owned(),
                "--all".to_owned(),
                "--quiet".to_owned(),
                "--no-trunc".to_owned(),
                "--filter".to_owned(),
                format!("name=^/{name}$"),
            ],
            Duration::from_secs(15),
        )?;
        Ok(listing.success
            && listing.exit_code == Some(0)
            && listing.stdout.iter().all(u8::is_ascii_whitespace))
    }
}
