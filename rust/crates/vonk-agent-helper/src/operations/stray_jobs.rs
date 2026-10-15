//! Stray job containers.
//!
//! A one-shot job container exists only while a helper Start owns its wait:
//! that Start reports its exit and removes it. A job container without that
//! owner -- left by a helper or host restart -- has no claim behind it, so it
//! must not run on. It is stopped and removed, its exit account and output
//! journaled first. A serving container (target id equal to its run id) is
//! never selected here.

use super::*;

/// One listing names at most this many managed containers, newest first. It
/// lists full ids only (64 hex digits and a newline), so it fits the helper's
/// captured-output bound however many containers the host holds; each id's
/// labels are then read on their own. Older containers come into view as these
/// are removed, at the next helper start or before their run's next job.
const MAX_LISTED: usize = (MAX_COMMAND_OUTPUT_BYTES / 65) as usize;

const JOB_CONTAINER_FORMAT: &str = "{{.Names}}\t{{.State}}\t{{.Label \"ai.vonkforge.run-id\"}}\t{{.Label \"ai.vonkforge.target-id\"}}\t{{.Label \"ai.vonkforge.installation-id\"}}\t{{.Label \"ai.vonkforge.run-generation\"}}\t{{.Label \"ai.vonkforge.plan-digest\"}}";

/// Why the managed container listing was not read. It is journaled as the
/// sweep's evidence and is never taken as proof that no job container remains.
#[derive(Debug)]
pub(super) enum ListingFailure {
    /// The runner refused or ended the command, with its own reason (start,
    /// deadline, output bound).
    Command(String),
    /// Docker answered with a failure status.
    Exit(Option<i32>),
}

impl std::fmt::Display for ListingFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Command(reason) => write!(formatter, "container listing failed: {reason}"),
            Self::Exit(Some(code)) => write!(formatter, "container listing exited {code}"),
            Self::Exit(None) => write!(formatter, "container listing ended by signal"),
        }
    }
}

impl From<ListingFailure> for OperationError {
    fn from(_: ListingFailure) -> Self {
        Self::CommandFailed
    }
}

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
    ) -> Result<(Vec<Option<JobContainer>>, bool), ListingFailure> {
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
            .map_err(ListingFailure::Command)?;
        // An unreadable listing is never proof that no job container remains.
        if !listing.success {
            return Err(ListingFailure::Exit(listing.exit_code));
        }
        let text = String::from_utf8_lossy(&listing.stdout);
        let ids: Vec<_> = text
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty())
            .collect();
        let full = ids.len() >= MAX_LISTED;
        let jobs = ids
            .into_iter()
            .filter_map(|id| self.read_job_container(id))
            .collect();
        Ok((jobs, full))
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
        }
        self.runtime_stop_once(job.identity, job.run_id, &job.plan_digest, 30)
    }

    /// At helper start no Start owns any job container, so every one found is
    /// a stray. One bounded pass; what it cannot remove is journaled and is
    /// removed before its run's next job starts.
    pub fn sweep_stray_jobs(&self) {
        let (jobs, truncated) = match self.list_job_containers(None) {
            Ok(listing) => listing,
            Err(error) => {
                eprintln!("vonk-agent-helper: stray job sweep deferred: {error}");
                return;
            }
        };
        if truncated {
            eprintln!(
                "vonk-agent-helper: stray job sweep saw the newest {MAX_LISTED} managed containers; older ones are reached as these are removed"
            );
        }
        for job in jobs.into_iter().flatten() {
            let outcome = self
                .lock_installation_runtime(&job.identity.installation_id.to_string())
                .and_then(|_lock| self.remove_stray_job(&job));
            journal_stray(&job, &outcome);
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
        let (jobs, truncated) = self.list_job_containers(Some(run_id))?;
        if truncated {
            return Err(OperationError::StopUncertain);
        }
        for job in jobs {
            let job = job.ok_or(OperationError::StopUncertain)?;
            if job.identity.runtime_id == identity.runtime_id {
                continue;
            }
            // Only the held installation lock may guard a removal.
            let outcome = if job.identity.installation_id == identity.installation_id {
                self.remove_stray_job(&job)
            } else {
                Err(OperationError::StopUncertain)
            };
            journal_stray(&job, &outcome);
            outcome?;
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
