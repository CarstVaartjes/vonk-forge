use std::{
    collections::{BTreeMap, HashSet},
    fs::{self, File},
    io::{Read, Seek, SeekFrom, Write},
    os::unix::{fs::MetadataExt, process::CommandExt},
    path::Path,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

use thiserror::Error;

mod liveness;
use liveness::Liveness;
pub use liveness::{ActivityScope, ActivitySink, note_activity, report_activity_to};

const DIAGNOSTIC_LIMIT: u64 = 64 * 1024;
const DIAGNOSTIC_TRUNCATED: &[u8] = b"[earlier diagnostic output truncated]\n";
const DISK_POLL_INTERVAL: Duration = Duration::from_secs(1);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Program {
    Curl,
    Getent,
    Ip,
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
            Self::Getent => "/usr/bin/getent",
            Self::Ip => "/usr/sbin/ip",
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
    /// Actual child exit status; None means no numeric status was observed.
    pub exit_code: Option<i32>,
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

/// Every `timeout` below is a stall window: it restarts whenever the child
/// writes output or grows its file or directory, so only a child that stops
/// progressing for a whole window is ended.
pub trait ProcessRunner {
    /// Independent ownership for blocking build work. Test runners may keep
    /// their deterministic in-thread implementation.
    fn worker(&self) -> Option<std::sync::Arc<dyn ProcessRunner + Send + Sync>> {
        None
    }

    fn run_to_file_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        sink: &mut File,
        maximum_bytes: u64,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        if cancelled() {
            return Err(ProcessError::Cancelled);
        }
        let output = self.run_to_file(program, arguments, timeout, sink, maximum_bytes)?;
        if cancelled() {
            return Err(ProcessError::Cancelled);
        }
        Ok(output)
    }

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
            exit_code: output.exit_code,
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
    fn worker(&self) -> Option<std::sync::Arc<dyn ProcessRunner + Send + Sync>> {
        Some(std::sync::Arc::new(SystemProcessRunner))
    }

    fn run_to_file_cancellable(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
        sink: &mut File,
        maximum_bytes: u64,
        cancelled: &dyn Fn() -> bool,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process_to_file(
            program,
            arguments,
            timeout,
            sink,
            maximum_bytes,
            Some(cancelled),
        )
    }

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
        run_process_to_file(program, arguments, timeout, sink, maximum_bytes, None)
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

#[expect(
    clippy::needless_pass_by_value,
    reason = "the call takes ownership at this boundary; the caller's value is consumed"
)]
fn run_process(
    program: Program,
    arguments: &[String],
    timeout: Duration,
    options: ProcessRunOptions<'_>,
) -> Result<ProcessOutput, ProcessError> {
    if options.cancellation.is_some_and(|cancelled| cancelled()) {
        return Err(ProcessError::Cancelled);
    }
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
    let liveness = Liveness::new(timeout);
    let mut child = Command::new(program.path())
        .args(arguments)
        .process_group(0)
        .stdin(stdin)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_clear()
        .envs(environment)
        .spawn()?;
    let pipe_stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let _pipe_owner = PipeOwner(pipe_stop.clone());
    let Some(stdout_pipe) = child.stdout.take() else {
        terminate_process(&mut child, program, arguments)?;
        return Err(std::io::Error::other("subprocess stdout is unavailable").into());
    };
    let stdout = match capture_diagnostics(
        stdout_pipe,
        options.diagnostic_limit,
        liveness.clone(),
        pipe_stop.clone(),
    ) {
        Ok(capture) => capture,
        Err(error) => {
            terminate_process(&mut child, program, arguments)?;
            return Err(error.into());
        }
    };
    let Some(stderr_pipe) = child.stderr.take() else {
        terminate_process(&mut child, program, arguments)?;
        return Err(std::io::Error::other("subprocess stderr is unavailable").into());
    };
    let stderr = match capture_diagnostics(
        stderr_pipe,
        options.diagnostic_limit,
        liveness.clone(),
        pipe_stop.clone(),
    ) {
        Ok(capture) => capture,
        Err(error) => {
            terminate_process(&mut child, program, arguments)?;
            return Err(error.into());
        }
    };
    let started = Instant::now();
    let mut last_storage_check = started;
    let mut last_disk_check = started;
    let mut stored_bytes = None;
    let mut lowest_available = None;
    let mut reported = 0;
    let status = loop {
        liveness.report(&mut reported);
        let observed = match child.try_wait() {
            Ok(value) => value,
            Err(error) => {
                terminate_process(&mut child, program, arguments)?;
                return Err(error.into());
            }
        };
        if let Some(status) = observed {
            break status;
        }
        if options.cancellation.is_some_and(|cancelled| cancelled()) {
            terminate_process(&mut child, program, arguments)?;
            return Err(ProcessError::Cancelled);
        }
        if liveness.expired() {
            terminate_process(&mut child, program, arguments)?;
            return Err(ProcessError::Timeout);
        }
        if let Some((directory, maximum_bytes)) = options.storage_limit
            && last_storage_check.elapsed() >= Duration::from_millis(25)
        {
            match directory_bytes_until(directory, liveness.deadline(), options.cancellation) {
                Ok(bytes) if bytes > maximum_bytes => {
                    terminate_process(&mut child, program, arguments)?;
                    return Err(ProcessError::StorageLimit);
                }
                Err(error) => {
                    terminate_process(&mut child, program, arguments)?;
                    return Err(ProcessError::Io(error));
                }
                Ok(bytes) => {
                    liveness.advance(bytes.saturating_sub(stored_bytes.unwrap_or(bytes)));
                    stored_bytes = Some(stored_bytes.unwrap_or(bytes).max(bytes));
                }
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
                // A new low of free space is the child's (e.g. a silent
                // compile's) writes; space freed elsewhere never counts.
                Ok(available) => {
                    let lowest = lowest_available.unwrap_or(available);
                    liveness.advance(lowest.saturating_sub(available));
                    lowest_available = Some(lowest.min(available));
                }
            }
            last_disk_check = Instant::now();
        }
        thread::sleep(Duration::from_millis(25));
    };
    while !stdout.is_finished() || !stderr.is_finished() {
        liveness.report(&mut reported);
        if liveness.expired() || options.cancellation.is_some_and(|cancelled| cancelled()) {
            pipe_stop.store(true, std::sync::atomic::Ordering::Release);
            terminate_process(&mut child, program, arguments)?;
            return Err(ProcessError::Timeout);
        }
        thread::sleep(
            Duration::from_millis(5).min(
                liveness
                    .deadline()
                    .saturating_duration_since(Instant::now()),
            ),
        );
    }
    liveness.report(&mut reported);
    let captured = join_diagnostics(stdout)
        .and_then(|stdout| join_diagnostics(stderr).map(|stderr| (stdout, stderr)));
    if captured.is_err() {
        terminate_process(&mut child, program, arguments)?;
    }
    let (stdout, stderr) = captured?;
    // Even a descendant that redirected every pipe still belongs to the
    // command. systemd services have their separate exact cgroup owner.
    if let Some(group) = i32::try_from(child.id())
        .ok()
        .and_then(rustix::process::Pid::from_raw)
    {
        #[expect(
            clippy::let_underscore_must_use,
            reason = "best-effort stop of a process that may already have ended"
        )]
        let _ = rustix::process::kill_process_group(group, rustix::process::Signal::KILL);
    }
    if options.cancellation.is_some_and(|cancelled| cancelled()) {
        return Err(ProcessError::Cancelled);
    }
    Ok(ProcessOutput {
        exit_code: status.code(),
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

struct PipeOwner(std::sync::Arc<std::sync::atomic::AtomicBool>);
impl Drop for PipeOwner {
    fn drop(&mut self) {
        self.0.store(true, std::sync::atomic::Ordering::Release);
    }
}

fn capture_diagnostics<R: Read + std::os::fd::AsFd + Send + 'static>(
    mut reader: R,
    limit: u64,
    liveness: std::sync::Arc<Liveness>,
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
) -> Result<thread::JoinHandle<Result<Vec<u8>, std::io::Error>>, std::io::Error> {
    let flags = rustix::fs::fcntl_getfl(&reader)?;
    rustix::fs::fcntl_setfl(&reader, flags | rustix::fs::OFlags::NONBLOCK)?;
    let capacity = usize::try_from(limit).unwrap_or(usize::MAX);
    thread::Builder::new()
        .name("vonk-process-diagnostics".to_owned())
        .spawn(move || {
            let mut ring = DiagnosticRing::new(capacity);
            let mut chunk = [0_u8; 8192];
            loop {
                if stop.load(std::sync::atomic::Ordering::Acquire) || liveness.expired() {
                    return Err(std::io::ErrorKind::TimedOut.into());
                }
                let count = match reader.read(&mut chunk) {
                    Ok(count) => count,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(
                            Duration::from_millis(5).min(
                                liveness
                                    .deadline()
                                    .saturating_duration_since(Instant::now()),
                            ),
                        );
                        continue;
                    }
                    Err(error) => return Err(error),
                };
                if count == 0 {
                    return Ok(ring.finish());
                }
                liveness.advance(count as u64);
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
    let deadline = Instant::now() + Duration::from_secs(2);
    // Local ownership never depends on the service-manager reply.
    if let Some(group) = i32::try_from(child.id())
        .ok()
        .and_then(rustix::process::Pid::from_raw)
    {
        #[expect(
            clippy::let_underscore_must_use,
            reason = "best-effort stop of a process that may already have ended"
        )]
        let _ = rustix::process::kill_process_group(group, rustix::process::Signal::KILL);
    }
    #[expect(
        clippy::let_underscore_must_use,
        reason = "best-effort stop of a process that may already have ended"
    )]
    let _ = child.kill();
    if let Some(unit) = transient_user_service_unit(program, arguments) {
        let environment =
            subprocess_environment(Program::Systemctl, rustix::process::geteuid().as_raw(), &[]);
        // Nonblocking submission: systemd owns stopping the entire cgroup.
        let stop = Command::new(Program::Systemctl.path())
            .args([
                "--user",
                "--no-block",
                vonk_agent_protocol::generated::ProfileChildPhase::Stop.as_str(),
                unit,
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .env_clear()
            .envs(environment)
            .spawn();
        if let Ok(mut stop) = stop {
            let observation_deadline = deadline.min(Instant::now() + Duration::from_millis(500));
            if reap_until(&mut stop, observation_deadline).is_err() {
                #[expect(
                    clippy::let_underscore_must_use,
                    reason = "best-effort stop of a process that may already have ended"
                )]
                let _ = stop.kill();
                #[expect(
                    clippy::let_underscore_must_use,
                    reason = "best-effort stop of a process that may already have ended"
                )]
                let _ = reap_until(&mut stop, deadline);
            }
        }
    }
    reap_until(child, deadline)
}

fn reap_until(child: &mut Child, deadline: Instant) -> Result<(), std::io::Error> {
    loop {
        if child.try_wait()?.is_some() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            #[expect(
                clippy::let_underscore_must_use,
                reason = "best-effort stop of a process that may already have ended"
            )]
            let _ = child.kill();
            return Err(std::io::ErrorKind::TimedOut.into());
        }
        thread::sleep(
            Duration::from_millis(5).min(deadline.saturating_duration_since(Instant::now())),
        );
    }
}

fn transient_user_service_unit(program: Program, arguments: &[String]) -> Option<&str> {
    if program != Program::SystemdRun || !arguments.iter().any(|value| value == "--user") {
        return None;
    }
    arguments.iter().find_map(|value| {
        let name = value.strip_prefix("--unit=")?;
        let id = name
            .strip_prefix("vonk-recipe-build-")
            .or_else(|| name.strip_prefix("vonk-runtime-adapter-"))?;
        // Egress uses the recipe unit's -e sibling.
        let id = id.strip_prefix("preflight-").unwrap_or(id);
        let id = id.strip_suffix("-e").unwrap_or(id);
        uuid::Uuid::parse_str(id).ok().map(|_| name)
    })
}

fn run_process_to_file(
    program: Program,
    arguments: &[String],
    timeout: Duration,
    sink: &mut File,
    maximum_bytes: u64,
    cancelled: Option<&dyn Fn() -> bool>,
) -> Result<ProcessOutput, ProcessError> {
    if cancelled.is_some_and(|cancelled| cancelled()) {
        return Err(ProcessError::Cancelled);
    }
    sink.set_len(0)?;
    sink.seek(SeekFrom::Start(0))?;
    let environment =
        subprocess_environment(program, rustix::process::geteuid().as_raw(), arguments);
    let liveness = Liveness::new(timeout);
    let mut child = Command::new(program.path())
        .args(arguments)
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::from(sink.try_clone()?))
        .stderr(Stdio::piped())
        .env_clear()
        .envs(environment)
        .spawn()?;
    let pipe_stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let _pipe_owner = PipeOwner(pipe_stop.clone());
    let Some(stderr_pipe) = child.stderr.take() else {
        terminate_process(&mut child, program, arguments)?;
        return Err(std::io::Error::other("subprocess stderr is unavailable").into());
    };
    let stderr = match capture_diagnostics(
        stderr_pipe,
        DIAGNOSTIC_LIMIT,
        liveness.clone(),
        pipe_stop.clone(),
    ) {
        Ok(capture) => capture,
        Err(error) => {
            terminate_process(&mut child, program, arguments)?;
            return Err(error.into());
        }
    };
    let mut sink_bytes = 0;
    let mut reported = 0;
    let status = loop {
        liveness.report(&mut reported);
        if cancelled.is_some_and(|cancelled| cancelled()) {
            terminate_process(&mut child, program, arguments)?;
            return Err(ProcessError::Cancelled);
        }
        let observed = match child.try_wait() {
            Ok(value) => value,
            Err(error) => {
                terminate_process(&mut child, program, arguments)?;
                return Err(error.into());
            }
        };
        if let Some(status) = observed {
            break status;
        }
        if liveness.expired() {
            terminate_process(&mut child, program, arguments)?;
            return Err(ProcessError::Timeout);
        }
        let length = match sink.metadata() {
            Ok(metadata) => metadata.len(),
            Err(error) => {
                terminate_process(&mut child, program, arguments)?;
                return Err(error.into());
            }
        };
        liveness.advance(length.saturating_sub(sink_bytes));
        sink_bytes = sink_bytes.max(length);
        if length > maximum_bytes {
            terminate_process(&mut child, program, arguments)?;
            return Err(ProcessError::StorageLimit);
        }
        thread::sleep(Duration::from_millis(25));
    };
    while !stderr.is_finished() {
        liveness.report(&mut reported);
        if liveness.expired() || cancelled.is_some_and(|cancelled| cancelled()) {
            pipe_stop.store(true, std::sync::atomic::Ordering::Release);
            terminate_process(&mut child, program, arguments)?;
            return Err(ProcessError::Timeout);
        }
        thread::sleep(
            Duration::from_millis(5).min(
                liveness
                    .deadline()
                    .saturating_duration_since(Instant::now()),
            ),
        );
    }
    liveness.report(&mut reported);
    let captured = join_diagnostics(stderr);
    if captured.is_err() {
        terminate_process(&mut child, program, arguments)?;
    }
    let stderr = captured?;
    if let Some(group) = i32::try_from(child.id())
        .ok()
        .and_then(rustix::process::Pid::from_raw)
    {
        #[expect(
            clippy::let_underscore_must_use,
            reason = "best-effort stop of a process that may already have ended"
        )]
        let _ = rustix::process::kill_process_group(group, rustix::process::Signal::KILL);
    }
    sink.flush()?;
    if sink.metadata()?.len() > maximum_bytes {
        return Err(ProcessError::StorageLimit);
    }
    Ok(ProcessOutput {
        exit_code: status.code(),
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
    directory_bytes_until(path, Instant::now() + Duration::from_secs(2), None)
}

fn directory_bytes_until(
    path: &Path,
    deadline: Instant,
    cancelled: Option<&dyn Fn() -> bool>,
) -> Result<u64, std::io::Error> {
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
        if Instant::now() >= deadline || cancelled.is_some_and(|value| value()) {
            return Err(std::io::ErrorKind::TimedOut.into());
        }
        let Some(entries) = present_during_scan(fs::read_dir(directory))? else {
            continue;
        };
        for entry in entries {
            if Instant::now() >= deadline || cancelled.is_some_and(|value| value()) {
                return Err(std::io::ErrorKind::TimedOut.into());
            }
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
mod tests;

#[cfg(test)]
mod settlement_tests;
