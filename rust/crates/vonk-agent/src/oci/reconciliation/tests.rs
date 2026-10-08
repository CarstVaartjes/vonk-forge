#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn reconciliation_removing_checkpoint_recovers_after_partial_or_complete_quarantine_deletion() {
    for delete_quarantine_before_retry in [false, true] {
        let directory = tempdir().unwrap();
        let data_root = directory.path().join("data");
        fs::create_dir_all(&data_root).unwrap();
        let installation_id = Uuid::new_v4();
        let (installation, identity) = reconciliation_installation(&data_root, installation_id);
        let runtime = OciRuntime {
            runner: &NoProcess,
            data_root: &data_root,
        };
        runtime.prepare_reconciliation(&identity).unwrap();

        let checkpoint_root = data_root.join("installation-reconciliation");
        let checkpoint_path =
            reconciliation_checkpoint_path(&checkpoint_root, &installation_id.to_string()).unwrap();
        let quarantine =
            reconciliation_quarantine_path(&checkpoint_root, &installation_id.to_string()).unwrap();
        let mut checkpoint = super::read_reconciliation_checkpoint(&checkpoint_path)
            .unwrap()
            .unwrap();
        let expected_directory_identity = read_reconciliation_directory_identity(&installation)
            .unwrap()
            .unwrap();
        assert_eq!(
            expected_directory_identity,
            (
                checkpoint.installation_device,
                checkpoint.installation_inode
            )
        );
        fs::rename(&installation, &quarantine).unwrap();
        checkpoint.state = InstallationReconciliationState::Removing;
        write_reconciliation_checkpoint(&checkpoint_root, &checkpoint_path, &checkpoint).unwrap();
        if delete_quarantine_before_retry {
            fs::remove_dir_all(&quarantine).unwrap();
        } else {
            fs::remove_file(quarantine.join("opaque-agent-file")).unwrap();
        }

        let resumed = runtime.prepare_reconciliation(&identity).unwrap();
        assert_eq!(resumed.complete, delete_quarantine_before_retry);
        let completed = runtime.finalize_reconciliation(&identity).unwrap();
        assert!(completed.complete);
        assert!(!installation.exists());
        assert!(!quarantine.exists());
        // A current reviewed retry of the same exact installation identity
        // can replay its durable receipt after a lost response.
        let replay = runtime.prepare_reconciliation(&identity).unwrap();
        assert!(replay.complete);
        assert_eq!(replay, completed);
    }
}

#[test]
fn reconciliation_missing_installation_reobserves_absence_without_history() {
    let directory = tempdir().unwrap();
    let data_root = directory.path().join("data");
    fs::create_dir_all(data_root.join("installations")).unwrap();
    let missing_id = Uuid::new_v4();
    let identity = reconciliation_identity(missing_id);
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: &data_root,
    };
    assert!(runtime.prepare_reconciliation(&identity).unwrap().complete);
    assert!(runtime.finalize_reconciliation(&identity).unwrap().complete);
}

#[test]
fn damaged_checkpoint_recovers_after_restart_and_does_not_poison_new_install() {
    // Wrong implementation: receipt parsing or its presence permanently
    // refused current cleanup and every later authorized install.
    for moved in [false, true] {
        let temp = tempdir().unwrap();
        let data = temp.path().join("data");
        fs::create_dir(&data).unwrap();
        let installation_id = Uuid::new_v4();
        let (installation, identity) = reconciliation_installation(&data, installation_id);
        let runtime = OciRuntime {
            runner: &NoProcess,
            data_root: &data,
        };
        runtime.prepare_reconciliation(&identity).unwrap();
        let root = data.join(INSTALLATION_RECONCILIATION_ROOT);
        let checkpoint =
            reconciliation_checkpoint_path(&root, &installation_id.to_string()).unwrap();
        if moved {
            fs::rename(
                &installation,
                reconciliation_quarantine_path(&root, &installation_id.to_string()).unwrap(),
            )
            .unwrap();
        }
        fs::write(&checkpoint, b"damaged").unwrap();
        let restarted = OciRuntime {
            runner: &NoProcess,
            data_root: &data,
        };
        restarted.prepare_reconciliation(&identity).unwrap();
        assert!(
            restarted
                .finalize_reconciliation(&identity)
                .unwrap()
                .complete
        );
        assert!(!installation.exists());
        // A fresh current installation is admitted while old completion
        // history still exists, and retains the shared model objects.
        let plan: CompiledExecutionPlan = serde_json::from_value(compiled_plan()).unwrap();
        for (artifact, bytes) in plan
            .artifacts
            .iter()
            .zip([b"primary".as_slice(), b"secondary".as_slice()])
        {
            let path = data.join("distribution/models").join(&artifact.sha256);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, bytes).unwrap();
        }
        restarted
            .install(
                &plan,
                &installation_id.to_string(),
                &plan.identity.recipe_revision_sha256,
            )
            .unwrap();
        restarted
            .verify_installation(&installation_id.to_string())
            .unwrap();
        assert!(installation.is_dir());
        assert!(!checkpoint.exists());
    }
}
