//! Every store below the agent data root has one owner, and what it holds
//! leaves by that owner.
//!
//! The Controller stays the authority for what is in use: an installation it
//! tracks or a job's scope leaves only on its order, and an installation it
//! says it does not track leaves here. The other stores hold entries whose
//! owner the agent observes locally, and the control lane runs one claim at a
//! time and runs this pass between claims, so no claim is live while it runs.
//! Two kinds of entry leave here:
//!
//! - Model files are the one cache kept until the space is needed: a model
//!   object no installation links or names leaves only while the disk is below
//!   the platform's low-space line, least recently used first, until free space
//!   is back above the line plus the eviction reserve.
//! - Everything else unused leaves once nothing has used or changed it for
//!   [`GRACE`], whatever the free space: a transfer checkpoint or build export
//!   whose claim ended, an isolated entry, a base image no build has opened, a
//!   retired store nothing reads, an installation directory the Controller
//!   has said for that long it does not track and no started run names.
//!
//! A later claim that needs a collected entry fetches or builds it again: a
//! local miss repairs, it never refuses.

use std::{
    collections::BTreeSet,
    fs,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
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

/// How long the Controller's inventory verdict is acted on: inventory reports
/// every two minutes, so a verdict this old means the Controller is not
/// answering, and nothing is removed on it.
pub const VERDICT_MAX_AGE: Duration = Duration::from_secs(10 * 60);

/// The file a verdict that the Controller does not track an installation
/// leaves in its directory; its age is the installation's grace.
const UNTRACKED_MARK: &str = "untracked-since";

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
    /// An installation the Controller has not tracked for [`GRACE`] and no
    /// started run names, and isolated entries; one it tracks only on its order.
    Untracked,
}

/// One top-level name below the agent data root (a directory's ends with
/// `/`), how its entries leave, and whose they are.
#[derive(Debug)]
pub struct Store(pub &'static str, pub Collector, pub &'static str);

/// Every top-level name the agent, its helper or its package writes below the
/// data root. A new store cannot be added without an owner and a collector.
#[rustfmt::skip]
pub const STORES: &[Store] = &[
    // First: a removed installation's model objects are unused in the same pass.
    Store("installations/", Untracked, "the Controller's installation record; its verdict"),
    Store("distribution/", ModelObjects, "installations linking or naming it; its transfer"),
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
    #[cfg(test)]
    if let Some(usage) = fake_disk::usage(path) {
        return Some(usage);
    }
    let filesystem = rustix::fs::statvfs(path).ok()?;
    Some((
        filesystem.f_bavail.checked_mul(filesystem.f_frsize)?,
        filesystem.f_blocks.checked_mul(filesystem.f_frsize)?,
    ))
}

/// The Controller's latest inventory verdict: the installation directories it
/// does not track, and when it said so. The inventory lane records each
/// answer; a retention pass acts on it only while it is fresh.
#[derive(Clone, Debug, Default)]
pub struct LatestVerdict(Arc<Mutex<Option<Verdict>>>);

/// When the Controller answered, and the installations it does not track.
type Verdict = (Instant, BTreeSet<String>);

impl LatestVerdict {
    /// Record the Controller's answer to the latest inventory; `None` (an older
    /// Controller, or no installations named) leaves no verdict.
    pub fn record(&self, untracked: Option<Vec<uuid::Uuid>>) {
        let answer = untracked.map(|ids| {
            (
                Instant::now(),
                ids.iter().map(ToString::to_string).collect(),
            )
        });
        *self
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = answer;
    }

    /// The untracked installations, when the Controller said so within
    /// [`VERDICT_MAX_AGE`].
    pub fn fresh(&self) -> Option<BTreeSet<String>> {
        let latest = self
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        latest
            .as_ref()
            .filter(|(said, _)| said.elapsed() <= VERDICT_MAX_AGE)
            .map(|(_, untracked)| untracked.clone())
    }
}

/// The installation directories below `data_root`, by id, as inventory
/// reports them (at most the wire limit, in order); `None` when they cannot
/// be listed.
pub fn installation_names(data_root: &Path) -> Option<Vec<uuid::Uuid>> {
    let entries = match fs::read_dir(data_root.join("installations")) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Some(Vec::new()),
        Err(error) => {
            eprintln!("vonk-agent: inventory cannot list the installations: {error}");
            return None;
        }
    };
    let mut names = BTreeSet::new();
    for entry in entries {
        let entry = entry.ok()?;
        let name = entry.file_name().into_string().unwrap_or_default();
        if installation_name(&name) && entry.file_type().ok()?.is_dir() {
            names.insert(uuid::Uuid::parse_str(&name).ok()?);
        }
    }
    Some(
        names
            .into_iter()
            .take(vonk_agent_protocol::MAX_REPORTED_INSTALLATIONS)
            .collect(),
    )
}

/// Each installation directory's logical size now, measured as its install
/// receipt was (metadata only), as inventory reports them; `None` when the
/// installations cannot be listed. One that cannot be measured is left out.
pub fn installation_sizes(
    data_root: &Path,
) -> Option<Vec<vonk_agent_protocol::generated::InstallationBytes>> {
    Some(
        installation_names(data_root)?
            .into_iter()
            .filter_map(|id| {
                match crate::oci::installation_tree_bytes(data_root, &id.to_string()) {
                    Ok(bytes) => Some(vonk_agent_protocol::generated::InstallationBytes {
                        installation_id: id,
                        bytes,
                    }),
                    Err(error) => {
                        eprintln!(
                            "vonk-agent: inventory cannot measure installation {id}: {error}"
                        );
                        None
                    }
                }
            })
            .collect(),
    )
}

/// Collect unused entries older than [`GRACE`] at `now`, then relieve low
/// space from unused model objects. `untracked` is the Controller's fresh
/// verdict (`None`: there is none, and no installation leaves). `disk`
/// observes `(free, total)`; `tick` runs after each removal (the service
/// watchdog).
pub fn sweep(
    data_root: &Path,
    now: SystemTime,
    deadline: Instant,
    untracked: Option<&BTreeSet<String>>,
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
    for Store(name, collector, _) in STORES {
        // Without its slash, so a store root that is a link is not resolved.
        let root = data_root.join(name.trim_end_matches('/'));
        match collector {
            Ended => pass.ended(&root, &|_, _| true),
            ModelObjects => {
                let objects = root.join("models");
                pass.ended(&root, &|name, _| name != "models");
                pass.ended(&objects, &|name, metadata| !model_object(name, metadata));
            }
            BaseImages => {
                let images = root.join("sha256");
                pass.ended(&images, &|name, _| name.starts_with(".damaged-"));
                pass.base_images(&images);
            }
            Untracked => {
                // A name that is no installation's was isolated (`.damaged`).
                pass.ended(&root, &|name, _| !installation_name(name));
                // A started run's installation stays whatever the verdict.
                if let Some(untracked) = untracked
                    && let Some(started) =
                        started_run_installations(&data_root.join("run-metadata"))
                {
                    pass.untracked(&root, &|name| {
                        untracked.contains(name) && !started.contains(name)
                    });
                }
            }
            ControllerOrder | Claim | Bookkeeping => {}
        }
    }
    let unused_models = unused_model_objects(data_root, pass.owner, &BTreeSet::new());
    pass.relieve(unused_models.unwrap_or_default(), disk);
    pass.swept
}

/// The model objects no installation links or names, least recently used
/// first, leaving out those `keep` names; `None` when a plan cannot be read
/// (it could name any). An object is owned by every installation that links
/// it or whose plan names it (a start prepares a private copy's installation
/// from it too).
fn unused_model_objects(
    data_root: &Path,
    owner: u32,
    keep: &BTreeSet<String>,
) -> Option<Vec<(SystemTime, PathBuf, fs::Metadata)>> {
    let named = installation_objects(&data_root.join("installations"))?;
    let objects = data_root.join("distribution/models");
    let mut unused: Vec<_> = list_owned(&objects, owner)
        .into_iter()
        .flatten()
        .filter_map(|(name, metadata)| {
            (model_object(&name, &metadata)
                && metadata.nlink() == 1
                && !named.contains(&name)
                && !keep.contains(&name))
            .then(|| (changed(&metadata), objects.join(&name), metadata))
        })
        .collect();
    unused.sort_by_key(|(used, _, _)| *used);
    Some(unused)
}

/// The bytes of the model objects no installation links or names, which the
/// agent removes when space is needed, as inventory reports them; `None`
/// when a plan or the data root cannot be read.
pub fn unused_model_bytes(data_root: &Path) -> Option<u64> {
    let owner = rustix::process::geteuid().as_raw();
    if !owned_directory(data_root, owner) {
        return None;
    }
    Some(
        unused_model_objects(data_root, owner, &BTreeSet::new())?
            .iter()
            .map(|(_, _, metadata)| metadata.len())
            .sum(),
    )
}

/// Before a claim writes, make room for it: remove unused model objects, least
/// recently used first and never one `keep` names (the claim's own), until
/// `required` bytes are free or none is left. Returns the bytes removed. The
/// claim then checks its space as before; a later claim that needs a removed
/// object fetches it again.
pub fn make_room(data_root: &Path, required: u64, keep: &BTreeSet<String>) -> u64 {
    let free = || disk_usage(data_root).map(|(free, _)| free);
    if free().is_none_or(|free| free >= required) {
        return 0;
    }
    let owner = rustix::process::geteuid().as_raw();
    if !owned_directory(data_root, owner) {
        return 0;
    }
    let mut removed = 0_u64;
    for (_, path, metadata) in unused_model_objects(data_root, owner, keep).unwrap_or_default() {
        if free().is_none_or(|free| free >= required) {
            break;
        }
        match fs::remove_file(&path) {
            Ok(()) => removed = removed.saturating_add(metadata.len()),
            Err(error) => eprintln!(
                "vonk-agent: store retention kept {}: {error}",
                path.display()
            ),
        }
    }
    if removed > 0 {
        eprintln!(
            "vonk-agent: store retention removed {removed} bytes of unused model files to make room for {required} free bytes"
        );
    }
    removed
}

/// An installation directory is named by its canonical id.
fn installation_name(name: &str) -> bool {
    uuid::Uuid::parse_str(name).is_ok_and(|id| id.to_string() == name)
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

    /// An installation the Controller does not track leaves a grace after the
    /// first verdict that said so. That verdict marks the directory, the mark's
    /// age is the grace (it survives an agent restart), and a verdict that
    /// tracks it again, or a run that names it, removes the mark.
    fn untracked(&mut self, root: &Path, untracked: &dyn Fn(&str) -> bool) {
        for (name, metadata) in self.list(root).into_iter().flatten() {
            if !metadata.is_dir() || !installation_name(&name) {
                continue;
            }
            let directory = root.join(&name);
            let mark = directory.join(UNTRACKED_MARK);
            let marked = fs::symlink_metadata(&mark)
                .ok()
                .filter(fs::Metadata::is_file);
            match (untracked(&name), marked) {
                (true, None) => match fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(&mark)
                {
                    Ok(_) => eprintln!(
                        "vonk-agent: store retention: the Controller does not track installation {name}; it leaves after a day unless tracked again"
                    ),
                    Err(error) => eprintln!(
                        "vonk-agent: store retention cannot mark installation {name}: {error}"
                    ),
                },
                (true, Some(since)) => {
                    let aged = self
                        .now
                        .duration_since(changed(&since))
                        .is_ok_and(|age| age > GRACE);
                    if aged && self.remove(&directory, &metadata) {
                        eprintln!(
                            "vonk-agent: store retention removed installation {name}: the Controller has not tracked it for a day and no started run names it"
                        );
                    }
                }
                (false, Some(_)) => match fs::remove_file(&mark) {
                    Ok(()) => eprintln!(
                        "vonk-agent: store retention keeps installation {name}: it is tracked or a run names it again"
                    ),
                    Err(error) => eprintln!(
                        "vonk-agent: store retention cannot unmark installation {name}: {error}"
                    ),
                },
                (false, None) => {}
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
        objects: Vec<(SystemTime, PathBuf, fs::Metadata)>,
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
        for (_, path, metadata) in objects {
            if disk().is_none_or(|(free, _)| free >= target) || self.swept.pending {
                return;
            }
            self.remove(&path, &metadata);
        }
    }

    /// The entries of an agent-owned store root, without following links.
    fn list(&mut self, root: &Path) -> Option<Vec<(String, fs::Metadata)>> {
        list_owned(root, self.owner)
    }

    /// Remove one entry; true when it is gone.
    fn remove(&mut self, path: &Path, metadata: &fs::Metadata) -> bool {
        if Instant::now() >= self.deadline {
            self.swept.pending = true;
            return false;
        }
        // remove_dir_all never follows a link; a link itself is only unlinked.
        // A read-only directory the agent owns is made removable first.
        let result = if metadata.is_dir() {
            crate::recipe_builder::make_owned_directories_removable(path)
                .and_then(|()| fs::remove_dir_all(path))
        } else {
            fs::remove_file(path)
        };
        let removed = match result {
            Ok(()) => {
                self.swept.entries += 1;
                self.swept.bytes += if metadata.is_dir() { 0 } else { metadata.len() };
                true
            }
            Err(error) => {
                eprintln!(
                    "vonk-agent: store retention kept {}: {error}",
                    path.display()
                );
                false
            }
        };
        (self.tick)();
        removed
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

/// The installation each started run names (its lifecycle record lives from
/// its start until its stop completes), or `None` when one cannot be read.
fn started_run_installations(run_metadata: &Path) -> Option<BTreeSet<String>> {
    let mut named = BTreeSet::new();
    let entries = match fs::read_dir(run_metadata) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Some(named),
        Err(error) => {
            eprintln!("vonk-agent: store retention keeps installations: {error}");
            return None;
        }
    };
    for entry in entries {
        let Ok(entry) = entry else {
            eprintln!(
                "vonk-agent: store retention keeps installations: run-metadata is unreadable"
            );
            return None;
        };
        if !entry.file_type().is_ok_and(|kind| kind.is_dir()) {
            continue;
        }
        match crate::oci::read_run_lifecycle(&entry.path().join("lifecycle.json")) {
            Ok(Some(record)) => {
                named.insert(record.installation_id);
            }
            Ok(None) => {}
            Err(error) => {
                eprintln!(
                    "vonk-agent: store retention keeps installations: a run record is unreadable: {error}"
                );
                return None;
            }
        }
    }
    Some(named)
}

/// The entries `owner` owns of a store root it owns, without following links.
fn list_owned(root: &Path, owner: u32) -> Option<Vec<(String, fs::Metadata)>> {
    if !owned_directory(root, owner) {
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
                (metadata.uid() == owner).then_some((name, metadata))
            })
            .collect(),
    )
}

fn owned_directory(path: &Path, owner: u32) -> bool {
    fs::symlink_metadata(path).is_ok_and(|metadata| metadata.is_dir() && metadata.uid() == owner)
}

#[cfg(test)]
pub(crate) mod fake_disk;
#[cfg(test)]
mod tests;
