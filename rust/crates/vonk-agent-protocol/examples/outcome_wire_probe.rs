//! Round-trip one lifecycle/outcome contract document through the generated Rust types.
//!
//! `outcome_wire_probe <type>` reads one JSON document on stdin, parses it with
//! the generated type, and prints its canonical JSON. A document the generated
//! type refuses exits nonzero, so the Controller test proves both directions:
//! every Python-produced document is accepted, and what Rust prints is what the
//! Python contract reads back.
use serde::{Serialize, de::DeserializeOwned};
use std::io::{self, Read};
use vonk_agent_protocol::generated::{
    ErrorCatalog, LifecycleVocabulary, OutcomeCatalog, OutcomeEvidence, ReasonCodeVocabulary,
};
use vonk_agent_protocol::{AgentResult, canonical_json, parse_strict};

fn roundtrip<T: DeserializeOwned + Serialize>(
    raw: &[u8],
) -> Result<(), Box<dyn std::error::Error>> {
    let value: T = parse_strict(raw)?;
    println!("{}", String::from_utf8(canonical_json(&value)?)?);
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut raw = Vec::new();
    io::stdin().read_to_end(&mut raw)?;
    match std::env::args().nth(1).as_deref() {
        Some("agent-result") => {
            let result: AgentResult = parse_strict(&raw)?;
            result.validate()?;
            println!("{}", String::from_utf8(canonical_json(&result)?)?);
            Ok(())
        }
        Some("outcome") => roundtrip::<OutcomeCatalog>(&raw),
        Some("error") => roundtrip::<ErrorCatalog>(&raw),
        Some("evidence") => roundtrip::<OutcomeEvidence>(&raw),
        Some("vocabulary") => roundtrip::<LifecycleVocabulary>(&raw),
        Some("reason-codes") => roundtrip::<ReasonCodeVocabulary>(&raw),
        _ => Err(
            "usage: outcome_wire_probe agent-result|outcome|error|evidence|vocabulary|reason-codes"
                .into(),
        ),
    }
}
