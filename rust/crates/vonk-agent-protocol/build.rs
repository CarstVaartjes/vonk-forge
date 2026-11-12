use std::{env, fs, path::PathBuf, process::Command};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    for input in [
        "agent_protocol/src",
        "control/src",
        "scripts/export-agent-wire-schema",
        "scripts/check_environment.py",
        "control/uv.lock",
        "src/cluster_profiles/__init__.py",
        "src/cluster_profiles/control_limits.py",
        "src/cluster_profiles/runtime_identity.py",
    ] {
        println!("cargo:rerun-if-changed={}", root.join(input).display());
    }
    println!("cargo:rerun-if-env-changed=UV_PROJECT_ENVIRONMENT");
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let schema = output.join("wire.json");
    let status = Command::new("python3")
        .arg(root.join("scripts/export-agent-wire-schema"))
        .arg("--output")
        .arg(&schema)
        .current_dir(&root)
        .status()?;
    if !status.success() {
        return Err(std::io::Error::other("Pydantic wire export failed").into());
    }
    let graph: serde_json::Value = serde_json::from_slice(&fs::read(&schema)?)?;
    for (model, constant, file, names) in [
        (
            "GpuClockPolicy",
            "GPU_CLOCK_POLICY",
            "gpu_clock_policy.rs",
            &["default_min_mhz", "default_max_mhz", "hard_max_mhz"][..],
        ),
        (
            "SwapPolicy",
            "SWAP_POLICY",
            "swap_policy.rs",
            &[
                "minimum_gib",
                "default_gib",
                "maximum_gib",
                "tolerance_mib",
                "release_headroom_gib",
                "exhausted_free_floor_gib",
                "exhausted_free_percent",
                "filling_used_percent",
            ][..],
        ),
        (
            "StoragePolicy",
            "STORAGE_POLICY",
            "storage_policy.rs",
            &["low_free_percent"][..],
        ),
    ] {
        let policy = &graph["$defs"][model]["properties"];
        let mut fields = Vec::new();
        for name in names {
            let value = policy[name]["const"]
                .as_u64()
                .ok_or("policy needs typed integer constants")?;
            fields.push(format!("{name}: {value}"));
        }
        fs::write(
            output.join(file),
            format!(
                "pub const {constant}: crate::generated::{model} = crate::generated::{model} {{ {} }};",
                fields.join(",")
            ),
        )?;
    }
    // One capacity bound for every telemetry byte field: the contract's own
    // `maximum`, never a second hand-written number.
    let sample = &graph["$defs"]["TelemetrySample"]["properties"];
    let mut bound = None;
    for name in [
        "memory_total_bytes",
        "memory_available_bytes",
        "disk_total_bytes",
        "disk_free_bytes",
        "gpu_memory_total_bytes",
        "gpu_memory_free_bytes",
    ] {
        let maximum = sample[name]["anyOf"][0]["maximum"]
            .as_u64()
            .ok_or("telemetry capacity needs a typed integer maximum")?;
        if *bound.get_or_insert(maximum) != maximum {
            return Err("telemetry capacity fields disagree on their maximum".into());
        }
    }
    fs::write(
        output.join("telemetry_capacity.rs"),
        format!(
            "pub const MAX_TELEMETRY_CAPACITY_BYTES: u64 = {};",
            bound.ok_or("telemetry capacity fields are missing")?
        ),
    )?;
    fs::write(
        output.join("generated.rs"),
        vonk_wire_codegen::render(schema.to_str().ok_or("non-UTF8 schema path")?)?,
    )?;
    Ok(())
}
