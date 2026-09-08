//! Emit one representative agent telemetry report for the Python/Controller
//! wire bridge. The sample is parsed and serialized by the same Rust serde
//! types used by the HTTP client; the bridge then validates the resulting
//! JSON through the shared Pydantic graph.

use std::io::{self, Read};

use vonk_agent::telemetry::{TelemetryRequest, TelemetrySample, valid_report_batch};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let sample: TelemetrySample = serde_json::from_str(&input)?;
    let samples = std::slice::from_ref(&sample);
    if !valid_report_batch(samples) {
        return Err("telemetry sample violates the agent client contract".into());
    }
    let report = TelemetryRequest {
        schema_version: 1,
        samples: samples.iter().map(|sample| sample.wire().clone()).collect(),
    };
    println!("{}", serde_json::to_string(&report)?);
    Ok(())
}
