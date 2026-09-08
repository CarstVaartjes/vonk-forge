//! Verify actual publisher bytes through the production signature and manifest reader.
use std::{env, fs};
use vonk_spark_setup::ReleaseAuthority;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args().skip(1).collect();
    if args.len() != 3 {
        return Err("expected manifest, signature, and public key paths".into());
    }
    let authority = ReleaseAuthority::from_pem(fs::read(&args[2])?)?;
    let document = authority.verify_manifest(&fs::read(&args[0])?, &fs::read(&args[1])?)?;
    println!("{}", serde_json::to_string(&document)?);
    Ok(())
}
