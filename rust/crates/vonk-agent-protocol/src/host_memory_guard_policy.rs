//! Host guard policy, independent of creator-kit runtime settings.
use std::time::Duration;

// spark-3542 froze 2026-10-07 after NVRM NV_ERR_NO_MEMORY without kernel OOM.
// gb10-oom-research-2026-10-08: GPU allocations bypass cgroup accounting.
// Healthy kits dip to ~3 GiB free; only wedge precursors justify killing.
pub const SAMPLE_INTERVAL: Duration = Duration::from_millis(250);
pub const EXHAUSTED_BYTES: u64 = 256 * 1024 * 1024;
pub const FULL_PSI_PERCENT: f64 = 30.0;
pub const SUSTAINED_PRESSURE: Duration = Duration::from_secs(3);
// Informational only: never changes admission or kit settings.
pub const PRELOAD_MARGIN_BYTES: u64 = 1024 * 1024 * 1024;
pub const COMMAND_TIMEOUT: Duration = Duration::from_secs(1);
pub const JOURNAL_RETRY: Duration = Duration::from_secs(2);
// Bound retained diagnostics after their exact container has been removed.
pub const RECEIPT_COLLECTION_INTERVAL: Duration = Duration::from_secs(60);
