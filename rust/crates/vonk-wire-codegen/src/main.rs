//! Pydantic owns structure; typify owns Rust declarations. The adapter only
//! chooses scalar representations and installs exact schema validation.
use std::{env, fs};

use vonk_wire_codegen::render;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args().skip(1);
    let schema_path = args.next().ok_or("schema path is required")?;
    let output_path = args.next().ok_or("output path is required")?;
    fs::write(output_path, render(&schema_path)?)?;
    Ok(())
}
