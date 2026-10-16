//! Exited serving containers.
//!
//! A serving container (target id equal to its run id) is removed by its
//! run's exact Stop. A serving process that dies instead leaves an exited
//! container that no Stop will reach: the Controller names the ended run
//! `unowned` and the agent retires its lifecycle claim
//! (`run-metadata/<run>/lifecycle.json`) without touching the container. The
//! claim's absence is therefore the Controller's verdict that the run has
//! ended, as the agent recorded it; a live run (planned, starting, running,
//! stopping, lost) keeps its claim.
//!
//! An exited serving container whose run holds no claim and that exited more
//! than a grace ago is removed, its exit account and bounded output journaled
//! first. Docker's own `rm` (never forced) refuses a running container, and
//! the removal names the exact container id, so a newer container under the
//! same name is never touched.

use super::*;

/// One pass is due at most this often, on a helper request.
pub const EXITED_SERVING_SWEEP_INTERVAL: Duration = Duration::from_secs(60 * 60);

/// An exited container stays this long after its process ended, for anyone
/// who wants to read it with Docker's own tools.
const GRACE: chrono::TimeDelta = chrono::TimeDelta::hours(24);

/// One listing names at most this many exited managed containers, newest
/// first, full ids only; older ones come into view as these are removed.
const MAX_LISTED: usize = (MAX_COMMAND_OUTPUT_BYTES / 65) as usize;

pub(super) const SERVING_FORMAT: &str = "{{.State.Status}}\t{{.State.FinishedAt}}\t{{.Name}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}\t{{index .Config.Labels \"ai.vonkforge.target-id\"}}\t{{index .Config.Labels \"ai.vonkforge.installation-id\"}}";

/// One exited managed serving container, identified by its exact id and the
/// labels its Start set.
#[derive(Debug, Clone, PartialEq, Eq)]
struct ExitedServing {
    id: String,
    run_id: uuid::Uuid,
    installation_id: uuid::Uuid,
    exited_at: chrono::DateTime<chrono::Utc>,
}

/// An exited serving container, or `None` for anything else (running, a job,
/// unreadable or not exactly labelled).
fn parse_exited_serving(id: &str, line: &str) -> Option<ExitedServing> {
    let uuid = |value: &str| {
        uuid::Uuid::parse_str(value)
            .ok()
            .filter(|parsed| parsed.to_string() == value)
    };
    let fields: Vec<_> = line.trim().split('\t').collect();
    let [state, finished, name, managed, run, target, installation] = fields.as_slice() else {
        return None;
    };
    if !matches!(*state, "exited" | "dead")
        || *managed != "true"
        || run != target
        || *name != format!("/vonk-{target}")
    {
        return None;
    }
    Some(ExitedServing {
        id: id.to_owned(),
        run_id: uuid(run)?,
        installation_id: uuid(installation)?,
        exited_at: chrono::DateTime::parse_from_rfc3339(finished)
            .ok()?
            .with_timezone(&chrono::Utc),
    })
}

/// Whether the agent still holds a lifecycle claim on a run.
#[derive(Debug, PartialEq, Eq)]
enum Claim {
    Held,
    Absent,
    /// Unreadable: never taken as absence.
    Unknown,
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// One bounded pass over exited serving containers, now.
    pub fn sweep_exited_serving(&self) {
        self.sweep_exited_serving_at(chrono::Utc::now());
    }

    pub(super) fn sweep_exited_serving_at(&self, now: chrono::DateTime<chrono::Utc>) {
        // Unknown is not none: without the agent's claims nothing is removed.
        if self.run_claim(None) != Claim::Absent {
            eprintln!(
                "vonk-agent-helper: exited serving sweep deferred: the agent's run claims are unreadable"
            );
            return;
        }
        let ids = match self.list_exited_managed() {
            Ok(ids) => ids,
            Err(error) => {
                eprintln!(
                    "vonk-agent-helper: exited serving sweep deferred: container listing failed: {error}"
                );
                return;
            }
        };
        for id in ids {
            let Some(container) = self.read_exited_serving(&id) else {
                continue;
            };
            if now - container.exited_at < GRACE
                || self.run_claim(Some(container.run_id)) != Claim::Absent
            {
                continue;
            }
            let outcome = self
                .lock_installation_runtime(&container.installation_id.to_string())
                .and_then(|_lock| self.remove_exited_serving(&container));
            match outcome {
                Ok(()) => eprintln!(
                    "vonk-agent-helper: exited serving container {} of ended run {} removed",
                    container.id, container.run_id
                ),
                Err(error) => eprintln!(
                    "vonk-agent-helper: exited serving container {} of ended run {} remains: {error}; retried next pass",
                    container.id, container.run_id
                ),
            }
        }
    }

    /// Full ids of exited managed containers, newest first, bounded.
    fn list_exited_managed(&self) -> Result<Vec<String>, OperationError> {
        let arguments = [
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--last",
            &MAX_LISTED.to_string(),
            "--filter",
            "label=ai.vonkforge.managed=true",
            "--filter",
            "status=exited",
            "--filter",
            "status=dead",
        ]
        .map(str::to_owned);
        let listing = self.run_docker_with_timeout(&arguments, Duration::from_secs(15))?;
        if !listing.success {
            return Err(CommandFailure::exit(&listing).into());
        }
        Ok(String::from_utf8_lossy(&listing.stdout)
            .lines()
            .map(str::trim)
            .filter(|id| lower_hex(id, 64))
            .map(str::to_owned)
            .collect())
    }

    fn read_exited_serving(&self, id: &str) -> Option<ExitedServing> {
        let arguments = ["container", "inspect", "--format", SERVING_FORMAT, id].map(str::to_owned);
        match self.run_docker_with_timeout(&arguments, Duration::from_secs(15)) {
            Ok(output) if output.success => std::str::from_utf8(&output.stdout)
                .ok()
                .and_then(|line| parse_exited_serving(id, line)),
            _ => None,
        }
    }

    /// The agent's claim on `run`, or with `None` whether its claims can be
    /// read at all (`Absent` then means readable). Nothing is followed.
    fn run_claim(&self, run: Option<uuid::Uuid>) -> Claim {
        let directory = rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC;
        let Ok(root) =
            rustix::fs::open(&self.roots.agent_data, directory, rustix::fs::Mode::empty())
        else {
            return Claim::Unknown;
        };
        let Ok(claims) =
            rustix::fs::openat(&root, "run-metadata", directory, rustix::fs::Mode::empty())
        else {
            return Claim::Unknown;
        };
        let Some(run) = run else {
            return Claim::Absent;
        };
        let metadata = match rustix::fs::openat(
            &claims,
            run.to_string(),
            directory,
            rustix::fs::Mode::empty(),
        ) {
            Ok(metadata) => metadata,
            Err(rustix::io::Errno::NOENT) => return Claim::Absent,
            Err(_) => return Claim::Unknown,
        };
        match rustix::fs::statat(
            &metadata,
            "lifecycle.json",
            rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
        ) {
            Ok(_) => Claim::Held,
            Err(rustix::io::Errno::NOENT) => Claim::Absent,
            Err(_) => Claim::Unknown,
        }
    }

    /// Under the installation lock no Start or Stop of its runs proceeds: the
    /// claim is read again, the exit account and output journaled, then the
    /// exact container removed without force.
    fn remove_exited_serving(&self, container: &ExitedServing) -> Result<(), OperationError> {
        if self.run_claim(Some(container.run_id)) != Claim::Absent {
            return Err(OperationError::StopUncertain);
        }
        let (logs, exit) = self.container_exit_evidence(&container.id);
        if let OperationError::RuntimeProcessExited {
            logs, exit_summary, ..
        } = process_exited(logs, exit)
        {
            let tail = |stream: Option<&str>| stream.unwrap_or("").escape_debug().to_string();
            eprintln!(
                "vonk-agent-helper: exited serving container {} of run {} exited at {} {exit_summary} stdout=\"{}\" stderr=\"{}\"",
                container.id,
                container.run_id,
                container.exited_at.to_rfc3339(),
                tail(logs.as_deref().map(|logs| logs.stdout.text.as_str())),
                tail(logs.as_deref().map(|logs| logs.stderr.text.as_str())),
            );
        }
        let removed = self.run_docker_with_timeout(
            &["rm".to_owned(), container.id.clone()],
            Duration::from_secs(15),
        )?;
        if !removed.success {
            return Err(CommandFailure::exit(&removed).into());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests;
