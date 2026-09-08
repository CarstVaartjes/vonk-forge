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

fn schema2_dual_plan() -> CompiledExecutionPlan {
    let mut value: Value = serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    value["runtime"]["placement"] = json!({
        "endpoint_address": null, "rank": 1, "role": "worker", "world_size": 2,
        "local_address": "192.168.100.11", "master_address": "192.168.100.10",
        "master_port": 29500, "port": 8000, "reserved_memory_bytes": 68719476736_u64
    });
    value["security"]["network_mode"] = json!("bridge");
    value["topology"] = json!({
        "name": "dual", "mode": "distributed", "backend": "nccl",
        "node_count": 2, "world_size": 2, "rank": 1, "role": "worker"
    });
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

fn identity(plan: &CompiledExecutionPlan) -> RecipeRunStartIdentity {
    RecipeRunStartIdentity {
        mapping_generation: 3,
        mapping_id: "11111111-1111-4111-8111-111111111111".parse().unwrap(),
        recipe_content_sha256: plan.identity.recipe_revision_sha256.clone(),
        recipe_revision_id: "22222222-2222-4222-8222-222222222222".parse().unwrap(),
        run_generation: 2,
    }
}

#[test]
fn unbound_install_to_bound_start_retains_exact_inspection_arguments() {
    let root = tempdir().unwrap();
    let mut installed = schema2_dual_plan();
    installed.topology.name = "solo".into();
    installed.topology.mode = "single".parse().unwrap();
    installed.topology.backend = "local".into();
    installed.topology.node_count = 1;
    installed.topology.world_size = 1;
    installed.topology.rank = 0;
    installed.topology.role = "entrypoint".into();
    installed.runtime.placement.rank = 0;
    installed.runtime.placement.role = "entrypoint".into();
    installed.runtime.placement.world_size = 1;
    installed.runtime.placement.endpoint_address = None;
    installed.runtime.placement.local_address = None;
    installed.runtime.placement.master_address = None;
    installed.runtime.placement.master_port = None;
    installed.security.network_mode = "none".parse().unwrap();
    installed.validate().unwrap();
    persist_plan(root.path(), &installed);

    let mut started = installed.clone();
    started.runtime.placement.endpoint_address = Some("192.168.1.211".parse().unwrap());
    started.runtime.placement.reserved_memory_bytes += 1024;
    started.security.network_mode = "bridge".parse().unwrap();
    started.validate().unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
        huggingface_curl_config: None,
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
    let inspections = runtime.recipe_run_inspection_plans().unwrap();
    assert_eq!(inspections.len(), 1);
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
fn retained_inspection_preserves_live_tmp_and_cache_but_actual_start_resets_tmp() {
    let root = tempdir().unwrap();
    let plan = schema2_dual_plan();
    persist_plan(root.path(), &plan);
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
        huggingface_curl_config: None,
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
            plan.runtime_image.registry_manifest_digest.clone().unwrap(),
            plan.runtime_image.platform_manifest_digest.clone(),
            plan.runtime_image.local_image_reference.clone(),
        ]
    );
    assert_eq!(fs::read(&marker).unwrap(), b"live kernel workspace");
    assert_eq!(fs::read(&cache).unwrap(), b"persistent cache");

    runtime.complete_stop(RUN).unwrap();
    runtime
        .prepare_start_with_inspection_identity(&plan, INSTALLATION, RUN, &placement, &identity)
        .unwrap();
    assert!(!marker.exists());
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
    assert!(encoded.len() > 500 * 1024);
    fs::write(directory.join("spec.json"), &encoded).unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: root.path(),
        huggingface_curl_config: None,
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
fn retained_job_stop_accepts_only_a_timeout_within_installed_limit() {
    let root = tempdir().unwrap();
    let mut installed = schema2_dual_plan();
    installed.endpoint = None;
    installed.runtime.placement.port = None;
    installed.security.mounts.push(
        serde_json::from_value(json!({
            "source": "inputs", "target": "/inputs", "read_only": true
        }))
        .unwrap(),
    );
    installed.job = Some(
        serde_json::from_value(json!({
            "interface": "artifact-job", "input": null,
            "output_path": "/outputs", "timeout_seconds": 90
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
        huggingface_curl_config: None,
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
        huggingface_curl_config: None,
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
