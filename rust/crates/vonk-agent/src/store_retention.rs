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
//! - Unused Spark model copies leave after [`GRACE`], or sooner when a load
//!   needs disk space or the low-space line is crossed. Installations' hard
//!   links record their last reference in the inode change time, including
//!   when the last installation drops its link. Plans and live operations
//!   also protect the exact objects they name.
//! - Everything else unused leaves once nothing has used or changed it for
//!   [`GRACE`], whatever the free space: a transfer checkpoint or build export
//!   whose claim ended, an isolated entry, a base image no build has opened, a
//!   retired store nothing reads, an installation directory the Controller
//!   has said for that long it does not track and no started run names, a
//!   run scope it has said for that long no run or job owns, the cleanup
//!   history and lock file of an installation that is gone.
//!
//! A later claim that needs a collected entry fetches or builds it again: a
//! local miss repairs, it never refuses.

use std::{
    collections::BTreeSet,
    fs,
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime},
};

use crate::held_lock::HeldLock;
use Collector::*;

/// How long an unused entry stays after its last use or change: a claim that
/// ended on a fault resumes from it within this time.
pub use vonk_agent_protocol::store_retention::GRACE;

/// The Controller's low-space line (`STORAGE_LOW_FREE_FRACTION`) without its
/// largest-install term; a current load can request more free space.
pub const LOW_FREE_PERCENT: u64 = 10;
/// The Controller's eviction reserve (`STORAGE_EVICTION_RESERVE_FRACTION` and
/// `STORAGE_EVICTION_RESERVE_FLOOR_BYTES`).
pub const RESERVE_PERCENT: u64 = 2;
pub const RESERVE_FLOOR_BYTES: u64 = 5 * 1024 * 1024 * 1024;

/// How long the Controller's inventory verdict is acted on: inventory reports
/// every two minutes, so a verdict this old means the Controller is not
/// answering, and nothing is removed on it.
pub const VERDICT_MAX_AGE: Duration = Duration::from_secs(10 * 60);

/// The file a verdict that the Controller does not track an installation or a
/// run scope leaves in its directory; its age is the grace.
const UNTRACKED_MARK: &str = "untracked-since";

pub use vonk_agent_protocol::store_retention::{Collector, STORES, Store, StoreRoot};

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

/// The Controller's latest inventory verdict: the installation directories and
/// run scopes it does not track, and when it said so. The inventory lane
/// records each answer; a retention pass acts on it only while it is fresh.
#[derive(Clone, Debug, Default)]
pub struct LatestVerdict(Arc<Mutex<Option<(Instant, Untracked)>>>);

/// What the Controller said it does not track, by name.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Untracked {
    pub installations: BTreeSet<String>,
    /// `None`: the verdict judged no run scope (an older Controller, or none
    /// named), and none leaves.
    pub runs: Option<BTreeSet<String>>,
    pub retention: Option<vonk_agent_protocol::generated::StoreRetentionRequest>,
    /// Only this observed batch may be judged absent from the untracked set.
    pub observed_installations: BTreeSet<String>,
    pub observed_runs: BTreeSet<String>,
}

impl LatestVerdict {
    /// Record the Controller's answer to the latest inventory; `None` (an older
    /// Controller, or nothing named) leaves no verdict.
    pub fn record(
        &self,
        verdict: Option<vonk_agent_protocol::generated::InventoryVerdict>,
        observed_installations: &[uuid::Uuid],
        observed_runs: &[uuid::Uuid],
    ) {
        let names = |ids: &[uuid::Uuid]| ids.iter().map(ToString::to_string).collect();
        let answer = verdict.map(|verdict| {
            (
                Instant::now(),
                Untracked {
                    retention: verdict.store_retention,
                    observed_installations: names(observed_installations),
                    observed_runs: names(observed_runs),
                    installations: names(&verdict.untracked_installations),
                    runs: verdict.untracked_runs.as_deref().map(names),
                },
            )
        });
        *self
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = answer;
    }

    /// What is untracked, when the Controller said so within
    /// [`VERDICT_MAX_AGE`].
    pub fn fresh(&self) -> Option<Untracked> {
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
/// reports them (a rotating page at most the wire limit); `None` when they cannot
/// be listed.
pub fn installation_names(data_root: &Path) -> Option<Vec<uuid::Uuid>> {
    let mut names = BTreeSet::new();
    scope_names(&data_root.join("installations"), &mut names)?;
    rotating_page(
        data_root,
        "installation-inventory-cursor",
        names,
        vonk_agent_protocol::MAX_REPORTED_INSTALLATIONS,
    )
}

/// The run scopes below `data_root` (`runs/` and `run-metadata/`), by id, as
/// inventory reports them (a rotating page at most the wire limit); `None` when
/// they cannot be listed.
pub fn run_scope_names(data_root: &Path) -> Option<Vec<uuid::Uuid>> {
    let mut names = BTreeSet::new();
    for store in ["runs", "run-metadata"] {
        scope_names(&StoreRoot::Agent.path(data_root, store)?, &mut names)?;
    }
    rotating_page(
        data_root,
        "run-inventory-cursor",
        names,
        vonk_agent_protocol::MAX_REPORTED_RUNS,
    )
}

/// Persist the next rotation across collect-inventory subprocesses and agent
/// restarts. A failed report skips a page only until the next rotation.
fn rotating_page(
    data_root: &Path,
    cursor_file: &str,
    names: BTreeSet<uuid::Uuid>,
    limit: usize,
) -> Option<Vec<uuid::Uuid>> {
    let cursor_path = StoreRoot::Agent.path(data_root, cursor_file)?;
    let cursor = fs::read_to_string(&cursor_path)
        .ok()
        .and_then(|value| uuid::Uuid::parse_str(&value).ok());
    let after = names
        .iter()
        .copied()
        .filter(|id| cursor.is_none_or(|cursor| *id > cursor));
    let wrapped = names
        .iter()
        .copied()
        .filter(|id| cursor.is_some_and(|cursor| *id <= cursor));
    let page: Vec<_> = after.chain(wrapped).take(limit).collect();
    if let Some(last) = page.last()
        && let Err(error) = crate::identity::atomic_private_write(
            cursor_path.parent()?,
            cursor_file,
            last.to_string().as_bytes(),
        )
    {
        eprintln!("vonk-agent: inventory rotation could not be saved: {error}");
        return None;
    }
    Some(page)
}

/// Add the id-named directories of `root` to `names`; `None` when it cannot
/// be listed (an absent root has none).
fn scope_names(root: &Path, names: &mut BTreeSet<uuid::Uuid>) -> Option<()> {
    let entries = match fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Some(()),
        Err(error) => {
            eprintln!(
                "vonk-agent: inventory cannot list {}: {error}",
                root.display()
            );
            return None;
        }
    };
    for entry in entries {
        let entry = entry.ok()?;
        let name = entry.file_name().into_string().unwrap_or_default();
        if installation_name(&name) && entry.file_type().ok()?.is_dir() {
            names.insert(uuid::Uuid::parse_str(&name).ok()?);
        }
    }
    Some(())
}

/// Each installation directory's logical size now, measured as its install
/// receipt was (metadata only), as inventory reports them; `None` when the
/// installations cannot be listed. One that cannot be measured is left out.
pub fn installation_sizes(
    data_root: &Path,
    installations: &[uuid::Uuid],
) -> Option<Vec<vonk_agent_protocol::generated::InstallationBytes>> {
    Some(
        installations
            .iter()
            .filter_map(|id| {
                match crate::oci::installation_tree_bytes(data_root, &id.to_string()) {
                    Ok(bytes) => Some(vonk_agent_protocol::generated::InstallationBytes {
                        installation_id: *id,
                        bytes,
                    }),
                    // A directory/file removed during the scan is a cache miss.
                    // Permission and other I/O errors remain visible, never absence.
                    Err(crate::oci::OciError::Io(error))
                        if error.kind() == std::io::ErrorKind::NotFound =>
                    {
                        None
                    }
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

/// Collect unused entries (including model copies) older than [`GRACE`] at
/// `now`, then relieve low space from younger unused model objects. `untracked` is the Controller's fresh
/// verdict (`None`: there is none, and no installation or run scope leaves). `disk`
/// observes `(free, total)`; `tick` runs after each removal (the service
/// watchdog).
pub fn sweep(
    data_root: &Path,
    now: SystemTime,
    deadline: Instant,
    untracked: Option<&Untracked>,
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
    for store @ Store(name, collector, _, root_kind) in STORES {
        if *root_kind != StoreRoot::Agent {
            continue;
        }
        if let Some((prefix, suffix)) = name.split_once('*') {
            pass.ended(data_root, &|name, _| {
                name.starts_with(prefix) && name.ends_with(suffix)
            });
            continue;
        }
        // Without its slash, so a store root that is a link is not resolved.
        let root = store.path(data_root);
        match collector {
            Ended => pass.ended(&root, &|_, _| true),
            ModelObjects => {
                let objects = root.join("models");
                pass.ended(&root, &|name, _| name != "models");
                pass.ended(&objects, &|name, metadata| !model_object(name, metadata));
            }
            BuildStaging => pass.build_staging(&root, &crate::process::SystemProcessRunner),
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
                    pass.untracked(
                        &root,
                        "installation directory",
                        &|name| {
                            untracked.observed_installations.contains(name)
                                || started.contains(name)
                        },
                        &|name| untracked.installations.contains(name) && !started.contains(name),
                    );
                }
            }
            UntrackedRuns => {
                if *name == "run-metadata/" {
                    pass.restore_run_scopes(&root, data_root);
                }
                // A name that is no scope's was isolated (`.damaged`).
                pass.ended(&root, &|name, _| !installation_name(name));
                // Runtime observation retires a stopped or forgotten run's
                // lifecycle before its scope and installation can expire.
                if let Some(untracked) = untracked
                    && let Some(runs) = untracked.runs.as_ref()
                {
                    pass.untracked(
                        &root,
                        "run scope",
                        &|name| untracked.observed_runs.contains(name),
                        &|name| {
                            runs.contains(name)
                                && matches!(
                                    crate::oci::read_run_lifecycle(
                                        &data_root
                                            .join("run-metadata")
                                            .join(name)
                                            .join("lifecycle.json")
                                    ),
                                    Ok(None)
                                )
                        },
                    );
                }
            }
            Reconciliation => pass.ended(&root, &|name, _| {
                match (name.strip_suffix(".json"), name.strip_suffix(".partial")) {
                    // A checkpoint is disposable once its installation is gone.
                    (Some(id), _) if installation_name(id) => installation_gone(data_root, id),
                    (_, Some(id)) if installation_name(id) => false,
                    // `.retained` bytes, `.retired` records, unfinished writes.
                    _ => true,
                }
            }),
            Ownership => pass.ownership_locks(&root, data_root),
            ControllerOrder | Claim | Bookkeeping => {}
        }
    }
    // Private copies from before files were shared become links of their store
    // object first, so that object is linked, not unused, when it is judged.
    // A started run's installation keeps its files as they are.
    if let Some(started) = started_run_installations(&data_root.join("run-metadata")) {
        pass.swept.pending |=
            crate::oci::relink_private_model_copies(data_root, &started, deadline).pending;
    }
    // A fresh Controller scan protects not-yet-installed profile/operation
    // content too. Missing evidence is unknown, never an empty reference set.
    if let Some(retention) = untracked.and_then(|verdict| verdict.retention.as_ref()) {
        let path = data_root.join("store-retention.json");
        // Transfer scratch already has a 24-hour collector, including after a
        // process dies before this projection's atomic rename.
        let scratch = data_root.join("distribution");
        let temporary = scratch.join(format!("retention-{}.partial", uuid::Uuid::new_v4()));
        let publish = || -> std::io::Result<()> {
            use std::io::Write;
            fs::create_dir_all(&scratch)?;
            let mut file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&temporary)?;
            let body = serde_json::to_vec(retention)?;
            if body.len() > 1024 * 1024 {
                return Err(std::io::Error::other(
                    "retention request exceeds byte limit",
                ));
            }
            file.write_all(&body)?;
            fs::rename(&temporary, &path)
        };
        if let Err(error) = publish() {
            let _ = fs::remove_file(&temporary);
            eprintln!("vonk-agent: store retention references unavailable: {error}");
        }
    }
    // A stale projection may still protect content, but never authorizes an
    // explicit removal. Local ownership remains authoritative for cache relief;
    // this pass runs between claims, with no live claim using an unlinked object.
    let projection = retention_projection(data_root);
    let keep = projection
        .as_ref()
        .map(|request| request.protected_model_objects.iter().cloned().collect())
        .unwrap_or_default();
    let current = untracked.and_then(|verdict| verdict.retention.as_ref());
    if let Some(retention) = read_retention(data_root).filter(|request| Some(request) == current) {
        let requested: BTreeSet<_> = retention.remove_model_objects.into_iter().collect();
        // This is the same collector and the same local ownership predicate as
        // pressure eviction. A request selects identities, never another deleter.
        if let Some(unused) = unused_model_objects(data_root, pass.owner, &keep) {
            for (_, path, metadata) in unused {
                if path
                    .file_name()
                    .is_some_and(|name| requested.contains(&name.to_string_lossy().into_owned()))
                {
                    pass.remove(&path, &metadata);
                }
            }
        }
    }
    let mut younger = Vec::new();
    for (used, path, metadata) in
        unused_model_objects(data_root, pass.owner, &keep).unwrap_or_default()
    {
        if now.duration_since(used).is_ok_and(|age| age > GRACE) {
            pass.remove(&path, &metadata);
        } else {
            younger.push((used, path, metadata));
        }
    }
    pass.relieve(
        younger,
        disk,
        projection.map_or(0, |request| request.required_free_bytes),
    );

    pass.swept
}

/// The model objects no installation links or names, least recently used
/// first, leaving out those `keep` names; `None` when a plan cannot be read
/// (it could name any). An object is owned by every installation that links
/// it or whose plan names it. Adding or dropping installation hard links
/// updates the shared inode's change time, so a newly unreferenced object
/// receives a full grace after its final installation lets it go. Reusing an
/// unlinked object also refreshes its timestamp in the distribution client.
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

/// A complete object scan. A failed entry read is unknown, never absence.
pub fn model_objects(
    data_root: &Path,
) -> Option<Vec<vonk_agent_protocol::generated::ModelStoreObject>> {
    let owner = rustix::process::geteuid().as_raw();
    if !owned_directory(data_root, owner) {
        return None;
    }
    let root = data_root.join("distribution/models");
    let entries = match fs::read_dir(&root) {
        Ok(entries) if owned_directory(&root, owner) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Some(Vec::new()),
        _ => return None,
    };
    // Reserve half of the one-MiB inventory document for other observations.
    // Bound memory while scanning, rather than after constructing a huge list.
    const EVIDENCE_BYTES: usize = (crate::inventory::INVENTORY_OUTPUT_BYTES / 2) as usize;
    let mut evidence_bytes = 2_usize;
    let mut objects = Vec::new();
    for entry in entries {
        let entry = entry.ok()?;
        let name = entry.file_name().into_string().ok()?;
        let metadata = fs::symlink_metadata(entry.path()).ok()?;
        if name.len() == 64
            && name
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            if !metadata.is_file() {
                return None;
            }
            if metadata.uid() != owner {
                return None;
            }
            let object = vonk_agent_protocol::generated::ModelStoreObject {
                sha256: name,
                bytes: metadata.len(),
            };
            evidence_bytes =
                evidence_bytes.checked_add(serde_json::to_vec(&object).ok()?.len() + 1)?;
            if evidence_bytes > EVIDENCE_BYTES {
                return None;
            }
            objects.push(object);
        }
    }
    objects.sort_by(|a, b| a.sha256.cmp(&b.sha256));
    Some(objects)
}

/// The bytes of the model objects no installation links or names, which the
/// agent removes when space is needed, as inventory reports them; `None`
/// when a plan or the data root cannot be read.
pub fn unused_model_bytes(data_root: &Path) -> Option<u64> {
    let owner = rustix::process::geteuid().as_raw();
    if !owned_directory(data_root, owner) {
        return None;
    }
    let keep = retention_projection(data_root)
        .map(|request| request.protected_model_objects.into_iter().collect())
        .unwrap_or_default();
    Some(
        unused_model_objects(data_root, owner, &keep)?
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
    let mut keep = keep.clone();
    if let Some(retention) = retention_projection(data_root) {
        keep.extend(retention.protected_model_objects);
    }
    let mut removed = 0_u64;
    for (_, path, metadata) in unused_model_objects(data_root, owner, &keep).unwrap_or_default() {
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

/// Neither the installation nor its quarantine is on disk: its cleanup is
/// complete (an unreadable look keeps its records).
fn installation_gone(data_root: &Path, id: &str) -> bool {
    [
        data_root.join("installations").join(id),
        data_root
            .join("installation-reconciliation")
            .join(format!("{id}.partial")),
    ]
    .iter()
    .all(|path| {
        fs::symlink_metadata(path).is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound)
    })
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

    /// Retained lifecycle records need a disposable run directory so the normal
    /// runtime observer can ask the helper and Controller about their owner.
    /// A crash between scope cleanup steps can leave only the metadata behind.
    fn restore_run_scopes(&mut self, metadata_root: &Path, data_root: &Path) {
        let runs = data_root.join("runs");
        for (name, metadata) in self.list(metadata_root).into_iter().flatten() {
            if Instant::now() >= self.deadline {
                self.swept.pending = true;
                return;
            }
            if !metadata.is_dir()
                || !installation_name(&name)
                || fs::symlink_metadata(metadata_root.join(&name).join("lifecycle.json")).is_err()
                || !fs::symlink_metadata(runs.join(&name))
                    .is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound)
            {
                continue;
            }
            if let Err(error) = fs::DirBuilder::new().mode(0o700).create(&runs)
                && error.kind() != std::io::ErrorKind::AlreadyExists
            {
                eprintln!("vonk-agent: cannot restore runs directory: {error}; retried next pass");
                continue;
            }
            if !owned_directory(&runs, self.owner) {
                eprintln!(
                    "vonk-agent: run observation scope {name} waits for a readable owned runs directory; retried next pass"
                );
                continue;
            }
            match fs::DirBuilder::new().mode(0o700).create(runs.join(&name)) {
                Ok(()) => {
                    eprintln!("vonk-agent: restored observation directory for retained run {name}")
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => eprintln!(
                    "vonk-agent: cannot restore observation directory for run {name}: {error}; retried next pass"
                ),
            }
        }
    }

    /// A crashed build can leave private Podman databases that no longer open.
    /// Their contents are not liveness: only the exact owning cgroups are.
    /// This pass runs between claims; unknown systemd evidence keeps the tree.
    fn build_staging(&mut self, root: &Path, runner: &impl crate::process::ProcessRunner) {
        for (name, metadata) in self.list(root).into_iter().flatten() {
            if !self
                .now
                .duration_since(changed(&metadata))
                .is_ok_and(|age| age > GRACE)
            {
                continue;
            }
            if let Some(build_id) = name
                .get(name.len().saturating_sub(36)..)
                .and_then(|id| uuid::Uuid::parse_str(id).ok())
            {
                let request = vonk_agent_protocol::RecipeBuildCleanupRequest {
                    build_id,
                    operation_id: build_id,
                };
                match crate::recipe_builder::observe_build_running(runner, &request, self.deadline)
                {
                    Ok(false) => {}
                    Ok(true) => continue,
                    Err(error) => {
                        eprintln!(
                            "vonk-agent: build staging {name} kept: {error}; retried next pass"
                        );
                        continue;
                    }
                }
            }
            self.remove(&root.join(name), &metadata);
        }
    }

    /// An installation or run scope the Controller does not track leaves a
    /// grace after the first verdict that said so. That verdict marks the
    /// directory, the mark's age is the grace (it survives an agent restart),
    /// and a verdict that tracks it again (or, for an installation, a run that
    /// names it) removes the mark.
    fn untracked(
        &mut self,
        root: &Path,
        kind: &str,
        observed: &dyn Fn(&str) -> bool,
        untracked: &dyn Fn(&str) -> bool,
    ) {
        for (name, metadata) in self.list(root).into_iter().flatten() {
            if !metadata.is_dir() || !installation_name(&name) {
                continue;
            }
            if !observed(&name) {
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
                        "vonk-agent: store retention: the Controller does not track {kind} {name}; it leaves after a day unless tracked again"
                    ),
                    Err(error) => {
                        eprintln!("vonk-agent: store retention cannot mark {kind} {name}: {error}")
                    }
                },
                (true, Some(since)) => {
                    let aged = self
                        .now
                        .duration_since(changed(&since))
                        .is_ok_and(|age| age > GRACE);
                    if aged && self.remove(&directory, &metadata) {
                        eprintln!(
                            "vonk-agent: store retention removed {kind} {name}: the Controller has not tracked it for a day"
                        );
                    }
                }
                (false, Some(_)) => match fs::remove_file(&mark) {
                    Ok(()) => eprintln!(
                        "vonk-agent: store retention keeps {kind} {name}: it is tracked again"
                    ),
                    Err(error) => eprintln!(
                        "vonk-agent: store retention cannot unmark {kind} {name}: {error}"
                    ),
                },
                (false, None) => {}
            }
        }
    }

    /// The lock file of an installation that is gone leaves once unchanged for
    /// the grace. It is removed while held, so no claim locks it meanwhile.
    fn ownership_locks(&mut self, root: &Path, data_root: &Path) {
        for (name, metadata) in self.list(root).into_iter().flatten() {
            let Some(id) = name
                .strip_suffix(".lock")
                .filter(|id| installation_name(id))
            else {
                continue;
            };
            let aged = self
                .now
                .duration_since(changed(&metadata))
                .is_ok_and(|age| age > GRACE);
            if !metadata.is_file() || !aged || !installation_gone(data_root, id) {
                continue;
            }
            let path = root.join(&name);
            // A claim holding it reconciles the installation now.
            let Ok(Ok(held)) = fs::File::open(&path).map(HeldLock::try_exclusive) else {
                continue;
            };
            self.remove(&path, &metadata);
            drop(held);
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
        required: u64,
    ) {
        let Some((free, total)) = disk() else {
            return;
        };
        let line = total / 100 * LOW_FREE_PERCENT;
        if free >= line.max(required) {
            return;
        }
        let target = if free < line {
            required.max(line + RESERVE_FLOOR_BYTES.max(total / 100 * RESERVE_PERCENT))
        } else {
            required
        };
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
pub(crate) mod tests;

/// Only a fresh projection authorizes explicit identity-selected removals.
pub fn read_retention(
    data_root: &Path,
) -> Option<vonk_agent_protocol::generated::StoreRetentionRequest> {
    let request = retention_projection(data_root)?;
    let now = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .ok()?
        .as_secs();
    (request.issued_at <= now
        && request.expires_at > now
        && request.expires_at <= request.issued_at.saturating_add(VERDICT_MAX_AGE.as_secs()))
    .then_some(request)
}

/// Retain readable protection even after a projection expires. Missing optional
/// Controller evidence cannot veto reclamation of locally unreferenced cache.
fn retention_projection(
    data_root: &Path,
) -> Option<vonk_agent_protocol::generated::StoreRetentionRequest> {
    use std::io::Read;
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32)
        .open(data_root.join("store-retention.json"))
        .ok()?;
    let metadata = file.metadata().ok()?;
    if !metadata.is_file() || metadata.len() > 1024 * 1024 {
        return None;
    }
    let mut body = Vec::new();
    file.take(1024 * 1024 + 1).read_to_end(&mut body).ok()?;
    serde_json::from_slice(&body).ok()
}

#[cfg(test)]
mod source_guard;
