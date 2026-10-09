#![cfg(test)]

use super::*;

#[test]
fn fixture_projects_colliding_selection_files_without_flattening() {
    let plan: CompiledExecutionPlan = serde_json::from_value(fixture()).unwrap();
    let invocation = project(&plan, &paths()).unwrap();
    assert_eq!(
        invocation.mounts[0].source,
        std::path::Path::new("/run/vonk/models/primary/config.json")
    );
    assert_eq!(invocation.mounts[0].target, "/models/target/config.json");
    assert_eq!(
        invocation.mounts[1].source,
        std::path::Path::new("/run/vonk/models/dependency-qwen3-8-27b-dspark-b3c99101/config.json")
    );
    assert_eq!(invocation.mounts[1].target, "/models/draft/config.json");
    assert!(invocation.mounts[0].read_only && invocation.mounts[1].read_only);
}

#[test]
fn opaque_argv_stays_byte_for_byte_after_image_boundary() {
    let mut value = fixture();
    let compact_json = format!("{{\"payload\":\"{}\"}}", "x".repeat(4_090));
    let unicode = "🙂".repeat(16_384);
    value["runtime"]["argv"] = json!(["--network", compact_json, "", unicode, "--device"]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let invocation = project(&plan, &paths()).unwrap();
    assert_eq!(&invocation.command[0], "/opt/vonk/bin/vllm");
    assert_eq!(&invocation.command[1..], &plan.runtime.argv);
    let podman = invocation.podman_arguments();
    let image_index = podman
        .iter()
        .position(|item| item == &invocation.image)
        .unwrap();
    assert_eq!(
        &podman[image_index - 2..image_index],
        ["--entrypoint", "/opt/vonk/bin/vllm"]
    );
    let mut expected_post_image = vec!["/opt/vonk/bin/vllm".to_owned()];
    expected_post_image.extend(plan.runtime.argv.clone());
    assert_eq!(&podman[image_index + 1..], expected_post_image.as_slice());
    assert_eq!(invocation.image, plan.runtime_image.local_image_reference());
    assert!(
        podman
            .windows(2)
            .any(|window| window == ["--network", "none"])
    );
    assert!(!podman[..image_index].contains(&"bridge".to_owned()));
    assert!(!podman[..image_index].contains(&"host".to_owned()));
}

#[test]
fn a_many_artifact_runtime_command_is_framed() {
    // Wrong implementations this catches: the retired
    // `MAX_HOST_RUNTIME_ARGUMENTS = 512` count cap refused the live GLM EXL3
    // dual command line before the helper was ever called, and a request
    // byte budget that merely equalled the helper frame budget could not
    // carry the command line the plan itself admitted.
    use super::OciMount;
    use crate::{HostRuntimeAction, HostRuntimeRequest};
    use uuid::Uuid;

    let plan: CompiledExecutionPlan = serde_json::from_value(fixture()).unwrap();
    let mut invocation = project(&plan, &paths()).unwrap();
    // The GLM EXL3 dual recipe selects 149 model files (144 + 5), so the
    // projection emits one read-only bind mount per file plus the outputs,
    // cache and runtime mounts.
    invocation.mounts = (0..152)
        .map(|index| OciMount {
            source: PathBuf::from(format!("/run/vonk/models/shard-{index:03}")),
            target: format!("/models/shard-{index:03}"),
            read_only: true,
        })
        .collect();
    // Its 29 recipe environment entries plus the platform ones.
    invocation.environment = (0..38)
        .map(|index| super::CompiledEnvironmentEntry {
            name: format!("VONK_ENV_{index:02}"),
            value: "1".to_owned(),
        })
        .collect();
    // The adapter's compiled vllm command: the entrypoint, the recipe's 21
    // arguments and the adapter's own engine flags.
    invocation.command = std::iter::once("/opt/vonk/bin/vllm".to_owned())
        .chain((0..96).map(|index| format!("--argument-{index:02}=value")))
        .collect();

    let mut podman = invocation.podman_arguments();
    // `start_arguments_for_paths` splices the run identity in after `run`.
    podman.splice(
        1..1,
        [
            "--name".to_owned(),
            "vonk-run".to_owned(),
            "--restart".to_owned(),
            "no".to_owned(),
        ],
    );
    let mut arguments = vec![
        "a".repeat(64),
        format!("sha256:{}", "b".repeat(64)),
        format!("sha256:{}", "c".repeat(64)),
        format!(
            "localhost/vonk/compiled-runtime-{}@sha256:{}",
            "d".repeat(64),
            "e".repeat(64)
        ),
    ];
    arguments.extend(podman);
    let count = arguments.len();
    assert!(
        count > 512,
        "the live many-artifact command must exceed the old cap, got {count}"
    );

    let start_plan = crate::recipe_start_tests::valid_start_plan();
    let request = HostRuntimeRequest {
        action: HostRuntimeAction::Start,
        fence: Uuid::new_v4(),
        arguments,
        job_plan: None,
        installation_id: None,
        reconciliation_identity: None,
        run_generation: Some(start_plan.run_generation),
        start_plan: Some(start_plan),
        stop_plan: None,
    };
    assert_eq!(request.validate(), Ok(()));
    let body = crate::canonical_json(&request).unwrap();
    assert!(
        body.len() <= crate::MAX_HOST_RUNTIME_REQUEST_BYTES,
        "the plan-admitted command line must fit one bounded helper exchange, got {} bytes",
        body.len()
    );
}

#[test]
fn conflicting_platform_environment_is_rejected() {
    let mut value = fixture();
    value["runtime"]["env"] = json!([{"name":"VONK_RUNTIME_SPEC","value":"/tmp/override"}]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(matches!(
        project(&plan, &paths()),
        Err(CompiledOciError::Invalid(
            "conflicting platform environment"
        ))
    ));
}

#[test]
fn duplicate_materialized_target_is_rejected() {
    let mut value = fixture();
    value["artifacts"][1]["mount"]["target"] = json!("/models/target");
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(matches!(
        project(&plan, &paths()),
        Err(CompiledOciError::Workload(_))
    ));
}

#[test]
fn one_physical_source_can_be_projected_to_two_targets() {
    let mut value = fixture();
    let mut projection = value["artifacts"][0].clone();
    projection["mount"]["target"] = json!("/models/secondary");
    value["artifacts"].as_array_mut().unwrap().push(projection);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let invocation = project(&plan, &paths()).unwrap();
    assert_eq!(invocation.mounts.len(), 7);
    assert_eq!(invocation.mounts[0].source, invocation.mounts[3].source);
    assert_eq!(invocation.mounts[0].target, "/models/target/config.json");
    assert_eq!(invocation.mounts[3].target, "/models/secondary/config.json");
}

#[test]
fn conflicting_duplicate_physical_source_is_rejected() {
    let mut value = fixture();
    let mut projection = value["artifacts"][0].clone();
    projection["mount"]["target"] = json!("/models/secondary");
    projection["file_id"] = json!("different-file");
    value["artifacts"].as_array_mut().unwrap().push(projection);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(matches!(
        project(&plan, &paths()),
        Err(CompiledOciError::Workload(_))
    ));
}

#[test]
fn identical_receipts_may_be_reused_by_separate_selections() {
    let mut value = fixture();
    let digest = value["artifacts"][0]["sha256"].clone();
    let bytes = value["artifacts"][0]["size_bytes"].clone();
    value["artifacts"][1]["sha256"] = digest;
    value["artifacts"][1]["size_bytes"] = bytes;
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let invocation = project(&plan, &paths()).unwrap();
    assert_eq!(
        invocation.mounts[0].source,
        std::path::Path::new("/run/vonk/models/primary/config.json")
    );
    assert_eq!(
        invocation.mounts[1].source,
        std::path::Path::new("/run/vonk/models/dependency-qwen3-8-27b-dspark-b3c99101/config.json")
    );
}

#[test]
fn empty_support_file_remains_a_read_only_mount() {
    let mut value = fixture();
    let artifact = &mut value["artifacts"][0];
    artifact["file_id"] = json!("tokenizer-config");
    artifact["path"] = json!("tokenizer_config.json");
    artifact["sha256"] = json!(crate::compiled_execution_plan::EMPTY_SHA256);
    artifact["size_bytes"] = json!(0);
    artifact["roles"] = json!(["tokenizer"]);
    value["artifacts"] = json!([artifact.clone()]);
    value["security"]["mounts"] = json!([
        {"source":"model","target":"/models/target"},
        {"source":"outputs","target":"/outputs"}
    ]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let invocation = project(&plan, &paths()).unwrap();
    assert_eq!(
        invocation.mounts[0].source,
        std::path::Path::new("/run/vonk/models/primary/tokenizer_config.json")
    );
}

