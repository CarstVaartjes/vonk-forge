use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime},
};

use super::{GRACE, LOW_FREE_PERCENT, RESERVE_FLOOR_BYTES, RESERVE_PERCENT, STORES, Swept, sweep};

const OBJECT: &str = "1111111111111111111111111111111111111111111111111111111111111111";
const OTHER: &str = "2222222222222222222222222222222222222222222222222222222222222222";
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
    sweep(
        root,
        now,
        Instant::now() + Duration::from_secs(60),
        disk,
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
    let store = root.path().join("distribution/models");
    let linked = file(&store.join(OBJECT), b"weights");
    let orphan = file(&store.join(OTHER), b"superseded");
    // The production trigger: an uninstall without model cleanup removed the
    // installation's link, so only the store's own name remains.
    let installation = root.path().join("installations/a/models/sel");
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
fn a_model_object_an_installation_plan_names_stays_while_the_installation_does() {
    let root = tempfile::tempdir().unwrap();
    let store = root.path().join("distribution/models");
    let object = file(&store.join(OBJECT), b"weights");
    // A private copy (from before files were shared, or where a link cannot be
    // made) leaves the object unlinked; every start still prepares from it.
    let installation = root.path().join("installations/a");
    file(
        &installation.join("models/sel/model.safetensors"),
        b"weights",
    );
    let plan = format!(r#"{{"artifacts":[{{"sha256":"{OBJECT}"}}]}}"#);
    file(&installation.join("spec.json"), plan.as_bytes());

    assert_eq!(run(root.path(), after_grace(), &|| SHORT).entries, 0);
    assert!(object.exists());

    // An unreadable plan could name any object; scratch still leaves.
    fs::write(installation.join("spec.json"), b"{").unwrap();
    let partial = file(&store.join(format!("{OTHER}.partial")), b"prefix");
    assert_eq!(run(root.path(), after_grace(), &|| SHORT).entries, 1);
    assert!(object.exists() && !partial.exists());

    // Uninstalled: the object has no owner left.
    fs::remove_dir_all(&installation).unwrap();
    assert_eq!(run(root.path(), SystemTime::now(), &|| SHORT).entries, 1);
    assert!(!object.exists());
}

#[test]
fn an_ended_transfer_checkpoint_and_isolated_entries_leave_after_the_grace() {
    let root = tempfile::tempdir().unwrap();
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
fn a_base_image_is_a_cache_no_build_holding_it_loses() {
    let root = tempfile::tempdir().unwrap();
    let images = root.path().join("base-images/sha256");
    let image = images.join(OBJECT);
    file(&image.join("image.oci.tar"), b"base");
    let lock = file(&images.join(format!("{OBJECT}-lock")), b"");

    assert_eq!(run(root.path(), after_grace(), &|| ROOMY).entries, 0);
    // A build holding the lock is using the image now.
    let held = crate::held_lock::HeldLock::try_exclusive(fs::File::open(&lock).unwrap()).unwrap();
    assert_eq!(run(root.path(), SystemTime::now(), &|| SHORT).entries, 0);
    assert!(image.exists());
    drop(held);

    assert_eq!(run(root.path(), SystemTime::now(), &|| SHORT).entries, 1);
    assert!(!image.exists() && !lock.exists());
}

#[test]
fn retired_stores_are_emptied() {
    let root = tempfile::tempdir().unwrap();
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
    let installation = file(&root.path().join("installations/a/spec.json"), b"{}");
    let run_scope = file(&root.path().join("runs/r/outputs/x"), b"out");
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
fn a_pass_past_its_deadline_removes_nothing_and_says_more_remain() {
    let root = tempfile::tempdir().unwrap();
    let orphan = file(&root.path().join("builds/b/image.docker.tar"), b"x");

    let swept = sweep(
        root.path(),
        after_grace(),
        Instant::now(),
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
