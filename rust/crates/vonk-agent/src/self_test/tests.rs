use super::*;
use std::io;
use std::os::unix::fs::{PermissionsExt, symlink};

#[test]
fn generated_directory_miss_repairs_and_unsafe_neighbor_is_retained() {
    let root = tempfile::tempdir().unwrap();
    let data = root.path().join("data");
    let runtime = root.path().join("runtime");
    verify_runtime_directories(&data, &runtime).unwrap();
    fs::remove_dir(&runtime).unwrap();
    let sentinel = root.path().join("sentinel");
    fs::write(&sentinel, b"keep").unwrap();
    symlink(&sentinel, &runtime).unwrap();
    verify_runtime_directories(&data, &runtime).unwrap();
    assert_eq!(fs::read(&sentinel).unwrap(), b"keep");
    verify_runtime_directories(&data, &runtime).unwrap();
    assert!(runtime.is_dir());
}

#[test]
fn private_directory_observation_repairs_owned_permissions_without_waiting() {
    let temporary = tempfile::tempdir().unwrap();
    let path = temporary.path().join("runtime");
    fs::create_dir(&path).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    let mut waits = 0;
    let result = super::observe_private_directory(
        || super::inspect_private_directory(&path, "runtime"),
        || waits += 1,
    );
    assert!(result.is_ok() && waits == 0);
    assert_eq!(fs::metadata(&path).unwrap().mode() & 0o777, 0o700);
    assert!(super::verify_private_directory(&path, "runtime").is_ok());
}

#[test]
fn unavailable_private_directory_has_a_finite_budget_and_no_fresh_gate() {
    for unreadable in [true, false] {
        let mut reads = 0;
        let mut waits = 0;
        let result = super::observe_private_directory(
            || {
                reads += 1;
                if unreadable {
                    Err(SelfTestError::Io(io::Error::from(
                        io::ErrorKind::Interrupted,
                    )))
                } else {
                    Err(SelfTestError::UnsafePath("runtime"))
                }
            },
            || waits += 1,
        );
        assert!(result.is_err());
        assert_eq!((reads, waits), (3, 2));
        assert!(super::observe_private_directory(|| Ok(()), || panic!("no wait")).is_ok());
    }
}
