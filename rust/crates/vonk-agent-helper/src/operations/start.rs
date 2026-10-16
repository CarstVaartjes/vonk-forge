//! Start.

use super::*;

/// A workload is the only memory consumer the host can reap: the kernel OOM
/// killer takes it before any host process, never the agent or helper
/// (`OOMScoreAdjust=-900`). Driver-side GPU memory on a unified-memory Spark
/// may not count as the workload's RSS, so its badness alone cannot be relied
/// on. Helper-owned and outside the run identity digest, so a run started
/// before this option is still adopted as is.
const WORKLOAD_OOM_SCORE_ADJ: &str = "--oom-score-adj=1000";

/// A workload container gets no memory limit. Spark memory is unified: GPU
/// allocations are driver-held and not charged to the container's memory
/// cgroup, so a limit cannot prevent a host wedge and only lets a host RSS
/// spike kill the workload. Admission against declared peaks and the host
/// memory guard own memory safety. The signed argv still names the limit: it
/// is part of the run identity digest, so a run started before this change is
/// still adopted as is. Only the launch omits it.
fn omit_container_memory_limit(arguments: &mut Vec<String>, image_index: usize) -> usize {
    // Validation proved each flag carries a numeric value before the image.
    let limits: Vec<usize> = arguments[..image_index]
        .iter()
        .enumerate()
        .filter(|(_, argument)| matches!(argument.as_str(), "--memory" | "--memory-swap"))
        .map(|(index, _)| index)
        .collect();
    for index in limits.iter().rev() {
        arguments.drain(*index..*index + 2);
    }
    image_index - 2 * limits.len()
}

/// The exit a job reports when the helper itself ended it -- at its deadline or
/// on the Controller's cancellation (the `timeout(1)` convention). Never a
/// stand-in for an exit that was not observed.
pub(super) const JOB_ENDED_BY_HELPER_EXIT_CODE: i32 = 124;

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_start_authorized(
        &self,
        arguments: &[String],
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
        image_config_id: &str,
        binding: &RuntimeRequestGrantBinding<'_>,
    ) -> Result<RuntimeRequestOutcome, OperationError> {
        let installation_id = identity.installation_id.to_string();
        let started_at = Instant::now();
        let (launch, active_start) = {
            let _installation_guard = self.lock_installation_runtime(&installation_id)?;
            self.prepare_runtime_generation_fence(&identity)?;
            self.accept_installation_intent(
                identity.installation_id,
                binding.installation_intent_nonce,
                binding.installation_intent_ordinal,
                false,
            )?;
            self.refuse_reconciled_runtime(&installation_id)?;
            self.update_runtime_generation_fence(&identity, RuntimeGenerationFenceUse::Start)?;
            let active_start = self.job_cancellation.begin(identity)?;
            let launch = self.runtime_start_locked(
                arguments,
                identity,
                logical_run_id,
                plan_digest,
                image_config_id,
            )?;
            (launch, active_start)
        };
        let outcome = match launch {
            RuntimeStartLaunch::Job { timeout_seconds } => self.runtime_wait_for_job(
                identity,
                logical_run_id,
                plan_digest,
                timeout_seconds,
                started_at,
            ),
            RuntimeStartLaunch::Service => Ok(RuntimeRequestOutcome::default()),
        };
        drop(active_start);
        outcome
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_start_locked(
        &self,
        arguments: &[String],
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
        image_config_id: &str,
    ) -> Result<RuntimeStartLaunch, OperationError> {
        let [
            archive_sha256,
            registry_index_digest,
            platform_manifest_digest,
            image_reference,
            docker @ ..,
        ] = arguments
        else {
            return Err(OperationError::InvalidOperation);
        };
        let mut validated = validate_docker_run_with_archive(
            docker,
            &self.roots,
            self.runtime_request_owner_uid,
            Some(archive_sha256),
            Some(registry_index_digest),
        )?;
        if platform_manifest_digest.strip_prefix("sha256:") != Some(archive_sha256.as_str())
            || registry_index_digest != platform_manifest_digest
            || image_reference != &validated.local_image_reference
            || archive_sha256 != &validated.archive_sha256
            || registry_index_digest != &validated.registry_index_digest
            || platform_manifest_digest != &validated.platform_manifest_digest
            || validated.run_id != identity.runtime_id.to_string()
            || validated.installation_id != identity.installation_id.to_string()
            || !lower_hex(plan_digest, 64)
        {
            return Err(OperationError::InvalidOperation);
        }
        self.bind_native_fabric(&mut validated, Path::new(NATIVE_FABRIC_ROOT))?;
        self.require_authorised_published_endpoint(&validated)?;
        let (inspected, operational_image) = self.inspect_accepted_runtime_image(
            &validated.local_image_reference,
            image_config_id,
            platform_manifest_digest,
        )?;
        if inspected.0 != image_config_id && inspected.0 != *platform_manifest_digest {
            return Err(OperationError::RuntimeImageIdentityInvalid);
        }
        self.project_image_receipt(RuntimeImageReceipt {
            schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
            platform_manifest_digest: platform_manifest_digest.to_owned(),
            image_config_id: inspected.0,
            local_image_reference: validated.local_image_reference.clone(),
        });
        let semantic_digest = hex_sha256(
            &canonical_json(&validated.arguments).map_err(|_| OperationError::InvalidOperation)?,
        );
        let target = format!("vonk-{}", identity.runtime_id);
        let expected_labels = "{{.State.Running}}\t{{index .Config.Labels \"ai.vonkforge.runtime-request-sha256\"}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}\t{{index .Config.Labels \"ai.vonkforge.target-id\"}}\t{{index .Config.Labels \"ai.vonkforge.installation-id\"}}\t{{index .Config.Labels \"ai.vonkforge.run-generation\"}}\t{{index .Config.Labels \"ai.vonkforge.plan-digest\"}}";
        let existing = self.run_docker_with_timeout(
            &[
                "container".to_owned(),
                "inspect".to_owned(),
                "--format".to_owned(),
                expected_labels.to_owned(),
                target.clone(),
            ],
            Duration::from_secs(15),
        )?;
        if existing.success {
            let expected = format!(
                "true\t{semantic_digest}\ttrue\t{logical_run_id}\t{}\t{}\t{}\t{plan_digest}",
                identity.runtime_id, identity.installation_id, identity.run_generation
            );
            let fields = std::str::from_utf8(&existing.stdout)
                .ok()
                .map(str::trim)
                .unwrap_or("");
            if fields != expected {
                return Err(OperationError::InvalidArtifact);
            }
            return if let Some(timeout_seconds) = validated.job_timeout_seconds {
                Ok(RuntimeStartLaunch::Job { timeout_seconds })
            } else if validated.detached {
                Ok(RuntimeStartLaunch::Service)
            } else {
                Err(OperationError::InvalidArtifact)
            };
        }
        if !self.prove_container_absent(&target, &existing)? {
            return Err(CommandFailure::exit(&existing).into());
        }
        if validated.job_timeout_seconds.is_some() {
            self.clear_stray_jobs_for_run(identity, logical_run_id)?;
        }
        self.reset_runtime_tmp_if_requested(&validated.run_id)?;
        self.prepare_runtime_access(&validated)?;
        // The signed wire shape includes the executable once after the image
        // as a validation marker; Docker already receives it via --entrypoint.
        let mut compiled = validated.docker_arguments()?;
        let image_index = omit_container_memory_limit(&mut compiled, validated.image_index);
        compiled[image_index] = operational_image;
        let launch_options = vec![
            WORKLOAD_OOM_SCORE_ADJ.to_owned(),
            "--label".to_owned(),
            format!("ai.vonkforge.runtime-request-sha256={semantic_digest}"),
            "--label".to_owned(),
            "ai.vonkforge.managed=true".to_owned(),
            "--label".to_owned(),
            format!("ai.vonkforge.run-id={logical_run_id}"),
            "--label".to_owned(),
            format!("ai.vonkforge.target-id={}", identity.runtime_id),
            "--label".to_owned(),
            format!("ai.vonkforge.installation-id={}", identity.installation_id),
            "--label".to_owned(),
            format!("ai.vonkforge.run-generation={}", identity.run_generation),
            "--label".to_owned(),
            format!("ai.vonkforge.plan-digest={plan_digest}"),
        ];
        // Options are spliced before the image while `image_index` still
        // names it; a later insert shifts it.
        compiled.splice(image_index..image_index, launch_options);
        if validated.job_timeout_seconds.is_some() && !validated.detached {
            // JobRun retains its canonical attached contract; detached Docker
            // launch is only the helper transport that permits concurrent Stop.
            compiled.insert(1, "--detach".to_owned());
        }
        validate_runtime_invocation(&compiled)?;
        let timeout = if validated.job_timeout_seconds.is_some() {
            Duration::from_secs(30)
        } else {
            Duration::from_secs(60)
        };
        let unread = match self.run_docker_with_timeout(&compiled, timeout) {
            Ok(output) if !output.success => return Err(CommandFailure::exit(&output).into()),
            Ok(output) => {
                let identifier = std::str::from_utf8(&output.stdout).map(str::trim);
                (!identifier.is_ok_and(|id| lower_hex(id, 64)))
                    .then_some(CommandFailure::UnexpectedAnswer)
            }
            Err(OperationError::CommandFailed { cause }) => Some(cause),
            Err(error) => return Err(error),
        };
        if let Some(cause) = unread {
            // The launch's answer was not read (no id, a deadline): what it
            // did is read again by identity. A container it created is this
            // Start's and is adopted; only proven absence or an unreadable
            // host reports the launch's own cause.
            match self.reobserve_by_identity(identity, logical_run_id, plan_digest) {
                Reobserved::Running | Reobserved::Exited { .. } => eprintln!(
                    "vonk-agent-helper: run {logical_run_id} launch answer unread ({cause}); its container exists and is adopted"
                ),
                Reobserved::Absent => return Err(cause.into()),
                Reobserved::Unknown(observation) => {
                    eprintln!(
                        "vonk-agent-helper: run {logical_run_id} launch answer unread ({cause}); re-observation unknown: {observation}"
                    );
                    return Err(cause.into());
                }
            }
        }
        if let Some(timeout_seconds) = validated.job_timeout_seconds {
            Ok(RuntimeStartLaunch::Job { timeout_seconds })
        } else {
            Ok(RuntimeStartLaunch::Service)
        }
    }
}

/// How a job's wait ended.
enum JobEnd {
    /// Docker reported the process's exit status.
    Exited(i32),
    /// Its container was proven gone without an exit being read.
    Vanished,
    /// Its deadline passed while it was still running.
    Deadline,
    /// Its deadline passed without the container being read; the cause.
    Unobserved(CommandFailure),
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_wait_for_job(
        &self,
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
        timeout_seconds: u16,
        started_at: Instant,
    ) -> Result<RuntimeRequestOutcome, OperationError> {
        let deadline = started_at + Duration::from_secs(u64::from(timeout_seconds));
        let target = format!("vonk-{}", identity.runtime_id);
        let mut unread = 0u32;
        let end = loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if !remaining.is_zero()
                && let Some(code) = self
                    .runner
                    .run_with_timeout(
                        Path::new("/usr/bin/docker"),
                        &["wait".to_owned(), target.clone()],
                        remaining,
                    )
                    .ok()
                    .filter(|output| output.success)
                    .as_ref()
                    .and_then(bounded_container_wait_exit_code)
            {
                break JobEnd::Exited(code);
            }
            // An unread wait concludes nothing: the container is read again
            // by identity, and a live or unreadable one is waited for again
            // until its deadline.
            let expired = Instant::now() >= deadline;
            match self.reobserve_by_identity(identity, logical_run_id, plan_digest) {
                Reobserved::Exited { code } => break JobEnd::Exited(code),
                Reobserved::Absent => break JobEnd::Vanished,
                Reobserved::Running if expired => break JobEnd::Deadline,
                Reobserved::Unknown(cause) if expired => break JobEnd::Unobserved(cause),
                Reobserved::Running | Reobserved::Unknown(_) => {
                    unread = unread.saturating_add(1);
                    thread::sleep(
                        Duration::from_millis(50 << unread.min(7))
                            .min(deadline.saturating_duration_since(Instant::now())),
                    );
                }
            }
        };
        // The container is read before cleanup removes it: a job that exits
        // unsuccessfully or runs out of time keeps its own output and exit
        // account instead of only a status code.
        let capture = |this: &Self| {
            let (logs, exit) = this.container_exit_evidence(&target);
            match process_exited(logs, exit) {
                OperationError::RuntimeProcessExited {
                    logs, exit_summary, ..
                } => Some(Box::new(JobEvidence {
                    logs,
                    summary: exit_summary,
                })),
                _ => None,
            }
        };
        let cancelled = self.job_cancellation.was_cancelled(identity)?;
        let ended_by_helper = |evidence| RuntimeRequestOutcome {
            exit_code: Some(JOB_ENDED_BY_HELPER_EXIT_CODE),
            evidence,
            stop_uncertain: false,
        };
        match end {
            JobEnd::Exited(code) => {
                let evidence = if code == 0 || cancelled {
                    None
                } else {
                    capture(self)
                };
                // Kept for a re-observation in case this answer never
                // reaches the agent.
                self.retain_job_exit(
                    identity,
                    logical_run_id,
                    plan_digest,
                    code,
                    evidence.as_deref(),
                );
                // The exit is measured; a removal that fails afterwards keeps
                // it and says the container may remain.
                let stop_uncertain = match self.cleanup_runtime_after_job(
                    identity,
                    logical_run_id,
                    false,
                ) {
                    Ok(()) => false,
                    Err(error) => {
                        eprintln!(
                            "vonk-agent-helper: job {} exited {code}; its container remains: {error}",
                            identity.runtime_id
                        );
                        true
                    }
                };
                Ok(if cancelled {
                    RuntimeRequestOutcome {
                        stop_uncertain,
                        ..ended_by_helper(None)
                    }
                } else {
                    RuntimeRequestOutcome {
                        exit_code: Some(code),
                        evidence,
                        stop_uncertain,
                    }
                })
            }
            JobEnd::Vanished => {
                self.cleanup_runtime_after_job(identity, logical_run_id, !cancelled)?;
                if cancelled {
                    Ok(ended_by_helper(None))
                } else {
                    Err(OperationError::RuntimeJobWaitFailed { evidence: None })
                }
            }
            JobEnd::Deadline => {
                // Its tail so far is read before the stop that ends it.
                let evidence = if cancelled { None } else { capture(self) };
                self.cleanup_runtime_after_job(identity, logical_run_id, true)?;
                Ok(ended_by_helper(evidence))
            }
            JobEnd::Unobserved(cause) => {
                // Never read until its deadline: it is ended, and its exit
                // stays unknown with what could be read of it.
                eprintln!(
                    "vonk-agent-helper: job {} unobserved until its deadline: {cause}",
                    identity.runtime_id
                );
                let evidence = if cancelled { None } else { capture(self) };
                self.cleanup_runtime_after_job(identity, logical_run_id, true)?;
                if cancelled {
                    Ok(ended_by_helper(None))
                } else {
                    Err(OperationError::RuntimeJobWaitFailed { evidence })
                }
            }
        }
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn cleanup_runtime_after_job(
        &self,
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        cancel_pending_start: bool,
    ) -> Result<(), OperationError> {
        let installation_id = identity.installation_id.to_string();
        let _installation_guard = self.lock_installation_runtime(&installation_id)?;
        self.refuse_reconciled_runtime(&installation_id)?;
        self.update_runtime_generation_fence(
            &identity,
            RuntimeGenerationFenceUse::Stop {
                cancel_pending_start,
            },
        )?;
        if cancel_pending_start {
            self.job_cancellation.cancel(identity)?;
        }
        self.runtime_stop_once(identity, logical_run_id, 30)
            .map_err(|_| OperationError::StopUncertain)
    }
}

#[cfg(test)]
mod tests;
