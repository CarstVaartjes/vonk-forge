//! Host guard policy, independent of creator-kit runtime settings.
use std::time::Duration;

// spark-3542 froze 2026-10-07 after NVRM NV_ERR_NO_MEMORY without kernel OOM.
// gb10-oom-research-2026-10-08: GPU allocations bypass cgroup accounting.
// The NVRM line alone is non-fatal on GB10 (once per normal model load): it kills
// only with MemAvailable exhausted in the same sample (at once) or full PSI
// sustained for SUSTAINED_PRESSURE below STALL_AVAILABLE_BYTES; a load's brief
// PSI spike is not enough.
// Healthy kits dip to ~3 GiB free; only wedge precursors justify killing.
pub const SAMPLE_INTERVAL: Duration = Duration::from_millis(250);
pub const EXHAUSTED_BYTES: u64 = 256 * 1024 * 1024;
pub const FULL_PSI_PERCENT: f64 = 30.0;
pub const SUSTAINED_PRESSURE: Duration = Duration::from_secs(3);
// Sustained full PSI counts only with MemAvailable below this bar: a load's
// KV-cache allocation reclaims page cache under full PSI with 27-35 GB left
// (spark-3542 2026-10-15); the 2026-10-13 wedge ran with ~10-15 GB left and
// healthy runs settle at ~19 GB.
pub const STALL_AVAILABLE_BYTES: u64 = 16 * 1024 * 1024 * 1024;
// Informational only: never changes admission or kit settings.
pub const PRELOAD_MARGIN_BYTES: u64 = 1024 * 1024 * 1024;
pub const COMMAND_TIMEOUT: Duration = Duration::from_secs(1);
pub const JOURNAL_RETRY: Duration = Duration::from_secs(2);
// Bound retained diagnostics after their exact container has been removed.
pub const RECEIPT_COLLECTION_INTERVAL: Duration = Duration::from_secs(60);
