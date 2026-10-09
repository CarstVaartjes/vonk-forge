use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use vonk_agent_protocol::{PackageActivationReceipt, parse_strict};

use super::custody::{publish_managed, safe};
use super::recovery::retry_process_proof;
use super::{STATE, Transaction};

pub struct Store {
    pub(super) root: PathBuf,
    pub(super) owner: u32,
}
impl Store {
    pub fn system() -> Self {
        Self {
            root: PathBuf::from(STATE),
            owner: 0,
        }
    }
    pub(super) fn lock(&self) -> Result<File, String> {
        safe(
            self.root.parent().ok_or("rollback parent missing")?,
            true,
            self.owner,
            0o755,
        )?;
        match fs::symlink_metadata(&self.root) {
            Ok(metadata) if metadata.is_dir() && metadata.uid() == self.owner => {
                fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))
                    .map_err(|error| error.to_string())?;
            }
            Ok(_) => {
                let parent = self.root.parent().ok_or("rollback parent missing")?;
                fs::rename(
                    &self.root,
                    parent.join(format!(".retired-{}", uuid::Uuid::new_v4())),
                )
                .map_err(|error| error.to_string())?;
                fs::create_dir(&self.root).map_err(|error| error.to_string())?;
                fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))
                    .map_err(|error| error.to_string())?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                fs::create_dir(&self.root).map_err(|error| error.to_string())?;
                fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))
                    .map_err(|error| error.to_string())?;
            }
            Err(error) => return Err(error.to_string()),
        }
        safe(&self.root, true, self.owner, 0o700)?;
        let path = self.root.join("lock");
        if let Ok(metadata) = fs::symlink_metadata(&path) {
            if metadata.is_file() && metadata.uid() == self.owner && metadata.nlink() == 1 {
                // Preserve the inode and any active flock while fixing mode.
                fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                    .map_err(|error| error.to_string())?;
            } else {
                fs::rename(
                    &path,
                    self.root
                        .join(format!(".retired-lock-{}", uuid::Uuid::new_v4())),
                )
                .map_err(|error| error.to_string())?;
            }
        }
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32,
            )
            .open(&path)
            .map_err(|e| e.to_string())?;
        safe(&path, false, self.owner, 0o600)?;
        retry_process_proof(
            Instant::now() + Duration::from_secs(2),
            Duration::from_millis(50),
            || {
                rustix::fs::flock(&file, rustix::fs::FlockOperation::NonBlockingLockExclusive)
                    .map_err(|error| error.to_string())
            },
        )?;
        Ok(file)
    }
    pub(super) fn read(&self) -> Result<Transaction, String> {
        let path = self.root.join("transaction.json");
        if let Ok(metadata) = fs::symlink_metadata(&path)
            && metadata.is_file()
            && metadata.uid() == self.owner
            && metadata.nlink() == 1
            && metadata.mode() & 0o022 == 0
        {
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                .map_err(|error| error.to_string())?;
        }
        safe(&path, false, self.owner, 0o600)?;
        let tx: Transaction = parse_strict(&fs::read(path).map_err(|e| e.to_string())?)
            .map_err(|_| "invalid rollback transaction")?;
        if tx.schema_version != 2 || !tx.rollback.valid() {
            return Err("invalid rollback authority".into());
        }
        Ok(tx)
    }
    pub(super) fn write(&self, tx: &Transaction) -> Result<(), String> {
        let path = self.root.join(format!(".{}.json", uuid::Uuid::new_v4()));
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&path)
            .map_err(|e| e.to_string())?;
        file.write_all(&serde_json::to_vec(tx).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
        file.sync_all().map_err(|e| e.to_string())?;
        publish_managed(&path, &self.root.join("transaction.json"))?;
        File::open(&self.root)
            .and_then(|f| f.sync_all())
            .map_err(|e| e.to_string())?;
        let receipt = PackageActivationReceipt {
            schema_version: 2,
            node_id: tx.node_id.clone(),
            source_package_sha256: tx.rollback.source.package_sha256.clone(),
            source_version: tx.rollback.source.package_version.clone(),
            source_binary_sha256: tx.rollback.source.binary_sha256.clone(),
            candidate_package_sha256: tx.candidate_sha256.clone(),
            candidate_version: tx.candidate_version.clone(),
            candidate_binary_sha256: tx.candidate_binary_sha256.clone(),
            attempt_nonce: tx.rollback.attempt_nonce.clone(),
            phase: tx.phase,
            created_at: tx.created_at,
            updated_at: tx.updated_at,
            outcome: tx.outcome,
        };
        receipt.validate()?;
        let parent = self.root.parent().ok_or("receipt parent")?;
        let temporary = parent.join(format!(".activation-{}.json", uuid::Uuid::new_v4()));
        let mut output = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o644)
            .open(&temporary)
            .map_err(|e| e.to_string())?;
        output
            .write_all(&serde_json::to_vec(&receipt).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
        // Service UMask=0077 narrows create mode to 0600. This public,
        // root-owned receipt must be readable by the unprivileged agent.
        output
            .set_permissions(fs::Permissions::from_mode(0o644))
            .map_err(|e| e.to_string())?;
        output.sync_all().map_err(|e| e.to_string())?;
        publish_managed(&temporary, &parent.join("package-activation.receipt.json"))?;
        File::open(parent)
            .and_then(|f| f.sync_all())
            .map_err(|e| e.to_string())
    }
    pub(super) fn copy_custody(&self, source: &Path, name: &str, mode: u32) -> Result<(), String> {
        let temporary = self.root.join(format!(".copy-{}", uuid::Uuid::new_v4()));
        let mut input = OpenOptions::new()
            .read(true)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32,
            )
            .open(source)
            .map_err(|e| e.to_string())?;
        let mut output = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(mode)
            .open(&temporary)
            .map_err(|e| e.to_string())?;
        std::io::copy(&mut input, &mut output).map_err(|e| e.to_string())?;
        output.sync_all().map_err(|e| e.to_string())?;
        publish_managed(&temporary, &self.root.join(name))?;
        File::open(&self.root)
            .and_then(|f| f.sync_all())
            .map_err(|e| e.to_string())
    }
}
