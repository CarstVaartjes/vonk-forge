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
        assert!(!resumed.complete);
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
fn reconciliation_missing_installation_without_checkpoint_is_not_cleanup_success() {
    let directory = tempdir().unwrap();
    let data_root = directory.path().join("data");
    fs::create_dir_all(data_root.join("installations")).unwrap();
    let missing_id = Uuid::new_v4();
    let identity = reconciliation_identity(missing_id);
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: &data_root,
    };
    assert!(runtime.prepare_reconciliation(&identity).is_err());
}
