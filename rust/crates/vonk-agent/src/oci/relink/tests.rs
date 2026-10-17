use super::*;
use crate::oci::test_support::{NoProcess, persisted_installation, runtime};
use std::time::Duration;

fn relink(data: &Path, started: &[&str]) -> Relinked {
    let started = started.iter().map(|name| (*name).to_owned()).collect();
    relink_private_model_copies(data, &started, Instant::now() + Duration::from_secs(60))
}

fn model_path(installation: &Path, artifact: &crate::workloads::CompiledModelArtifact) -> PathBuf {
    installation
        .join("models")
        .join(&artifact.selection_id)
        .join(&artifact.path)
}

/// An installation made before files were shared: a private copy of each
/// model file, receipted, and the store's own object of each digest.
fn before_sharing(
    data: &Path,
) -> (
    String,
    PathBuf,
    crate::workloads::CompiledExecutionPlan,
    Vec<PathBuf>,
) {
    let (id, installation, plan) = persisted_installation(data);
    let store = data.join("distribution/models");
    fs::create_dir_all(&store).unwrap();
    let objects = unique_plan_artifacts(&plan)
        .into_iter()
        .map(|artifact| {
            let object = store.join(&artifact.sha256);
            fs::copy(model_path(&installation, artifact), &object).unwrap();
            fs::set_permissions(&object, fs::Permissions::from_mode(0o600)).unwrap();
            object
        })
        .collect();
    (id, installation, plan, objects)
}

fn same_inode(left: &Path, right: &Path) -> bool {
    let (left, right) = (fs::metadata(left).unwrap(), fs::metadata(right).unwrap());
    left.dev() == right.dev() && left.ino() == right.ino()
}

#[test]
fn a_receipted_private_copy_becomes_a_link_of_its_store_object() {
    let data = tempfile::tempdir().unwrap();
    let (id, installation, plan, objects) = before_sharing(data.path());
    let runner = NoProcess;

    let relinked = relink(data.path(), &[]);

    let artifacts = unique_plan_artifacts(&plan);
    assert_eq!(relinked.files, artifacts.len() as u64);
    assert_eq!(
        relinked.freed_bytes,
        artifacts
            .iter()
            .map(|artifact| artifact.size_bytes)
            .sum::<u64>()
    );
    for (artifact, object) in artifacts.iter().zip(&objects) {
        let file = model_path(&installation, artifact);
        assert!(same_inode(&file, object));
        assert_eq!(fs::metadata(object).unwrap().nlink(), 2);
    }
    // The receipt now binds the shared inode: the strict checks a start makes
    // pass as they did for the copy.
    runtime(data.path(), &runner)
        .verify_installation(&id)
        .unwrap();
    runtime(data.path(), &runner)
        .begin_installation_acl_transition(&id)
        .unwrap();
    // Nothing is left to relink.
    assert_eq!(relink(data.path(), &[]), Relinked::default());
}

#[test]
fn a_receipted_copy_without_a_store_object_becomes_the_object() {
    let data = tempfile::tempdir().unwrap();
    let (id, installation, plan) = persisted_installation(data.path());
    fs::create_dir_all(data.path().join("distribution/models")).unwrap();
    let runner = NoProcess;

    let relinked = relink(data.path(), &[]);

    assert_eq!(relinked.freed_bytes, 0);
    for artifact in unique_plan_artifacts(&plan) {
        let object = store_object_path(data.path(), &artifact.sha256);
        assert!(same_inode(&model_path(&installation, artifact), &object));
        assert_eq!(fs::metadata(&object).unwrap().nlink(), 2);
    }
    assert_eq!(relinked.files, unique_plan_artifacts(&plan).len() as u64);
    runtime(data.path(), &runner)
        .begin_installation_acl_transition(&id)
        .unwrap();
}

#[test]
fn only_a_copy_its_receipt_proves_is_relinked() {
    let data = tempfile::tempdir().unwrap();
    let (_, installation, plan, objects) = before_sharing(data.path());
    let artifacts = unique_plan_artifacts(&plan);
    let (first, second) = (artifacts[0], artifacts[1]);
    // Rewritten after its receipt: no longer proven to be the digest's bytes.
    let rewritten = model_path(&installation, first);
    let bytes = fs::read(&rewritten).unwrap();
    std::thread::sleep(Duration::from_millis(20));
    fs::write(&rewritten, &bytes).unwrap();
    // A store object of another size is not this file's object.
    fs::write(&objects[1], b"x").unwrap();

    assert_eq!(relink(data.path(), &[]).files, 0);
    assert_eq!(fs::metadata(&rewritten).unwrap().nlink(), 1);
    assert_eq!(
        fs::metadata(model_path(&installation, second))
            .unwrap()
            .nlink(),
        1
    );
}

#[test]
fn an_installation_without_a_receipt_or_with_a_started_run_keeps_its_copies() {
    let data = tempfile::tempdir().unwrap();
    let (id, installation, plan, _) = before_sharing(data.path());
    let file = model_path(&installation, unique_plan_artifacts(&plan)[0]);

    // A started run's installation keeps the files its workload mounted.
    assert_eq!(relink(data.path(), &[&id]).files, 0);
    assert_eq!(fs::metadata(&file).unwrap().nlink(), 1);

    fs::remove_file(installation.join(INSTALLATION_METADATA_FILE)).unwrap();
    assert_eq!(relink(data.path(), &[]).files, 0);
    assert_eq!(fs::metadata(&file).unwrap().nlink(), 1);
}

#[test]
fn a_link_made_before_its_receipt_was_rewritten_is_recorded_on_the_next_pass() {
    let data = tempfile::tempdir().unwrap();
    let (id, installation, plan, objects) = before_sharing(data.path());
    // A pass that ended between the rename and the receipt write.
    let artifact = unique_plan_artifacts(&plan)[0];
    let file = model_path(&installation, artifact);
    let temporary = installation.join("linked.partial");
    fs::hard_link(&objects[0], &temporary).unwrap();
    fs::rename(&temporary, &file).unwrap();
    let runner = NoProcess;
    assert!(
        runtime(data.path(), &runner)
            .begin_installation_acl_transition(&id)
            .is_err()
    );

    relink(data.path(), &[]);

    runtime(data.path(), &runner)
        .begin_installation_acl_transition(&id)
        .unwrap();
}

#[test]
fn a_pass_at_its_deadline_relinks_nothing_and_says_more_remain() {
    let data = tempfile::tempdir().unwrap();
    let (_, installation, plan, _) = before_sharing(data.path());

    let relinked = relink_private_model_copies(data.path(), &BTreeSet::new(), Instant::now());

    assert!(relinked.pending);
    assert_eq!(relinked.files, 0);
    let file = model_path(&installation, unique_plan_artifacts(&plan)[0]);
    assert_eq!(fs::metadata(file).unwrap().nlink(), 1);
}

#[test]
#[cfg(target_os = "linux")]
fn a_copy_a_workload_once_read_relinks_with_its_access_intact() {
    let data = tempfile::tempdir().unwrap();
    let (id, installation, plan) = persisted_installation(data.path());
    let artifacts = unique_plan_artifacts(&plan);
    // A start granted the runtime user read access on both copies and
    // recorded them again.
    for artifact in &artifacts {
        crate::oci::test_support::apply_acl(
            &model_path(&installation, artifact),
            &[
                (0x0001, 0o6, u32::MAX),
                (0x0002, 0o4, 10_001),
                (0x0004, 0, u32::MAX),
                (0x0010, 0o4, u32::MAX),
                (0x0020, 0, u32::MAX),
            ],
        );
    }
    write_installation_metadata(data.path(), &installation, &plan).unwrap();
    // The store holds the second file's object, not the first's.
    let store = data.path().join("distribution/models");
    fs::create_dir_all(&store).unwrap();
    let second = store.join(&artifacts[1].sha256);
    fs::copy(model_path(&installation, artifacts[1]), &second).unwrap();
    fs::set_permissions(&second, fs::Permissions::from_mode(0o600)).unwrap();

    assert_eq!(relink(data.path(), &[]).files, 2);

    // The first copy became the object with its access; the second is the
    // store's object, whose access the next start grants.
    let first = store_object_path(data.path(), &artifacts[0].sha256);
    assert_eq!(fs::metadata(&first).unwrap().mode() & 0o777, 0o640);
    assert!(same_inode(
        &model_path(&installation, artifacts[1]),
        &second
    ));
    let runner = NoProcess;
    runtime(data.path(), &runner)
        .verify_installation(&id)
        .unwrap();
    runtime(data.path(), &runner)
        .begin_installation_acl_transition(&id)
        .unwrap();
}
