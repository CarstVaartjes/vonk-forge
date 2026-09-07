#![forbid(unsafe_code)]

//! Exercise the same bootstrap parser used by Spark setup discovery.

use std::io::{self, BufRead, Write};

use vonk_spark_setup::parse_enrollment_bootstrap;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut output = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let document = parse_enrollment_bootstrap(line?.as_bytes())?;
        serde_json::to_writer(&mut output, &document)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}
