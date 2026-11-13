//! Exact, bounded reconciliation of every service owned by one build.

use super::diagnostics::process_error_diagnostic;
use super::*;

pub fn cleanup_build<R: ProcessRunner + ?Sized>(
    runner: &R,
    request: &RecipeBuildCleanupRequest,
) -> Result<RecipeBuildCleanupEvidence, RecipeBuildError> {
    cleanup_build_until(runner, request, Instant::now() + Duration::from_secs(60))
}

pub fn cleanup_build_until<R: ProcessRunner + ?Sized>(
    runner: &R,
    request: &RecipeBuildCleanupRequest,
    deadline: Instant,
) -> Result<RecipeBuildCleanupEvidence, RecipeBuildError> {
    let units = build_units(request);
    let mut pending = Vec::new();
    for attempt in 0..3 {
        pending.clear();
        for unit in &units {
            let observed = match observe_unit(runner, unit, deadline) {
                Ok(None) => continue,
                Ok(Some(state)) => state,
                Err(cause) => format!("is unobserved ({cause})"),
            };
            pending.push(format!("unit \"{unit}\" {observed}"));
            // A failed/malformed observation is unknown. Submit the exact stop
            // and observe again; never infer absence from a failed query.
            let _ = runner.run(
                Program::Systemctl,
                &[
                    "--user".into(),
                    "--no-block".into(),
                    vonk_agent_protocol::generated::ProfileChildPhase::Stop
                        .as_str()
                        .into(),
                    unit.clone(),
                ],
                phase_time(deadline, Duration::from_secs(5))?,
            );
        }
        if pending.is_empty() {
            return Ok(RecipeBuildCleanupEvidence {});
        }
        if attempt < 2 {
            std::thread::sleep(
                Duration::from_millis(50 * (attempt + 1)).min(remaining_build_time(deadline)?),
            );
        }
    }
    Err(RecipeBuildError::CleanupPending {
        subject: "build units to exit".into(),
        observed: pending.join("; "),
    })
}

/// `None` when the unit is gone or settled, else what it still shows. An
/// observation that cannot be read is an error naming why.
fn observe_unit<R: ProcessRunner + ?Sized>(
    runner: &R,
    unit: &str,
    deadline: Instant,
) -> Result<Option<String>, String> {
    let systemctl = |query: &str, arguments: Vec<String>| -> Result<String, String> {
        let timeout = phase_time(deadline, Duration::from_secs(5))
            .map_err(|_| "the cleanup deadline elapsed".to_owned())?;
        let output = runner
            .run(Program::Systemctl, &arguments, timeout)
            .map_err(|error| format!("systemctl {query} {}", process_error_diagnostic(&error)))?;
        if !output.success {
            return Err(format!("systemctl {query} failed"));
        }
        String::from_utf8(output.stdout)
            .map_err(|_| format!("systemctl {query} output is unreadable"))
    };
    let text = systemctl(
        "list-units",
        vec![
            "--user".into(),
            "list-units".into(),
            "--all".into(),
            "--plain".into(),
            "--no-legend".into(),
            "--no-pager".into(),
            "--full".into(),
            unit.into(),
        ],
    )?;
    if text.trim().is_empty() {
        return Ok(None);
    }
    if text.lines().count() != 1 || text.split_whitespace().next() != Some(unit) {
        return Err("systemctl list-units output is unreadable".into());
    }
    let text = systemctl(
        "show",
        vec![
            "--user".into(),
            "show".into(),
            unit.into(),
            "--property=LoadState".into(),
            "--property=ActiveState".into(),
            "--property=MainPID".into(),
            "--property=ControlGroup".into(),
        ],
    )?;
    let fields: std::collections::BTreeMap<_, _> = text
        .lines()
        .filter_map(|line| line.split_once('='))
        .collect();
    if fields.len() != 4 || text.lines().count() != 4 {
        return Err("systemctl show output is unreadable".into());
    }
    // These are systemd's external state words, not platform contract states.
    let field = |name: &str| fields.get(name).copied().unwrap_or_default();
    let settled = (field("ActiveState") == "inactive"
        || field("ActiveState") == vonk_agent_protocol::generated::LifecycleState::Failed.as_str())
        && field("MainPID") == "0"
        && field("ControlGroup").is_empty();
    Ok((!settled).then(|| {
        format!(
            "is {} (MainPID {}, control group \"{}\")",
            field("ActiveState"),
            field("MainPID"),
            field("ControlGroup")
        )
    }))
}

/// Explicit cleanup also retires the operation's private networks and storage.
/// Retained exported bytes live separately in builds/ and survive this pass.
pub fn cleanup_build_storage<R: ProcessRunner + ?Sized>(
    runner: &R,
    data_root: &Path,
    runtime_root: &Path,
    request: &RecipeBuildCleanupRequest,
) -> Result<RecipeBuildCleanupEvidence, RecipeBuildError> {
    let deadline = Instant::now() + Duration::from_secs(60);
    cleanup_build_until(runner, request, deadline)?;
    let metadata = fs::symlink_metadata(data_root).map_err(at("stat", data_root))?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || fs::canonicalize(data_root).map_err(at("resolve", data_root))? != data_root
    {
        return Err(RecipeBuildError::CleanupBlocked(format!(
            "the data root \"{}\" is not a canonical directory",
            data_root.display()
        )));
    }
    let root = data_root.join("build-staging");
    if let Ok(metadata) = fs::symlink_metadata(&root)
        && (!metadata.is_dir() || metadata.file_type().is_symlink())
    {
        fs::rename(
            &root,
            data_root.join(format!(".build-staging-{}", Uuid::new_v4())),
        )
        .map_err(at("move aside", &root))?;
        return Ok(RecipeBuildCleanupEvidence {});
    }
    let entries = match fs::read_dir(&root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(RecipeBuildCleanupEvidence {});
        }
        Err(error) => return Err(at("list", &root)(error)),
    };
    let suffix = format!("-{}", request.build_id);
    for entry in entries {
        remaining_build_time(deadline)?;
        let entry = entry.map_err(at("list", &root))?;
        let name = entry.file_name();
        let Some(identity) = name.to_str().and_then(|name| name.strip_suffix(&suffix)) else {
            continue;
        };
        if identity.len() != 64
            || !identity
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            continue;
        }
        let metadata = fs::symlink_metadata(entry.path()).map_err(at("stat", &entry.path()))?;
        if metadata.is_dir() && !metadata.file_type().is_symlink() {
            fs::create_dir_all(runtime_root).map_err(at("create", runtime_root))?;
            let runroot = super::builder::build_runroot(runtime_root, request.build_id)?;
            let staging = entry.path();
            let tmp = staging.join("podman-image-tmp");
            let storage = staging.join("podman-storage");
            for path in [&tmp, &storage] {
                if let Ok(metadata) = fs::symlink_metadata(path)
                    && (!metadata.is_dir() || metadata.file_type().is_symlink())
                {
                    fs::rename(path, staging.join(format!(".damaged-{}", Uuid::new_v4())))
                        .map_err(at("move aside", path))?;
                }
                fs::create_dir_all(path).map_err(at("create", path))?;
            }
            let id = request.build_id.simple().to_string();
            for extra in [
                vec![
                    "rm".into(),
                    "--force".into(),
                    format!("vonk-build-proxy-{id}"),
                ],
                vec![
                    "network".into(),
                    "rm".into(),
                    "--force".into(),
                    format!("vonk-build-in-{id}"),
                ],
                vec![
                    "network".into(),
                    "rm".into(),
                    "--force".into(),
                    format!("vonk-build-out-{id}"),
                ],
            ] {
                let timeout = phase_time(deadline, Duration::from_secs(5))?;
                let mut arguments = podman_user_service_arguments(
                    &format!("vonk-recipe-build-{}-e", request.build_id),
                    runroot.as_path(),
                    &staging,
                    timeout,
                    true,
                );
                arguments.push("/usr/bin/podman".into());
                arguments.extend(podman_storage_arguments(&storage, runroot.as_path()));
                arguments.extend(extra);
                // A missing container/network is an idempotent miss. The exact
                // cgroup must still settle before its private storage is removed.
                if let Ok(output) = runner.run(Program::SystemdRun, &arguments, timeout)
                    && super::import::podman_database_conflict(&output)
                {
                    // Exact operation services are already settled. Reconcile
                    // an incompatible private execution cache once, retaining
                    // verified base images and unrelated builds.
                    remove_private_tree(&storage, deadline)?;
                    fs::create_dir_all(&storage)
                        .map_err(at("recreate private storage", &storage))?;
                    break;
                }
            }
            let mut present = Vec::new();
            for attempt in 0..3 {
                present.clear();
                for (what, extra) in [
                    (
                        "container",
                        vec![
                            "ps".into(),
                            "--all".into(),
                            "--format={{.Names}}".into(),
                            format!("--filter=name=vonk-build-proxy-{id}"),
                        ],
                    ),
                    (
                        "network",
                        vec![
                            "network".into(),
                            "ls".into(),
                            "--format={{.Name}}".into(),
                            format!("--filter=name=vonk-build-in-{id}|vonk-build-out-{id}"),
                        ],
                    ),
                ] {
                    let timeout = phase_time(deadline, Duration::from_secs(5))?;
                    let mut arguments = podman_user_service_arguments(
                        &format!("vonk-recipe-build-{}-e", request.build_id),
                        runroot.as_path(),
                        &staging,
                        timeout,
                        true,
                    );
                    arguments.push("/usr/bin/podman".into());
                    arguments.extend(podman_storage_arguments(&storage, runroot.as_path()));
                    arguments.extend(extra);
                    match runner.run(Program::SystemdRun, &arguments, timeout) {
                        Ok(output) if output.success => present.extend(
                            String::from_utf8_lossy(&output.stdout)
                                .split_whitespace()
                                .map(|name| format!("{what} \"{name}\" is still present")),
                        ),
                        Ok(output) => {
                            return Err(super::diagnostics::command_failure(
                                FailureStage::BoundedBuildProcess,
                                &output,
                            ));
                        }
                        Err(error) => present.push(format!(
                            "the Podman {what} query did not run ({})",
                            process_error_diagnostic(&error)
                        )),
                    }
                }
                if present.is_empty() {
                    break;
                }
                if attempt < 2 {
                    std::thread::sleep(
                        Duration::from_millis(50 * (attempt + 1))
                            .min(remaining_build_time(deadline)?),
                    );
                }
            }
            if !present.is_empty() {
                return Err(RecipeBuildError::CleanupPending {
                    subject: "build containers and networks to be removed".into(),
                    observed: present.join("; "),
                });
            }
            cleanup_build_until(runner, request, deadline)?;
            remove_private_tree(&staging, deadline)?;
            remove_private_tree(&runroot, deadline)?;
        } else {
            // Isolate the generated entry without touching its uncertain target.
            fs::rename(
                entry.path(),
                root.join(format!(".damaged-{}", Uuid::new_v4())),
            )
            .map_err(at("move aside", &entry.path()))?;
        }
    }
    Ok(RecipeBuildCleanupEvidence {})
}

pub(super) fn remove_private_tree(path: &Path, deadline: Instant) -> Result<(), RecipeBuildError> {
    remaining_build_time(deadline)?;
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(at("stat", path)(error)),
    };
    if metadata.is_dir() && !metadata.file_type().is_symlink() {
        let mut permissions = metadata.permissions();
        permissions.set_mode(permissions.mode() | 0o700);
        fs::set_permissions(path, permissions).map_err(at("make writable", path))?;
        for entry in fs::read_dir(path).map_err(at("list", path))? {
            remove_private_tree(&entry.map_err(at("list", path))?.path(), deadline)?;
        }
        fs::remove_dir(path).map_err(at("remove", path))?;
    } else {
        fs::remove_file(path).map_err(at("remove", path))?;
    }
    Ok(())
}

/// The error of an IO `action` on `path`: a cleanup says which path failed.
fn at(action: &'static str, path: &Path) -> impl FnOnce(std::io::Error) -> RecipeBuildError {
    let path = path.to_path_buf();
    move |source| RecipeBuildError::CleanupPath {
        action,
        path,
        source,
    }
}

/// Errors that fail the same way on every attempt until the host changes: a
/// path the agent may not change, a busy mount, a read-only filesystem.
const BLOCKING_ERRNOS: [rustix::io::Errno; 4] = [
    rustix::io::Errno::ACCESS,
    rustix::io::Errno::PERM,
    rustix::io::Errno::BUSY,
    rustix::io::Errno::ROFS,
];

impl RecipeBuildError {
    /// A cleanup error that waiting cannot end: it is reported as a failure
    /// with its evidence, never as an unconfirmed effect that is retried as is.
    pub fn cleanup_cannot_succeed_as_is(&self) -> bool {
        match self {
            Self::CleanupBlocked(_) => true,
            Self::CleanupPath { source, .. } => source.raw_os_error().is_some_and(|code| {
                BLOCKING_ERRNOS
                    .iter()
                    .any(|errno| errno.raw_os_error() == code)
            }),
            _ => false,
        }
    }

    /// The stable, secret-free word for why a cleanup is not confirmed.
    pub fn cleanup_diagnostic(&self) -> String {
        match self {
            Self::CleanupPending { .. } => "build-effect-still-present".into(),
            Self::CleanupPath { source, .. } => source.raw_os_error().map_or_else(
                || "build-path-error".into(),
                |code| format!("build-path-os-error-{code}"),
            ),
            Self::CleanupBlocked(_) => "build-cleanup-precondition".into(),
            Self::Process(error) => process_error_diagnostic(error).into(),
            _ => "bounded unit reconciliation ended without a complete observation".into(),
        }
    }

    /// The reason a cleanup that is not confirmed reports: its cause and subject.
    pub fn cleanup_reason(&self) -> String {
        match self {
            Self::Process(error) => format!(
                "build cleanup did not finish: {}",
                process_error_diagnostic(error)
            ),
            Self::CleanupPending { .. } | Self::CleanupPath { .. } | Self::CleanupBlocked(_) => {
                self.to_string()
            }
            _ => format!("build effects are not yet confirmed quiescent: {self}"),
        }
    }
}

/// Check the same exact unit names and private staging owned by cleanup, with
/// no stop/remove/repair. A failed listing never establishes absence.
pub fn observe_build_cleanup<R: ProcessRunner + ?Sized>(
    runner: &R,
    data_root: &Path,
    request: &RecipeBuildCleanupRequest,
    deadline: Instant,
) -> Result<bool, RecipeBuildError> {
    for unit in build_units(request) {
        match observe_unit(runner, &unit, deadline) {
            Ok(None) => {}
            Ok(Some(_)) => return Ok(false),
            Err(cause) => {
                return Err(RecipeBuildError::CleanupPending {
                    subject: unit,
                    observed: cause,
                });
            }
        }
    }
    let entries = match fs::read_dir(data_root.join("build-staging")) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(true),
        Err(error) => return Err(at("list", &data_root.join("build-staging"))(error)),
    };
    let suffix = format!("-{}", request.build_id);
    for entry in entries {
        if entry
            .map_err(at("list", data_root))?
            .file_name()
            .to_str()
            .is_some_and(|name| name.ends_with(&suffix))
        {
            return Ok(false);
        }
    }
    Ok(true)
}

fn build_units(request: &RecipeBuildCleanupRequest) -> [String; 3] {
    [
        format!("vonk-recipe-build-{}.service", request.build_id),
        format!("vonk-runtime-adapter-{}.service", request.build_id),
        format!("vonk-recipe-build-{}-e.service", request.build_id),
    ]
}

/// Crash-only backstop for build effects that no build unit owns.
///
/// Rootless Podman starts each container (`libpod-*`), its network namespace
/// (`rootless-netns-*`) and each runroot's pause process (`podman-pause-*`) in
/// scopes of their own beside the unit that ran it, so stopping that unit, or
/// an agent restart that skips the in-process stop, leaves them running. In the
/// agent's user manager only builds run Podman (loads run on Docker through the
/// helper), so with no build unit live and no build beside the lane
/// (`beside`, read after the units) every such scope is a leftover. The
/// scopes are listed before liveness is read: a build that starts later never
/// has its scopes in this list. An egress unit (`-e`) whose build is not beside
/// the lane and whose recipe unit and runtime adapter are both gone serves
/// nothing and is stopped; its scopes follow on a later pass. Any listing that
/// cannot be read stops nothing, and a scope already stopped or failed is not
/// asked again. Returns the units it asked to stop.
pub fn stop_orphaned_build_effects<R: ProcessRunner + ?Sized>(
    runner: &R,
    beside: &dyn Fn() -> std::collections::BTreeSet<Uuid>,
    deadline: Instant,
) -> Result<Vec<String>, RecipeBuildError> {
    let scopes = list_units(
        runner,
        &[
            "--state=active",
            "libpod-*.scope",
            "rootless-netns-*.scope",
            "podman-pause-*.scope",
        ],
        deadline,
    )?;
    let mut live = Vec::new();
    for unit in list_units(
        runner,
        &["vonk-recipe-build-*", "vonk-runtime-adapter-*"],
        deadline,
    )? {
        match observe_unit(runner, &unit, deadline) {
            Ok(None) => {}
            Ok(Some(_)) => live.push(unit),
            Err(cause) => {
                return Err(RecipeBuildError::CleanupPending {
                    subject: unit,
                    observed: cause,
                });
            }
        }
    }
    let beside = beside();
    let stop = if live.is_empty() && beside.is_empty() {
        scopes
    } else {
        live.iter()
            .filter(|unit| {
                unit.strip_prefix("vonk-recipe-build-")
                    .and_then(|rest| rest.strip_suffix("-e.service"))
                    .and_then(|id| Uuid::parse_str(id).ok())
                    .is_some_and(|build_id| {
                        let request = RecipeBuildCleanupRequest {
                            build_id,
                            operation_id: build_id,
                        };
                        !beside.contains(&build_id)
                            && build_units(&request)[..2]
                                .iter()
                                .all(|owner| !live.contains(owner))
                    })
            })
            .cloned()
            .collect()
    };
    if !stop.is_empty() {
        let mut arguments = vec![
            "--user".into(),
            "--no-block".into(),
            vonk_agent_protocol::generated::ProfileChildPhase::Stop
                .as_str()
                .into(),
        ];
        arguments.extend(stop.iter().cloned());
        // Submitted, not awaited: the next pass observes what remains.
        let _ = runner.run(
            Program::Systemctl,
            &arguments,
            phase_time(deadline, Duration::from_secs(5))?,
        );
    }
    Ok(stop)
}

/// The names of the user manager's loaded units matching `patterns` (which
/// may start with a state filter).
fn list_units<R: ProcessRunner + ?Sized>(
    runner: &R,
    patterns: &[&str],
    deadline: Instant,
) -> Result<Vec<String>, RecipeBuildError> {
    let mut arguments: Vec<String> = [
        "--user",
        "list-units",
        "--all",
        "--plain",
        "--no-legend",
        "--no-pager",
        "--full",
    ]
    .map(String::from)
    .into();
    arguments.extend(patterns.iter().map(|pattern| (*pattern).to_owned()));
    let unreadable = |observed: String| RecipeBuildError::CleanupPending {
        subject: patterns.join(" "),
        observed,
    };
    let output = runner
        .run(
            Program::Systemctl,
            &arguments,
            phase_time(deadline, Duration::from_secs(5))?,
        )
        .map_err(|error| unreadable(process_error_diagnostic(&error).into()))?;
    if !output.success {
        return Err(unreadable("systemctl list-units failed".into()));
    }
    let text = String::from_utf8(output.stdout)
        .map_err(|_| unreadable("systemctl list-units output is unreadable".into()))?;
    Ok(text
        .lines()
        .filter_map(|line| line.split_whitespace().next())
        .map(str::to_owned)
        .collect())
}

pub fn observe_build_running<R: ProcessRunner + ?Sized>(
    runner: &R,
    request: &RecipeBuildCleanupRequest,
    deadline: Instant,
) -> Result<bool, RecipeBuildError> {
    for unit in build_units(request) {
        match observe_unit(runner, &unit, deadline) {
            Ok(Some(_)) => return Ok(true),
            Ok(None) => {}
            Err(cause) => {
                return Err(RecipeBuildError::CleanupPending {
                    subject: unit,
                    observed: cause,
                });
            }
        }
    }
    Ok(false)
}
