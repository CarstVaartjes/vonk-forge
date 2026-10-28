//! Host guard policy, independent of creator-kit runtime settings.
use std::time::Duration;

// spark-3542 froze 2026-10-07 after NVRM NV_ERR_NO_MEMORY without kernel OOM.
// gb10-oom-research-2026-10-08: GPU allocations bypass cgroup accounting.
//
// Swap-aware policy R1-R5 (owner approved 2026-10-28). A load that swaps
// steadily is a cushion working, not a wedge: MemAvailable leaves out free
// swap, and kswapd reclaim alone raises full PSI past 30%. The shapes follow
// systemd-oomd (full avg10 60% for 30 s; memory AND swap low), Meta oomd
// (avg60 80% for 60 s not falling; swap below 10% while still swapping out)
// and earlyoom (memory AND swap below 10%). Every threshold below marked
// CALIBRATABLE is a starting value to measure on hardware (kill evidence
// carries the readings), never a kit setting.
pub const SAMPLE_INTERVAL: Duration = Duration::from_millis(250);

// R1, no swap (or swap unreadable): MemAvailable below this for
// SUSTAINED_PRESSURE, the pre-swap rule. Healthy kits dip to ~3 GiB free.
pub const EXHAUSTED_BYTES: u64 = 256 * 1024 * 1024;
pub const SUSTAINED_PRESSURE: Duration = Duration::from_secs(3);
// R1 with swap: MemAvailable below 1 GiB AND free swap below the larger of
// 2 GiB and 10% of swap, for 2 s. CALIBRATABLE.
pub const SWAP_EXHAUSTED_MEMORY_BYTES: u64 = 1024 * 1024 * 1024;
pub const SWAP_EXHAUSTED_FREE_FLOOR_BYTES: u64 = 2 * 1024 * 1024 * 1024;
pub const SWAP_EXHAUSTED_FREE_PERCENT: u64 = 10;
pub const SWAP_EXHAUSTED_SUSTAIN: Duration = Duration::from_secs(2);
// R2: swap more than 90% used AND still swapping out AND MemAvailable below
// 4 GiB, for 3 s. CALIBRATABLE.
pub const SWAP_FILLING_USED_PERCENT: u64 = 90;
pub const SWAP_FILLING_MEMORY_BYTES: u64 = 4 * 1024 * 1024 * 1024;
pub const SWAP_FILLING_SUSTAIN: Duration = Duration::from_secs(3);
// R3, thrash: swap-ins plus workingset refaults (anon + file) at or above this
// rate AND full avg10 above 60% AND MemAvailable + free swap below 8 GiB, for
// 10 s. Refaults, not steady swap-out, are the freeze precursor. CALIBRATABLE.
pub const THRASH_PAGES_PER_SECOND: u64 = 50_000;
pub const THRASH_FULL_PSI_PERCENT: f64 = 60.0;
pub const THRASH_HEADROOM_BYTES: u64 = 8 * 1024 * 1024 * 1024;
pub const THRASH_SUSTAIN: Duration = Duration::from_secs(10);
// R4, the PSI backstop. Serving: full avg10 above 60% for 30 s. A declared
// loading phase: full avg60 above 80% for 60 s while avg10 is not falling
// (avg10 at least 0.85 x avg60). R1 and R2 apply in both. CALIBRATABLE.
pub const SERVING_FULL_PSI_PERCENT: f64 = 60.0;
pub const SERVING_PRESSURE_SUSTAIN: Duration = Duration::from_secs(30);
pub const LOADING_FULL_AVG60_PERCENT: f64 = 80.0;
pub const LOADING_PRESSURE_SUSTAIN: Duration = Duration::from_secs(60);
pub const PSI_FALLING_RATIO: f64 = 0.85;
// R5: the NVRM NV_ERR_NO_MEMORY line is non-fatal on GB10 (once per normal
// load); it kills within SUSTAINED_PRESSURE of the line only together with
// R1's condition, MemFree below this, or R3's condition. CALIBRATABLE.
pub const DRIVER_FAILURE_FREE_BYTES: u64 = 1024 * 1024 * 1024;
// R5's corroboration is sustained like R1, never one sample: a healthy load's
// derived MemFree touches ~0.9 GB at its NVRM line (GLM mosaic a0 2026-10-28).
pub const DRIVER_FAILURE_SUSTAIN: Duration = Duration::from_secs(2);
// The line counts this long: corroboration that starts within
// SUSTAINED_PRESSURE of it still completes its DRIVER_FAILURE_SUSTAIN.
pub const DRIVER_LINE_WINDOW: Duration =
    Duration::from_secs(SUSTAINED_PRESSURE.as_secs() + DRIVER_FAILURE_SUSTAIN.as_secs());
// Kill evidence keeps one reading per RECENT_SAMPLE_SPACING for the last
// RECENT_SAMPLES of them, so each kill can be judged afterwards.
pub const RECENT_SAMPLES: usize = 6;
pub const RECENT_SAMPLE_SPACING: Duration = Duration::from_secs(1);
/// The kill evidence's two packed properties: each value is `field:reading`
/// pairs joined by `+`, one summary token the agent keeps as one exit fact
/// (the agent keeps at most MAX_PREFLIGHT_PROPERTIES facts per failure).
pub const PRESSURE_PROPERTY: &str = "pressure_readings";
pub const RECENT_PROPERTY: &str = "recent_samples";
/// The readings `PRESSURE_PROPERTY` packs, in order.
pub const PRESSURE_FIELDS: [&str; 8] = [
    "loading",
    "mem_free_mib",
    "swap_total_mib",
    "swap_free_mib",
    "memory_full_avg60",
    "swapin_per_s",
    "swapout_per_s",
    "refault_per_s",
];
/// The series `RECENT_PROPERTY` packs (each series' samples joined by `-`,
/// oldest first).
pub const RECENT_FIELDS: [&str; 4] = [
    "recent_available_mib",
    "recent_free_mib",
    "recent_swap_free_mib",
    "recent_full_avg10",
];
// A Spark is loading while the agent's page-cache drop cadence was heard
// within this window. The agent waits PAGE_CACHE_DROP_INTERVAL after each
// answer and an answer takes up to PAGE_CACHE_DROP_REPLY_TIMEOUT, so the
// longest healthy gap between asks is their sum; one more interval is margin.
pub const LOADING_PHASE_WINDOW: Duration = Duration::from_secs(
    2 * PAGE_CACHE_DROP_INTERVAL.as_secs() + PAGE_CACHE_DROP_REPLY_TIMEOUT.as_secs(),
);
// Informational only: never changes admission or kit settings.
pub const PRELOAD_MARGIN_BYTES: u64 = 1024 * 1024 * 1024;
pub const COMMAND_TIMEOUT: Duration = Duration::from_secs(1);
pub const JOURNAL_RETRY: Duration = Duration::from_secs(2);
// Bound retained diagnostics after their exact container has been removed.
pub const RECEIPT_COLLECTION_INTERVAL: Duration = Duration::from_secs(60);
// Page-cache drops during a workload's load (owner 2026-10-27). GB10 counts
// reclaimable page cache as used: vLLM's free-memory check and NVRM
// allocations fail at ~6-7 GB MemAvailable with 7-14 GB cached (NVIDIA's DGX
// Spark vLLM troubleshooting; vLLM #35313). The agent asks once before the
// container starts and every interval until the workload is ready or its
// start ends; the helper syncs within the bound, then drops.
pub const PAGE_CACHE_DROP_INTERVAL: Duration = Duration::from_secs(5);
// A slow disk never stalls a start: the drop proceeds after this bound and
// reclaims only clean pages.
pub const PAGE_CACHE_SYNC_TIMEOUT: Duration = Duration::from_secs(2);
// One drop answers within the sync bound plus the drop itself.
pub const PAGE_CACHE_DROP_REPLY_TIMEOUT: Duration = Duration::from_secs(15);
