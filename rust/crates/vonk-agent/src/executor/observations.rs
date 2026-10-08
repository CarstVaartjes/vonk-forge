//! Observations.

use super::*;

/// True the first time this process reports on `key`; keeps a permanent
/// per-run condition from filling the journal every sweep.
pub(super) fn first_report_of_run(key: &str) -> bool {
    static SEEN: std::sync::Mutex<Option<std::collections::HashSet<String>>> =
        std::sync::Mutex::new(None);
    let mut seen = SEEN.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    seen.get_or_insert_with(Default::default)
        .insert(key.to_owned())
}

#[cfg(test)]
pub(super) async fn report_complete_recipe_run_observations(
    client: &AgentHttpClient,
    observed_at: DateTime<Utc>,
    results: Vec<Result<ExactRecipeRunObservation, RecipeObservationError>>,
) -> Result<usize, RecipeObservationError> {
    report_recipe_run_observation_page(client, observed_at, results, true).await
}

/// A collection failure is unknown evidence for its run, not for another
/// successfully inspected run. Nonempty partial reports preserve omitted ranks
/// on the Controller. Only a successfully collected empty set reports absence.
pub(super) async fn report_recipe_run_observation_page(
    client: &AgentHttpClient,
    observed_at: DateTime<Utc>,
    results: Vec<Result<ExactRecipeRunObservation, RecipeObservationError>>,
    allow_empty: bool,
) -> Result<usize, RecipeObservationError> {
    let mut observations = Vec::with_capacity(results.len());
    let mut failure = None;
    let mut skipped_run = false;
    for result in results {
        match result {
            Ok(observation) => observations.push(observation),
            Err(RecipeObservationError::SkippedRun) => skipped_run = true,
            // Not a failure of this sweep: the Controller named the run as
            // one it never owned, so there is nothing to report for it and
            // it must never hide the observations of owned runs.
            Err(RecipeObservationError::UnownedRun) => {}
            Err(error) => {
                failure.get_or_insert(error);
            }
        }
    }
    for batch in observations.chunks(MAX_RECIPE_RUN_OBSERVATIONS_PER_BATCH) {
        client
            .report_exact_recipe_run_observations(observed_at, batch)
            .await?;
    }
    if observations.is_empty() && allow_empty && failure.is_none() && !skipped_run {
        client
            .report_exact_recipe_run_observations(observed_at, &[])
            .await?;
    }
    match failure {
        Some(error) => Err(error),
        None => Ok(observations.len()),
    }
}

impl<R> RecipeExecutor<'_, R> {
    /// Ask the Controller about one local run; a failed lookup is logged once
    /// per process and never changes anything.
    pub(super) async fn run_disposition(&self, run_id: &str) -> Option<RecipeRunDisposition>
    where
        R: ProcessRunner,
    {
        let id = uuid::Uuid::parse_str(run_id).ok()?;
        match self.client.recipe_run_disposition(id).await {
            Ok(disposition) => Some(disposition),
            Err(lookup) => {
                if first_report_of_run(&format!("{run_id}/disposition")) {
                    eprintln!(
                        "vonk-agent: exact recipe run {run_id} disposition is unavailable ({lookup}); logged once per process"
                    );
                }
                None
            }
        }
    }
}

impl<R> RecipeExecutor<'_, R> {
    /// Retire the local lifecycle of a run the Controller has no record of.
    /// Only the lifecycle is removed; the run directory stays as history and
    /// no process is touched.
    pub(super) async fn retire_unowned_run(&self, run_id: &str, reason: &str) -> bool
    where
        R: ProcessRunner,
    {
        if self.run_disposition(run_id).await != Some(RecipeRunDisposition::Unowned) {
            return false;
        }
        self.retire_claim(run_id, reason)
    }
}

impl<R> RecipeExecutor<'_, R> {
    /// Drop the lifecycle claim of a run the Controller already named unowned.
    pub(super) fn retire_claim(&self, run_id: &str, reason: &str) -> bool
    where
        R: ProcessRunner,
    {
        match self.runtime.complete_stop(run_id) {
            Ok(()) => {
                eprintln!(
                    "vonk-agent: retired exact recipe run {run_id}: {reason} and unknown to the Controller"
                );
                true
            }
            Err(retire) => {
                eprintln!(
                    "vonk-agent: exact recipe run {run_id} is unknown to the Controller; retirement failed ({})",
                    retire.safe_category()
                );
                false
            }
        }
    }
}

impl<R> RecipeExecutor<'_, R> {
    /// Converge a retained run whose managed metadata cannot be read or
    /// parsed (an older agent's format, a retired field, a missing
    /// generation), using only the run id and the Controller's answer:
    ///
    /// - unowned: retire the local claim;
    /// - known: when the exact container is provably not running, report that
    ///   truth at the Controller's generation so it can recover or release
    ///   the run; otherwise keep the run and log once;
    /// - unreachable Controller: do nothing.
    pub(super) async fn observe_unreadable_run(
        &self,
        run_id: &str,
        error: &OciError,
    ) -> Result<ExactRecipeRunObservation, RecipeObservationError>
    where
        R: ProcessRunner,
    {
        let skip = |detail: &str| {
            if first_report_of_run(run_id) {
                eprintln!(
                    "vonk-agent: skipping exact recipe run {run_id}: invalid managed metadata ({}); {detail}; logged once per process",
                    error.safe_category()
                );
            }
            RecipeObservationError::SkippedRun
        };
        let Some(disposition) = self.run_disposition(run_id).await else {
            return Err(skip("the Controller is unreachable"));
        };
        match disposition {
            RecipeRunDisposition::Unowned => {
                if self.retire_claim(run_id, "unreadable managed metadata") {
                    Err(RecipeObservationError::UnownedRun)
                } else {
                    Err(skip("retirement failed"))
                }
            }
            RecipeRunDisposition::Known {
                run_generation: Some(run_generation),
            } => {
                let request_root = self.runtime_root.join("runtime-requests");
                let boundary = HostRuntimeBoundary {
                    client: self.client,
                    request_root: &request_root,
                    helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
                };
                match boundary
                    .inspect_recipe_run_evidence_for_observation(vec![run_id.to_owned()])
                    .await
                    .map(|report| report.running)
                {
                    Ok(false) => Ok(ExactRecipeRunObservation {
                        run_id: uuid::Uuid::parse_str(run_id)
                            .map_err(|_| RecipeObservationError::SkippedRun)?,
                        run_generation,
                        process_running: false,
                        endpoint_ready: Some(false),
                        failure_diagnostics: None,
                    }),
                    Ok(true) => Err(skip("the container is running")),
                    Err(_) => Err(skip("the container could not be inspected")),
                }
            }
            RecipeRunDisposition::Known {
                run_generation: None,
            } => Err(skip("the Controller does not have it running")),
        }
    }
}

impl<R> RecipeExecutor<'_, R> {
    #[cfg(test)]
    pub async fn report_exact_recipe_run_observations(
        &self,
    ) -> Result<usize, RecipeObservationError>
    where
        R: ProcessRunner,
    {
        Ok(self
            .report_recipe_run_observation_page(None)
            .await?
            .reported)
    }
}

impl<R> RecipeExecutor<'_, R> {
    pub async fn report_recipe_run_observation_page(
        &self,
        checkpoint: Option<&RecipeRunObservationCheckpoint>,
    ) -> Result<RecipeObservationSweep, RecipeObservationError>
    where
        R: ProcessRunner,
    {
        let mut page = self.runtime.recipe_run_inspection_page(checkpoint)?;
        let prepared: Vec<_> = page
            .plans
            .into_iter()
            .map(Ok)
            .chain(page.failures.into_iter().map(Err))
            .collect();
        let expected = prepared.len();
        // Inspection is read-only. A timed-out page retains unknown evidence
        // but releases the claim lane; durable traversal proceeds, and the next
        // complete cycle retries every omitted inspection.
        let results = stream::iter(prepared)
            .map(|prepared| async move {
                let plan = match prepared {
                    Ok(plan) => plan,
                    Err(failure) => {
                        return match failure.run_id {
                            Some(run_id) => {
                                self.observe_unreadable_run(&run_id, &failure.error).await
                            }
                            None => {
                                if first_report_of_run("invalid-observation-directory-entry") {
                                    eprintln!("vonk-agent: managed run scan has an invalid directory entry ({}); absence remains unknown", failure.error.safe_category());
                                }
                                Err(RecipeObservationError::SkippedRun)
                            },
                        };
                    }
                };
                let request_root = self.runtime_root.join("runtime-requests");
                let boundary = HostRuntimeBoundary {
                    client: self.client,
                    request_root: &request_root,
                    helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
                };
                let inspection = boundary
                    .inspect_recipe_run_evidence_for_observation(plan.arguments)
                    .await
                    .inspect_err(|error| {
                        eprintln!(
                            "vonk-agent: exact recipe run {} inspection failed: {}",
                            plan.run_id,
                            error.preflight_code()
                        );
                    })?;
                let process_running = inspection.running;
                let failure_diagnostics = inspection.log_error.as_deref()
                    .filter(|detail| !process_running && detail.split_whitespace().any(|token| token == "exit_cause=host_memory_exhausted"))
                    .map(|detail| {
                    crate::failure_evidence::from_failure(&AgentOperation::RecipeStart, &Failure::new(FailureCode::WorkloadHostMemoryExhausted.as_str()).diagnostic(detail))
                });
                // A stopped process of a run this Controller never owned (for
                // example after its database was rebuilt) is retired locally
                // instead of being reported forever.
                if !process_running
                    && self
                        .retire_unowned_run(&plan.run_id.to_string(), "not running")
                        .await
                {
                    return Err(RecipeObservationError::UnownedRun);
                }
                let endpoint_ready = match plan.endpoint_address {
                    Some(address) => Some(
                        process_running
                            && crate::health::readiness_once(
                                address,
                                plan.endpoint_port,
                                &plan.health_path,
                            )
                            .await,
                    ),
                    None => None,
                };
                Ok::<_, RecipeObservationError>(ExactRecipeRunObservation {
                    run_id: plan.run_id,
                    run_generation: plan.run_generation,
                    process_running,
                    endpoint_ready,
                    failure_diagnostics,
                })
            })
            .buffer_unordered(BACKGROUND_RUN_INSPECTION_CONCURRENCY)
            .take_until(tokio::time::sleep(Duration::from_secs(10)))
            .collect::<Vec<_>>()
            .await;
        if results.len() != expected {
            eprintln!(
                "vonk-agent: run observation page reached its 10s inspection budget; {} of {} inspections completed; omitted runs remain unknown and retry next cycle",
                results.len(),
                expected
            );
        }
        let had_unknown = results.len() != expected || results.iter().any(Result::is_err);
        if had_unknown {
            page.empty_snapshot_safe = false;
            if let Some(progress) = &mut page.checkpoint {
                progress.had_failures = true;
            }
        }
        let results = results
            .into_iter()
            .map(|result| match result {
                Err(RecipeObservationError::Inspection(error)) => {
                    eprintln!(
                        "vonk-agent: partial run observation: {}",
                        error.preflight_code()
                    );
                    Err(RecipeObservationError::SkippedRun)
                }
                other => other,
            })
            .collect();
        let reported = report_recipe_run_observation_page(
            self.client,
            page.observed_at,
            results,
            page.empty_snapshot_safe,
        )
        .await?;
        Ok(RecipeObservationSweep {
            reported,
            checkpoint: page.checkpoint,
            empty_snapshot_safe: page.empty_snapshot_safe,
        })
    }
}

impl<R> RecipeExecutor<'_, R> {
    /// Read the exact workload's bounded output without stopping it, for a
    /// start that is about to be failed for lack of readiness or stability.
    ///
    /// The tail must be read before the stop that ends the attempt removes the
    /// container.  The result always says what it knows: the output, or the
    /// typed reason there is none.
    pub(super) async fn running_workload_evidence(
        &self,
        arguments: &[String],
        why: &str,
    ) -> (Option<crate::failure_evidence::FailureProcessLogs>, String) {
        let request_root = self.runtime_root.join("runtime-requests");
        let boundary = HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: Path::new("/run/vonk-forge-package-helper/package-helper.sock"),
        };
        match boundary
            .inspect_recipe_run_report(arguments.to_vec(), true)
            .await
        {
            Ok(report) => {
                let mut account = format!("{why} container_running={}", report.running);
                if report.process_logs.is_none() {
                    let reason = report
                        .log_error
                        .as_deref()
                        .unwrap_or("the helper returned no output");
                    account.push_str(&format!(
                        " logs_unavailable={:?}",
                        reason.chars().take(160).collect::<String>()
                    ));
                }
                (report.process_logs.map(|logs| *logs), account)
            }
            // An exited container answers with the process-exit rejection and
            // its output and exit account.
            Err(error) => {
                let mut account = format!("{why} container_running=false");
                if let Some(detail) = error.diagnostic() {
                    account.push(' ');
                    account.push_str(&detail.chars().take(320).collect::<String>());
                }
                if error.process_logs().is_none() {
                    account.push_str(&format!(
                        " logs_unavailable=\"inspection failed: {}\"",
                        error.preflight_code()
                    ));
                }
                (error.process_logs().cloned(), account)
            }
        }
    }
}

#[cfg(test)]
mod tests;
