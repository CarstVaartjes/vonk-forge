#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn fresh_claim_retains_exact_started_plan_without_resetting_writable_state() {
    let data = tempdir().unwrap();
    let (installation_id, installation, plan) = persisted_installation(data.path());
    authorize_installation(&installation, &plan.identity.recipe_revision_sha256);
    let run_id = Uuid::new_v4().to_string();
    let runner = NoProcess;
    let first_agent = runtime(data.path(), &runner);
    first_agent
        .prepare_start(&plan, &installation_id, &run_id, &plan.runtime.placement)
        .unwrap();
    let reset = data
        .path()
        .join("run-metadata")
        .join(&run_id)
        .join("tmp-reset-required");
    // Stand in for the helper's completed cleanup. Retained recovery must
    // not request a second cleanup after a workload has run.
    fs::remove_file(&reset).unwrap();
    let marker = data
        .path()
        .join("runs")
        .join(&run_id)
        .join("outputs/tmp")
        .join(&run_id)
        .join("in-flight-output");
    fs::create_dir_all(marker.parent().unwrap()).unwrap();
    fs::write(&marker, b"keep").unwrap();

    let restarted_agent = runtime(data.path(), &runner);
    restarted_agent
        .prepare_retained_start_if_present(
            &plan,
            &installation_id,
            &run_id,
            &plan.runtime.placement,
            None,
        )
        .unwrap()
        .unwrap();
    assert!(!reset.exists());
    assert_eq!(fs::read(marker).unwrap(), b"keep");
    let mut other_placement = plan.runtime.placement.clone();
    other_placement.reserved_memory_bytes += 1;
    assert!(
        restarted_agent
            .prepare_retained_start_if_present(
                &plan,
                &installation_id,
                &run_id,
                &other_placement,
                None,
            )
            .is_err()
    );
}

#[test]
fn restart_preparation_leaves_private_runtime_tmp_for_the_authorized_helper() {
    use std::os::unix::process::CommandExt;

    const CHILD_ROOT: &str = "VONK_RESTART_TMP_TEST_ROOT";
    if let Ok(root) = std::env::var(CHILD_ROOT) {
        let root = Path::new(&root);
        let installation_id = std::env::var("VONK_RESTART_TMP_INSTALLATION").unwrap();
        let run_id = std::env::var("VONK_RESTART_TMP_RUN").unwrap();
        let runner = NoProcess;
        let runtime = runtime(root, &runner);
        let plan = runtime.load_spec(&installation_id).unwrap();
        runtime
            .prepare_start(&plan, &installation_id, &run_id, &plan.runtime.placement)
            .unwrap();
        return;
    }

    let data = tempdir().unwrap();
    let (installation_id, _, plan) = persisted_installation(data.path());
    let run_id = Uuid::new_v4().to_string();
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    runtime
        .prepare_start(&plan, &installation_id, &run_id, &plan.runtime.placement)
        .unwrap();
    runtime.complete_stop(&run_id).unwrap();
    let private = data
        .path()
        .join("runs")
        .join(&run_id)
        .join("outputs/tmp")
        .join(&run_id);
    fs::create_dir(&private).unwrap();
    fs::write(private.join("engine-owned"), b"temporary").unwrap();
    fs::set_permissions(&private, fs::Permissions::from_mode(0o700)).unwrap();

    let mut child = std::process::Command::new(std::env::current_exe().unwrap());
    child
        .args([
            "--exact",
            "oci::tests::restart_preparation_leaves_private_runtime_tmp_for_the_authorized_helper",
            "--nocapture",
        ])
        .env(CHILD_ROOT, data.path())
        .env("VONK_RESTART_TMP_INSTALLATION", &installation_id)
        .env("VONK_RESTART_TMP_RUN", &run_id);
    if rustix::process::geteuid().is_root() {
        fn agent_owns(path: &Path) {
            rustix::fs::chown(path, Some(rustix::process::Uid::from_raw(65534)), None).unwrap();
            if path.is_dir() {
                for entry in fs::read_dir(path).unwrap() {
                    agent_owns(&entry.unwrap().path());
                }
            }
        }
        agent_owns(data.path());
        // The helper creates this directory as root and grants only the
        // runtime UID access. The unprivileged agent cannot traverse it.
        rustix::fs::chown(&private, Some(rustix::process::Uid::ROOT), None).unwrap();
        child.uid(65534).gid(65534);
    } else {
        fs::set_permissions(&private, fs::Permissions::from_mode(0o000)).unwrap();
    }
    let result = child.output().unwrap();
    fs::set_permissions(&private, fs::Permissions::from_mode(0o700)).unwrap();
    assert!(
        result.status.success(),
        "{}{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
    assert_eq!(
        fs::read(private.join("engine-owned")).unwrap(),
        b"temporary"
    );
}

#[test]
fn runtime_tmp_boundary_is_kept_private_without_deleting_runtime_owned_content() {
    let data = tempdir().unwrap();
    let outputs = data.path().join("outputs");
    fs::create_dir_all(outputs.join("tmp")).unwrap();
    fs::write(outputs.join("tmp").join("stale.marker"), b"stale").unwrap();

    ensure_runtime_tmp(&outputs).unwrap();

    let temporary = outputs.join("tmp");
    assert!(temporary.join("stale.marker").exists());
    let metadata = fs::symlink_metadata(temporary).unwrap();
    assert!(metadata.is_dir());
    assert!(!metadata.file_type().is_symlink());
    assert_eq!(metadata.mode() & 0o777, 0o700);
}
