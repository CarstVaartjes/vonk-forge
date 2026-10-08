#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn attached_jobs_require_readonly_inputs_from_the_same_run() {
    let (_temp, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    fs::create_dir(&model).unwrap();
    let arguments = job_runtime_arguments(&roots, model);
    let validated = validate_docker_run(&arguments, &roots, None).unwrap();
    assert!(!validated.detached);
    assert_eq!(validated.job_timeout_seconds, Some(3600));
    let expected_inputs = roots.agent_data.join("runs").join(RUN_ID).join("inputs");
    assert_eq!(validated.inputs.as_deref(), Some(expected_inputs.as_path()));

    let mut writable = arguments.clone();
    let mount = writable
        .iter_mut()
        .find(|value| value.contains("dst=/inputs"))
        .unwrap();
    mount.truncate(mount.len() - ",readonly".len());
    assert!(validate_docker_run(&writable, &roots, None).is_err());

    let other_run = "50000000-0000-4000-8000-000000000005";
    let other_inputs = roots.agent_data.join("runs").join(other_run).join("inputs");
    fs::create_dir_all(&other_inputs).unwrap();
    let mut cross_run = arguments.clone();
    *cross_run
        .iter_mut()
        .find(|value| value.contains("dst=/inputs"))
        .unwrap() = format!(
        "type=bind,src={},dst=/inputs,readonly",
        other_inputs.display()
    );
    assert!(validate_docker_run(&cross_run, &roots, None).is_err());

    let run_root = roots.agent_data.join("runs").join(RUN_ID);
    let relocated = roots.agent_data.join("runs").join(other_run);
    fs::remove_dir_all(&relocated).unwrap();
    fs::rename(&run_root, &relocated).unwrap();
    symlink(&relocated, &run_root).unwrap();
    assert!(validate_docker_run(&arguments, &roots, None).is_err());
}

#[test]
fn runtime_publications_require_an_explicit_routable_bind_address() {
    assert!(parse_publication("192.168.1.211:8101:8000").is_some());
    assert!(parse_publication("192.168.100.10:29500:29500").is_some());

    for value in [
        "8101:8000",
        "0.0.0.0:8101:8000",
        "127.0.0.1:8101:8000",
        "169.254.1.1:8101:8000",
        "[::]:8101:8000",
        "[fe80::1]:8101:8000",
        "[fd00::10]:8101:8000",
        "192.168.1.211:80:8000",
        "192.168.1.211:8101:80",
    ] {
        assert!(parse_publication(value).is_none(), "{value}");
    }
}

#[test]
fn compiled_workload_fixture_reaches_helper_validation_with_scoped_receipts() {
    let plan: vonk_agent_protocol::compiled_execution_plan::CompiledExecutionPlan =
        serde_json::from_str(include_str!(
            "../../../tests/fixtures/compiled_workload_v2.json"
        ))
        .unwrap();
    plan.validate().unwrap();
    assert_eq!(plan.runtime.executable, "/opt/vonk/bin/vllm");
    assert!(!plan.security.host_network());

    let (_temp, roots) = runtime_fixture();
    let model_set = runtime_models(&roots).join(&plan.identity.model_artifact_set_sha256);
    let primary = model_set.join("primary");
    let draft = model_set.join("draft");
    fs::create_dir_all(&primary).unwrap();
    fs::create_dir_all(&draft).unwrap();
    let primary_file = primary.join("config.json");
    let draft_file = draft.join("config.json");
    fs::write(&primary_file, b"same cached receipt").unwrap();
    fs::write(&draft_file, b"same cached receipt").unwrap();
    let mut arguments = runtime_arguments(
        &roots,
        &[
            (primary_file, "/models/primary", true),
            (draft_file, "/models/draft", true),
        ],
    );
    let image = arguments
        .iter_mut()
        .find(|value| value.starts_with("localhost/vonk/recipe-build-"))
        .unwrap();
    *image = format!(
        "localhost/vonk/compiled-runtime-{}@{}",
        plan.runtime_image.oci_layout_sha256, plan.runtime_image.image_digest,
    );
    let validated = validate_docker_run(&arguments, &roots, None).unwrap();
    assert_eq!(validated.models.len(), 2);
    assert_eq!(
        validated.platform_manifest_digest,
        plan.runtime_image.image_digest
    );
    assert_eq!(validated.arguments.last().unwrap(), "/opt/vonk/bin/vllm");
}

#[test]
fn runtime_requires_explicit_none_network_and_entrypoint() {
    let (_temp, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    fs::create_dir_all(&model).unwrap();
    let arguments = runtime_arguments(&roots, &[(model.clone(), "/models", true)]);
    for value in ["bridge", "host", "custom-network"] {
        let mut candidate = arguments.clone();
        let position = candidate.iter().position(|item| item == "none").unwrap();
        candidate[position] = value.to_owned();
        assert!(
            validate_docker_run(&candidate, &roots, None).is_err(),
            "{value}"
        );
    }
    let mut missing = arguments.clone();
    let entrypoint = missing
        .iter()
        .position(|item| item == "--entrypoint")
        .unwrap();
    missing.drain(entrypoint..=entrypoint + 1);
    assert!(validate_docker_run(&missing, &roots, None).is_err());
    let mut mismatched = arguments;
    let command = mismatched
        .iter()
        .position(|item| item == "/opt/vonk/bin/vllm")
        .unwrap();
    mismatched[command] = "/opt/vonk/bin/other".to_owned();
    assert!(validate_docker_run(&mismatched, &roots, None).is_err());
}

#[test]
fn runtime_accepts_signed_bridge_endpoint_and_exact_cdi_gpu() {
    let (_temp, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    fs::create_dir_all(&model).unwrap();
    let mut arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
    let network = arguments.iter().position(|value| value == "none").unwrap();
    arguments[network] = "bridge".to_owned();
    let image = arguments
        .iter()
        .position(|value| value.starts_with("localhost/vonk/"))
        .unwrap();
    arguments.splice(
        image..image,
        [
            "--publish".to_owned(),
            "192.168.1.211:8101:8000".to_owned(),
            "--device".to_owned(),
            "nvidia.com/gpu=all".to_owned(),
            "--env".to_owned(),
            "VONK_LISTEN_PORT=8000".to_owned(),
        ],
    );
    assert!(validate_docker_run(&arguments, &roots, None).is_ok());

    let mut wrong_device = arguments.clone();
    let device = wrong_device
        .iter()
        .position(|value| value == "nvidia.com/gpu=all")
        .unwrap();
    wrong_device[device] = "vendor.example/gpu=all".to_owned();
    assert!(validate_docker_run(&wrong_device, &roots, None).is_err());
    let mut fabric = arguments;
    let device = fabric
        .iter()
        .position(|value| value == "nvidia.com/gpu=all")
        .unwrap();
    fabric[device] = "/dev/infiniband:/dev/infiniband".to_owned();
    assert!(validate_docker_run(&fabric, &roots, None).is_err());
}

#[test]
fn runtime_cache_rejects_other_installations_and_symlinked_ancestors() {
    for case in [
        "other-installation",
        "installation-alias",
        "outside-installation",
    ] {
        let (temp, roots) = runtime_fixture_with_separate_agent_data();
        let model = artifact_path(&roots, 'a');
        fs::create_dir_all(&model).unwrap();
        let mut arguments = runtime_arguments(&roots, &[(model, "/models", true)]);
        validate_docker_run(&arguments, &roots, None).unwrap();

        let installation = runtime_models(&roots).parent().unwrap().to_path_buf();
        let other = roots
            .agent_data
            .join("installations")
            .join("installation-2");
        match case {
            "other-installation" => fs::create_dir_all(other.join("runtime-cache")).unwrap(),
            "installation-alias" => symlink(&installation, &other).unwrap(),
            "outside-installation" => {
                let outside = temp.path().join("outside-installation");
                fs::create_dir_all(outside.join("runtime-cache")).unwrap();
                symlink(&outside, &other).unwrap();
            }
            _ => unreachable!(),
        }
        *arguments
            .iter_mut()
            .find(|value| value.ends_with("dst=/outputs/cache"))
            .unwrap() = format!(
            "type=bind,src={},dst=/outputs/cache",
            other.join("runtime-cache").display()
        );
        assert!(
            matches!(
                validate_docker_run(&arguments, &roots, None),
                Err(OperationError::UnsafePath)
            ),
            "cache boundary accepted {case}"
        );
    }
}
