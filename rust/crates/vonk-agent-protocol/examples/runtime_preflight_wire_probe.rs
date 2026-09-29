use std::io::{self, Read};
use vonk_agent_protocol::runtime_preflight::{RuntimePreflightRequest, RuntimePreflightResult};
use vonk_agent_protocol::{canonical_json, parse_strict};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut raw = Vec::new();
    io::stdin().read_to_end(&mut raw)?;
    let _request: RuntimePreflightRequest = parse_strict(&raw)?;
    let result = RuntimePreflightResult {
        fingerprint: "a".repeat(64),
        observed_at: 100,
        findings: vec![],
    };
    result.validate()?;
    println!("{}", String::from_utf8(canonical_json(&result)?)?);
    Ok(())
}
