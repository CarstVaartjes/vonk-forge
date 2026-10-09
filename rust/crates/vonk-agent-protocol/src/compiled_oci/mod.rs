//! Pure projection of a signed compiled workload into Podman launch pieces.
//!
//! This module deliberately does not execute Podman or touch the filesystem.  The
//! caller supplies the already materialized paths and may add run-specific
//! arguments (such as a container name) around the returned launch pieces.

use thiserror::Error;

use crate::compiled_execution_plan::WorkloadError;

mod accounting;
mod invocation;
mod projection;
mod validation;

pub use accounting::{ExecInvocationLimits, ExecInvocationUsage, measure_exec_invocation};
pub use invocation::{
    CompiledOciInvocation, CompiledOciPaths, OciImageReceipt, OciMount, OciNetworkMode,
    OciSecurityOptions,
};
pub use projection::{project, start_arguments_for_paths};

#[derive(Debug, Error)]
pub enum CompiledOciError {
    #[error("compiled execution plan is invalid")]
    Workload(#[from] WorkloadError),
    #[error("compiled OCI projection rejected: {0}")]
    Invalid(&'static str),
    #[error("projected OCI invocation exceeds the host argument-byte limit ({observed} > {limit})")]
    InvocationBytes { limit: u64, observed: u64 },
    #[error(
        "projected OCI invocation string exceeds the host per-string limit ({observed} > {limit})"
    )]
    InvocationStringBytes { limit: u64, observed: u64 },
}

#[cfg(test)]
mod tests;
#[cfg(test)]
mod mount_equivalence_tests;
