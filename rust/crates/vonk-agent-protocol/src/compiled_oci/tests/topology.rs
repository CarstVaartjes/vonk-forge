#![cfg(test)]

use super::*;

#[test]
fn job_projection_keeps_input_output_and_lifecycle_boundaries() {
    let mut value = fixture();
    value["endpoint"] = Value::Null;
    value["runtime"]["placement"]["port"] = Value::Null;
    value["job"] = json!({
        "interface": "image-job",
        "input": {
            "required": true,
            "media_types": ["application/octet-stream"],
            "max_bytes": 1024,
            "slots": null
        },
        "timeout_seconds": 90
    });
    value["security"]["mounts"] = json!([
        {"source":"model","target":"/models/target"},
        {"source":"inputs","target":"/inputs"},
        {"source":"outputs","target":"/outputs"}
    ]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let mut layout = paths();
    layout.input_root = Some(PathBuf::from("/run/vonk/inputs"));
    let invocation = project(&plan, &layout).unwrap();
    assert!(!invocation.detach);
    assert!(
        invocation
            .mounts
            .iter()
            .any(|mount| mount.target == "/inputs" && mount.read_only)
    );
    assert_eq!(invocation.lifecycle.stop_timeout_seconds, 30);
    assert!(invocation.publishes.is_empty());
    assert!(
        invocation
            .podman_arguments()
            .windows(2)
            .any(|window| window == ["--network", "none"])
    );
}

#[test]
fn native_ranks_keep_host_addresses_without_docker_port_translation() {
    for rank in [0, 1] {
        let mut value = fixture();
        let role = if rank == 0 { "entrypoint" } else { "worker" };
        value["runtime"]["placement"]["rank"] = json!(rank);
        value["runtime"]["placement"]["role"] = json!(role);
        value["runtime"]["placement"]["world_size"] = json!(2);
        value["runtime"]["placement"]["local_address"] =
            json!(format!("192.168.100.{}", 10 + rank));
        value["runtime"]["placement"]["master_address"] = json!("192.168.100.10");
        value["runtime"]["placement"]["master_port"] = json!(29500);
        value["runtime"]["placement"]["endpoint_address"] = if rank == 0 {
            json!("192.168.1.211")
        } else {
            json!(null)
        };
        value["topology"] = json!({"name":"dual", "node_count":2});
        value["security"]["network_mode"] = json!("host");
        value["security"]["gpu"] = json!(true);
        let plan: CompiledExecutionPlan = serde_json::from_value(value.clone()).unwrap();
        let mut installed_value = value.clone();
        for field in [
            "local_address",
            "master_address",
            "master_port",
            "endpoint_address",
        ] {
            installed_value["runtime"]["placement"][field] = json!(null);
        }
        installed_value["security"]["network_mode"] = json!("none");
        let installed: CompiledExecutionPlan = serde_json::from_value(installed_value).unwrap();
        assert!(crate::compiled_execution_plan::same_installed_workload(
            &installed, &plan
        ));
        let invocation = project(&plan, &paths()).unwrap();
        let argv = invocation.podman_arguments();
        assert!(invocation.publishes.is_empty());
        assert!(!argv.iter().any(|arg| arg == "--publish" || arg == "--ipc"));
        assert!(
            argv.windows(2)
                .any(|args| args == ["--device", "/dev/infiniband:/dev/infiniband"])
        );
        assert!(
            argv.windows(2)
                .any(|args| args == ["--ulimit", "memlock=-1:-1"])
        );
        value["security"]["network_mode"] = json!("bridge");
        let wrong: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        assert!(project(&wrong, &paths()).is_err());
    }
}

#[test]
fn unauthorized_host_network_mode_is_rejected() {
    let mut value = fixture();
    value["security"]["network_mode"] = json!("host");
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(project(&plan, &paths()).is_err());
}

#[test]
fn signed_endpoint_selects_bridge_and_exact_cdi_device() {
    let mut value = fixture();
    value["runtime"]["placement"]["endpoint_address"] = json!("192.168.1.211");
    value["security"]["network_mode"] = json!("bridge");
    value["security"]["gpu"] = json!(true);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let invocation = project(&plan, &paths()).unwrap();
    assert_eq!(invocation.security.network_mode, OciNetworkMode::Bridge);
    assert_eq!(invocation.security.devices, vec!["nvidia.com/gpu=all"]);
    assert_eq!(invocation.publishes, vec!["192.168.1.211:8000:8000"]);
    assert!(
        invocation
            .environment
            .iter()
            .any(|entry| entry.name == "VONK_LISTEN_PORT" && entry.value == "8000")
    );
    assert!(
        invocation
            .podman_arguments()
            .windows(2)
            .any(|window| window == ["--network", "bridge"])
    );
    let podman = invocation.podman_arguments();
    assert!(
        podman
            .windows(2)
            .any(|window| window == ["--log-driver", "local"])
    );
    assert!(
        podman
            .windows(2)
            .any(|window| window == ["--log-opt", "max-size=10m"])
    );
    assert!(
        podman
            .windows(2)
            .any(|window| window == ["--log-opt", "max-file=3"])
    );
}
