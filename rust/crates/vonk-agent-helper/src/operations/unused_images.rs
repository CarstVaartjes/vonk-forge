//! Unused runtime images.
//!
//! An install (or a distribution ahead of a load) pulls its compiled runtime
//! image into Docker, and Docker keeps it until something removes it: an
//! uninstall does not. Every pull rewrites the image's receipt in
//! `runtime-images/<address>`, so the receipt's age is the time since the
//! image was last pulled. An image that no installation on this host names
//! and that was last pulled longer than the grace ago is unused: it is
//! removed, and a later install pulls it again from the Controller's store.
//!
//! Docker refuses to remove an image a container still uses, a stopped one
//! included, so such an image stays with that container. A pass that cannot
//! read what the installations name removes nothing.

use super::*;

/// How long an image no installation names stays after its last pull.
pub const UNUSED_IMAGE_GRACE: Duration = Duration::from_secs(24 * 60 * 60);
/// How often the helper looks for unused images.
pub const UNUSED_IMAGE_SWEEP_INTERVAL: Duration = Duration::from_secs(60 * 60);
/// One `docker image rm` of a large image.
const IMAGE_REMOVE_TIMEOUT: Duration = Duration::from_secs(120);

impl<R: CommandRunner> OperationExecutor<R> {
    /// One bounded pass; what it cannot remove now waits for the next one.
    pub fn sweep_unused_runtime_images(&self) {
        self.sweep_runtime_images_unused_for(UNUSED_IMAGE_GRACE);
    }

    pub(super) fn sweep_runtime_images_unused_for(&self, grace: Duration) {
        let named = match self.installation_image_addresses() {
            Ok(named) => named,
            Err(error) => {
                eprintln!(
                    "vonk-agent-helper: unused image sweep deferred: installations unreadable: {error}"
                );
                return;
            }
        };
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
        for entry in entries.flatten() {
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
            if names.iter().any(|name| named.contains(*name)) {
                continue;
            }
            self.remove_unused_image(&address, &image, grace);
        }
    }

    /// Remove one image unless a pull is in flight or renewed it since.
    fn remove_unused_image(&self, address: &str, image: &str, grace: Duration) {
        // A pull holds this for its whole duration, so no image is removed
        // under a pull that found it present.
        let _no_pull = match self.image_pulls.try_write() {
            Ok(guard) => guard,
            Err(std::sync::TryLockError::Poisoned(guard)) => guard.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => return,
        };
        let receipt = self.roots.runtime_image_receipts.join(address);
        // A receipt dated ahead of the clock reads as just pulled.
        let Some(unused_for) = fs::symlink_metadata(&receipt)
            .and_then(|metadata| metadata.modified())
            .ok()
            .map(|pulled| pulled.elapsed().unwrap_or_default())
            .filter(|age| *age >= grace)
        else {
            return;
        };
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
            let image = &plan["runtime_image"];
            for name in [
                image["oci_layout_sha256"].as_str(),
                image["image_digest"]
                    .as_str()
                    .map(|digest| digest.trim_start_matches("sha256:")),
            ]
            .into_iter()
            .flatten()
            {
                named.insert(name.to_owned());
            }
        }
        Ok(named)
    }
}

/// An installation's plan; `None` when it has none the agent could start
/// from (absent, a link, not a regular file, too large, not JSON).
fn read_plan(path: &Path) -> std::io::Result<Option<serde_json::Value>> {
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
