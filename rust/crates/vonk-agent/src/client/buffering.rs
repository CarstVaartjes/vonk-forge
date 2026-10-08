//! Bounded range writes and kernel page-cache hints.

use super::*;

/// Where a distribution object lands and which governor paces its ranges.
pub(super) struct ObjectPlacement<'a> {
    pub(super) destination: &'a Path,
    pub(super) managed_root: &'a Path,
    pub(super) governor: &'a StreamGovernor,
}

/// The last byte of the range that continues a transfer at `offset`.
pub(super) fn range_end(offset: u64, total: u64) -> u64 {
    total
        .saturating_sub(1)
        .min(offset.saturating_add(DISTRIBUTION_RANGE_BYTES - 1))
}

/// Ask the filesystem to reserve the object's remaining space up front, keeping
/// the file's length at what was written (resume reads that length). This
/// avoids fragmentation, and a full disk shows before hours of transfer. A
/// filesystem that cannot reserve is left to allocate as it goes.
#[cfg(target_os = "linux")]
pub(super) fn preallocate(file: &tokio::fs::File, offset: u64, total: u64) {
    if total > offset {
        let _ = rustix::fs::fallocate(
            file,
            rustix::fs::FallocateFlags::KEEP_SIZE,
            offset,
            total - offset,
        );
    }
}

#[cfg(not(target_os = "linux"))]
pub(super) fn preallocate(_file: &tokio::fs::File, _offset: u64, _total: u64) {}

/// Starts writeback of what was written and releases its pages every
/// [`WRITE_BEHIND_BYTES`]. At most one flush runs while the transfer continues.
pub(super) struct WriteBehind {
    pub(super) window: u64,
    pub(super) started: u64,
    pub(super) pending: Option<tokio::task::JoinHandle<std::io::Result<()>>>,
}

impl WriteBehind {
    pub(super) fn new(offset: u64) -> Self {
        Self::with_window(offset, WRITE_BEHIND_BYTES)
    }

    pub(super) fn with_window(offset: u64, window: u64) -> Self {
        Self {
            window,
            started: offset,
            pending: None,
        }
    }

    pub(super) async fn written(
        &mut self,
        output: &mut BufWriter<tokio::fs::File>,
        offset: u64,
    ) -> Result<(), ClientError> {
        if offset - self.started < self.window {
            return Ok(());
        }
        self.finish().await?;
        output.flush().await?;
        let file = output.get_ref().try_clone().await?.into_std().await;
        let (from, length) = (self.started, offset - self.started);
        self.started = offset;
        self.pending = Some(tokio::task::spawn_blocking(move || {
            file.sync_data()?;
            release_pages(&file, from, length);
            Ok(())
        }));
        Ok(())
    }

    pub(super) async fn finish(&mut self) -> Result<(), ClientError> {
        if let Some(pending) = self.pending.take() {
            tokio::time::timeout(CONTROLLER_REQUEST_TIMEOUT, pending)
                .await
                .map_err(|_| ClientError::Retryable)?
                .map_err(|error| std::io::Error::other(error.to_string()))??;
        }
        Ok(())
    }
}

#[cfg(target_os = "linux")]
pub(super) fn release_pages(file: &fs::File, offset: u64, length: u64) {
    let _ = rustix::fs::fadvise(
        file,
        offset,
        std::num::NonZeroU64::new(length),
        rustix::fs::Advice::DontNeed,
    );
}

#[cfg(not(target_os = "linux"))]
pub(super) fn release_pages(_file: &fs::File, _offset: u64, _length: u64) {}
/// Release the page cache for a completed distribution object.
///
/// Best effort on purpose: the object is transferred, verified and receipted,
/// and a failed hint must not turn that into a failed transfer.  A node that
/// keeps the pages is the behaviour that shipped before this call.
pub(super) fn release_distribution_page_cache(path: &Path) {
    if let Ok(file) = std::fs::File::open(path) {
        let _ = rustix::fs::fadvise(&file, 0, None, rustix::fs::Advice::DontNeed);
    }
}
