//! Local Linux acceptance driver, not installed in the agent package.
use std::path::Path;
use vonk_agent::{process::SystemProcessRunner, runtime_preflight::RuntimePreflight};
use vonk_agent_protocol::{
    generated::{RuntimePreflightRequestArchitecture, RuntimePreflightRequestFabricConnectivity},
    runtime_preflight::RuntimePreflightRequest,
};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let request = RuntimePreflightRequest {
        schema_version: 1,
        architecture: if cfg!(target_arch = "aarch64") {
            RuntimePreflightRequestArchitecture::LinuxArm64
        } else {
            RuntimePreflightRequestArchitecture::LinuxAmd64
        },
        source_build: true,
        minimum_free_bytes: 0,
        fabric_connectivity: RuntimePreflightRequestFabricConnectivity::None,
        fabric_minimum_mbps: 0,
        mandatory_capabilities: vec![],
    };
    let result = RuntimePreflight {
        runner: &SystemProcessRunner,
        data_root: Path::new("/var/lib/vonk-forge-agent"),
        runtime_root: Path::new("/run/vonk-forge-agent"),
        probe_binary: Path::new("/usr/lib/vonk-forge/vonk-runtime-probe"),
    }
    .run(&request, "a".repeat(64), None, &|| false)?;
    println!("{}", serde_json::to_string(&result)?);
    Ok(())
}
