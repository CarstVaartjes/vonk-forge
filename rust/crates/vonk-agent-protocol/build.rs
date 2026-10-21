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
    let policy = &graph["$defs"]["GpuClockPolicy"]["properties"];
    let mut fields = Vec::new();
    for name in ["default_min_mhz", "default_max_mhz", "hard_max_mhz"] {
        let value = policy[name]["const"]
            .as_u64()
            .ok_or("GPU clock policy needs typed integer constants")?;
        fields.push(format!("{name}: {value}"));
    }
    fs::write(
        output.join("gpu_clock_policy.rs"),
        format!(
            "pub const GPU_CLOCK_POLICY: crate::generated::GpuClockPolicy = crate::generated::GpuClockPolicy {{ {} }};",
            fields.join(",")
        ),
    )?;
    fs::write(
        output.join("generated.rs"),
        vonk_wire_codegen::render(schema.to_str().ok_or("non-UTF8 schema path")?)?,
    )?;
    Ok(())
}
