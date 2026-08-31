use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{Read, Seek, SeekFrom, Write},
    path::Path,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

use tempfile::tempfile;
use thiserror::Error;

const OUTPUT_LIMIT: u64 = 64 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Program {
    Curl,
    Docker,
    NvidiaCtk,
    NvidiaSmi,
    Oras,
    Podman,
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
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessOutput {
    pub success: bool,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
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
}

pub trait ProcessRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError>;

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
        let diagnostic_limit = maximum_output_bytes.min(OUTPUT_LIMIT);
        let output =
            self.run_bounded_directory(program, arguments, timeout, directory, maximum_bytes)?;
        if output_bytes(&output) > diagnostic_limit {
            return Err(ProcessError::OutputLimit);
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
}

pub struct SystemProcessRunner;

impl ProcessRunner for SystemProcessRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(program, arguments, timeout, None, OUTPUT_LIMIT, None)
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
            Some((directory, maximum_bytes)),
            OUTPUT_LIMIT,
            None,
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
            Some((directory, maximum_bytes)),
            maximum_output_bytes.min(OUTPUT_LIMIT),
            None,
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
        run_process(program, arguments, timeout, None, OUTPUT_LIMIT, Some(input))
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
            Some((directory, maximum_bytes)),
            OUTPUT_LIMIT,
            Some(input),
        )
    }
}

fn run_process(
    program: Program,
    arguments: &[String],
    timeout: Duration,
    storage_limit: Option<(&Path, u64)>,
    output_limit: u64,
    input: Option<&File>,
) -> Result<ProcessOutput, ProcessError> {
    let mut stdout = tempfile()?;
    let mut stderr = tempfile()?;
    let environment =
        subprocess_environment(program, rustix::process::geteuid().as_raw(), arguments);
    let stdin = match input {
        Some(input) => Stdio::from(input.try_clone()?),
        None => Stdio::null(),
    };
    let mut child = Command::new(program.path())
        .args(arguments)
        .stdin(stdin)
        .stdout(Stdio::from(stdout.try_clone()?))
        .stderr(Stdio::from(stderr.try_clone()?))
        .env_clear()
        .envs(environment)
        .spawn()?;
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
        let stdout_bytes = stdout.metadata()?.len();
        let stderr_bytes = stderr.metadata()?.len();
        if stdout_bytes > output_limit
            || stderr_bytes > output_limit
            || stdout_bytes.saturating_add(stderr_bytes) > output_limit
        {
            child.kill()?;
            child.wait()?;
            return Err(ProcessError::OutputLimit);
        }
        if let Some((directory, maximum_bytes)) = storage_limit {
            match directory_bytes(directory) {
                Ok(bytes) if bytes > maximum_bytes => {
                    child.kill()?;
                    child.wait()?;
                    return Err(ProcessError::StorageLimit);
                }
                Err(error) => {
                    child.kill()?;
                    child.wait()?;
                    return Err(ProcessError::Io(error));
                }
                Ok(_) => {}
            }
        }
        thread::sleep(Duration::from_millis(25));
    };
    Ok(ProcessOutput {
        success: status.success(),
        stdout: bounded_read(&mut stdout, output_limit)?,
        stderr: bounded_read(&mut stderr, output_limit)?,
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
    let mut stderr = tempfile()?;
    let environment =
        subprocess_environment(program, rustix::process::geteuid().as_raw(), arguments);
    let mut child = Command::new(program.path())
        .args(arguments)
        .stdin(Stdio::null())
        .stdout(Stdio::from(sink.try_clone()?))
        .stderr(Stdio::from(stderr.try_clone()?))
        .env_clear()
        .envs(environment)
        .spawn()?;
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
        if stderr.metadata()?.len() > OUTPUT_LIMIT {
            child.kill()?;
            child.wait()?;
            return Err(ProcessError::OutputLimit);
        }
        thread::sleep(Duration::from_millis(25));
    };
    sink.flush()?;
    if sink.metadata()?.len() > maximum_bytes {
        return Err(ProcessError::StorageLimit);
    }
    Ok(ProcessOutput {
        success: status.success(),
        stdout: Vec::new(),
        stderr: bounded_read(&mut stderr, OUTPUT_LIMIT)?,
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
        ("XDG_DATA_HOME", "/var/lib/vonk-forge-agent".to_owned()),
        (
            "CONTAINERS_STORAGE_CONF",
            "/etc/vonk-forge-agent/containers-storage.conf".to_owned(),
        ),
    ]);
    if program == Program::Podman {
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

fn bounded_read(file: &mut File, output_limit: u64) -> Result<Vec<u8>, ProcessError> {
    if file.metadata()?.len() > output_limit {
        return Err(ProcessError::OutputLimit);
    }
    file.seek(SeekFrom::Start(0))?;
    let mut value = Vec::new();
    file.take(output_limit.saturating_add(1))
        .read_to_end(&mut value)?;
    if value.len() as u64 > output_limit {
        return Err(ProcessError::OutputLimit);
    }
    Ok(value)
}

fn output_bytes(output: &ProcessOutput) -> u64 {
    (output.stdout.len() as u64).saturating_add(output.stderr.len() as u64)
}

#[cfg(test)]
mod tests {
    use super::{
        ProcessError, ProcessRunner, Program, SystemProcessRunner, directory_bytes,
        podman_image_tmpdir, present_during_scan, subprocess_environment,
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
    fn podman_uses_the_effective_users_systemd_bus() {
        let temporary = tempdir().unwrap();
        let root = temporary.path().join("podman-storage");
        let arguments = vec!["--root".to_owned(), root.display().to_string()];
        let environment = subprocess_environment(Program::Podman, 128, &arguments);

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
    fn system_runner_kills_a_process_while_output_exceeds_the_live_cap() {
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
        let result = SystemProcessRunner.run(
            Program::Curl,
            &["--silent".to_owned(), format!("http://{address}")],
            Duration::from_secs(5),
        );
        assert!(matches!(result, Err(ProcessError::OutputLimit)));
        server.join().unwrap();
    }

    #[test]
    fn system_runner_applies_a_declared_output_limit_without_weakening_storage_limit() {
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
        let result = SystemProcessRunner.run_bounded_directory_with_output_limit(
            Program::Curl,
            &["--silent".to_owned(), format!("http://{address}")],
            Duration::from_secs(5),
            directory.path(),
            1024 * 1024,
            16,
        );
        assert!(matches!(result, Err(ProcessError::OutputLimit)));
        server.join().unwrap();
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
