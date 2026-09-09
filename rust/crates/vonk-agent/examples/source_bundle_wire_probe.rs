//! Materialize the exact Controller-produced source archive through production code.
use std::io::{self, Read};
use vonk_agent::build_source::materialize_source_bundle;
use vonk_agent_protocol::canonical_json;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let expected = std::env::args().nth(1).ok_or("source digest is required")?;
    let mut payload = Vec::new();
    io::stdin().read_to_end(&mut payload)?;
    let root = tempfile::tempdir()?;
    let source = materialize_source_bundle(&payload, &expected, root.path())?;
    for (path, content) in &source.files {
        if std::fs::read(root.path().join(path))? != *content {
            return Err("materialized file differs from verified source".into());
        }
    }
    use std::io::Write;
    io::stdout().write_all(&canonical_json(&source.files)?)?;
    Ok(())
}
