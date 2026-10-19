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
mod tests {
    use super::*;
    use crate::operations::test_support::*;

    struct ProgressHost {
        host: Host,
        log_at: String,
    }

    impl CommandRunner for ProgressHost {
        fn run(&self, executable: &Path, args: &[String]) -> Result<CommandOutput, String> {
            if args.iter().any(|arg| arg == "{{.State.StartedAt}}") {
                return Ok(docker_output(
                    true,
                    &(Utc::now() - chrono::Duration::hours(2)).to_rfc3339(),
                    0,
                ));
            }
            if args.first().is_some_and(|arg| arg == "logs")
                && args.iter().any(|arg| arg == "--timestamps")
            {
                return Ok(docker_output(
                    true,
                    &format!("{} denoising advanced\n", self.log_at),
                    0,
                ));
            }
            self.host.run(executable, args)
        }
    }

    #[test]
    fn a_working_job_outlives_its_original_deadline_but_stale_logs_do_not_renew_it() {
        const RUN: &str = "a0000000-0000-4000-8000-00000000000a";
        const JOB: &str = "a1000000-0000-4000-8000-0000000000a1";
        for progressing in [false, true] {
            let host = ProgressHost {
                host: Host::with(&[Container {
                    exit: 0,
                    ..job(RUN, JOB, "running")
                }]),
                log_at: (Utc::now() - chrono::Duration::seconds(if progressing { 1 } else { 120 }))
                    .to_rfc3339(),
            };
            let temp = tempfile::tempdir().unwrap();
            let executor =
                OperationExecutor::new(ManagedRoots::under(temp.path()), &[0; 32], host, None)
                    .unwrap();
            let identity = RuntimeEffectIdentity {
                runtime_id: JOB.parse().unwrap(),
                installation_id: INSTALLATION.parse().unwrap(),
                run_generation: 1,
            };
            // A helper restart adopts the original Docker timestamp. Its
            // own elapsed lifetime cannot restart a stalled job's allowance.
            let result = executor
                .runtime_wait_for_job(
                    identity,
                    RUN.parse().unwrap(),
                    PLAN_DIGEST,
                    60,
                    Instant::now() - Duration::from_secs(120),
                )
                .unwrap();
            assert_eq!(result.exit_code, Some(if progressing { 0 } else { 124 }));
        }
    }
}
