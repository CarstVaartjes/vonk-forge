use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

use crate::package_command;
use crate::package_rollback::Store;

const COMMAND_TIMEOUT: Duration = Duration::from_secs(60);
const PAUSE: Duration = Duration::from_millis(100);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    File,
    Dir,
    Symlink,
    Other,
}

/// What the hooks need to know about a path.
#[derive(Clone, Copy, Debug)]
pub struct Entry {
    pub kind: Kind,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
}

/// Every effect and observation a hook has. Paths and programs are absolute.
pub trait Host {
    /// The entry at `path`, a symlink itself reported as one.
    fn lstat(&self, path: &str) -> Option<Entry>;
    /// The entry `path` resolves to.
    fn stat(&self, path: &str) -> Option<Entry>;
    fn read(&self, path: &str) -> Result<String, String>;
    /// Create the directory when absent, then set its owner and mode.
    fn make_dir(&mut self, path: &str, uid: u32, gid: u32, mode: u32) -> Result<(), String>;
    /// Atomically replace `path` with a new file.
    fn write_file(
        &mut self,
        path: &str,
        contents: &str,
        uid: u32,
        gid: u32,
        mode: u32,
    ) -> Result<(), String>;
    /// A file, a symlink or an empty directory; never follows a symlink.
    fn remove(&mut self, path: &str) -> Result<(), String>;
    /// Stdout of a command that succeeded.
    fn run(&mut self, program: &str, arguments: &[&str]) -> Result<String, String>;
    fn random_hex(&mut self) -> String;
    fn systemd_live(&self) -> bool;
    fn parent_pid(&self) -> u32;
    /// The start time of a live process; `None` once it is gone.
    fn process_start(&self, pid: u32) -> Option<u64>;
    fn exe_sha256(&self, pid: u32) -> Result<String, String>;
    /// The ingress digest of the helper being activated, if one is armed for
    /// the installed package version.
    fn armed_helper_sha256(&self, installed_version: &str) -> Option<String>;
    fn pause(&mut self);
}

pub struct SystemHost;

fn entry(metadata: &fs::Metadata) -> Entry {
    let kind = if metadata.file_type().is_symlink() {
        Kind::Symlink
    } else if metadata.is_dir() {
        Kind::Dir
    } else if metadata.is_file() {
        Kind::File
    } else {
        Kind::Other
    };
    Entry {
        kind,
        uid: metadata.uid(),
        gid: metadata.gid(),
        mode: metadata.mode() & 0o7777,
    }
}

impl Host for SystemHost {
    fn lstat(&self, path: &str) -> Option<Entry> {
        fs::symlink_metadata(path).as_ref().map(entry).ok()
    }
    fn stat(&self, path: &str) -> Option<Entry> {
        fs::metadata(path).as_ref().map(entry).ok()
    }
    fn read(&self, path: &str) -> Result<String, String> {
        fs::read_to_string(path).map_err(|error| format!("{path}: {error}"))
    }
    fn make_dir(&mut self, path: &str, uid: u32, gid: u32, mode: u32) -> Result<(), String> {
        let failed = |error: std::io::Error| format!("{path}: {error}");
        if self.stat(path).is_none() {
            fs::create_dir(path).map_err(failed)?;
        }
        std::os::unix::fs::chown(path, Some(uid), Some(gid)).map_err(failed)?;
        fs::set_permissions(path, fs::Permissions::from_mode(mode)).map_err(failed)
    }
    fn write_file(
        &mut self,
        path: &str,
        contents: &str,
        uid: u32,
        gid: u32,
        mode: u32,
    ) -> Result<(), String> {
        let failed = |error: std::io::Error| format!("{path}: {error}");
        let parent = Path::new(path).parent().ok_or("file has no parent")?;
        let name = Path::new(path).file_name().ok_or("file has no name")?;
        let temporary = parent.join(format!(
            ".{}.{}",
            name.to_string_lossy(),
            uuid::Uuid::new_v4()
        ));
        let result = (|| {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&temporary)?;
            file.write_all(contents.as_bytes())?;
            file.sync_all()?;
            std::os::unix::fs::chown(&temporary, Some(uid), Some(gid))?;
            fs::set_permissions(&temporary, fs::Permissions::from_mode(mode))?;
            fs::rename(&temporary, path)
        })();
        if result.is_err() {
            // Best effort: a leftover is collected by the stores' `.machine-evidence.*` rule.
            if let Err(error) = fs::remove_file(&temporary) {
                eprintln!("vonk-forge-agent: {}: {error}", temporary.display());
            }
        }
        result.map_err(failed)
    }
    fn remove(&mut self, path: &str) -> Result<(), String> {
        let metadata = fs::symlink_metadata(path).map_err(|error| format!("{path}: {error}"))?;
        let removed = if metadata.is_dir() {
            fs::remove_dir(path)
        } else {
            fs::remove_file(path)
        };
        removed.map_err(|error| format!("{path}: {error}"))
    }
    fn run(&mut self, program: &str, arguments: &[&str]) -> Result<String, String> {
        let mut command = Command::new(program);
        command
            .args(arguments)
            .current_dir("/")
            .stdin(Stdio::null());
        let output = package_command::run(&mut command, COMMAND_TIMEOUT)?;
        if output.timed_out || !output.status.success() {
            return Err(format!(
                "{program} {}: {}",
                arguments.first().copied().unwrap_or_default(),
                String::from_utf8_lossy(&output.diagnostic()).trim()
            ));
        }
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
    }
    fn random_hex(&mut self) -> String {
        format!(
            "{}{}",
            uuid::Uuid::new_v4().simple(),
            uuid::Uuid::new_v4().simple()
        )
    }
    fn systemd_live(&self) -> bool {
        std::env::var_os("SYSTEMD_OFFLINE").is_none_or(|value| value != "1")
            && Path::new("/run/systemd/system").is_dir()
    }
    fn parent_pid(&self) -> u32 {
        rustix::process::getppid().map_or(0, |pid| pid.as_raw_nonzero().get() as u32)
    }
    fn process_start(&self, pid: u32) -> Option<u64> {
        let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
        // Field 22; the command name (field 2) may hold spaces, so count after it.
        let after_name = stat.rsplit_once(')')?.1;
        after_name.split_whitespace().nth(19)?.parse().ok()
    }
    fn exe_sha256(&self, pid: u32) -> Result<String, String> {
        crate::package_rollback::digest_process(pid)
    }
    fn armed_helper_sha256(&self, installed_version: &str) -> Option<String> {
        Store::system().armed_helper_sha256(installed_version)
    }
    fn pause(&mut self) {
        std::thread::sleep(PAUSE);
    }
}
