//! RDMA traffic per device over the window since the last report.
//!
//! A cabled port is two RDMA devices; whether both carry traffic during a
//! multi-node run is for the Controller to judge, from the byte deltas this
//! reports. The previous reading lives in memory only: after an agent restart
//! the first report has no window, and a byte counter that went backwards (a
//! device reset) has none for that device. Such a device still reports its
//! negotiated rate, a fact that needs no window, with bytes, window and errors
//! unset.
use std::{path::Path, sync::Mutex, time::Instant};

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
            .map(|device| {
                let (rate_mbps, errors) = read_fabric_health(root, &device.name);
                (device, total(device), rate_mbps, errors)
            })
            .collect::<Vec<_>>();
        let totals = now_readings
            .iter()
            .filter_map(|(device, bytes, _, errors)| {
                Some((device.name.clone(), (*bytes)?, *errors))
            })
            .collect();
        let mut previous = self
            .previous
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let before = previous.replace((now, totals));
        let window = before.as_ref().map_or(0, |(then, _)| {
            now.saturating_duration_since(*then).as_secs()
        });
        let window_seconds = u32::try_from(window).ok().filter(|seconds| *seconds > 0);
        now_readings
            .iter()
            .filter_map(|(device, bytes, rate_mbps, errors_after)| {
                let port = device.port.clone()?;
                // The delta needs a window and an earlier reading; without
                // them (restart, counter reset, unreadable) only the rate goes.
                let delta = (|| {
                    let (_, before) = before.as_ref()?;
                    let (_, first, errors_before) =
                        before.iter().find(|(name, ..)| *name == device.name)?;
                    let bytes = (*bytes)?.checked_sub(*first)?;
                    let errors = errors_after
                        .zip(*errors_before)
                        .and_then(|(after, before)| after.checked_sub(before));
                    Some((bytes, window_seconds?, errors))
                })();
                let (bytes, window_seconds, errors) = match delta {
                    Some((bytes, window, errors)) => (Some(bytes), Some(window), errors),
                    None => (None, None, None),
                };
                Some(FabricDevice {
                    port,
                    active: device.active,
                    bytes,
                    window_seconds,
                    rate_mbps: *rate_mbps,
                    errors,
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
