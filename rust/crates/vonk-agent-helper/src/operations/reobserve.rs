//! Re-observation by identity.
//!
//! A command whose answer the helper could not read (a launch that printed no
//! id, a wait that answered nothing usable, a deadline) has not told it what
//! the container did. Before it reports anything, the helper reads the
//! container again by its own identity -- name `vonk-<target>` and the labels
//! its Start set -- and acts on what is there: a running container is adopted,
//! an exited one reports its own exit, absence is proven by an empty listing,
//! and anything else stays unknown with its cause.

use super::*;

/// One listed container: whether its process is live (running, paused or
/// restarting), whether it ended (exited or dead), its full id and identity
/// labels. A container created but never started, or being removed, is
/// neither.
pub(super) const REOBSERVE_FORMAT: &str = "{{eq .State \"running\" \"paused\" \"restarting\"}}\t{{eq .State \"exited\" \"dead\"}}\t{{.ID}}\t{{.Label \"ai.vonkforge.managed\"}}\t{{.Label \"ai.vonkforge.run-id\"}}\t{{.Label \"ai.vonkforge.target-id\"}}\t{{.Label \"ai.vonkforge.installation-id\"}}\t{{.Label \"ai.vonkforge.run-generation\"}}\t{{.Label \"ai.vonkforge.plan-digest\"}}";

/// What one read of a runtime container by its identity found.
#[derive(Debug)]
pub(super) enum Reobserved {
    /// Its process is live.
    Running,
    /// Its process exited with this status, as Docker recorded it.
    Exited { code: i32 },
    /// A successful listing holds no container under its name.
    Absent,
    /// Unreadable, foreign under the name, or neither live nor ended; the
    /// cause is kept.
    Unknown(CommandFailure),
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// Read the exact container of `identity` again. Read-only.
    pub(super) fn reobserve_by_identity(
        &self,
        identity: RuntimeEffectIdentity,
        run_id: uuid::Uuid,
        plan_digest: &str,
    ) -> Reobserved {
        let listing = match self.run_docker_with_timeout(
            &[
                "container".to_owned(),
                "ls".to_owned(),
                "--all".to_owned(),
                "--no-trunc".to_owned(),
                "--filter".to_owned(),
                format!("name=^/vonk-{}$", identity.runtime_id),
                "--format".to_owned(),
                REOBSERVE_FORMAT.to_owned(),
            ],
            Duration::from_secs(15),
        ) {
            Ok(listing) if listing.success => listing,
            Ok(listing) => return Reobserved::Unknown(CommandFailure::exit(&listing)),
            Err(OperationError::CommandFailed { cause }) => return Reobserved::Unknown(cause),
            Err(error) => return Reobserved::Unknown(CommandFailure::Runner(error.to_string())),
        };
        let Ok(text) = std::str::from_utf8(&listing.stdout) else {
            return Reobserved::Unknown(CommandFailure::UnexpectedAnswer);
        };
        let lines: Vec<_> = text
            .lines()
            .filter(|line| !line.trim().is_empty())
            .collect();
        let [line] = lines.as_slice() else {
            return if lines.is_empty() {
                Reobserved::Absent
            } else {
                Reobserved::Unknown(CommandFailure::UnexpectedAnswer)
            };
        };
        let expected = format!(
            "true\t{run_id}\t{}\t{}\t{}\t{plan_digest}",
            identity.runtime_id, identity.installation_id, identity.run_generation
        );
        let mut fields = line.trim().splitn(4, '\t');
        match (fields.next(), fields.next(), fields.next(), fields.next()) {
            (Some(live), Some(ended), Some(id), Some(labels))
                if labels == expected && lower_hex(id, 64) =>
            {
                match (live, ended) {
                    ("true", "false") => Reobserved::Running,
                    ("false", "true") => match self.container_exit(id) {
                        Ok(exit) => exit
                            .exit_code
                            .filter(|code| (0..=255).contains(code))
                            .map_or(
                                Reobserved::Unknown(CommandFailure::UnexpectedAnswer),
                                |code| Reobserved::Exited { code },
                            ),
                        Err(reason) => {
                            Reobserved::Unknown(CommandFailure::Runner(reason.to_owned()))
                        }
                    },
                    _ => Reobserved::Unknown(CommandFailure::UnexpectedAnswer),
                }
            }
            _ => Reobserved::Unknown(CommandFailure::UnexpectedAnswer),
        }
    }
}

#[cfg(test)]
mod tests;
