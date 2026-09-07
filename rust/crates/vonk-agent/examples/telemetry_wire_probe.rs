//! Emit one representative agent telemetry report for the Python/Controller
//! wire bridge. The sample is parsed and serialized by the same Rust serde
//! types used by the HTTP client; the bridge then validates the resulting
//! JSON through the shared Pydantic graph.

use std::io::{self, Read};

use serde::Serialize;
use vonk_agent::telemetry::TelemetrySample;

#[derive(Serialize)]
struct TelemetryReport<'a> {
    schema_version: u8,
    samples: [&'a TelemetrySample; 1],
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let sample: TelemetrySample = serde_json::from_str(&input)?;
    let report = TelemetryReport {
        schema_version: 1,
        samples: [&sample],
    };
    println!("{}", serde_json::to_string(&report)?);
    Ok(())
}
