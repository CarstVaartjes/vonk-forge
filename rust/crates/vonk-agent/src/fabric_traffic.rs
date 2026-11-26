//! RDMA traffic per device over the window since the last report.
//!
//! A cabled port is two RDMA devices; whether both carry traffic during a
//! multi-node run is for the Controller to judge, from the byte deltas this
//! reports. The previous reading lives in memory only: after an agent restart
//! the first report has no window and carries nothing, and a counter that went
//! backwards (a device reset) is skipped for that report.
use std::{
    path::{Path, PathBuf},
    sync::Mutex,
    time::Instant,
};

use vonk_agent_protocol::generated::FabricDevice;

use crate::telemetry::host_readings::{
    FabricDeviceReading, read_fabric_devices, read_fabric_health,
};

/// Cumulative received plus transmitted bytes and link and transport errors
/// (`None` when unreadable) per device, and when read.
type Reading = (Instant, Vec<(String, u64, Option<u64>)>);

#[derive(Default)]
pub struct Meter {
    previous: Mutex<Option<Reading>>,
}

impl Meter {
    /// The traffic of every device since the previous call.
    pub fn observe(&self, root: &Path) -> Vec<FabricDevice> {
        self.observe_at(root, Instant::now())
    }

    pub fn observe_at(&self, root: &Path, now: Instant) -> Vec<FabricDevice> {
        let Some(devices) = read_fabric_devices(root) else {
            return Vec::new();
        };
        let now_readings = devices
            .iter()
            .filter_map(|device| {
                let (rate_mbps, errors) = read_fabric_health(root, &device.name);
                Some((device.name.clone(), total(device)?, rate_mbps, errors))
            })
            .collect::<Vec<_>>();
        let totals = now_readings
            .iter()
            .map(|(name, bytes, _, errors)| (name.clone(), *bytes, *errors))
            .collect();
        let mut previous = self
            .previous
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let before = previous.replace((now, totals));
        let Some((then, before)) = before else {
            return Vec::new();
        };
        let window = now.saturating_duration_since(then).as_secs();
        if window == 0 {
            return Vec::new();
        }
        let Ok(window_seconds) = u32::try_from(window) else {
            return Vec::new();
        };
        devices
            .iter()
            .filter_map(|device| {
                let (_, after, rate_mbps, errors_after) = now_readings
                    .iter()
                    .find(|(name, ..)| *name == device.name)?;
                let (_, first, errors_before) =
                    before.iter().find(|(name, ..)| *name == device.name)?;
                Some(FabricDevice {
                    port: device.port.clone()?,
                    active: device.active,
                    bytes: after.checked_sub(*first)?,
                    window_seconds,
                    rate_mbps: *rate_mbps,
                    errors: errors_after
                        .zip(*errors_before)
                        .and_then(|(after, before)| after.checked_sub(before)),
                })
            })
            .take(16)
            .collect()
    }
}

/// One sysfs observation at a time per process, bounded in time. A hung read
/// keeps its permit until it ends, so later passes report no devices without
/// spawning another blocked worker; the lane never waits on sysfs.
pub async fn observe_bounded(meter: std::sync::Arc<Meter>, root: PathBuf) -> Vec<FabricDevice> {
    static SLOT: tokio::sync::Semaphore = tokio::sync::Semaphore::const_new(1);
    observe_with_slot(meter, root, std::time::Duration::from_secs(5), &SLOT).await
}

async fn observe_with_slot(
    meter: std::sync::Arc<Meter>,
    root: PathBuf,
    budget: std::time::Duration,
    slot: &'static tokio::sync::Semaphore,
) -> Vec<FabricDevice> {
    let Ok(permit) = slot.try_acquire() else {
        eprintln!("vonk-agent: previous fabric observation still running; reporting no devices");
        return Vec::new();
    };
    let work = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        meter.observe(&root)
    });
    match tokio::time::timeout(budget, work).await {
        Ok(Ok(devices)) => devices,
        Ok(Err(error)) => {
            eprintln!("vonk-agent: fabric observation failed: {error}");
            Vec::new()
        }
        Err(elapsed) => {
            eprintln!("vonk-agent: fabric observation timed out ({elapsed}); reporting no devices");
            Vec::new()
        }
    }
}

fn total(device: &FabricDeviceReading) -> Option<u64> {
    device.rx_bytes?.checked_add(device.tx_bytes?)
}

#[cfg(test)]
mod tests;
