//! Staging.

use super::*;

pub(super) fn verify_release_artifact_size(path: &Path, expected: u64) -> Result<(), SetupError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| SetupError::ReleaseSignature)?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() != expected
    {
        return Err(SetupError::ReleaseSignature);
    }
    Ok(())
}

pub(super) fn validated_staging_session(
    executable_path: &Path,
    paths: &InstallPaths,
) -> Result<PathBuf, SetupError> {
    if !executable_path.is_absolute()
        || executable_path.file_name().and_then(|name| name.to_str()) != Some("vonk-spark-setup")
    {
        return Err(SetupError::PrivilegedInput);
    }
    let session = executable_path
        .parent()
        .ok_or(SetupError::PrivilegedInput)?;
    if session.parent() != Some(paths.staging_root.as_path()) {
        return Err(SetupError::PrivilegedInput);
    }
    let session_name = session
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or(SetupError::PrivilegedInput)?;
    let suffix = session_name
        .strip_prefix("vonk-spark-setup.")
        .ok_or(SetupError::PrivilegedInput)?;
    if !(6..=64).contains(&suffix.len()) || !suffix.bytes().all(|byte| byte.is_ascii_alphanumeric())
    {
        return Err(SetupError::PrivilegedInput);
    }
    let expected_owner = paths
        .required_owner
        .unwrap_or_else(|| rustix::process::geteuid().as_raw());
    let session_metadata =
        fs::symlink_metadata(session).map_err(|_| SetupError::PrivilegedInput)?;
    if !session_metadata.file_type().is_dir()
        || session_metadata.file_type().is_symlink()
        || session_metadata.uid() != expected_owner
        || session_metadata.permissions().mode() & 0o777 != 0o700
    {
        return Err(SetupError::PrivilegedInput);
    }
    let package = session.join("vonk-forge-agent.deb");
    for (path, mode) in [(executable_path, 0o700), (package.as_path(), 0o600)] {
        let metadata = fs::symlink_metadata(path).map_err(|_| SetupError::PrivilegedInput)?;
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.nlink() != 1
            || metadata.uid() != expected_owner
            || metadata.permissions().mode() & 0o777 != mode
        {
            return Err(SetupError::PrivilegedInput);
        }
    }
    Ok(package)
}

pub(super) struct StagedPackage {
    pub(super) _directory: TempDir,
    pub(super) path: PathBuf,
}

pub(super) fn secure_tempdir(prefix: &str) -> Result<TempDir, SetupError> {
    tempfile::Builder::new()
        .prefix(prefix)
        .tempdir_in("/var/tmp")
        .map_err(SetupError::PrivilegedWrite)
}

impl StagedPackage {
    pub(super) fn path(&self) -> &Path {
        &self.path
    }
}

pub(super) fn stage_verified_package_from(
    source: &Path,
    expected: &str,
    expected_version: &str,
    expected_architecture: &str,
    require_release_name: bool,
) -> Result<StagedPackage, SetupError> {
    if !source.is_absolute()
        || !valid_sha256(expected)
        || (require_release_name && !valid_package_name(source))
    {
        return Err(SetupError::UnsafePackage);
    }
    let before = fs::symlink_metadata(source).map_err(|_| SetupError::UnsafePackage)?;
    if !before.file_type().is_file()
        || before.file_type().is_symlink()
        || before.nlink() != 1
        || before.len() < 68
        || before.len() > MAX_PACKAGE_BYTES
        || before.permissions().mode() & 0o022 != 0
    {
        return Err(SetupError::UnsafePackage);
    }
    let mut input = OpenOptions::new()
        .read(true)
        .custom_flags(libc_nofollow())
        .open(source)
        .map_err(|_| SetupError::UnsafePackage)?;
    let open = input.metadata().map_err(|_| SetupError::UnsafePackage)?;
    if !same_file(&before, &open) {
        return Err(SetupError::UnsafePackage);
    }
    let directory = secure_tempdir("vonk-spark-package.")?;
    let path = directory
        .path()
        .join(source.file_name().ok_or(SetupError::UnsafePackage)?);
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .map_err(SetupError::PrivilegedWrite)?;
    let mut digest = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    let mut remaining = before.len();
    while remaining > 0 {
        let count = input
            .read(&mut buffer)
            .map_err(|_| SetupError::UnsafePackage)?;
        if count == 0 {
            break;
        }
        total = total
            .checked_add(count as u64)
            .ok_or(SetupError::UnsafePackage)?;
        if total > MAX_PACKAGE_BYTES {
            return Err(SetupError::UnsafePackage);
        }
        remaining = remaining
            .checked_sub(count as u64)
            .ok_or(SetupError::UnsafePackage)?;
        digest.update(&buffer[..count]);
        output
            .write_all(&buffer[..count])
            .map_err(SetupError::PrivilegedWrite)?;
    }
    output.sync_all().map_err(SetupError::PrivilegedWrite)?;
    let after = input.metadata().map_err(|_| SetupError::UnsafePackage)?;
    if !same_file(&open, &after) || total != before.len() {
        return Err(SetupError::UnsafePackage);
    }
    if hex::encode(digest.finalize()) != expected {
        return Err(SetupError::PackageDigest);
    }
    verify_debian_identity(&path, expected_version, expected_architecture)?;
    Ok(StagedPackage {
        _directory: directory,
        path,
    })
}

pub(super) fn valid_package_name(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    let Some(version) = name.strip_prefix("vonk-forge-agent_") else {
        return false;
    };
    let Some((version, architecture)) = version.rsplit_once('_') else {
        return false;
    };
    matches!(architecture, "amd64.deb" | "arm64.deb")
        && !version.is_empty()
        && version.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'+' | b'~' | b':' | b'-')
        })
}

pub(super) fn libc_nofollow() -> i32 {
    0o400000
}

pub(super) fn same_file(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.dev() == right.dev()
        && left.ino() == right.ino()
        && left.nlink() == right.nlink()
        && left.len() == right.len()
        && left.mtime_nsec() == right.mtime_nsec()
        && left.ctime_nsec() == right.ctime_nsec()
}

pub(super) fn verify_regular_file_digest(
    path: &Path,
    expected: &str,
    maximum: u64,
) -> Result<(), SetupError> {
    let before = fs::symlink_metadata(path).map_err(|_| SetupError::UnsafePackage)?;
    if !before.file_type().is_file()
        || before.file_type().is_symlink()
        || before.nlink() != 1
        || before.len() == 0
        || before.len() > maximum
        || before.permissions().mode() & 0o022 != 0
    {
        return Err(SetupError::UnsafePackage);
    }
    let mut input = OpenOptions::new()
        .read(true)
        .custom_flags(libc_nofollow())
        .open(path)
        .map_err(|_| SetupError::UnsafePackage)?;
    let open = input.metadata().map_err(|_| SetupError::UnsafePackage)?;
    if !same_file(&before, &open) {
        return Err(SetupError::UnsafePackage);
    }
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    let mut remaining = before.len();
    while remaining > 0 {
        let count = input
            .read(&mut buffer)
            .map_err(|_| SetupError::UnsafePackage)?;
        if count == 0 {
            break;
        }
        remaining = remaining
            .checked_sub(count as u64)
            .ok_or(SetupError::UnsafePackage)?;
        digest.update(&buffer[..count]);
    }
    let after = input.metadata().map_err(|_| SetupError::UnsafePackage)?;
    if !same_file(&open, &after) || hex::encode(digest.finalize()) != expected {
        return Err(SetupError::PackageDigest);
    }
    Ok(())
}

pub(super) fn verify_debian_identity(
    path: &Path,
    expected_version: &str,
    expected_architecture: &str,
) -> Result<(), SetupError> {
    fn field(path: &Path, name: &str) -> Result<String, SetupError> {
        let output = ProcessCommand::new("/usr/bin/dpkg-deb")
            .args(["--field"])
            .arg(path)
            .arg(name)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
            .map_err(|_| SetupError::PackageFormat)?;
        if !output.status.success() {
            return Err(SetupError::PackageFormat);
        }
        String::from_utf8(output.stdout)
            .map(|value| value.trim().to_owned())
            .map_err(|_| SetupError::PackageFormat)
    }

    let package = field(path, "Package")?;
    let version = field(path, "Version")?;
    let architecture = field(path, "Architecture")?;
    if package != "vonk-forge-agent"
        || version != expected_version
        || architecture != expected_architecture
    {
        return Err(SetupError::PackageIdentity);
    }
    Ok(())
}
