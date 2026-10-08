#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn observation_capture_rename_is_unknown_then_same_directory_reopens() {
    let root = tempdir().unwrap();
    let runs = root.path().join("runs");
    fs::create_dir(&runs).unwrap();
    let before_name = runs.join(Uuid::new_v4().to_string());
    let after_name = runs.join(Uuid::new_v4().to_string());
    fs::create_dir(&before_name).unwrap();
    // An actual old filesystem mtime makes the subsequent rename's change
    // observable even when this filesystem's clock has coarse resolution.
    fs::File::open(&runs)
        .unwrap()
        .set_times(fs::FileTimes::new().set_modified(std::time::UNIX_EPOCH))
        .unwrap();
    // Capture the actual root stat, then mutate its real directory entry
    // before the production guarded open; no injected stamp or hook.
    let captured = observation_directory_stamp(&runs).unwrap().unwrap();
    fs::rename(&before_name, &after_name).unwrap();
    let changed = observation_directory_stamp(&runs).unwrap().unwrap();
    assert!(same_observation_directory(&captured, &changed));
    assert_ne!(
        captured, changed,
        "real rename must change the captured stamp"
    );
    let guarded = open_observation_directory(&runs, &captured);
    assert!(
        matches!(&guarded, Err(OciError::Io(error))
            if error.kind() == std::io::ErrorKind::WouldBlock),
        "capture-rename coverage must be retryable; category={:?}",
        guarded
            .as_ref()
            .err()
            .map(|error| error.safe_start_context().1)
    );
    let reopened = open_observation_directory(&runs, &changed).unwrap();
    assert_eq!(
        reopened.metadata().unwrap().ino(),
        fs::metadata(&runs).unwrap().ino()
    );
    assert!(after_name.is_dir());
    assert!(!before_name.exists());
}

#[test]
fn observation_capture_refuses_replaced_symlink_and_nondirectory_roots() {
    let root = tempdir().unwrap();
    let runs = root.path().join("runs");
    let retained = root.path().join("retained-runs");
    fs::create_dir(&runs).unwrap();
    let captured = observation_directory_stamp(&runs).unwrap().unwrap();
    fs::rename(&runs, &retained).unwrap();
    fs::create_dir(&runs).unwrap();
    let replacement = observation_directory_stamp(&runs).unwrap().unwrap();
    assert!(!same_observation_directory(&captured, &replacement));
    assert!(matches!(
        open_observation_directory(&runs, &captured),
        Err(OciError::Artifact)
    ));
    fs::remove_dir(&runs).unwrap();
    symlink(&retained, &runs).unwrap();
    assert!(matches!(
        open_observation_directory(&runs, &captured),
        Err(OciError::Io(ref error)) if error.raw_os_error() == Some(rustix::io::Errno::LOOP.raw_os_error())
    ));
    fs::remove_file(&runs).unwrap();
    fs::write(&runs, b"not a directory").unwrap();
    assert!(matches!(
        open_observation_directory(&runs, &captured),
        Err(OciError::Artifact)
    ));
    assert!(retained.is_dir());
}

#[test]
fn observation_capture_keeps_actual_permission_denial_explicit() {
    let root = tempdir().unwrap();
    let runs = root.path().join("runs");
    fs::create_dir(&runs).unwrap();
    let captured = observation_directory_stamp(&runs).unwrap().unwrap();
    let permissions = fs::metadata(&runs).unwrap().permissions();
    fs::set_permissions(&runs, fs::Permissions::from_mode(0o0)).unwrap();
    let refused = open_observation_directory(&runs, &captured);
    fs::set_permissions(&runs, permissions).unwrap();
    assert!(matches!(
        refused,
        Err(OciError::Io(ref error)) if error.kind() == std::io::ErrorKind::PermissionDenied
    ));
    let restored = observation_directory_stamp(&runs).unwrap().unwrap();
    assert!(open_observation_directory(&runs, &restored).is_ok());
}

#[test]
fn service_start_persists_its_full_controller_generation_for_restart_observation() {
    let data = tempdir().unwrap();
    let (installation_id, installation, plan) = persisted_installation(data.path());
    authorize_installation(&installation, &"9".repeat(64));
    let placement = plan.runtime.placement.clone();
    let runner = NoProcess;
    for generation in [u64::from(u32::MAX) + 1, i64::MAX as u64] {
        let run_id = Uuid::new_v4().to_string();
        let identity = super::RecipeRunStartIdentity {
            run_generation: generation,
        };
        runtime(data.path(), &runner)
            .prepare_start_with_inspection_identity(
                &plan,
                &installation_id,
                &run_id,
                &placement,
                &identity,
            )
            .expect("legal Controller generations must not narrow at local persistence");
        let lifecycle_path = data
            .path()
            .join("run-metadata")
            .join(&run_id)
            .join("lifecycle.json");
        let lifecycle: Value = serde_json::from_slice(&fs::read(&lifecycle_path).unwrap()).unwrap();
        assert_eq!(lifecycle["run_generation"], generation);
        let restarted = runtime(data.path(), &runner);
        let (_, _, _, retained) = restarted.load_run_lifecycle(&run_id).unwrap().unwrap();
        assert_eq!(retained, Some(generation));
        let mut invalid = lifecycle;
        invalid["run_generation"] = serde_json::json!(i64::MAX as u64 + 1);
        fs::write(&lifecycle_path, serde_json::to_vec(&invalid).unwrap()).unwrap();
        assert!(matches!(
            restarted.read_run_lifecycle(&lifecycle_path),
            Err(OciError::Json(_))
        ));
    }
}
