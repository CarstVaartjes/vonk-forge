#![forbid(unsafe_code)]

//! Stdin/stdout probe for the compiled execution-plan serde boundary.

use std::io::{self, BufRead, Write};

use vonk_agent::executor::parse_compiled_execution_plan;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut output = io::BufWriter::new(stdout.lock());
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let value: serde_json::Value = serde_json::from_str(&line)?;
        let plan = parse_compiled_execution_plan(&value)
            .map_err(|error| io::Error::other(error.to_string()))?;
        serde_json::to_writer(&mut output, &plan)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}
