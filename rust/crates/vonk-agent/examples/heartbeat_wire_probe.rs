#![forbid(unsafe_code)]

//! Parse heartbeat responses with the same strict Rust type used by the agent.
//!
//! The connected Controller bridge feeds this probe the response body emitted
//! by ``POST /agent/heartbeat``.  Keeping the probe small makes the wire
//! boundary explicit while leaving mTLS and request transport in the client.

use std::io::{self, BufRead, Write};

use vonk_agent_protocol::{AgentDirective, canonical_json, parse_strict};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut output = io::BufWriter::new(stdout.lock());

    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let directive: AgentDirective = parse_strict(line.as_bytes())?;
        directive.validate()?;
        output.write_all(&canonical_json(&directive)?)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }

    Ok(())
}
