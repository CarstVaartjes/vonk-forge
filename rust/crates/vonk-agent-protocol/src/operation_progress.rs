//! Typed counterpart of the canonical Pydantic operation progress graph.
use crate::ProtocolError;
pub use crate::generated::{
    OperationCheckpoint, OperationMemberProgress, OperationProgress,
    OperationProgressActivity as ProgressActivity,
};
use chrono::DateTime;
use std::collections::BTreeSet;

fn bounded(value: &str, max: usize) -> bool {
    !value.is_empty() && value.chars().count() <= max
}
fn number(value: Option<f64>, max: f64) -> bool {
    value.is_none_or(|v| v.is_finite() && v >= 0.0 && v <= max)
}
fn timestamp(value: &Option<String>) -> bool {
    value
        .as_ref()
        .is_none_or(|v| v.len() <= 64 && DateTime::parse_from_rfc3339(v).is_ok())
}

impl OperationMemberProgress {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        crate::validate_generated("OperationMemberProgress", self)
            .map_err(|_| ProtocolError::Identity("operation progress"))?;
        if !bounded(&self.phase, 80)
            || self.kind.as_ref().is_some_and(|v| !bounded(v, 80))
            || self
                .object_sha256
                .as_ref()
                .is_some_and(|v| !valid_sha256(v))
            || self
                .total_bytes
                .as_ref()
                .is_some_and(|total| &self.completed_bytes > total)
            || self.total_items.as_ref().is_some_and(|total| {
                self.completed_items
                    .as_ref()
                    .is_some_and(|done| done > total)
            })
            || !number(self.bytes_per_second, 1e15)
            || !number(self.smoothed_bytes_per_second, 1e15)
            || !number(self.eta_seconds, 1e9)
            || !number(self.elapsed_seconds, f64::MAX)
            || !timestamp(&self.observed_at)
            || !timestamp(&self.last_progress_at)
            || !bounded(&self.member_id, 128)
            || !bounded(&self.state, 32)
        {
            return Err(ProtocolError::Identity("operation progress"));
        }
        Ok(())
    }
}

impl OperationProgress {
    /// Whether this snapshot moved past `previous`: bytes, items, subprocess
    /// observations or a new phase. The one agent-side counterpart of the Controller's rule;
    /// lease renewals and retries are liveness, never progress.
    pub fn advanced_from(&self, previous: Option<&Self>) -> bool {
        previous.is_none_or(|previous| {
            if let (Some(old), Some(new)) = (&previous.checkpoint, &self.checkpoint)
                && old.key != new.key
            {
                return false;
            }

            self.completed_bytes > previous.completed_bytes
                || self.completed_items.clone().unwrap_or_default()
                    > previous.completed_items.clone().unwrap_or_default()
                || self.activity_sequence.clone().unwrap_or_default()
                    > previous.activity_sequence.clone().unwrap_or_default()
                || self.phase != previous.phase
        })
    }

    pub fn validate(&self) -> Result<(), ProtocolError> {
        crate::validate_generated("OperationProgress", self)
            .map_err(|_| ProtocolError::Identity("operation progress"))?;
        if !bounded(&self.phase, 80)
            || self.kind.as_ref().is_some_and(|v| !bounded(v, 80))
            || self
                .object_sha256
                .as_ref()
                .is_some_and(|v| !valid_sha256(v))
            || self
                .total_bytes
                .as_ref()
                .is_some_and(|total| &self.completed_bytes > total)
            || self.total_items.as_ref().is_some_and(|total| {
                self.completed_items
                    .as_ref()
                    .is_some_and(|done| done > total)
            })
            || !number(self.bytes_per_second, 1e15)
            || !number(self.smoothed_bytes_per_second, 1e15)
            || !number(self.eta_seconds, 1e9)
            || !number(self.elapsed_seconds, f64::MAX)
            || !timestamp(&self.observed_at)
            || !timestamp(&self.last_progress_at)
            || self.total_bytes_known != self.total_bytes.is_some()
            || self.members.len() > 1024
        {
            return Err(ProtocolError::Identity("operation progress"));
        }
        if let Some(checkpoint) = &self.checkpoint
            && (!bounded(&checkpoint.key, 128)
                || checkpoint
                    .cursor
                    .as_ref()
                    .is_some_and(|v| v.chars().count() > 512)
                || checkpoint.digest.as_ref().is_some_and(|v| !valid_sha256(v)))
        {
            return Err(ProtocolError::Identity("operation checkpoint"));
        }
        let mut ids = BTreeSet::new();
        for member in &self.members {
            member.validate()?;
            if !ids.insert(&member.member_id) {
                return Err(ProtocolError::Identity("operation member"));
            }
        }
        Ok(())
    }
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
