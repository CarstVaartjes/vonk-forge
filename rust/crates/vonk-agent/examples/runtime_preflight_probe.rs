//! Wire-runner driver: runs the agent's real runtime preflight for the request
//! on stdin against the data root in argv[1] and prints the canonical result.
use std::io::{self, Read};
use std::path::PathBuf;
use vonk_agent::{process::SystemProcessRunner, runtime_preflight::RuntimePreflight};
use vonk_agent_protocol::{
    canonical_json, parse_strict, runtime_preflight::RuntimePreflightRequest,
};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let data_root = PathBuf::from(
        std::env::args()
            .nth(1)
            .ok_or("usage: runtime_preflight_probe <data-root>")?,
    );
    let mut raw = Vec::new();
    io::stdin().read_to_end(&mut raw)?;
    let request: RuntimePreflightRequest = parse_strict(&raw)?;
    let result = RuntimePreflight {
        runner: &SystemProcessRunner,
        data_root: &data_root,
        runtime_root: &data_root.join("run"),
        probe_binary: &data_root.join("vonk-runtime-probe"),
    }
    .run(&request, "a".repeat(64), None, &|| false)?;
    println!("{}", String::from_utf8(canonical_json(&result)?)?);
    Ok(())
}
