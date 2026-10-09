//! Signed current-intent challenge fencing, separate from disposable history.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    // Called only while holding this installation's nonblocking runtime lock.
    pub(super) fn accept_installation_intent(
        &self,
        installation_id: uuid::Uuid,
        nonce: Option<&str>,
        ordinal: Option<u64>,
        cleanup: bool,
    ) -> Result<(), OperationError> {
        let ordinal = ordinal
            .filter(|value| *value <= i64::MAX as u64)
            .ok_or(OperationError::InvalidOperation)?;
        let root = self.roots.data.join("installation-intent-fences");
        let observe = || -> Result<(), OperationError> {
            ensure_runtime_directory(&root)?;
            require_exact_directory(&root, Some(rustix::process::geteuid().as_raw()), 0o700)?;
            Ok(())
        };
        observe().map_err(|_| OperationError::InstallationReconciliationStorageUnavailable)?;
        let path = root.join(format!("{installation_id}.json"));
        let current = (|| -> Option<InstallationIntentFence> {
            let mut file = OpenOptions::new()
                .read(true)
                .custom_flags(
                    (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32,
                )
                .open(&path)
                .ok()?;
            let metadata = file.metadata().ok()?;
            if !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.uid() != rustix::process::geteuid().as_raw()
                || metadata.mode() & 0o777 != 0o600
                || metadata.len() > 1024
            {
                return None;
            }
            let mut bytes = Vec::new();
            Read::by_ref(&mut file)
                .take(1025)
                .read_to_end(&mut bytes)
                .ok()?;
            let record: InstallationIntentFence = parse_strict(&bytes).ok()?;
            (record.installation_id == installation_id
                && record.schema_version == 2
                && record.highest_ordinal <= i64::MAX as u64
                && record.nonce.len() == 64
                && record
                    .nonce
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                && canonical_json(&record).ok()? == bytes)
                .then_some(record)
        })();
        let fresh_nonce = || hex_sha256(uuid::Uuid::new_v4().as_bytes());
        let mut record = current.clone().unwrap_or_else(|| InstallationIntentFence {
            schema_version: 2,
            installation_id,
            nonce: fresh_nonce(),
            highest_ordinal: 0,
            cancelled: false,
        });
        let challenged = current.is_none() || nonce != Some(record.nonce.as_str());
        if challenged {
            // Damaged/missing storage is reconstructed only after a new nonce
            // returns through the authenticated Controller's current decision.
            // A previously signed grant cannot predict or reuse this challenge.
            record.nonce = fresh_nonce();
        } else {
            if ordinal < record.highest_ordinal
                || (!cleanup && record.cancelled && ordinal == record.highest_ordinal)
            {
                return Err(OperationError::InvalidOperation);
            }
            record.highest_ordinal = ordinal;
            record.cancelled = cleanup;
            if cleanup {
                // Invalidate every outstanding START challenge, including one
                // for a runtime which has never reached Docker or a run fence.
                record.nonce = fresh_nonce();
            }
        }
        let staging = root.join(format!(".{}.tmp", uuid::Uuid::new_v4()));
        let publish = (|| -> Result<(), OperationError> {
            let bytes = canonical_json(&record).map_err(|_| OperationError::InvalidOperation)?;
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
                .mode(0o600)
                .open(&staging)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&staging, &path)?;
            sync_directory(&root)?;
            sync_directory(&self.roots.data)
        })();
        if publish.is_err() {
            let _ = fs::remove_file(&staging);
        }
        publish.map_err(|_| OperationError::InstallationReconciliationStorageUnavailable)?;
        if challenged {
            Err(OperationError::InstallationIntentObservationRequired {
                nonce: record.nonce,
            })
        } else {
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests;
