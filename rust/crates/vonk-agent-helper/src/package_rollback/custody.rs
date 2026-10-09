use std::fs::{self, File, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::Path;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};

pub(super) fn now() -> Result<i64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .map_err(|e| e.to_string())
}
pub(super) fn digest(path: &Path) -> Result<String, String> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32)
        .open(path)
        .map_err(|e| e.to_string())?;
    digest_file(&mut file)
}

pub(super) fn digest_file(file: &mut File) -> Result<String, String> {
    let metadata = file.metadata().map_err(|error| error.to_string())?;
    if !metadata.is_file() {
        return Err("package identity is not regular executable data".into());
    }
    let mut hash = Sha256::new();
    let mut remaining = metadata.len() + 1;
    let mut buffer = [0; 65536];
    let deadline = Instant::now() + Duration::from_secs(180);
    while remaining > 0 {
        if Instant::now() >= deadline {
            return Err(
                vonk_agent_protocol::generated::WaitReason::ObservationUnavailable.to_string(),
            );
        }
        let limit = remaining.min(buffer.len() as u64) as usize;
        let count = file
            .read(&mut buffer[..limit])
            .map_err(|error| error.to_string())?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
        remaining -= count as u64;
    }
    let copied = metadata.len() + 1 - remaining;
    if copied != metadata.len() {
        return Err("package identity bytes changed during verification".into());
    }
    Ok(hex::encode(hash.finalize()))
}

pub(super) fn publish_managed(temporary: &Path, destination: &Path) -> Result<(), String> {
    if fs::symlink_metadata(destination).is_ok_and(|metadata| metadata.is_dir()) {
        // Preserve an unexpected managed object without traversing it. Its old
        // shape is neither rollback authority nor a gate on a current request.
        let parent = destination.parent().ok_or("managed parent missing")?;
        fs::rename(
            destination,
            parent.join(format!(".retired-{}", uuid::Uuid::new_v4())),
        )
        .map_err(|error| error.to_string())?;
    }
    fs::rename(temporary, destination).map_err(|error| error.to_string())
}

pub(super) fn safe(path: &Path, directory: bool, owner: u32, mode: u32) -> Result<(), String> {
    let m = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
    if m.file_type().is_symlink()
        || m.is_dir() != directory
        || m.uid() != owner
        || m.mode() & 0o777 & !mode != 0
        || (!directory && (!m.is_file() || m.nlink() != 1))
    {
        return Err("unsafe rollback custody".into());
    }
    Ok(())
}
