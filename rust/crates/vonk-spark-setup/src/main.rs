#![forbid(unsafe_code)]

use std::{io, path::PathBuf};

use clap::Parser;
use vonk_spark_setup::{
    InstallPaths, SetupRequest, SystemCommandRunner, TtyPrompt, privileged_write_from, run_setup,
};

#[derive(Parser)]
#[command(
    name = "vonk-spark-setup",
    version,
    about = "Install or upgrade Vonk Forge on a Spark"
)]
struct Cli {
    #[arg(long, hide = true, required_unless_present = "privileged_write")]
    package: Option<PathBuf>,
    #[arg(long, hide = true, required_unless_present = "privileged_write")]
    package_sha256: Option<String>,
    #[arg(long, hide = true)]
    privileged_write: bool,
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
    if cli.privileged_write {
        if cli.package.is_some() || cli.package_sha256.is_some() {
            return Err("privileged writer does not accept setup arguments".into());
        }
        return privileged_write_from(io::stdin(), &paths).map_err(Into::into);
    }
    let request = SetupRequest::new(
        cli.package.ok_or("package is required")?,
        cli.package_sha256.ok_or("package SHA-256 is required")?,
        std::env::current_exe()?,
    )?;
    let mut prompt = TtyPrompt::open()?;
    let mut runner = SystemCommandRunner;
    run_setup(&request, &paths, &mut prompt, &mut runner)?;
    Ok(())
}
