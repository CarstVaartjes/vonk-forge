#![forbid(unsafe_code)]

use std::fs::{self, OpenOptions};
use std::io::{BufReader, Write};
use std::path::PathBuf;

use clap::Parser;
use vonk_nas_setup::{
    CanonicalTemplatePayload, HiddenSecretInput, OsSecretGenerator, PromptIo, SetupRequest, prepare,
};

const MAX_TEMPLATE_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Debug, Parser)]
#[command(about = "Prepare a Vonk Forge NAS drag-and-drop bundle")]
struct Cli {
    /// Already-verified canonical template payload on the local filesystem.
    #[arg(long)]
    template: PathBuf,
    /// Directory in which the vonk-forge bundle directory will be created.
    #[arg(long, default_value = ".")]
    output: PathBuf,
    /// Preserve site-local values and atomically replace only docker-compose.yaml.
    #[arg(long)]
    upgrade: bool,
    /// Explicitly enable Hermes while preserving all other site-local state.
    #[arg(long, conflicts_with = "disable_hermes")]
    enable_hermes: bool,
    /// Explicitly disable Hermes while preserving its configuration for reuse.
    #[arg(long)]
    disable_hermes: bool,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("vonk-nas-setup: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    let metadata = fs::metadata(&cli.template)?;
    if !metadata.is_file() || metadata.len() > MAX_TEMPLATE_BYTES {
        return Err("template payload is not a regular file of an acceptable size".into());
    }
    let payload = CanonicalTemplatePayload::from_json(&fs::read(&cli.template)?)?;

    let mut tty = OpenOptions::new().read(true).write(true).open("/dev/tty")?;
    let reader = BufReader::new(tty.try_clone()?);
    let mut prompt = PromptIo::with_secret_input(reader, &mut tty, HiddenSecretInput);
    let request = if cli.upgrade {
        SetupRequest::upgrade(&cli.output)
    } else {
        SetupRequest::install(&cli.output)
    };
    let request = if cli.enable_hermes {
        request.with_hermes_enabled(true)
    } else if cli.disable_hermes {
        request.with_hermes_enabled(false)
    } else {
        request
    };
    let outcome = prepare(&payload, request, &mut prompt, &OsSecretGenerator)?;
    writeln!(tty, "Bundle ready at {}", outcome.root.display())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn output_defaults_to_current_directory() {
        let cli = Cli::try_parse_from(["vonk-nas-setup", "--template", "payload.json"])
            .expect("output is optional");

        assert_eq!(cli.output, PathBuf::from("."));
        assert!(!cli.upgrade);
        assert!(!cli.enable_hermes);
        assert!(!cli.disable_hermes);
    }

    #[test]
    fn hermes_switches_are_explicit_and_mutually_exclusive() {
        let enabled = Cli::try_parse_from([
            "vonk-nas-setup",
            "--template",
            "payload.json",
            "--enable-hermes",
        ])
        .expect("enable flag accepted");
        assert!(enabled.enable_hermes);

        Cli::try_parse_from([
            "vonk-nas-setup",
            "--template",
            "payload.json",
            "--enable-hermes",
            "--disable-hermes",
        ])
        .expect_err("conflicting Hermes flags rejected");
    }
}
