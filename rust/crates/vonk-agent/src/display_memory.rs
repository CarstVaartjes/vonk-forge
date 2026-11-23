//! Ask the existing root helper: unprivileged iomem hides physical addresses.
use std::{path::Path, time::Duration};
use vonk_agent_protocol::{
    canonical_generated_json,
    generated::{
        DisplayMemoryInspectionKind, DisplayMemoryInspectionRequest, DisplayMemoryObservation,
    },
};

pub(crate) fn observe(
    socket: &Path,
    memory_total_bytes: Option<u64>,
) -> Option<DisplayMemoryObservation> {
    let request = DisplayMemoryInspectionRequest {
        kind: DisplayMemoryInspectionKind::DisplayMemory,
    };
    let body = canonical_generated_json(&request).ok()?;
    let reply = crate::stall_window::Inactivity::new(Duration::from_secs(5));
    let mut observation: DisplayMemoryObservation =
        crate::host_runtime::inspect_helper(socket, &body, &reply).ok()?;
    observation.memory_total_bytes = memory_total_bytes;
    Some(observation)
}

#[cfg(test)]
mod tests;
