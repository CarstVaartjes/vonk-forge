#![forbid(unsafe_code)]

use std::path::PathBuf;

use clap::Parser;
use vonk_spark_setup::{
    InstallPaths, SetupRequest, SystemCommandRunner, TtyPrompt, handoff_to_root, run_setup,
    validate_system_host,
};

#[derive(Parser)]
#[command(
    name = "vonk-spark-setup",
    version,
    about = "Install or upgrade Vonk Forge on a Spark"
)]
struct Cli {
    #[arg(long, hide = true)]
    package: PathBuf,
    #[arg(long, hide = true)]
    package_sha256: String,
    #[arg(long, hide = true)]
    package_version: String,
    #[arg(long, hide = true)]
    package_architecture: String,
    #[arg(long, hide = true)]
    setup_sha256: String,
    #[arg(long, hide = true)]
    privileged: bool,
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
    let request = SetupRequest::new(
        cli.package,
        cli.package_sha256,
        cli.package_version,
        cli.package_architecture,
        cli.setup_sha256,
        std::env::current_exe()?,
    )?;
    validate_system_host(&request)?;
    let mut runner = SystemCommandRunner;
    let root = rustix::process::geteuid().as_raw() == 0;
    if !cli.privileged && !root {
        handoff_to_root(&request, &mut runner)?;
        return Ok(());
    }
    if cli.privileged && !root {
        return Err("privileged setup must run as root".into());
    }
    let mut prompt = TtyPrompt::open()?;
    run_setup(&request, &paths, &mut prompt, &mut runner)?;
    Ok(())
}
