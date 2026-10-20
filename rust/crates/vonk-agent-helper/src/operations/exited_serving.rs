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
/// first, full ids only; a before cursor continues past protected entries.
const CLAIM_DIRECTORY: rustix::fs::OFlags = rustix::fs::OFlags::RDONLY
    .union(rustix::fs::OFlags::DIRECTORY)
    .union(rustix::fs::OFlags::NOFOLLOW)
    .union(rustix::fs::OFlags::CLOEXEC);

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
    /// Unreadable, with its cause: never taken as absence.
    Unknown(rustix::io::Errno),
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// One bounded pass over exited serving containers, now.
    pub fn sweep_exited_serving(&self) {
        self.sweep_exited_serving_at(chrono::Utc::now());
    }

    pub(super) fn sweep_exited_serving_at(&self, now: chrono::DateTime<chrono::Utc>) {
        // Unknown is not none: without the agent's claims nothing is removed.
        if let Err(error) = self.run_claims() {
            eprintln!(
                "vonk-agent-helper: exited serving sweep deferred: the agent's run claims are unreadable: {error}"
            );
            return;
        }
        let mut ids = match self.list_exited_managed(None) {
            Ok(ids) => ids,
            Err(error) => {
                eprintln!(
                    "vonk-agent-helper: exited serving sweep deferred: container listing failed: {error}"
                );
                return;
            }
        };
        loop {
            // Fetch before removal so Docker can still resolve the cursor id.
            let next_page = if ids.len() == MAX_LISTED {
                self.list_exited_managed(ids.last().map(String::as_str))
                    .map(Some)
            } else {
                Ok(None)
            };
            for id in ids {
                let Some(container) = self.read_exited_serving(&id) else {
                    continue;
                };
                if now - container.exited_at < GRACE {
                    continue;
                }
                match self.run_claim(container.run_id) {
                    Claim::Absent => {}
                    Claim::Held => continue,
                    Claim::Unknown(error) => {
                        eprintln!(
                            "vonk-agent-helper: exited serving container {} of run {} kept: its claim is unreadable: {error}",
                            container.id, container.run_id
                        );
                        continue;
                    }
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
            match next_page {
                Ok(Some(next)) => ids = next,
                Ok(None) => break,
                Err(error) => {
                    eprintln!("vonk-agent-helper: exited serving continuation deferred: {error}");
                    break;
                }
            }
        }
    }

    /// Full ids of exited managed containers, newest first, bounded.
    fn list_exited_managed(&self, before: Option<&str>) -> Result<Vec<String>, OperationError> {
        let mut arguments = [
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
        .map(str::to_owned)
        .to_vec();
        if let Some(before) = before {
            arguments.extend(["--filter".to_owned(), format!("before={before}")]);
        }
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
        let output = self.container_fields(id, SERVING_FORMAT);
        // Gone since the listing, or unreadable: the next pass reads it again.
        let output = output.ok().filter(|output| output.success)?;
        parse_exited_serving(id, std::str::from_utf8(&output.stdout).ok()?)
    }

    /// The agent's run claims directory. Nothing is followed.
    fn run_claims(&self) -> Result<std::os::fd::OwnedFd, rustix::io::Errno> {
        let root = rustix::fs::open(
            &self.roots.agent_data,
            CLAIM_DIRECTORY,
            rustix::fs::Mode::empty(),
        )?;
        rustix::fs::openat(
            &root,
            "run-metadata",
            CLAIM_DIRECTORY,
            rustix::fs::Mode::empty(),
        )
    }

    /// The agent's lifecycle claim on `run`. Nothing is followed.
    fn run_claim(&self, run: uuid::Uuid) -> Claim {
        let metadata = match self.run_claims().and_then(|claims| {
            rustix::fs::openat(
                &claims,
                run.to_string(),
                CLAIM_DIRECTORY,
                rustix::fs::Mode::empty(),
            )
        }) {
            Ok(metadata) => metadata,
            Err(rustix::io::Errno::NOENT) => return Claim::Absent,
            Err(error) => return Claim::Unknown(error),
        };
        match rustix::fs::statat(
            &metadata,
            "lifecycle.json",
            rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
        ) {
            Ok(_) => Claim::Held,
            Err(rustix::io::Errno::NOENT) => Claim::Absent,
            Err(error) => Claim::Unknown(error),
        }
    }

    /// Under the installation lock no Start or Stop of its runs proceeds: the
    /// claim is read again, the exit account and output journaled, then the
    /// exact container removed without force.
    fn remove_exited_serving(&self, container: &ExitedServing) -> Result<(), OperationError> {
        match self.run_claim(container.run_id) {
            Claim::Absent => {}
            Claim::Held => return Err(OperationError::StopUncertain),
            Claim::Unknown(error) => return Err(errno_io(error).into()),
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
