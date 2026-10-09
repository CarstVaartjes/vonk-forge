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
            Err(_) => return failed("job invocation is invalid"),
        };
        let input_manifest = match recipe_job_input_manifest(&request) {
            Ok(bytes) => bytes,
            Err(_) => return failed("job input manifest is invalid"),
        };
        let placement = match job_placement(&invocation) {
            Ok(value) => value,
            Err(_) => {
                return failed_stage_owned(
                    "job placement is invalid",
                    FailureStage::JobState,
                    "caller supplied malformed placement".to_owned(),
                );
            }
        };
        let Some(stop_plan) = exact_stop_plan_from_claim(claim, &request.run_id.to_string(), true)
        else {
            return failed("job cancellation binding is invalid");
        };
        let unknown = || {
            unconfirmed_job(
                &request,
                started,
                WaitReason::JobStateUncertain,
                FailureStage::JobState,
                "job custody or delivery remains unobserved",
                None,
            )
        };
        if *cancellation.borrow() {
            return cancelled_job(&request, started, "controller cancellation requested");
        }
        let mut receipt = match self.runtime.retained_job_completion(&request) {
            Ok(Some(receipt)) => receipt,
            Err(_) => {
                // Dispatch may have succeeded before its durable exit account
                // was lost. Reconcile the exact authorized effect, never run
                // the process again from damaged bookkeeping.
                if self
                    .execute_host_runtime_plan(
                        claim,
                        Vec::new(),
                        HostRuntimePlan::Stop(stop_plan.clone()),
                    )
                    .await
                    .is_ok()
                {
                    let _ = self.runtime.complete_stop(&job_scope);
                }
                return unknown();
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
                if self.runtime.cleanup_job_scope(&job_scope).is_err() {
                    return unknown();
                }
                for input in &request.inputs {
                    let destination =
                        match self.runtime.job_input_destination(&job_scope, &input.name) {
                            Ok(path) => path,
                            Err(_) => return unknown(),
                        };
                    let result = distribution::run_with_authority(
                        self.client.download_recipe_job_input(
                            request.job_id,
                            &input.sha256,
                            u64::from(input.size_bytes),
                            &destination,
                        ),
                        lease_deadline.clone(),
                        cancellation.clone(),
                        Duration::from_secs(75),
                    )
                    .await;
                    if !matches!(result, Some(Ok(()))) {
                        return unknown();
                    }
                }
                let names = request
                    .inputs
                    .iter()
                    .map(|input| input.name.clone())
                    .collect::<Vec<_>>();
                if self
                    .runtime
                    .write_job_input_manifest(
                        &job_scope,
                        &names,
                        &input_manifest,
                        &request.input_manifest_sha256,
                    )
                    .is_err()
                {
                    return unknown();
                }
                let plan = match self.runtime.prepare_job_start(
                    spec,
                    &request.installation_id.to_string(),
                    &job_scope,
                    &placement,
                    &invocation,
                ) {
                    Ok(plan) => plan,
                    Err(_) => return unknown(),
                };
                let mut arguments = vec![
                    plan.archive_sha256,
                    plan.registry_index_digest,
                    plan.platform_manifest_digest,
                    plan.image_reference,
                ];
                arguments.extend(plan.main);
                if self.runtime.persist_job_intent(&request).is_err() {
                    return unknown();
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
                        let _ = self.runtime.complete_stop(&job_scope);
                        // A confirmed cancellation ends this request; cleanup
                        // cannot veto a new job with its own request identity.
                        let _ = JobScopeCleanup::new(&self.runtime, &job_scope).finish();
                        return cancelled_job(
                            &request,
                            started,
                            "controller cancellation confirmed",
                        );
                    }
                    _ => {
                        if self
                            .execute_host_runtime_plan(
                                claim,
                                Vec::new(),
                                HostRuntimePlan::Stop(stop_plan.clone()),
                            )
                            .await
                            .is_ok()
                        {
                            let _ = self.runtime.complete_stop(&job_scope);
                        }
                        return unknown();
                    }
                };
                let Some(code) = outcome.exit_code.filter(|code| (0..=255).contains(code)) else {
                    return unknown();
                };
                let _ = self.runtime.complete_stop(&job_scope);
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
                if self.runtime.persist_job_completion(&receipt).is_err() {
                    return unknown();
                }
                receipt
            }
        };
        let output_root = match self.runtime.job_output_root(&job_scope) {
            Ok(root) => root,
            Err(_) => return unknown(),
        };
        if receipt.output_manifest.files.is_empty() {
            receipt.output_manifest = match collect_job_outputs(
                Some(&output_root),
                &request.output_limits,
                &request.output_mappings,
            ) {
                Ok(manifest) => manifest,
                Err(_) => return unknown(),
            };
        }
        if self.runtime.persist_job_completion(&receipt).is_err() {
            return unknown();
        }
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
                Duration::from_secs(3600),
            )
            .await;
            if !matches!(uploaded, Some(Ok(()))) {
                // The content-addressed PUT is idempotent at the Controller;
                // lost acknowledgements are reconciled by the same PUT. Its
                // receipt and outputs stay until the owning job is retired.
                return ExecutionResult::Unknown(crate::outcome::Unconfirmed {
                    wait_reason: WaitReason::JobStateUncertain,
                    reason: "job output delivery remains unobserved".into(),
                    evidence: UnknownEvidence::at(FailureStage::JobState),
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
