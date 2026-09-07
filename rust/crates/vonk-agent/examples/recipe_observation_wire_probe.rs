#![forbid(unsafe_code)]

//! Serialize the production Rust observation wire for the Python Controller
//! boundary.  Input is one exact observation; output is the current snapshot.

use std::io::{self, BufRead};

use vonk_agent_protocol::{
    RECIPE_RUN_OBSERVATION_SCHEMA_VERSION, RecipeRunObservationWire, RecipeRunObservationsWire,
    parse_strict,
};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    for line in io::stdin().lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let observation: RecipeRunObservationWire = parse_strict(line.as_bytes())?;
        observation.validate()?;
        let envelope = RecipeRunObservationsWire {
            schema_version: RECIPE_RUN_OBSERVATION_SCHEMA_VERSION,
            observed_at: observation.observed_at,
            runs: std::slice::from_ref(&observation),
        };
        println!("{}", serde_json::to_string(&envelope)?);
    }
    Ok(())
}
