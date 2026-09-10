#![forbid(unsafe_code)]
pub mod runtime_preflight;

pub mod agent_upgrade;
mod base_images;
pub mod build_source;
pub mod client;
pub mod compiled_oci;
pub mod config;
pub mod executor;
pub mod failure_evidence;
pub mod health;
pub mod host_runtime;
pub mod identity;
pub mod image_importer;
pub mod inventory;
pub mod oci;
pub mod package_activation;
pub mod pair;
pub mod process;
pub mod readiness;
pub mod recipe_builder;
pub mod rotation;
pub mod runtime_identity;
pub mod self_test;
pub mod source_policy;
pub mod state;
pub mod telemetry;
pub mod workloads;

/// Capabilities advertised by the current Rust agent to the Controller.
pub const CLAIM_CAPABILITIES: &[&str] = &[
    "runtime.preflight.v1",
    "agent.runtime.rust.v1",
    "runtime.vonk.v1",
    "agent.upgrade.v1",
    "artifact.distribution.v1",
    "recipe.build.v1",
    "recipe.image.import.v1",
    "recipe.job.run.v1",
    "recipe.install",
    "recipe.start",
    "recipe.start.two-phase.v1",
    "recipe.run.inspect.exact.v1",
    "recipe.run.inspect.receipt.v1",
    "recipe.stop",
    "recipe.uninstall",
];
