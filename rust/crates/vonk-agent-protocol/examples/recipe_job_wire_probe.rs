//! Small NDJSON probe used by the Controller-to-agent recipe-job wire test.
//!
//! Each input line contains a Controller claim and the result body emitted by
//! the agent executor. The probe parses both through the production Rust
//! protocol types and returns the typed inner payloads for Python to consume.

use std::io::{self, BufRead, Write};

use serde::Deserialize;
use serde_json::{Value, json};
use vonk_agent_protocol::{AgentClaim, AgentResult, RecipeOperationRequest};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BridgeInput {
    claim: AgentClaim,
    result: AgentResult,
}

fn parse_line(line: &str) -> Result<Value, Box<dyn std::error::Error>> {
    let input: BridgeInput = serde_json::from_str(line)?;
    input.claim.validate()?;
    let RecipeOperationRequest::JobRun(request) = RecipeOperationRequest::parse(&input.claim)?
    else {
        return Err("claim did not contain recipe.job.run.v1".into());
    };
    input.result.validate()?;
    let vonk_agent_protocol::generated::AgentResultResult::RecipeJobRunResult(typed_result) =
        input.result.result
    else {
        return Err("result did not contain a recipe job result".into());
    };
    typed_result.validate()?;
    Ok(json!({
        "request": request,
        "result": typed_result,
    }))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        serde_json::to_writer(&mut stdout, &parse_line(&line)?)?;
        stdout.write_all(b"\n")?;
    }
    stdout.flush()?;
    Ok(())
}
