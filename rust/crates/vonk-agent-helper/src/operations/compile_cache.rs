//! Disposable JIT bytes. Authority remains the signed plan, never this store.
use super::*;
use std::{os::unix::fs::DirBuilderExt, time::SystemTime};
use vonk_agent_protocol::store_retention::GRACE;

pub(super) const GPU_QUERY: &str = "--query-gpu=compute_cap,driver_version";
const STORE: &str = "compile-cache";
const FALLBACK_STORE: &str = "compile-cache-fallback";

impl<R: CommandRunner> OperationExecutor<R> {
    /// Called only after exact-plan validation and proven container absence,
    /// under runtime_stores' launch lock. Cache failure cannot veto a run.
    pub(super) fn bind_compile_cache(
        &self,
        run: &mut ValidatedDockerRun,
    ) -> Result<(), OperationError> {
        let persistent = self.compile_cache_identity(run).and_then(|key| {
            let root = self.roots.data.join(STORE);
            cache_directory(&root)?;
            let entry = root.join(key);
            cache_directory(&entry)?;
            let cache_data = self.prepare_compile_cache_data(&entry, run.uid)?;
            File::open(&entry)?.set_modified(SystemTime::now())?;
            Ok(cache_data)
        });
        let source = match persistent {
            Ok(source) => source,
            Err(error) => {
                eprintln!(
                    "vonk-agent-helper: warning: run {} persistent compile cache unavailable: {error}; using a fresh per-run cache; next start retries persistence",
                    run.run_id
                );
                // Keep privileged scratch with its privileged collector. An
                // agent-owned run tree cannot chmod/remove helper-owned dirs.
                let root = self.roots.data.join(FALLBACK_STORE);
                cache_directory(&root)?;
                let entry = root.join(format!("{}-{}", run.run_id, uuid::Uuid::new_v4()));
                cache_directory(&entry)?;
                self.prepare_compile_cache_data(&entry, run.uid)?
            }
        };
        let old = format!(
            "type=bind,src={},dst={}",
            run.cache_root.display(),
            vonk_agent_protocol::generated::CompileCacheMount::OutputsCache.as_str()
        );
        let mount = run
            .arguments
            .iter_mut()
            .find(|arg| **arg == old)
            .ok_or(OperationError::InvalidOperation)?;
        *mount = format!(
            "type=bind,src={},dst={}",
            source.display(),
            vonk_agent_protocol::generated::CompileCacheMount::OutputsCache.as_str()
        );
        Ok(())
    }

    fn compile_cache_identity(&self, run: &ValidatedDockerRun) -> Result<String, OperationError> {
        let host = if run.gpu {
            let output = self
                .runner
                .run_with_timeout(
                    Path::new(gpu_clock::NVIDIA_SMI),
                    &[
                        GPU_QUERY.to_owned(),
                        vonk_agent_protocol::gpu_clock::CSV_FORMAT.to_owned(),
                    ],
                    Duration::from_secs(10),
                )
                .map_err(CommandFailure::Runner)?;
            if !output.success {
                return Err(CommandFailure::exit(&output).into());
            }
            Some(String::from_utf8(output.stdout).map_err(|_| OperationError::InvalidArtifact)?)
        } else {
            // A GPU-free signed plan needs no NVIDIA identity. Keep CPU/GPU
            // namespaces distinct; never reuse an unknown GPU bucket.
            None
        };
        let devices = host
            .as_deref()
            .map(|host| {
                let mut devices = host
                    .lines()
                    .map(|line| {
                        let (arch, driver) = line.trim().split_once(',')?;
                        let arch = arch.trim();
                        let driver = driver.trim();
                        let valid = |value: &str| {
                            !value.is_empty()
                                && value.bytes().all(|b| b.is_ascii_digit() || b == b'.')
                        };
                        (valid(arch) && valid(driver)).then_some((arch, driver))
                    })
                    .collect::<Option<Vec<_>>>()
                    .filter(|rows| !rows.is_empty())
                    .ok_or(OperationError::InvalidArtifact)?;
                devices.sort_unstable();
                Ok::<_, OperationError>(devices)
            })
            .transpose()?;
        // Preserve every engine env/argv value, including empty and multiline
        // values. Per-start platform rendezvous/listen coordinates and job
        // observation deadlines are not compiler inputs; retaining them would
        // defeat reuse when a dual run gets a fresh rendezvous port.
        let env = run.arguments[..run.image_index]
            .windows(2)
            .filter(|pair| pair[0] == "--env")
            .map(|pair| &pair[1])
            .filter(|value| compilation_environment(value))
            .collect::<Vec<_>>();
        canonical_json(&(
            &run.platform_manifest_digest,
            run.gpu,
            std::env::consts::ARCH,
            devices,
            run.uid,
            run.gid,
            &run.entrypoint,
            env,
            &run.arguments[run.image_index + 1..],
        ))
        .map(|bytes| hex_sha256(&bytes))
        .map_err(|_| OperationError::InvalidOperation)
    }

    fn prepare_compile_cache_data(
        &self,
        entry: &Path,
        uid: u32,
    ) -> Result<PathBuf, OperationError> {
        let cache_data = entry.join("data");
        create_cache_directory(&cache_data)?;
        require_runtime_directory(&cache_data, Some(rustix::process::geteuid().as_raw()), uid)?;
        if fs::symlink_metadata(&cache_data)?.mode() & 0o700 != 0o700 {
            return Err(OperationError::InvalidArtifact);
        }
        let probe = cache_data.join(format!(".probe-{}", uuid::Uuid::new_v4()));
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&probe)?;
        fs::remove_file(probe)?;
        let output = self
            .runner
            .run(
                Path::new("/usr/bin/setfacl"),
                &[
                    "-m".to_owned(),
                    format!("u:{uid}:rwx"),
                    cache_data.display().to_string(),
                ],
            )
            .map_err(CommandFailure::Runner)?;
        if !output.success {
            return Err(CommandFailure::exit(&output).into());
        }
        // HOME is platform-owned and must be writable before image entrypoint.
        let home = cache_data.join("home");
        create_cache_directory(&home)?;
        require_runtime_directory(&home, Some(rustix::process::geteuid().as_raw()), uid)?;
        let output = self
            .runner
            .run(
                Path::new("/usr/bin/setfacl"),
                &[
                    "-m".to_owned(),
                    format!("u:{uid}:rwx"),
                    home.display().to_string(),
                ],
            )
            .map_err(CommandFailure::Runner)?;
        if !output.success {
            return Err(CommandFailure::exit(&output).into());
        }
        Ok(cache_data)
    }

    pub(super) fn collect_compile_caches(
        &self,
        now: SystemTime,
        deadline: Instant,
        mounts: &HashSet<PathBuf>,
    ) {
        self.collect_compile_cache_store(&self.roots.data.join(STORE), now, deadline, mounts);
        self.collect_compile_cache_store(
            &self.roots.data.join(FALLBACK_STORE),
            now,
            deadline,
            mounts,
        );
    }

    fn collect_compile_cache_store(
        &self,
        root: &Path,
        now: SystemTime,
        deadline: Instant,
        mounts: &HashSet<PathBuf>,
    ) {
        if require_exact_directory(root, Some(rustix::process::geteuid().as_raw()), 0o700).is_err()
        {
            return;
        }
        let Ok(entries) = fs::read_dir(root) else {
            return;
        };
        let mut entries = entries.filter_map(Result::ok).collect::<Vec<_>>();
        entries.sort_by_key(|entry| entry.metadata().and_then(|m| m.modified()).ok());
        let retention = self.store_retention_request().filter(|request| {
            now.duration_since(SystemTime::UNIX_EPOCH)
                .is_ok_and(|elapsed| {
                    request.issued_at <= elapsed.as_secs() && elapsed.as_secs() < request.expires_at
                })
        });
        for entry in entries {
            if Instant::now() >= deadline {
                break;
            }
            let path = entry.path();
            if mounts.iter().any(|mount| mount.starts_with(&path)) {
                // Last use means the end of use, not the time population began.
                let _ = File::open(&path).and_then(|dir| dir.set_modified(now));
                continue;
            }
            let Ok(metadata) = fs::symlink_metadata(&path) else {
                continue;
            };
            if metadata.uid() != rustix::process::geteuid().as_raw() {
                continue;
            }
            let age = metadata
                .modified()
                .ok()
                .and_then(|time| now.duration_since(time).ok());
            let pressured = retention.as_ref().is_some_and(|request| {
                rustix::fs::statvfs(&self.roots.agent_data).is_ok_and(|stat| {
                    stat.f_bavail.saturating_mul(stat.f_frsize) < request.required_free_bytes
                })
            });
            if !pressured && !age.is_some_and(|age| age >= GRACE) {
                continue;
            }
            let result = (|| {
                if !metadata.is_dir() || metadata.file_type().is_symlink() {
                    fs::remove_file(&path)?;
                    return Ok(());
                }
                let dir = OpenOptions::new()
                    .read(true)
                    .custom_flags(
                        (rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::NOFOLLOW).bits()
                            as i32,
                    )
                    .open(&path)?;
                remove_directory_contents(&dir, metadata.dev())?;
                fs::remove_dir(&path)?;
                Ok::<_, OperationError>(())
            })();
            if let Err(error) = result {
                eprintln!(
                    "vonk-agent-helper: compile cache {} retained: {error}; next periodic pass retries",
                    path.display()
                );
            }
        }
    }
}

fn compilation_environment(value: &str) -> bool {
    ![
        "VONK_LOCAL_ADDR=",
        "VONK_MASTER_ADDR=",
        "VONK_MASTER_PORT=",
        "VONK_LISTEN_HOST=",
        "VONK_LISTEN_PORT=",
        "VONK_JOB_TIMEOUT_SECONDS=",
    ]
    .iter()
    .any(|name| value.starts_with(name))
}

// Atomic private creation; another launch may populate the same key first.
fn create_cache_directory(path: &Path) -> Result<(), OperationError> {
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(()),
        Err(error) => Err(error.into()),
    }
}
fn cache_directory(path: &Path) -> Result<(), OperationError> {
    create_cache_directory(path)?;
    require_exact_directory(path, Some(rustix::process::geteuid().as_raw()), 0o700).map(|_| ())
}

#[cfg(test)]
mod tests;
