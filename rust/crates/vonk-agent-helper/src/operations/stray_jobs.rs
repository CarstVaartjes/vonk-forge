//! Stray job containers.
//!
//! A one-shot job container is owned by a helper Start's wait: that Start
//! reports its exit and removes it. A helper restart ends the wait, never the
//! job: a running job container is left for its owner -- a Start of the same
//! identity adopts it, an exact Stop ends it. One that has exited with nobody
//! left to account for it is removed, its exit account and output journaled
//! first. A serving container (target id equal to its run id) is never
//! selected here.

use super::*;

/// One listing names at most this many managed containers, newest first. It
/// lists full ids only (64 hex digits and a newline), so it fits the helper's
/// captured-output bound however many containers the host holds; each id's
/// labels are then read on their own. A before cursor continues to older
/// containers, even when the first page is protected.
const MAX_LISTED: usize = (MAX_COMMAND_OUTPUT_BYTES / 65) as usize;

const JOB_CONTAINER_FORMAT: &str = "{{.Names}}\t{{.State}}\t{{.Label \"ai.vonkforge.run-id\"}}\t{{.Label \"ai.vonkforge.target-id\"}}\t{{.Label \"ai.vonkforge.installation-id\"}}\t{{.Label \"ai.vonkforge.run-generation\"}}\t{{.Label \"ai.vonkforge.plan-digest\"}}";

/// One managed job container, identified exactly by the labels its Start set.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct JobContainer {
    identity: RuntimeEffectIdentity,
    run_id: uuid::Uuid,
    plan_digest: String,
    state: String,
}

/// A managed container line: serving is `None`; a line whose identity cannot
/// be proven is `Some(None)` and is never acted on.
fn parse_job_container(line: &str) -> Option<Option<JobContainer>> {
    let uuid = |value: &str| {
        uuid::Uuid::parse_str(value)
            .ok()
            .filter(|parsed| parsed.to_string() == value)
    };
    let fields: Vec<_> = line.trim().split('\t').collect();
    let [name, state, run, target, installation, generation, digest] = fields.as_slice() else {
        return Some(None);
    };
    if run == target {
        return None;
    }
    if *name != format!("vonk-{target}") || !lower_hex(digest, 64) {
        return Some(None);
    }
    let job = (|| {
        Some(JobContainer {
            identity: RuntimeEffectIdentity {
                runtime_id: uuid(target)?,
                installation_id: uuid(installation)?,
                run_generation: generation
                    .parse::<u64>()
                    .ok()
                    .filter(|value| (1..=i64::MAX as u64).contains(value))?,
            },
            run_id: uuid(run)?,
            plan_digest: (*digest).to_owned(),
            state: (*state).to_owned(),
        })
    })();
    Some(job)
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// Managed job containers, of one run when `run_id` is given, and whether
    /// the listing was full. Serving containers are left out; `None` marks one
    /// whose identity is unproven.
    fn list_job_containers(
        &self,
        run_id: Option<uuid::Uuid>,
        before: Option<&str>,
    ) -> Result<(Vec<Option<JobContainer>>, Option<String>), CommandFailure> {
        let mut arguments = vec![
            "container".to_owned(),
            "ls".to_owned(),
            "--all".to_owned(),
            "--quiet".to_owned(),
            "--no-trunc".to_owned(),
            "--last".to_owned(),
            MAX_LISTED.to_string(),
            "--filter".to_owned(),
            "label=ai.vonkforge.managed=true".to_owned(),
        ];
        if let Some(before) = before {
            arguments.extend(["--filter".to_owned(), format!("before={before}")]);
        }
        if let Some(run_id) = run_id {
            arguments.extend([
                "--filter".to_owned(),
                format!("label=ai.vonkforge.run-id={run_id}"),
            ]);
        }
        let listing = self
            .runner
            .run_with_timeout(
                Path::new("/usr/bin/docker"),
                &arguments,
                Duration::from_secs(15),
            )
            .map_err(CommandFailure::Runner)?;
        // An unreadable listing is never proof that no job container remains.
        if !listing.success {
            return Err(CommandFailure::exit(&listing));
        }
        let text = String::from_utf8_lossy(&listing.stdout);
        let ids: Vec<_> = text
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty())
            .collect();
        let next_cursor = (ids.len() >= MAX_LISTED)
            .then(|| ids.last().map(|id| (*id).to_owned()))
            .flatten();
        let jobs = ids
            .into_iter()
            .filter_map(|id| self.read_job_container(id))
            .collect();
        Ok((jobs, next_cursor))
    }

    /// Fetch the next page while its before cursor still exists, then clean
    /// this page. Removal cannot invalidate the cursor and protected entries
    /// cannot starve older containers. Each command retains its byte bound.
    fn visit_job_containers(
        &self,
        run_id: Option<uuid::Uuid>,
        mut visit: impl FnMut(Vec<Option<JobContainer>>),
    ) -> Result<(), CommandFailure> {
        let mut page = self.list_job_containers(run_id, None)?;
        loop {
            let next_page = page
                .1
                .as_deref()
                .map(|cursor| self.list_job_containers(run_id, Some(cursor)))
                .transpose();
            visit(page.0);
            match next_page? {
                Some(next) => page = next,
                None => return Ok(()),
            }
        }
    }

    /// The labels of one listed container: serving is `None`; one that is
    /// unreadable, gone or not exactly labelled is `Some(None)`.
    fn read_job_container(&self, id: &str) -> Option<Option<JobContainer>> {
        if !lower_hex(id, 64) {
            return Some(None);
        }
        let arguments = [
            "container".to_owned(),
            "ls".to_owned(),
            "--all".to_owned(),
            "--no-trunc".to_owned(),
            "--filter".to_owned(),
            format!("id={id}"),
            "--format".to_owned(),
            JOB_CONTAINER_FORMAT.to_owned(),
        ];
        match self.run_docker_with_timeout(&arguments, Duration::from_secs(15)) {
            Ok(output) if output.success => {
                std::str::from_utf8(&output.stdout).map_or(Some(None), parse_job_container)
            }
            _ => Some(None),
        }
    }

    /// Stop and remove one job container no Start of this helper owns. Its
    /// exit account and output are read before removal, so they are kept.
    fn remove_stray_job(&self, job: &JobContainer) -> Result<(), OperationError> {
        if self.job_cancellation.is_active(job.identity)? {
            // A live Start owns this job; it ends by its own exit, timeout or
            // an exact Stop, never by a sweep.
            return Err(OperationError::StopUncertain);
        }
        let name = format!("vonk-{}", job.identity.runtime_id);
        let (logs, exit) = self.container_exit_evidence(&name);
        // The exit of an ended process, kept for its owner's re-observation.
        let code = exit
            .as_ref()
            .ok()
            .and_then(|exit| exit.exit_code)
            .filter(|_| matches!(job.state.as_str(), "exited" | "dead"));
        let completion_recorded = fs::symlink_metadata(
            self.roots
                .agent_data
                .join("runs")
                .join(job.identity.runtime_id.to_string())
                .join("job-exit.json"),
        )
        .is_ok_and(|metadata| metadata.is_file());
        if let OperationError::RuntimeProcessExited {
            logs, exit_summary, ..
        } = process_exited(logs, exit)
        {
            let tail = |stream: Option<&str>| stream.unwrap_or("").escape_debug().to_string();
            eprintln!(
                "vonk-agent-helper: stray job {} of run {} ({}) completion_recorded={completion_recorded} {exit_summary} stdout=\"{}\" stderr=\"{}\"",
                job.identity.runtime_id,
                job.run_id,
                job.state.escape_debug(),
                tail(logs.as_deref().map(|logs| logs.stdout.text.as_str())),
                tail(logs.as_deref().map(|logs| logs.stderr.text.as_str())),
            );
            if let Some(code) = code {
                self.retain_job_exit(
                    job.identity,
                    job.run_id,
                    &job.plan_digest,
                    code,
                    Some(&JobEvidence {
                        logs,
                        summary: exit_summary,
                    }),
                );
            }
        }
        self.runtime_stop_once(job.identity, job.run_id, 30)
    }

    /// At helper start no Start owns any job container. A running one is a
    /// workload that survives the restart and is left for its owner; one whose
    /// process ended is a stray. Byte-bounded pages continue to exhaustion;
    /// removals that fail are journaled and retried before the run's next job.
    pub fn sweep_stray_jobs(&self) {
        let outcome = self.visit_job_containers(None, |jobs| {
            for job in jobs.into_iter().flatten() {
                match self.reobserve_by_identity(job.identity, job.run_id, &job.plan_digest) {
                    Reobserved::Exited { code } => eprintln!(
                        "vonk-agent-helper: job {} of run {} exited {code} with no owner left; removing it",
                        job.identity.runtime_id, job.run_id
                    ),
                    Reobserved::Absent => continue,
                    Reobserved::Running => {
                        eprintln!(
                            "vonk-agent-helper: job {} of run {} is running; left for its owner",
                            job.identity.runtime_id, job.run_id
                        );
                        continue;
                    }
                    Reobserved::Unknown(cause) => {
                        eprintln!(
                            "vonk-agent-helper: job {} of run {} left for its owner: its state is unobserved: {cause}",
                            job.identity.runtime_id, job.run_id
                        );
                        continue;
                    }
                }
                let outcome = self
                    .lock_installation_runtime(&job.identity.installation_id.to_string())
                    .and_then(|_lock| self.remove_stray_job(&job));
                journal_stray(&job, &outcome);
            }
        });
        if let Err(error) = outcome {
            eprintln!("vonk-agent-helper: stray job sweep deferred: {error}");
        }
    }

    /// No two job containers of one run share the host: before a run's next
    /// job starts, an earlier one without an owning Start is removed, and one
    /// still owned refuses this Start. Called under `identity`'s installation
    /// lock.
    pub(super) fn clear_stray_jobs_for_run(
        &self,
        identity: RuntimeEffectIdentity,
        run_id: uuid::Uuid,
    ) -> Result<(), OperationError> {
        let mut failure = None;
        self.visit_job_containers(Some(run_id), |jobs| {
            for job in jobs {
                let outcome = match job {
                    Some(job) if job.identity.runtime_id == identity.runtime_id => continue,
                    Some(job) if job.identity.installation_id == identity.installation_id => {
                        let outcome = self.remove_stray_job(&job);
                        journal_stray(&job, &outcome);
                        outcome
                    }
                    _ => Err(OperationError::StopUncertain),
                };
                if failure.is_none() {
                    failure = outcome.err();
                }
            }
        })?;
        if let Some(error) = failure {
            return Err(error);
        }
        Ok(())
    }
}

fn journal_stray(job: &JobContainer, outcome: &Result<(), OperationError>) {
    match outcome {
        Ok(()) => eprintln!(
            "vonk-agent-helper: stray job {} of run {} stopped and removed",
            job.identity.runtime_id, job.run_id
        ),
        Err(error) => eprintln!(
            "vonk-agent-helper: stray job {} of run {} remains: {error}; its run's next job waits for its removal",
            job.identity.runtime_id, job.run_id
        ),
    }
}

#[cfg(test)]
mod tests;
