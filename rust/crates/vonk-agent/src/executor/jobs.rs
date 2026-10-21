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

/// One job whose process this agent may have dispatched.
pub(super) struct DispatchedJob<'job> {
    pub(super) claim: &'job AgentClaim,
    pub(super) request: &'job vonk_agent_protocol::RecipeJobRunRequest,
    pub(super) stop_plan: &'job RecipeStopRequest,
    pub(super) job_scope: &'job str,
    pub(super) started: Instant,
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    /// A confirmed cancellation ends this request; its bookkeeping cannot
    /// veto a new job with its own request identity, and a failure is
    /// journaled.
    fn finish_cancelled_job(&self, job: &DispatchedJob<'_>) -> ExecutionResult {
        self.record_job_stop(job.job_scope);
        if let Err(error) = JobScopeCleanup::new(&self.runtime, job.job_scope).finish() {
            eprintln!(
                "vonk-agent: {} job scope cleanup deferred: {}",
                job.job_scope,
                error.safe_category()
            );
        }
        cancelled_job(
            job.request,
            job.started,
            "controller cancellation confirmed",
        )
    }

    /// What the helper answered for the dispatched job, settled into the
    /// job's exit account or the result that ends this request.
    pub(super) async fn settle_job_dispatch(
        &self,
        job: &DispatchedJob<'_>,
        outcome: InterruptibleJob<
            Result<HostRuntimeOutcome, crate::host_runtime::HostRuntimeError>,
        >,
        dispatched: Instant,
        cancellation: &mut tokio::sync::watch::Receiver<bool>,
    ) -> Result<HostRuntimeOutcome, Box<ExecutionResult>> {
        match outcome {
            InterruptibleJob::Completed(Ok(outcome)) if !outcome.stop_uncertain => Ok(outcome),
            InterruptibleJob::Cancelled { stopped: true } => {
                Err(Box::new(self.finish_cancelled_job(job)))
            }
            other => {
                if let InterruptibleJob::Completed(Err(error)) = &other
                    && let Some(refused) = refused_job_start(error)
                {
                    // Nothing started, so no stop: release the scope.
                    let _ = JobScopeCleanup::new(&self.runtime, job.job_scope).finish();
                    return Err(Box::new(refused));
                }
                let (stage, cause) = match other {
                    InterruptibleJob::Completed(Err(error)) => (
                        FailureStage::RuntimeAdapter,
                        format!("job run unconfirmed: {}", error.cause()),
                    ),
                    InterruptibleJob::Completed(Ok(_)) => (
                        FailureStage::JobStop,
                        "job run stop is uncertain".to_owned(),
                    ),
                    InterruptibleJob::Cancelled { .. } => (
                        FailureStage::JobCancelStop,
                        "job cancellation stop is unconfirmed".to_owned(),
                    ),
                };
                // The answer was lost, not the job: it is re-observed by its
                // identity before anything stops it.
                let deadline = reobserve::job_deadline(dispatched, job.request);
                self.reobserve_job(job, deadline, cancellation, stage, cause)
                    .await
            }
        }
    }

    /// The job's measured exit as its durable receipt, published before any
    /// output is observed.
    pub(super) fn job_exit_receipt(
        &self,
        job: &DispatchedJob<'_>,
        outcome: HostRuntimeOutcome,
    ) -> Result<RecipeJobRunResult, Box<ExecutionResult>> {
        let Some(code) = outcome.exit_code.filter(|code| (0..=255).contains(code)) else {
            return Err(out_of_range_exit(outcome.exit_code));
        };
        // The exit is observed; the stop record is bookkeeping, and a
        // failure to retire it is journaled with its cause.
        self.record_job_stop(job.job_scope);
        let reason = if code as u32 == JOB_CANCEL_EXIT_CODE {
            Some("job adapter exited after interruption")
        } else {
            (code != 0).then_some("job adapter exited unsuccessfully")
        };
        let mut receipt = job_receipt(
            job.request,
            code as u32,
            job.started,
            empty_job_output_manifest(),
            reason,
        );
        let mut diagnostics = self.runtime.report_preload_memory(
            job.request.placement().reserved_memory_bytes,
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
            let (stage, cause) =
                local_step(FailureStage::JobState, "job completion record", &error);
            return Err(Box::new(job.unknown_after_exit(
                &receipt,
                stage,
                "job custody or delivery remains unobserved",
                cause,
            )));
        }
        Ok(receipt)
    }
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_job_run(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
        request: vonk_agent_protocol::RecipeJobRunRequest,
    ) -> ExecutionResult {
        let work = self.run_job(
            claim,
            lease_deadline.clone(),
            cancellation.clone(),
            request.clone(),
        );
        tokio::pin!(work);
        let result = tokio::select! {
            biased;
            result = &mut work => result,
            () = self.observe_job_progress(claim, &request, lease_deadline, cancellation) => work.await,
        };
        bounded_job_result(result)
    }

    async fn run_job(
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
        let Some(stop_plan) = job_run_stop_plan(claim, &request) else {
            return failed("job cancellation binding is invalid");
        };
        if *cancellation.borrow() {
            return cancelled_job(&request, started, "controller cancellation requested");
        }
        let retained_completion = self.runtime.retained_job_completion(&request);
        if request.output_delivery_only == Some(true)
            && !matches!(&retained_completion, Ok(Some(_)))
        {
            return failed_stage_owned(
                "retained job output receipt is unavailable; delivery-only retry cannot launch a process",
                FailureStage::JobOutputDelivery,
                "job output scope or receipt is missing or unreadable".to_owned(),
            );
        }
        let retained = match retained_completion {
            // No record of its own does not prove the job never ran: its
            // whole scope can be gone while the helper keeps its exit. It is
            // re-observed by its identity, and dispatched only when unseen.
            Ok(None) => match self.job_seen_before_dispatch(&request).await {
                Ok(seen) => seen.map_or(Ok(None), Err),
                // Unread: this attempt runs nothing, and the Controller
                // issues the job again.
                Err(error) => {
                    if matches!(
                        &error,
                        crate::host_runtime::HostRuntimeError::HelperRejected {
                            code: HelperErrorCode::OperationInvalid
                                | HelperErrorCode::OperationInvalidArtifact,
                            ..
                        }
                    ) {
                        return runtime_failure("job observation refused before dispatch", &error);
                    }
                    return temporary_preparation_failure(
                        FailureStage::RunStorage,
                        "job reobserve",
                        &error.preflight_code(),
                    );
                }
            },
            Ok(Some(receipt)) => Ok(Some(receipt)),
            // Dispatch may have succeeded before its durable exit account
            // was lost.
            Err(error) => Err(local_step(
                FailureStage::JobState,
                "retained job completion unreadable",
                &error,
            )),
        };
        let receipt = match retained {
            Ok(Some(receipt)) => receipt,
            Err((stage, cause)) => {
                // The exact authorized effect is re-observed by its identity;
                // the process is never run again.
                let job = DispatchedJob {
                    claim,
                    request: &request,
                    stop_plan: &stop_plan,
                    job_scope: &job_scope,
                    started,
                };
                let deadline = reobserve::job_deadline(Instant::now(), &request);
                let receipt = match self
                    .reobserve_job(&job, deadline, &mut cancellation, stage, cause)
                    .await
                {
                    Ok(outcome) => self.job_exit_receipt(&job, outcome),
                    Err(result) => Err(result),
                };
                match receipt {
                    Ok(receipt) => receipt,
                    Err(result) => return *result,
                }
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
                    return job_step_before_launch(
                        FailureStage::RunStorage,
                        "job scope cleanup",
                        &error,
                    );
                }
                self.report_phase(claim, ProgressPhase::Downloading).await;
                for input in &request.inputs {
                    let destination =
                        match self.runtime.job_input_destination(&job_scope, &input.name) {
                            Ok(path) => path,
                            Err(error) => {
                                return job_step_before_launch(
                                    FailureStage::JobInputs,
                                    "job input destination",
                                    &error,
                                );
                            }
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
                        // The job has not run: a definite refusal of its input
                        // (authority, 400/404/410/422) ends it with that typed
                        // evidence instead of an anonymous uncertainty.
                        Some(Err(error))
                            if matches!(
                                error.disposition(),
                                Disposition::Fail | Disposition::Security
                            ) =>
                        {
                            return recipe_build_client_failure_result(
                                &error,
                                FailureStage::JobInputs,
                                "job input was refused by the Controller",
                            );
                        }
                        // The job has not run: an input that did not arrive is a
                        // wait, and the next attempt fetches it again. Keep why
                        // (a stall names its window and the bytes that moved).
                        Some(Err(error)) => {
                            let cause = crate::failure_evidence::sanitize_text(&error.to_string())
                                .chars()
                                .take(400)
                                .collect::<String>();
                            return temporary_preparation_failure(
                                FailureStage::JobInputs,
                                "job input download",
                                &cause,
                            );
                        }
                        None if *cancellation.borrow() => {
                            return cancelled_job(
                                &request,
                                started,
                                "controller cancellation requested",
                            );
                        }
                        None => {
                            return temporary_preparation_failure(
                                FailureStage::JobInputs,
                                "job input download",
                                super::installation::attempt_end_cause(&lease_deadline),
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
                    return job_step_before_launch(
                        FailureStage::JobInputs,
                        "job input manifest write",
                        &error,
                    );
                }
                let plan = match self.runtime.prepare_job_start(
                    spec,
                    &request.installation_id.to_string(),
                    &job_scope,
                    &placement,
                    &invocation,
                ) {
                    Ok(plan) => plan,
                    Err(error) => {
                        return job_step_before_launch(
                            FailureStage::RuntimeProjection,
                            "job start preparation",
                            &error,
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
                if let Err(error) = self.runtime.persist_job_intent(&request) {
                    return job_step_before_launch(
                        FailureStage::RunStorage,
                        "job intent record",
                        &error,
                    );
                }
                JobScopeCleanup::new(&self.runtime, &job_scope).retain();
                let dispatched = Instant::now();
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
                let job = DispatchedJob {
                    claim,
                    request: &request,
                    stop_plan: &stop_plan,
                    job_scope: &job_scope,
                    started,
                };
                let outcome = match self
                    .settle_job_dispatch(&job, outcome, dispatched, &mut cancellation)
                    .await
                {
                    Ok(outcome) => outcome,
                    Err(result) => return *result,
                };
                match self.job_exit_receipt(&job, outcome) {
                    Ok(receipt) => receipt,
                    Err(result) => return *result,
                }
            }
        };
        let job = DispatchedJob {
            claim,
            request: &request,
            stop_plan: &stop_plan,
            job_scope: &job_scope,
            started,
        };
        self.finish_receipted_job(&job, receipt, lease_deadline, cancellation)
            .await
    }

    /// Everything after the job's exit is receipted: collect, record and
    /// deliver its outputs. The process has ceased, so every unconfirmed
    /// step here carries the receipt (`unknown_after_exit`).
    async fn finish_receipted_job(
        &self,
        job: &DispatchedJob<'_>,
        mut receipt: RecipeJobRunResult,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        mut cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        let (claim, request) = (job.claim, job.request);
        let unknown = |receipt: &RecipeJobRunResult, stage, step, error| {
            let (stage, cause) = local_step(stage, step, &error);
            job.unknown_after_exit(
                receipt,
                stage,
                "job custody or delivery remains unobserved",
                cause,
            )
        };
        let output_root = match self.runtime.job_output_root(job.job_scope) {
            Ok(root) => root,
            Err(error) => {
                return unknown(
                    &receipt,
                    FailureStage::OutputStorage,
                    "job output root",
                    error,
                );
            }
        };
        if receipt.output_manifest.files.is_empty() {
            receipt.output_manifest = match self
                .collect_receipted_outputs(job, &receipt, &output_root, &mut cancellation)
                .await
            {
                Ok(manifest) => manifest,
                Err(result) => return *result,
            };
        }
        if let Err(error) = self.runtime.persist_job_completion(&receipt) {
            return unknown(
                &receipt,
                FailureStage::JobState,
                "job completion record",
                error,
            );
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
                if let Some(Err(error)) = &uploaded
                    && !error.output_delivery_retryable()
                {
                    return ExecutionResult::Failed(
                        Failure::new("job output upload was refused")
                            .kind(recipe_build_client_failure_kind(error))
                            .diagnostic(controller_denial_diagnostic(error))
                            .receipt(receipt),
                    );
                }
                return job.unknown_after_exit(
                    &receipt,
                    FailureStage::JobOutputDelivery,
                    "job output undelivered; retained files await re-delivery",
                    cause,
                );
            }
        }
        // Retain completion after result delivery too: a lost finish response
        // must replay the same result without another process execution.
        if receipt.exit_code == 0 {
            ExecutionResult::done(receipt)
        } else {
            let logs = receipt.diagnostics.as_ref().map(|diagnostics| {
                Box::new(crate::failure_evidence::FailureProcessLogs {
                    exit_code: None,
                    stdout: diagnostics.stdout.clone(),
                    stderr: diagnostics.stderr.clone(),
                })
            });
            job_failure(receipt, Some((None, logs)))
        }
    }
}

/// The one Stop of a JobRun, sent for cancellation and for cleanup alike: it
/// fences any pending Start of the same job and targets the job's container.
pub(super) fn job_run_stop_plan(
    claim: &AgentClaim,
    request: &vonk_agent_protocol::RecipeJobRunRequest,
) -> Option<RecipeStopRequest> {
    exact_stop_plan_from_claim(claim, &request.run_id.to_string(), true)
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

/// The Controller's definite refusal of the job's Start grant. No grant was
/// issued, so the container never started: the job ends with that refusal as
/// its evidence. A wait or an unconfirmed answer is not a refusal.
pub(super) fn refused_job_start(
    error: &crate::host_runtime::HostRuntimeError,
) -> Option<ExecutionResult> {
    if refused_before_effect(error) {
        return Some(runtime_failure("job start refused before execution", error));
    }
    match error {
        crate::host_runtime::HostRuntimeError::Controller(error)
            if matches!(
                error.disposition(),
                Disposition::Fail | Disposition::Security
            ) =>
        {
            Some(recipe_build_client_failure_result(
                error,
                FailureStage::RuntimeAdapter,
                "job start was refused by the Controller",
            ))
        }
        _ => None,
    }
}

/// A local step's stage (the runtime's own, when its error names one) and its
/// bounded, secret-free cause.
pub(super) fn local_step(
    fallback: FailureStage,
    step: &str,
    error: &crate::oci::OciError,
) -> (FailureStage, String) {
    let (stage, category) = error.safe_start_context();
    let stage = if matches!(stage, FailureStage::Unknown) {
        fallback
    } else {
        stage
    };
    (stage, format!("{step}: {category}"))
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
        // The Controller stores the ending beside a receipt; the agent reports
        // it as the outcome's evidence.
        ending: None,
    };
    debug_assert!(result.validate().is_ok());
    result
}

mod endings;
use endings::*;
mod outputs;
#[cfg(test)]
pub(super) use outputs::*;
mod reobserve;

#[cfg(test)]
mod endings_tests;
#[cfg(test)]
mod output_collection_tests;
#[cfg(test)]
mod reobserve_tests;
#[cfg(test)]
mod tests;

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    /// Reopen measured exit/output custody, never the execution path. Each
    /// probe does one bounded collection; uploads require a fresh delivery-only
    /// order from the Controller, even if the old lease has expired.
    pub(super) fn observe_job_outputs(
        &self,
        claim: &AgentClaim,
        request: &vonk_agent_protocol::RecipeJobRunRequest,
        mut receipt: RecipeJobRunResult,
    ) -> super::operation_observation::OperationObservation {
        use super::operation_observation::{OperationObservation, PendingObservation};
        let unknown = |stage, cause: String, receipt: RecipeJobRunResult| {
            OperationObservation::confirmed(ExecutionResult::Unknown(crate::outcome::Unconfirmed {
                wait_reason: WaitReason::ReceiptMissing,
                reason: "measured job exit retained; output custody awaits recovery".to_owned(),
                evidence: UnknownEvidence::at(stage).because(cause),
                receipt: Some(receipt),
            }))
        };
        let root = match self.runtime.job_output_root(&request.job_id.to_string()) {
            Ok(root) => root,
            Err(error) => return unknown(FailureStage::OutputStorage, error.to_string(), receipt),
        };
        if receipt.output_manifest.files.is_empty() {
            receipt.output_manifest = match outputs::collect_job_outputs(
                Some(&root),
                &request.output_limits,
                &request.output_mappings,
            ) {
                Ok(manifest) => manifest,
                Err(outputs::OutputRefusal::Breach(breach)) => {
                    return OperationObservation::confirmed(breach.failure(&receipt));
                }
                Err(outputs::OutputRefusal::Unreadable { cause, .. }) => {
                    return OperationObservation::Unknown(PendingObservation::new(
                        claim,
                        WaitReason::ObservationUnavailable,
                        cause,
                    ));
                }
            };
        }
        if let Err(error) = self.runtime.persist_job_completion(&receipt) {
            return unknown(FailureStage::JobState, error.to_string(), receipt);
        }
        if !receipt.output_manifest.files.is_empty() {
            return unknown(
                FailureStage::JobOutputDelivery,
                "retained outputs require delivery-only authority".to_owned(),
                receipt,
            );
        }
        OperationObservation::confirmed(if receipt.exit_code == 0 {
            ExecutionResult::done(receipt)
        } else {
            job_failure(receipt, None)
        })
    }

    pub(super) fn observe_job_exit(
        &self,
        claim: &AgentClaim,
        request: &vonk_agent_protocol::RecipeJobRunRequest,
        outcome: HostRuntimeOutcome,
    ) -> super::operation_observation::OperationObservation {
        use super::operation_observation::OperationObservation;
        let Some(stop_plan) = job_run_stop_plan(claim, request) else {
            return OperationObservation::confirmed(failed("job observation identity is invalid"));
        };
        let scope = request.job_id.to_string();
        let job = DispatchedJob {
            claim,
            request,
            stop_plan: &stop_plan,
            job_scope: &scope,
            started: Instant::now(),
        };
        match self.job_exit_receipt(&job, outcome) {
            Ok(receipt) => self.observe_job_outputs(claim, request, receipt),
            Err(result) => OperationObservation::confirmed(*result),
        }
    }
}
