#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn trusted_installation_verification_reuses_unchanged_metadata_receipt() {
    let data = tempdir().unwrap();
    let (installation_id, _, _) = persisted_installation(data.path());
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);

    runtime.verify_installation(&installation_id).unwrap();
}

#[test]
fn completed_install_retry_reuses_exact_receipt_without_another_space_reservation_or_copy() {
    let data = tempdir().unwrap();
    let installation_id = "cb555393-764b-4eb6-8f15-b416d289428f".to_owned();
    let installation = data.path().join("installations").join(&installation_id);
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let recipe_digest = plan.identity.recipe_revision_sha256.clone();
    let model_root = data.path().join("distribution/models");
    fs::create_dir_all(&model_root).unwrap();
    for (bytes, digest) in [
        (b"primary".as_slice(), &plan.artifacts[0].sha256),
        (b"secondary".as_slice(), &plan.artifacts[1].sha256),
    ] {
        let path = model_root.join(digest);
        fs::write(&path, bytes).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }

    let runner = NoProcess;
    {
        let first_runtime = runtime(data.path(), &runner);
        // The first attempt really copies the verified distribution objects
        // and persists the installation receipt. Its acknowledgement is lost.
        // The host's free space is irrelevant to this setup step.
        first_runtime
            .install_unlocked(&plan, &installation_id, &recipe_digest, &mut |_, _| {})
            .unwrap();
        assert_eq!(
            fs::read(installation.join("models/primary/config.json")).unwrap(),
            b"primary"
        );
        assert!(
            installation
                .join(super::INSTALLATION_METADATA_FILE)
                .is_file()
        );
    }
    let runtime = runtime(data.path(), &runner);
    let unavailable_full_copy_bytes = crate::inventory::available_disk_bytes(data.path()).unwrap();
    assert!(matches!(
        runtime.ensure_disk_available(unavailable_full_copy_bytes),
        Err(OciError::Capacity)
    ));
    let model = installation.join("models/primary/config.json");
    let before = fs::metadata(&model).unwrap();
    runtime
        .install_with_space_check(
            &plan,
            &installation_id,
            &recipe_digest,
            unavailable_full_copy_bytes,
        )
        .unwrap();

    let after = fs::metadata(&model).unwrap();
    assert_eq!(
        (after.dev(), after.ino(), after.ctime_nsec()),
        (before.dev(), before.ino(), before.ctime_nsec())
    );
    assert!(model_root.is_dir());

    fs::write(
        installation.join(super::INSTALLATION_METADATA_FILE),
        b"invalid receipt",
    )
    .unwrap();
    runtime
        .install_with_space_check(
            &plan,
            &installation_id,
            &recipe_digest,
            plan.artifacts
                .iter()
                .map(|artifact| artifact.size_bytes)
                .sum(),
        )
        .unwrap();
    runtime.verify_installation(&installation_id).unwrap();
    runtime
        .install_with_space_check(
            &plan,
            &installation_id,
            &recipe_digest,
            unavailable_full_copy_bytes,
        )
        .unwrap();
    assert_eq!(fs::metadata(&model).unwrap().ino(), before.ino());

    // A newer authorized request replaces stale derived identity while exact
    // shared model content is retained. Subsequent admission sees the repair.
    let newer_digest = "e".repeat(64);
    runtime
        .install_with_space_check(
            &plan,
            &installation_id,
            &newer_digest,
            plan.artifacts
                .iter()
                .map(|artifact| artifact.size_bytes)
                .sum(),
        )
        .unwrap();
    assert_eq!(
        runtime.recipe_digest(&installation_id).unwrap(),
        newer_digest
    );
    runtime
        .install_with_space_check(
            &plan,
            &installation_id,
            &newer_digest,
            unavailable_full_copy_bytes,
        )
        .unwrap();
    assert_eq!(fs::metadata(&model).unwrap().ino(), before.ino());
}

#[test]
fn installation_metadata_deduplicates_physical_projection_entries() {
    let data = tempdir().unwrap();
    let mut value = compiled_plan();
    let first = value["artifacts"][0].clone();
    let mut second = first.clone();
    second["mount"]["target"] = json!("/models/target");
    value["artifacts"] = json!([first, second]);
    let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let (installation_id, installation, plan) = persisted_plan_installation(
        data.path(),
        "cb555393-764b-4eb6-8f15-b416d2894291".to_owned(),
        plan,
    );
    assert_eq!(plan.artifacts.len(), 2);
    assert_eq!(
        read_installation_metadata(&installation)
            .unwrap()
            .unwrap()
            .entries
            .len(),
        1
    );

    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    runtime.verify_installation(&installation_id).unwrap();

    std::thread::sleep(Duration::from_millis(2));
    fs::write(installation.join("models/primary/config.json"), b"primary").unwrap();
    runtime.verify_installation(&installation_id).unwrap();
    runtime.verify_installation(&installation_id).unwrap();
}

#[test]
fn trusted_installation_verification_reuses_751_entry_receipt_without_hashing() {
    let data = tempdir().unwrap();
    let plan = large_plan();
    let (installation_id, _, _) = persisted_plan_installation(
        data.path(),
        "cb555393-764b-4eb6-8f15-b416d2894290".to_owned(),
        plan,
    );
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);

    runtime.verify_installation(&installation_id).unwrap();
}

#[test]
#[cfg(target_os = "linux")]
fn authorized_runtime_acl_transition_preserves_receipt_without_model_rehash() {
    let data = tempdir().unwrap();
    let (installation_id, installation, _) = persisted_installation(data.path());
    let primary = installation.join("models/primary/config.json");
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    runtime.verify_installation(&installation_id).unwrap();

    let transition = runtime
        .begin_installation_acl_transition(&installation_id)
        .unwrap();
    apply_acl(
        &primary,
        &[
            (0x0001, 0o6, u32::MAX),
            (0x0002, 0o4, 10_001),
            (0x0004, 0, u32::MAX),
            (0x0010, 0o4, u32::MAX),
            (0x0020, 0, u32::MAX),
        ],
    );
    runtime
        .finish_installation_acl_transition(&installation_id, &transition)
        .unwrap();
    runtime.verify_installation(&installation_id).unwrap();
    runtime.verify_installation(&installation_id).unwrap();
}

#[test]
#[cfg(target_os = "linux")]
fn runtime_acl_transition_refuses_same_size_content_change() {
    let data = tempdir().unwrap();
    let (installation_id, installation, _) = persisted_installation(data.path());
    let primary = installation.join("models/primary/config.json");
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    let transition = runtime
        .begin_installation_acl_transition(&installation_id)
        .unwrap();
    fs::write(&primary, b"changed").unwrap();
    assert!(
        runtime
            .finish_installation_acl_transition(&installation_id, &transition)
            .is_err()
    );
}

#[test]
#[cfg(target_os = "linux")]
fn trusted_installation_verification_rejects_unauthorized_runtime_acls() {
    for entries in [
        vec![
            (0x0001, 0o6, u32::MAX),
            (0x0002, 0o4, 10_001),
            (0x0004, 0o4, u32::MAX),
            (0x0010, 0o4, u32::MAX),
            (0x0020, 0, u32::MAX),
        ],
        vec![
            (0x0001, 0o6, u32::MAX),
            (0x0002, 0o4, 10_001),
            (0x0004, 0, u32::MAX),
            (0x0010, 0o4, u32::MAX),
            (0x0020, 0o4, u32::MAX),
        ],
        vec![
            (0x0001, 0o6, u32::MAX),
            (0x0002, 0o4, 10_001),
            (0x0002, 0o4, 10_002),
            (0x0004, 0, u32::MAX),
            (0x0010, 0o4, u32::MAX),
            (0x0020, 0, u32::MAX),
        ],
        vec![
            (0x0001, 0o6, u32::MAX),
            (0x0002, 0o6, 10_001),
            (0x0004, 0, u32::MAX),
            (0x0010, 0o4, u32::MAX),
            (0x0020, 0, u32::MAX),
        ],
    ] {
        let data = tempdir().unwrap();
        let (installation_id, installation, _) = persisted_installation(data.path());
        let primary = installation.join("models/primary/config.json");
        apply_acl(&primary, &entries);
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        assert!(
            matches!(
                runtime.verify_installation(&installation_id),
                Err(OciError::Artifact)
            ),
            "entries {entries:?}"
        );
    }
}

#[test]
fn installation_verification_does_not_rehash_same_size_content() {
    let data = tempdir().unwrap();
    let (installation_id, installation, _) = persisted_installation(data.path());
    let primary = installation.join("models/primary/config.json");
    fs::write(&primary, b"mutated").unwrap();
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);

    // Bytes were placed by the agent from the Controller; size and
    // custody are the check, so a same-size edit is not re-hashed.
    runtime.verify_installation(&installation_id).unwrap();
}

#[test]
fn trusted_installation_verification_refreshes_after_metadata_only_change() {
    let data = tempdir().unwrap();
    let (installation_id, installation, _) = persisted_installation(data.path());
    let primary = installation.join("models/primary/config.json");
    std::thread::sleep(Duration::from_millis(2));
    fs::write(&primary, b"primary").unwrap();
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);

    runtime.verify_installation(&installation_id).unwrap();
    runtime.verify_installation(&installation_id).unwrap();
}

#[test]
fn trusted_installation_verification_rejects_invalid_file_metadata() {
    let cases = ["size", "symlink", "directory", "mode", "nlink", "owner"];
    for case in cases {
        let data = tempdir().unwrap();
        let (installation_id, installation, _) = persisted_installation(data.path());
        let primary = installation.join("models/primary/config.json");
        match case {
            "size" => fs::write(&primary, b"short").unwrap(),
            "symlink" => {
                fs::remove_file(&primary).unwrap();
                symlink(installation.join("models/secondary/config.json"), &primary).unwrap();
            }
            "directory" => {
                fs::remove_file(&primary).unwrap();
                fs::create_dir(&primary).unwrap();
            }
            "mode" => fs::set_permissions(&primary, fs::Permissions::from_mode(0o644)).unwrap(),
            "nlink" => fs::hard_link(
                &primary,
                installation.join("models/primary/config-link.json"),
            )
            .unwrap(),
            "owner" => {
                if rustix::process::geteuid().as_raw() != 0 {
                    continue;
                }
                rustix::fs::chown(&primary, Some(rustix::process::Uid::from_raw(65_534)), None)
                    .unwrap();
            }
            _ => unreachable!(),
        }
        let runner = NoProcess;
        let runtime = runtime(data.path(), &runner);
        assert!(
            matches!(
                runtime.verify_installation(&installation_id),
                Err(OciError::Artifact)
            ),
            "case {case}"
        );
    }
}
