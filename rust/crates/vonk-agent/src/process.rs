use std::{
    collections::{BTreeMap, HashSet},
    fs::{self, File},
    io::{Read, Seek, SeekFrom, Write},
    os::unix::fs::MetadataExt,
    path::Path,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

use thiserror::Error;

const DIAGNOSTIC_LIMIT: u64 = 64 * 1024;
const DIAGNOSTIC_TRUNCATED: &[u8] = b"[earlier diagnostic output truncated]\n";
const DISK_POLL_INTERVAL: Duration = Duration::from_secs(1);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Program {
    Curl,
    Docker,
    NvidiaCtk,
    NvidiaSmi,
    Oras,
    Podman,
    SystemdRun,
    Systemctl,
}

impl Program {
    fn path(self) -> &'static str {
        match self {
            Self::Curl => "/usr/bin/curl",
            Self::Docker => "/usr/bin/docker",
            Self::NvidiaCtk => "/usr/bin/nvidia-ctk",
            Self::NvidiaSmi => "/usr/bin/nvidia-smi",
            Self::Oras => "/usr/lib/vonk-forge/oras",
            Self::Podman => "/usr/bin/podman",
            Self::SystemdRun => "/usr/bin/systemd-run",
            Self::Systemctl => "/usr/bin/systemctl",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessOutput {
    pub success: bool,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

#[derive(Clone, Copy)]
pub struct ProcessOutputBounds<'a> {
    directory: &'a Path,
    maximum_bytes: u64,
    maximum_output_bytes: u64,
}

impl<'a> ProcessOutputBounds<'a> {
    pub fn new(directory: &'a Path, maximum_bytes: u64, maximum_output_bytes: u64) -> Self {
        Self {
            directory,
            maximum_bytes,
            maximum_output_bytes,
        }
    }
}

#[derive(Clone, Copy)]
pub struct ProcessInputBounds<'a> {
    input: &'a File,
    directory: &'a Path,
    maximum_bytes: u64,
}

#[derive(Clone, Copy)]
pub struct ProcessDiskReserve<'a> {
    filesystem: &'a Path,
    minimum_free_bytes: u64,
}

impl<'a> ProcessDiskReserve<'a> {
    pub fn new(filesystem: &'a Path, minimum_free_bytes: u64) -> Self {
        Self {
            filesystem,
            minimum_free_bytes,
        }
    }

    pub fn filesystem(self) -> &'a Path {
        self.filesystem
    }

    pub fn minimum_free_bytes(self) -> u64 {
        self.minimum_free_bytes
    }
}

impl<'a> ProcessInputBounds<'a> {
    pub fn new(input: &'a File, directory: &'a Path, maximum_bytes: u64) -> Self {
        Self {
            input,
            directory,
            maximum_bytes,
        }
    }
}

#[derive(Debug, Error)]
pub enum ProcessError {
    #[error("approved subprocess failed to start or complete")]
    Io(#[from] std::io::Error),
    #[error("approved subprocess exceeded its deadline")]
    Timeout,
    #[error("approved subprocess output exceeded its limit")]
    OutputLimit,
    #[error("approved subprocess exceeded its storage limit")]
    StorageLimit,
    #[error("approved subprocess was cancelled")]
    Cancelled,
}

pub trait ProcessRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError>;

    fn run_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        if cancelled() {
            return Err(ProcessError::Cancelled);
        }
        let output = self.run(program, arguments, timeout)?;
        if cancelled() {
            return Err(ProcessError::Cancelled);
        }
        Ok(output)
    }

    /// Run a subprocess while preserving a real free-space reserve on the
    /// filesystem that contains its working data. This is intentionally not a
    /// directory-size quota: container graph drivers may expose shared layers
    /// through implementation-specific links and paths.
    fn run_with_disk_reserve_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        _reserve: ProcessDiskReserve<'_>,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        self.run_cancellable(program, arguments, timeout, cancelled)
    }

    fn run_bounded_directory(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        directory: &Path,
        maximum_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        let output = self.run(program, arguments, timeout)?;
        if directory_bytes(directory)? > maximum_bytes {
            return Err(ProcessError::StorageLimit);
        }
        Ok(output)
    }

    /// Run a subprocess while enforcing both its bounded working directory and
    /// declared output limits. Existing runners can keep implementing
    /// `run_bounded_directory`; the default implementation applies the output
    /// check after that call, while the system runner enforces it while the
    /// process is still running.
    fn run_bounded_directory_with_output_limit(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        directory: &Path,
        maximum_bytes: u64,
        maximum_output_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        let diagnostic_limit = maximum_output_bytes.min(DIAGNOSTIC_LIMIT);
        let mut output =
            self.run_bounded_directory(program, arguments, timeout, directory, maximum_bytes)?;
        truncate_process_output(&mut output, diagnostic_limit);
        Ok(output)
    }

    fn run_bounded_directory_with_output_limit_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        bounds: ProcessOutputBounds<'_>,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        if cancelled() {
            return Err(ProcessError::Cancelled);
        }
        let output = self.run_bounded_directory_with_output_limit(
            program,
            arguments,
            timeout,
            bounds.directory,
            bounds.maximum_bytes,
            bounds.maximum_output_bytes,
        )?;
        if cancelled() {
            return Err(ProcessError::Cancelled);
        }
        Ok(output)
    }

    /// Stream stdout into a caller-owned regular file while bounding the
    /// artifact independently from diagnostic stderr. The default keeps test
    /// runners small; the system runner overrides this to avoid buffering
    /// multi-gigabyte artifacts in memory.
    fn run_to_file(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        sink: &mut File,
        maximum_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        let output = self.run(program, arguments, timeout)?;
        if output.stdout.len() as u64 > maximum_bytes {
            return Err(ProcessError::OutputLimit);
        }
        sink.set_len(0)?;
        sink.seek(SeekFrom::Start(0))?;
        sink.write_all(&output.stdout)?;
        sink.flush()?;
        Ok(ProcessOutput {
            success: output.success,
            stdout: Vec::new(),
            stderr: output.stderr,
        })
    }

    /// Run an approved process with a pre-opened immutable input descriptor.
    fn run_with_input(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        _input: &File,
    ) -> Result<ProcessOutput, ProcessError> {
        self.run(program, arguments, timeout)
    }

    fn run_with_input_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        input: &File,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        if cancelled() {
            return Err(ProcessError::Cancelled);
        }
        let output = self.run_with_input(program, arguments, timeout, input)?;
        if cancelled() {
            return Err(ProcessError::Cancelled);
        }
        Ok(output)
    }

    fn run_with_input_disk_reserve_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        input: &File,
        _reserve: ProcessDiskReserve<'_>,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        self.run_with_input_cancellable(program, arguments, timeout, input, cancelled)
    }

    fn run_bounded_directory_with_input(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        input: &File,
        directory: &Path,
        maximum_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        let output = self.run_with_input(program, arguments, timeout, input)?;
        if directory_bytes(directory)? > maximum_bytes {
            return Err(ProcessError::StorageLimit);
        }
        Ok(output)
    }

    fn run_bounded_directory_with_input_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        bounds: ProcessInputBounds<'_>,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        if cancelled() {
            return Err(ProcessError::Cancelled);
        }
        let output = self.run_bounded_directory_with_input(
            program,
            arguments,
            timeout,
            bounds.input,
            bounds.directory,
            bounds.maximum_bytes,
        )?;
        if cancelled() {
            return Err(ProcessError::Cancelled);
        }
        Ok(output)
    }
}

pub struct SystemProcessRunner;

struct ProcessRunOptions<'a> {
    storage_limit: Option<(&'a Path, u64)>,
    disk_reserve: Option<(&'a Path, u64)>,
    diagnostic_limit: u64,
    input: Option<&'a File>,
    cancellation: Option<&'a dyn Fn() -> bool>,
}

impl ProcessRunner for SystemProcessRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(
            program,
            arguments,
            timeout,
            ProcessRunOptions {
                storage_limit: None,
                disk_reserve: None,
                diagnostic_limit: DIAGNOSTIC_LIMIT,
                input: None,
                cancellation: None,
            },
        )
    }

    fn run_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(
            program,
            arguments,
            timeout,
            ProcessRunOptions {
                storage_limit: None,
                disk_reserve: None,
                diagnostic_limit: DIAGNOSTIC_LIMIT,
                input: None,
                cancellation: Some(cancelled),
            },
        )
    }

    fn run_with_disk_reserve_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        reserve: ProcessDiskReserve<'_>,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(
            program,
            arguments,
            timeout,
            ProcessRunOptions {
                storage_limit: None,
                disk_reserve: Some((reserve.filesystem, reserve.minimum_free_bytes)),
                diagnostic_limit: DIAGNOSTIC_LIMIT,
                input: None,
                cancellation: Some(cancelled),
            },
        )
    }

    fn run_bounded_directory(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        directory: &Path,
        maximum_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(
            program,
            arguments,
            timeout,
            ProcessRunOptions {
                storage_limit: Some((directory, maximum_bytes)),
                disk_reserve: None,
                diagnostic_limit: DIAGNOSTIC_LIMIT,
                input: None,
                cancellation: None,
            },
        )
    }

    fn run_bounded_directory_with_output_limit(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        directory: &Path,
        maximum_bytes: u64,
        maximum_output_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(
            program,
            arguments,
            timeout,
            ProcessRunOptions {
                storage_limit: Some((directory, maximum_bytes)),
                disk_reserve: None,
                diagnostic_limit: maximum_output_bytes.min(DIAGNOSTIC_LIMIT),
                input: None,
                cancellation: None,
            },
        )
    }

    fn run_bounded_directory_with_output_limit_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        bounds: ProcessOutputBounds<'_>,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(
            program,
            arguments,
            timeout,
            ProcessRunOptions {
                storage_limit: Some((bounds.directory, bounds.maximum_bytes)),
                disk_reserve: None,
                diagnostic_limit: bounds.maximum_output_bytes.min(DIAGNOSTIC_LIMIT),
                input: None,
                cancellation: Some(cancelled),
            },
        )
    }

    fn run_to_file(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        sink: &mut File,
        maximum_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process_to_file(program, arguments, timeout, sink, maximum_bytes)
    }

    fn run_with_input(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        input: &File,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(
            program,
            arguments,
            timeout,
            ProcessRunOptions {
                storage_limit: None,
                disk_reserve: None,
                diagnostic_limit: DIAGNOSTIC_LIMIT,
                input: Some(input),
                cancellation: None,
            },
        )
    }

    fn run_with_input_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        input: &File,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(
            program,
            arguments,
            timeout,
            ProcessRunOptions {
                storage_limit: None,
                disk_reserve: None,
                diagnostic_limit: DIAGNOSTIC_LIMIT,
                input: Some(input),
                cancellation: Some(cancelled),
            },
        )
    }

    fn run_with_input_disk_reserve_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        input: &File,
        reserve: ProcessDiskReserve<'_>,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(
            program,
            arguments,
            timeout,
            ProcessRunOptions {
                storage_limit: None,
                disk_reserve: Some((reserve.filesystem, reserve.minimum_free_bytes)),
                diagnostic_limit: DIAGNOSTIC_LIMIT,
                input: Some(input),
                cancellation: Some(cancelled),
            },
        )
    }

    fn run_bounded_directory_with_input(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        input: &File,
        directory: &Path,
        maximum_bytes: u64,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(
            program,
            arguments,
            timeout,
            ProcessRunOptions {
                storage_limit: Some((directory, maximum_bytes)),
                disk_reserve: None,
                diagnostic_limit: DIAGNOSTIC_LIMIT,
                input: Some(input),
                cancellation: None,
            },
        )
    }

    fn run_bounded_directory_with_input_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        bounds: ProcessInputBounds<'_>,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(
            program,
            arguments,
            timeout,
            ProcessRunOptions {
                storage_limit: Some((bounds.directory, bounds.maximum_bytes)),
                disk_reserve: None,
                diagnostic_limit: DIAGNOSTIC_LIMIT,
                input: Some(bounds.input),
                cancellation: Some(cancelled),
            },
        )
    }
}

fn run_process(
    program: Program,
    arguments: &[String],
    timeout: Duration,
    options: ProcessRunOptions<'_>,
) -> Result<ProcessOutput, ProcessError> {
    if let Some((filesystem, minimum_free_bytes)) = options.disk_reserve
        && available_filesystem_bytes(filesystem)? < minimum_free_bytes
    {
        return Err(ProcessError::StorageLimit);
    }
    let environment =
        subprocess_environment(program, rustix::process::geteuid().as_raw(), arguments);
    let stdin = match options.input {
        Some(input) => Stdio::from(input.try_clone()?),
        None => Stdio::null(),
    };
    let mut child = Command::new(program.path())
        .args(arguments)
        .stdin(stdin)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_clear()
        .envs(environment)
        .spawn()?;
    let stdout_pipe = match child.stdout.take() {
        Some(pipe) => pipe,
        None => {
            terminate_process(&mut child, program, arguments)?;
            return Err(std::io::Error::other("subprocess stdout is unavailable").into());
        }
    };
    let stdout = match capture_diagnostics(stdout_pipe, options.diagnostic_limit) {
        Ok(capture) => capture,
        Err(error) => {
            terminate_process(&mut child, program, arguments)?;
            return Err(error.into());
        }
    };
    let stderr_pipe = match child.stderr.take() {
        Some(pipe) => pipe,
        None => {
            terminate_process(&mut child, program, arguments)?;
            return Err(std::io::Error::other("subprocess stderr is unavailable").into());
        }
    };
    let stderr = match capture_diagnostics(stderr_pipe, options.diagnostic_limit) {
        Ok(capture) => capture,
        Err(error) => {
            terminate_process(&mut child, program, arguments)?;
            return Err(error.into());
        }
    };
    let started = Instant::now();
    let mut last_storage_check = started;
    let mut last_disk_check = started;
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if options.cancellation.is_some_and(|cancelled| cancelled()) {
            terminate_process(&mut child, program, arguments)?;
            return Err(ProcessError::Cancelled);
        }
        if started.elapsed() >= timeout {
            terminate_process(&mut child, program, arguments)?;
            return Err(ProcessError::Timeout);
        }
        if let Some((directory, maximum_bytes)) = options.storage_limit
            && last_storage_check.elapsed() >= Duration::from_millis(25)
        {
            match directory_bytes(directory) {
                Ok(bytes) if bytes > maximum_bytes => {
                    terminate_process(&mut child, program, arguments)?;
                    return Err(ProcessError::StorageLimit);
                }
                Err(error) => {
                    terminate_process(&mut child, program, arguments)?;
                    return Err(ProcessError::Io(error));
                }
                Ok(_) => {}
            }
            last_storage_check = Instant::now();
        }
        if let Some((filesystem, minimum_free_bytes)) = options.disk_reserve
            && last_disk_check.elapsed() >= DISK_POLL_INTERVAL
        {
            match available_filesystem_bytes(filesystem) {
                Ok(available) if available < minimum_free_bytes => {
                    terminate_process(&mut child, program, arguments)?;
                    return Err(ProcessError::StorageLimit);
                }
                Err(error) => {
                    terminate_process(&mut child, program, arguments)?;
                    return Err(ProcessError::Io(error));
                }
                Ok(_) => {}
            }
            last_disk_check = Instant::now();
        }
        thread::sleep(Duration::from_millis(25));
    };
    let stdout = join_diagnostics(stdout)?;
    let stderr = join_diagnostics(stderr)?;
    if options.cancellation.is_some_and(|cancelled| cancelled()) {
        return Err(ProcessError::Cancelled);
    }
    Ok(ProcessOutput {
        success: status.success(),
        stdout,
        stderr,
    })
}

fn available_filesystem_bytes(path: &Path) -> Result<u64, std::io::Error> {
    let filesystem = rustix::fs::statvfs(path).map_err(std::io::Error::from)?;
    filesystem
        .f_bavail
        .checked_mul(filesystem.f_frsize)
        .ok_or_else(|| std::io::Error::other("filesystem capacity overflow"))
}

fn capture_diagnostics<R: Read + Send + 'static>(
    mut reader: R,
    limit: u64,
) -> Result<thread::JoinHandle<Result<Vec<u8>, std::io::Error>>, std::io::Error> {
    let capacity = usize::try_from(limit).unwrap_or(usize::MAX);
    thread::Builder::new()
        .name("vonk-process-diagnostics".to_owned())
        .spawn(move || {
            let mut ring = DiagnosticRing::new(capacity);
            let mut chunk = [0_u8; 8192];
            loop {
                let count = reader.read(&mut chunk)?;
                if count == 0 {
                    return Ok(ring.finish());
                }
                ring.push(&chunk[..count]);
            }
        })
}

fn join_diagnostics(
    capture: thread::JoinHandle<Result<Vec<u8>, std::io::Error>>,
) -> Result<Vec<u8>, std::io::Error> {
    capture
        .join()
        .map_err(|_| std::io::Error::other("diagnostic capture thread failed"))?
}

struct DiagnosticRing {
    bytes: Vec<u8>,
    capacity: usize,
    truncated: bool,
}

impl DiagnosticRing {
    fn new(capacity: usize) -> Self {
        Self {
            bytes: Vec::with_capacity(capacity),
            capacity,
            truncated: false,
        }
    }

    fn push(&mut self, value: &[u8]) {
        if self.capacity == 0 {
            self.truncated |= !value.is_empty();
            return;
        }
        if value.len() >= self.capacity {
            self.bytes.clear();
            self.bytes
                .extend_from_slice(&value[value.len() - self.capacity..]);
            self.truncated = true;
            return;
        }
        let overflow = self
            .bytes
            .len()
            .saturating_add(value.len())
            .saturating_sub(self.capacity);
        if overflow > 0 {
            self.bytes.drain(..overflow);
            self.truncated = true;
        }
        self.bytes.extend_from_slice(value);
    }

    fn finish(mut self) -> Vec<u8> {
        if !self.truncated || self.capacity == 0 {
            return self.bytes;
        }
        let marker_bytes = DIAGNOSTIC_TRUNCATED.len().min(self.capacity);
        let retained = self.capacity - marker_bytes;
        let start = self.bytes.len().saturating_sub(retained);
        let mut value = Vec::with_capacity(self.capacity);
        value.extend_from_slice(&DIAGNOSTIC_TRUNCATED[..marker_bytes]);
        value.extend_from_slice(&self.bytes[start..]);
        self.bytes.clear();
        value
    }
}

fn terminate_process(
    child: &mut Child,
    program: Program,
    arguments: &[String],
) -> Result<(), std::io::Error> {
    if let Some(unit) = transient_user_service_unit(program, arguments) {
        let environment =
            subprocess_environment(Program::Systemctl, rustix::process::geteuid().as_raw(), &[]);
        let _ = Command::new(Program::Systemctl.path())
            .args(["--user", "stop", unit])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .env_clear()
            .envs(environment)
            .status();
    }
    let _ = child.kill();
    child.wait()?;
    Ok(())
}

fn transient_user_service_unit(program: Program, arguments: &[String]) -> Option<&str> {
    if program != Program::SystemdRun || !arguments.iter().any(|value| value == "--user") {
        return None;
    }
    arguments.iter().find_map(|value| {
        let unit = value.strip_prefix("--unit=vonk-recipe-build-")?;
        (!unit.is_empty()
            && unit
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() || byte == b'-'))
        .then_some(value.strip_prefix("--unit=").unwrap())
    })
}

fn run_process_to_file(
    program: Program,
    arguments: &[String],
    timeout: Duration,
    sink: &mut File,
    maximum_bytes: u64,
) -> Result<ProcessOutput, ProcessError> {
    sink.set_len(0)?;
    sink.seek(SeekFrom::Start(0))?;
    let environment =
        subprocess_environment(program, rustix::process::geteuid().as_raw(), arguments);
    let mut child = Command::new(program.path())
        .args(arguments)
        .stdin(Stdio::null())
        .stdout(Stdio::from(sink.try_clone()?))
        .stderr(Stdio::piped())
        .env_clear()
        .envs(environment)
        .spawn()?;
    let stderr_pipe = match child.stderr.take() {
        Some(pipe) => pipe,
        None => {
            child.kill()?;
            child.wait()?;
            return Err(std::io::Error::other("subprocess stderr is unavailable").into());
        }
    };
    let stderr = match capture_diagnostics(stderr_pipe, DIAGNOSTIC_LIMIT) {
        Ok(capture) => capture,
        Err(error) => {
            child.kill()?;
            child.wait()?;
            return Err(error.into());
        }
    };
    let started = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if started.elapsed() >= timeout {
            child.kill()?;
            child.wait()?;
            return Err(ProcessError::Timeout);
        }
        if sink.metadata()?.len() > maximum_bytes {
            child.kill()?;
            child.wait()?;
            return Err(ProcessError::StorageLimit);
        }
        thread::sleep(Duration::from_millis(25));
    };
    let stderr = join_diagnostics(stderr)?;
    sink.flush()?;
    if sink.metadata()?.len() > maximum_bytes {
        return Err(ProcessError::StorageLimit);
    }
    Ok(ProcessOutput {
        success: status.success(),
        stdout: Vec::new(),
        stderr,
    })
}

fn subprocess_environment(
    program: Program,
    effective_uid: u32,
    arguments: &[String],
) -> BTreeMap<&'static str, String> {
    let mut environment = BTreeMap::from([
        ("LANG", "C.UTF-8".to_owned()),
        ("LC_ALL", "C.UTF-8".to_owned()),
        ("PATH", "/usr/bin:/bin".to_owned()),
        ("HOME", "/var/lib/vonk-forge-agent".to_owned()),
        (
            "XDG_CONFIG_HOME",
            "/var/lib/vonk-forge-agent/.config".to_owned(),
        ),
        ("XDG_DATA_HOME", "/var/lib/vonk-forge-agent".to_owned()),
        (
            "CONTAINERS_STORAGE_CONF",
            "/etc/vonk-forge-agent/containers-storage.conf".to_owned(),
        ),
    ]);
    if matches!(
        program,
        Program::Podman | Program::SystemdRun | Program::Systemctl
    ) {
        let runtime = format!("/run/user/{effective_uid}");
        environment.insert("XDG_RUNTIME_DIR", runtime.clone());
        environment.insert(
            "DBUS_SESSION_BUS_ADDRESS",
            format!("unix:path={runtime}/bus"),
        );
        if let Some(temporary_directory) = podman_image_tmpdir(arguments) {
            environment.insert("TMPDIR", temporary_directory.display().to_string());
        }
    } else {
        environment.insert("XDG_RUNTIME_DIR", "/run/vonk-forge-agent".to_owned());
    }
    environment
}

fn podman_image_tmpdir(arguments: &[String]) -> Option<std::path::PathBuf> {
    arguments.windows(2).find_map(|pair| {
        (pair[0] == "--root")
            .then_some(Path::new(&pair[1]))
            .and_then(Path::parent)
            .map(|parent| parent.join("podman-image-tmp"))
    })
}

fn directory_bytes(path: &Path) -> Result<u64, std::io::Error> {
    if !path.exists() {
        return Ok(0);
    }
    let mut total = 0_u64;
    // Overlay stores can expose the same layer payload through several hard
    // links. Disk reservations describe bytes consumed, so counting every
    // pathname at its full logical length can falsely multiply one inode past
    // the limit and kill an otherwise bounded build. Keep sparse-file logical
    // length accounting fail-closed, but count each regular-file inode once.
    let mut regular_files = HashSet::new();
    let mut pending = vec![path.to_path_buf()];
    while let Some(directory) = pending.pop() {
        let Some(entries) = present_during_scan(fs::read_dir(directory))? else {
            continue;
        };
        for entry in entries {
            let Some(entry) = present_during_scan(entry)? else {
                continue;
            };
            let Some(metadata) = present_during_scan(entry.file_type())? else {
                continue;
            };
            if metadata.is_symlink() {
                if let Some(metadata) = present_during_scan(fs::symlink_metadata(entry.path()))? {
                    total = total
                        .checked_add(metadata.len())
                        .ok_or_else(|| std::io::Error::other("directory size overflow"))?;
                }
            } else if metadata.is_dir() {
                pending.push(entry.path());
            } else if metadata.is_file()
                && let Some(metadata) = present_during_scan(entry.metadata())?
                && regular_files.insert((metadata.dev(), metadata.ino()))
            {
                total = total
                    .checked_add(metadata.len())
                    .ok_or_else(|| std::io::Error::other("directory size overflow"))?;
            }
        }
    }
    Ok(total)
}

fn present_during_scan<T>(result: Result<T, std::io::Error>) -> Result<Option<T>, std::io::Error> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn truncate_process_output(output: &mut ProcessOutput, limit: u64) {
    let capacity = usize::try_from(limit).unwrap_or(usize::MAX);
    for diagnostics in [&mut output.stdout, &mut output.stderr] {
        if diagnostics.len() <= capacity {
            continue;
        }
        let mut ring = DiagnosticRing::new(capacity);
        ring.push(diagnostics);
        *diagnostics = ring.finish();
    }
}

#[cfg(test)]
mod tests {
    use super::{
        DIAGNOSTIC_LIMIT, DIAGNOSTIC_TRUNCATED, ProcessDiskReserve, ProcessError, ProcessRunner,
        Program, SystemProcessRunner, directory_bytes, podman_image_tmpdir, present_during_scan,
        subprocess_environment, transient_user_service_unit,
    };
    use std::{
        fs,
        io::{Read, Seek, SeekFrom, Write},
        net::TcpListener,
        os::unix::fs::symlink,
        thread,
        time::Duration,
    };
    use tempfile::{tempdir, tempfile};

    #[test]
    fn podman_and_its_user_service_wrapper_use_the_effective_users_systemd_bus() {
        let temporary = tempdir().unwrap();
        let root = temporary.path().join("podman-storage");
        let arguments = vec!["--root".to_owned(), root.display().to_string()];
        for program in [Program::Podman, Program::SystemdRun] {
            let environment = subprocess_environment(program, 128, &arguments);

            assert_eq!(
                environment["XDG_CONFIG_HOME"],
                "/var/lib/vonk-forge-agent/.config"
            );
            assert_eq!(environment["XDG_RUNTIME_DIR"], "/run/user/128");
            assert_eq!(
                environment["DBUS_SESSION_BUS_ADDRESS"],
                "unix:path=/run/user/128/bus"
            );
            assert_eq!(
                environment["TMPDIR"],
                temporary
                    .path()
                    .join("podman-image-tmp")
                    .display()
                    .to_string()
            );
        }
        assert_eq!(
            podman_image_tmpdir(&arguments),
            Some(temporary.path().join("podman-image-tmp"))
        );
    }

    #[test]
    fn non_podman_tools_keep_the_private_agent_runtime() {
        let environment = subprocess_environment(Program::Curl, 128, &[]);

        assert_eq!(environment["XDG_RUNTIME_DIR"], "/run/vonk-forge-agent");
        assert!(!environment.contains_key("DBUS_SESSION_BUS_ADDRESS"));
    }

    #[test]
    fn only_the_fixed_recipe_build_user_service_is_stoppable() {
        let arguments = vec![
            "--user".to_owned(),
            "--unit=vonk-recipe-build-00000000-0000-4000-8000-000000000002".to_owned(),
        ];
        assert_eq!(
            transient_user_service_unit(Program::SystemdRun, &arguments),
            Some("vonk-recipe-build-00000000-0000-4000-8000-000000000002")
        );
        assert_eq!(
            transient_user_service_unit(
                Program::SystemdRun,
                &["--unit=unrelated.service".to_owned()]
            ),
            None
        );
        assert_eq!(
            transient_user_service_unit(
                Program::SystemdRun,
                &[
                    "--user".to_owned(),
                    "--unit=vonk-recipe-build-../../other".to_owned(),
                ]
            ),
            None
        );
    }

    #[test]
    fn storage_scan_ignores_paths_removed_by_the_running_process() {
        let directory = tempdir().unwrap();
        let removed = directory.path().join("transient-layer");

        assert!(
            present_during_scan(fs::metadata(removed))
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn storage_accounting_counts_a_symlink_without_following_it() {
        let directory = tempdir().unwrap();
        symlink("/usr/bin", directory.path().join("bin")).unwrap();

        assert_eq!(directory_bytes(directory.path()).unwrap(), 8);
    }

    #[test]
    fn storage_accounting_counts_hardlinked_layer_payload_once() {
        let directory = tempdir().unwrap();
        let layer = directory.path().join("layer");
        fs::write(&layer, b"immutable-layer").unwrap();
        fs::hard_link(&layer, directory.path().join("reused-layer")).unwrap();

        assert_eq!(directory_bytes(directory.path()).unwrap(), 15);
    }

    #[test]
    fn system_runner_truncates_verbose_diagnostics_without_killing_the_process() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n")
                .unwrap();
            for _ in 0..1024 {
                if stream.write_all(&[b'x'; 1024]).is_err() {
                    break;
                }
                thread::sleep(Duration::from_millis(1));
            }
        });
        let output = SystemProcessRunner
            .run(
                Program::Curl,
                &["--silent".to_owned(), format!("http://{address}")],
                Duration::from_secs(5),
            )
            .unwrap();
        assert!(output.success);
        assert_eq!(output.stdout.len(), DIAGNOSTIC_LIMIT as usize);
        assert!(output.stdout.starts_with(DIAGNOSTIC_TRUNCATED));
        assert!(output.stdout.ends_with(&[b'x'; 1024]));
        server.join().unwrap();
    }

    #[test]
    fn system_runner_honors_a_small_diagnostic_ring_without_failing_the_process() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n")
                .unwrap();
            for _ in 0..1024 {
                if stream.write_all(&[b'x'; 1024]).is_err() {
                    break;
                }
                thread::sleep(Duration::from_millis(1));
            }
        });
        let directory = tempdir().unwrap();
        let output = SystemProcessRunner
            .run_bounded_directory_with_output_limit(
                Program::Curl,
                &["--silent".to_owned(), format!("http://{address}")],
                Duration::from_secs(5),
                directory.path(),
                1024 * 1024,
                16,
            )
            .unwrap();
        assert!(output.success);
        assert_eq!(output.stdout.len(), 16);
        assert_eq!(output.stdout, DIAGNOSTIC_TRUNCATED[..16]);
        server.join().unwrap();
    }

    #[test]
    fn system_runner_rejects_work_that_would_violate_the_filesystem_reserve() {
        let directory = tempdir().unwrap();
        let result = SystemProcessRunner.run_with_disk_reserve_cancellable(
            Program::Curl,
            &["--version".to_owned()],
            Duration::from_secs(5),
            ProcessDiskReserve::new(directory.path(), u64::MAX),
            &|| false,
        );

        assert!(matches!(result, Err(ProcessError::StorageLimit)));
    }

    #[test]
    fn system_runner_streams_a_large_artifact_to_a_bounded_preopened_file() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 4096];
            let mut received = 0;
            while received < request.len()
                && !request[..received]
                    .windows(4)
                    .any(|window| window == b"\r\n\r\n")
            {
                let count = stream.read(&mut request[received..]).unwrap();
                assert!(count > 0);
                received += count;
            }
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n")
                .unwrap();
            stream.write_all(&[b'x'; 1024 * 1024]).unwrap();
        });
        let mut sink = tempfile().unwrap();

        let output = SystemProcessRunner
            .run_to_file(
                Program::Curl,
                &["--silent".to_owned(), format!("http://{address}")],
                Duration::from_secs(5),
                &mut sink,
                1024 * 1024,
            )
            .unwrap();

        assert!(output.success);
        assert!(output.stdout.is_empty());
        assert_eq!(sink.metadata().unwrap().len(), 1024 * 1024);
        sink.seek(SeekFrom::Start(0)).unwrap();
        let mut sample = [0_u8; 16];
        sink.read_exact(&mut sample).unwrap();
        assert_eq!(sample, [b'x'; 16]);
        server.join().unwrap();
    }

    #[test]
    fn curl_enforces_the_download_body_limit() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n")
                .unwrap();
            let _ = stream.write_all(&[b'x'; 1024]);
        });
        let directory = tempdir().unwrap();
        let destination = directory.path().join("artifact");
        let output = SystemProcessRunner
            .run(
                Program::Curl,
                &[
                    "--silent".to_owned(),
                    "--show-error".to_owned(),
                    "--max-filesize".to_owned(),
                    "16".to_owned(),
                    "--output".to_owned(),
                    destination.display().to_string(),
                    format!("http://{address}"),
                ],
                Duration::from_secs(5),
            )
            .unwrap();

        assert!(!output.success);
        if destination.exists() {
            assert!(fs::metadata(destination).unwrap().len() <= 16);
        }
        server.join().unwrap();
    }
}
