use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime},
};

use super::{
    GRACE, LOW_FREE_PERCENT, RESERVE_FLOOR_BYTES, RESERVE_PERCENT, STORES, Swept, Untracked, sweep,
};

const OBJECT: &str = "1111111111111111111111111111111111111111111111111111111111111111";
const OTHER: &str = "2222222222222222222222222222222222222222222222222222222222222222";
const THIRD: &str = "3333333333333333333333333333333333333333333333333333333333333333";
const INSTALLATION: &str = "0b7d2c9e-6a51-4c43-9d07-3a3c3c3c3c3c";
const ORPHAN: &str = "5f0c1e2d-3b4a-4c5d-8e6f-7a8b9c0d1e2f";
const TOTAL: u64 = 4_000_000_000_000;
/// Below the 10 % low-space line.
const SHORT: Option<(u64, u64)> = Some((TOTAL / 20, TOTAL));
const ROOMY: Option<(u64, u64)> = Some((TOTAL / 2, TOTAL));

fn after_grace() -> SystemTime {
    SystemTime::now() + GRACE + Duration::from_secs(60)
}

fn within_grace() -> SystemTime {
    SystemTime::now() + GRACE - Duration::from_secs(60)
}

fn run(root: &Path, now: SystemTime, disk: &dyn Fn() -> Option<(u64, u64)>) -> Swept {
    judged(root, now, None, disk)
}

/// A pass with the Controller's verdict naming `untracked` installations.
fn judged(
    root: &Path,
    now: SystemTime,
    untracked: Option<&[&str]>,
    disk: &dyn Fn() -> Option<(u64, u64)>,
) -> Swept {
    let untracked = Some(verdict(untracked.unwrap_or_default(), None));
    sweep(
        root,
        now,
        Instant::now() + Duration::from_secs(60),
        untracked.as_ref(),
        disk,
        &|| {},
    )
}

/// A verdict naming `installations` and, when it judged them, `runs`.
fn verdict(installations: &[&str], runs: Option<&[&str]>) -> Untracked {
    let names = |names: &[&str]| names.iter().map(|name| (*name).to_owned()).collect();
    Untracked {
        observed_installations: names(&[INSTALLATION, ORPHAN]),
        observed_runs: names(runs.unwrap_or_default()),
        installations: names(installations),
        runs: runs.map(names),
        retention: Some(retention()),
    }
}

fn swept_with(root: &Path, now: SystemTime, untracked: &Untracked) -> Swept {
    sweep(
        root,
        now,
        Instant::now() + Duration::from_secs(60),
        Some(untracked),
        &|| ROOMY,
        &|| {},
    )
}

fn file(path: &Path, bytes: &[u8]) -> PathBuf {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, bytes).unwrap();
    path.to_path_buf()
}

#[test]
fn an_unlinked_model_object_is_a_cache_that_leaves_only_while_space_is_short() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let store = root.path().join("distribution/models");
    let linked = file(&store.join(OBJECT), b"weights");
    let orphan = file(&store.join(OTHER), b"superseded");
    // The production trigger: an uninstall without model cleanup removed the
    // installation's link, so only the store's own name remains.
    let installation = root
        .path()
        .join("installations")
        .join(INSTALLATION)
        .join("models/sel");
    fs::create_dir_all(&installation).unwrap();
    fs::hard_link(&linked, installation.join("model.safetensors")).unwrap();

    // Reusable content is not a leak: with room to spare it stays, however old.
    assert_eq!(run(root.path(), after_grace(), &|| ROOMY).entries, 0);
    assert!(orphan.exists());

    let swept = run(root.path(), SystemTime::now(), &|| SHORT);
    assert_eq!((swept.entries, swept.bytes), (1, 10));
    assert!(!orphan.exists());
    // A linked object is an installation's file: kept whatever the space.
    assert!(linked.exists() && installation.join("model.safetensors").exists());
}

#[test]
fn low_space_takes_the_least_recently_used_first_and_stops_above_the_line() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let store = root.path().join("distribution/models");
    let older = file(&store.join(OBJECT), b"old");
    std::thread::sleep(Duration::from_millis(20));
    let newer = file(&store.join(OTHER), b"new");
    let target =
        TOTAL / 100 * LOW_FREE_PERCENT + RESERVE_FLOOR_BYTES.max(TOTAL / 100 * RESERVE_PERCENT);
    // Removing the older object brings free space back above line + reserve.
    let disk = || Some((if older.exists() { TOTAL / 20 } else { target }, TOTAL));

    run(root.path(), SystemTime::now(), &disk);

    assert!(!older.exists());
    assert!(newer.exists());
}

#[test]
fn a_claim_makes_room_from_unused_model_files_least_recently_used_first() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let store = root.path().join("distribution/models");
    let oldest = file(&store.join(OBJECT), b"old");
    std::thread::sleep(Duration::from_millis(20));
    let own = file(&store.join(OTHER), b"own");
    std::thread::sleep(Duration::from_millis(20));
    let newest = file(&store.join(THIRD), b"new");
    assert_eq!(super::unused_model_bytes(root.path()), Some(9));
    let free = super::disk_usage(root.path()).unwrap().0;

    // Already room: nothing goes.
    let keep: BTreeSet<String> = [OTHER.to_owned()].into();
    assert_eq!(super::make_room(root.path(), free, &keep), 0);
    assert!(oldest.exists() && own.exists() && newest.exists());

    // More than the disk can offer: every unused object goes but the claim's
    // own, least recently used first, and the claim then checks as before.
    assert_eq!(super::make_room(root.path(), u64::MAX, &keep), 6);
    assert!(!oldest.exists() && !newest.exists() && own.exists());
    assert_eq!(super::unused_model_bytes(root.path()), Some(3));
}

#[test]
fn a_model_object_an_installation_plan_names_stays_while_the_installation_does() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    // A private copy (from before files were shared, or where a link cannot be
    // made) leaves the object unlinked; every start still prepares from it.
    let (_, installation, plan) = crate::oci::test_support::persisted_installation(root.path());
    let store = root.path().join("distribution/models");
    let object = file(&store.join(&plan.artifacts[0].sha256), b"primary");

    assert_eq!(run(root.path(), after_grace(), &|| SHORT).entries, 0);
    assert!(object.exists());

    // An unreadable plan could name any object; scratch still leaves.
    fs::write(installation.join("spec.json"), b"{").unwrap();
    let partial = file(&store.join(format!("{OTHER}.partial")), b"prefix");
    assert_eq!(run(root.path(), after_grace(), &|| SHORT).entries, 1);
    assert!(object.exists() && !partial.exists());

    // Uninstalled: the object has no owner left, nor the one the private
    // copy of the other file became on the first pass.
    fs::remove_dir_all(&installation).unwrap();
    assert_eq!(run(root.path(), SystemTime::now(), &|| SHORT).entries, 2);
    assert!(!object.exists() && !store.join(&plan.artifacts[1].sha256).exists());
}

#[test]
fn an_ended_transfer_checkpoint_and_isolated_entries_leave_after_the_grace() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let store = root.path().join("distribution/models");
    let partial = file(&store.join(format!("{OBJECT}.partial")), b"prefix");
    let damaged = file(&store.join(format!("{OTHER}.x.damaged")), b"bad");
    let isolated_root = root.path().join("distribution/models.y.damaged");
    file(&isolated_root.join(OBJECT), b"old");

    // A fault-ended transfer resumes from its checkpoint within the grace.
    assert_eq!(run(root.path(), within_grace(), &|| SHORT).entries, 0);
    // Past it, scratch is a leak: it leaves whatever the free space.
    let swept = run(root.path(), after_grace(), &|| ROOMY);

    assert_eq!(swept.entries, 3);
    assert!(!partial.exists() && !damaged.exists() && !isolated_root.exists());
    assert!(store.is_dir());
}

#[test]
fn a_build_export_whose_upload_never_finished_leaves_after_the_grace() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let build = root
        .path()
        .join("builds/0b7d2c9e-6a51-4c43-9d07-3a3c3c3c3c3c");
    file(&build.join("image.docker.tar"), b"layers");
    file(&build.join(format!("{OBJECT}.json")), b"{}");

    assert_eq!(run(root.path(), within_grace(), &|| ROOMY).entries, 0);
    assert!(build.exists());
    assert_eq!(run(root.path(), after_grace(), &|| ROOMY).entries, 1);
    assert!(!build.exists());
    assert!(root.path().join("builds").is_dir());
}

#[test]
fn a_base_image_no_build_opened_for_the_grace_leaves_unless_a_build_holds_it() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let images = root.path().join("base-images/sha256");
    let image = images.join(OBJECT);
    file(&image.join("image.oci.tar"), b"base");
    let lock = file(&images.join(format!("{OBJECT}-lock")), b"");

    assert_eq!(run(root.path(), within_grace(), &|| SHORT).entries, 0);
    // A build holding the lock is using the image now.
    let held = crate::held_lock::HeldLock::try_exclusive(fs::File::open(&lock).unwrap()).unwrap();
    assert_eq!(run(root.path(), after_grace(), &|| ROOMY).entries, 0);
    assert!(image.exists());
    drop(held);

    assert_eq!(run(root.path(), after_grace(), &|| ROOMY).entries, 1);
    assert!(!image.exists() && !lock.exists());
}

#[test]
fn retired_stores_are_emptied() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let legacy = file(
        &root.path().join("models/sha256").join(OBJECT).join("blob"),
        b"old",
    );
    // Old custody left read-only directories behind.
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(legacy.parent().unwrap(), fs::Permissions::from_mode(0o500)).unwrap();
    let import = file(&root.path().join("image-imports/archive.tar"), b"old");
    let archive = file(&root.path().join("oci-archives").join(OBJECT), b"old");

    run(root.path(), after_grace(), &|| ROOMY);

    assert!(!legacy.exists() && !import.exists() && !archive.exists());
    // The package still creates image-imports; the directory is left alone.
    assert!(root.path().join("image-imports").is_dir());
}

#[test]
fn controller_owned_stores_and_links_out_of_a_store_are_never_followed() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let installation = file(
        &root
            .path()
            .join("installations")
            .join(INSTALLATION)
            .join("spec.json"),
        b"{}",
    );
    let run_scope = file(
        &root
            .path()
            .join("runs/9d4e5f6a-7b8c-4d9e-8f0a-1b2c3d4e5f6a/outputs/x"),
        b"out",
    );
    let outside = tempfile::tempdir().unwrap();
    let target = file(&outside.path().join("keep"), b"user data");
    fs::create_dir_all(root.path().join("builds")).unwrap();
    std::os::unix::fs::symlink(outside.path(), root.path().join("builds/link")).unwrap();
    // A store root that is a link is not walked at all.
    std::os::unix::fs::symlink(outside.path(), root.path().join("oci-archives")).unwrap();

    run(root.path(), after_grace(), &|| SHORT);

    assert!(installation.exists() && run_scope.exists());
    assert!(target.exists());
    assert!(fs::symlink_metadata(root.path().join("builds/link")).is_err());
}

#[test]
fn an_installation_the_controller_does_not_track_leaves_a_day_after_it_said_so() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let installations = root.path().join("installations");
    let store = root.path().join("distribution/models");
    let object = file(&store.join(OBJECT), b"weights");
    // An orphan links a shared model object and holds its own runtime cache.
    let orphan = installations.join(ORPHAN);
    file(&orphan.join("runtime-cache/compiled"), b"kernels");
    fs::create_dir_all(orphan.join("models/sel")).unwrap();
    fs::hard_link(&object, orphan.join("models/sel/model.safetensors")).unwrap();
    // A tracked installation with a readable plan.
    let (_, tracked, plan) = crate::oci::test_support::persisted_installation(root.path());

    // Without the Controller's verdict (an older Controller) nothing goes,
    // however old the directory.
    assert_eq!(run(root.path(), after_grace(), &|| ROOMY).entries, 0);
    // The first verdict starts the grace; within it nothing goes.
    assert_eq!(
        judged(root.path(), after_grace(), Some(&[ORPHAN]), &|| ROOMY).entries,
        0
    );
    assert_eq!(
        judged(root.path(), within_grace(), Some(&[ORPHAN]), &|| ROOMY).entries,
        0
    );
    // Tracked again (a restored record): the grace starts over.
    judged(root.path(), after_grace(), Some(&[]), &|| ROOMY);
    assert_eq!(
        judged(root.path(), SystemTime::now(), Some(&[ORPHAN]), &|| ROOMY).entries,
        0
    );
    assert!(orphan.exists());

    // A started run's installation stays whatever the verdict says.
    let record = vonk_agent_protocol::generated::RunLifecycleRecord {
        installation_id: ORPHAN.to_owned(),
        placement: plan.runtime.placement.clone(),
        run_generation: None,
    };
    let lifecycle = file(
        &root
            .path()
            .join("run-metadata/9d4e5f6a-7b8c-4d9e-8f0a-1b2c3d4e5f6a/lifecycle.json"),
        &serde_json::to_vec(&record).unwrap(),
    );
    assert_eq!(
        judged(root.path(), after_grace(), Some(&[ORPHAN]), &|| ROOMY).entries,
        0
    );
    // Local run ownership clears an old grace mark even outside this page.
    let mut omitted = verdict(&[], None);
    omitted.observed_installations.clear();
    swept_with(root.path(), after_grace(), &omitted);
    assert!(!orphan.join("untracked-since").exists());
    // A run record that cannot be read could name any installation.
    fs::write(&lifecycle, b"{").unwrap();
    assert_eq!(
        judged(root.path(), after_grace(), Some(&[ORPHAN]), &|| ROOMY).entries,
        0
    );
    assert!(orphan.exists());

    // The run stopped: a day after the verdict the orphan leaves, and its
    // model object is unused in the same pass, so low space takes it too.
    fs::remove_file(&lifecycle).unwrap();
    judged(root.path(), SystemTime::now(), Some(&[ORPHAN]), &|| ROOMY);
    let swept = judged(root.path(), after_grace(), Some(&[ORPHAN]), &|| SHORT);
    assert!(!orphan.exists() && !object.exists());
    assert!(swept.entries >= 2);
    // What the Controller tracks stays.
    assert!(tracked.exists());
}

#[test]
fn a_run_scope_no_record_owns_leaves_a_day_after_the_verdict_and_frees_its_installation() {
    const RUN: &str = "6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
    const JOB: &str = "7b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e";
    const LIVE: &str = "8c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f";
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let (tracked_id, tracked, plan) = crate::oci::test_support::persisted_installation(root.path());
    let lifecycle = |installation: &str| {
        serde_json::to_vec(&vonk_agent_protocol::generated::RunLifecycleRecord {
            installation_id: installation.to_owned(),
            placement: plan.runtime.placement.clone(),
            run_generation: None,
        })
        .unwrap()
    };
    // A stopped run whose lifecycle record leaked keeps an orphan installation.
    let orphan = file(
        &root
            .path()
            .join("installations")
            .join(ORPHAN)
            .join("spec.json"),
        b"{}",
    );
    let run_scope = file(
        &root.path().join("runs").join(RUN).join("outputs/x"),
        b"out",
    );
    let run_record = file(
        &root
            .path()
            .join("run-metadata")
            .join(RUN)
            .join("lifecycle.json"),
        &lifecycle(ORPHAN),
    );
    // A forgotten job's scope, a live run, and an isolated scope.
    let job_scope = file(&root.path().join("runs").join(JOB).join("inputs/x"), b"in");
    let live = file(
        &root
            .path()
            .join("run-metadata")
            .join(LIVE)
            .join("lifecycle.json"),
        &lifecycle(&tracked_id),
    );
    let isolated = file(
        &root
            .path()
            .join("runs")
            .join(format!("{JOB}.{ORPHAN}.damaged/x")),
        b"bad",
    );
    let scopes = [&run_scope, &run_record, &job_scope];

    // A verdict that judged no run scope (an older Controller) removes none.
    swept_with(root.path(), after_grace(), &verdict(&[ORPHAN], None));
    assert!(scopes.iter().all(|path| path.exists()) && orphan.exists());
    // The first verdict that names them marks them; tracked again unmarks.
    let mut forgotten = verdict(&[ORPHAN], Some(&[RUN, JOB]));
    forgotten.observed_runs = [RUN, JOB, LIVE].into_iter().map(str::to_owned).collect();
    assert_eq!(
        swept_with(root.path(), SystemTime::now(), &forgotten).entries,
        0
    );
    let mark = root
        .path()
        .join("run-metadata")
        .join(RUN)
        .join("untracked-since");
    assert!(mark.exists());
    let mut tracked_again = verdict(&[ORPHAN], Some(&[]));
    tracked_again.observed_runs = forgotten.observed_runs.clone();
    swept_with(root.path(), SystemTime::now(), &tracked_again);
    assert!(!mark.exists());
    swept_with(root.path(), SystemTime::now(), &forgotten);

    // A day later both stores' scopes leave; the live run and its
    // installation stay, and the isolated scope leaves with the grace.
    swept_with(root.path(), after_grace(), &forgotten);
    assert!(scopes.iter().all(|path| !path.parent().unwrap().exists()));
    assert!(!root.path().join("runs").join(JOB).exists());
    assert!(!isolated.exists());
    assert!(live.exists() && tracked.exists());
    // The orphan installation no longer has a run naming it: it is marked
    // now and leaves a day later.
    assert!(orphan.exists());
    swept_with(root.path(), SystemTime::now(), &forgotten);
    swept_with(root.path(), after_grace(), &forgotten);
    assert!(!orphan.parent().unwrap().exists());
}

#[test]
fn inventory_names_the_run_scopes_of_both_stores() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    assert_eq!(super::run_scope_names(root.path()), Some(vec![]));
    file(
        &root.path().join("runs").join(ORPHAN).join("outputs/x"),
        b"",
    );
    file(
        &root
            .path()
            .join("run-metadata")
            .join(ORPHAN)
            .join("lifecycle.json"),
        b"{}",
    );
    file(
        &root
            .path()
            .join("run-metadata")
            .join(INSTALLATION)
            .join("lifecycle.json"),
        b"{}",
    );
    file(&root.path().join("runs/x.damaged/x"), b"");
    assert_eq!(
        super::run_scope_names(root.path()),
        Some(vec![INSTALLATION.parse().unwrap(), ORPHAN.parse().unwrap()])
    );
}

#[test]
fn an_isolated_entry_among_the_installations_leaves_after_the_grace() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let installations = root.path().join("installations");
    let damaged = file(
        &installations
            .join(format!("{INSTALLATION}.{ORPHAN}.damaged"))
            .join("spec.json"),
        b"{}",
    );

    assert_eq!(run(root.path(), within_grace(), &|| ROOMY).entries, 0);
    assert_eq!(run(root.path(), after_grace(), &|| ROOMY).entries, 1);
    assert!(!damaged.exists() && installations.is_dir());
}

#[test]
fn a_removed_installations_cleanup_history_and_lock_leave_after_the_grace() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let history = root.path().join("installation-reconciliation");
    let locks = root.path().join("installation-ownership");
    const QUARANTINED: &str = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
    const HELD: &str = "8b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e";
    // Removed long ago: superseded bytes, retired records, an unfinished
    // write, the checkpoint and the lock file of the gone installation.
    let retained = file(&history.join(format!("{ORPHAN}.retained/spec.json")), b"{}");
    let retired = file(&history.join(format!("{ORPHAN}.retired")), b"{}");
    let unfinished = file(&history.join(".checkpoint-x.tmp"), b"{");
    let gone = file(&history.join(format!("{ORPHAN}.json")), b"{}");
    let gone_lock = file(&locks.join(format!("{ORPHAN}.lock")), b"");
    // Still on disk: an installation, and a removal the Controller resumes.
    file(
        &root
            .path()
            .join("installations")
            .join(INSTALLATION)
            .join("spec.json"),
        b"{}",
    );
    let live = file(&history.join(format!("{INSTALLATION}.json")), b"{}");
    let live_lock = file(&locks.join(format!("{INSTALLATION}.lock")), b"");
    let quarantine = file(
        &history.join(format!("{QUARANTINED}.partial/spec.json")),
        b"{}",
    );
    let resumed = file(&history.join(format!("{QUARANTINED}.json")), b"{}");
    let resumed_lock = file(&locks.join(format!("{QUARANTINED}.lock")), b"");
    // A claim holds this one's lock: it is reconciling it now.
    let held_lock = file(&locks.join(format!("{HELD}.lock")), b"");
    let held =
        crate::held_lock::HeldLock::try_exclusive(fs::File::open(&held_lock).unwrap()).unwrap();

    assert_eq!(run(root.path(), within_grace(), &|| ROOMY).entries, 0);
    run(root.path(), after_grace(), &|| ROOMY);

    for leftover in [&retained, &retired, &unfinished, &gone, &gone_lock] {
        assert!(!leftover.exists(), "{}", leftover.display());
    }
    assert!(!retained.parent().unwrap().exists());
    for kept in [
        &live,
        &live_lock,
        &quarantine,
        &resumed,
        &resumed_lock,
        &held_lock,
    ] {
        assert!(kept.exists(), "{}", kept.display());
    }
    drop(held);
    run(root.path(), after_grace(), &|| ROOMY);
    assert!(!held_lock.exists());
    assert!(history.is_dir() && locks.is_dir());
}

#[test]
fn inventory_names_the_installation_directories() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    assert_eq!(super::installation_names(root.path()), Some(vec![]));
    file(
        &root
            .path()
            .join("installations")
            .join(ORPHAN)
            .join("spec.json"),
        b"{}",
    );
    file(
        &root
            .path()
            .join("installations")
            .join(INSTALLATION)
            .join("spec.json"),
        b"{}",
    );
    file(
        &root.path().join("installations/x.damaged/spec.json"),
        b"{}",
    );
    file(
        &root.path().join("installations").join(format!("{ORPHAN}0")),
        b"",
    );

    assert_eq!(
        super::installation_names(root.path()),
        Some(vec![INSTALLATION.parse().unwrap(), ORPHAN.parse().unwrap()])
    );
}

#[test]
fn inventory_measures_each_installation_as_it_is_now() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    assert_eq!(
        super::installation_sizes(
            root.path(),
            &super::installation_names(root.path()).unwrap()
        ),
        Some(vec![])
    );
    let installation = root.path().join("installations").join(INSTALLATION);
    let object = file(
        &root.path().join("distribution/models").join(OBJECT),
        b"weights",
    );
    fs::create_dir_all(installation.join("models/sel")).unwrap();
    fs::hard_link(&object, installation.join("models/sel/model.safetensors")).unwrap();
    file(&installation.join("spec.json"), b"{}");
    // A runtime cache grows the installation after its install.
    file(&installation.join("runtime-cache/compiled"), b"kernels!!");
    // A tree that cannot be measured is left out, never guessed.
    let broken = root.path().join("installations").join(ORPHAN);
    fs::create_dir_all(&broken).unwrap();
    std::os::unix::fs::symlink("/etc", broken.join("escape")).unwrap();

    let sizes = super::installation_sizes(
        root.path(),
        &super::installation_names(root.path()).unwrap(),
    )
    .unwrap();

    assert_eq!(sizes.len(), 1);
    assert_eq!(sizes[0].installation_id.to_string(), INSTALLATION);
    // Every file at its full length, a shared model link included.
    assert_eq!(sizes[0].bytes, 7 + 2 + 9);
}

#[test]
fn a_pass_past_its_deadline_removes_nothing_and_says_more_remain() {
    let root = tempfile::tempdir().unwrap();
    authorize_retention(root.path());
    let orphan = file(&root.path().join("builds/b/image.docker.tar"), b"x");

    let swept = sweep(
        root.path(),
        after_grace(),
        Instant::now(),
        None,
        &|| ROOMY,
        &|| {},
    );

    assert!(swept.pending);
    assert_eq!(swept.entries, 0);
    assert!(orphan.exists());
}

/// The low-space line and reserve are the Controller's own.
#[test]
fn the_low_space_line_is_the_controllers() {
    let settings =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../control/src/vonk_control/settings.py");
    let settings = fs::read_to_string(settings).unwrap();
    for line in [
        format!("STORAGE_LOW_FREE_FRACTION = 0.{LOW_FREE_PERCENT}"),
        format!("STORAGE_EVICTION_RESERVE_FRACTION = 0.0{RESERVE_PERCENT}"),
        format!(
            "STORAGE_EVICTION_RESERVE_FLOOR_BYTES = {} * 1024**3",
            RESERVE_FLOOR_BYTES >> 30
        ),
    ] {
        assert!(settings.lines().any(|setting| setting == line), "{line}");
    }
}

/// Every top-level name written below the agent data root, by the agent, its
/// helper or its package, has a declared owner and collector.
#[test]
fn every_store_below_the_data_root_has_an_owner_and_a_collector() {
    let repository = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let mut sources = Vec::new();
    for crate_name in ["vonk-agent", "vonk-agent-helper", "vonk-spark-setup"] {
        collect_sources(
            &repository.join("rust/crates").join(crate_name).join("src"),
            &mut sources,
        );
    }
    for package in [
        "packaging/debian/postinst",
        "packaging/debian/postinst-repair",
    ] {
        sources.push(repository.join(package));
    }
    let mut names = BTreeSet::new();
    for source in &sources {
        let text = fs::read_to_string(source).unwrap();
        for prefix in [
            "data_root.join(\"",
            "data_dir.join(\"",
            "agent_data.join(\"",
            "data_root, \"",
            "&data_root_file, \"",
            "/var/lib/vonk-forge-agent/",
        ] {
            for (index, _) in text.match_indices(prefix) {
                let rest = &text[index + prefix.len()..];
                let name: String = rest
                    .chars()
                    .take_while(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
                    .collect();
                // A mktemp template names the temporary's prefix.
                let name = name.strip_suffix(".XXXXXX").unwrap_or(&name).to_owned();
                if !name.is_empty() {
                    names.insert((name, source.display().to_string()));
                }
            }
        }
    }
    let declared: BTreeSet<_> = STORES
        .iter()
        .map(|store| store.0.trim_end_matches('/'))
        .collect();
    let undeclared: Vec<_> = names
        .iter()
        .filter(|(name, _)| !declared.contains(name.as_str()))
        .collect();
    assert!(
        undeclared.is_empty(),
        "declare an owner and a collector in store_retention::STORES for {undeclared:?}"
    );
    assert!(names.len() > 10, "the scan found the data root's stores");
    assert_eq!(declared.len(), STORES.len(), "one entry per store");
    assert!(STORES.iter().all(|store| !store.2.is_empty()));
    // An entry only a Controller order removes stays for ever once the
    // Controller forgets it. Each such store is a reviewed exception; prefer a
    // collector that asks the Controller, as installations/ does.
    let order_only: Vec<_> = STORES
        .iter()
        .filter(|store| store.1 == super::Collector::ControllerOrder)
        .map(|store| store.0)
        .collect();
    assert_eq!(
        order_only,
        Vec::<&str>::new(),
        "a new Controller-order-only store needs a collector for what the Controller forgets"
    );
}

fn collect_sources(directory: &Path, sources: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(directory).unwrap() {
        let path = entry.unwrap().path();
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        if path.is_dir() {
            if name != "tests" && name != "test_support" {
                collect_sources(&path, sources);
            }
        } else if name.ends_with(".rs")
            && name != "tests.rs"
            && !name.ends_with("_tests.rs")
            && name != "store_retention.rs"
        {
            sources.push(path);
        }
    }
}

pub(crate) fn retention() -> vonk_agent_protocol::generated::StoreRetentionRequest {
    vonk_agent_protocol::generated::StoreRetentionRequest {
        issued_at: std::time::SystemTime::now()
            .duration_since(std::time::SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs(),
        expires_at: SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs()
            + 300,
        required_free_bytes: 0,
        remove_model_objects: Vec::new(),
        protected_model_objects: Vec::new(),
        protected_image_addresses: Vec::new(),
    }
}

pub(crate) fn authorize_retention(root: &Path) {
    fs::write(
        root.join("store-retention.json"),
        serde_json::to_vec(&retention()).unwrap(),
    )
    .unwrap();
}

#[test]
fn a_load_demand_above_the_low_space_line_evicts_lru_objects_and_preserves_references() {
    use super::fake_disk::{FakeDisk, stored_bytes};
    let root = tempfile::tempdir().unwrap();
    let protected = file(
        &root.path().join("distribution/models").join(OBJECT),
        &[0; 4096],
    );
    std::thread::sleep(Duration::from_millis(20));
    let oldest = file(
        &root.path().join("distribution/models").join(OTHER),
        &[0; 4096],
    );
    std::thread::sleep(Duration::from_millis(20));
    let newest = file(
        &root.path().join("distribution/models").join(THIRD),
        &[0; 4096],
    );
    let mut request = retention();
    request.protected_model_objects = vec![OBJECT.to_owned()];
    request.required_free_bytes = 100_000;
    fs::write(
        root.path().join("store-retention.json"),
        serde_json::to_vec(&request).unwrap(),
    )
    .unwrap();
    let _disk = FakeDisk::mount(root.path(), stored_bytes(root.path()) + 98_000);
    assert!(super::disk_usage(root.path()).unwrap().0 < request.required_free_bytes);
    let verdict = Untracked {
        retention: Some(request.clone()),
        ..Default::default()
    };
    sweep(
        root.path(),
        SystemTime::now(),
        Instant::now() + Duration::from_secs(10),
        Some(&verdict),
        &|| super::disk_usage(root.path()),
        &|| {},
    );
    assert!(!oldest.exists());
    assert!(newest.exists());
    assert!(protected.exists());
    assert!(super::disk_usage(root.path()).unwrap().0 >= request.required_free_bytes);
}

#[test]
fn unavailable_projection_does_not_veto_locally_unreferenced_model_reclamation() {
    // Catches optional Controller evidence turning a full cache into a permanent
    // storage blocker. Real links, installation plans and this claim's objects stay.
    for projection in [
        None,
        Some(b"{".to_vec()),
        Some({
            let mut request = retention();
            request.expires_at = 0;
            serde_json::to_vec(&request).unwrap()
        }),
    ] {
        let root = tempfile::tempdir().unwrap();
        if let Some(body) = projection {
            file(&root.path().join("store-retention.json"), &body);
        }
        let store = root.path().join("distribution/models");
        let orphan = file(&store.join(OBJECT), b"unused");
        let shared = file(&store.join(OTHER), b"shared");
        let claimed = file(&store.join(THIRD), b"claim");
        let installation = root.path().join("installations").join(INSTALLATION);
        fs::create_dir_all(&installation).unwrap();
        fs::hard_link(&shared, installation.join("model")).unwrap();
        assert_eq!(super::unused_model_bytes(root.path()), Some(11));
        let _disk = super::fake_disk::FakeDisk::mount(
            root.path(),
            super::fake_disk::stored_bytes(root.path()) + 2,
        );
        assert_eq!(super::disk_usage(root.path()).unwrap().0, 2);
        assert_eq!(
            super::make_room(root.path(), 8, &[THIRD.to_owned()].into()),
            6
        );
        assert_eq!(super::disk_usage(root.path()).unwrap().0, 8);
        assert!(!orphan.exists());
        assert!(shared.exists() && claimed.exists());
        assert_eq!(
            super::make_room(root.path(), 8, &[THIRD.to_owned()].into()),
            0
        );
    }
}

#[test]
fn between_claims_pressure_reclaims_without_a_current_projection_and_reports_bytes() {
    // Catches make_room being fixed while the autonomous demand/low-space path
    // still requires a current inventory verdict. No per-object inventory is used.
    let root = tempfile::tempdir().unwrap();
    let orphan = file(
        &root.path().join("distribution/models").join(OBJECT),
        b"unused",
    );
    let swept = sweep(
        root.path(),
        SystemTime::now(),
        Instant::now() + Duration::from_secs(10),
        None,
        &|| SHORT,
        &|| {},
    );
    assert_eq!((swept.entries, swept.bytes), (1, 6));
    assert!(!orphan.exists());
}

#[test]
fn expired_projection_still_protects_content_but_cannot_order_removals() {
    let root = tempfile::tempdir().unwrap();
    let protected = file(
        &root.path().join("distribution/models").join(OBJECT),
        b"protected",
    );
    let unused = file(
        &root.path().join("distribution/models").join(OTHER),
        b"unused",
    );
    let mut request = retention();
    request.expires_at = 0;
    request.protected_model_objects = vec![OBJECT.to_owned()];
    request.remove_model_objects = vec![OTHER.to_owned()];
    file(
        &root.path().join("store-retention.json"),
        &serde_json::to_vec(&request).unwrap(),
    );
    assert!(super::read_retention(root.path()).is_none());
    assert_eq!(super::unused_model_bytes(root.path()), Some(6));
    let pass = |disk: &dyn Fn() -> Option<(u64, u64)>| {
        sweep(
            root.path(),
            SystemTime::now(),
            Instant::now() + Duration::from_secs(10),
            None,
            disk,
            &|| {},
        )
    };
    assert_eq!(pass(&|| ROOMY).entries, 0);
    assert!(unused.exists());
    assert_eq!(pass(&|| SHORT).bytes, 6);
    assert!(!unused.exists() && protected.exists());
}

#[test]
fn requested_model_objects_leave_without_pressure_but_shared_and_protected_bytes_stay() {
    // Catches a pressure-only collector that acknowledges a fleet request
    // without removing it, or an explicit request bypassing local references.
    let root = tempfile::tempdir().unwrap();
    let store = root.path().join("distribution/models");
    let exclusive = file(&store.join(OBJECT), b"finished");
    let shared = file(&store.join(OTHER), b"shared");
    let protected = file(&store.join(THIRD), b"pinned");
    let installation = root.path().join("installations").join(INSTALLATION);
    fs::create_dir_all(&installation).unwrap();
    fs::hard_link(&shared, installation.join("model")).unwrap();
    let mut verdict = verdict(&[], None);
    let retention = verdict.retention.as_mut().unwrap();
    retention.remove_model_objects = vec![OBJECT.into(), OTHER.into(), THIRD.into()];
    retention.protected_model_objects = vec![THIRD.into()];
    let swept = swept_with(root.path(), SystemTime::now(), &verdict);
    assert_eq!(swept.bytes, 8);
    assert!(!exclusive.exists());
    assert!(shared.exists() && protected.exists());
    // The real inventory scanner confirms exact absence; replay removes no
    // extra files and never counts protected bytes as reclaimed.
    let objects = super::model_objects(root.path()).unwrap();
    assert_eq!(objects.len(), 2);
    assert_eq!(
        swept_with(root.path(), SystemTime::now(), &verdict).bytes,
        0
    );
}

#[test]
fn incomplete_object_inventory_is_unknown_instead_of_false_absence() {
    // Catches filtering an unreadable or invalid digest path out of a scan,
    // which would let the Controller report cleanup without exact evidence.
    let root = tempfile::tempdir().unwrap();
    let store = root.path().join("distribution/models");
    file(&store.join(OBJECT), b"weights");
    std::os::unix::fs::symlink(root.path().join("missing"), store.join(OTHER)).unwrap();
    assert!(super::model_objects(root.path()).is_none());
}

#[test]
fn a_retained_projection_cannot_replay_an_explicit_removal_without_a_current_verdict() {
    // Catches retaining old protections accidentally retaining removal authority.
    let root = tempfile::tempdir().unwrap();
    let object = file(
        &root.path().join("distribution/models").join(OBJECT),
        b"cache",
    );
    let mut request = retention();
    request.remove_model_objects = vec![OBJECT.to_owned()];
    file(
        &root.path().join("store-retention.json"),
        &serde_json::to_vec(&request).unwrap(),
    );
    let swept = sweep(
        root.path(),
        SystemTime::now(),
        Instant::now() + Duration::from_secs(10),
        None,
        &|| ROOMY,
        &|| {},
    );
    assert_eq!(swept.entries, 0);
    assert!(object.exists());
}

#[test]
fn inventory_rotates_past_protected_prefix_and_only_judges_its_observed_batch() {
    use vonk_agent_protocol::generated::InventoryVerdict;
    for count in [4096, 4097, 8193] {
        let root = tempfile::tempdir().unwrap();
        authorize_retention(root.path());
        let ids: Vec<_> = (1..=count).map(uuid::Uuid::from_u128).collect();
        for id in &ids {
            file(
                &root
                    .path()
                    .join("installations")
                    .join(id.to_string())
                    .join("x"),
                b"tail",
            );
            file(
                &root.path().join("runs").join(id.to_string()).join("x"),
                b"tail",
            );
        }
        let tail = *ids.last().unwrap();
        let mut seen = BTreeSet::new();
        let mut seen_runs = BTreeSet::new();
        // Each call reloads the on-disk cursor, as the real inventory subprocess
        // does after restart. The protected prefix is never removed.
        for turn in 0..8 {
            let installations = super::installation_names(root.path()).unwrap();
            let runs = super::run_scope_names(root.path()).unwrap();
            let sizes = super::installation_sizes(root.path(), &installations).unwrap();
            assert_eq!(
                sizes
                    .iter()
                    .map(|item| item.installation_id)
                    .collect::<Vec<_>>(),
                installations
            );
            seen.extend(installations.iter().copied());
            seen_runs.extend(runs.iter().copied());
            let verdict = super::LatestVerdict::default();
            verdict.record(
                Some(InventoryVerdict {
                    untracked_installations: installations
                        .iter()
                        .copied()
                        .filter(|id| *id == tail)
                        .collect(),
                    untracked_runs: Some(runs.iter().copied().filter(|id| *id == tail).collect()),
                    store_retention: Some(retention()),
                }),
                &installations,
                &runs,
            );
            let now = if turn < 4 {
                SystemTime::now()
            } else {
                after_grace()
            };
            swept_with(root.path(), now, &verdict.fresh().unwrap());
        }
        assert_eq!(seen, ids.iter().copied().collect());
        assert_eq!(seen_runs, seen);
        assert!(
            !root
                .path()
                .join("installations")
                .join(tail.to_string())
                .exists()
        );
        assert!(!root.path().join("runs").join(tail.to_string()).exists());
        for id in &ids[..ids.len() - 1] {
            assert!(
                root.path()
                    .join("installations")
                    .join(id.to_string())
                    .exists()
            );
            assert!(root.path().join("runs").join(id.to_string()).exists());
        }
    }
}

#[test]
fn deleted_installation_directories_are_absent_on_the_next_inventory() {
    // Wrong implementation: a cached install receipt keeps reporting an
    // installation after its directory is removed, poisoning every load.
    let root = tempfile::tempdir().unwrap();
    let installation = root.path().join("installations").join(INSTALLATION);
    file(&installation.join("spec.json"), b"{}");
    assert_eq!(super::installation_names(root.path()).unwrap().len(), 1);
    assert_eq!(super::installation_sizes(root.path()).unwrap().len(), 1);
    fs::remove_dir_all(installation).unwrap();
    assert_eq!(super::installation_names(root.path()), Some(vec![]));
    assert_eq!(super::installation_sizes(root.path()), Some(vec![]));
}
