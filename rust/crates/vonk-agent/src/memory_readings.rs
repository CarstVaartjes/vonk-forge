//! Memory fragmentation readings around workload loads and stops.
//!
//! Page-table pages a workload leaves behind (`SecPageTables` in
//! `/proc/meminfo`) are not freed when it exits, fragment memory, and only a
//! reboot returns them. The ledger reads `SecPageTables` and free memory by
//! block size before and after every workload start and stop, keeps the latest
//! of each and the first and latest after-stop readings taken while no other
//! workload was resident, and hands them to the inventory. The Controller
//! derives the advice; nothing here refuses or changes anything, and a reading
//! that cannot be taken is absent.
//!
//! Readings belong to one boot (`boot_id`); a new boot starts them afresh. The
//! ledger survives an agent restart in one small file, because the agent is
//! upgraded far more often than the Spark reboots.
use std::{
    collections::BTreeSet,
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::Mutex,
};

use chrono::{SubsecRound, Utc};
use uuid::Uuid;
use vonk_agent_protocol::generated::{MemoryFragmentation, MemoryPoint};

const READINGS_FILE: &str = "memory-readings.json";
/// The runs resident on this boot, one id per line.
const RESIDENTS_FILE: &str = "memory-residents";
/// /proc files are small; a longer read is not the file we expect.
const READ_LIMIT: u64 = 256 * 1024;

/// A point in a workload's life at which memory is read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Moment {
    LoadBefore,
    LoadAfter,
    StopBefore,
    StopAfter,
}

/// What survives a restart: the readings and the workloads still resident.
#[derive(Default)]
struct State {
    readings: Option<MemoryFragmentation>,
    /// Runs started and not yet stopped on this boot.
    residents: BTreeSet<Uuid>,
}

pub struct Ledger {
    /// The `/` of the host (a directory shaped like it in tests).
    root: PathBuf,
    directory: PathBuf,
    page_size: u64,
    state: Mutex<State>,
    /// Jobs running now; they end by themselves and a restart forgets them.
    jobs: Mutex<u32>,
}

impl Ledger {
    /// The ledger saved in `directory`, or an empty one: a missing, unreadable
    /// or other-boot file is the same as none.
    pub fn open(root: &Path, directory: &Path, page_size: u64) -> Self {
        let readings = fs::read(directory.join(READINGS_FILE))
            .ok()
            .and_then(|bytes| serde_json::from_slice::<MemoryFragmentation>(&bytes).ok())
            .filter(|saved| Some(saved.boot_id.as_str()) == boot_id(root).as_deref());
        let residents = if readings.is_some() {
            fs::read_to_string(directory.join(RESIDENTS_FILE))
                .unwrap_or_default()
                .lines()
                .filter_map(|line| Uuid::parse_str(line).ok())
                .collect()
        } else {
            BTreeSet::new()
        };
        let state = State {
            readings,
            residents,
        };
        Self {
            root: root.to_owned(),
            directory: directory.to_owned(),
            page_size,
            state: Mutex::new(state),
            jobs: Mutex::new(0),
        }
    }

    /// A workload start begins: `run` is resident from now on.
    pub fn load_begins(&self, run: Uuid) {
        self.record(Moment::LoadBefore, Some(run), None);
    }

    pub fn load_ends(&self) {
        self.record(Moment::LoadAfter, None, None);
    }

    pub fn stop_begins(&self) {
        self.record(Moment::StopBefore, None, None);
    }

    /// A workload stop is done: `run` is no longer resident.
    pub fn stop_ends(&self, run: Uuid) {
        self.record(Moment::StopAfter, None, Some(run));
    }

    pub fn job_begins(&self) {
        *self.jobs.lock().unwrap_or_else(|error| error.into_inner()) += 1;
    }

    pub fn job_ends(&self) {
        let mut jobs = self.jobs.lock().unwrap_or_else(|error| error.into_inner());
        *jobs = jobs.saturating_sub(1);
    }

    /// The readings of this boot; `None` before the first one.
    pub fn snapshot(&self) -> Option<MemoryFragmentation> {
        self.lock().readings.clone()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|error| error.into_inner())
    }

    fn record(&self, moment: Moment, arrived: Option<Uuid>, left: Option<Uuid>) {
        let Some(boot) = boot_id(&self.root) else {
            return;
        };
        let point = read_point(&self.root, self.page_size);
        let idle_jobs = *self.jobs.lock().unwrap_or_else(|error| error.into_inner()) == 0;
        let mut state = self.lock();
        if state
            .readings
            .as_ref()
            .is_none_or(|readings| readings.boot_id != boot)
        {
            // A new boot: nothing of the old one is resident or measured.
            *state = State::default();
            state.readings =
                u32::try_from(self.page_size)
                    .ok()
                    .map(|page_size_bytes| MemoryFragmentation {
                        boot_id: boot,
                        page_size_bytes,
                        idle_stop_first: None,
                        idle_stop_latest: None,
                        load_after: None,
                        load_before: None,
                        stop_after: None,
                        stop_before: None,
                    });
        }
        if let Some(run) = arrived {
            state.residents.insert(run);
        }
        if let Some(run) = left {
            state.residents.remove(&run);
        }
        let idle = state.residents.is_empty() && idle_jobs;
        let Some(readings) = state.readings.as_mut() else {
            return;
        };
        match moment {
            Moment::LoadBefore => readings.load_before = Some(point),
            Moment::LoadAfter => readings.load_after = Some(point),
            Moment::StopBefore => readings.stop_before = Some(point),
            Moment::StopAfter => {
                if idle {
                    if readings.idle_stop_first.is_none() {
                        readings.idle_stop_first = Some(point.clone());
                    }
                    readings.idle_stop_latest = Some(point.clone());
                }
                readings.stop_after = Some(point);
            }
        }
        self.save(&state);
    }

    fn save(&self, state: &State) {
        let residents = state
            .residents
            .iter()
            .map(|run| format!("{run}\n"))
            .collect::<String>();
        let saved = state
            .readings
            .as_ref()
            .map(serde_json::to_vec)
            .transpose()
            .map_err(std::io::Error::other)
            .and_then(|readings| {
                for (name, bytes) in [
                    (RESIDENTS_FILE, Some(residents.into_bytes())),
                    (READINGS_FILE, readings),
                ] {
                    if let Some(bytes) = bytes {
                        crate::identity::atomic_private_write(&self.directory, name, &bytes)?;
                    }
                }
                Ok(())
            });
        if let Err(error) = saved {
            // The readings stay in memory; a restart then starts them afresh.
            eprintln!("vonk-agent: memory readings not saved: {error}");
        }
    }
}

fn boot_id(root: &Path) -> Option<String> {
    let text = read(root, "proc/sys/kernel/random/boot_id")?;
    let id = text.trim();
    (id.len() == 36).then(|| id.to_ascii_lowercase())
}

fn read_point(root: &Path, page_size: u64) -> MemoryPoint {
    MemoryPoint {
        // The wire carries microseconds.
        at: Utc::now().trunc_subsecs(6).fixed_offset(),
        sec_page_tables_bytes: read(root, "proc/meminfo")
            .and_then(|text| sec_page_tables_bytes(&text)),
        free_memory: read(root, "proc/buddyinfo")
            .and_then(|text| vonk_agent_protocol::buddyinfo::free_memory(&text, page_size)),
    }
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
