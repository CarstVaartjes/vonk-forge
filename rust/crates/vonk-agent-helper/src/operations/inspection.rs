//! Inspection.

use super::*;

#[derive(Clone, Copy)]
enum EffectObservation {
    Job,
    Runtime,
    Stop { cancel_pending_start: bool },
}

/// The one place a process-exit failure is built.  Whatever could not be read
/// is named in the failure, so it can never read as a container with nothing to
/// say: it carries its logs, or a typed reason they are missing, and an exit
/// summary.
pub(crate) fn process_exited(
    logs: Result<CommandOutput, String>,
    exit: Result<crate::runtime_logs::ContainerExit, &'static str>,
) -> OperationError {
    // The exit is classified from the capture; the windows cut from it once
    // are what cross the protocol.
    let (captured, capture_error) = match logs {
        // An unread log is reported as unread; it never becomes an empty tail
        // that reads like a container with nothing to say.
        Ok(logs) if logs.success => (
            Some(crate::runtime_logs::CapturedOutput::new(
                &logs.stdout,
                &logs.stderr,
            )),
            None,
        ),
        Ok(_) => (None, Some("the container log command failed")),
        Err(_) => (None, Some("the container log command did not run")),
    };
    let exit_summary = crate::runtime_logs::exit_summary(
        exit.as_ref().map_err(|e| *e),
        captured.as_ref(),
        capture_error,
    );
    let logs = captured.map(|captured| Box::new(captured.into_retained()));
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
        self.inspect_recipe_run_with_logs(request_sha256, false)
            .map(|inspection| inspection.running)
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
        self.require_directory(&self.roots.data)?;
        let request = self.read_runtime_request(request_sha256)?;
        if request.action != HostRuntimeAction::RunInspect
            || request.installation_id.is_some()
            || request.reconciliation_identity.is_some()
        {
            return Err(OperationError::InvalidOperation);
        }
        // Only the full run identity can bind a named generation; the shorter
        // shapes never answer for one.
        let unbound = request.run_generation.is_none();
        match request.arguments.as_slice() {
            [marker, installation_id]
                if unbound
                    && !include_logs
                    && marker == vonk_agent_protocol::INSTALLATION_REOBSERVATION_ARGUMENT =>
            {
                Ok(RunInspection {
                    running: self.installation_runtime_present(installation_id, false)?,
                    ..RunInspection::default()
                })
            }
            [marker, identity @ ..]
                if unbound && marker == vonk_agent_protocol::JOB_REOBSERVATION_ARGUMENT =>
            {
                self.inspect_effect(identity, include_logs, EffectObservation::Job)
            }
            [marker, identity @ ..]
                if unbound && marker == vonk_agent_protocol::RUNTIME_REOBSERVATION_ARGUMENT =>
            {
                self.inspect_effect(identity, include_logs, EffectObservation::Runtime)
            }
            [marker, identity @ ..]
                if unbound && marker == vonk_agent_protocol::STOP_REOBSERVATION_ARGUMENT =>
            {
                self.inspect_effect(
                    identity,
                    include_logs,
                    EffectObservation::Stop {
                        cancel_pending_start: false,
                    },
                )
            }
            [marker, identity @ ..]
                if unbound && marker == vonk_agent_protocol::CANCEL_STOP_REOBSERVATION_ARGUMENT =>
            {
                self.inspect_effect(
                    identity,
                    include_logs,
                    EffectObservation::Stop {
                        cancel_pending_start: true,
                    },
                )
            }
            // A single argument is a run id alone: the Controller-side truth
            // for a run whose local metadata is unreadable, checked by
            // container name. Without the full run identity the container
            // cannot be proven to be this run's, so its output is never read.
            [run_id] if unbound => {
                let named = self.runtime_run_container(run_id)?;
                Ok(RunInspection {
                    running: named.running,
                    job_progress_at: None,
                    exit_code: None,
                    logs: None,
                    log_error: include_logs.then(|| {
                        "the run identity was too short to authorise reading its output".into()
                    }),
                    container_labels: named.labels,
                })
            }
            arguments => self.runtime_run_inspect_detail(
                arguments,
                include_logs,
                include_logs,
                request.run_generation,
            ),
        }
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// Re-observe one job container by its whole identity (job, run,
    /// installation, generation, plan digest). Read-only. A live process
    /// answers running; an ended one its recorded exit, and for a failure its
    /// output and exit account when asked; proven absence is
    /// `RuntimeRunMissing`; anything unread keeps its cause.
    fn inspect_effect(
        &self,
        identity: &[String],
        include_logs: bool,
        mode: EffectObservation,
    ) -> Result<RunInspection, OperationError> {
        let canonical = |value: &String| {
            uuid::Uuid::parse_str(value)
                .ok()
                .filter(|parsed| parsed.to_string() == *value)
        };
        let [job_id, run_id, installation_id, run_generation, plan_digest] = identity else {
            return Err(OperationError::InvalidOperation);
        };
        let (Some(runtime_id), Some(run_id), Some(installation_id), Some(run_generation), true) = (
            canonical(job_id),
            canonical(run_id),
            canonical(installation_id),
            run_generation
                .parse::<u64>()
                .ok()
                .filter(|parsed| parsed.to_string() == *run_generation),
            lower_hex(plan_digest, 64),
        ) else {
            return Err(OperationError::InvalidOperation);
        };
        let identity = RuntimeEffectIdentity {
            runtime_id,
            installation_id,
            run_generation,
        };
        // Serialize the snapshot with launch/Stop admission, but never wait or
        // change the fence. An empty name before the high-water mark was
        // durable cannot prove that a delayed older Start remains fenced.
        let _installation_guard = matches!(mode, EffectObservation::Stop { .. })
            .then(|| self.lock_installation_runtime(&installation_id.to_string()))
            .transpose()?;
        if let EffectObservation::Stop {
            cancel_pending_start,
        } = mode
        {
            let fence = self.read_runtime_generation_fence(installation_id, runtime_id)?;
            if !fence.is_some_and(|fence| {
                fence.highest_generation == run_generation
                    && (!cancel_pending_start || fence.cancelled)
            }) || (cancel_pending_start && self.job_cancellation.is_active(identity)?)
            {
                return Err(OperationError::StopUncertain);
            }
        }
        match self.reobserve_by_identity(identity, run_id, plan_digest) {
            Reobserved::Running => Ok(RunInspection {
                job_progress_at: self.job_progress_at(runtime_id),
                running: true,
                ..RunInspection::default()
            }),
            Reobserved::Exited { code } => {
                let (logs, log_error) = if include_logs && code != 0 {
                    let (logs, exit) = self.container_exit_evidence(&format!("vonk-{job_id}"));
                    match process_exited(logs, exit) {
                        OperationError::RuntimeProcessExited {
                            logs, exit_summary, ..
                        } => (logs, Some(exit_summary)),
                        _ => (None, None),
                    }
                } else {
                    (None, None)
                };
                Ok(RunInspection {
                    running: false,
                    job_progress_at: None,
                    exit_code: Some(code),
                    logs,
                    log_error,
                    container_labels: None,
                })
            }
            // Removed after its exit was retained: that exit is the answer.
            Reobserved::Absent if matches!(mode, EffectObservation::Job) => self
                .retained_job_exit(identity, run_id, plan_digest, include_logs)
                .ok_or(OperationError::RuntimeRunMissing),
            Reobserved::Absent => Err(OperationError::RuntimeRunMissing),
            Reobserved::Unknown(cause) => Err(cause.into()),
        }
    }
}

/// What a name-only look at the managed container `vonk-<run_id>` found.
pub(super) struct NamedContainer {
    pub(super) running: bool,
    /// Its managed identity, when every label parses.
    pub(super) labels: Option<HostHelperContainerLabels>,
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// Whether the exact managed container `vonk-<run_id>` is running, and
    /// the managed identity its labels carry.
    /// Provably absent or stopped is `false`; a foreign container under the
    /// name or an unproven answer is an error, never a claim of absence.
    pub(super) fn runtime_run_container(
        &self,
        run_id: &str,
    ) -> Result<NamedContainer, OperationError> {
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
            "{{.Id}}\t{{.State.Running}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}\t{{index .Config.Labels \"ai.vonkforge.target-id\"}}\t{{index .Config.Labels \"ai.vonkforge.installation-id\"}}\t{{index .Config.Labels \"ai.vonkforge.run-generation\"}}\t{{index .Config.Labels \"ai.vonkforge.plan-digest\"}}".to_owned(),
            name.clone(),
        ])?;
        if !existing.success {
            return if self.prove_container_absent(&name, &existing)? {
                Ok(NamedContainer {
                    running: false,
                    labels: None,
                })
            } else {
                Err(CommandFailure::exit(&existing).into())
            };
        }
        // An empty last label must not be trimmed away with the line end.
        let fields = std::str::from_utf8(&existing.stdout)
            .ok()
            .map(|text| text.strip_suffix('\n').unwrap_or(text))
            .map(|text| text.split('\t').collect::<Vec<_>>())
            .unwrap_or_default();
        let [
            container_id,
            running,
            managed,
            label_run_id,
            target,
            installation,
            generation,
            plan_digest,
        ] = fields.as_slice()
        else {
            return Err(OperationError::InvalidArtifact);
        };
        if !lower_hex(container_id, 64) || *managed != "true" || *label_run_id != run_id {
            return Err(OperationError::InvalidArtifact);
        }
        let running = match *running {
            "true" => true,
            "false" => false,
            _ => return Err(OperationError::InvalidArtifact),
        };
        Ok(NamedContainer {
            running,
            labels: parse_managed_labels(run_id, target, installation, generation, plan_digest),
        })
    }
}

/// The managed identity of a container from its labels; none unless every
/// label is canonical, so a damaged label never becomes part of a stop.
fn parse_managed_labels(
    run_id: &str,
    target: &str,
    installation: &str,
    generation: &str,
    plan_digest: &str,
) -> Option<HostHelperContainerLabels> {
    let uuid = |text: &str| {
        uuid::Uuid::parse_str(text)
            .ok()
            .filter(|parsed| parsed.to_string() == text)
            .filter(|parsed| parsed.get_version() == Some(uuid::Version::Random))
    };
    Some(HostHelperContainerLabels {
        run_id: uuid(run_id)?,
        target_id: uuid(target)?,
        installation_id: uuid(installation)?,
        run_generation: generation
            .parse::<u64>()
            .ok()
            .filter(|parsed| parsed.to_string() == generation)
            .filter(|parsed| (1..=i64::MAX as u64).contains(parsed))?,
        plan_digest: lower_hex(plan_digest, 64).then(|| plan_digest.to_owned())?,
    })
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_run_inspect(
        &self,
        arguments: &[String],
        capture_failure: bool,
        expected_generation: Option<u64>,
    ) -> Result<bool, OperationError> {
        self.runtime_run_inspect_detail(arguments, capture_failure, false, expected_generation)
            .map(|inspection| inspection.running)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    /// The exact run container's state. `expected_generation`, when named,
    /// is the run generation its label must carry.
    pub(super) fn runtime_run_inspect_detail(
        &self,
        arguments: &[String],
        capture_failure: bool,
        include_logs: bool,
        expected_generation: Option<u64>,
    ) -> Result<RunInspection, OperationError> {
        let not_running = RunInspection {
            running: false,
            job_progress_at: None,
            exit_code: None,
            logs: None,
            log_error: None,
            container_labels: None,
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
        if self
            .observe_native_fabric(&mut validated, Path::new(NATIVE_FABRIC_ROOT))
            .is_err()
            && capture_failure
        {
            return Err(CommandFailure::FabricUnobserved.into());
        }
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
            "{{.Id}}\t{{.State.Running}}\t{{index .Config.Labels \"ai.vonkforge.runtime-request-sha256\"}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.run-id\"}}\t{{index .Config.Labels \"ai.vonkforge.run-generation\"}}\t{{index .Config.Labels \"ai.vonkforge.target-id\"}}\t{{index .Config.Labels \"ai.vonkforge.installation-id\"}}\t{{index .Config.Labels \"ai.vonkforge.plan-digest\"}}".to_owned(),
            format!("vonk-{}", validated.run_id),
        ])?;
        if !existing.success {
            if capture_failure
                && self.prove_container_absent(&format!("vonk-{}", validated.run_id), &existing)?
            {
                return Err(OperationError::RuntimeRunMissing);
            }
            return Err(CommandFailure::exit(&existing).into());
        }
        // Only the line end is dropped: an empty last label is a field.
        let fields = std::str::from_utf8(&existing.stdout)
            .ok()
            .map(|text| text.strip_suffix('\n').unwrap_or(text))
            .map(|text| text.split('\t').collect::<Vec<_>>())
            .unwrap_or_default();
        let [
            container_id,
            running,
            digest,
            managed,
            run_id,
            generation @ ..,
        ] = fields.as_slice()
        else {
            return Err(CommandFailure::UnexpectedAnswer.into());
        };
        // A container without the generation label ends the line there; a
        // whole line also carries the identity labels.
        let (generation, identity) = match generation {
            [] => ("", None),
            [label] => (*label, None),
            [label, target, installation, plan_digest] => {
                (*label, Some((*target, *installation, *plan_digest)))
            }
            _ => return Err(CommandFailure::UnexpectedAnswer.into()),
        };
        if !lower_hex(container_id, 64) || *managed != "true" || *run_id != validated.run_id {
            return Err(CommandFailure::UnexpectedAnswer.into());
        }
        // A container of another generation, or one without the label, is
        // not this Start's: the helper's own Start refuses to adopt it, and
        // so does an inspection that names the Start's generation.
        if expected_generation.is_some_and(|expected| generation != expected.to_string()) {
            return Err(OperationError::InvalidArtifact);
        }
        if *digest != semantic_digest {
            if capture_failure {
                // An unmatched observation cannot prove this workload stopped.
                return Err(CommandFailure::UnexpectedAnswer.into());
            }
            // Observation: this run's own container under its own name, launched
            // from a request an earlier agent rendered differently. It is still
            // this run; report what it is doing and say why it differs.
            eprintln!(
                "vonk-agent-helper: run {} container was launched from another request rendering; observing it by its run identity",
                validated.run_id
            );
        }
        if !matches!(*running, "true" | "false") {
            return Err(CommandFailure::UnexpectedAnswer.into());
        }
        let container_labels = identity.and_then(|(target, installation, plan_digest)| {
            parse_managed_labels(run_id, target, installation, generation, plan_digest)
        });
        if *running == "true" {
            if !include_logs {
                return Ok(RunInspection {
                    running: true,
                    job_progress_at: None,
                    exit_code: None,
                    logs: None,
                    log_error: None,
                    container_labels,
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
                job_progress_at: None,
                exit_code: None,
                logs,
                log_error,
                container_labels,
            });
        }
        if *running == "false"
            && !capture_failure
            && let Some(evidence) =
                crate::host_memory_guard::load_evidence(&self.roots.data, container_id)
        {
            return Ok(RunInspection {
                running: false,
                job_progress_at: None,
                exit_code: None,
                logs: None,
                log_error: Some(crate::host_memory_guard::summary(&evidence)),
                container_labels: None,
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
        let exit = self.container_exit(target);
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

impl<R: CommandRunner> OperationExecutor<R> {
    /// The fields Docker records for the container `target` (an exact id or
    /// name), in a Docker `format`. Read-only.
    pub(super) fn container_fields(
        &self,
        target: &str,
        format: &str,
    ) -> Result<CommandOutput, String> {
        self.runner.run_with_timeout(
            Path::new("/usr/bin/docker"),
            &[
                "container".into(),
                "inspect".into(),
                "--format".into(),
                format.into(),
                target.into(),
            ],
            Duration::from_secs(30),
        )
    }

    /// How the container `target` (an exact id or name) ended, as Docker
    /// recorded it.
    pub(super) fn container_exit(
        &self,
        target: &str,
    ) -> Result<crate::runtime_logs::ContainerExit, &'static str> {
        match self.container_fields(target, crate::runtime_logs::EXIT_FORMAT) {
            Ok(output) if output.success => crate::runtime_logs::parse_exit(&output.stdout)
                .ok_or("the container exit state was not readable"),
            Ok(_) => Err("the container exit inspection failed"),
            Err(_) => Err("the container exit inspection did not run"),
        }
    }
}

pub(super) fn bounded_container_wait_exit_code(output: &CommandOutput) -> Option<i32> {
    std::str::from_utf8(&output.stdout)
        .ok()
        .map(str::trim)
        .and_then(|value| value.parse::<i32>().ok())
        .filter(|code| (0..=255).contains(code))
}

#[cfg(test)]
mod tests;

impl<R: CommandRunner> OperationExecutor<R> {
    /// Read-only preparation probes. No grant, nonce, ledger, receipt or runtime
    /// mutation is performed; the request digest binds the exact subject.
    pub fn observe_preparation(&self, digest: &str) -> Result<bool, OperationError> {
        self.require_directory(&self.roots.data)?;
        let request = self.read_runtime_request(digest)?;
        match request.action {
            HostRuntimeAction::ImagePull => {
                let [_, manifest, config, reference] = request.arguments.as_slice() else {
                    return Err(OperationError::InvalidOperation);
                };
                let (local, embedded) = parse_local_image_reference(reference)?;
                if embedded != *manifest || !valid_oci_digest(config) {
                    return Err(OperationError::InvalidOperation);
                }
                let Some(image) = self.inspect_runtime_image_if_present(&local)? else {
                    return Ok(false);
                };
                if (image.0 != *config && image.0 != *manifest)
                    || image.1 != "linux"
                    || image.2 != "arm64"
                    || image.3 != "v1"
                    || !numeric_non_root_user(&image.4)
                {
                    return Err(OperationError::RuntimeImageIdentityInvalid);
                }
                Ok(true)
            }
            HostRuntimeAction::InstallationCleanup => {
                let id = request
                    .installation_id
                    .ok_or(OperationError::InvalidOperation)?
                    .to_string();
                self.require_no_unclassified_installation_runtime(&id)?;
                let cache = self
                    .roots
                    .agent_data
                    .join("installations")
                    .join(id)
                    .join("runtime-cache");
                match fs::symlink_metadata(cache) {
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(true),
                    Err(error) => Err(error.into()),
                    Ok(_) => Ok(false),
                }
            }
            // Never dispatch a mutator through the observation frame.
            _ => Err(OperationError::InvalidOperation),
        }
    }
}
