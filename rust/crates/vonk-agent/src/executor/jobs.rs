//! Jobs.

use super::*;

impl<'runtime, 'data, R: ProcessRunner> JobScopeCleanup<'runtime, 'data, R> {
    pub(super) fn new(runtime: &'runtime OciRuntime<'data, R>, job_scope: &'runtime str) -> Self {
        Self {
            runtime,
            job_scope,
            active: true,
        }
    }

    pub(super) fn finish(mut self) -> Result<(), crate::oci::OciError> {
        let result = self.runtime.cleanup_job_scope(self.job_scope);
        self.active = result.is_err();
        result
    }

    pub(super) fn retain(mut self) {
        self.active = false;
    }
}

impl<R: ProcessRunner> Drop for JobScopeCleanup<'_, '_, R> {
    fn drop(&mut self) {
        if self.active {
            let _ = self.runtime.cleanup_job_scope(self.job_scope);
        }
    }
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_job_run(
        &self,
        claim: &AgentClaim,
        mut cancellation: tokio::sync::watch::Receiver<bool>,
        request: vonk_agent_protocol::RecipeJobRunRequest,
    ) -> ExecutionResult {
        self.report_phase(claim, ProgressPhase::Preparing).await;
        let started = Instant::now();
        let installation_id = request.installation_id.to_string();
        let job_scope = request.job_id.to_string();
        if self.runtime.recipe_digest(&installation_id).ok().as_deref()
            != Some(
                &request
                    .compiled_execution_plan
                    .identity
                    .recipe_revision_sha256,
            )
            || self.runtime.verify_installation(&installation_id).is_err()
        {
            return failed_job(
                &request,
                1,
                started,
                "installed recipe identity or artifact manifest does not match",
            );
        }
        let spec = match self.runtime.load_spec(&installation_id) {
            Ok(spec) => spec,
            Err(_) => {
                return failed_job(
                    &request,
                    1,
                    started,
                    "installed recipe specification is corrupt",
                );
            }
        };
        let invocation = match prepare_job_invocation(&spec, &request) {
            Ok(plan) => plan,
            Err(_) => return failed_job(&request, 1, started, "job invocation is invalid"),
        };
        // Kit peak is an estimate, never an admission veto. The host
        // guard observes actual wedge precursors independently.
        let preload_diagnostics = self.runtime.report_preload_memory(
            request.placement().reserved_memory_bytes,
            Path::new("/proc/meminfo"),
        );
        if *cancellation.borrow() {
            return cancelled_job(&request, started, "controller cancellation requested");
        }
        if self.runtime.cleanup_job_scope(&job_scope).is_err() {
            return failed_job(&request, 1, started, "prior job scope is unsafe");
        }
        // Keep the scope alive through output collection/upload, then guarantee bounded
        // local cleanup for every success, adapter failure, timeout, and transport error.
        let job_scope_cleanup = JobScopeCleanup::new(&self.runtime, &job_scope);
        for input in &request.inputs {
            let destination = match self.runtime.job_input_destination(&job_scope, &input.name) {
                Ok(destination) => destination,
                Err(_) => {
                    let _ = self.runtime.cleanup_job_scope(&job_scope);
                    return failed_job(&request, 1, started, "job input staging failed");
                }
            };
            let download = run_until_cancelled(
                self.client.download_recipe_job_input(
                    request.job_id,
                    &input.sha256,
                    u64::from(input.size_bytes),
                    &destination,
                ),
                &mut cancellation,
            )
            .await;
            if download.is_none() {
                if job_scope_cleanup.finish().is_err() {
                    return failed_job(
                        &request,
                        JOB_CANCEL_EXIT_CODE,
                        started,
                        "cancelled job scope cleanup failed",
                    );
                }
                return cancelled_job(&request, started, "controller cancellation requested");
            }
            if download.is_some_and(|result| result.is_err()) {
                let _ = self.runtime.cleanup_job_scope(&job_scope);
                return failed_job(&request, 1, started, "authorized job input is unavailable");
            }
        }
        let input_names = request
            .inputs
            .iter()
            .map(|input| input.name.clone())
            .collect::<Vec<_>>();
        let input_manifest = match recipe_job_input_manifest(&request) {
            Ok(bytes) => bytes,
            Err(_) => {
                let _ = self.runtime.cleanup_job_scope(&job_scope);
                return failed_job(&request, 1, started, "job input manifest is invalid");
            }
        };
        if self
            .runtime
            .write_job_input_manifest(
                &job_scope,
                &input_names,
                &input_manifest,
                &request.input_manifest_sha256,
            )
            .is_err()
        {
            let _ = self.runtime.cleanup_job_scope(&job_scope);
            return failed_job(
                &request,
                1,
                started,
                "job input staging is not same-run exact",
            );
        }
        let placement = match job_placement(&invocation) {
            Ok(placement) => placement,
            Err(_) => {
                return failed_job(
                    &request,
                    1,
                    started,
                    "job placement does not match the installed workload",
                );
            }
        };
        let plan = match self.runtime.prepare_job_start(
            &spec,
            &installation_id,
            &job_scope,
            &placement,
            &invocation,
        ) {
            Ok(plan) => plan,
            Err(_) => {
                let _ = self.runtime.cleanup_job_scope(&job_scope);
                return failed_job(
                    &request,
                    1,
                    started,
                    "container runtime could not prepare the job",
                );
            }
        };
        let mut arguments = vec![
            plan.archive_sha256,
            plan.registry_index_digest,
            plan.platform_manifest_digest,
            plan.image_reference,
        ];
        arguments.extend(plan.main);
        let Some(job_cancel_stop_plan) =
            exact_stop_plan_from_claim(claim, &request.run_id.to_string(), true)
        else {
            let _ = self.runtime.cleanup_job_scope(&job_scope);
            return failed_job(
                &request,
                1,
                started,
                "job cancellation stop plan could not be bound",
            );
        };
        let outcome = run_interruptible_job(
            self.execute_host_runtime_plan_outcome(
                claim,
                arguments,
                HostRuntimePlan::JobRun(request.clone()),
            ),
            &mut cancellation,
            || async {
                self.execute_host_runtime_plan(
                    claim,
                    Vec::new(),
                    HostRuntimePlan::Stop(job_cancel_stop_plan),
                )
                .await
            },
        )
        .await;
        let outcome = match outcome {
            InterruptibleJob::Completed(outcome) => outcome,
            InterruptibleJob::Cancelled { stopped: true } => {
                let _ = self.runtime.complete_stop(&job_scope);
                if job_scope_cleanup.finish().is_err() {
                    return failed_job(
                        &request,
                        JOB_CANCEL_EXIT_CODE,
                        started,
                        "cancelled job scope cleanup failed",
                    );
                }
                return cancelled_job(&request, started, "controller cancellation requested");
            }
            InterruptibleJob::Cancelled { stopped: false } => {
                job_scope_cleanup.retain();
                return unconfirmed_job(
                    &request,
                    started,
                    WaitReason::JobStopUnconfirmed,
                    FailureStage::JobCancelStop,
                    "controller cancellation could not stop the active job",
                    None,
                );
            }
        };
        if outcome.as_ref().is_ok_and(|outcome| outcome.stop_uncertain) {
            job_scope_cleanup.retain();
            return unconfirmed_job(
                &request,
                started,
                WaitReason::JobStopUnconfirmed,
                FailureStage::JobStop,
                "job runtime could not be stopped safely",
                None,
            );
        }
        if let Err(error) = &outcome {
            job_scope_cleanup.retain();
            return unconfirmed_job(
                &request,
                started,
                WaitReason::JobStateUncertain,
                FailureStage::JobState,
                "job runtime execution or cleanup state is uncertain",
                Some(error.preflight_code()),
            );
        }
        // The job's own exit account and output, read by the helper
        // before it removed the container.
        let job_evidence = outcome
            .as_ref()
            .ok()
            .map(|outcome| (outcome.diagnostic.clone(), outcome.process_logs.clone()));
        let (exit_code, exit_reason) = match outcome {
            Ok(outcome) => match outcome.exit_code {
                Some(0) => (0, None),
                Some(124) => (124, Some("job adapter exceeded its deadline")),
                Some(code) if (0..=255).contains(&code) => (
                    u32::try_from(code).expect("nonnegative process exit status"),
                    Some("job adapter exited unsuccessfully"),
                ),
                Some(_) => {
                    return failed_job(
                        &request,
                        1,
                        started,
                        "job adapter reported an invalid exit status",
                    );
                }
                None => (1, Some("job adapter did not report an exit status")),
            },
            Err(_) => unreachable!("runtime errors return operator-waiting above"),
        };
        let _ = self.runtime.complete_stop(&job_scope);
        if *cancellation.borrow() {
            if job_scope_cleanup.finish().is_err() {
                return failed_job(
                    &request,
                    JOB_CANCEL_EXIT_CODE,
                    started,
                    "cancelled job scope cleanup failed",
                );
            }
            return cancelled_job(&request, started, "controller cancellation requested");
        }
        let output_manifest = match collect_job_outputs(
            self.runtime.job_output_root(&job_scope).ok().as_deref(),
            &request.output_limits,
            &request.output_mappings,
        ) {
            Ok(manifest) => manifest,
            Err(reason) => {
                let _ = self.runtime.cleanup_job_scope(&job_scope);
                return failed_job(&request, exit_code.max(1), started, reason);
            }
        };
        let output_root = match self.runtime.job_output_root(&job_scope) {
            Ok(root) => root,
            Err(_) => {
                let _ = self.runtime.cleanup_job_scope(&job_scope);
                return failed_job(
                    &request,
                    exit_code.max(1),
                    started,
                    "job output directory is unavailable",
                );
            }
        };
        for output in &output_manifest.files {
            let path = output_root.join(&output.name);
            let upload = run_until_cancelled(
                self.client.upload_recipe_job_output(
                    request.job_id,
                    &output.name,
                    &output.media_type,
                    &output.sha256,
                    u64::from(output.size_bytes),
                    &path,
                ),
                &mut cancellation,
            )
            .await;
            if upload.is_none() {
                if job_scope_cleanup.finish().is_err() {
                    return failed_job(
                        &request,
                        JOB_CANCEL_EXIT_CODE,
                        started,
                        "cancelled job scope cleanup failed",
                    );
                }
                return cancelled_job(&request, started, "controller cancellation requested");
            }
            if upload.is_some_and(|result| result.is_err()) {
                let _ = self.runtime.cleanup_job_scope(&job_scope);
                return failed_job(
                    &request,
                    exit_code.max(1),
                    started,
                    "job output upload failed",
                );
            }
        }
        if *cancellation.borrow() {
            if job_scope_cleanup.finish().is_err() {
                return failed_job(
                    &request,
                    JOB_CANCEL_EXIT_CODE,
                    started,
                    "cancelled job scope cleanup failed",
                );
            }
            return cancelled_job(&request, started, "controller cancellation requested");
        }
        let mut receipt = job_receipt(&request, exit_code, started, output_manifest, exit_reason);
        if job_scope_cleanup.finish().is_err() {
            return failed_job(
                &request,
                exit_code.max(1),
                started,
                "job scope cleanup failed",
            );
        }
        if exit_code == 0 {
            receipt.diagnostics = Some(preload_diagnostics);
            ExecutionResult::done(receipt)
        } else {
            job_failure(receipt, job_evidence)
        }
    }
}

pub(super) async fn wait_for_cancellation(cancellation: &mut tokio::sync::watch::Receiver<bool>) {
    // The enclosing operation owns the deadline and drops this observer at
    // completion. Losing its cancellation owner ends observation promptly.
    let _ = cancellation.wait_for(|cancelled| *cancelled).await;
}

pub(super) async fn run_until_cancelled<T, F>(
    operation: F,
    cancellation: &mut tokio::sync::watch::Receiver<bool>,
) -> Option<T>
where
    F: Future<Output = T>,
{
    tokio::pin!(operation);
    tokio::select! {
        biased;
        result = &mut operation => Some(result),
        () = wait_for_cancellation(cancellation) => None,
    }
}

pub(super) async fn run_interruptible_job<T, F, S, SF, E>(
    job: F,
    cancellation: &mut tokio::sync::watch::Receiver<bool>,
    stop: S,
) -> InterruptibleJob<T>
where
    F: Future<Output = T>,
    S: FnOnce() -> SF,
    SF: Future<Output = Result<(), E>>,
{
    tokio::pin!(job);
    tokio::select! {
        biased;
        result = &mut job => InterruptibleJob::Completed(result),
        () = wait_for_cancellation(cancellation) => {
            let stopped = stop().await.is_ok();
            if stopped {
                let _ = tokio::time::timeout(JOB_CANCEL_DRAIN_TIMEOUT, &mut job).await;
            }
            InterruptibleJob::Cancelled { stopped }
        }
    }
}

pub(super) fn job_placement(
    spec: &CompiledExecutionPlan,
) -> Result<CompiledRuntimePlacement, WorkloadError> {
    let placement = &spec.runtime.placement;
    if placement.rank != 0 || placement.world_size != 1 || placement.port.is_some() {
        return Err(WorkloadError::Invalid("job placement"));
    }
    placement.validate_bound()?;
    Ok(placement.clone())
}

pub fn recipe_job_input_manifest(
    request: &vonk_agent_protocol::RecipeJobRunRequest,
) -> Result<Vec<u8>, vonk_agent_protocol::ProtocolError> {
    canonical_json(&vonk_agent_protocol::generated::RecipeJobInputManifest {
        schema_version: 1,
        total_bytes: request.input_total_bytes,
        files: request.inputs.clone(),
    })
}

pub fn prepare_job_invocation(
    installed: &CompiledExecutionPlan,
    request: &vonk_agent_protocol::RecipeJobRunRequest,
) -> Result<CompiledExecutionPlan, WorkloadError> {
    let plan = request.compiled_execution_plan.clone();
    plan.validate()?;
    if plan.job.is_none() {
        return Err(WorkloadError::Invalid("job interface"));
    }
    if !crate::workloads::same_job_workload(installed, &plan) {
        return Err(WorkloadError::Invalid("job invocation authority"));
    }
    job_placement(&plan)?;
    Ok(plan)
}

pub(super) fn failed_job(
    request: &vonk_agent_protocol::RecipeJobRunRequest,
    exit_code: u32,
    started: Instant,
    reason: &'static str,
) -> ExecutionResult {
    // This failure did not come from the container's own exit, so its typed
    // reason is the account: there is no exit state or output to read.
    job_failure(
        job_receipt(
            request,
            exit_code,
            started,
            empty_job_output_manifest(),
            Some(reason),
        ),
        Some((Some(format!("job_not_run_to_completion={reason:?}")), None)),
    )
}

/// A job whose process ran and exited nonzero (or never ran): a definite failure
/// that keeps its receipt.
pub(super) fn job_failure(
    receipt: RecipeJobRunResult,
    evidence: Option<(
        Option<String>,
        Option<Box<crate::failure_evidence::FailureProcessLogs>>,
    )>,
) -> ExecutionResult {
    let reason = receipt
        .reason
        .clone()
        .unwrap_or_else(|| "job adapter exited unsuccessfully".to_owned());
    let (diagnostic, logs) = evidence.unwrap_or((None, None));
    // A failed job always says what it knows: its output and exit account, or
    // the typed reason there are none.
    let diagnostic = diagnostic.unwrap_or_else(|| {
        "exit_state_unavailable=\"helper reported no detail\" logs_unavailable=\"not captured\""
            .to_owned()
    });
    let code = if diagnostic
        .split_whitespace()
        .any(|token| token == "exit_cause=host_memory_exhausted")
    {
        FailureCode::WorkloadHostMemoryExhausted
    } else {
        FailureCode::RecipeJobRunFailed
    };
    let mut failure = Failure::new(reason)
        .code(code)
        .diagnostic(diagnostic.chars().take(480).collect::<String>())
        .receipt(receipt);
    if let Some(logs) = logs {
        failure = failure.process_logs(Some(*logs));
    }
    ExecutionResult::Failed(failure)
}

pub(super) fn cancelled_job(
    request: &vonk_agent_protocol::RecipeJobRunRequest,
    started: Instant,
    reason: &'static str,
) -> ExecutionResult {
    ExecutionResult::Failed(
        Failure::new(reason)
            .code(FailureCode::OperationCancelled)
            .receipt(job_receipt(
                request,
                JOB_CANCEL_EXIT_CODE,
                started,
                empty_job_output_manifest(),
                Some(reason),
            )),
    )
}

/// A job whose stop or final state could not be confirmed: its receipt is kept.
pub(super) fn unconfirmed_job(
    request: &vonk_agent_protocol::RecipeJobRunRequest,
    started: Instant,
    wait_reason: WaitReason,
    stage: FailureStage,
    reason: &'static str,
    cause: Option<String>,
) -> ExecutionResult {
    ExecutionResult::Unknown(crate::outcome::Unconfirmed {
        wait_reason,
        reason: reason.to_owned(),
        evidence: UnknownEvidence::at(stage).because(cause.unwrap_or_else(|| reason.to_owned())),
        receipt: Some(job_receipt(
            request,
            JOB_CANCEL_EXIT_CODE,
            started,
            empty_job_output_manifest(),
            Some(reason),
        )),
    })
}

pub(super) fn output_manifest_with_digest(
    content: vonk_agent_protocol::generated::RecipeJobOutputManifestContent,
) -> Result<RecipeJobOutputManifest, ProtocolError> {
    let manifest_sha256 = hex_sha256(&canonical_json(&content)?);
    Ok(RecipeJobOutputManifest {
        schema_version: content.schema_version,
        manifest_sha256,
        total_bytes: content.total_bytes,
        files: content.files,
    })
}

pub(super) fn empty_job_output_manifest() -> RecipeJobOutputManifest {
    output_manifest_with_digest(
        vonk_agent_protocol::generated::RecipeJobOutputManifestContent {
            schema_version: 1,
            total_bytes: 0,
            files: Vec::new(),
        },
    )
    .expect("canonical empty job manifest")
}

pub(super) fn job_receipt(
    request: &vonk_agent_protocol::RecipeJobRunRequest,
    exit_code: u32,
    started: Instant,
    output_manifest: RecipeJobOutputManifest,
    reason: Option<&str>,
) -> RecipeJobRunResult {
    let result = RecipeJobRunResult {
        job_id: request.job_id,
        run_id: request.run_id,
        exit_code,
        output_manifest,
        evidence: RecipeJobEvidence {
            // A job that legitimately runs past the u32::MAX millisecond
            // ceiling (~49.7 days) saturates its reported elapsed time instead
            // of failing the result report with a panic.
            elapsed_milliseconds: u32::try_from(started.elapsed().as_millis()).unwrap_or(u32::MAX),
            // The helper does not expose a cgroup peak for transient containers yet. Null is
            // honest unavailable evidence; zero would falsely claim a measurement.
            peak_memory_bytes: None,
        },
        reason: reason.map(str::to_owned),
        diagnostics: None,
    };
    debug_assert!(result.validate().is_ok());
    result
}

pub(super) fn collect_job_outputs(
    root: Option<&Path>,
    limits: &RecipeJobOutputLimits,
    mappings: &[RecipeJobOutputMapping],
) -> Result<RecipeJobOutputManifest, &'static str> {
    let root = root.ok_or("job output directory is unavailable")?;
    let mut entries = fs::read_dir(root)
        .map_err(|_| "job output directory is unavailable")?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| "job output directory is unavailable")?;
    entries.sort_by_key(fs::DirEntry::file_name);
    if entries.len() > usize::try_from(limits.max_files).expect("bounded job output count") {
        return Err("job output file count exceeded its bound");
    }
    let mut files = Vec::with_capacity(entries.len());
    let mut total_bytes = 0_u64;
    for entry in entries {
        let file_type = entry.file_type().map_err(|_| "job output is unsafe")?;
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| "job output name is invalid")?;
        if !file_type.is_file() || file_type.is_symlink() || !valid_job_output_name(&name) {
            return Err("job output is unsafe");
        }
        let metadata = entry.metadata().map_err(|_| "job output is unsafe")?;
        if metadata.len() > u64::from(limits.max_file_bytes) {
            return Err("job output file size exceeded its bound");
        }
        total_bytes = total_bytes
            .checked_add(metadata.len())
            .filter(|total| *total <= u64::from(limits.max_total_bytes))
            .ok_or("job output total size exceeded its bound")?;
        let media_type = output_media_type(&name, mappings)
            .ok_or("job output media type is not declared by its signed slot mapping")?;
        if !limits
            .allowed_media_types
            .iter()
            .any(|allowed| allowed == media_type)
        {
            return Err("job output media type is not allowed");
        }
        let mut file = File::open(entry.path()).map_err(|_| "job output is unsafe")?;
        let mut hasher = Sha256::new();
        let mut observed = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let read = file
                .read(&mut buffer)
                .map_err(|_| "job output could not be read")?;
            if read == 0 {
                break;
            }
            observed = observed
                .checked_add(read as u64)
                .filter(|bytes| *bytes <= metadata.len())
                .ok_or("job output changed while it was collected")?;
            hasher.update(&buffer[..read]);
        }
        if observed != metadata.len() {
            return Err("job output changed while it was collected");
        }
        files.push(RecipeJobFile {
            name,
            media_type: media_type.to_owned(),
            size_bytes: u32::try_from(observed)
                .map_err(|_| "job output file size exceeded its bound")?,
            sha256: hex::encode(hasher.finalize()),
        });
    }
    output_manifest_with_digest(
        vonk_agent_protocol::generated::RecipeJobOutputManifestContent {
            schema_version: 1,
            total_bytes: u32::try_from(total_bytes)
                .map_err(|_| "job output total size exceeded its bound")?,
            files,
        },
    )
    .map_err(|_| "job output manifest is invalid")
}

pub(super) fn valid_job_output_name(value: &str) -> bool {
    !value.is_empty()
        && value != "manifest.json"
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

pub(super) fn output_media_type<'mapping>(
    name: &str,
    mappings: &'mapping [RecipeJobOutputMapping],
) -> Option<&'mapping str> {
    mappings
        .iter()
        .flat_map(|mapping| {
            mapping
                .extensions
                .iter()
                .map(move |extension| (extension, mapping.media_type.as_str()))
        })
        .filter(|(extension, _)| name.len() > extension.len() && name.ends_with(extension.as_str()))
        .max_by_key(|(extension, _)| extension.len())
        .map(|(_, media_type)| media_type)
}

#[cfg(test)]
mod tests;
