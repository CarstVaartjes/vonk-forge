#![forbid(unsafe_code)]

use std::fs::{self, File, OpenOptions};
use std::io::{self, BufReader, Read, Write};
use std::path::{Path, PathBuf};

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
    prepare_bundle(cli, Path::new("/dev/tty"), &mut io::stdout())
}

fn prepare_bundle(
    cli: Cli,
    tty_path: &Path,
    status: &mut dyn Write,
) -> Result<(), Box<dyn std::error::Error>> {
    let metadata = fs::metadata(&cli.template)?;
    if !metadata.is_file() || metadata.len() > MAX_TEMPLATE_BYTES {
        return Err("template payload is not a regular file of an acceptable size".into());
    }
    let payload = CanonicalTemplatePayload::from_json(&fs::read(&cli.template)?)?;

    // Upgrades of complete bundles do not prompt. Delay opening the controlling
    // terminal until PromptIo actually needs interactive input so that the
    // supported installer can perform those upgrades from a pipe or exec job.
    let reader = BufReader::new(LazyTty::new(tty_path));
    let mut writer = LazyTty::new(tty_path);
    let mut prompt = PromptIo::with_secret_input(reader, &mut writer, HiddenSecretInput);
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
    writeln!(status, "Bundle ready at {}", outcome.root.display())?;
    Ok(())
}

#[derive(Debug)]
struct LazyTty {
    path: PathBuf,
    file: Option<File>,
}

impl LazyTty {
    fn new(path: &Path) -> Self {
        Self {
            path: path.to_path_buf(),
            file: None,
        }
    }

    fn file(&mut self) -> io::Result<&mut File> {
        if self.file.is_none() {
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .open(&self.path)
                .map_err(|error| {
                    io::Error::new(
                        error.kind(),
                        format!(
                            "interactive input requires {}: {error}",
                            self.path.display()
                        ),
                    )
                })?;
            self.file = Some(file);
        }
        Ok(self.file.as_mut().expect("terminal opened"))
    }
}

impl Read for LazyTty {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        self.file()?.read(buffer)
    }
}

impl Write for LazyTty {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        self.file()?.write(buffer)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file()?.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

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

    #[test]
    fn lazy_tty_opens_only_when_interaction_starts_and_fails_closed() {
        let temporary = tempdir().expect("temporary directory");
        let unavailable_tty = temporary.path().join("no-such-tty");
        let mut tty = LazyTty::new(&unavailable_tty);

        let error = tty
            .write_all(b"prompt")
            .expect_err("first interactive write must open the terminal");
        assert!(
            error.to_string().contains("interactive input requires"),
            "unexpected error: {error}"
        );
    }
}
