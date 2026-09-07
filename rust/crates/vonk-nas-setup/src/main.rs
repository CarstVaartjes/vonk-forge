#![forbid(unsafe_code)]

use std::fs::{self, File, OpenOptions};
use std::io::{self, BufReader, Read, Write};
use std::os::unix::fs::MetadataExt;
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
    /// Read newline-delimited answers from a private regular file instead of a TTY.
    #[arg(long)]
    answers_file: Option<PathBuf>,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("vonk-nas-setup: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    if let Some(path) = cli.answers_file.clone() {
        let answers = open_private_answers(&path)?;
        let reader = BufReader::new(answers);
        let mut prompt = PromptIo::new(reader, io::sink());
        prepare_bundle(cli, &mut prompt, &mut io::stdout())
    } else {
        let reader = BufReader::new(LazyTty::new(Path::new("/dev/tty")));
        let mut writer = LazyTty::new(Path::new("/dev/tty"));
        let mut prompt = PromptIo::with_secret_input(reader, &mut writer, HiddenSecretInput);
        prepare_bundle(cli, &mut prompt, &mut io::stdout())
    }
}

fn prepare_bundle<R: io::BufRead, W: Write, S: vonk_nas_setup::SecretInput<R, W>>(
    cli: Cli,
    prompt: &mut PromptIo<R, W, S>,
    status: &mut dyn Write,
) -> Result<(), Box<dyn std::error::Error>> {
    let metadata = fs::metadata(&cli.template)?;
    if !metadata.is_file() || metadata.len() > MAX_TEMPLATE_BYTES {
        return Err("template payload is not a regular file of an acceptable size".into());
    }
    let payload = CanonicalTemplatePayload::from_json(&fs::read(&cli.template)?)?;

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
    let outcome = prepare(&payload, request, prompt, &OsSecretGenerator)?;
    writeln!(status, "Bundle ready at {}", outcome.root.display())?;
    Ok(())
}

fn open_private_answers(path: &Path) -> Result<File, Box<dyn std::error::Error>> {
    let before = fs::symlink_metadata(path)?;
    if !before.is_file()
        || before.file_type().is_symlink()
        || before.nlink() != 1
        || before.len() > 256 * 1024
        || before.mode() & 0o077 != 0
    {
        return Err("answers file must be a private, singly linked regular file".into());
    }
    let file = File::open(path)?;
    let after = file.metadata()?;
    if before.dev() != after.dev() || before.ino() != after.ino() {
        return Err("answers file changed while it was opened".into());
    }
    Ok(file)
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
    use std::os::unix::fs::PermissionsExt;
    use tempfile::tempdir;

    #[test]
    fn output_defaults_to_current_directory() {
        let cli = Cli::try_parse_from(["vonk-nas-setup", "--template", "payload.json"])
            .expect("output is optional");

        assert_eq!(cli.output, PathBuf::from("."));
        assert!(!cli.upgrade);
        assert!(!cli.enable_hermes);
        assert!(!cli.disable_hermes);
        assert!(cli.answers_file.is_none());
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

    #[test]
    fn answers_file_is_an_explicit_supported_input() {
        let cli = Cli::try_parse_from([
            "vonk-nas-setup",
            "--template",
            "payload.json",
            "--answers-file",
            "answers.txt",
            "--disable-hermes",
        ])
        .expect("answers file accepted");

        assert_eq!(cli.answers_file, Some(PathBuf::from("answers.txt")));
        assert!(cli.disable_hermes);
    }

    #[test]
    fn answers_file_must_be_private() {
        let temporary = tempdir().expect("temporary directory");
        let path = temporary.path().join("answers.txt");
        fs::write(&path, "answer\n").expect("write answers");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644))
            .expect("set public permissions");

        let error = open_private_answers(&path).expect_err("public answers rejected");
        assert!(error.to_string().contains("must be a private"));

        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
            .expect("set private permissions");
        open_private_answers(&path).expect("private answers accepted");
    }
}
