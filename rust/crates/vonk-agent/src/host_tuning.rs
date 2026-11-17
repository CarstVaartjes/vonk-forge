//! The facts behind the host checks: which listed units run, the installed
//! snaps, the watermark boost, the boot splash and free memory by block size.
//! Informative evidence only; each reading that fails is unknown on its own
//! and never fails the inventory.
use std::{fs, io::Read, path::Path, time::Duration};

use vonk_agent_protocol::generated::{ActiveUnit, HostTuning, HostUnit};

use crate::process::{ProcessRunner, Program};

/// Every unit the host checks name, in contract order.
const UNITS: [HostUnit; 12] = [
    HostUnit::BluetoothService,
    HostUnit::CupsService,
    HostUnit::CupsSocket,
    HostUnit::CupsPath,
    HostUnit::CupsBrowsedService,
    HostUnit::SnapdService,
    HostUnit::SnapdSocket,
    HostUnit::SnapdSeededService,
    HostUnit::SnapdSnapRepairTimer,
    HostUnit::DgxDashboardService,
    HostUnit::DgxDashboardAdminService,
    HostUnit::FwupdRefreshTimer,
];

/// A new contract unit is a compile error here until `UNITS` names it.
const fn position(unit: HostUnit) -> usize {
    match unit {
        HostUnit::BluetoothService => 0,
        HostUnit::CupsService => 1,
        HostUnit::CupsSocket => 2,
        HostUnit::CupsPath => 3,
        HostUnit::CupsBrowsedService => 4,
        HostUnit::SnapdService => 5,
        HostUnit::SnapdSocket => 6,
        HostUnit::SnapdSeededService => 7,
        HostUnit::SnapdSnapRepairTimer => 8,
        HostUnit::DgxDashboardService => 9,
        HostUnit::DgxDashboardAdminService => 10,
        HostUnit::FwupdRefreshTimer => 11,
    }
}

const MAX_SNAPS: usize = 128;
/// The contract's memory bound (`host_tuning.MemoryBytes`, 16 TiB).
const MAX_MEMORY_BYTES: u64 = 16 << 40;
/// /proc files are small; a longer read is not the file we expect.
const READ_LIMIT: u64 = 64 * 1024;

/// `root` is `/` on a Spark (tests pass a directory shaped like it) and
/// `page_size` the running kernel's.
pub fn observe<R: ProcessRunner>(runner: &R, root: &Path, page_size: u64) -> HostTuning {
    let read = |path: &str| -> Option<String> {
        let mut text = String::new();
        fs::File::open(root.join(path))
            .ok()?
            .take(READ_LIMIT)
            .read_to_string(&mut text)
            .ok()?;
        Some(text)
    };
    HostTuning {
        active_units: active_units(runner),
        snaps: snaps(&root.join("snap")),
        watermark_boost_factor: read("proc/sys/vm/watermark_boost_factor")
            .and_then(|text| text.trim().parse().ok())
            .filter(|factor| *factor <= i32::MAX as u32),
        kernel_splash: read("proc/cmdline")
            .map(|text| text.split_ascii_whitespace().any(|word| word == "splash")),
        free_memory: read("proc/buddyinfo")
            .and_then(|text| vonk_agent_protocol::buddyinfo::free_memory(&text, page_size)),
    }
}

/// One `systemctl show` for all units. A unit that is not installed reads
/// inactive; no answer at all is unknown.
fn active_units<R: ProcessRunner>(runner: &R) -> Option<Vec<ActiveUnit>> {
    let mut arguments = vec![
        "show".to_owned(),
        "--property=Id,ActiveState,MemoryCurrent".to_owned(),
        "--".to_owned(),
    ];
    arguments.extend(UNITS.iter().map(ToString::to_string));
    let output = runner
        .run(Program::Systemctl, &arguments, Duration::from_secs(5))
        .ok()
        .filter(|output| output.success)?;
    parse_show(std::str::from_utf8(&output.stdout).ok()?)
}

/// `systemctl show` prints one `Key=value` block per unit, blank-line separated.
fn parse_show(text: &str) -> Option<Vec<ActiveUnit>> {
    let mut active: [Option<ActiveUnit>; UNITS.len()] = std::array::from_fn(|_| None);
    let mut answered = 0;
    for block in text.split("\n\n") {
        let property = |key: &str| {
            block
                .lines()
                .find_map(|line| line.strip_prefix(key)?.strip_prefix('='))
        };
        let Some(unit) = property("Id").and_then(|id| id.parse::<HostUnit>().ok()) else {
            continue;
        };
        answered += 1;
        if block.lines().any(|line| line == "ActiveState=active") {
            active[position(unit)] = Some(ActiveUnit {
                unit,
                // An unset value some systemd versions print as u64::MAX is
                // unknown, never 16 EiB (the contract bounds it to 16 TiB).
                memory_bytes: property("MemoryCurrent")
                    .and_then(|value| value.parse().ok())
                    .filter(|bytes| *bytes <= MAX_MEMORY_BYTES),
            });
        }
    }
    (answered > 0).then(|| active.into_iter().flatten().collect())
}

/// Snap names from `/snap/<name>` (no snapd call), sorted; `None` without `/snap`.
fn snaps(directory: &Path) -> Option<Vec<String>> {
    let mut names: Vec<String> = fs::read_dir(directory)
        .ok()?
        .filter_map(Result::ok)
        .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_dir()))
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| name != "bin" && snap_name(name))
        .collect();
    names.sort();
    names.truncate(MAX_SNAPS);
    Some(names)
}

fn snap_name(name: &str) -> bool {
    name.len() <= 40
        && name
            .bytes()
            .next()
            .is_some_and(|first| first.is_ascii_lowercase() || first.is_ascii_digit())
        && name
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}

#[cfg(test)]
mod tests;
