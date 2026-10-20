//! Fixed offline runtime exercise through the existing signed helper boundary.
//! No request-supplied path, image, argument, capability, mount or executable.
use std::{
    fs, io,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

pub const PROBE_BINARY: &str = "/usr/lib/vonk-forge/vonk-runtime-probe";

struct Cleanup(PathBuf);
impl Drop for Cleanup {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// Closure must use the helper's normal sanitized Docker execution boundary.
/// Stable exit evidence: 0 success; 30 import; 31 sandbox run; 32 cleanup.
pub fn run(
    root: &Path,
    probe_binary: &Path,
    mut docker: impl FnMut(&[String], Duration) -> io::Result<(bool, Vec<u8>)>,
) -> io::Result<i32> {
    let started = Instant::now();
    let deadline = started + Duration::from_secs(10);
    let metadata = fs::symlink_metadata(probe_binary)?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.uid() != 0
        || metadata.mode() & 0o022 != 0
    {
        return Err(io::Error::other("unsafe installed runtime probe"));
    }
    let parent = fs::symlink_metadata(root)?;
    if !parent.is_dir()
        || parent.file_type().is_symlink()
        || parent.uid() != 0
        || parent.mode() & 0o022 != 0
    {
        return Err(io::Error::other("unsafe runtime preflight root"));
    }
    let nonce = uuid::Uuid::new_v4();
    let staging = root.join(format!("runtime-preflight-{nonce}"));
    fs::create_dir(&staging)?;
    fs::set_permissions(&staging, fs::Permissions::from_mode(0o700))?;
    let _cleanup = Cleanup(staging.clone());
    let archive_path = staging.join("probe.tar");
    let mut archive = tar::Builder::new(fs::File::create(&archive_path)?);
    archive.append_path_with_name(probe_binary, "probe")?;
    let mut temporary = tar::Header::new_gnu();
    temporary.set_path("tmp")?;
    temporary.set_entry_type(tar::EntryType::Directory);
    temporary.set_mode(0o1777);
    temporary.set_size(0);
    temporary.set_cksum();
    archive.append(&temporary, io::empty())?;
    vonk_agent_protocol::runtime_identity::append_to(&mut archive, 65534, 65534)?;
    archive.finish()?;
    drop(archive);
    let image = format!("vonk-runtime-preflight:{nonce}");
    let name = format!("vonk-runtime-preflight-{nonce}");
    let remaining = || {
        deadline
            .saturating_duration_since(Instant::now())
            .max(Duration::from_millis(1))
    };
    let outcome = (|| {
        let (success, _) = docker(
            &[
                "image".into(),
                "import".into(),
                archive_path.display().to_string(),
                image.clone(),
            ],
            remaining(),
        )?;
        if !success {
            return Ok(30);
        }
        let (success, stdout) = docker(&run_arguments(&name, &image), remaining())?;
        Ok(if success && stdout == b"vonk-runtime-preflight-ok\n" {
            0
        } else {
            (21..=25)
                .find(|code| {
                    String::from_utf8_lossy(&stdout)
                        .contains(&format!("vonk-runtime-preflight-error:{code}"))
                })
                .unwrap_or(31)
        })
    })();
    // Always remove exact, uniquely named probe objects, including failed runs.
    let _ = docker(
        &["container".into(), "rm".into(), "--force".into(), name],
        cleanup_budget(started, deadline),
    );
    let cleaned = docker(
        &["image".into(), "rm".into(), "--force".into(), image],
        cleanup_budget(started, deadline),
    );
    match outcome {
        Ok(0) if !matches!(cleaned, Ok((true, _))) => Ok(32),
        value => value,
    }
}

/// How long removing one of the probe's own objects may take: as long as
/// making and running them took so far, and never less than what is left of
/// the preflight's budget. It follows the Docker daemon's own pace on this
/// host, so a busy daemon is not reported as a failed cleanup.
fn cleanup_budget(started: Instant, deadline: Instant) -> Duration {
    started
        .elapsed()
        .max(deadline.saturating_duration_since(Instant::now()))
}

pub fn run_arguments(name: &str, image: &str) -> Vec<String> {
    [
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--read-only",
        "--user=65534:65534",
        "--pids-limit=64",
        "--memory=64m",
        "--cpus=1",
        "--tmpfs=/tmp:rw,nosuid,nodev,noexec,mode=1777,size=1m",
        "--name",
        name,
        image,
        "/probe",
    ]
    .into_iter()
    .map(str::to_owned)
    .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn probe_cleanup_follows_the_work_it_undoes_never_a_fixed_second() {
        // A quick probe leaves its cleanup the rest of the preflight budget.
        let now = Instant::now();
        assert!(cleanup_budget(now, now + Duration::from_secs(10)) > Duration::from_secs(9));
        // A probe that ran past the budget on a loaded host gets as long to
        // remove its objects as it took to make and run them.
        let slow = now.checked_sub(Duration::from_secs(12)).unwrap();
        assert!(cleanup_budget(slow, slow + Duration::from_secs(10)) >= Duration::from_secs(12));
    }
    #[test]
    fn serving_probe_has_no_device_mount_capability_or_network_grants() {
        let args = run_arguments("probe-name", "probe-image");
        for flag in [
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--read-only",
            "--pull=never",
            "--user=65534:65534",
        ] {
            assert!(args.contains(&flag.to_owned()));
        }
        assert!(!args.iter().any(|arg| arg.starts_with("--cap-add")
            || arg.starts_with("--device")
            || arg.starts_with("--volume")
            || arg == "--privileged"
            || arg == "--pid=host"));
    }
}
