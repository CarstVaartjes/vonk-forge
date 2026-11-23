//! Which RDMA rails NCCL uses, read from a loading workload's own log.
//!
//! With `NCCL_DEBUG=INFO` NCCL prints the `NCCL_IB_HCA` it was given and the
//! devices it then selects:
//!
//! ```text
//! NCCL INFO NCCL_IB_HCA set by environment to =rocep1s0f1:1,roceP2p1s0f1:1
//! NCCL INFO NET/IB : Using [0]rocep1s0f1:1/RoCE [1]roceP2p1s0f1:1/RoCE ; OOB ...
//! ```
//!
//! The first is what the helper launched (`launch_hca`), the second what NCCL
//! uses; fewer used than launched halves the bandwidth of a 200 Gb/s port.
//! The readiness guard already reads the container's bounded tail every ten
//! seconds, so this scans that tail and adds no reader. A run whose log never
//! shows both lines (`NCCL_DEBUG` not set, a single-node run, or the lines
//! scrolled out of the tail between two reads) stays unseen and silent: it is
//! never guessed. Informative only; nothing acts on it.
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
};

use uuid::Uuid;
use vonk_agent_protocol::generated::FabricRails;

const FILE: &str = "fabric-rails.json";
const PROVIDED: &str = "NCCL_IB_HCA set by environment to ";
const USING: &str = "NET/IB : Using ";

/// What the log of one run has shown so far. Only `Observed` is reported.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rails {
    Unseen,
    /// The launched device list was seen; NCCL's choice not yet.
    Provided(u32),
    Observed {
        provided: u32,
        used: u32,
    },
}

impl Rails {
    /// The state after reading `tail`; a state never goes back to an earlier
    /// one, and an unreadable line leaves it as it was.
    pub fn after(self, tail: &str) -> Self {
        let provided = match self {
            Self::Provided(count)
            | Self::Observed {
                provided: count, ..
            } => Some(count),
            Self::Unseen => provided_devices(tail),
        };
        match (provided, used_devices(tail)) {
            (Some(provided), Some(used)) => Self::Observed { provided, used },
            (Some(provided), None) => match self {
                Self::Observed { .. } => self,
                _ => Self::Provided(provided),
            },
            (None, _) => self,
        }
    }
}

/// The devices of the `NCCL_IB_HCA` NCCL echoed; an exclusion list (`^`) names
/// none, so it is unknown.
fn provided_devices(tail: &str) -> Option<u32> {
    let value = tail.split_once(PROVIDED)?.1.lines().next()?.trim();
    let value = value.strip_prefix('=').unwrap_or(value);
    if value.starts_with('^') {
        return None;
    }
    let count = value
        .split(',')
        .filter(|item| !item.trim().is_empty())
        .count();
    u32::try_from(count).ok().filter(|count| *count > 0)
}

/// The devices on NCCL's `NET/IB : Using` line: one per `[n]name` item, and
/// one per `+`-joined name of an item that NCCL merged into a virtual device.
fn used_devices(tail: &str) -> Option<u32> {
    let line = tail.split_once(USING)?.1.lines().next()?;
    let items = line.split(';').next()?;
    let mut devices = 0_usize;
    for item in items.split_ascii_whitespace() {
        let name = item
            .strip_prefix('[')
            .and_then(|rest| rest.split_once(']'))
            .map(|(_, name)| name.split(':').next().unwrap_or(name));
        if let Some(name) = name {
            devices += name.split('+').filter(|part| !part.is_empty()).count();
        }
    }
    u32::try_from(devices).ok().filter(|count| *count > 0)
}

/// The rails observed per run, kept across agent restarts in one small file
/// because the lines are printed once, at the workload's start.
pub struct Store {
    directory: PathBuf,
    runs: Mutex<BTreeMap<Uuid, Rails>>,
    observed: Mutex<BTreeMap<Uuid, FabricRails>>,
}

static STORE: OnceLock<Store> = OnceLock::new();

/// Open the store in `directory`; called once at agent start.
pub fn open(directory: &Path) {
    let observed: BTreeMap<Uuid, FabricRails> = fs::read(directory.join(FILE))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Vec<FabricRails>>(&bytes).ok())
        .unwrap_or_default()
        .into_iter()
        .map(|rails| (rails.run_id, rails))
        .collect();
    let _ = STORE.set(Store {
        directory: directory.to_owned(),
        runs: Mutex::new(BTreeMap::new()),
        observed: Mutex::new(observed),
    });
}

/// Read a run's log tail; a no-op before `open`.
pub fn note(run: Uuid, tail: &str) {
    if let Some(store) = STORE.get() {
        store.note(run, tail);
    }
}

/// A run starts or stops: what was seen of its previous life is gone.
pub fn forget(run: Uuid) {
    if let Some(store) = STORE.get() {
        store.forget(run);
    }
}

/// Every observed run, for the inventory.
pub fn snapshot() -> Vec<FabricRails> {
    STORE.get().map(Store::snapshot).unwrap_or_default()
}

impl Store {
    pub fn new(directory: &Path) -> Self {
        Self {
            directory: directory.to_owned(),
            runs: Mutex::new(BTreeMap::new()),
            observed: Mutex::new(BTreeMap::new()),
        }
    }

    pub fn note(&self, run: Uuid, tail: &str) {
        let mut runs = self.runs.lock().unwrap_or_else(|error| error.into_inner());
        let before = runs.get(&run).copied().unwrap_or(Rails::Unseen);
        let after = before.after(tail);
        runs.insert(run, after);
        if let (Rails::Observed { provided, used }, true) = (after, before != after) {
            drop(runs);
            self.observed
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .insert(
                    run,
                    FabricRails {
                        run_id: run,
                        provided,
                        used,
                    },
                );
            self.save();
        }
    }

    pub fn forget(&self, run: Uuid) {
        self.runs
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .remove(&run);
        let removed = self
            .observed
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .remove(&run)
            .is_some();
        if removed {
            self.save();
        }
    }

    pub fn snapshot(&self) -> Vec<FabricRails> {
        self.observed
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .values()
            .cloned()
            .collect()
    }

    fn save(&self) {
        let saved = serde_json::to_vec(&self.snapshot())
            .map_err(std::io::Error::other)
            .and_then(|bytes| crate::identity::atomic_private_write(&self.directory, FILE, &bytes));
        if let Err(error) = saved {
            eprintln!("vonk-agent: fabric rails not saved: {error}");
        }
    }
}

#[cfg(test)]
mod tests;
