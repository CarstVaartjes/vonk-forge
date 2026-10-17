//! Range checkpoints, positioned writes and kernel page-cache hints.

use super::*;
use std::os::unix::fs::FileExt;

/// Where a distribution object lands and which governor paces its ranges.
pub(super) struct ObjectPlacement<'a> {
    pub(super) destination: &'a Path,
    pub(super) managed_root: &'a Path,
    pub(super) governor: &'a StreamGovernor,
}

/// The last byte of grid range `index`: ranges are `range_bytes` long from
/// the object's first byte, and the last one stops at its final byte.
pub(super) fn range_end(index: u64, range_bytes: u64, total: u64) -> u64 {
    total.saturating_sub(1).min(
        index
            .saturating_mul(range_bytes)
            .saturating_add(range_bytes - 1),
    )
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

/// Names the range record behind an object's bytes in its `.partial` file.
const RANGE_RECORD_MAGIC: &[u8; 8] = b"vonkrng1";
/// The magic and the size of the ranges the record's bits stand for.
const RANGE_RECORD_HEADER: u64 = 16;
/// Most bitmap bytes a record may claim; a longer one is damage.
const RANGE_RECORD_MAX_BITMAP: u64 = 1024 * 1024;

/// What one range request still has to fetch: `start..=end`, the rest of grid
/// range `index`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct PendingRange {
    pub(super) index: u64,
    pub(super) start: u64,
    pub(super) end: u64,
}

fn bitmap_len(total: u64, range_bytes: u64) -> u64 {
    total.div_ceil(range_bytes).div_ceil(8)
}

/// The checkpoint of a `.partial` file whose ranges land out of order.
///
/// The file's length says what it holds. At most the object's length, it is
/// a contiguous prefix and nothing was ever written past it: the checkpoint
/// of an earlier agent, and the start of every transfer. Exactly the object's
/// length plus a record, the record behind the object's bytes names the
/// ranges whose bytes are written and synced. Any other length is damage and
/// starts over, which is also how an earlier agent reads every longer file:
/// after a rollback it fetches again rather than trusting a gap.
///
/// A transfer with one range left to fetch writes it in order and keeps the
/// prefix (no record), so it resumes from its exact length after a restart.
///
/// Order: the prefix is synced, then the record is written and the file
/// synced before any byte lands out of order, so the length never says
/// "prefix" while a gap exists. A range's bit is set only after its bytes are
/// synced, and bits only turn on: a torn or reordered record write can only
/// drop a bit, and that range is fetched again.
pub(super) struct RangeRecord {
    file: Arc<fs::File>,
    total: u64,
    range_bytes: u64,
    bitmap: Mutex<Vec<u8>>,
    /// Whether the file holds the record; without it, its length is a prefix.
    recorded: bool,
}

impl RangeRecord {
    /// Adopt the checkpoint of `length` bytes and name the ranges still to fetch.
    pub(super) async fn adopt(
        file: Arc<fs::File>,
        length: u64,
        total: u64,
        range_bytes: u64,
    ) -> Result<(Self, Vec<PendingRange>), ClientError> {
        blocking(move || {
            if let Some((range_bytes, bitmap)) = read_record(&file, length, total)? {
                let record = Self {
                    file,
                    total,
                    range_bytes,
                    bitmap: Mutex::new(bitmap),
                    recorded: true,
                };
                let pending = record.pending(0);
                return Ok((record, pending));
            }
            // The checkpoint is disposable. Its handle has passed the
            // private, single-link, no-follow custody checks.
            let prefix = if length > total {
                file.set_len(0)?;
                0
            } else {
                length
            };
            let mut bitmap = vec![0; bitmap_len(total, range_bytes) as usize];
            for index in 0..total.div_ceil(range_bytes) {
                if range_end(index, range_bytes, total) < prefix {
                    bitmap[(index / 8) as usize] |= 1 << (index % 8);
                }
            }
            let mut record = Self {
                file,
                total,
                range_bytes,
                bitmap: Mutex::new(bitmap),
                recorded: false,
            };
            let pending = record.pending(prefix);
            // One range left continues the prefix in order: nothing can land
            // out of order, so the length stays the checkpoint.
            if pending.len() > 1 {
                record.recorded = true;
                record.file.sync_data()?;
                let mut header = RANGE_RECORD_MAGIC.to_vec();
                header.extend_from_slice(&range_bytes.to_le_bytes());
                header.extend_from_slice(&record.lock());
                record.file.write_all_at(&header, total)?;
                record.file.sync_all()?;
            }
            Ok((record, pending))
        })
        .await
    }

    /// The ranges without a bit, each from `prefix` at the earliest.
    fn pending(&self, prefix: u64) -> Vec<PendingRange> {
        let bitmap = self.lock();
        (0..self.total.div_ceil(self.range_bytes))
            .filter(|index| bitmap[(index / 8) as usize] & (1 << (index % 8)) == 0)
            .map(|index| PendingRange {
                index,
                start: (index * self.range_bytes).max(prefix),
                end: range_end(index, self.range_bytes, self.total),
            })
            .collect()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Vec<u8>> {
        self.bitmap
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Write accepted bytes at their place in the object.
    pub(super) async fn write(&self, at: u64, bytes: Vec<u8>) -> Result<(), ClientError> {
        let file = self.file.clone();
        blocking(move || file.write_all_at(&bytes, at)).await
    }

    /// Sync a range's bytes, release their pages and set its bit.
    pub(super) async fn complete(&self, range: PendingRange) -> Result<(), ClientError> {
        let file = self.file.clone();
        let from = range.index * self.range_bytes;
        let length = range.end + 1 - from;
        blocking(move || {
            file.sync_data()?;
            release_pages(&file, from, length);
            Ok(())
        })
        .await?;
        if !self.recorded {
            return Ok(());
        }
        let byte = (range.index / 8) as usize;
        let value = {
            let mut bitmap = self.lock();
            bitmap[byte] |= 1 << (range.index % 8);
            bitmap[byte]
        };
        let file = self.file.clone();
        let at = self.total + RANGE_RECORD_HEADER + byte as u64;
        blocking(move || file.write_all_at(&[value], at)).await
    }

    /// Persist the bits set so far; their ranges' bytes are already synced.
    pub(super) async fn sync(&self) -> Result<(), ClientError> {
        let file = self.file.clone();
        blocking(move || file.sync_data()).await
    }

    /// Every range is synced: drop the record and leave the object's bytes.
    pub(super) async fn finish(&self) -> Result<(), ClientError> {
        let file = self.file.clone();
        let total = self.total;
        blocking(move || {
            file.set_len(total)?;
            file.sync_all()
        })
        .await
    }
}

/// The record behind `total` bytes of a `length`-byte file, if it holds one.
fn read_record(
    file: &fs::File,
    length: u64,
    total: u64,
) -> std::io::Result<Option<(u64, Vec<u8>)>> {
    if length < total + RANGE_RECORD_HEADER {
        return Ok(None);
    }
    let (mut magic, mut range_bytes) = ([0_u8; 8], [0_u8; 8]);
    file.read_exact_at(&mut magic, total)?;
    file.read_exact_at(&mut range_bytes, total + 8)?;
    let range_bytes = u64::from_le_bytes(range_bytes);
    if &magic != RANGE_RECORD_MAGIC || range_bytes == 0 {
        return Ok(None);
    }
    let bitmap_bytes = bitmap_len(total, range_bytes);
    if bitmap_bytes > RANGE_RECORD_MAX_BITMAP
        || length != total + RANGE_RECORD_HEADER + bitmap_bytes
    {
        return Ok(None);
    }
    let mut bitmap = vec![0; bitmap_bytes as usize];
    file.read_exact_at(&mut bitmap, total + RANGE_RECORD_HEADER)?;
    Ok(Some((range_bytes, bitmap)))
}

async fn blocking<T: Send + 'static>(
    work: impl FnOnce() -> std::io::Result<T> + Send + 'static,
) -> Result<T, ClientError> {
    Ok(tokio::task::spawn_blocking(work)
        .await
        .map_err(|error| std::io::Error::other(error.to_string()))??)
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
