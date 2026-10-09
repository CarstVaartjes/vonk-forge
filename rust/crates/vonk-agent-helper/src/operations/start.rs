//! Start.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_start_authorized(
        &self,
        arguments: &[String],
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
        image_config_id: &str,
        binding: &RuntimeRequestGrantBinding<'_>,
    ) -> Result<(Option<i32>, Option<Box<JobEvidence>>), OperationError> {
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
            RuntimeStartLaunch::TimedOut => Ok((Some(124), None)),
            RuntimeStartLaunch::Service => Ok((None, None)),
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
            return Err(OperationError::CommandFailed);
        }
        self.reset_runtime_tmp_if_requested(&validated.run_id)?;
        self.prepare_runtime_access(&validated)?;
        // The signed wire shape includes the executable once after the image
        // as a validation marker; Docker already receives it via --entrypoint.
        let mut compiled = validated.docker_arguments()?;
        compiled[validated.image_index] = operational_image;
        let labels = vec![
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
        if validated.job_timeout_seconds.is_some() && !validated.detached {
            // JobRun retains its canonical attached contract; detached Docker
            // launch is only the helper transport that permits concurrent Stop.
            compiled.insert(1, "--detach".to_owned());
        }
        compiled.splice(validated.image_index..validated.image_index, labels);
        validate_runtime_invocation(&compiled)?;
        let timeout = if validated.job_timeout_seconds.is_some() {
            Duration::from_secs(30)
        } else {
            Duration::from_secs(60)
        };
        let output = match self.run_docker_with_timeout(&compiled, timeout) {
            Ok(output) => output,
            Err(_) if validated.job_timeout_seconds.is_some() => {
                self.update_runtime_generation_fence(
                    &identity,
                    RuntimeGenerationFenceUse::Stop {
                        cancel_pending_start: true,
                    },
                )?;
                self.job_cancellation.cancel(identity)?;
                self.runtime_stop_once(identity, logical_run_id, plan_digest, 30)
                    .map_err(|_| OperationError::StopUncertain)?;
                return Ok(RuntimeStartLaunch::TimedOut);
            }
            Err(_) => return Err(OperationError::CommandFailed),
        };
        let identifier = std::str::from_utf8(&output.stdout)
            .ok()
            .map(str::trim)
            .unwrap_or("");
        if !output.success || !lower_hex(identifier, 64) {
            return Err(OperationError::CommandFailed);
        }
        if let Some(timeout_seconds) = validated.job_timeout_seconds {
            Ok(RuntimeStartLaunch::Job { timeout_seconds })
        } else {
            Ok(RuntimeStartLaunch::Service)
        }
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_wait_for_job(
        &self,
        identity: RuntimeEffectIdentity,
        logical_run_id: uuid::Uuid,
        plan_digest: &str,
        timeout_seconds: u16,
        started_at: Instant,
    ) -> Result<(Option<i32>, Option<Box<JobEvidence>>), OperationError> {
        let deadline = started_at + Duration::from_secs(u64::from(timeout_seconds));
        let target = format!("vonk-{}", identity.runtime_id);
        let mut waited = Err("runtime job observation unavailable".to_owned());
        for attempt in 0..3 {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
            waited = self.runner.run_with_timeout(
                Path::new("/usr/bin/docker"),
                &["wait".to_owned(), target.clone()],
                remaining,
            );
            if matches!(
                &waited,
                Ok(output) if output.success && bounded_container_wait_exit_code(output).is_some()
            ) {
                break;
            }
            thread::sleep(
                Duration::from_millis(50 * (attempt + 1))
                    .min(deadline.saturating_duration_since(Instant::now())),
            );
        }
        // The container is read before cleanup removes it: a job that exits
        // unsuccessfully or runs out of time keeps its own output and exit
        // account instead of only a status code.
        let capture = |this: &Self| {
            let (logs, exit) =
                this.container_exit_evidence(&format!("vonk-{}", identity.runtime_id));
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
        let measured_exit = waited
            .as_ref()
            .ok()
            .filter(|output| output.success)
            .and_then(|output| bounded_container_wait_exit_code(output));
        match (waited, measured_exit) {
            (Ok(_), Some(exit_code)) => {
                let evidence = if exit_code == 0 { None } else { capture(self) };
                self.cleanup_runtime_after_job(identity, logical_run_id, plan_digest, false)?;
                if self.job_cancellation.was_cancelled(identity)? {
                    Ok((Some(124), None))
                } else {
                    Ok((Some(exit_code), evidence))
                }
            }
            (Ok(_), None) => {
                let cancelled = self.job_cancellation.was_cancelled(identity)?;
                let evidence = if cancelled { None } else { capture(self) };
                self.cleanup_runtime_after_job(identity, logical_run_id, plan_digest, !cancelled)?;
                if cancelled {
                    Ok((Some(124), None))
                } else {
                    Err(OperationError::RuntimeJobWaitFailed { evidence })
                }
            }
            (Err(_), _) => {
                let expired = Instant::now() >= deadline;
                // An unreadable wait remains unknown. Its tail so far is read
                // before the stop that ends it.
                let evidence = if self.job_cancellation.was_cancelled(identity)? {
                    None
                } else {
                    capture(self)
                };
                self.cleanup_runtime_after_job(identity, logical_run_id, plan_digest, true)?;
                if expired {
                    Ok((Some(124), evidence))
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
        plan_digest: &str,
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
        self.runtime_stop_once(identity, logical_run_id, plan_digest, 30)
            .map_err(|_| OperationError::StopUncertain)
    }
}
