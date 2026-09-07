use std::io::{self, Read};
use vonk_agent_protocol::runtime_preflight::{RuntimePreflightRequest, RuntimePreflightResult};
use vonk_agent_protocol::{canonical_json, parse_strict};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut raw = Vec::new();
    io::stdin().read_to_end(&mut raw)?;
    let request: RuntimePreflightRequest = parse_strict(&raw)?;
    request.validate()?;
    let result = RuntimePreflightResult {
        schema_version: 1,
        fingerprint: "a".repeat(64),
        request_sha256: request.digest()?,
        observed_at: 100,
        duration_ms: 0,
        cached: false,
        findings: vec![],
    };
    result.validate()?;
    println!("{}", String::from_utf8(canonical_json(&result)?)?);
    Ok(())
}
