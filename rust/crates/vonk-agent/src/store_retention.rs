//! Every store below the agent data root has one owner, and what it holds
//! leaves by that owner.
//!
//! The Controller stays the authority for what is in use: an installation or a
//! job's scope leaves only on its order. The stores collected here hold entries
//! whose owner the agent observes locally, and the control lane runs one claim
//! at a time and runs this pass between claims, so no claim is live while it
//! runs. Two kinds of entry leave here:
//!
//! - Model files are the one cache kept until the space is needed: a model
//!   object no installation links or names leaves only while the disk is below
//!   the platform's low-space line, least recently used first, until free space
//!   is back above the line plus the eviction reserve.
//! - Everything else unused leaves once nothing has used or changed it for
//!   [`GRACE`], whatever the free space: a transfer checkpoint or build export
//!   whose claim ended, an isolated entry, a base image no build has opened, a
//!   retired store nothing reads.
//!
//! A later claim that needs a collected entry fetches or builds it again: a
//! local miss repairs, it never refuses.

use std::{
    collections::BTreeSet,
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime},
};

use crate::held_lock::HeldLock;
use Collector::*;

/// How long an unused entry stays after its last use or change: a claim that
/// ended on a fault resumes from it within this time.
pub const GRACE: Duration = Duration::from_secs(24 * 60 * 60);

/// The Controller's low-space line (`STORAGE_LOW_FREE_FRACTION`) without its
/// largest-install term, so the agent never evicts above the Controller's line.
pub const LOW_FREE_PERCENT: u64 = 10;
/// The Controller's eviction reserve (`STORAGE_EVICTION_RESERVE_FRACTION` and
/// `STORAGE_EVICTION_RESERVE_FLOOR_BYTES`).
pub const RESERVE_PERCENT: u64 = 2;
pub const RESERVE_FLOOR_BYTES: u64 = 5 * 1024 * 1024 * 1024;

/// How one store's entries leave.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Collector {
    /// Only on a Controller order (uninstall, job scope cleanup).
    ControllerOrder,
    /// The claim that made it removes it before the claim ends.
    Claim,
    /// One bounded file or credential set the agent rewrites in place.
    Bookkeeping,
    /// Every entry, once unchanged for [`GRACE`].
    Ended,
    /// Model objects leave on low space; a checkpoint or isolated entry is [`Ended`].
    ModelObjects,
    /// A base image no build has opened for [`GRACE`], and isolated entries.
    BaseImages,
}

/// One top-level name below the agent data root (a directory's ends with
/// `/`), how its entries leave, and whose they are.
#[derive(Debug)]
pub struct Store(pub &'static str, pub Collector, pub &'static str);

/// Every top-level name the agent, its helper or its package writes below the
/// data root. A new store cannot be added without an owner and a collector.
#[rustfmt::skip]
pub const STORES: &[Store] = &[
    Store("distribution/", ModelObjects, "installations linking or naming it; its transfer"),
    Store("installations/", ControllerOrder, "the Controller's installation record"),
    Store("installation-reconciliation/", ControllerOrder, "the Controller's uninstall"),
    Store("installation-ownership/", Bookkeeping, "one lock file per installation"),
    Store("runs/", ControllerOrder, "the Controller's run record"),
    Store("run-metadata/", ControllerOrder, "the Controller's run record"),
    Store("builds/", Ended, "the build claim, until the Controller has the image"),
    Store("build-staging/", Claim, "the build claim"),
    Store("base-images/", BaseImages, "the builds that open each image"),
    Store("containers/", Claim, "Podman's default storage; builds use private storage"),
    Store("tmp/", Claim, "the process that made each temporary file"),
    Store("credentials/", Bookkeeping, "the agent identity"),
    Store("state.sqlite", Bookkeeping, "the agent journal"),
    Store(".state-db-reserve", Bookkeeping, "the agent journal"),
    Store("runtime-preflight-cache.json", Bookkeeping, "the host fingerprint"),
    Store("registry-auth.json", Bookkeeping, "the base-image registry credential"),
    Store(".config/", Bookkeeping, "the rootless Podman configuration"),
    Store(".machine-evidence", Claim, "the package step publishing machine-evidence"),
    Store("machine-evidence", Bookkeeping, "the package"),
    // Retired: nothing writes or reads these any more.
    Store("models/", Ended, "none: the model store before installations"),
    Store("oci-archives/", Ended, "none: image archives before the layered store"),
    Store("image-imports/", Ended, "none: Docker imports before the layered store"),
];

/// What one pass did.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Swept {
    pub entries: u64,
    /// Bytes of the removed files (a removed directory's contents are not counted).
    pub bytes: u64,
    /// The pass reached its deadline with entries left to look at.
    pub pending: bool,
}

/// `(free, total)` bytes of the filesystem holding `path`, as inventory reports.
pub fn disk_usage(path: &Path) -> Option<(u64, u64)> {
    let filesystem = rustix::fs::statvfs(path).ok()?;
    Some((
        filesystem.f_bavail.checked_mul(filesystem.f_frsize)?,
        filesystem.f_blocks.checked_mul(filesystem.f_frsize)?,
    ))
}

/// Collect unused entries older than [`GRACE`] at `now`, then relieve low
/// space from unused model objects. `disk` observes `(free, total)`; `tick` runs after each
/// removal (the service watchdog).
pub fn sweep(
    data_root: &Path,
    now: SystemTime,
    deadline: Instant,
    disk: &dyn Fn() -> Option<(u64, u64)>,
    tick: &dyn Fn(),
) -> Swept {
    let mut pass = Pass {
        now,
        deadline,
        tick,
        owner: rustix::process::geteuid().as_raw(),
        swept: Swept::default(),
    };
    if !owned_directory(data_root, pass.owner) {
        return pass.swept;
    }
    let mut unused_models = Vec::new();
    for Store(name, collector, _) in STORES {
        // Without its slash, so a store root that is a link is not resolved.
        let root = data_root.join(name.trim_end_matches('/'));
        match collector {
            Ended => pass.ended(&root, &|_, _| true),
            ModelObjects => {
                let objects = root.join("models");
                pass.ended(&root, &|name, _| name != "models");
                pass.ended(&objects, &|name, metadata| !model_object(name, metadata));
                // An object is owned by every installation that links it or
                // whose plan names it (a start prepares a private copy's
                // installation from it too). An unreadable plan could name any.
                if let Some(named) = installation_objects(&data_root.join("installations")) {
                    unused_models.extend(pass.list(&objects).into_iter().flatten().filter_map(
                        |(name, metadata)| {
                            (model_object(&name, &metadata)
                                && metadata.nlink() == 1
                                && !named.contains(&name))
                            .then(|| (changed(&metadata), objects.join(&name), metadata))
                        },
                    ));
                }
            }
            BaseImages => {
                let images = root.join("sha256");
                pass.ended(&images, &|name, _| name.starts_with(".damaged-"));
                pass.base_images(&images);
            }
            ControllerOrder | Claim | Bookkeeping => {}
        }
    }
    pass.relieve(unused_models, disk);
    pass.swept
}

/// A file at its digest name is a model object; any other name in the model
/// store is a transfer checkpoint or an isolated entry.
fn model_object(name: &str, metadata: &fs::Metadata) -> bool {
    metadata.is_file()
        && name.len() == 64
        && name
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

struct Pass<'a> {
    now: SystemTime,
    deadline: Instant,
    tick: &'a dyn Fn(),
    owner: u32,
    swept: Swept,
}

impl Pass<'_> {
    /// Remove each entry of `root` that `scratch` selects and that nothing has
    /// changed for the grace period.
    fn ended(&mut self, root: &Path, scratch: &dyn Fn(&str, &fs::Metadata) -> bool) {
        for (name, metadata) in self.list(root).into_iter().flatten() {
            let aged = self
                .now
                .duration_since(changed(&metadata))
                .is_ok_and(|age| age > GRACE);
            if scratch(&name, &metadata) && aged {
                self.remove(&root.join(name), &metadata);
            }
        }
    }

    /// A base image's last use is its lock's change time: every build that
    /// opens the image takes the lock and marks it. A held lock is a build
    /// using it now.
    fn base_images(&mut self, images: &Path) {
        for (name, metadata) in self.list(images).into_iter().flatten() {
            if !metadata.is_dir() || name.starts_with('.') {
                continue;
            }
            let lock_path = images.join(format!("{name}-lock"));
            let used = fs::symlink_metadata(&lock_path).map_or(changed(&metadata), |lock| {
                changed(&lock).max(changed(&metadata))
            });
            if self.now.duration_since(used).is_ok_and(|age| age <= GRACE) {
                continue;
            }
            let held = match fs::File::open(&lock_path).map(HeldLock::try_exclusive) {
                Ok(Ok(held)) => Some(held),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
                // A build holds the lock (the image is in use) or it is unreadable.
                Ok(Err(busy)) => {
                    if busy != rustix::io::Errno::WOULDBLOCK {
                        eprintln!(
                            "vonk-agent: store retention kept {}: {busy}",
                            lock_path.display()
                        );
                    }
                    continue;
                }
                Err(error) => {
                    eprintln!(
                        "vonk-agent: store retention kept {}: {error}",
                        lock_path.display()
                    );
                    continue;
                }
            };
            self.remove(&images.join(&name), &metadata);
            if held.is_some()
                && let Err(error) = fs::remove_file(&lock_path)
            {
                eprintln!(
                    "vonk-agent: store retention kept {}: {error}",
                    lock_path.display()
                );
            }
            drop(held);
        }
    }

    /// Below the low-space line, remove the least recently used unused model
    /// objects until free space is back above the line plus the reserve.
    fn relieve(
        &mut self,
        mut objects: Vec<(SystemTime, PathBuf, fs::Metadata)>,
        disk: &dyn Fn() -> Option<(u64, u64)>,
    ) {
        let Some((free, total)) = disk() else {
            return;
        };
        let line = total / 100 * LOW_FREE_PERCENT;
        if free >= line {
            return;
        }
        let target = line + RESERVE_FLOOR_BYTES.max(total / 100 * RESERVE_PERCENT);
        objects.sort_by_key(|(used, _, _)| *used);
        for (_, path, metadata) in objects {
            if disk().is_none_or(|(free, _)| free >= target) || self.swept.pending {
                return;
            }
            self.remove(&path, &metadata);
        }
    }

    /// The entries of an agent-owned store root, without following links.
    fn list(&mut self, root: &Path) -> Option<Vec<(String, fs::Metadata)>> {
        if !owned_directory(root, self.owner) {
            return None;
        }
        let entries = match fs::read_dir(root) {
            Ok(entries) => entries,
            Err(error) => {
                eprintln!(
                    "vonk-agent: store retention cannot list {}: {error}",
                    root.display()
                );
                return None;
            }
        };
        Some(
            entries
                .filter_map(Result::ok)
                .filter_map(|entry| {
                    let name = entry.file_name().into_string().ok()?;
                    let metadata = fs::symlink_metadata(entry.path()).ok()?;
                    (metadata.uid() == self.owner).then_some((name, metadata))
                })
                .collect(),
        )
    }

    fn remove(&mut self, path: &Path, metadata: &fs::Metadata) {
        if Instant::now() >= self.deadline {
            self.swept.pending = true;
            return;
        }
        // remove_dir_all never follows a link; a link itself is only unlinked.
        // A read-only directory the agent owns is made removable first.
        let result = if metadata.is_dir() {
            crate::recipe_builder::make_owned_directories_removable(path)
                .and_then(|()| fs::remove_dir_all(path))
        } else {
            fs::remove_file(path)
        };
        match result {
            Ok(()) => {
                self.swept.entries += 1;
                self.swept.bytes += if metadata.is_dir() { 0 } else { metadata.len() };
            }
            Err(error) => eprintln!(
                "vonk-agent: store retention kept {}: {error}",
                path.display()
            ),
        }
        (self.tick)();
    }
}

/// When anything last changed the entry: its bytes, its links, its name.
fn changed(metadata: &fs::Metadata) -> SystemTime {
    SystemTime::UNIX_EPOCH
        + Duration::new(
            u64::try_from(metadata.ctime()).unwrap_or(0),
            u32::try_from(metadata.ctime_nsec()).unwrap_or(0),
        )
}

/// The model object digests every installation's plan names, or `None` when a
/// plan cannot be read. The plan is the one the agent itself starts from.
fn installation_objects(installations: &Path) -> Option<BTreeSet<String>> {
    let mut named = BTreeSet::new();
    let entries = match fs::read_dir(installations) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Some(named),
        Err(error) => {
            eprintln!("vonk-agent: store retention keeps model objects: {error}");
            return None;
        }
    };
    let data_root = installations.parent()?;
    for entry in entries {
        let name = match entry {
            Ok(entry) => entry.file_name().to_string_lossy().into_owned(),
            Err(error) => {
                eprintln!("vonk-agent: store retention keeps model objects: {error}");
                return None;
            }
        };
        // Only an installation's own name holds a plan; anything else is not one.
        if uuid::Uuid::parse_str(&name).map(|id| id.to_string()) != Ok(name.clone()) {
            continue;
        }
        match crate::oci::read_persisted_plan(data_root, &name) {
            Ok(plan) => named.extend(plan.artifacts.into_iter().map(|artifact| artifact.sha256)),
            // An installation still being prepared has no plan and links nothing.
            Err(crate::oci::OciError::Io(error))
                if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                eprintln!(
                    "vonk-agent: store retention keeps model objects: installation {name} plan is unreadable: {error}"
                );
                return None;
            }
        }
    }
    Some(named)
}

fn owned_directory(path: &Path, owner: u32) -> bool {
    fs::symlink_metadata(path).is_ok_and(|metadata| metadata.is_dir() && metadata.uid() == owner)
}

#[cfg(test)]
mod tests;
