#![forbid(unsafe_code)]

use std::path::PathBuf;

use clap::{Parser, Subcommand};
use vonk_spark_setup::{
    CallerIdentity, InstallPaths, SetupRequest, SystemCommandRunner, TtyPrompt, apply_setup_from,
    handoff_to_root, prepare_setup, validate_system_host,
};

#[derive(Parser)]
#[command(
    name = "vonk-spark-setup",
    version,
    about = "Install or upgrade Vonk Forge on a Spark",
    args_conflicts_with_subcommands = true
)]
struct Cli {
    #[arg(long, hide = true)]
    package: Option<PathBuf>,
    #[arg(long, hide = true)]
    package_sha256: Option<String>,
    #[arg(long, hide = true)]
    package_version: Option<String>,
    #[arg(long, hide = true)]
    package_architecture: Option<String>,
    #[arg(long, hide = true)]
    setup_sha256: Option<String>,
    #[command(subcommand)]
    command: Option<InternalCommand>,
}

#[derive(Debug, Subcommand)]
enum InternalCommand {
    #[command(name = "__apply", hide = true)]
    Apply {
        #[arg(long, hide = true)]
        package: PathBuf,
    },
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
    if let Some(InternalCommand::Apply { package }) = cli.command {
        let executable = std::env::current_exe()?;
        apply_setup_from(
            std::io::stdin().lock(),
            &package,
            &executable,
            &paths,
            &mut SystemCommandRunner,
            caller,
        )?;
        return Ok(());
    }
    caller.ensure_public()?;
    let request = SetupRequest::new(
        cli.package.ok_or("release package is required")?,
        cli.package_sha256
            .ok_or("release package SHA-256 is required")?,
        cli.package_version
            .ok_or("release package version is required")?,
        cli.package_architecture
            .ok_or("release package architecture is required")?,
        cli.setup_sha256
            .ok_or("release setup SHA-256 is required")?,
        std::env::current_exe()?,
    )?;
    validate_system_host(&request)?;
    let mut runner = SystemCommandRunner;
    let mut prompt = TtyPrompt::open()?;
    let prepared = prepare_setup(&request, &paths, &mut prompt, &mut runner, caller)?;
    handoff_to_root(&prepared, &mut runner)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hidden_apply_is_the_only_privileged_cli_phase() {
        let cli = Cli::try_parse_from([
            "vonk-spark-setup",
            "__apply",
            "--package",
            "/var/tmp/vonk-spark-setup/agent.deb",
        ])
        .unwrap();
        assert!(matches!(cli.command, Some(InternalCommand::Apply { .. })));
        assert!(
            Cli::try_parse_from(["vonk-spark-setup", "--privileged"]).is_err(),
            "the former whole-installer root mode must not remain accepted"
        );
        assert!(
            Cli::try_parse_from([
                "vonk-spark-setup",
                "--package-sha256",
                &"a".repeat(64),
                "__apply",
                "--package",
                "/var/tmp/vonk-spark-setup/agent.deb",
            ])
            .is_err(),
            "public release arguments cannot be mixed into the root apply phase"
        );
    }
}
