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
        if self.active
            && let Err(error) = self.runtime.cleanup_job_scope(self.job_scope)
        {
            eprintln!(
                "vonk-agent: job {} scope cleanup deferred: {}",
                self.job_scope,
                error.safe_category()
            );
        }
    }
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    /// Retire the exact job run's lifecycle record. A failure keeps its cause
    /// for the outcome the caller returns, and in the agent journal.
    fn record_job_stop(&self, job_scope: &str) -> Option<String> {
        let error = self.runtime.complete_stop(job_scope).err()?;
        let cause = format!("job stop record deferred: {}", error.safe_category());
        eprintln!("vonk-agent: {job_scope} {cause}");
        Some(cause)
    }

    /// Stop the exact job run and retire its record; the cause of whichever
    /// step did not complete is returned for the unknown outcome.
    async fn stop_job(
        &self,
        claim: &AgentClaim,
        stop_plan: &RecipeStopRequest,
        job_scope: &str,
    ) -> Option<String> {
        match self
            .execute_host_runtime_plan(claim, Vec::new(), HostRuntimePlan::Stop(stop_plan.clone()))
            .await
        {
            Ok(()) => self.record_job_stop(job_scope),
            Err(error) => Some(format!("job stop unconfirmed: {}", error.cause())),
        }
    }
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_job_run(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        mut cancellation: tokio::sync::watch::Receiver<bool>,
        request: vonk_agent_protocol::RecipeJobRunRequest,
    ) -> ExecutionResult {
        self.report_phase(claim, ProgressPhase::Preparing).await;
        let started = Instant::now();
        let job_scope = request.job_id.to_string();
        let spec = &request.compiled_execution_plan;
        let invocation = match prepare_job_invocation(spec, &request) {
            Ok(plan) => plan,
            Err(error) => {
                return failed_stage_owned(
                    "job invocation is invalid",
                    FailureStage::JobState,
                    error.to_string(),
                );
            }
        };
        let input_manifest = match recipe_job_input_manifest(&request) {
            Ok(bytes) => bytes,
            Err(error) => {
                return failed_stage_owned(
                    "job input manifest is invalid",
                    FailureStage::JobState,
                    error.to_string(),
                );
            }
        };
        let placement = match job_placement(&invocation) {
            Ok(value) => value,
            Err(error) => {
                return failed_stage_owned(
                    "job placement is invalid",
                    FailureStage::JobState,
                    format!("caller supplied malformed placement: {error}"),
                );
            }
        };
        let Some(stop_plan) = exact_stop_plan_from_claim(claim, &request.run_id.to_string(), true)
        else {
            return failed("job cancellation binding is invalid");
        };
        let unknown = |cause: String| {
            unconfirmed_job(
                &request,
                started,
                WaitReason::JobStateUncertain,
                FailureStage::JobState,
                "job custody or delivery remains unobserved",
                Some(cause),
            )
        };
        let local =
            |step: &str, error: crate::oci::OciError| format!("{step}: {}", error.safe_category());
        if *cancellation.borrow() {
            return cancelled_job(&request, started, "controller cancellation requested");
        }
        let mut receipt = match self.runtime.retained_job_completion(&request) {
            Ok(Some(receipt)) => receipt,
            Err(error) => {
                // Dispatch may have succeeded before its durable exit account
                // was lost. Reconcile the exact authorized effect, never run
                // the process again from damaged bookkeeping.
                let mut cause = local("retained job completion unreadable", error);
                if let Some(stop) = self.stop_job(claim, &stop_plan, &job_scope).await {
                    cause = format!("{cause}; {stop}");
                }
                return unknown(cause);
            }
            Ok(None) => {
                if let Err(result) = self
                    .prepare_installation(
                        claim,
                        spec,
                        &request.installation_id.to_string(),
                        &lease_deadline,
                        &cancellation,
                    )
                    .await
                {
                    return *result;
                }
                if let Err(error) = self.runtime.cleanup_job_scope(&job_scope) {
                    return unknown(local("job scope cleanup", error));
                }
                self.report_phase(claim, ProgressPhase::Downloading).await;
                for input in &request.inputs {
                    let destination =
                        match self.runtime.job_input_destination(&job_scope, &input.name) {
                            Ok(path) => path,
                            Err(error) => return unknown(local("job input destination", error)),
                        };
                    let result = distribution::run_with_authority(
                        self.client.download_recipe_job_input(
                            request.job_id,
                            &input.sha256,
                            u64::from(input.size_bytes),
                            &destination,
                            |bytes| self.client.add_progress_bytes(claim.fence, bytes),
                        ),
                        lease_deadline.clone(),
                        cancellation.clone(),
                    )
                    .await;
                    match result {
                        Some(Ok(())) => {}
                        // Keep why the input did not arrive (a stall names its
                        // window and the bytes that moved).
                        Some(Err(error)) => {
                            return unconfirmed_job(
                                &request,
                                started,
                                WaitReason::JobStateUncertain,
                                FailureStage::JobState,
                                "job input delivery remains unobserved",
                                Some(error.to_string()),
                            );
                        }
                        None => {
                            return unknown(
                                "order authority ended during the input delivery".to_owned(),
                            );
                        }
                    }
                }
                let names = request
                    .inputs
                    .iter()
                    .map(|input| input.name.clone())
                    .collect::<Vec<_>>();
                if let Err(error) = self.runtime.write_job_input_manifest(
                    &job_scope,
                    &names,
                    &input_manifest,
                    &request.input_manifest_sha256,
                ) {
                    return unknown(local("job input manifest write", error));
                }
                let plan = match self.runtime.prepare_job_start(
                    spec,
                    &request.installation_id.to_string(),
                    &job_scope,
                    &placement,
                    &invocation,
                ) {
                    Ok(plan) => plan,
                    Err(error) => return unknown(local("job start preparation", error)),
                };
                let mut arguments = vec![
                    plan.archive_sha256,
                    plan.registry_index_digest,
                    plan.platform_manifest_digest,
                    plan.image_reference,
                ];
                arguments.extend(plan.main);
                if let Err(error) = self.runtime.persist_job_intent(&request) {
                    return unknown(local("job intent record", error));
                }
                JobScopeCleanup::new(&self.runtime, &job_scope).retain();
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
                            HostRuntimePlan::Stop(stop_plan.clone()),
                        )
                        .await
                    },
                )
                .await;
                let outcome = match outcome {
                    InterruptibleJob::Completed(Ok(outcome)) if !outcome.stop_uncertain => outcome,
                    InterruptibleJob::Cancelled { stopped: true } => {
                        // A confirmed cancellation ends this request; its
                        // bookkeeping cannot veto a new job with its own
                        // request identity, and a failure is journaled.
                        self.record_job_stop(&job_scope);
                        if let Err(error) = JobScopeCleanup::new(&self.runtime, &job_scope).finish()
                        {
                            eprintln!(
                                "vonk-agent: {job_scope} job scope cleanup deferred: {}",
                                error.safe_category()
                            );
                        }
                        return cancelled_job(
                            &request,
                            started,
                            "controller cancellation confirmed",
                        );
                    }
                    other => {
                        let mut cause = match other {
                            InterruptibleJob::Completed(Err(error)) => {
                                format!("job run unconfirmed: {}", error.cause())
                            }
                            InterruptibleJob::Completed(Ok(_)) => {
                                "job run stop is uncertain".to_owned()
                            }
                            InterruptibleJob::Cancelled { .. } => {
                                "job cancellation stop is unconfirmed".to_owned()
                            }
                        };
                        if let Some(stop) = self.stop_job(claim, &stop_plan, &job_scope).await {
                            cause = format!("{cause}; {stop}");
                        }
                        return unknown(cause);
                    }
                };
                let Some(code) = outcome.exit_code.filter(|code| (0..=255).contains(code)) else {
                    return unknown(format!(
                        "job exit code {:?} is out of range",
                        outcome.exit_code
                    ));
                };
                // The exit is observed; the stop record is bookkeeping, and a
                // failure to retire it is journaled with its cause.
                self.record_job_stop(&job_scope);
                let reason = if code as u32 == JOB_CANCEL_EXIT_CODE {
                    Some("job adapter exited after interruption")
                } else {
                    (code != 0).then_some("job adapter exited unsuccessfully")
                };
                let mut receipt = job_receipt(
                    &request,
                    code as u32,
                    started,
                    empty_job_output_manifest(),
                    reason,
                );
                let mut diagnostics = self.runtime.report_preload_memory(
                    request.placement().reserved_memory_bytes,
                    Path::new("/proc/meminfo"),
                );
                if let Some(logs) = outcome.process_logs {
                    diagnostics.stdout = logs.stdout.clone();
                    diagnostics.stderr = logs.stderr.clone();
                }
                receipt.diagnostics = Some(diagnostics);
                // Publish the real exit independently from output observation.
                // Failure here retains intent and output; it never repeats an
                // already dispatched process or invents a process exit.
                if let Err(error) = self.runtime.persist_job_completion(&receipt) {
                    return unknown(local("job completion record", error));
                }
                receipt
            }
        };
        let output_root = match self.runtime.job_output_root(&job_scope) {
            Ok(root) => root,
            Err(error) => return unknown(local("job output root", error)),
        };
        if receipt.output_manifest.files.is_empty() {
            receipt.output_manifest = match collect_job_outputs(
                Some(&output_root),
                &request.output_limits,
                &request.output_mappings,
            ) {
                Ok(manifest) => manifest,
                Err(cause) => return unknown(format!("job output collection: {cause}")),
            };
        }
        if let Err(error) = self.runtime.persist_job_completion(&receipt) {
            return unknown(local("job completion record", error));
        }
        self.report_phase(claim, ProgressPhase::Uploading).await;
        for output in &receipt.output_manifest.files {
            let uploaded = distribution::run_with_authority(
                self.client.upload_recipe_job_output(
                    request.job_id,
                    &output.name,
                    &output.media_type,
                    &output.sha256,
                    u64::from(output.size_bytes),
                    &output_root.join(&output.name),
                ),
                lease_deadline.clone(),
                cancellation.clone(),
            )
            .await;
            if !matches!(uploaded, Some(Ok(()))) {
                // The content-addressed PUT is idempotent at the Controller;
                // lost acknowledgements are reconciled by the same PUT. Its
                // receipt and outputs stay until the owning job is retired.
                let cause = match &uploaded {
                    Some(Err(error)) => error.to_string(),
                    _ => "order authority ended during the upload".to_owned(),
                };
                return ExecutionResult::Unknown(crate::outcome::Unconfirmed {
                    wait_reason: WaitReason::JobStateUncertain,
                    reason: "job output delivery remains unobserved".into(),
                    evidence: UnknownEvidence::at(FailureStage::JobState).because(cause),
                    receipt: Some(receipt),
                });
            }
        }
        // Retain completion after result delivery too: a lost finish response
        // must replay the same result without another process execution.
        if receipt.exit_code == 0 {
            ExecutionResult::done(receipt)
        } else {
            let logs = receipt.diagnostics.as_ref().map(|diagnostics| {
                Box::new(crate::failure_evidence::FailureProcessLogs {
                    stdout: diagnostics.stdout.clone(),
                    stderr: diagnostics.stderr.clone(),
                })
            });
            job_failure(receipt, Some((None, logs)))
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

/// A measured nonzero process exit retains its real output and exit account.
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
    _request: &vonk_agent_protocol::RecipeJobRunRequest,
    _started: Instant,
    reason: &'static str,
) -> ExecutionResult {
    // Cancellation is an operation ending, not an invented process exit.
    ExecutionResult::cancelled(reason)
}

/// No synthetic process exit is attached to an unobserved job effect.
pub(super) fn unconfirmed_job(
    _request: &vonk_agent_protocol::RecipeJobRunRequest,
    _started: Instant,
    wait_reason: WaitReason,
    stage: FailureStage,
    reason: &'static str,
    cause: Option<String>,
) -> ExecutionResult {
    ExecutionResult::Unknown(crate::outcome::Unconfirmed {
        wait_reason,
        reason: reason.to_owned(),
        evidence: UnknownEvidence::at(stage).because(cause.unwrap_or_else(|| reason.to_owned())),
        receipt: None,
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
    // Runtime scratch is owned separately from exported job files. Preserve
    // it for lifecycle cleanup; it is never an output slot or count claim.
    entries.retain(|entry| {
        entry.file_name() != "tmp" || !entry.file_type().is_ok_and(|kind| kind.is_dir())
    });
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
