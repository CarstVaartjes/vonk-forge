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
        if !bounded(&self.phase, 80)
            || self.kind.as_ref().is_some_and(|v| !bounded(v, 80))
            || self
                .object_sha256
                .as_ref()
                .is_some_and(|v| !valid_sha256(v))
            || self
                .total_bytes
                .is_some_and(|total| self.completed_bytes > total)
            || self
                .total_items
                .is_some_and(|total| self.completed_items.unwrap_or(0) > total)
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
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if !bounded(&self.phase, 80)
            || self.kind.as_ref().is_some_and(|v| !bounded(v, 80))
            || self
                .object_sha256
                .as_ref()
                .is_some_and(|v| !valid_sha256(v))
            || self
                .total_bytes
                .is_some_and(|total| self.completed_bytes > total)
            || self
                .total_items
                .is_some_and(|total| self.completed_items.unwrap_or(0) > total)
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
