//! Commands.

use super::*;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Command {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub stdin: Vec<u8>,
    pub stderr: CommandStderr,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CommandStderr {
    Inherit,
    Suppress,
    CaptureAndForward,
}

impl Command {
    pub fn new(
        program: impl Into<PathBuf>,
        args: impl IntoIterator<Item = impl Into<String>>,
    ) -> Self {
        Self {
            program: program.into(),
            args: args.into_iter().map(Into::into).collect(),
            env: BTreeMap::new(),
            stdin: Vec::new(),
            stderr: CommandStderr::Inherit,
        }
    }

    pub fn with_stdin(mut self, stdin: Vec<u8>) -> Self {
        self.stdin = stdin;
        self
    }

    pub fn with_env(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.env.insert(name.into(), value.into());
        self
    }

    pub fn suppress_stderr(mut self) -> Self {
        self.stderr = CommandStderr::Suppress;
        self
    }

    pub fn capture_and_forward_stderr(mut self) -> Self {
        self.stderr = CommandStderr::CaptureAndForward;
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    pub success: bool,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

impl CommandOutput {
    pub fn success(stdout: Vec<u8>) -> Self {
        Self {
            success: true,
            stdout,
            stderr: Vec::new(),
        }
    }

    pub fn success_empty() -> Self {
        Self::success(Vec::new())
    }
}

pub trait CommandRunner {
    fn run(&mut self, command: Command) -> Result<CommandOutput, String>;

    fn authenticate_sudo(&mut self, sudo: &Path) -> Result<(), SetupError>;

    fn sleep(&mut self, duration: Duration) {
        thread::sleep(duration);
    }
}

pub trait Prompt {
    fn value(&mut self, label: &str) -> Result<String, String>;
    fn secret(&mut self, label: &str) -> Result<String, String>;
}
