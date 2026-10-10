use std::{env, fs, path::PathBuf, process::Command};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    for input in [
        "agent_protocol/src",
        "control/src",
        "scripts/export-agent-wire-schema",
        "scripts/check_environment.py",
        "control/uv.lock",
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
    fs::write(
        output.join("generated.rs"),
        vonk_wire_codegen::render(schema.to_str().ok_or("non-UTF8 schema path")?)?,
    )?;
    Ok(())
}
