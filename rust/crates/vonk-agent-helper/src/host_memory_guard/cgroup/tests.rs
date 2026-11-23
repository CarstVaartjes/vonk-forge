#![cfg(test)]
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
