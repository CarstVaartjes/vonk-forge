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

// Docker emits the two canonical identity labels, then one JSON string per
// mount source. Its unrelated inspection fields need no parallel wire model.
const OWNERS_FORMAT: &str = "{{index .Config.Labels \"ai.vonkforge.installation-id\"}}\t{{index .Config.Labels \"ai.vonkforge.target-id\"}}{{println}}{{range .Mounts}}{{json .Source}}{{println}}{{end}}";

impl<R: CommandRunner> OperationExecutor<R> {
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

    fn helper_store_owners(&self, deadline: Instant) -> Result<Owners, OperationError> {
        let output = self.run_docker_with_timeout(
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
            Duration::from_secs(10),
        )?;
        if !output.success {
            return Err(OperationError::CommandFailed {
                cause: CommandFailure::exit(&output),
            });
        }
        let listing =
            std::str::from_utf8(&output.stdout).map_err(|_| OperationError::InvalidArtifact)?;
        let mut owners = Owners::default();
        for id in listing.lines() {
            if Instant::now() >= deadline {
                return Err(OperationError::InstallationReconciliationBusy);
            }
            if !lower_hex(id, 64) {
                return Err(OperationError::InvalidArtifact);
            }
            // Reuse the helper's existing bounded Docker inspection path.
            if deadline.saturating_duration_since(Instant::now()) < Duration::from_secs(30) {
                return Err(OperationError::InstallationReconciliationBusy);
            }
            let output = self
                .container_fields(id, OWNERS_FORMAT)
                .map_err(CommandFailure::Runner)?;
            if !output.success {
                return Err(OperationError::CommandFailed {
                    cause: CommandFailure::exit(&output),
                });
            }
            let text =
                std::str::from_utf8(&output.stdout).map_err(|_| OperationError::InvalidArtifact)?;
            let mut lines = text.lines();
            let (installation, runtime) = lines
                .next()
                .and_then(|line| line.split_once('\t'))
                .filter(|(installation, runtime)| {
                    valid_artifact_id(installation) && valid_artifact_id(runtime)
                })
                .ok_or(OperationError::InvalidArtifact)?;
            owners.installations.insert(installation.to_owned());
            owners.runtimes.insert(runtime.to_owned());
            for line in lines {
                let source: PathBuf =
                    serde_json::from_str(line).map_err(|_| OperationError::InvalidArtifact)?;
                if !source.is_absolute() {
                    return Err(OperationError::InvalidArtifact);
                }
                owners.mounts.insert(source);
            }
        }
        Ok(owners)
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
