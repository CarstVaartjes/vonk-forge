//! Free memory by block size from `/proc/buddyinfo`, shared by the agent's
//! host checks and the helper's pre-load compaction report.
use crate::generated::FreeMemory;

/// The physically contiguous block size the GPU driver wants.
pub const LARGE_BLOCK_BYTES: u64 = 2 * 1024 * 1024;

/// All free memory and the part in blocks of at least 2 MiB, summed over every
/// node and zone. Each line counts free blocks of `page_size << order` bytes for
/// order 0, 1, ...; the page size is the running kernel's, never assumed. `None`
/// when the text names no zone or a count is not a number.
pub fn free_memory(text: &str, page_size: u64) -> Option<FreeMemory> {
    if page_size == 0 {
        return None;
    }
    let (mut free_bytes, mut free_2mib_block_bytes, mut zones) = (0_u64, 0_u64, 0_usize);
    for line in text.lines().filter(|line| !line.trim().is_empty()) {
        let (_, counts) = line.split_once("zone")?;
        let mut fields = counts.split_ascii_whitespace();
        fields.next()?;
        for (order, count) in fields.enumerate() {
            let block = page_size.checked_shl(u32::try_from(order).ok()?)?;
            let bytes = count.parse::<u64>().ok()?.checked_mul(block)?;
            free_bytes = free_bytes.checked_add(bytes)?;
            if block >= LARGE_BLOCK_BYTES {
                free_2mib_block_bytes = free_2mib_block_bytes.checked_add(bytes)?;
            }
        }
        zones += 1;
    }
    (zones > 0).then_some(FreeMemory {
        free_2mib_block_bytes,
        free_bytes,
    })
}

#[cfg(test)]
mod tests;
