//! Images.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    /// Pull a pinned runtime image from the Controller's layered image store.
    ///
    /// The agent serves the store on a loopback port for the duration of the
    /// pull (Docker treats loopback registries as plain HTTP), so Docker skips
    /// every layer it already holds and fetches only the new ones. The
    /// Controller binds the manifest digest and its config digest in the
    /// signed request; Docker verifies each blob against the manifest while
    /// pulling, and the helper requires the pulled image to be exactly that
    /// config with the platform runtime identity before tagging it locally.
    pub(super) fn runtime_image_pull(&self, arguments: &[String]) -> Result<(), OperationError> {
        let [registry, manifest_digest, config_digest, image_reference] = arguments else {
            return Err(OperationError::InvalidOperation);
        };
        let (local_image, embedded_digest) = parse_local_image_reference(image_reference)?;
        if !valid_loopback_registry(registry)
            || !valid_oci_digest(manifest_digest)
            || !valid_oci_digest(config_digest)
            || embedded_digest != *manifest_digest
            || local_image
                != format!(
                    "localhost/vonk/compiled-runtime-{}",
                    &manifest_digest["sha256:".len()..]
                )
        {
            return Err(OperationError::InvalidOperation);
        }
        // Docker's classic image store names an image by its config digest;
        // the containerd image store names it by its manifest digest.
        let runtime_identity_valid = |inspected: &RuntimeImageInspection| {
            (inspected.0 == *config_digest || inspected.0 == *manifest_digest)
                && inspected.1 == "linux"
                && inspected.2 == "arm64"
                && inspected.3 == "v1"
                && numeric_non_root_user(&inspected.4)
        };
        // An earlier pull of the same pinned image is reused as is.
        if let Some(inspected) = self.inspect_runtime_image_if_present(&local_image)?
            && runtime_identity_valid(&inspected)
        {
            return self
                .write_image_receipt(RuntimeImageReceipt {
                    schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
                    platform_manifest_digest: manifest_digest.to_owned(),
                    image_config_id: inspected.0,
                    local_image_reference: image_reference.to_owned(),
                })
                .map_err(|_| OperationError::RuntimeImageReceiptFailed);
        }
        let remote = format!("{registry}/vonk/runtime@{manifest_digest}");
        let pulled = self
            .run_docker_with_timeout(
                &[
                    "pull".to_owned(),
                    "--quiet".to_owned(),
                    "--platform".to_owned(),
                    "linux/arm64".to_owned(),
                    remote.clone(),
                ],
                RUNTIME_IMAGE_PULL_TIMEOUT,
            )
            .map_err(|error| match error {
                OperationError::CommandFailed => OperationError::RuntimeImageLoadFailed,
                other => other,
            })?;
        if !pulled.success {
            return Err(OperationError::RuntimeImageLoadFailed);
        }
        let inspected = self
            .inspect_runtime_image(&remote)
            .map_err(|error| match error {
                OperationError::CommandFailed | OperationError::InvalidArtifact => {
                    OperationError::RuntimeImageInspectFailed
                }
                other => other,
            })?;
        if !runtime_identity_valid(&inspected) {
            return Err(OperationError::RuntimeImageIdentityInvalid);
        }
        let tagged = self.run_docker(&["tag".to_owned(), remote.clone(), local_image.clone()])?;
        if !tagged.success {
            return Err(OperationError::RuntimeImageInspectFailed);
        }
        // The loopback reference names an ephemeral port; drop it. The image
        // stays under its local tag, and Docker's layer metadata keeps later
        // pulls incremental.
        let _ = self.run_docker(&["image".to_owned(), "rm".to_owned(), remote]);
        let Some(inspected) = self.inspect_runtime_image_if_present(&local_image)? else {
            return Err(OperationError::RuntimeImageIdentityInvalid);
        };
        if !runtime_identity_valid(&inspected) {
            return Err(OperationError::RuntimeImageIdentityInvalid);
        }
        self.write_image_receipt(RuntimeImageReceipt {
            schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
            platform_manifest_digest: manifest_digest.to_owned(),
            image_config_id: inspected.0,
            local_image_reference: image_reference.to_owned(),
        })
        .map_err(|error| match error {
            OperationError::Io(_) | OperationError::InvalidArtifact => {
                OperationError::RuntimeImageReceiptFailed
            }
            other => other,
        })
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn repair_signed_image_receipt(
        &self,
        compiled: &CompiledExecutionPlan,
    ) -> Result<(), OperationError> {
        let image = &compiled.runtime_image;
        let local = format!(
            "localhost/vonk/compiled-runtime-{}@{}",
            image.oci_layout_sha256, image.image_digest
        );
        for attempt in 0..3 {
            let inspected = self.inspect_runtime_image_for_reference(&local);
            if let Ok((inspected, _)) = inspected {
                // Both identities come from the current grant-bound compiled plan,
                // which authorize_runtime_effect verified before this observation.
                if inspected.0 != image.local_image_config_id && inspected.0 != image.image_digest {
                    return Err(OperationError::RuntimeImageIdentityInvalid);
                }
                // Stored metadata never supplies authority, but untrusted custody
                // must not be overwritten as though it were a damaged receipt.
                match self.read_image_receipt(&image.oci_layout_sha256) {
                    Ok(_) | Err(OperationError::Io(_)) => {}
                    Err(error) => return Err(error),
                }
                if self
                    .write_image_receipt_at(
                        &image.oci_layout_sha256,
                        RuntimeImageReceipt {
                            schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
                            platform_manifest_digest: image.image_digest.clone(),
                            image_config_id: inspected.0.clone(),
                            local_image_reference: local.clone(),
                        },
                    )
                    .is_ok()
                {
                    return Ok(());
                }
            }
            if attempt < 2 {
                std::thread::sleep(Duration::from_millis(10 * (attempt + 1)));
            }
        }
        Err(OperationError::RuntimeImageReceiptFailed)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_image_inspect(&self, arguments: &[String]) -> Result<(), OperationError> {
        let [
            archive_sha256,
            registry_index_digest,
            platform_manifest_digest,
            image_reference,
            user,
        ] = arguments
        else {
            return Err(OperationError::InvalidOperation);
        };
        let (_image, embedded_digest) = parse_local_image_reference(image_reference)?;
        if &embedded_digest != platform_manifest_digest
            || !lower_hex(archive_sha256, 64)
            || !valid_oci_digest(registry_index_digest)
            || !valid_oci_digest(platform_manifest_digest)
            || !numeric_non_root_user(user)
        {
            return Err(OperationError::InvalidOperation);
        }
        let (inspected, _) = self.inspect_runtime_image_for_reference(image_reference)?;
        if inspected.1 != "linux"
            || inspected.2 != "arm64"
            || inspected.3 != "v1"
            || inspected.4 != *user
        {
            return Err(OperationError::InvalidArtifact);
        }
        self.require_image_receipt(
            archive_sha256,
            registry_index_digest,
            platform_manifest_digest,
            image_reference,
            &inspected.0,
        )
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn inspect_runtime_image(
        &self,
        image: &str,
    ) -> Result<RuntimeImageInspection, OperationError> {
        self.inspect_runtime_image_if_present(image)?
            .ok_or(OperationError::InvalidArtifact)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn inspect_runtime_image_if_present(
        &self,
        image: &str,
    ) -> Result<Option<RuntimeImageInspection>, OperationError> {
        let output = self.run_docker(&[
            "image".to_owned(),
            "inspect".to_owned(),
            "--format".to_owned(),
            "{{.Id}}\t{{.Os}}\t{{.Architecture}}\t{{index .Config.Labels \"ai.vonkforge.runtime-interface\"}}\t{{.Config.User}}".to_owned(),
            image.to_owned(),
        ])?;
        if !output.success {
            // Docker writes a newline to stdout for some missing image
            // references. A miss remains provisional here: receipt reuse
            // additionally requires a successful empty image listing.
            return if output.exit_code == Some(1)
                && output.stdout.iter().all(u8::is_ascii_whitespace)
            {
                Ok(None)
            } else {
                Err(OperationError::RuntimeImageInspectFailed)
            };
        }
        let fields = std::str::from_utf8(&output.stdout)
            .ok()
            .map(str::trim)
            .map(|value| value.split('\t').map(str::to_owned).collect::<Vec<_>>())
            .unwrap_or_default();
        if output.exit_code != Some(0) || fields.len() != 5 || !valid_oci_digest(&fields[0]) {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(Some((
            fields[0].clone(),
            fields[1].clone(),
            fields[2].clone(),
            fields[3].clone(),
            fields[4].clone(),
        )))
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn inspect_runtime_image_for_reference(
        &self,
        image_reference: &str,
    ) -> Result<(RuntimeImageInspection, String), OperationError> {
        self.inspect_runtime_image_for_reference_if_present(image_reference)?
            .ok_or(OperationError::InvalidArtifact)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn inspect_runtime_image_for_reference_if_present(
        &self,
        image_reference: &str,
    ) -> Result<Option<(RuntimeImageInspection, String)>, OperationError> {
        let (local_image, _) = parse_local_image_reference(image_reference)?;
        match self.inspect_runtime_image_if_present(image_reference)? {
            Some(inspected) => Ok(Some((inspected, image_reference.to_owned()))),
            None => {
                // Classic Docker may discard RepoDigests while loading an OCI
                // archive. The signed logical reference remains receipt-bound;
                // use the verified local config ID as the daemon reference so
                // launch stays pinned to the inspected image object.
                let Some(inspected) = self.inspect_runtime_image_if_present(&local_image)? else {
                    return Ok(None);
                };
                let operational_image = inspected.0.clone();
                Ok(Some((inspected, operational_image)))
            }
        }
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn write_image_receipt(
        &self,
        receipt: RuntimeImageReceipt,
    ) -> Result<(), OperationError> {
        let address = receipt
            .platform_manifest_digest
            .strip_prefix("sha256:")
            .unwrap_or_default()
            .to_owned();
        self.write_image_receipt_at(&address, receipt)
    }

    fn write_image_receipt_at(
        &self,
        address: &str,
        receipt: RuntimeImageReceipt,
    ) -> Result<(), OperationError> {
        if !lower_hex(address, 64)
            || receipt.schema_version != RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION
            || !valid_oci_digest(&receipt.platform_manifest_digest)
            || !valid_local_image_reference(&receipt.local_image_reference)
            || !valid_oci_digest(&receipt.image_config_id)
        {
            return Err(OperationError::InvalidArtifact);
        }
        fs::create_dir_all(&self.roots.runtime_image_receipts)?;
        fs::set_permissions(
            &self.roots.runtime_image_receipts,
            fs::Permissions::from_mode(0o700),
        )?;
        let path = self.roots.runtime_image_receipts.join(&address);
        let mut body = canonical_json(&receipt).map_err(|_| OperationError::InvalidArtifact)?;
        body.push(b'\n');
        // A re-pull of the same image may report a new daemon object (after a
        // prune, or on another image store), so the newest pull's receipt wins.
        let staged = path.with_extension("tmp");
        {
            let mut file = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(&staged)?;
            file.write_all(&body)?;
            file.sync_all()?;
        }
        fs::rename(&staged, &path)?;
        sync_directory(&self.roots.runtime_image_receipts)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn require_image_receipt(
        &self,
        archive_sha256: &str,
        registry_index_digest: &str,
        platform_manifest_digest: &str,
        local_image_reference: &str,
        image_config_id: &str,
    ) -> Result<(), OperationError> {
        if !lower_hex(archive_sha256, 64)
            || !valid_oci_digest(registry_index_digest)
            || !valid_oci_digest(platform_manifest_digest)
            || !valid_local_image_reference(local_image_reference)
            || !valid_oci_digest(image_config_id)
        {
            return Err(OperationError::InvalidOperation);
        }
        for attempt in 0..3 {
            let stored = match self.read_image_receipt(archive_sha256) {
                Err(OperationError::Io(_)) => None,
                observed => observed?,
            };
            let matches = match stored {
                Some(StoredImageReceipt::Current(receipt)) => {
                    receipt.schema_version == RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION
                        && registry_index_digest == platform_manifest_digest
                        && receipt.platform_manifest_digest == platform_manifest_digest
                        && receipt.local_image_reference == local_image_reference
                        && receipt.image_config_id == image_config_id
                }
                // An image the previous agent loaded from an archive: its receipt
                // is keyed by the archive digest and names both registry digests.
                // The installation that names it keeps starting until it is
                // replaced; the image itself is still compared live.
                Some(StoredImageReceipt::Legacy(receipt)) => {
                    receipt.archive_sha256 == archive_sha256
                        && receipt.registry_index_digest == registry_index_digest
                        && receipt.platform_manifest_digest == platform_manifest_digest
                        && receipt.local_image_reference == local_image_reference
                        && receipt.image_config_id == image_config_id
                }
                None => false,
            };
            if matches {
                return Ok(());
            }
            // A config ID alone cannot prove a missing manifest. Ask the daemon
            // for its verified manifest association under the signed exact digest.
            // Docker acquired these RepoDigests at its content-verifying ingress.
            let observed = self.run_docker_with_timeout(
                &[
                    "image".to_owned(),
                    "inspect".to_owned(),
                    "--format".to_owned(),
                    "{{json .RepoDigests}}".to_owned(),
                    image_config_id.to_owned(),
                ],
                Duration::from_secs(15),
            );
            if let Ok(output) = observed
                && output.success
                && output.exit_code == Some(0)
                && serde_json::from_slice::<Vec<String>>(&output.stdout).is_ok_and(|digests| {
                    digests.iter().any(|digest| {
                        digest
                            .rsplit_once('@')
                            .is_some_and(|(_, value)| value == platform_manifest_digest)
                    })
                })
                && platform_manifest_digest.strip_prefix("sha256:") == Some(archive_sha256)
                && registry_index_digest == platform_manifest_digest
            {
                return self
                    .write_image_receipt(RuntimeImageReceipt {
                        schema_version: RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION,
                        platform_manifest_digest: platform_manifest_digest.to_owned(),
                        image_config_id: image_config_id.to_owned(),
                        local_image_reference: local_image_reference.to_owned(),
                    })
                    .map_err(|_| OperationError::RuntimeImageReceiptFailed);
            }
            if attempt < 2 {
                std::thread::sleep(Duration::from_millis(10 * (attempt + 1)));
            }
        }
        Err(OperationError::RuntimeImageReceiptFailed)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn read_image_receipt(
        &self,
        archive_sha256: &str,
    ) -> Result<Option<StoredImageReceipt>, OperationError> {
        let path = self.roots.runtime_image_receipts.join(archive_sha256);
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
            || metadata.nlink() != 1
            || metadata.mode() & 0o022 != 0
        {
            return Err(OperationError::InvalidArtifact);
        }
        if metadata.len() > 2048 {
            return Ok(None);
        }
        let body = fs::read(path)?;
        if let Ok(receipt) = serde_json::from_slice::<RuntimeImageReceipt>(&body) {
            return Ok(Some(StoredImageReceipt::Current(receipt)));
        }
        let Ok(legacy) = serde_json::from_slice::<LegacyRuntimeImageReceipt>(&body) else {
            return Ok(None);
        };
        if legacy.schema_version != LEGACY_RUNTIME_IMAGE_RECEIPT_SCHEMA_VERSION {
            return Ok(None);
        }
        Ok(Some(StoredImageReceipt::Legacy(legacy)))
    }
}

#[cfg(test)]
mod tests;
