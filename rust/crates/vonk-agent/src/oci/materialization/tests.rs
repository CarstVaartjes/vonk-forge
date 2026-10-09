#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn runtime_tmp_refuses_symlink_replacement_targets() {
    let data = tempdir().unwrap();
    let outputs = data.path().join("outputs");
    fs::create_dir(&outputs).unwrap();
    let target = data.path().join("outside");
    fs::create_dir(&target).unwrap();
    symlink(&target, outputs.join("tmp")).unwrap();

    assert!(matches!(
        ensure_runtime_tmp(&outputs),
        Err(OciError::Artifact)
    ));
    assert!(target.is_dir());
}

#[test]
fn model_materialization_temp_cleanup_is_task_owned() {
    let data = tempdir().unwrap();
    let temporary = data.path().join("model.partial");
    fs::write(&temporary, b"incomplete").unwrap();
    {
        let _guard = super::TemporaryArtifact::new(temporary.clone());
    }
    assert!(!temporary.exists());

    fs::write(&temporary, b"published").unwrap();
    {
        let mut guard = super::TemporaryArtifact::new(temporary.clone());
        guard.retain();
    }
    assert_eq!(fs::read(temporary).unwrap(), b"published");
}

#[test]
fn compiled_models_materialize_selection_scoped_colliding_paths() {
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    plan.validate().unwrap();
    let data = tempdir().unwrap();
    let root = data.path().join("distribution").join("models");
    fs::create_dir_all(&root).unwrap();
    for (artifact, bytes) in [
        (&plan.artifacts[0], b"primary".as_slice()),
        (&plan.artifacts[1], b"secondary".as_slice()),
    ] {
        let path = root.join(&artifact.sha256);
        fs::write(&path, bytes).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    }

    let paths =
        materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
            .unwrap();
    assert_eq!(paths.len(), 2);
    assert_eq!(
        fs::read(
            data.path().join(
                "installations/cb555393-764b-4eb6-8f15-b416d289428f/models/primary/config.json"
            )
        )
        .unwrap(),
        b"primary"
    );
    assert_eq!(
        fs::read(data.path().join(
            "installations/cb555393-764b-4eb6-8f15-b416d289428f/models/secondary/config.json"
        ))
        .unwrap(),
        b"secondary"
    );
}

#[test]
fn model_materialization_reports_bytes_while_a_large_file_is_copied() {
    // A first install copies hundreds of gigabytes. Its progress must move
    // inside a file, not only between files, or the operation shows no
    // bytes for minutes at a time.
    const LARGE: u64 = 70 * 1024 * 1024;
    let mut value = compiled_plan();
    value["artifacts"][0]["size_bytes"] = json!(LARGE);
    let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let data = tempdir().unwrap();
    let root = data.path().join("distribution").join("models");
    fs::create_dir_all(&root).unwrap();
    let large = root.join(&plan.artifacts[0].sha256);
    fs::File::create(&large).unwrap().set_len(LARGE).unwrap();
    fs::set_permissions(&large, fs::Permissions::from_mode(0o600)).unwrap();
    let small = root.join(&plan.artifacts[1].sha256);
    fs::write(&small, b"secondary").unwrap();
    fs::set_permissions(&small, fs::Permissions::from_mode(0o600)).unwrap();
    let total = LARGE + plan.artifacts[1].size_bytes;

    let mut reports = Vec::new();
    materialize_compiled_models_with(
        data.path(),
        &plan,
        "cb555393-764b-4eb6-8f15-b416d289428f",
        false,
        &mut |done, of| reports.push((done, of)),
        &|| false,
    )
    .unwrap();

    assert!(reports.iter().all(|(_, of)| *of == total));
    assert!(reports.windows(2).all(|pair| pair[0].0 <= pair[1].0));
    assert_eq!(reports.first(), Some(&(0, total)));
    assert_eq!(reports.last(), Some(&(total, total)));
    assert!(
        reports.iter().any(|(done, _)| *done > 0 && *done < LARGE),
        "no progress inside the large file: {reports:?}"
    );
}

#[test]
fn compiled_models_materialize_valid_empty_support_files() {
    let mut value = compiled_plan();
    let artifact = &mut value["artifacts"][0];
    artifact["selection_id"] = json!("primary");
    artifact["file_id"] = json!("tokenizer-config");
    artifact["path"] = json!("tokenizer_config.json");
    artifact["sha256"] = json!(crate::workloads::EMPTY_SHA256);
    artifact["size_bytes"] = json!(0);
    artifact["roles"] = json!(["tokenizer"]);
    value["artifacts"] = json!([artifact.clone()]);
    let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let data = tempdir().unwrap();
    let source = data.path().join("distribution").join("models");
    fs::create_dir_all(&source).unwrap();
    let source_file = source.join(&plan.artifacts[0].sha256);
    fs::write(&source_file, []).unwrap();
    fs::set_permissions(&source_file, fs::Permissions::from_mode(0o600)).unwrap();
    materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
        .unwrap();
    assert_eq!(
        fs::metadata(data.path().join("installations/cb555393-764b-4eb6-8f15-b416d289428f/models/primary/tokenizer_config.json")).unwrap().len(),
        0
    );
}

#[test]
fn compiled_models_materialize_one_source_for_two_mount_projections() {
    let mut value = compiled_plan();
    let mut projection = value["artifacts"][0].clone();
    projection["mount"]["target"] = json!("/models/target");
    value["artifacts"] = json!([value["artifacts"][0].clone(), projection]);
    let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let data = tempdir().unwrap();
    let source = data.path().join("distribution").join("models");
    fs::create_dir_all(&source).unwrap();
    let source_file = source.join(&plan.artifacts[0].sha256);
    fs::write(&source_file, b"primary").unwrap();
    fs::set_permissions(&source_file, fs::Permissions::from_mode(0o600)).unwrap();
    let paths =
        materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
            .unwrap();
    assert_eq!(paths.len(), 1);
    assert_eq!(
        paths[0],
        data.path()
            .join("installations/cb555393-764b-4eb6-8f15-b416d289428f/models/primary/config.json")
    );
    let installation = data
        .path()
        .join("installations/cb555393-764b-4eb6-8f15-b416d289428f");
    write_installation_metadata(data.path(), &installation, &plan).unwrap();
    let repeated =
        materialize_compiled_models(data.path(), &plan, "cb555393-764b-4eb6-8f15-b416d289428f")
            .unwrap();
    assert_eq!(repeated.len(), 1);
    assert_eq!(plan.artifacts.len(), 2);
}

#[test]
fn a_new_installation_links_the_shared_model_files_instead_of_copying_them() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let store = stock_store(data.path(), &plan);

    let first = linked_installation(data.path(), &plan, FIRST);
    let second = linked_installation(data.path(), &plan, SECOND);

    for (artifact, object) in plan.artifacts.iter().zip(&store) {
        let object = fs::metadata(object).unwrap();
        for installation in [&first, &second] {
            let file = fs::metadata(
                installation
                    .join("models")
                    .join(&artifact.selection_id)
                    .join(&artifact.path),
            )
            .unwrap();
            assert_eq!((file.dev(), file.ino()), (object.dev(), object.ino()));
        }
        // The store and both installations: one inode, three names, one
        // set of bytes on disk.
        assert_eq!(object.nlink(), 3);
    }
    assert_eq!(
        fs::read(first.join("models/primary/config.json")).unwrap(),
        b"primary"
    );

    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    runtime.verify_installation(FIRST).unwrap();
    runtime.verify_installation(SECOND).unwrap();
    // The tree still measures at its full logical size.
    assert!(runtime.installed_bytes(FIRST).unwrap() > 16);
}

#[test]
fn a_sibling_installation_changing_the_link_count_does_not_stale_the_others_custody() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    stock_store(data.path(), &plan);
    linked_installation(data.path(), &plan, FIRST);
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    runtime.verify_installation(FIRST).unwrap();

    // Linking a second installation and removing it again both change the
    // shared inode's change time.
    std::thread::sleep(Duration::from_millis(2));
    let second = linked_installation(data.path(), &plan, SECOND);
    fs::remove_dir_all(second).unwrap();

    let transition = runtime.begin_installation_acl_transition(FIRST).unwrap();
    runtime
        .finish_installation_acl_transition(FIRST, &transition)
        .unwrap();
    runtime.verify_installation(FIRST).unwrap();
}

#[test]
fn a_model_file_linked_to_anything_but_the_store_object_is_refused() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    stock_store(data.path(), &plan);
    let installation = linked_installation(data.path(), &plan, FIRST);
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    runtime.verify_installation(FIRST).unwrap();

    // A second, foreign name for the same inode does not change that it is
    // the store object, but a different inode with two names is not one.
    let foreign = installation.join("models/primary/config.json");
    fs::remove_file(&foreign).unwrap();
    let other = data.path().join("other-secret");
    fs::write(&other, b"primary").unwrap();
    fs::set_permissions(&other, fs::Permissions::from_mode(0o600)).unwrap();
    fs::hard_link(&other, &foreign).unwrap();
    assert!(matches!(
        runtime.verify_installation(FIRST),
        Err(OciError::Artifact)
    ));
}

#[test]
#[cfg(target_os = "linux")]
fn an_object_a_workload_already_runs_from_still_links_into_a_new_installation() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let store = stock_store(data.path(), &plan);
    linked_installation(data.path(), &plan, FIRST);
    // The helper grants the runtime user read access when the first
    // installation starts; the shared inode carries it for every name.
    apply_acl(
        &store[0],
        &[
            (0x0001, 0o6, u32::MAX),
            (0x0002, 0o4, 10_001),
            (0x0004, 0, u32::MAX),
            (0x0010, 0o4, u32::MAX),
            (0x0020, 0, u32::MAX),
        ],
    );
    assert_eq!(fs::metadata(&store[0]).unwrap().mode() & 0o777, 0o640);

    linked_installation(data.path(), &plan, SECOND);
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    runtime.verify_installation(FIRST).unwrap();
    runtime.verify_installation(SECOND).unwrap();
}

#[test]
fn an_installation_keeps_a_private_copy_it_already_holds() {
    let data = tempdir().unwrap();
    let (installation_id, installation, plan) = persisted_installation(data.path());
    stock_store(data.path(), &plan);
    let before = fs::metadata(installation.join("models/primary/config.json")).unwrap();

    materialize_compiled_models(data.path(), &plan, &installation_id).unwrap();

    let after = fs::metadata(installation.join("models/primary/config.json")).unwrap();
    assert_eq!((after.dev(), after.ino()), (before.dev(), before.ino()));
    assert_eq!(after.nlink(), 1);
    let runner = NoProcess;
    runtime(data.path(), &runner)
        .verify_installation(&installation_id)
        .unwrap();
}

#[test]
fn a_store_object_that_is_not_private_owner_only_is_never_linked() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let store = stock_store(data.path(), &plan);
    fs::set_permissions(&store[0], fs::Permissions::from_mode(0o644)).unwrap();

    assert!(matches!(
        materialize_compiled_models(data.path(), &plan, FIRST),
        Err(OciError::Artifact)
    ));
    assert!(
        !data
            .path()
            .join("installations")
            .join(FIRST)
            .join("models/primary/config.json")
            .exists()
    );
}

#[test]
fn model_materialization_copies_when_a_link_cannot_be_made() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let store = stock_store(data.path(), &plan);

    materialize_compiled_models_with(data.path(), &plan, FIRST, false, &mut |_, _| {}, &|| false)
        .unwrap();

    let copy = fs::metadata(
        data.path()
            .join("installations")
            .join(FIRST)
            .join("models/primary/config.json"),
    )
    .unwrap();
    let object = fs::metadata(&store[0]).unwrap();
    assert_ne!(copy.ino(), object.ino());
    assert_eq!((copy.nlink(), object.nlink()), (1, 1));
}

// This uses a hosted, owned bind mount: distribution/models is real tmpfs,
// installations is the runner filesystem. No fake link function or link=false.
#[cfg(target_os = "linux")]
#[test]
#[ignore = "requires the hosted owned cross-device model-store fixture"]
fn real_cross_device_link_failure_logs_cause_copies_and_recovers() {
    const ROOT: &str = "VONK_OCI_CROSS_DEVICE_ROOT";
    const PHASE: &str = "VONK_OCI_CROSS_DEVICE_PHASE";
    const TEST: &str = "oci::tests::real_cross_device_link_failure_logs_cause_copies_and_recovers";
    let data = PathBuf::from(std::env::var_os(ROOT).expect("hosted fixture root"));
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let model_root = data.join("distribution/models");
    assert_ne!(
        fs::metadata(&model_root).unwrap().dev(),
        fs::metadata(&data).unwrap().dev(),
        "the test must reach the real cross-device hard-link error"
    );
    if let Ok(phase) = std::env::var(PHASE) {
        if phase == "first" {
            stock_store(&data, &plan);
        }
        let runner = NoProcess;
        // A new process and runtime reopen the actual persisted installation.
        let instance = runtime(&data, &runner);
        let mut reports = Vec::new();
        instance
            .install_unlocked(
                &plan,
                FIRST,
                &plan.identity.recipe_revision_sha256,
                &mut |done, total| reports.push((done, total)),
                &|| false,
            )
            .unwrap();
        instance.verify_installation(FIRST).unwrap();
        let total: u64 = plan
            .artifacts
            .iter()
            .map(|artifact| artifact.size_bytes)
            .sum();
        assert_eq!(reports.last(), Some(&(total, total)));
        for (artifact, bytes) in plan
            .artifacts
            .iter()
            .zip([b"primary".as_slice(), b"secondary".as_slice()])
        {
            let destination = data
                .join("installations")
                .join(FIRST)
                .join("models")
                .join(&artifact.selection_id)
                .join(&artifact.path);
            let actual = fs::read(&destination).unwrap();
            assert_eq!(actual, bytes);
            assert_eq!(vonk_agent_protocol::hex_sha256(&actual), artifact.sha256);
            let stored = fs::metadata(model_root.join(&artifact.sha256)).unwrap();
            let copied = fs::metadata(&destination).unwrap();
            assert_ne!((copied.dev(), copied.ino()), (stored.dev(), stored.ino()));
            assert_eq!((copied.nlink(), stored.nlink()), (1, 1));
        }
        return;
    }
    let execute = |phase: &str| {
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([TEST, "--exact", "--ignored", "--nocapture"])
            .env(PHASE, phase)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stderr).unwrap()
    };
    let cause = std::io::Error::from_raw_os_error(18).to_string(); // Linux EXDEV.
    let assert_fallbacks = |stderr: &str, artifacts: &[crate::workloads::CompiledModelArtifact]| {
        let logs = stderr
            .lines()
            .filter(|line| line.contains(vonk_agent_protocol::generated::AgentDiagnosticOperation::ModelMaterializationCopyFallback.as_str()))
            .collect::<Vec<_>>();
        assert_eq!(logs.len(), artifacts.len(), "{stderr}");
        for artifact in artifacts {
            let expected = format!(
                "vonk-agent: {} sha256={} bytes={} cause={cause}",
                vonk_agent_protocol::generated::AgentDiagnosticOperation::ModelMaterializationCopyFallback.as_str(),
                artifact.sha256, artifact.size_bytes
            );
            assert!(
                logs.contains(&expected.as_str()),
                "missing safe cause: {stderr}"
            );
        }
        assert!(
            !stderr.contains(data.to_str().unwrap()),
            "owned paths must not leak in the cause log"
        );
    };
    let first = execute("first");
    assert_fallbacks(&first, &plan.artifacts);
    // Preserve the captured production cause in the hosted proof log.
    eprint!("{first}");
    let model = data
        .join("installations")
        .join(FIRST)
        .join("models/primary/config.json");
    let before = fs::metadata(&model).unwrap();
    let reused = execute("reuse");
    assert_fallbacks(&reused, &[]);
    let after = fs::metadata(&model).unwrap();
    assert_eq!(
        (before.dev(), before.ino(), before.ctime_nsec()),
        (after.dev(), after.ino(), after.ctime_nsec())
    );
    // A stale private copy is recovered through the same normal materializer.
    // Same-size damage prevents a length-only check from accidentally passing.
    fs::write(&model, b"damaged").unwrap();
    let recovered = execute("recover");
    assert_fallbacks(&recovered, &plan.artifacts[..1]);
    eprint!("{recovered}");
    assert_eq!(fs::read(&model).unwrap(), b"primary");
    assert_ne!(fs::metadata(&model).unwrap().ino(), after.ino());
    let installation = data.join("installations").join(FIRST);
    assert!(
        installation
            .join(super::INSTALLATION_METADATA_FILE)
            .is_file()
    );
    assert!(
        !fs::read_dir(model.parent().unwrap())
            .unwrap()
            .any(|entry| entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .ends_with(".partial"))
    );
}

#[test]
fn linkable_bytes_count_only_complete_store_objects() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    assert_eq!(super::linkable_model_bytes(data.path(), &plan), 0);
    let store = stock_store(data.path(), &plan);
    assert_eq!(super::linkable_model_bytes(data.path(), &plan), 7 + 9);
    fs::write(&store[1], b"short").unwrap();
    assert_eq!(super::linkable_model_bytes(data.path(), &plan), 7);
}

#[test]
fn cancelled_copy_yields_without_publishing_and_fresh_preparation_converges() {
    let data = tempdir().unwrap();
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let store = stock_store(data.path(), &plan);
    let checks = std::cell::Cell::new(0);
    let cancelled = || {
        let count = checks.get() + 1;
        checks.set(count);
        count > 2
    };
    assert!(
        materialize_compiled_models_controlled(
            data.path(),
            &plan,
            FIRST,
            false,
            &mut |_, _| {},
            &cancelled,
        )
        .is_err()
    );
    assert!(
        !data
            .path()
            .join("installations")
            .join(FIRST)
            .join("models/primary/config.json")
            .exists()
    );
    assert!(store.iter().all(|path| path.exists()));
    materialize_compiled_models_controlled(
        data.path(),
        &plan,
        FIRST,
        false,
        &mut |_, _| {},
        &|| false,
    )
    .unwrap();
    assert_eq!(
        fs::read(
            data.path()
                .join("installations")
                .join(FIRST)
                .join("models/primary/config.json")
        )
        .unwrap(),
        b"primary"
    );
}

#[test]
fn in_flight_model_copy_cancels_at_checkpoint_and_fresh_copy_reuses_verified_sources() {
    use std::cell::Cell;
    const LARGE: u64 = MATERIALIZE_PROGRESS_STEP * 2;
    let mut value = compiled_plan();
    value["artifacts"][0]["size_bytes"] = json!(LARGE);
    let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let data = tempdir().unwrap();
    let root = data.path().join("distribution/models");
    fs::create_dir_all(&root).unwrap();
    let source = root.join(&plan.artifacts[0].sha256);
    fs::File::create(&source).unwrap().set_len(LARGE).unwrap();
    fs::set_permissions(&source, fs::Permissions::from_mode(0o600)).unwrap();
    let small = root.join(&plan.artifacts[1].sha256);
    fs::write(&small, b"secondary").unwrap();
    fs::set_permissions(&small, fs::Permissions::from_mode(0o600)).unwrap();
    let inode = fs::metadata(&source).unwrap().ino();
    let cancellation = Cell::new(false);
    let copied = Cell::new(0);
    let result = materialize_compiled_models_with(
        data.path(),
        &plan,
        FIRST,
        false,
        &mut |done, _| {
            copied.set(done);
            if done >= MATERIALIZE_PROGRESS_STEP {
                cancellation.set(true);
            }
        },
        &|| cancellation.get(),
    );
    assert!(result.unwrap_err().is_cancelled());
    assert!(copied.get() < LARGE);
    assert_eq!(fs::metadata(&source).unwrap().ino(), inode);
    cancellation.set(false);
    let placed =
        materialize_compiled_models_with(data.path(), &plan, FIRST, false, &mut |_, _| {}, &|| {
            cancellation.get()
        })
        .unwrap();
    assert_eq!(placed.len(), plan.artifacts.len());
    assert_eq!(fs::metadata(&placed[0]).unwrap().len(), LARGE);
    assert_eq!(fs::read(&placed[1]).unwrap(), b"secondary");
}
