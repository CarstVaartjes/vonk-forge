//! Measured artifact transfer progress and retained heartbeat counters.

use super::*;

#[derive(Debug, Clone)]
pub struct DistributionDownloadEvidence {
    pub model_digests: Vec<String>,
    pub model_paths: Vec<std::path::PathBuf>,
    pub oci_image_digest: String,
    pub oci_image_config_digest: String,
    pub downloaded_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DistributionProgress {
    pub phase: ProgressPhase,
    pub completed_items: u64,
    pub total_items: u64,
    pub object_sha256: String,
    pub kind: String,
    pub bytes: u64,
    pub total_bytes: Option<u64>,
}

pub(super) struct DistributionProgressTracker<F> {
    pub(super) object_bytes: Vec<u64>,
    pub(super) bytes: u64,
    pub(super) completed_items: u64,
    pub(super) total_bytes: u64,
    pub(super) callback: F,
}

impl<F: FnMut(DistributionProgress)> DistributionProgressTracker<F> {
    /// Report one object's byte count. The operation-wide phase is always
    /// "copying": objects finish in any order while others are still moving,
    /// so a per-object step must not become the phase the Controller shows for
    /// the whole transfer. Nothing here re-reads or re-hashes content.
    pub(super) fn report(
        &mut self,
        index: usize,
        object: &vonk_agent_protocol::DistributionObject,
        bytes: u64,
        completed: bool,
    ) {
        // Retried ranges and verification can report the same offset again.
        // Count each object's bytes once, regardless of completion order.
        self.bytes += bytes.saturating_sub(self.object_bytes[index]);
        self.object_bytes[index] = self.object_bytes[index].max(bytes);
        self.completed_items += u64::from(completed);
        (self.callback)(DistributionProgress {
            phase: ProgressPhase::Copying,
            completed_items: self.completed_items,
            total_items: self.object_bytes.len() as u64,
            object_sha256: object.sha256.clone(),
            kind: object.kind.to_string(),
            bytes: self.bytes,
            total_bytes: Some(self.total_bytes),
        });
    }
}

pub(super) struct ProgressSnapshot {
    pub(super) fence: uuid::Uuid,
    pub(super) phase: ProgressPhase,
    pub(super) counters: Option<(u64, u64)>,
}
