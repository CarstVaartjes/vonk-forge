use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::Path,
    time::Duration,
};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::runtime_identity::AgentRuntimeIdentity;

const MAX_RECEIPT_BYTES: u64 = 4096;

#[derive(Debug, Error)]
pub enum ReadinessError {
    #[error("controller readiness receipt is unsafe")]
    Unsafe,
    #[error("controller readiness receipt does not match the running agent")]
    Mismatch,
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReadinessReceipt {
    schema_version: u8,
    pid: u32,
    process_start_ticks: u64,
    boot_id: String,
    accepted_at: DateTime<Utc>,
    runtime_identity: AgentRuntimeIdentity,
}

impl ReadinessReceipt {
    pub fn new(
        runtime_identity: AgentRuntimeIdentity,
        pid: u32,
        process_start_ticks: u64,
        boot_id: String,
        accepted_at: DateTime<Utc>,
    ) -> Self {
        Self {
            schema_version: 1,
            pid,
            process_start_ticks,
            boot_id,
            accepted_at,
            runtime_identity,
        }
    }

    pub fn write_secure(&self, path: &Path) -> Result<(), ReadinessError> {
        let parent = path.parent().ok_or(ReadinessError::Unsafe)?;
        let temporary = parent.join(format!(".readiness.{}.new", std::process::id()));
        let raw = serde_json::to_vec(self)?;
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        let result = (|| {
            file.write_all(&raw)?;
            file.write_all(b"\n")?;
            file.sync_all()?;
            fs::rename(&temporary, path)?;
            File::open(parent)?.sync_all()?;
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }
}

#[allow(clippy::too_many_arguments)]
pub fn verify_readiness_at(
    path: &Path,
    expected_identity: &AgentRuntimeIdentity,
    expected_pid: u32,
    expected_start_ticks: u64,
    expected_boot_id: &str,
    now: DateTime<Utc>,
    max_age: Duration,
) -> Result<(), ReadinessError> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.nlink() != 1
        || metadata.len() == 0
        || metadata.len() > MAX_RECEIPT_BYTES
        || metadata.permissions().mode() & 0o177 != 0
    {
        return Err(ReadinessError::Unsafe);
    }
    let mut raw = Vec::with_capacity(metadata.len() as usize);
    File::open(path)?
        .take(MAX_RECEIPT_BYTES + 1)
        .read_to_end(&mut raw)?;
    if raw.len() as u64 != metadata.len() {
        return Err(ReadinessError::Unsafe);
    }
    let receipt: ReadinessReceipt = serde_json::from_slice(&raw)?;
    let age = now
        .signed_duration_since(receipt.accepted_at)
        .to_std()
        .map_err(|_| ReadinessError::Mismatch)?;
    if receipt.schema_version != 1
        || receipt.pid != expected_pid
        || receipt.process_start_ticks != expected_start_ticks
        || receipt.boot_id != expected_boot_id
        || receipt.runtime_identity != *expected_identity
        || !receipt.runtime_identity.self_test_passed
        || age > max_age
    {
        return Err(ReadinessError::Mismatch);
    }
    Ok(())
}

pub fn publish_current(
    path: &Path,
    runtime_identity: &AgentRuntimeIdentity,
) -> Result<(), ReadinessError> {
    let pid = std::process::id();
    ReadinessReceipt::new(
        runtime_identity.clone(),
        pid,
        process_start_ticks(pid)?,
        boot_id()?,
        Utc::now(),
    )
    .write_secure(path)
}

pub fn verify_current(
    path: &Path,
    expected_identity: &AgentRuntimeIdentity,
    expected_pid: u32,
    max_age: Duration,
) -> Result<(), ReadinessError> {
    verify_readiness_at(
        path,
        expected_identity,
        expected_pid,
        process_start_ticks(expected_pid)?,
        &boot_id()?,
        Utc::now(),
        max_age,
    )
}

fn boot_id() -> Result<String, std::io::Error> {
    Ok(fs::read_to_string("/proc/sys/kernel/random/boot_id")?
        .trim()
        .to_owned())
}

fn process_start_ticks(pid: u32) -> Result<u64, std::io::Error> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat"))?;
    let after_name = stat
        .rsplit_once(')')
        .ok_or_else(|| std::io::Error::other("process stat is invalid"))?
        .1;
    after_name
        .split_whitespace()
        .nth(19)
        .ok_or_else(|| std::io::Error::other("process start time is unavailable"))?
        .parse()
        .map_err(|_| std::io::Error::other("process start time is invalid"))
}
