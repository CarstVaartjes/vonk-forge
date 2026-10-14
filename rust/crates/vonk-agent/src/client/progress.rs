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
    /// Bytes this order completed in its earlier phases, so the bytes it
    /// reports never move backwards when a later phase starts at zero.
    pub(super) settled_bytes: u64,
    /// This phase's bytes and, when known, its total.
    pub(super) counters: Option<(u64, Option<u64>)>,
    /// Subprocess output and growth observed for this order. It is local
    /// evidence that a silent step still advances, not a transfer counter.
    pub(super) activity: u64,
}

/// One order's progress as the heartbeat and its stall window read it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ProgressCounters {
    pub(crate) phase: ProgressPhase,
    /// Bytes across all phases of the order; never moves backwards.
    pub(crate) bytes: u64,
    pub(crate) total_bytes: Option<u64>,
    /// Whether any phase measured bytes.
    pub(crate) measured: bool,
    pub(crate) activity: u64,
}

impl AgentHttpClient {
    fn with_snapshot(&self, fence: uuid::Uuid, update: impl FnOnce(&mut ProgressSnapshot)) {
        if let Some(snapshot) = self
            .progress_phase
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_mut()
            && snapshot.fence == fence
        {
            update(snapshot);
        }
    }

    /// Enter `phase`. Bytes and activity only grow within one order, so the
    /// progress it reports never reads as going backwards.
    pub(crate) fn set_progress_phase(&self, fence: uuid::Uuid, phase: ProgressPhase) {
        let mut snapshot = self
            .progress_phase
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        match snapshot.as_mut().filter(|current| current.fence == fence) {
            Some(current) if current.phase == phase => {}
            Some(current) => {
                current.settled_bytes = current
                    .settled_bytes
                    .saturating_add(current.counters.map_or(0, |(bytes, _)| bytes));
                current.counters = None;
                current.phase = phase;
            }
            None => {
                *snapshot = Some(ProgressSnapshot {
                    fence,
                    phase,
                    settled_bytes: 0,
                    counters: None,
                    activity: 0,
                });
            }
        }
    }

    /// Record subprocess output or growth as progress of this order.
    pub(crate) fn note_process_activity(&self, fence: uuid::Uuid, amount: u64) {
        self.with_snapshot(fence, |snapshot| {
            snapshot.activity = snapshot.activity.saturating_add(amount);
        });
    }

    /// This phase reached `bytes` of a known `total`.
    pub(crate) fn set_progress_bytes(&self, fence: uuid::Uuid, bytes: u64, total: u64) {
        self.with_snapshot(fence, |snapshot| {
            let high_water = snapshot
                .counters
                .map_or(0, |(current, _)| current)
                .max(bytes);
            snapshot.counters = Some((high_water, Some(total.max(high_water))));
        });
    }

    /// This phase moved `amount` more bytes of a total it cannot know.
    pub(crate) fn add_progress_bytes(&self, fence: uuid::Uuid, amount: u64) {
        self.with_snapshot(fence, |snapshot| {
            let (bytes, total) = snapshot.counters.unwrap_or((0, None));
            let bytes = bytes.saturating_add(amount);
            snapshot.counters = Some((bytes, total.map(|total| total.max(bytes))));
        });
    }

    /// The order's local progress: its phase, transferred bytes and observed
    /// subprocess activity.
    pub(crate) fn progress_snapshot(&self, fence: uuid::Uuid) -> Option<ProgressCounters> {
        self.progress_phase
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .filter(|snapshot| snapshot.fence == fence)
            .map(|snapshot| {
                let (bytes, total) = snapshot.counters.unwrap_or((0, None));
                ProgressCounters {
                    phase: snapshot.phase,
                    bytes: snapshot.settled_bytes.saturating_add(bytes),
                    total_bytes: total.map(|total| snapshot.settled_bytes.saturating_add(total)),
                    measured: snapshot.settled_bytes > 0 || snapshot.counters.is_some(),
                    activity: snapshot.activity,
                }
            })
    }
}
