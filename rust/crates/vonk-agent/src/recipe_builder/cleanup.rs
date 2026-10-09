//! Exact, bounded reconciliation of every service owned by one build.

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
    let units = [
        format!("vonk-recipe-build-{}.service", request.build_id),
        format!("vonk-runtime-adapter-{}.service", request.build_id),
        format!("vonk-recipe-build-{}-e.service", request.build_id),
    ];
    for attempt in 0..3 {
        let mut settled = true;
        for unit in &units {
            if matches!(observe_unit(runner, unit, deadline), Ok(true)) {
                continue;
            }
            settled = false;
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
        if settled {
            return Ok(RecipeBuildCleanupEvidence {});
        }
        if attempt < 2 {
            std::thread::sleep(
                Duration::from_millis(50 * (attempt + 1)).min(remaining_build_time(deadline)?),
            );
        }
    }
    Err(RecipeBuildError::Evidence)
}

fn observe_unit<R: ProcessRunner + ?Sized>(
    runner: &R,
    unit: &str,
    deadline: Instant,
) -> Result<bool, RecipeBuildError> {
    let listed = runner.run(
        Program::Systemctl,
        &[
            "--user".into(),
            "list-units".into(),
            "--all".into(),
            "--plain".into(),
            "--no-legend".into(),
            "--no-pager".into(),
            "--full".into(),
            unit.into(),
        ],
        phase_time(deadline, Duration::from_secs(5))?,
    )?;
    if !listed.success {
        return Err(RecipeBuildError::Evidence);
    }
    let text = std::str::from_utf8(&listed.stdout).map_err(|_| RecipeBuildError::Evidence)?;
    if text.trim().is_empty() {
        return Ok(true);
    }
    if text.lines().count() != 1 || text.split_whitespace().next() != Some(unit) {
        return Err(RecipeBuildError::Evidence);
    }
    let output = runner.run(
        Program::Systemctl,
        &[
            "--user".into(),
            "show".into(),
            unit.into(),
            "--property=LoadState".into(),
            "--property=ActiveState".into(),
            "--property=MainPID".into(),
            "--property=ControlGroup".into(),
        ],
        phase_time(deadline, Duration::from_secs(5))?,
    )?;
    if !output.success {
        return Err(RecipeBuildError::Evidence);
    }
    let text = std::str::from_utf8(&output.stdout).map_err(|_| RecipeBuildError::Evidence)?;
    let fields: std::collections::BTreeMap<_, _> = text
        .lines()
        .filter_map(|line| line.split_once('='))
        .collect();
    if fields.len() != 4 || text.lines().count() != 4 {
        return Err(RecipeBuildError::Evidence);
    }
    // These are systemd's external state words, not platform contract states.
    Ok((fields.get("ActiveState") == Some(&"inactive")
        || fields.get("ActiveState")
            == Some(&vonk_agent_protocol::generated::LifecycleState::Failed.as_str()))
        && fields.get("MainPID") == Some(&"0")
        && fields.get("ControlGroup") == Some(&""))
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
    let metadata = fs::symlink_metadata(data_root)?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || fs::canonicalize(data_root)? != data_root
    {
        return Err(RecipeBuildError::Evidence);
    }
    let root = data_root.join("build-staging");
    if let Ok(metadata) = fs::symlink_metadata(&root)
        && (!metadata.is_dir() || metadata.file_type().is_symlink())
    {
        fs::rename(
            &root,
            data_root.join(format!(".build-staging-{}", Uuid::new_v4())),
        )?;
        return Ok(RecipeBuildCleanupEvidence {});
    }
    let entries = match fs::read_dir(&root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(RecipeBuildCleanupEvidence {});
        }
        Err(error) => return Err(error.into()),
    };
    let suffix = format!("-{}", request.build_id);
    for entry in entries {
        remaining_build_time(deadline)?;
        let entry = entry?;
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
        let metadata = fs::symlink_metadata(entry.path())?;
        if metadata.is_dir() && !metadata.file_type().is_symlink() {
            fs::create_dir_all(runtime_root)?;
            let runroot = Builder::new().prefix("b-").tempdir_in(runtime_root)?;
            if runroot.path().as_os_str().as_bytes().len() > 50 {
                return Err(RecipeBuildError::Evidence);
            }
            let staging = entry.path();
            let tmp = staging.join("podman-image-tmp");
            let storage = staging.join("podman-storage");
            for path in [&tmp, &storage] {
                if let Ok(metadata) = fs::symlink_metadata(path)
                    && (!metadata.is_dir() || metadata.file_type().is_symlink())
                {
                    fs::rename(path, staging.join(format!(".damaged-{}", Uuid::new_v4())))?;
                }
                fs::create_dir_all(path)?;
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
                    runroot.path(),
                    &staging,
                    timeout,
                    true,
                );
                arguments.push("/usr/bin/podman".into());
                arguments.extend(podman_storage_arguments(&storage, runroot.path()));
                arguments.extend(extra);
                // A missing container/network is an idempotent miss. The exact
                // cgroup must still settle before its private storage is removed.
                let _ = runner.run(Program::SystemdRun, &arguments, timeout);
            }
            let mut absent = false;
            for attempt in 0..3 {
                absent = true;
                for extra in [
                    vec![
                        "ps".into(),
                        "--all".into(),
                        "--format={{.Names}}".into(),
                        format!("--filter=name=vonk-build-proxy-{id}"),
                    ],
                    vec![
                        "network".into(),
                        "ls".into(),
                        "--format={{.Name}}".into(),
                        format!("--filter=name=vonk-build-in-{id}|vonk-build-out-{id}"),
                    ],
                ] {
                    let timeout = phase_time(deadline, Duration::from_secs(5))?;
                    let mut arguments = podman_user_service_arguments(
                        &format!("vonk-recipe-build-{}-e", request.build_id),
                        runroot.path(),
                        &staging,
                        timeout,
                        true,
                    );
                    arguments.push("/usr/bin/podman".into());
                    arguments.extend(podman_storage_arguments(&storage, runroot.path()));
                    arguments.extend(extra);
                    absent &= runner
                        .run(Program::SystemdRun, &arguments, timeout)
                        .is_ok_and(|output| {
                            output.success
                                && output.stdout.iter().all(|byte| byte.is_ascii_whitespace())
                        });
                }
                if absent {
                    break;
                }
                if attempt < 2 {
                    std::thread::sleep(
                        Duration::from_millis(50 * (attempt + 1))
                            .min(remaining_build_time(deadline)?),
                    );
                }
            }
            if !absent {
                return Err(RecipeBuildError::Evidence);
            }
            cleanup_build_until(runner, request, deadline)?;
            remove_private_tree(&staging, deadline)?;
        } else {
            // Isolate the generated entry without touching its uncertain target.
            fs::rename(
                entry.path(),
                root.join(format!(".damaged-{}", Uuid::new_v4())),
            )?;
        }
    }
    Ok(RecipeBuildCleanupEvidence {})
}

pub(super) fn remove_private_tree(path: &Path, deadline: Instant) -> Result<(), RecipeBuildError> {
    remaining_build_time(deadline)?;
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    if metadata.is_dir() && !metadata.file_type().is_symlink() {
        let mut permissions = metadata.permissions();
        permissions.set_mode(permissions.mode() | 0o700);
        fs::set_permissions(path, permissions)?;
        for entry in fs::read_dir(path)? {
            remove_private_tree(&entry?.path(), deadline)?;
        }
        fs::remove_dir(path)?;
    } else {
        fs::remove_file(path)?;
    }
    Ok(())
}
