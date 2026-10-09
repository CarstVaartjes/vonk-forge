//! Root-custody activation transaction, independent of the replaced helper cgroup.
//!
//! Source bytes enter here only after the signed artifact verifier has copied
//! them to root custody. A rollback never chooses a version, path or command
//! from a request: it restores precisely that captured source package.
use std::time::Duration;

const STATE: &str = "/var/lib/vonk-forge/package-rollback";
const AGENT: &str = "/usr/lib/vonk-forge/vonk-agent";
const HELPER: &str = "/usr/lib/vonk-forge/vonk-agent-helper";
const PATH: &str = "/usr/sbin:/usr/bin:/sbin:/bin";
const PROCESS_PROOF_TIMEOUT: Duration = Duration::from_secs(15);
const PROCESS_PROOF_INTERVAL: Duration = Duration::from_millis(100);
const ROLLBACK_RETRY_TIMEOUT: Duration = Duration::from_secs(600);

pub use vonk_agent_protocol::generated::PackageRollbackTransaction as Transaction;

mod activation;
mod commands;
mod custody;
mod recovery;
mod store;

pub use commands::prerequisites;
pub use store::Store;

#[cfg(test)]
mod tests;
