#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn runtime_access_grants_exact_model_output_cache_and_run_tmp_acls() {
    let (_temp, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    fs::create_dir_all(&model).unwrap();
    let arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
    let validated = validate_docker_run(&arguments, &roots, None).unwrap();
    let runner = RecordingAclRunner::default();
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
    executor.prepare_runtime_access(&validated).unwrap();

    let calls = runner.calls.lock().unwrap();
    let paths = calls
        .iter()
        .filter_map(|call| call.last())
        .map(PathBuf::from)
        .collect::<Vec<_>>();
    assert!(
        paths
            .iter()
            .any(|path| path.ends_with("models/primary/artifact-a.bin"))
    );
    assert!(paths.iter().any(|path| path.ends_with("outputs")));
    assert!(
        paths
            .iter()
            .any(|path| path.ends_with("installations/installation-1/runtime-cache/home"))
    );
    assert!(
        paths
            .iter()
            .any(|path| path.ends_with(format!("outputs/tmp/{RUN_ID}")))
    );
    assert!(
        paths
            .iter()
            .any(|path| path.ends_with("run-metadata/".to_owned() + RUN_ID + "/runtime.json"))
    );
    assert!(
        calls
            .iter()
            .all(|call| call.iter().all(|value| value != "777" && value != "chown"))
    );
}

#[test]
#[cfg(target_os = "linux")]
fn runtime_access_skips_exact_model_acl_and_rejects_unexpected_acl() {
    let (_temp, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    fs::write(&model, b"model").unwrap();
    fs::set_permissions(&model, fs::Permissions::from_mode(0o600)).unwrap();
    let mut acl = 2_u32.to_le_bytes().to_vec();
    for (tag, permissions, identifier) in [
        (0x0001_u16, 0o6_u16, u32::MAX),
        (0x0002, 0o4, 10_001),
        (0x0004, 0, u32::MAX),
        (0x0010, 0o4, u32::MAX),
        (0x0020, 0, u32::MAX),
    ] {
        acl.extend_from_slice(&tag.to_le_bytes());
        acl.extend_from_slice(&permissions.to_le_bytes());
        acl.extend_from_slice(&identifier.to_le_bytes());
    }
    xattr::set(&model, "system.posix_acl_access", &acl).unwrap();
    let arguments = runtime_arguments(&roots, &[(model.clone(), "/models", true)]);
    let validated = validate_docker_run(&arguments, &roots, None).unwrap();
    let runner = RecordingAclRunner::default();
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
    executor.prepare_runtime_access(&validated).unwrap();
    assert!(
        !runner
            .calls
            .lock()
            .unwrap()
            .iter()
            .any(|call| call.last() == Some(&model.display().to_string()))
    );

    // An additional named ACL entry cannot be silently transformed into
    // a runtime grant by the helper.
    let mut unexpected = acl[..4].to_vec();
    unexpected.extend_from_slice(&acl[4..20]);
    unexpected.extend_from_slice(&0x0002_u16.to_le_bytes());
    unexpected.extend_from_slice(&0o4_u16.to_le_bytes());
    unexpected.extend_from_slice(&10_002_u32.to_le_bytes());
    unexpected.extend_from_slice(&acl[20..]);
    xattr::set(&model, "system.posix_acl_access", &unexpected).unwrap();
    assert!(executor.prepare_runtime_access(&validated).is_err());
}

#[test]
#[cfg(target_os = "linux")]
fn repeated_model_and_input_prepare_reuses_kernel_acls() {
    let (_temp, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    fs::write(&model, b"model").unwrap();
    fs::set_permissions(&model, fs::Permissions::from_mode(0o600)).unwrap();
    let inputs = roots.agent_data.join("runs").join(RUN_ID).join("inputs");
    fs::set_permissions(&inputs, fs::Permissions::from_mode(0o700)).unwrap();
    let data = inputs.join("data.bin");
    let manifest = inputs.join("manifest.json");
    let readable = inputs.join("readable.txt");
    for (path, mode) in [(&data, 0o600), (&manifest, 0o400), (&readable, 0o644)] {
        fs::write(path, b"input").unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
    }
    let arguments = job_runtime_arguments(&roots, model.clone());
    let agent_uid = fs::metadata(&model).unwrap().uid();
    let validated = validate_docker_run(&arguments, &roots, Some(agent_uid)).unwrap();
    let runner = KernelAclRunner::default();
    let executor = OperationExecutor::new(
        roots,
        &[0; 32],
        runner.clone(),
        Some(agent_uid.wrapping_add(1)),
    )
    .unwrap()
    .with_runtime_request_owner(agent_uid);
    executor.prepare_runtime_access(&validated).unwrap();
    let first_writes = runner
        .calls
        .lock()
        .unwrap()
        .iter()
        .filter(|call| call.first().map(String::as_str) == Some("-R"))
        .count();
    assert_eq!(first_writes, 2);
    let before_second = [&model, &inputs, &data, &manifest, &readable].map(|path| {
        let metadata = fs::metadata(path).unwrap();
        (metadata.ctime(), metadata.ctime_nsec())
    });
    executor.prepare_runtime_access(&validated).unwrap();
    let second_writes = runner
        .calls
        .lock()
        .unwrap()
        .iter()
        .filter(|call| call.first().map(String::as_str) == Some("-R"))
        .count();
    assert_eq!(second_writes, first_writes);
    let after_second = [&model, &inputs, &data, &manifest, &readable].map(|path| {
        let metadata = fs::metadata(path).unwrap();
        (metadata.ctime(), metadata.ctime_nsec())
    });
    assert_eq!(after_second, before_second);
    assert_eq!(fs::read(model).unwrap(), b"model");
    for path in [&data, &manifest, &readable] {
        assert_eq!(fs::read(path).unwrap(), b"input");
    }
}

#[test]
fn runtime_tmp_reset_rejects_fifo_without_waiting_for_a_writer() {
    use std::os::unix::fs::FileTypeExt;
    use wait_timeout::ChildExt;

    const CHILD_ROOT: &str = "VONK_TMP_RESET_FIFO_TEST_ROOT";
    if let Some(root) = std::env::var_os(CHILD_ROOT) {
        let roots = ManagedRoots::under(Path::new(&root));
        let executor =
            OperationExecutor::new(roots, &[0; 32], MissingContainerRunner, None).unwrap();
        assert!(matches!(
            executor.reset_runtime_tmp_if_requested(RUN_ID),
            Err(OperationError::UnsafePath)
        ));
        return;
    }

    let (_temp, roots) = runtime_fixture();
    let marker = roots
        .agent_data
        .join("run-metadata")
        .join(RUN_ID)
        .join("tmp-reset-required");
    rustix::fs::mknodat(
        rustix::fs::CWD,
        &marker,
        rustix::fs::FileType::Fifo,
        rustix::fs::Mode::from_raw_mode(0o600),
        0,
    )
    .unwrap();
    let temporary = roots
        .agent_data
        .join("runs")
        .join(RUN_ID)
        .join("outputs/tmp");
    fs::create_dir(&temporary).unwrap();
    fs::write(temporary.join("sentinel"), b"keep").unwrap();
    // Run the real open/fstat boundary in another process so the wrong
    // blocking open fails this test within a deadline rather than hanging
    // the suite indefinitely on a FIFO with no writer.
    let mut child = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "operations::access::tests::runtime_tmp_reset_rejects_fifo_without_waiting_for_a_writer",
        ])
        .env(CHILD_ROOT, &roots.agent_data)
        .spawn()
        .unwrap();
    let status = child.wait_timeout(Duration::from_secs(5)).unwrap();
    if status.is_none() {
        child.kill().unwrap();
        child.wait().unwrap();
        panic!("runtime tmp cleanup blocked on a FIFO marker with no writer");
    }
    assert!(status.unwrap().success());
    assert!(fs::symlink_metadata(marker).unwrap().file_type().is_fifo());
    assert_eq!(fs::read(temporary.join("sentinel")).unwrap(), b"keep");
}
