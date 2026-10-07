#![forbid(unsafe_code)]

use serde_json::{Value, json};
use std::{fs, path::Path, time::Duration};
use tempfile::tempdir;
use vonk_agent::{
    oci::{OciRuntime, RecipeRunStartIdentity},
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    workloads::{CompiledExecutionPlan, CompiledRuntimePlacement},
};

const INSTALLATION: &str = "cb555393-764b-4eb6-8f15-b416d289428f";
const RUN: &str = "45ea6921-50c9-4971-be2a-4cd04ce05069";
struct NoProcess;
impl ProcessRunner for NoProcess {
    fn run(&self, _: Program, _: &[String], _: Duration) -> Result<ProcessOutput, ProcessError> {
        panic!("retained-plan reconstruction must not launch a process");
    }
}

fn schema2_single_plan() -> CompiledExecutionPlan {
    serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap()
}

fn schema2_dual_plan() -> CompiledExecutionPlan {
    let mut value = serde_json::to_value(schema2_single_plan()).unwrap();
    value["runtime"]["placement"] = json!({
        "endpoint_address": null, "rank": 1, "role": "worker", "world_size": 2,
        "local_address": "192.168.100.11", "master_address": "192.168.100.10",
        "master_port": 29500, "port": 8000, "reserved_memory_bytes": 68719476736_u64,
        "memory_floor_bytes": 0
    });
    value["security"]["network_mode"] = json!("host");
    value["security"]["gpu"] = json!(true);
    value["topology"] = json!({"name": "dual", "node_count": 2});
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    plan.validate().unwrap();
    plan
}

fn persist_plan(root: &Path, plan: &CompiledExecutionPlan) {
    let directory = root.join("installations").join(INSTALLATION);
    fs::create_dir_all(&directory).unwrap();
    fs::write(
        directory.join("spec.json"),
        serde_json::to_vec(plan).unwrap(),
    )
    .unwrap();
    fs::write(
        directory.join("recipe-content.sha256"),
        &plan.identity.recipe_revision_sha256,
    )
    .unwrap();
}

fn placement(plan: &CompiledExecutionPlan) -> CompiledRuntimePlacement {
    plan.runtime.placement.clone()
}

fn identity(_plan: &CompiledExecutionPlan) -> RecipeRunStartIdentity {
    RecipeRunStartIdentity { run_generation: 2 }
}

#[test]
fn unbound_install_to_bound_start_retains_exact_inspection_arguments() {
    assert_unbound_install_retains_inspection(schema2_single_plan());
    assert_unbound_install_retains_inspection(schema2_dual_plan());
}

fn assert_unbound_install_retains_inspection(mut started: CompiledExecutionPlan) {
    let root = tempdir().unwrap();
    if started.runtime.placement.world_size == 1 {
        started.runtime.placement.endpoint_address = Some("192.168.1.211".parse().unwrap());
        started.security.network_mode = "bridge".parse().unwrap();
    }
    let mut installed = started.clone();
    installed.runtime.placement.endpoint_address = None;
    installed.runtime.placement.local_address = None;
    installed.runtime.placement.master_address = None;
    installed.runtime.placement.master_port = None;
    installed.security.network_mode = "none".parse().unwrap();
    installed.validate().unwrap();
    persist_plan(root.path(), &installed);

    started.runtime.placement.reserved_memory_bytes += 1024;
    started.validate().unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    let launched = runtime
        .prepare_start_with_inspection_identity(
            &started,
            INSTALLATION,
            RUN,
            &placement(&started),
            &identity(&started),
        )
        .unwrap();
    // One historical run with invalid metadata must not prevent the valid
    // managed run from reaching the Controller's observation sweep.
    let invalid_run = "e85c4710-e437-4d12-8191-499596aa2a4c";
    fs::create_dir_all(root.path().join("runs").join(invalid_run)).unwrap();
    let invalid_metadata = root.path().join("run-metadata").join(invalid_run);
    fs::create_dir_all(&invalid_metadata).unwrap();
    fs::write(invalid_metadata.join("lifecycle.json"), b"not-json").unwrap();
    fs::create_dir_all(root.path().join("runs").join("not-a-run-id")).unwrap();
    let inspections = runtime.recipe_run_inspection_plans().unwrap();
    assert_eq!(inspections.len(), 1);
    assert_eq!(inspections[0].run_id.to_string(), RUN);
    assert_eq!(&inspections[0].arguments[4..], launched.main.as_slice());
    assert_eq!(runtime.load_spec(INSTALLATION).unwrap(), installed);

    let retained_path = root
        .path()
        .join("run-metadata")
        .join(RUN)
        .join("runtime.json");
    let mut changed_workload = started.clone();
    changed_workload
        .runtime
        .argv
        .push("--different-workload".into());
    let mut changed_placement = started.clone();
    changed_placement.runtime.placement.endpoint_address = Some("192.168.1.213".parse().unwrap());
    for tampered in [changed_workload, changed_placement] {
        fs::write(&retained_path, serde_json::to_vec(&tampered).unwrap()).unwrap();
        assert!(runtime.recipe_run_inspection_plans().is_err());
    }
    fs::write(&retained_path, b"{}").unwrap();
    assert!(runtime.recipe_run_inspection_plans().is_err());
    fs::remove_file(&retained_path).unwrap();
    assert!(runtime.recipe_run_inspection_plans().is_err());
    std::os::unix::fs::symlink(
        root.path()
            .join("installations")
            .join(INSTALLATION)
            .join("spec.json"),
        &retained_path,
    )
    .unwrap();
    assert!(runtime.recipe_run_inspection_plans().is_err());
}

#[test]
fn retained_inspection_and_agent_preparation_leave_private_tmp_cleanup_to_helper() {
    let root = tempdir().unwrap();
    let plan = schema2_dual_plan();
    persist_plan(root.path(), &plan);
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    let placement = placement(&plan);
    let identity = identity(&plan);
    runtime
        .prepare_start_with_inspection_identity(&plan, INSTALLATION, RUN, &placement, &identity)
        .unwrap();
    let marker = root
        .path()
        .join("runs")
        .join(RUN)
        .join("outputs/tmp/live.marker");
    let cache = root
        .path()
        .join("installations")
        .join(INSTALLATION)
        .join("runtime-cache/live.marker");
    fs::write(&marker, b"live kernel workspace").unwrap();
    fs::write(&cache, b"persistent cache").unwrap();

    runtime
        .prepare_retained_start(&plan, INSTALLATION, RUN, &placement)
        .unwrap();
    runtime
        .prepare_retained_start_with_inspection_identity(
            &plan,
            INSTALLATION,
            RUN,
            &placement,
            &identity,
        )
        .unwrap();
    let inspections = runtime.recipe_run_inspection_plans().unwrap();
    assert_eq!(inspections.len(), 1);
    assert_eq!(inspections[0].endpoint_address, None);
    assert!(
        !inspections[0]
            .arguments
            .iter()
            .any(|value| value == "--publish")
    );
    assert_eq!(
        &inspections[0].arguments[..4],
        &[
            plan.runtime_image.oci_layout_sha256.clone(),
            plan.runtime_image.image_digest.clone(),
            plan.runtime_image.image_digest.clone(),
            plan.runtime_image.local_image_reference(),
        ]
    );
    assert_eq!(fs::read(&marker).unwrap(), b"live kernel workspace");
    assert_eq!(fs::read(&cache).unwrap(), b"persistent cache");

    runtime.complete_stop(RUN).unwrap();
    runtime
        .prepare_start_with_inspection_identity(&plan, INSTALLATION, RUN, &placement, &identity)
        .unwrap();
    assert_eq!(fs::read(marker).unwrap(), b"live kernel workspace");
    assert_eq!(fs::read(cache).unwrap(), b"persistent cache");
}

#[test]
fn real_751_artifact_spec_json_round_trips_through_persisted_loader() {
    let plan: CompiledExecutionPlan = serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_plan_751.json"
    ))
    .unwrap();
    plan.validate().unwrap();
    let root = tempdir().unwrap();
    let directory = root.path().join("installations").join(INSTALLATION);
    fs::create_dir_all(&directory).unwrap();
    let encoded = serde_json::to_vec(&plan).unwrap();
    assert!(encoded.len() > 256 * 1024);
    fs::write(directory.join("spec.json"), &encoded).unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    let loaded = runtime.load_spec(INSTALLATION).unwrap();
    assert_eq!(loaded.artifacts.len(), 751);
}

#[test]
fn observation_errors_preserve_safe_category_without_storage_details() {
    let error = vonk_agent::executor::RecipeObservationError::from(vonk_agent::oci::OciError::Io(
        std::io::Error::other("private path and credential"),
    ));
    assert_eq!(
        error.to_string(),
        "managed recipe run observation failed (storage)"
    );
}

#[test]
fn uninstall_removes_only_its_materialization_despite_unrelated_metadata() {
    let root = tempdir().unwrap();
    let plan = schema2_single_plan();
    persist_plan(root.path(), &plan);
    let installation = root.path().join("installations").join(INSTALLATION);
    let unrelated = root.path().join("installations").join(RUN);
    fs::create_dir_all(&unrelated).unwrap();
    fs::write(unrelated.join("spec.json"), b"invalid unrelated metadata").unwrap();
    let cache = root.path().join("shared-cache-object");
    fs::write(&cache, b"shared model bytes").unwrap();
    let mut expected_bytes = 0;
    for artifact in &plan.artifacts {
        let file = installation
            .join("models")
            .join(&artifact.selection_id)
            .join(&artifact.path);
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        fs::hard_link(&cache, &file).unwrap();
        expected_bytes += fs::metadata(file).unwrap().len();
    }
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    assert_eq!(
        runtime
            .uninstall_with_model_cleanup(
                INSTALLATION,
                &plan.identity.recipe_revision_sha256,
                &plan.artifacts[0].model.content_sha256,
            )
            .unwrap(),
        expected_bytes
    );
    assert!(!installation.exists());
    assert_eq!(
        fs::read(unrelated.join("spec.json")).unwrap(),
        b"invalid unrelated metadata"
    );
    assert_eq!(fs::read(cache).unwrap(), b"shared model bytes");
}

/// An installation of the fixture plan whose files are hard links to objects of
/// the Spark's shared store, as a new install makes them. Returns the store
/// objects of the plan's first model.
fn installation_linked_to_the_store(root: &Path, plan: &CompiledExecutionPlan) -> Vec<String> {
    let installation = root.join("installations").join(INSTALLATION);
    let store = root.join("distribution").join("models");
    fs::create_dir_all(&store).unwrap();
    let model = &plan.artifacts[0].model.content_sha256;
    let mut objects = Vec::new();
    for artifact in &plan.artifacts {
        let object = store.join(&artifact.sha256);
        if !object.exists() {
            fs::write(&object, b"model bytes").unwrap();
        }
        let file = installation
            .join("models")
            .join(&artifact.selection_id)
            .join(&artifact.path);
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        fs::hard_link(&object, &file).unwrap();
        if &artifact.model.content_sha256 == model && !objects.contains(&artifact.sha256) {
            objects.push(artifact.sha256.clone());
        }
    }
    objects
}

#[test]
fn uninstall_with_model_cleanup_frees_the_store_objects_nothing_else_links() {
    let root = tempdir().unwrap();
    let plan = schema2_single_plan();
    persist_plan(root.path(), &plan);
    let objects = installation_linked_to_the_store(root.path(), &plan);
    let store = root.path().join("distribution").join("models");
    let unrelated = store.join("e".repeat(64));
    fs::write(&unrelated, b"another model").unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };

    // Removing an installation alone leaves its model bytes in the store.
    runtime
        .uninstall(INSTALLATION, &plan.identity.recipe_revision_sha256)
        .unwrap();
    assert!(objects.iter().all(|digest| store.join(digest).exists()));

    assert_eq!(
        runtime.reclaim_unshared_model_objects(&objects),
        (objects.len() * "model bytes".len()) as u64
    );
    assert!(objects.iter().all(|digest| !store.join(digest).exists()));
    assert!(unrelated.exists());
}

#[test]
fn uninstall_with_model_cleanup_keeps_a_store_object_another_installation_links() {
    use std::os::unix::fs::MetadataExt;

    let root = tempdir().unwrap();
    let plan = schema2_single_plan();
    persist_plan(root.path(), &plan);
    let objects = installation_linked_to_the_store(root.path(), &plan);
    let store = root.path().join("distribution").join("models");
    let other = root.path().join("installations").join(RUN);
    fs::create_dir_all(&other).unwrap();
    fs::hard_link(store.join(&objects[0]), other.join("model")).unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    let stored = runtime
        .model_store_objects(
            INSTALLATION,
            &plan.identity.recipe_revision_sha256,
            &plan.artifacts[0].model.content_sha256,
        )
        .unwrap();
    assert_eq!(stored.len(), objects.len());

    runtime
        .uninstall_with_model_cleanup(
            INSTALLATION,
            &plan.identity.recipe_revision_sha256,
            &plan.artifacts[0].model.content_sha256,
        )
        .unwrap();

    assert_eq!(fs::metadata(store.join(&objects[0])).unwrap().nlink(), 2);
    assert_eq!(fs::read(other.join("model")).unwrap(), b"model bytes");
}

#[test]
fn uninstall_validates_storage_without_requiring_launchable_placement() {
    let root = tempdir().unwrap();
    let mut plan = schema2_dual_plan();
    plan.runtime.placement.local_address = None;
    assert!(plan.validate().is_err());
    persist_plan(root.path(), &plan);
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    runtime
        .uninstall(INSTALLATION, &plan.identity.recipe_revision_sha256)
        .unwrap();
    assert!(
        !root
            .path()
            .join("installations")
            .join(INSTALLATION)
            .exists()
    );
}

#[test]
fn uninstall_rejects_its_own_invalid_metadata_identity_and_artifact_paths() {
    for defect in ["malformed", "identity", "artifact-path"] {
        let root = tempdir().unwrap();
        let mut plan = schema2_single_plan();
        let recipe = plan.identity.recipe_revision_sha256.clone();
        match defect {
            "identity" => plan.identity.recipe_revision_sha256 = "a".repeat(64),
            "artifact-path" => plan.artifacts[0].path = "../../outside".to_owned(),
            _ => {}
        }
        persist_plan(root.path(), &plan);
        let installation = root.path().join("installations").join(INSTALLATION);
        fs::write(installation.join("recipe-content.sha256"), &recipe).unwrap();
        if defect == "malformed" {
            fs::write(installation.join("spec.json"), b"{}").unwrap();
        }
        let runtime = OciRuntime {
            runner: &NoProcess,
            data_root: root.path(),
        };
        assert!(
            runtime.uninstall(INSTALLATION, &recipe).is_err(),
            "{defect}"
        );
        assert!(installation.exists());
    }
}

#[test]
fn retained_job_stop_accepts_only_a_timeout_within_installed_limit() {
    let root = tempdir().unwrap();
    let mut installed = schema2_single_plan();
    installed.endpoint = None;
    installed.runtime.placement.port = None;
    installed.security.mounts.push(
        serde_json::from_value(json!({
            "source": "inputs", "target": "/inputs"
        }))
        .unwrap(),
    );
    installed.job = Some(
        serde_json::from_value(json!({
            "interface": "artifact-job", "input": null, "timeout_seconds": 90
        }))
        .unwrap(),
    );
    installed.validate().unwrap();
    let lifecycle_placement = placement(&installed);
    persist_plan(root.path(), &installed);
    let metadata = root.path().join("run-metadata").join(RUN);
    fs::create_dir_all(&metadata).unwrap();
    fs::write(
        metadata.join("lifecycle.json"),
        serde_json::to_vec(&json!({
            "installation_id": INSTALLATION, "placement": lifecycle_placement,
        }))
        .unwrap(),
    )
    .unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    let mut retained = installed;
    retained.job.as_mut().unwrap().timeout_seconds = 30;
    fs::write(
        metadata.join("runtime.json"),
        serde_json::to_vec(&retained).unwrap(),
    )
    .unwrap();
    assert!(runtime.prepare_stop(RUN).is_ok());
    retained.job.as_mut().unwrap().timeout_seconds = 91;
    fs::write(
        metadata.join("runtime.json"),
        serde_json::to_vec(&retained).unwrap(),
    )
    .unwrap();
    assert!(runtime.prepare_stop(RUN).is_err());
}

#[test]
fn retained_lifecycle_requires_all_canonical_placement_fields() {
    let root = tempdir().unwrap();
    let plan = schema2_dual_plan();
    persist_plan(root.path(), &plan);
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    runtime
        .prepare_start_with_inspection_identity(
            &plan,
            INSTALLATION,
            RUN,
            &plan.runtime.placement,
            &identity(&plan),
        )
        .unwrap();
    runtime.recipe_run_inspection_plans().unwrap();
    let path = root
        .path()
        .join("run-metadata")
        .join(RUN)
        .join("lifecycle.json");
    let lifecycle: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    for field in [
        "endpoint_address",
        "local_address",
        "master_address",
        "master_port",
        "port",
    ] {
        let mut incomplete = lifecycle.clone();
        incomplete["placement"]
            .as_object_mut()
            .unwrap()
            .remove(field);
        fs::write(&path, serde_json::to_vec(&incomplete).unwrap()).unwrap();
        assert!(
            runtime.recipe_run_inspection_plans().is_err(),
            "missing {field}"
        );
    }
}

#[test]
fn unbound_compiled_placement_requires_addresses_only_at_execution() {
    let mut plan = schema2_dual_plan();
    plan.runtime.placement.local_address = None;
    plan.runtime.placement.master_address = None;
    plan.runtime.placement.master_port = None;
    plan.security.network_mode = "none".parse().unwrap();
    plan.validate().unwrap();
    assert!(plan.runtime.placement.validate_bound().is_err());
    let bound = schema2_dual_plan();
    bound.runtime.placement.validate_bound().unwrap();
}

fn native_observation_plan(root: &Path) -> CompiledExecutionPlan {
    let mut plan = schema2_single_plan();
    persist_plan(root, &plan);
    plan.runtime.placement.endpoint_address = Some("192.168.1.211".parse().unwrap());
    plan.security.network_mode = "bridge".parse().unwrap();
    plan.validate().unwrap();
    plan
}

/// The native retained Start producer, node-bound SQLite checkpoint and a new
/// runtime/store instance on every page share the same exact generation.
#[test]
fn sixty_five_native_starts_survive_durable_pagination_and_restart() {
    use std::collections::BTreeSet;
    use vonk_agent::state::StateStore;
    let root = tempdir().unwrap();
    let plan = native_observation_plan(root.path());
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    let mut expected = BTreeSet::new();
    for _ in 0..65 {
        let id = uuid::Uuid::new_v4();
        runtime
            .prepare_start_with_inspection_identity(
                &plan,
                INSTALLATION,
                &id.to_string(),
                &placement(&plan),
                &identity(&plan),
            )
            .unwrap();
        expected.insert(id);
    }
    let database = root.path().join("agent-state.sqlite");
    let mut observed = BTreeSet::new();
    let mut pages = 0;
    loop {
        let mut state = StateStore::open(&database, "observation-test-node").unwrap();
        let checkpoint = state.observation_checkpoint().unwrap();
        let runtime = OciRuntime {
            runner: &NoProcess,
            data_root: root.path(),
        };
        let page = runtime
            .recipe_run_inspection_page(checkpoint.as_ref())
            .unwrap();
        assert!(page.plans.len() <= 64);
        assert!(page.failures.is_empty());
        assert!(!page.empty_snapshot_safe);
        for plan in &page.plans {
            assert_eq!(plan.run_generation, 2);
            assert!(
                observed.insert(plan.run_id),
                "restart must resume rather than replay an already delivered page"
            );
        }
        state
            .save_observation_checkpoint(page.checkpoint.as_ref())
            .unwrap();
        assert_eq!(state.observation_checkpoint().unwrap(), page.checkpoint);
        pages += 1;
        if page.complete {
            break;
        }
        assert!(pages < 70);
    }
    assert!(pages >= 2);
    assert_eq!(observed, expected);
}

/// Reopen the existing SQL key with its original UTC JSON spelling. Reading
/// must not rewrite the retained bytes, and the native witness must still let
/// the next page continue the exact producer-created runs without replay.
#[test]
fn legacy_checkpoint_json_reopens_without_rewrite_and_resumes_native_scan() {
    use std::collections::BTreeSet;
    use vonk_agent::state::StateStore;
    let root = tempdir().unwrap();
    let plan = native_observation_plan(root.path());
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    let mut expected = BTreeSet::new();
    for _ in 0..9 {
        let id = uuid::Uuid::new_v4();
        runtime
            .prepare_start_with_inspection_identity(
                &plan,
                INSTALLATION,
                &id.to_string(),
                &placement(&plan),
                &identity(&plan),
            )
            .unwrap();
        expected.insert(id);
    }
    let first = runtime.recipe_run_inspection_page(None).unwrap();
    assert!(!first.complete);
    assert!(!first.plans.is_empty());
    assert!(first.failures.is_empty());
    let mut checkpoint = first.checkpoint.clone().unwrap();
    assert!(checkpoint.witness.is_some());
    // This exact nine-digit UTC spelling was emitted by the preceding native
    // chrono owner; its final 789ns must survive the canonical reader.
    checkpoint.started_at = "2026-10-07T00:00:00.123456789Z".to_owned();
    let database = root.path().join("agent-state.sqlite");
    let mut state = StateStore::open(&database, "observation-test-node").unwrap();
    state
        .save_observation_checkpoint(Some(&checkpoint))
        .unwrap();
    drop(state);

    // This fixture uses the actual native producer's record, with the spelling
    // emitted by the preceding DateTime<Utc> checkpoint owner. No alternate
    // reader or checkpoint migration is introduced in production.
    let connection = rusqlite::Connection::open(&database).unwrap();
    let stored: String = connection
        .query_row(
            "SELECT value FROM metadata WHERE key='recipe_observation_scan_v1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let legacy: Value = serde_json::from_str(&stored).unwrap();
    assert_eq!(
        legacy["started_at"],
        json!("2026-10-07T00:00:00.123456789Z")
    );
    assert!(legacy.as_object().unwrap().contains_key("metadata_stamp"));
    assert!(legacy.as_object().unwrap().contains_key("witness"));
    let legacy_bytes = serde_json::to_string(&legacy).unwrap();
    assert!(legacy_bytes.len() <= 16 * 1024);
    connection
        .execute(
            "UPDATE metadata SET value=?1 WHERE key='recipe_observation_scan_v1'",
            [&legacy_bytes],
        )
        .unwrap();
    drop(connection);

    let mut state = StateStore::open(&database, "observation-test-node").unwrap();
    let reopened = state.observation_checkpoint().unwrap().unwrap();
    assert_eq!(reopened, checkpoint);
    let connection = rusqlite::Connection::open(&database).unwrap();
    let unchanged: String = connection
        .query_row(
            "SELECT value FROM metadata WHERE key='recipe_observation_scan_v1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(unchanged, legacy_bytes);
    drop(connection);
    let mut observed: BTreeSet<_> = first.plans.iter().map(|plan| plan.run_id).collect();
    let mut retained = Some(reopened);
    let mut pages = 0;
    loop {
        let page = runtime
            .recipe_run_inspection_page(retained.as_ref())
            .unwrap();
        assert!(page.failures.is_empty());
        assert!(!page.empty_snapshot_safe);
        for plan in &page.plans {
            assert_eq!(plan.run_generation, 2);
            assert!(
                observed.insert(plan.run_id),
                "retained witness must not replay delivered runs"
            );
        }
        state
            .save_observation_checkpoint(page.checkpoint.as_ref())
            .unwrap();
        retained = state.observation_checkpoint().unwrap();
        pages += 1;
        if page.complete {
            break;
        }
        assert!(pages < 12);
        drop(state);
        state = StateStore::open(&database, "observation-test-node").unwrap();
    }
    assert_eq!(observed, expected);
    assert!(retained.is_none());
    // Shared Python/native corpus checks the actual persisted restart boundary,
    // including ISO spellings accepted by Python but refused by RFC3339.
    let cases: Value = serde_json::from_str(include_str!(
        "../../../../agent_protocol/tests/fixtures/runtime_scan_cutoffs.json"
    ))
    .unwrap();
    let connection = rusqlite::Connection::open(&database).unwrap();
    for (category, accepted) in [("valid", true), ("invalid", false)] {
        for cutoff in cases[category].as_array().unwrap() {
            let mut record = legacy.clone();
            record["started_at"] = cutoff.clone();
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES ('recipe_observation_scan_v1',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [serde_json::to_string(&record).unwrap()],
            ).unwrap();
            let restarted = StateStore::open(&database, "observation-test-node").unwrap();
            let parsed = restarted.observation_checkpoint();
            assert_eq!(parsed.is_ok(), accepted, "cutoff: {cutoff}");
            if let Ok(Some(checkpoint)) = parsed {
                assert_eq!(checkpoint.started_at, cutoff.as_str().unwrap());
                assert!(
                    runtime
                        .recipe_run_inspection_page(Some(&checkpoint))
                        .is_ok()
                );
            }
        }
    }
}

fn historical_runs(root: &Path, count: usize) {
    for _ in 0..count {
        fs::create_dir_all(root.join("runs").join(uuid::Uuid::new_v4().to_string())).unwrap();
    }
}

#[test]
fn history_beyond_4096_keeps_cursor_progress_during_new_arrivals_and_restart() {
    use vonk_agent::state::StateStore;
    let root = tempdir().unwrap();
    historical_runs(root.path(), 4097);
    // Choose the actual filesystem iteration tail, rather than assuming a
    // newly created UUID sorts last in getdents order.
    let tail_id = fs::read_dir(root.path().join("runs"))
        .unwrap()
        .nth(4096)
        .unwrap()
        .unwrap()
        .file_name()
        .into_string()
        .unwrap();
    let plan = native_observation_plan(root.path());
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    runtime
        .prepare_start_with_inspection_identity(
            &plan,
            INSTALLATION,
            &tail_id,
            &placement(&plan),
            &identity(&plan),
        )
        .unwrap();
    let database = root.path().join("agent-state.sqlite");
    let mut found = false;
    let mut pages = 0;
    loop {
        let mut state = StateStore::open(&database, "observation-test-node").unwrap();
        let checkpoint = state.observation_checkpoint().unwrap();
        let runtime = OciRuntime {
            runner: &NoProcess,
            data_root: root.path(),
        };
        let page = runtime
            .recipe_run_inspection_page(checkpoint.as_ref())
            .unwrap();
        found |= page
            .plans
            .iter()
            .any(|plan| plan.run_id.to_string() == tail_id);
        assert!(!page.empty_snapshot_safe);
        state
            .save_observation_checkpoint(page.checkpoint.as_ref())
            .unwrap();
        pages += 1;
        if page.complete {
            break;
        }
        // A root stamp changes at every page. Resetting to zero would never
        // reach EOF/history tail; witnessed filesystem progress must survive.
        historical_runs(root.path(), 1);
        assert!(pages < 80, "new arrivals must not rewind every page");
    }
    assert!(pages > 16);
    assert!(
        found,
        "the retained Start must reach the consumer beyond old history"
    );
}

#[test]
fn complete_empty_history_uses_initial_cutoff_and_partial_scan_is_never_empty() {
    let root = tempdir().unwrap();
    historical_runs(root.path(), 4097);
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    let first = runtime.recipe_run_inspection_page(None).unwrap();
    assert!(!first.complete);
    assert!(!first.empty_snapshot_safe);
    let original_cutoff = first.observed_at;
    let mut checkpoint = first.checkpoint;
    loop {
        let page = runtime
            .recipe_run_inspection_page(checkpoint.as_ref())
            .unwrap();
        checkpoint = page.checkpoint;
        if page.complete {
            assert!(page.empty_snapshot_safe);
            assert_eq!(page.observed_at, original_cutoff);
            break;
        }
        assert!(!page.empty_snapshot_safe);
    }
}

#[test]
fn interrupted_empty_scan_cannot_erase_a_native_start_arriving_between_pages() {
    use vonk_agent::state::StateStore;
    let root = tempdir().unwrap();
    historical_runs(root.path(), 4097);
    let database = root.path().join("agent-state.sqlite");
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    let first = runtime.recipe_run_inspection_page(None).unwrap();
    let mut state = StateStore::open(&database, "observation-test-node").unwrap();
    state
        .save_observation_checkpoint(first.checkpoint.as_ref())
        .unwrap();
    drop(state);
    let plan = native_observation_plan(root.path());
    runtime
        .prepare_start_with_inspection_identity(
            &plan,
            INSTALLATION,
            RUN,
            &placement(&plan),
            &identity(&plan),
        )
        .unwrap();
    let mut found = false;
    // Arrivals before the cursor may belong to the next cycle. Both cycles
    // must stay truthful; the next complete cycle must recover the new run.
    for _ in 0..100 {
        let mut state = StateStore::open(&database, "observation-test-node").unwrap();
        let checkpoint = state.observation_checkpoint().unwrap();
        let page = runtime
            .recipe_run_inspection_page(checkpoint.as_ref())
            .unwrap();
        assert!(!page.empty_snapshot_safe);
        found |= page.plans.iter().any(|plan| plan.run_id.to_string() == RUN);
        state
            .save_observation_checkpoint(page.checkpoint.as_ref())
            .unwrap();
        if page.complete && found {
            return;
        }
    }
    panic!("the original scan and subsequent complete cycle must recover the new Start");
}

#[test]
fn damaged_and_symlinked_runs_do_not_hide_exact_healthy_generation_on_later_pages() {
    use std::os::unix::fs::symlink;
    use vonk_agent::state::StateStore;
    let root = tempdir().unwrap();
    let outside = tempdir().unwrap();
    fs::write(outside.path().join("untouched"), b"external state").unwrap();
    let plan = native_observation_plan(root.path());
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
    };
    runtime
        .prepare_start_with_inspection_identity(
            &plan,
            INSTALLATION,
            RUN,
            &placement(&plan),
            &identity(&plan),
        )
        .unwrap();
    for _ in 0..17 {
        let id = uuid::Uuid::new_v4().to_string();
        fs::create_dir_all(root.path().join("runs").join(&id)).unwrap();
        let metadata = root.path().join("run-metadata").join(&id);
        fs::create_dir_all(&metadata).unwrap();
        fs::write(metadata.join("lifecycle.json"), b"not-json").unwrap();
    }
    symlink(
        outside.path(),
        root.path()
            .join("runs")
            .join(uuid::Uuid::new_v4().to_string()),
    )
    .unwrap();
    let database = root.path().join("state.sqlite");
    let mut healthy = 0;
    let mut damaged = 0;
    for _ in 0..30 {
        let mut state = StateStore::open(&database, "observation-test-node").unwrap();
        let checkpoint = state.observation_checkpoint().unwrap();
        let page = runtime
            .recipe_run_inspection_page(checkpoint.as_ref())
            .unwrap();
        assert!(!page.empty_snapshot_safe);
        damaged += page.failures.len();
        for plan in page.plans {
            assert_eq!(plan.run_id.to_string(), RUN);
            assert_eq!(plan.run_generation, 2);
            healthy += 1;
        }
        state
            .save_observation_checkpoint(page.checkpoint.as_ref())
            .unwrap();
        if page.complete {
            break;
        }
    }
    assert_eq!(healthy, 1);
    assert_eq!(damaged, 18);
    assert_eq!(
        fs::read(outside.path().join("untouched")).unwrap(),
        b"external state"
    );
}
