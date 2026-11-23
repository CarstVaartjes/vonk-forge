//! RDMA traffic per device over the window since the last report.
//!
//! A cabled port is two RDMA devices; whether both carry traffic during a
//! multi-node run is for the Controller to judge, from the byte deltas this
//! reports. The previous reading lives in memory only: after an agent restart
//! the first report has no window and carries nothing, and a counter that went
//! backwards (a device reset) is skipped for that report.
use std::{path::Path, sync::Mutex, time::Instant};

use vonk_agent_protocol::generated::FabricDevice;

use crate::telemetry::host_readings::{FabricDeviceReading, read_fabric_devices};

/// Cumulative received plus transmitted bytes per device, and when read.
type Reading = (Instant, Vec<(String, u64)>);

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
        let totals = devices
            .iter()
            .filter_map(|device| Some((device.name.clone(), total(device)?)))
            .collect::<Vec<_>>();
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
                let after = total(device)?;
                let first = before.iter().find(|(name, _)| *name == device.name)?.1;
                Some(FabricDevice {
                    port: device.port.clone()?,
                    active: device.active,
                    bytes: after.checked_sub(first)?,
                    window_seconds,
                })
            })
            .take(16)
            .collect()
    }
}

fn total(device: &FabricDeviceReading) -> Option<u64> {
    device.rx_bytes?.checked_add(device.tx_bytes?)
}

#[cfg(test)]
mod tests;
