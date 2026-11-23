//! Docker owns job progress across helper and agent restarts. Re-reading a
//! timestamp never earns more time; only a later container log does.

use super::*;
use chrono::{DateTime, Utc};

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn job_progress_at(
        &self,
        job_id: uuid::Uuid,
    ) -> Option<DateTime<chrono::FixedOffset>> {
        let target = format!("vonk-{job_id}");
        let started = self
            .container_fields(&target, "{{.State.StartedAt}}")
            .ok()
            .filter(|output| output.success)?;
        let start =
            DateTime::parse_from_rfc3339(std::str::from_utf8(&started.stdout).ok()?.trim()).ok()?;
        let now = Utc::now();
        if start > now {
            return None;
        }
        let logs = self.run_docker_with_timeout(
            &[
                "logs".into(),
                "--timestamps".into(),
                "--tail".into(),
                "1".into(),
                target,
            ],
            Duration::from_secs(15),
        );
        // A failed read is no progress. The retained start still bounds a
        // silent job, rather than restarting its window after a daemon fault.
        let latest = logs.ok().filter(|output| output.success).map(|output| {
            [output.stdout, output.stderr]
                .iter()
                .flat_map(|stream| {
                    String::from_utf8_lossy(stream)
                        .lines()
                        .filter_map(|line| {
                            DateTime::parse_from_rfc3339(line.split_whitespace().next()?).ok()
                        })
                        .collect::<Vec<_>>()
                })
                .filter(|stamp| *stamp >= start && *stamp <= now)
                .max()
                .unwrap_or(start)
        });
        Some(latest.unwrap_or(start))
    }
}

#[cfg(test)]
mod tests;
