//! Drive the real store collector with a Controller verdict and a bounded
//! model-store capacity. Measuring actual object files injects a full disk
//! without allocating the host's remaining terabytes or requiring root.
use std::{
    fs,
    path::Path,
    time::{Duration, Instant, SystemTime},
};
use vonk_agent::{
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    store_retention::{LatestVerdict, sweep},
};

/// The probe drives the model store only: the user manager it reports holds
/// no build unit and no Podman scope, so it never stops the host's.
struct IdleUserManager;

impl ProcessRunner for IdleUserManager {
    fn run(&self, _: Program, _: &[String], _: Duration) -> Result<ProcessOutput, ProcessError> {
        Ok(ProcessOutput {
            exit_code: None,
            success: true,
            stdout: vec![],
            stderr: vec![],
        })
    }
}

fn free(root: &Path, capacity: u64) -> Option<(u64, u64)> {
    let mut used = 0_u64;
    for entry in fs::read_dir(root.join("distribution/models")).ok()? {
        let entry = entry.ok()?;
        used = used.checked_add(entry.metadata().ok()?.len())?;
    }
    Some((capacity.saturating_sub(used), capacity))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 4 {
        return Err("expected data root, inventory verdict, capacity".into());
    }
    let root = Path::new(&args[1]);
    let capacity: u64 = args[3].parse()?;
    let verdict = LatestVerdict::default();
    verdict.record(
        Some(serde_json::from_slice(&fs::read(&args[2])?)?),
        &vonk_agent::store_retention::installation_names(root).unwrap_or_default(),
        &vonk_agent::store_retention::run_scope_names(root).unwrap_or_default(),
    );
    sweep(
        root,
        SystemTime::now(),
        Instant::now() + Duration::from_secs(10),
        verdict.fresh().as_ref(),
        &|| free(root, capacity),
        &|| {},
        &IdleUserManager,
    );
    println!(
        "{}",
        free(root, capacity)
            .ok_or("model store measurement failed")?
            .0
    );
    Ok(())
}
