//! Helper-owned stores are collected by the existing periodic image pass.
//! Missing fences are safe: START reconstructs them only through a fresh
//! authenticated installation-intent challenge, never through an old grant.
use super::*;
use std::time::SystemTime;
use vonk_agent_protocol::store_retention::{Collector, GRACE, STORES, Store, StoreRoot};

#[derive(Default)]
struct Owners {
    installations: HashSet<String>,
    runtimes: HashSet<String>,
    mounts: HashSet<PathBuf>,
}

// One record per container: its id and the two canonical identity labels,
// then one JSON string per mount source (a model with many files has one mount
// each), then the `--` line that ends the record. The id and the end line let
// one `docker container inspect` answer for many containers and be read as a
// stream; Docker's unrelated inspection fields need no parallel wire model.
pub(super) const OWNERS_FORMAT: &str = "{{.Id}}\t{{index .Config.Labels \"ai.vonkforge.installation-id\"}}\t{{index .Config.Labels \"ai.vonkforge.target-id\"}}{{println}}{{range .Mounts}}{{json .Source}}{{println}}{{end}}--{{println}}";
const OWNER_RECORD_END: &str = "--";
/// Containers per `docker container inspect`: a pass costs one listing,
/// `ceil(n / 100)` inspections and at most one re-listing, whatever `n` is.
const OWNER_BATCH: usize = 100;

impl<R: CommandRunner> OperationExecutor<R> {
    /// Local retention delegates only an agent-owned scope. Reuse the same
    /// no-follow, same-device deletion as installation cleanup and tmp reset.
    /// This removes bytes, never workloads or signed installation authority.
    pub fn remove_agent_store(
        &self,
        request: &vonk_agent_protocol::generated::AgentStoreRemovalRequest,
    ) -> Result<(), OperationError> {
        use vonk_agent_protocol::generated::AgentRetainedStore;
        let _launches = match self.runtime_stores.try_write() {
            Ok(guard) => guard,
            Err(std::sync::TryLockError::Poisoned(guard)) => guard.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => {
                return Err(OperationError::InstallationReconciliationBusy);
            }
        };
        let store = request.store.as_str();
        let id = request.scope_id.to_string();
        let root = StoreRoot::Agent
            .path(&self.roots.agent_data, store)
            .ok_or(OperationError::UnsafePath)?;
        for path in [&self.roots.agent_data, &root] {
            require_safe_directory(path, self.runtime_request_owner_uid)?;
        }
        let flags = rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC;
        let agent = OpenOptions::new()
            .read(true)
            .custom_flags(flags.bits() as i32)
            .open(&self.roots.agent_data)?;
        let parent = rustix::fs::openat(&agent, store, flags, rustix::fs::Mode::empty())
            .map_err(errno_io)?;
        let scope = match rustix::fs::openat(&parent, id.as_str(), flags, rustix::fs::Mode::empty())
        {
            Ok(scope) => scope,
            Err(rustix::io::Errno::NOENT) => return Ok(()),
            Err(error) => return Err(errno_io(error).into()),
        };
        let metadata = rustix::fs::fstat(&scope).map_err(errno_io)?;
        let device = rustix::fs::fstat(&agent).map_err(errno_io)?.st_dev;
        if (metadata.st_dev, metadata.st_ino) != (request.device, request.inode)
            || metadata.st_dev != device
            || metadata.st_mode & 0o022 != 0
            || self
                .runtime_request_owner_uid
                .is_some_and(|uid| metadata.st_uid != uid)
        {
            return Err(OperationError::UnsafePath);
        }
        let owners = self.helper_store_owners(Instant::now() + Duration::from_secs(60))?;
        let path = root.join(&id);
        if match request.store {
            AgentRetainedStore::Runs => owners.runtimes.contains(&id),
            AgentRetainedStore::Installations => owners.installations.contains(&id),
        } || owners
            .mounts
            .iter()
            .any(|mount| mount.starts_with(&path) || path.starts_with(mount))
        {
            return Err(OperationError::InstallationReconciliationBusy);
        }
        remove_directory_contents_last(&scope, device, Some(c"untracked-since"))?;
        // A lost response is safe to retry; a replacement scope is never removed.
        let current =
            rustix::fs::statat(&parent, id.as_str(), rustix::fs::AtFlags::SYMLINK_NOFOLLOW)
                .map_err(errno_io)?;
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino) {
            return Err(OperationError::UnsafePath);
        }
        rustix::fs::unlinkat(&parent, id.as_str(), rustix::fs::AtFlags::REMOVEDIR)
            .map_err(errno_io)?;
        rustix::fs::fsync(&parent).map_err(errno_io)?;
        Ok(())
    }

    pub(super) fn collect_helper_stores(&self, now: SystemTime) {
        let deadline = Instant::now() + Duration::from_secs(60);
        // START holds the read side from challenge through Docker launch.
        // No launch can acquire mounts or fences after this snapshot.
        let _launches = match self.runtime_stores.try_write() {
            Ok(guard) => guard,
            Err(std::sync::TryLockError::Poisoned(guard)) => guard.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => {
                eprintln!(
                    "vonk-agent-helper: store collection waits for active launches; retried next periodic pass"
                );
                return;
            }
        };
        let owners = match self.helper_store_owners(deadline) {
            Ok(owners) => Some(owners),
            Err(error) => {
                eprintln!(
                    "vonk-agent-helper: store owners unavailable: {error}; owner-dependent collection retries next pass"
                );
                None
            }
        };
        if let Some(owners) = &owners {
            self.collect_compile_caches(now, deadline, &owners.mounts);
        }
        for store @ Store(name, collector, _, root_kind) in STORES {
            if *root_kind != StoreRoot::Helper || Instant::now() >= deadline {
                continue;
            }
            if let Some((prefix, suffix)) = name.split_once('*') {
                self.collect_store_entries(&self.roots.data, now, deadline, &|name, _| {
                    name.starts_with(prefix) && name.ends_with(suffix)
                });
                continue;
            }
            let root = store.path(&self.roots.data);
            match collector {
                Collector::Ended if *name == "runtime-identities/" => {
                    if let Some(owners) = &owners {
                        self.collect_store_entries(&root, now, deadline, &|_, path| {
                            !owners.mounts.iter().any(|mount| mount.starts_with(path))
                        });
                    }
                }
                Collector::Ended if *name == "incoming/" => {
                    let _package = match self.package_install.try_lock() {
                        Ok(guard) => guard,
                        Err(std::sync::TryLockError::Poisoned(guard)) => guard.into_inner(),
                        Err(std::sync::TryLockError::WouldBlock) => {
                            eprintln!(
                                "vonk-agent-helper: incoming collection waits for package custody; retried next periodic pass"
                            );
                            continue;
                        }
                    };
                    self.collect_store_entries(&root, now, deadline, &|_, _| true);
                }
                Collector::Ended => self.collect_store_entries(&root, now, deadline, &|_, _| true),
                Collector::Bookkeeping if matches!(*name, "gpu-clock/" | "package-rollback/") => {
                    // Current checkpoint/transaction and custody are bounded.
                    // The producer's lock protects interrupted scratch cleanup.
                    let lock = OpenOptions::new()
                        .read(true)
                        .write(true)
                        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
                        .open(root.join("lock"));
                    if let Ok(lock) = lock
                        && let Ok(_held) = HeldLock::try_exclusive(lock)
                    {
                        self.collect_store_entries(&root, now, deadline, &|name, _| {
                            name.starts_with('.')
                        });
                    }
                }
                Collector::Ownership | Collector::Reconciliation => {
                    if let Some(owners) = &owners {
                        self.collect_store_entries(&root, now, deadline, &|entry, _| {
                            self.helper_record_unused(store, entry, owners)
                        });
                    }
                }
                // Existing image/custody/memory-guard owners collect their stores.
                _ => {}
            }
        }
    }

    /// What every managed container (any state) keeps in use. A container
    /// whose owner cannot be proven (its record does not parse, or Docker
    /// will not inspect it) could use any store, so the pass then reports
    /// `InstallationReconciliationBusy` and every owner-dependent collection
    /// keeps everything. It stays that way for as long as the container is
    /// listed and unreadable. A container that Docker no longer lists owns
    /// nothing. Every Docker call is capped by what is left of `deadline`.
    fn helper_store_owners(&self, deadline: Instant) -> Result<Owners, OperationError> {
        let ids = self.managed_container_ids(deadline, Duration::from_secs(10))?;
        let mut owners = Owners::default();
        let mut proven = HashSet::new();
        for batch in ids.chunks(OWNER_BATCH) {
            let timeout = time_left(deadline, Duration::from_secs(30))?;
            let mut arguments = vec![
                "container".to_owned(),
                "inspect".to_owned(),
                "--format".to_owned(),
                OWNERS_FORMAT.to_owned(),
            ];
            arguments.extend(batch.iter().cloned());
            let mut records = OwnerRecords::default();
            // Whatever the outcome (a missing container makes Docker exit 1
            // after answering for the others), only complete records count;
            // the containers without one are judged below.
            let _ = self.run_docker_lines(&arguments, timeout, &mut |line| records.line(line));
            for (id, installation, runtime, mounts) in records.done {
                if batch.contains(&id) && proven.insert(id) {
                    owners.installations.insert(installation);
                    owners.runtimes.insert(runtime);
                    owners.mounts.extend(mounts);
                }
            }
        }
        let mut unproven: Vec<&String> = ids.iter().filter(|id| !proven.contains(*id)).collect();
        if !unproven.is_empty() {
            // One re-listing tells the gone from the unreadable.
            let listed = self.managed_container_ids(deadline, Duration::from_secs(10))?;
            let listed: HashSet<_> = listed.iter().collect();
            unproven.retain(|id| listed.contains(*id));
        }
        if unproven.is_empty() {
            return Ok(owners);
        }
        eprintln!(
            "vonk-agent-helper: {} managed container(s) have no provable owner ({}); owner-dependent collection keeps everything while they stay unreadable",
            unproven.len(),
            unproven
                .iter()
                .map(|id| id.as_str())
                .collect::<Vec<_>>()
                .join(",")
        );
        Err(OperationError::InstallationReconciliationBusy)
    }

    /// The ids of every managed container, in any state, read as a stream.
    fn managed_container_ids(
        &self,
        deadline: Instant,
        cap: Duration,
    ) -> Result<Vec<String>, OperationError> {
        let mut ids = Vec::new();
        let mut malformed = false;
        let output = self.run_docker_lines(
            &[
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "label=ai.vonkforge.managed=true",
            ]
            .map(str::to_owned),
            time_left(deadline, cap)?,
            &mut |line| {
                if lower_hex(line, 64) {
                    ids.push(line.to_owned());
                } else {
                    malformed = true;
                }
            },
        )?;
        if !output.success {
            return Err(OperationError::CommandFailed {
                cause: CommandFailure::exit(&output),
            });
        }
        if malformed {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(ids)
    }

    fn helper_record_unused(&self, store: &Store, name: &str, owners: &Owners) -> bool {
        let Some(installation) = name.get(..36).filter(|name| valid_artifact_id(name)) else {
            // Interrupted writes and isolated records carry no authority.
            return true;
        };
        if name == format!("{installation}.partial") {
            return false;
        }
        if store.0 == "runtime-generation-fences/"
            && let Some(runtime) = name
                .strip_suffix(".json")
                .and_then(|stem| stem.get(37..))
                .filter(|runtime| valid_artifact_id(runtime))
        {
            // A run's claim and any live/stopped container keep its fence.
            // A collected fence cannot revive a grant: prepare challenges.
            return !owners.runtimes.contains(runtime)
                && absent(&self.roots.agent_data.join("run-metadata").join(runtime));
        }
        !owners.installations.contains(installation)
            && absent(
                &self
                    .roots
                    .agent_data
                    .join("installations")
                    .join(installation),
            )
            && absent(
                &self
                    .roots
                    .data
                    .join("installation-reconciliation")
                    .join(format!("{installation}.partial")),
            )
    }

    fn collect_store_entries(
        &self,
        root: &Path,
        now: SystemTime,
        deadline: Instant,
        unused: &dyn Fn(&str, &Path) -> bool,
    ) {
        let owner = if root == self.roots.incoming {
            self.package_owner_uid
                .unwrap_or(rustix::process::geteuid().as_raw())
        } else {
            rustix::process::geteuid().as_raw()
        };
        for ancestor in root
            .ancestors()
            .take_while(|path| path.starts_with(&self.roots.data))
        {
            let expected_owner = if ancestor == root {
                owner
            } else {
                rustix::process::geteuid().as_raw()
            };
            if require_safe_directory(ancestor, Some(expected_owner)).is_err() {
                return;
            }
        }
        let Ok(entries) = fs::read_dir(root) else {
            return;
        };
        for entry in entries.flatten() {
            if Instant::now() >= deadline {
                return;
            }
            let path = entry.path();
            let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
                continue;
            };
            let Ok(metadata) = fs::symlink_metadata(&path) else {
                continue;
            };
            if metadata.uid() != owner || !old(&metadata, now) {
                continue;
            }
            let installation = name.get(..36).filter(|id| valid_artifact_id(id));
            let _installation_lock = if root.file_name().is_some_and(|name| {
                matches!(
                    name.to_str(),
                    Some(
                        "runtime-ownership"
                            | "installation-intent-fences"
                            | "runtime-generation-fences"
                            | "installation-reconciliation"
                    )
                )
            }) {
                match installation
                    .map(|id| self.lock_installation_runtime(id))
                    .transpose()
                {
                    Ok(lock) => lock,
                    Err(error) => {
                        eprintln!(
                            "vonk-agent-helper: store entry {} waits for installation lock: {error}; retried next periodic pass",
                            path.display()
                        );
                        continue;
                    }
                }
            } else {
                None
            };
            if !unused(&name, &path) {
                continue;
            }
            // A writer since the owner scan renews the entry's grace.
            if !fs::symlink_metadata(&path).is_ok_and(|current| {
                artifact_identity(&current) == artifact_identity(&metadata) && old(&current, now)
            }) {
                continue;
            }
            let removed = if metadata.is_dir() {
                fs::remove_dir_all(&path)
            } else {
                fs::remove_file(&path)
            };
            if let Err(error) = removed {
                eprintln!(
                    "vonk-agent-helper: store entry {} kept: {error}; retried next pass",
                    path.display()
                );
            }
        }
    }
}

/// Time left before `deadline`, at most `cap`; none left is a busy pass.
fn time_left(deadline: Instant, cap: Duration) -> Result<Duration, OperationError> {
    let left = deadline.saturating_duration_since(Instant::now());
    if left.is_zero() {
        return Err(OperationError::InstallationReconciliationBusy);
    }
    Ok(left.min(cap))
}

/// The complete `OWNERS_FORMAT` records of a streamed inspection. A record
/// whose id, labels or mounts are not what the format produces, or that ends
/// before its end line, is dropped, so its container stays unproven.
#[derive(Default)]
struct OwnerRecords {
    header: Option<Option<(String, String, String)>>,
    mounts: Vec<PathBuf>,
    valid: bool,
    done: Vec<(String, String, String, Vec<PathBuf>)>,
}

impl OwnerRecords {
    fn line(&mut self, line: &str) {
        if line == OWNER_RECORD_END {
            if let (Some(Some((id, installation, runtime))), true) =
                (self.header.take(), self.valid)
            {
                self.done
                    .push((id, installation, runtime, std::mem::take(&mut self.mounts)));
            }
            *self = Self {
                done: std::mem::take(&mut self.done),
                ..Self::default()
            };
            return;
        }
        if self.header.is_none() {
            let mut fields = line.split('\t');
            let header = match (fields.next(), fields.next(), fields.next(), fields.next()) {
                (Some(id), Some(installation), Some(runtime), None)
                    if lower_hex(id, 64)
                        && valid_artifact_id(installation)
                        && valid_artifact_id(runtime) =>
                {
                    Some((id.to_owned(), installation.to_owned(), runtime.to_owned()))
                }
                _ => None,
            };
            self.valid = header.is_some();
            self.header = Some(header);
            return;
        }
        match serde_json::from_str::<PathBuf>(line) {
            Ok(source) if source.is_absolute() => self.mounts.push(source),
            _ => self.valid = false,
        }
    }
}

fn absent(path: &Path) -> bool {
    fs::symlink_metadata(path).is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound)
}
fn old(metadata: &fs::Metadata, now: SystemTime) -> bool {
    let changed = SystemTime::UNIX_EPOCH
        + Duration::new(
            u64::try_from(metadata.ctime()).unwrap_or(0),
            u32::try_from(metadata.ctime_nsec()).unwrap_or(0),
        );
    now.duration_since(changed).is_ok_and(|age| age > GRACE)
}

#[cfg(test)]
mod tests;
