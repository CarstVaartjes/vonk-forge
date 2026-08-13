use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{Read, Seek, SeekFrom},
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
            Self::Oras => "/usr/bin/oras",
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
}

pub struct SystemProcessRunner;

impl ProcessRunner for SystemProcessRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        run_process(program, arguments, timeout, None, OUTPUT_LIMIT)
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
        )
    }
}

fn run_process(
    program: Program,
    arguments: &[String],
    timeout: Duration,
    storage_limit: Option<(&Path, u64)>,
    output_limit: u64,
) -> Result<ProcessOutput, ProcessError> {
    let mut stdout = tempfile()?;
    let mut stderr = tempfile()?;
    let environment = subprocess_environment(program, rustix::process::geteuid().as_raw());
    let mut child = Command::new(program.path())
        .args(arguments)
        .stdin(Stdio::null())
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

fn subprocess_environment(program: Program, effective_uid: u32) -> BTreeMap<&'static str, String> {
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
    } else {
        environment.insert("XDG_RUNTIME_DIR", "/run/vonk-forge-agent".to_owned());
    }
    environment
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
        present_during_scan, subprocess_environment,
    };
    use std::{fs, io::Write, net::TcpListener, os::unix::fs::symlink, thread, time::Duration};
    use tempfile::tempdir;

    #[test]
    fn podman_uses_the_effective_users_systemd_bus() {
        let environment = subprocess_environment(Program::Podman, 128);

        assert_eq!(environment["XDG_RUNTIME_DIR"], "/run/user/128");
        assert_eq!(
            environment["DBUS_SESSION_BUS_ADDRESS"],
            "unix:path=/run/user/128/bus"
        );
    }

    #[test]
    fn non_podman_tools_keep_the_private_agent_runtime() {
        let environment = subprocess_environment(Program::Curl, 128);

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
