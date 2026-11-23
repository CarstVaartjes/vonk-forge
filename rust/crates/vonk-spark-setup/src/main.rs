#![forbid(unsafe_code)]

use std::path::PathBuf;

use clap::{Parser, Subcommand};
use vonk_spark_setup::{
    CallerIdentity, FirewallInputs, InstallPaths, ReleaseAuthority, SetupRequest,
    SystemCommandRunner, TtyPrompt, apply_setup_from, handoff_to_root, prepare_setup,
    validate_system_host,
};

#[derive(Parser)]
#[command(
    name = "vonk-spark-setup",
    version,
    about = "Install or upgrade Vonk Forge on a Spark",
    args_conflicts_with_subcommands = true
)]
struct Cli {
    /// Enroll this Spark using a fresh one-time grant.
    #[arg(long)]
    enroll: bool,
    #[arg(long, hide = true)]
    package: Option<PathBuf>,
    #[arg(long, hide = true)]
    release_manifest: Option<PathBuf>,
    #[arg(long, hide = true)]
    release_signature: Option<PathBuf>,
    #[arg(long, hide = true)]
    setup_signature: Option<PathBuf>,
    /// The installer trust root the channel script verified the release with.
    #[arg(long, hide = true)]
    release_root: Option<PathBuf>,
    #[command(subcommand)]
    command: Option<InternalCommand>,
}

#[derive(Debug, Subcommand)]
enum InternalCommand {
    #[command(name = "__apply", hide = true)]
    Apply,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("vonk-spark-setup: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    let paths = InstallPaths::system();
    let caller = CallerIdentity::current()?;
    if let Some(InternalCommand::Apply) = cli.command {
        let executable = std::env::current_exe()?;
        apply_setup_from(
            std::io::stdin().lock(),
            &executable,
            &paths,
            &mut SystemCommandRunner,
            caller,
        )?;
        return Ok(());
    }
    caller.ensure_public()?;
    let authority = ReleaseAuthority::from_file(
        &cli.release_root
            .ok_or("the release trust root is required")?,
    )?;
    let request = SetupRequest::from_signed_release(
        cli.package.ok_or("release package is required")?,
        cli.release_manifest
            .ok_or("signed release manifest is required")?,
        cli.release_signature
            .ok_or("signed release signature is required")?,
        cli.setup_signature
            .ok_or("signed setup signature is required")?,
        std::env::current_exe()?,
    )?
    .with_controller_address(std::env::var("VONK_CONTROLLER_ADDRESS").ok().as_deref())?
    .with_enrollment_values(
        std::env::var("VONK_ENROLLMENT_URL").ok().as_deref(),
        std::env::var("VONK_CONTROLLER_CA_SHA256").ok().as_deref(),
    )?
    .with_enroll(cli.enroll)
    .with_firewall_inputs(FirewallInputs::from_environment()?);
    validate_system_host(&request)?;
    let mut runner = SystemCommandRunner;
    let mut prompt = TtyPrompt::new();
    let prepared = prepare_setup(
        &request,
        &paths,
        &mut prompt,
        &mut runner,
        caller,
        &authority,
    )?;
    handoff_to_root(&prepared, &mut runner, &authority)?;
    Ok(())
}

#[cfg(test)]
#[path = "main_tests.rs"]
mod tests;
