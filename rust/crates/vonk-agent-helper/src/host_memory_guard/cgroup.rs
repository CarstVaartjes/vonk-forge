//! Kernel-only termination of one managed container. Under GB10 memory thrash
//! dockerd stops answering (spark-3542, 2026-10-13: every `docker` call missed
//! its deadline for 30+ minutes) while the kernel still honours `cgroup.kill`,
//! so the safety action never depends on the daemon it protects the host from.
use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
};

pub const ROOT: &str = "/sys/fs/cgroup";

/// Docker names a container's cgroup after its full id:
/// `system.slice/docker-<id>.scope` (systemd driver) or `docker/<id>`
/// (cgroupfs driver). The id is exactly 64 lowercase hex characters, so no
/// candidate can leave `root`, and no other container's cgroup can match.
pub fn directory(root: &Path, id: &str) -> Option<PathBuf> {
    if !super::valid_id(id) {
        return None;
    }
    [
        root.join("system.slice").join(format!("docker-{id}.scope")),
        root.join("docker").join(id),
    ]
    .into_iter()
    .find(|path| path.is_dir())
}

#[derive(Debug, PartialEq, Eq)]
pub enum Outcome {
    /// `cgroup.kill` accepted: the kernel SIGKILLs every member, forks included.
    Killed,
    /// Fallback without `cgroup.kill`: each listed member was sent SIGKILL.
    Signalled(usize),
    /// The recorded cgroup no longer exists: nothing is left to stop.
    Gone,
}

/// SIGKILL every process in `directory`, a cgroup recorded by [`directory`].
/// Pure kernel I/O: no daemon, lock, or child process is involved, so nothing
/// here can wait on dockerd.
pub fn kill(directory: &Path, signal: impl Fn(i32) -> io::Result<()>) -> io::Result<Outcome> {
    let written = OpenOptions::new()
        .write(true)
        .open(directory.join("cgroup.kill"))
        .and_then(|mut file| file.write_all(b"1"));
    let Err(error) = written else {
        return Ok(Outcome::Killed);
    };
    // Kernels before 5.14 have no cgroup.kill. Members listed here can fork
    // meanwhile; the next guard cycle signals any survivor again.
    let members = match fs::read_to_string(directory.join("cgroup.procs")) {
        Ok(members) => members,
        Err(missing) if missing.kind() == io::ErrorKind::NotFound => return Ok(Outcome::Gone),
        Err(listing) => {
            return Err(io::Error::other(format!(
                "{error}; cgroup.procs: {listing}"
            )));
        }
    };
    let mut signalled = 0;
    let mut failure = None;
    for pid in members
        .lines()
        .filter_map(|line| line.trim().parse::<i32>().ok())
        .filter(|pid| *pid > 1)
    {
        match signal(pid) {
            Ok(()) => signalled += 1,
            Err(error) => failure = Some(error),
        }
    }
    match failure {
        Some(error) if signalled == 0 => Err(error),
        _ => Ok(Outcome::Signalled(signalled)),
    }
}

/// SIGKILL one process; a process that already exited counts as stopped.
pub fn sigkill(pid: i32) -> io::Result<()> {
    let pid = rustix::process::Pid::from_raw(pid).ok_or_else(|| io::Error::other("pid"))?;
    match rustix::process::kill_process(pid, rustix::process::Signal::KILL) {
        Ok(()) | Err(rustix::io::Errno::SRCH) => Ok(()),
        Err(error) => Err(error.into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    fn scope(root: &Path, id: &str) -> PathBuf {
        let path = root.join("system.slice").join(format!("docker-{id}.scope"));
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[test]
    fn cgroup_kill_is_written_and_foreign_or_malformed_ids_never_resolve() {
        let root = tempfile::tempdir().unwrap();
        let id = "a".repeat(64);
        let path = scope(root.path(), &id);
        fs::write(path.join("cgroup.kill"), "").unwrap();
        assert_eq!(directory(root.path(), &id), Some(path.clone()));
        assert_eq!(
            kill(&path, |_| panic!("no per-pid fallback")).unwrap(),
            Outcome::Killed
        );
        assert_eq!(fs::read_to_string(path.join("cgroup.kill")).unwrap(), "1");
        // Another container's cgroup, traversal, and the root itself never match.
        let other = "b".repeat(64);
        for id in [other.as_str(), "..", "", "../system.slice", &"A".repeat(64)] {
            assert_eq!(directory(root.path(), id), None);
        }
        let cgroupfs = root.path().join("docker").join(&other);
        fs::create_dir_all(&cgroupfs).unwrap();
        assert_eq!(directory(root.path(), &other), Some(cgroupfs));
        fs::remove_dir_all(&path).unwrap();
        assert_eq!(kill(&path, |_| unreachable!()).unwrap(), Outcome::Gone);
    }

    #[test]
    fn without_cgroup_kill_every_listed_member_is_signalled() {
        let root = tempfile::tempdir().unwrap();
        let id = "c".repeat(64);
        let path = scope(root.path(), &id);
        fs::write(path.join("cgroup.procs"), "4242\n1\n4243\n").unwrap();
        let sent = Mutex::new(vec![]);
        let outcome = kill(&path, |pid| {
            sent.lock().unwrap().push(pid);
            Ok(())
        });
        assert_eq!(outcome.unwrap(), Outcome::Signalled(2));
        assert_eq!(*sent.lock().unwrap(), [4242, 4243]);
        assert!(kill(&path, |_| Err(io::Error::other("EPERM"))).is_err());
    }
}
