//! Authority.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn execute_runtime_request(
        &self,
        action: &ContainerRuntimeAction,
        binding: RuntimeRequestGrantBinding<'_>,
        request_sha256: &str,
    ) -> Result<RuntimeRequestOutcome, OperationError> {
        let request = self.read_runtime_request(request_sha256)?;
        let expected_action = match action {
            ContainerRuntimeAction::RuntimePreflight => HostRuntimeAction::RuntimePreflight,
            ContainerRuntimeAction::ImagePull => HostRuntimeAction::ImagePull,
            ContainerRuntimeAction::ImageInspect => HostRuntimeAction::ImageInspect,
            ContainerRuntimeAction::RunInspect => HostRuntimeAction::RunInspect,
            ContainerRuntimeAction::Start => HostRuntimeAction::Start,
            ContainerRuntimeAction::Stop => HostRuntimeAction::Stop,
            ContainerRuntimeAction::InstallationCleanup => HostRuntimeAction::InstallationCleanup,
        };
        if request.action != expected_action
            || &request.fence != binding.fence
            || request.installation_id.as_ref() != binding.installation_id
            || request.reconciliation_identity.as_ref() != binding.reconciliation_identity
        {
            return Err(OperationError::InvalidOperation);
        }
        let authorized_effect = self.authorize_runtime_effect(&request, binding)?;
        match request.action {
            HostRuntimeAction::RuntimePreflight => {
                let code = crate::runtime_preflight::run(
                    Path::new("/var/lib/vonk-forge"),
                    Path::new(crate::runtime_preflight::PROBE_BINARY),
                    |arguments, timeout| {
                        self.run_docker_with_timeout(arguments, timeout)
                            .map(|output| (output.success, output.stdout))
                            .map_err(|_| std::io::Error::other("preflight runtime unavailable"))
                    },
                )
                .map_err(|_| OperationError::CommandFailed)?;
                Ok(RuntimeRequestOutcome {
                    exit_code: Some(code),
                    evidence: None,
                })
            }
            HostRuntimeAction::ImagePull => {
                self.runtime_image_pull(&request.arguments)
                    .map(|()| RuntimeRequestOutcome {
                        exit_code: None,
                        evidence: None,
                    })
            }
            HostRuntimeAction::ImageInspect => {
                self.runtime_image_inspect(&request.arguments)
                    .map(|()| RuntimeRequestOutcome {
                        exit_code: None,
                        evidence: None,
                    })
            }
            HostRuntimeAction::RunInspect => {
                if !self.runtime_run_inspect(&request.arguments, true)? {
                    return Err(OperationError::InvalidArtifact);
                }
                Ok(RuntimeRequestOutcome {
                    exit_code: None,
                    evidence: None,
                })
            }
            HostRuntimeAction::Start => {
                let Some(AuthorizedRuntimeEffect::Start {
                    identity,
                    logical_run_id,
                    plan_digest,
                }) = authorized_effect
                else {
                    return Err(OperationError::InvalidOperation);
                };
                self.runtime_start_authorized(
                    &request.arguments,
                    identity,
                    logical_run_id,
                    &plan_digest,
                )
                .map(|(exit_code, evidence)| RuntimeRequestOutcome {
                    exit_code,
                    evidence,
                })
            }
            HostRuntimeAction::Stop => {
                let Some(AuthorizedRuntimeEffect::Stop {
                    identity,
                    logical_run_id,
                    plan_digest,
                    stop_timeout_seconds,
                    cancel_pending_start,
                }) = authorized_effect
                else {
                    return Err(OperationError::InvalidOperation);
                };
                self.runtime_stop_authorized(
                    identity,
                    logical_run_id,
                    &plan_digest,
                    stop_timeout_seconds,
                    cancel_pending_start,
                )
                .map(|()| RuntimeRequestOutcome {
                    exit_code: None,
                    evidence: None,
                })
            }
            HostRuntimeAction::InstallationCleanup => {
                let installation_id = request
                    .installation_id
                    .as_ref()
                    .ok_or(OperationError::InvalidOperation)?;
                if let Some(identity) = request.reconciliation_identity.as_ref() {
                    if identity.installation_id != *installation_id {
                        return Err(OperationError::InvalidOperation);
                    }
                    self.runtime_reconcile_installation(identity)?;
                } else {
                    self.runtime_installation_cleanup(&installation_id.to_string())?;
                }
                Ok(RuntimeRequestOutcome {
                    exit_code: None,
                    evidence: None,
                })
            }
        }
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn authorize_runtime_effect(
        &self,
        request: &HostRuntimeRequest,
        grant: RuntimeRequestGrantBinding<'_>,
    ) -> Result<Option<AuthorizedRuntimeEffect>, OperationError> {
        match request.action {
            HostRuntimeAction::Start => {
                if request.installation_id.is_some()
                    || request.reconciliation_identity.is_some()
                    || request.stop_plan.is_some()
                    || request.run_generation.is_none()
                    || (request.start_plan.is_some() == request.job_plan.is_some())
                    || grant.start_plan_sha256.is_none()
                    || grant.stop_plan_sha256.is_some()
                {
                    return Err(OperationError::InvalidOperation);
                }
                let (plan, compiled, logical_run_id, target_id, installation_id, generation) =
                    if let Some(plan) = request.start_plan.as_ref() {
                        validate_runtime_start_plan(plan)?;
                        if plan.compiled_execution_plan.job.is_some() {
                            return Err(OperationError::InvalidOperation);
                        }
                        (
                            canonical_json(plan).map_err(|_| OperationError::InvalidOperation)?,
                            &plan.compiled_execution_plan,
                            plan.run_id,
                            plan.run_id,
                            plan.installation_id,
                            plan.run_generation,
                        )
                    } else {
                        let plan = request
                            .job_plan
                            .as_ref()
                            .ok_or(OperationError::InvalidOperation)?;
                        validate_runtime_job_plan(plan)?;
                        (
                            canonical_json(plan).map_err(|_| OperationError::InvalidOperation)?,
                            &plan.compiled_execution_plan,
                            plan.run_id,
                            plan.job_id,
                            plan.installation_id,
                            plan.run_generation,
                        )
                    };
                let grant_target = grant
                    .runtime_target_id
                    .ok_or(OperationError::InvalidOperation)?;
                if generation == 0
                    || generation > i64::MAX as u64
                    || request.run_generation != Some(generation)
                    || grant.run_generation != Some(generation)
                    || grant.runtime_run_id != Some(&logical_run_id)
                    || grant_target != &target_id
                    || grant.runtime_installation_id != Some(&installation_id)
                    || grant.installation_id.is_some()
                    || grant.reconciliation_identity.is_some()
                    || grant.stop_plan_sha256.is_some()
                    || !grant
                        .start_plan_sha256
                        .is_some_and(|digest| lower_hex(digest, 64) && hex_sha256(&plan) == digest)
                    || !valid_oci_digest(&compiled.runtime_image.image_digest)
                {
                    return Err(OperationError::InvalidOperation);
                }
                let expected_arguments =
                    self.projected_runtime_arguments(compiled, installation_id, target_id)?;
                if request.arguments != expected_arguments {
                    return Err(OperationError::InvalidOperation);
                }
                Ok(Some(AuthorizedRuntimeEffect::Start {
                    identity: RuntimeEffectIdentity {
                        runtime_id: target_id,
                        installation_id,
                        run_generation: generation,
                    },
                    logical_run_id,
                    plan_digest: if let Some(plan) = request.start_plan.as_ref() {
                        plan.plan_digest.clone()
                    } else {
                        request
                            .job_plan
                            .as_ref()
                            .ok_or(OperationError::InvalidOperation)?
                            .plan_digest
                            .clone()
                    },
                }))
            }
            HostRuntimeAction::Stop => {
                let plan = request
                    .stop_plan
                    .as_ref()
                    .ok_or(OperationError::InvalidOperation)?;
                validate_runtime_stop_plan(plan)?;
                if !request.arguments.is_empty()
                    || request.installation_id.is_some()
                    || request.reconciliation_identity.is_some()
                    || request.start_plan.is_some()
                    || request.job_plan.is_some()
                    || request.run_generation != Some(plan.run_generation)
                    || grant.run_generation != Some(plan.run_generation)
                    || grant.runtime_run_id != Some(&plan.run_id)
                    || grant.runtime_target_id != Some(&plan.target_runtime_id)
                    || grant.runtime_installation_id != Some(&plan.installation_id)
                    || grant.installation_id.is_some()
                    || grant.reconciliation_identity.is_some()
                    || grant.start_plan_sha256.is_some()
                    || !grant.stop_plan_sha256.is_some_and(|digest| {
                        lower_hex(digest, 64)
                            && canonical_json(plan)
                                .ok()
                                .is_some_and(|encoded| hex_sha256(&encoded) == digest)
                    })
                {
                    return Err(OperationError::InvalidOperation);
                }
                let stop_timeout_seconds = u16::try_from(plan.stop_timeout_seconds)
                    .ok()
                    .filter(|seconds| (1..=600).contains(seconds))
                    .ok_or(OperationError::InvalidOperation)?;
                Ok(Some(AuthorizedRuntimeEffect::Stop {
                    identity: RuntimeEffectIdentity {
                        runtime_id: plan.target_runtime_id,
                        installation_id: plan.installation_id,
                        run_generation: plan.run_generation,
                    },
                    logical_run_id: plan.run_id,
                    plan_digest: plan.plan_digest.clone(),
                    stop_timeout_seconds,
                    cancel_pending_start: plan.cancel_pending_start,
                }))
            }
            _ => {
                if request.start_plan.is_some()
                    || request.job_plan.is_some()
                    || request.stop_plan.is_some()
                    || request.run_generation.is_some()
                    || grant.start_plan_sha256.is_some()
                    || grant.stop_plan_sha256.is_some()
                    || grant.run_generation.is_some()
                    || grant.runtime_run_id.is_some()
                    || grant.runtime_target_id.is_some()
                    || grant.runtime_installation_id.is_some()
                {
                    return Err(OperationError::InvalidOperation);
                }
                Ok(None)
            }
        }
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn projected_runtime_arguments(
        &self,
        plan: &CompiledExecutionPlan,
        installation_id: uuid::Uuid,
        target_runtime_id: uuid::Uuid,
    ) -> Result<Vec<String>, OperationError> {
        plan.validate()
            .map_err(|_| OperationError::InvalidOperation)?;
        let installation = installation_id.to_string();
        let target = target_runtime_id.to_string();
        let run_root = self.roots.agent_data.join("runs").join(&target);
        let main = start_arguments_for_paths(
            plan,
            &CompiledOciPaths {
                model_root: self
                    .roots
                    .agent_data
                    .join("installations")
                    .join(&installation)
                    .join("models"),
                input_root: plan.job.as_ref().map(|_| run_root.join("inputs")),
                output_root: run_root.join("outputs"),
                cache_root: self
                    .roots
                    .agent_data
                    .join("installations")
                    .join(&installation)
                    .join("runtime-cache"),
                runtime_spec: self
                    .roots
                    .agent_data
                    .join("run-metadata")
                    .join(&target)
                    .join("runtime.json"),
            },
            &target,
        )
        .map_err(|_| OperationError::InvalidOperation)?;
        Ok(runtime_plan_prefix(plan, main))
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn read_runtime_request(
        &self,
        request_sha256: &str,
    ) -> Result<HostRuntimeRequest, OperationError> {
        if !lower_hex(request_sha256, 64) {
            return Err(OperationError::InvalidOperation);
        }
        let path = self
            .roots
            .runtime_requests
            .join(format!("{request_sha256}.json"));
        let metadata = fs::symlink_metadata(&path).map_err(|_| OperationError::UnsafePath)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.len() == 0
            || metadata.len() > MAX_RUNTIME_REQUEST_BYTES
            || metadata.mode() & 0o077 != 0
            || self
                .runtime_request_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(OperationError::UnsafePath);
        }
        let mut file = File::open(&path).map_err(|_| OperationError::UnsafePath)?;
        let before = file.metadata().map_err(|_| OperationError::UnsafePath)?;
        let mut raw = Vec::new();
        Read::by_ref(&mut file)
            .take(MAX_RUNTIME_REQUEST_BYTES + 1)
            .read_to_end(&mut raw)
            .map_err(|_| OperationError::UnsafePath)?;
        let after = file.metadata().map_err(|_| OperationError::UnsafePath)?;
        if raw.len() as u64 > MAX_RUNTIME_REQUEST_BYTES
            || stable_identity(&before) != stable_identity(&after)
            || hex_sha256(&raw) != request_sha256
        {
            return Err(OperationError::UnsafePath);
        }
        let request: HostRuntimeRequest =
            parse_strict(&raw).map_err(|_| OperationError::InvalidOperation)?;
        request
            .validate()
            .map_err(|_| OperationError::InvalidOperation)?;
        if canonical_json(&request).map_err(|_| OperationError::InvalidOperation)? != raw {
            return Err(OperationError::InvalidOperation);
        }
        Ok(request)
    }
}

#[cfg(test)]
mod tests;
