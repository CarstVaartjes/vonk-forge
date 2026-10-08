//! Inspection.

use super::*;

/// The one place a process-exit failure is built.  Whatever could not be read
/// is named in the failure, so it can never read as a container with nothing to
/// say: it carries its logs, or a typed reason they are missing, and an exit
/// summary.
pub(crate) fn process_exited(
    logs: Result<CommandOutput, String>,
    exit: Result<crate::runtime_logs::ContainerExit, &'static str>,
) -> OperationError {
    let (logs, capture_error) = match logs {
        // An unread log is reported as unread; it never becomes an empty tail
        // that reads like a container with nothing to say.
        Ok(logs) if logs.success => (
            Some(Box::new(crate::runtime_logs::retain_container(
                &logs.stdout,
                &logs.stderr,
            ))),
            None,
        ),
        Ok(_) => (None, Some("the container log command failed")),
        Err(_) => (None, Some("the container log command did not run")),
    };
    let exit_summary = crate::runtime_logs::exit_summary(
        exit.as_ref().map_err(|e| *e),
        logs.as_deref(),
        capture_error,
    );
    OperationError::RuntimeProcessExited {
        logs,
        capture_error,
        exit_summary,
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// Report whether the exact container of one agent-written `run-inspect`
    /// request is running.  Read-only, so it needs no Controller grant.
    pub fn inspect_recipe_run(&self, request_sha256: &str) -> Result<bool, OperationError> {
        self.require_directory(&self.roots.data)?;
        let request = self.read_runtime_request(request_sha256)?;
        if request.action != HostRuntimeAction::RunInspect
            || request.installation_id.is_some()
            || request.reconciliation_identity.is_some()
        {
            return Err(OperationError::InvalidOperation);
        }
        // A single argument is a run id alone: the Controller-side truth for a
        // run whose local metadata is unreadable, checked by container name.
        if let [run_id] = request.arguments.as_slice() {
            return self.runtime_run_container_running(run_id);
        }
        self.runtime_run_inspect(&request.arguments, false)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// Like `inspect_recipe_run`, and when asked also reads the running
    /// container's bounded output.  Read-only: nothing is stopped or removed.
    /// An exited container is reported as the process-exit rejection with its
    /// output, exactly as a privileged inspection reports it.
    pub fn inspect_recipe_run_with_logs(
        &self,
        request_sha256: &str,
        include_logs: bool,
    ) -> Result<RunInspection, OperationError> {
        if !include_logs {
            return self
                .inspect_recipe_run(request_sha256)
                .map(|running| RunInspection {
                    running,
                    logs: None,
                    log_error: None,
                });
        }
        self.require_directory(&self.roots.data)?;
        let request = self.read_runtime_request(request_sha256)?;
        if request.action != HostRuntimeAction::RunInspect
            || request.installation_id.is_some()
            || request.reconciliation_identity.is_some()
        {
            return Err(OperationError::InvalidOperation);
        }
        if let [run_id] = request.arguments.as_slice() {
            // Without the full run identity the container cannot be proven to
            // be this run's, so its output is never read.
            return Ok(RunInspection {
                running: self.runtime_run_container_running(run_id)?,
                logs: None,
                log_error: Some(
                    "the run identity was too short to authorise reading its output".into(),
                ),
            });
        }
        self.runtime_run_inspect_detail(&request.arguments, true, true)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// Whether the exact managed container `vonk-<run_id>` is running.
    /// Provably absent or stopped is `false`; a foreign container under the
    /// name or an unproven answer is an error, never a claim of absence.
    pub(super) fn runtime_run_container_running(
        &self,
        run_id: &str,
    ) -> Result<bool, OperationError> {
        if uuid::Uuid::parse_str(run_id)
            .map(|parsed| parsed.to_string() != run_id)
            .unwrap_or(true)
        {
            return Err(OperationError::InvalidOperation);
        }
        let name = format!("vonk-{run_id}");
        let existing = self.run_docker(&[
            "container".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{.Id}}\t{{.State.Running}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}".to_owned(),
            name.clone(),
        ])?;
        if !existing.success {
            return if self.prove_container_absent(&name, &existing)? {
                Ok(false)
            } else {
                Err(OperationError::CommandFailed)
            };
        }
        let fields = std::str::from_utf8(&existing.stdout)
            .ok()
            .map(str::trim)
            .map(|text| text.split('\t').collect::<Vec<_>>())
            .unwrap_or_default();
        let [container_id, running, managed, label_run_id] = fields.as_slice() else {
            return Err(OperationError::InvalidArtifact);
        };
        if !lower_hex(container_id, 64) || *managed != "true" || *label_run_id != run_id {
            return Err(OperationError::InvalidArtifact);
        }
        match *running {
            "true" => Ok(true),
            "false" => Ok(false),
            _ => Err(OperationError::InvalidArtifact),
        }
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_run_inspect(
        &self,
        arguments: &[String],
        capture_failure: bool,
    ) -> Result<bool, OperationError> {
        self.runtime_run_inspect_detail(arguments, capture_failure, false)
            .map(|inspection| inspection.running)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_run_inspect_detail(
        &self,
        arguments: &[String],
        capture_failure: bool,
        include_logs: bool,
    ) -> Result<RunInspection, OperationError> {
        let not_running = RunInspection {
            running: false,
            logs: None,
            log_error: None,
        };
        let [
            archive_sha256,
            registry_index_digest,
            platform_manifest_digest,
            image_reference,
            docker @ ..,
        ] = arguments
        else {
            return Err(OperationError::InvalidOperation);
        };
        let mut validated = validate_docker_run_with_archive(
            docker,
            &self.roots,
            self.runtime_request_owner_uid,
            Some(archive_sha256),
            Some(registry_index_digest),
        )?;
        if image_reference != &validated.local_image_reference
            || archive_sha256 != &validated.archive_sha256
            || registry_index_digest != &validated.registry_index_digest
            || platform_manifest_digest != &validated.platform_manifest_digest
            || !validated.detached
        {
            return Err(OperationError::InvalidOperation);
        }
        self.bind_native_fabric(&mut validated, Path::new(NATIVE_FABRIC_ROOT))?;
        // A running container is identified by its own labels, never by image
        // bookkeeping: a run started by an earlier agent keeps being observed
        // after receipts or image naming change. Start proved the image.
        let semantic_digest = hex_sha256(
            &canonical_json(&validated.arguments).map_err(|_| OperationError::InvalidOperation)?,
        );
        let existing = self.run_docker(&[
            "container".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{.Id}}\t{{.State.Running}}\t{{index .Config.Labels \"ai.vonkforge.runtime-request-sha256\"}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}".to_owned(),
            format!("vonk-{}", validated.run_id),
        ])?;
        if !existing.success {
            if capture_failure
                && self.prove_container_absent(&format!("vonk-{}", validated.run_id), &existing)?
            {
                return Err(OperationError::RuntimeRunMissing);
            }
            return Err(OperationError::CommandFailed);
        }
        let fields = std::str::from_utf8(&existing.stdout)
            .ok()
            .map(str::trim)
            .map(|text| text.split('\t').collect::<Vec<_>>())
            .unwrap_or_default();
        let [container_id, running, digest, managed, run_id] = fields.as_slice() else {
            return Ok(not_running);
        };
        if !lower_hex(container_id, 64) || *managed != "true" || *run_id != validated.run_id {
            return Ok(not_running);
        }
        if *digest != semantic_digest {
            if capture_failure {
                // A launch check is strict: this exact request started it.
                return Ok(not_running);
            }
            // Observation: this run's own container under its own name, launched
            // from a request an earlier agent rendered differently. It is still
            // this run; report what it is doing and say why it differs.
            eprintln!(
                "vonk-agent-helper: run {} container was launched from another request rendering; observing it by its run identity",
                validated.run_id
            );
        }
        if *running == "true" {
            if !include_logs {
                return Ok(RunInspection {
                    running: true,
                    logs: None,
                    log_error: None,
                });
            }
            // Read-only: the exact, identity-checked container's tail while it
            // runs. Nothing is stopped, so a workload that is merely slow to
            // become ready is left alone and only described.
            let (logs, log_error) = match self.runner.run_with_timeout(
                Path::new("/usr/bin/docker"),
                &[
                    "logs".into(),
                    "--tail".into(),
                    crate::runtime_logs::CAPTURE_LINES.into(),
                    (*container_id).into(),
                ],
                Duration::from_secs(30),
            ) {
                Ok(output) if output.success => (
                    Some(Box::new(crate::runtime_logs::retain_container(
                        &output.stdout,
                        &output.stderr,
                    ))),
                    None,
                ),
                Ok(_) => (None, Some("the container log command failed".into())),
                Err(_) => (None, Some("the container log command did not run".into())),
            };
            return Ok(RunInspection {
                running: true,
                logs,
                log_error,
            });
        }
        if *running == "false"
            && !capture_failure
            && let Some(evidence) =
                crate::host_memory_guard::load_evidence(&self.roots.data, container_id)
        {
            return Ok(RunInspection {
                running: false,
                logs: None,
                log_error: Some(crate::host_memory_guard::summary(&evidence)),
            });
        }
        if *running == "false" && capture_failure {
            // Read only the exact inspected container, never a reusable name.
            // Capture before the agent removes it; failed or foreign identity
            // checks above must never grant access to container logs.
            // Exit state and output of the same exact container id, read
            // before the agent removes it.
            let (logs, exit) = self.container_exit_evidence(container_id);
            return Err(process_exited(logs, exit));
        }
        Ok(not_running)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// The container's retained output and how it ended, read from `target`
    /// (an exact container id or name) before anything removes it.  A silent
    /// crash leaves no output at all; the exit code, the OOM flag and the
    /// runtime's own error are then the only evidence there will ever be.
    pub(super) fn container_exit_evidence(
        &self,
        target: &str,
    ) -> (
        Result<CommandOutput, String>,
        Result<crate::runtime_logs::ContainerExit, &'static str>,
    ) {
        let logs = self.runner.run_with_timeout(
            Path::new("/usr/bin/docker"),
            &[
                "logs".into(),
                "--tail".into(),
                crate::runtime_logs::CAPTURE_LINES.into(),
                target.into(),
            ],
            Duration::from_secs(30),
        );
        let exit = match self.runner.run_with_timeout(
            Path::new("/usr/bin/docker"),
            &[
                "container".into(),
                "inspect".into(),
                "--format".into(),
                crate::runtime_logs::EXIT_FORMAT.into(),
                target.into(),
            ],
            Duration::from_secs(30),
        ) {
            Ok(output) if output.success => crate::runtime_logs::parse_exit(&output.stdout)
                .ok_or("the container exit state was not readable"),
            Ok(_) => Err("the container exit inspection failed"),
            Err(_) => Err("the container exit inspection did not run"),
        };
        let exit = exit.map(|mut exit| {
            // Names can be reused by a new attempt; evidence is keyed to the
            // exact immutable container id, never to a run name.
            let id = if target.len() == 64 && target.bytes().all(|byte| byte.is_ascii_hexdigit()) {
                Some(target.to_owned())
            } else {
                self.runner
                    .run_with_timeout(
                        Path::new("/usr/bin/docker"),
                        &[
                            "inspect".into(),
                            "--format".into(),
                            "{{.Id}}".into(),
                            target.into(),
                        ],
                        Duration::from_secs(1),
                    )
                    .ok()
                    .filter(|output| output.success)
                    .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_owned())
            };
            exit.host_memory = id
                .as_deref()
                .and_then(|id| crate::host_memory_guard::load_evidence(&self.roots.data, id));
            exit
        });
        (logs, exit)
    }
}

pub(super) fn bounded_container_wait_exit_code(output: &CommandOutput) -> i32 {
    std::str::from_utf8(&output.stdout)
        .ok()
        .map(str::trim)
        .and_then(|value| value.parse::<i32>().ok())
        .filter(|code| (0..=255).contains(code))
        .unwrap_or(1)
}

#[cfg(test)]
mod tests;
