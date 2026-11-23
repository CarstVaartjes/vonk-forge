//! `SecPageTables` after workload stops that left the Spark idle.
//!
//! Page-table pages a workload leaves behind (`SecPageTables` in
//! `/proc/meminfo`) are not freed when it exits, can each pin a large free
//! block, and only a reboot returns them. After every completed workload stop
//! the ledger reads `SecPageTables` and, when no GPU compute process runs (the
//! `nvidia-smi` reading telemetry already takes), keeps the first such reading
//! of this boot and the latest. The Controller derives the advice; nothing here
//! refuses or changes anything, and a reading that cannot be taken is absent.
//!
//! The readings belong to one boot and survive an agent restart in one small
//! file, because the agent is upgraded far more often than the Spark reboots.
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::Mutex,
};

use vonk_agent_protocol::generated::MemoryFragmentation;

const FILE: &str = "memory-readings.json";
const BOOT_ID: &str = "proc/sys/kernel/random/boot_id";
/// /proc files are small; a longer read is not the file we expect.
const READ_LIMIT: u64 = 256 * 1024;

pub struct Ledger {
    /// The `/` of the host (a directory shaped like it in tests).
    root: PathBuf,
    directory: PathBuf,
    page_size: u32,
    readings: Mutex<Option<MemoryFragmentation>>,
}

impl Ledger {
    /// The ledger saved in `directory`, or an empty one: a missing, unreadable
    /// or other-boot file is the same as none.
    pub fn open(root: &Path, directory: &Path, page_size: u32) -> Self {
        let readings = fs::read(directory.join(FILE))
            .ok()
            .and_then(|bytes| serde_json::from_slice::<MemoryFragmentation>(&bytes).ok())
            .filter(|saved| Some(saved.boot_id.clone()) == boot_id(root));
        Self {
            root: root.to_owned(),
            directory: directory.to_owned(),
            page_size,
            readings: Mutex::new(readings),
        }
    }

    /// A workload stop is done. `idle` is whether no GPU compute process runs
    /// now; only an idle stop is a reading, since a workload still up holds
    /// its own page tables.
    pub fn stop_ended(&self, idle: bool) {
        let (Some(boot), true) = (boot_id(&self.root), idle) else {
            return;
        };
        let Some(sec_page_tables_bytes) =
            read(&self.root, "proc/meminfo").and_then(|text| sec_page_tables_bytes(&text))
        else {
            return;
        };
        let mut readings = self
            .readings
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let first = match readings.as_ref() {
            Some(saved) if saved.boot_id == boot => saved.idle_stop_first_bytes,
            _ => sec_page_tables_bytes,
        };
        let updated = MemoryFragmentation {
            boot_id: boot,
            page_size_bytes: self.page_size,
            idle_stop_first_bytes: first,
            idle_stop_latest_bytes: sec_page_tables_bytes,
        };
        let saved = serde_json::to_vec(&updated)
            .map_err(std::io::Error::other)
            .and_then(|bytes| crate::identity::atomic_private_write(&self.directory, FILE, &bytes));
        if let Err(error) = saved {
            // The readings stay in memory; a restart then starts them afresh.
            eprintln!("vonk-agent: memory readings not saved: {error}");
        }
        *readings = Some(updated);
    }

    /// The readings of this boot; `None` before the first idle stop.
    pub fn snapshot(&self) -> Option<MemoryFragmentation> {
        self.readings
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }
}

fn boot_id(root: &Path) -> Option<String> {
    crate::telemetry::read_boot_id(&root.join(BOOT_ID))
        .ok()
        .map(|id| id.to_string())
}

/// The `SecPageTables:` line of `/proc/meminfo` (kB), in bytes.
pub fn sec_page_tables_bytes(meminfo: &str) -> Option<u64> {
    let line = meminfo
        .lines()
        .find_map(|line| line.strip_prefix("SecPageTables:"))?;
    let mut fields = line.split_ascii_whitespace();
    let kib = fields.next()?.parse::<u64>().ok()?;
    (fields.next() == Some("kB"))
        .then(|| kib.checked_mul(1024))
        .flatten()
}

fn read(root: &Path, path: &str) -> Option<String> {
    let mut text = String::new();
    fs::File::open(root.join(path))
        .ok()?
        .take(READ_LIMIT)
        .read_to_string(&mut text)
        .ok()?;
    Some(text)
}

#[cfg(test)]
mod tests;
