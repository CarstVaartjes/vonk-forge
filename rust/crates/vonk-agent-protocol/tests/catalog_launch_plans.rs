#![forbid(unsafe_code)]

//! Compiled launch plans derived from the real recipe catalog must be accepted
//! by the agent's typed wire model.
//!
//! The Controller's `test_catalog_launch_fixtures.py` compiles a representative
//! recipe per engine, topology and option shape and commits the resulting
//! launch payloads under `agent_protocol/tests/fixtures/catalog-launch`. This
//! test deserialises and validates the very same documents with the production
//! Rust types, so a value the Controller can emit and the agent cannot read
//! fails here rather than on a Spark.

use std::{fs, path::PathBuf};

use serde_json::Value;
use vonk_agent_protocol::compiled_execution_plan::CompiledExecutionPlan;

fn fixture_directory() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../agent_protocol/tests/fixtures/catalog-launch")
}

fn fixture_paths() -> Vec<PathBuf> {
    let mut paths: Vec<PathBuf> = fs::read_dir(fixture_directory())
        .expect("catalog launch fixtures are committed")
        .map(|entry| entry.unwrap().path())
        .filter(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .collect();
    paths.sort();
    paths
}

fn accepted(value: &Value) -> Result<CompiledExecutionPlan, String> {
    let plan: CompiledExecutionPlan =
        serde_json::from_value(value.clone()).map_err(|error| error.to_string())?;
    plan.validate().map_err(|error| error.to_string())?;
    Ok(plan)
}

#[test]
fn every_catalog_launch_fixture_is_accepted_and_round_trips() {
    let paths = fixture_paths();
    assert!(paths.len() >= 20, "fixture set is unexpectedly small");
    for path in paths {
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        let value: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        let plan = accepted(&value).unwrap_or_else(|error| panic!("{name}: {error}"));
        // Nothing the Controller emits may be dropped or invented by the agent.
        assert_eq!(serde_json::to_value(&plan).unwrap(), value, "{name}");
    }
}

#[test]
fn catalog_launch_fixtures_cover_endpoint_and_job_interfaces() {
    let mut endpoints = 0;
    let mut jobs = 0;
    let mut multi_node = 0;
    for path in fixture_paths() {
        let value: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        let plan = accepted(&value).unwrap();
        endpoints += usize::from(plan.endpoint.is_some());
        jobs += usize::from(plan.job.is_some());
        multi_node += usize::from(plan.topology.node_count > 1);
    }
    assert!(endpoints > 0 && jobs > 0 && multi_node > 0);
}

#[test]
fn catalog_launch_fixtures_reject_unknown_authority() {
    for path in fixture_paths() {
        let mut value: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        value["runtime"]
            .as_object_mut()
            .unwrap()
            .insert("shell".into(), Value::Bool(true));
        assert!(accepted(&value).is_err());
    }
}

fn with_environment_name(mut value: Value, name: &str) -> Value {
    value["runtime"]["env"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({ "name": name, "value": "1" }));
    value
}

#[test]
fn environment_names_keep_engine_case_and_stay_shell_safe() {
    let path = fixture_paths().into_iter().next().unwrap();
    let value: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    // Engines such as Ray read case-sensitive names like RAY_memory_usage_threshold.
    for name in ["RAY_memory_usage_threshold", "NCCL_DEBUG", "A1"] {
        assert!(accepted(&with_environment_name(value.clone(), name)).is_ok());
    }
    for name in [
        "lowercase",
        "_LEADING",
        "HAS-DASH",
        "HAS=EQUALS",
        "HAS SPACE",
        "",
    ] {
        assert!(
            accepted(&with_environment_name(value.clone(), name)).is_err(),
            "{name:?}"
        );
    }
}
