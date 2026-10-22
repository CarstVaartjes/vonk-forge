//! Unused runtime images.
//!
//! An install (or a distribution ahead of a load) pulls its compiled runtime
//! image into Docker, and Docker keeps it until something removes it: an
//! uninstall does not. Every pull rewrites the image's receipt in
//! `runtime-images/<address>`, and an uninstall's cleanup dates it anew, so
//! the receipt's age is the time since the image was last pulled or its
//! installation removed. An image that no installation on this host names
//! and whose receipt is older than the grace is unused: it is removed, and a
//! later install pulls it again from the Controller's store. The grace also
//! covers an install between its pull and the plan it writes after it.
//!
//! Docker refuses to remove an image a container still uses, a stopped one
//! included, so such an image stays with that container. A pass that cannot
//! read what the installations name removes nothing. Receiptless Docker
//! images in the managed namespace get a durable local discovery grace too.

use super::*;

/// How long an image no installation names stays after its last pull.
pub const UNUSED_IMAGE_GRACE: Duration = vonk_agent_protocol::store_retention::GRACE;
/// How often the helper looks for unused images.
pub const UNUSED_IMAGE_SWEEP_INTERVAL: Duration = Duration::from_secs(60);
/// One `docker image rm` of a large image.
const IMAGE_REMOVE_TIMEOUT: Duration = Duration::from_secs(120);

impl<R: CommandRunner> OperationExecutor<R> {
    /// Retire unowned exited containers before collecting their images.
    /// What this periodic pass cannot remove waits for the next one.
    pub fn sweep_unused_runtime_images(&self) {
        self.sweep_stray_jobs();
        self.sweep_exited_serving();
        self.sweep_unused_runtime_images_at(std::time::SystemTime::now());
    }

    pub(super) fn sweep_unused_runtime_images_at(&self, now: std::time::SystemTime) {
        self.collect_helper_stores(now);
        let disk = || {
            let stat = rustix::fs::statvfs(&self.roots.agent_data).ok()?;
            stat.f_bavail.checked_mul(stat.f_frsize)
        };
        self.sweep_runtime_images_unused_for(UNUSED_IMAGE_GRACE, &disk);
    }

    pub(super) fn sweep_runtime_images_unused_for(
        &self,
        grace: Duration,
        disk: &dyn Fn() -> Option<u64>,
    ) {
        let retention = self.store_retention_request();
        let named = match self.installation_image_addresses() {
            Ok(named) => named,
            Err(error) => {
                eprintln!(
                    "vonk-agent-helper: unused image sweep deferred: installations unreadable: {error}"
                );
                return;
            }
        };
        self.collect_unreceipted_images(&named, retention.as_ref(), grace);
        let entries = match fs::read_dir(&self.roots.runtime_image_receipts) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return,
            Err(error) => {
                eprintln!(
                    "vonk-agent-helper: unused image sweep deferred: receipts unreadable: {error}"
                );
                return;
            }
        };
        let mut entries: Vec<_> = entries.flatten().collect();
        entries.sort_by_key(|entry| {
            entry
                .metadata()
                .and_then(|metadata| metadata.modified())
                .ok()
        });
        let deadline = Instant::now() + Duration::from_secs(60);
        for entry in entries {
            if Instant::now() >= deadline {
                break;
            }
            let Some(address) = entry
                .file_name()
                .to_str()
                .filter(|name| lower_hex(name, 64))
                .map(str::to_owned)
            else {
                continue;
            };
            let Ok(receipt) = self.read_image_receipt(&address) else {
                continue;
            };
            let Ok((image, digest)) = parse_local_image_reference(&receipt.local_image_reference)
            else {
                continue;
            };
            let names = [
                address.as_str(),
                digest.trim_start_matches("sha256:"),
                image.rsplit('-').next().unwrap_or_default(),
            ];
            if names.iter().any(|name| {
                named.contains(*name)
                    || retention.as_ref().is_some_and(|request| {
                        request
                            .protected_image_addresses
                            .iter()
                            .any(|kept| kept.as_str() == *name)
                    })
            }) {
                continue;
            }
            let pressured = retention.as_ref().is_some_and(|request| {
                disk().is_some_and(|free| free < request.required_free_bytes)
            });
            // A pull newer than this reference scan may belong to a newly
            // accepted operation whose plan is not on disk yet. A later scan
            // must judge it before pressure may bypass the age grace.
            let scanned = retention
                .as_ref()
                .map_or_else(std::time::SystemTime::now, |request| {
                    std::time::SystemTime::UNIX_EPOCH + Duration::from_secs(request.issued_at)
                });
            self.remove_unused_image(
                &address,
                &image,
                if pressured { Duration::ZERO } else { grace },
                scanned,
                disk,
            );
        }
    }

    /// Discover images from Docker itself, including leftovers with no receipt.
    /// Keep aliases of any protected content, and every image a container names.
    fn collect_unreceipted_images(
        &self,
        named: &HashSet<String>,
        retention: Option<&vonk_agent_protocol::generated::StoreRetentionRequest>,
        grace: Duration,
    ) {
        let _pulls = match self.image_pulls.try_write() {
            Ok(guard) => guard,
            Err(std::sync::TryLockError::Poisoned(guard)) => guard.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => return,
        };
        let _launches = match self.runtime_stores.try_write() {
            Ok(guard) => guard,
            Err(std::sync::TryLockError::Poisoned(guard)) => guard.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => return,
        };
        let listing = self.run_docker_with_timeout(
            &[
                "image",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                "reference=localhost/vonk/*",
                "--format",
                "{{.Repository}}:{{.Tag}}\t{{.ID}}",
            ]
            .map(str::to_owned),
            Duration::from_secs(15),
        );
        let listing = match listing {
            Ok(output) if output.success => output,
            other => {
                eprintln!(
                    "vonk-agent-helper: Docker image inventory unavailable: {other:?}; retried next pass"
                );
                return;
            }
        };
        let Ok(text) = std::str::from_utf8(&listing.stdout) else {
            return;
        };
        let images: Vec<_> = text
            .lines()
            .filter_map(|line| line.split_once('\t'))
            .collect();
        let protected = |reference: &str| {
            let name = reference.split(':').next().unwrap_or_default();
            let address = name.rsplit('-').next().unwrap_or_default();
            named.contains(address)
                || retention.is_some_and(|request| {
                    request
                        .protected_image_addresses
                        .iter()
                        .any(|kept| kept == address)
                })
                || self.read_image_receipt(address).is_ok()
        };
        let kept: HashSet<_> = images
            .iter()
            .filter(|(reference, _)| protected(reference))
            .map(|(_, id)| *id)
            .collect();
        if fs::create_dir_all(&self.roots.runtime_image_receipts).is_err() {
            return;
        }
        let configs: HashSet<_> = images.iter().map(|(_, id)| *id).collect();
        if let Ok(markers) = fs::read_dir(&self.roots.runtime_image_receipts) {
            for entry in markers.flatten() {
                if let Some(config) = entry
                    .file_name()
                    .to_str()
                    .and_then(|name| name.strip_prefix(".untracked-"))
                    .filter(|config| lower_hex(config, 64))
                    && !configs.contains(format!("sha256:{config}").as_str())
                {
                    let _ = fs::remove_file(entry.path());
                }
            }
        }
        let deadline = Instant::now() + Duration::from_secs(60);
        for (reference, id) in images {
            if Instant::now() >= deadline {
                break;
            }
            let repository = reference.split(':').next().unwrap_or_default();
            if ![
                "localhost/vonk/compiled-runtime-",
                "localhost/vonk/runtime-adapter-",
                "localhost/vonk/recipe-build-",
            ]
            .iter()
            .any(|prefix| repository.starts_with(prefix))
                || !id
                    .strip_prefix("sha256:")
                    .is_some_and(|id| lower_hex(id, 64))
            {
                continue;
            }
            let marker = self
                .roots
                .runtime_image_receipts
                .join(format!(".untracked-{}", &id[7..]));
            if kept.contains(id) {
                let _ = fs::remove_file(&marker);
                continue;
            }
            match self.image_has_no_containers(id) {
                Some(true) => {}
                Some(false) => {
                    let _ = fs::remove_file(&marker);
                    continue;
                }
                None => continue,
            }
            // Docker's Created is the upstream build time, not the local pull
            // time. A receiptless image starts a local grace when discovered;
            // a crash between tagging and receipt publication cannot shorten it.
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&marker)
            {
                Ok(_) => continue,
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => {
                    eprintln!("vonk-agent-helper: orphan image {id} grace unavailable: {error}");
                    continue;
                }
            }
            if !fs::symlink_metadata(&marker).is_ok_and(|metadata| {
                metadata.is_file()
                    && metadata.uid() == rustix::process::geteuid().as_raw()
                    && metadata
                        .modified()
                        .is_ok_and(|time| time.elapsed().is_ok_and(|age| age >= grace))
            }) {
                continue;
            }
            let inspected = self.run_docker_with_timeout(
                &Self::image_inspection_arguments(reference, "{{.Id}}"),
                Duration::from_secs(15),
            );
            let Ok(output) = inspected else { continue };
            if !output.success {
                continue;
            }
            let Ok(fields) = std::str::from_utf8(&output.stdout) else {
                continue;
            };
            if fields.trim() != id {
                continue;
            }
            // Pulls and launches are fenced above. Remove each owned alias
            // without force so shared tags do not strand the entire image.
            if let Err(error) = self
                .run_docker_with_timeout(
                    &["image", "rm", reference].map(str::to_owned),
                    IMAGE_REMOVE_TIMEOUT,
                )
                .and_then(|output| {
                    if output.success {
                        Ok(())
                    } else {
                        Err(CommandFailure::exit(&output).into())
                    }
                })
            {
                eprintln!("vonk-agent-helper: orphan image {id} kept: {error}; retried next pass");
            }
        }
    }

    fn image_has_no_containers(&self, image: &str) -> Option<bool> {
        match self.run_docker_with_timeout(
            &[
                "container".to_owned(),
                "ls".to_owned(),
                "--all".to_owned(),
                "--quiet".to_owned(),
                "--filter".to_owned(),
                format!("ancestor={image}"),
            ],
            Duration::from_secs(15),
        ) {
            Ok(output) if output.success => Some(output.stdout.iter().all(u8::is_ascii_whitespace)),
            other => {
                eprintln!(
                    "vonk-agent-helper: container references for image {image} unavailable: {other:?}; retried next pass"
                );
                None
            }
        }
    }

    /// An agent-owned projection of the Controller's reference scan. Stale
    /// protections still narrow collection; unreadable evidence retains the
    /// age grace and all local installation, pull and container checks.
    /// This only narrows autonomous cache collection; it never authorizes a
    /// container removal or bypasses Docker's in-use image check.
    fn store_retention_request(
        &self,
    ) -> Option<vonk_agent_protocol::generated::StoreRetentionRequest> {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32,
            )
            .open(self.roots.agent_data.join("store-retention.json"))
            .ok()?;
        let metadata = file.metadata().ok()?;
        if !metadata.is_file() || metadata.len() > 1024 * 1024 {
            return None;
        }
        let mut body = Vec::new();
        file.take(1024 * 1024 + 1).read_to_end(&mut body).ok()?;
        serde_json::from_slice(&body).ok()
    }

    /// Remove one image unless a pull is in flight or renewed it since.
    fn remove_unused_image(
        &self,
        address: &str,
        image: &str,
        grace: Duration,
        scanned: std::time::SystemTime,
        disk: &dyn Fn() -> Option<u64>,
    ) {
        // A pull holds this for its whole duration, so no image is removed
        // under a pull that found it present.
        let _no_pull = match self.image_pulls.try_write() {
            Ok(guard) => guard,
            Err(std::sync::TryLockError::Poisoned(guard)) => guard.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => return,
        };
        let _launches = match self.runtime_stores.try_write() {
            Ok(guard) => guard,
            Err(std::sync::TryLockError::Poisoned(guard)) => guard.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => return,
        };
        if self.image_has_no_containers(image) != Some(true) {
            return;
        }
        let receipt = self.roots.runtime_image_receipts.join(address);
        // A receipt dated ahead of the clock reads as just pulled.
        let Some(unused_for) = fs::symlink_metadata(&receipt)
            .and_then(|metadata| metadata.modified())
            .ok()
            .filter(|pulled| *pulled <= scanned)
            .map(|pulled| pulled.elapsed().unwrap_or_default())
            .filter(|age| *age >= grace)
        else {
            return;
        };
        let before = disk();
        let removed = self.run_docker_with_timeout(
            &["image".to_owned(), "rm".to_owned(), image.to_owned()],
            IMAGE_REMOVE_TIMEOUT,
        );
        let gone = match &removed {
            Ok(output) if output.success => true,
            // Already gone: its receipt names nothing any more.
            _ => matches!(self.inspect_runtime_image_if_present(image), Ok(None)),
        };
        if !gone {
            let cause = match removed {
                Ok(output) => CommandFailure::exit(&output).to_string(),
                Err(error) => error.to_string(),
            };
            eprintln!(
                "vonk-agent-helper: unused runtime image {image} kept: {cause}; the next sweep tries again"
            );
            return;
        }
        let _ = fs::remove_file(&receipt);
        if let (Some(before), Some(after)) = (before, disk())
            && after > before
        {
            eprintln!(
                "vonk-agent-helper: removed runtime image {image}; observed free-space increase {} bytes",
                after - before
            );
        }
        eprintln!(
            "vonk-agent-helper: removed runtime image {image}: no installation names it and it was last pulled {}s ago",
            unused_for.as_secs()
        );
    }

    /// The image addresses every installation on this host names. An
    /// installation without a readable plan names nothing (it cannot start);
    /// a directory that cannot be listed or read makes the answer unknown.
    fn installation_image_addresses(&self) -> std::io::Result<HashSet<String>> {
        let mut named = HashSet::new();
        let installations = match fs::read_dir(self.roots.agent_data.join("installations")) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(named),
            Err(error) => return Err(error),
        };
        for entry in installations {
            let entry = entry?;
            if !entry.file_type()?.is_dir()
                || !entry.file_name().to_str().is_some_and(valid_artifact_id)
            {
                continue;
            }
            let Some(plan) = read_plan(&entry.path().join("spec.json"))? else {
                continue;
            };
            named.extend(plan_image_addresses(&plan));
        }
        Ok(named)
    }

    /// An uninstall starts its image's grace: the image reads as pulled now,
    /// so it stays a full grace after its installation is gone. Best effort:
    /// without it the grace runs from the image's last pull.
    pub(super) fn start_image_grace(&self, installation_id: &str) {
        let spec = self
            .roots
            .agent_data
            .join("installations")
            .join(installation_id)
            .join("spec.json");
        let Ok(Some(plan)) = read_plan(&spec) else {
            return;
        };
        for address in plan_image_addresses(&plan) {
            let _ = OpenOptions::new()
                .write(true)
                .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
                .open(self.roots.runtime_image_receipts.join(address))
                .and_then(|receipt| receipt.set_modified(std::time::SystemTime::now()));
        }
    }
}

/// The image addresses a plan names: its layout and its manifest digest.
fn plan_image_addresses(plan: &CompiledExecutionPlan) -> impl Iterator<Item = String> {
    let image = &plan.runtime_image;
    [
        Some(image.oci_layout_sha256.as_str()),
        image.image_digest.strip_prefix("sha256:"),
    ]
    .into_iter()
    .flatten()
    .filter(|address| lower_hex(address, 64))
    .map(str::to_owned)
    .collect::<Vec<_>>()
    .into_iter()
}

/// An installation's plan; `None` when it has none the agent could start
/// from (absent, a link, not a regular file, too large, not a plan).
fn read_plan(path: &Path) -> std::io::Result<Option<CompiledExecutionPlan>> {
    let limit = vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES as u64;
    let file = match OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32)
        .open(path)
    {
        Ok(file) => file,
        Err(error)
            if error.kind() == std::io::ErrorKind::NotFound
                || error.raw_os_error() == Some(rustix::io::Errno::LOOP.raw_os_error()) =>
        {
            return Ok(None);
        }
        Err(error) => return Err(error),
    };
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len() > limit {
        return Ok(None);
    }
    let mut body = Vec::new();
    file.take(limit + 1).read_to_end(&mut body)?;
    Ok(serde_json::from_slice(&body).ok())
}

#[cfg(test)]
mod tests;
