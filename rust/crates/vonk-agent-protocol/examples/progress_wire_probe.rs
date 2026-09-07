use std::io::{self, BufRead};
use vonk_agent_protocol::{OperationProgress, canonical_json, parse_strict};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    for line in io::stdin().lock().lines() {
        let progress: OperationProgress = parse_strict(line?.as_bytes())?;
        progress.validate()?;
        println!("{}", String::from_utf8(canonical_json(&progress)?)?);
    }
    Ok(())
}
