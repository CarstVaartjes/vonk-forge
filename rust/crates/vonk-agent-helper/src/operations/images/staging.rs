//! Collection of owned unpublished receipts under the publication fence.
use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn lock_image_publication(&self) -> Result<File, OperationError> {
        // Lock the directory inode, avoiding another disposable lock filename.
        // All publishers fence collection and staging under this same owner.
        let directory = OpenOptions::new()
            .read(true)
            .custom_flags(
                (rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::NOFOLLOW).bits() as i32,
            )
            .open(&self.roots.runtime_image_receipts)?;
        rustix::fs::flock(
            &directory,
            rustix::fs::FlockOperation::NonBlockingLockExclusive,
        )
        .map_err(|error| OperationError::Io(errno_io(error)))?;
        Ok(directory)
    }

    pub(super) fn collect_image_staging(&self, deadline: Instant) {
        let Ok(entries) = fs::read_dir(&self.roots.runtime_image_receipts) else {
            return;
        };
        for entry in entries {
            if Instant::now() >= deadline {
                break;
            }
            let Ok(entry) = entry else {
                continue;
            };
            let name = entry.file_name();
            let Some(uuid) = name
                .to_str()
                .and_then(|name| name.strip_prefix(".receipt-"))
                .and_then(|name| name.strip_suffix(".tmp"))
            else {
                continue;
            };
            let Ok(identity) = uuid::Uuid::parse_str(uuid) else {
                continue;
            };
            if identity.to_string() != uuid {
                continue;
            }
            let Ok(metadata) = fs::symlink_metadata(entry.path()) else {
                continue;
            };
            if !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.uid() != rustix::process::geteuid().as_raw()
                || metadata.mode() & 0o777 != 0o600
            {
                continue;
            }
            // No active publisher can own this staging file while the fence
            // is held. A failed unlink remains pending for the next bounded
            // collection; it never changes verified Docker availability.
            if let Err(error) = fs::remove_file(entry.path()) {
                eprintln!("image receipt staging collection unavailable: {error}");
            }
        }
    }
}
