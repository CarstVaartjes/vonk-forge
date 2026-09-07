#![forbid(unsafe_code)]

//! Exercise the actual enrollment request and certificate response parsers.

use std::io::{self, BufRead, Write};

use vonk_agent::pair::validate_enrollment_response;
use vonk_agent_protocol::{EnrollmentRequest, canonical_json, parse_strict};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let stdin = io::stdin();
    let mut output = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let line = line?;
        let bytes = match arguments.as_slice() {
            [mode] if mode == "--request" => {
                let request: EnrollmentRequest = parse_strict(line.as_bytes())?;
                request.evidence.validate()?;
                canonical_json(&request)?
            }
            [mode, node_id] if mode == "--issued" => {
                let issued = validate_enrollment_response(200, line.as_bytes(), node_id)?;
                canonical_json(&issued)?
            }
            _ => return Err("expected --request or --issued NODE_ID".into()),
        };
        output.write_all(&bytes)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}
