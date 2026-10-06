use std::env;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use ring::signature::{Ed25519KeyPair, KeyPair};
use serde_json::{Value, json};
use uuid::Uuid;
use vonk_agent::workloads::CompiledExecutionPlan;
use vonk_agent_helper::protocol::{
    AUTHORITY, GrantClaims, GrantSignature, HostOperation, SignedGrant, canonical_signing_bytes,
    read_frame, write_frame,
};
use vonk_agent_protocol::generated::HostHelperResponseStatus;
use vonk_agent_protocol::{
    HostRuntimeAction, HostRuntimeRequest, RecipeStartRequest, canonical_json,
    compiled_oci::{CompiledOciPaths, start_arguments_for_paths},
    hex_sha256,
};

fn env_required(name: &str) -> String {
    env::var(name).unwrap_or_else(|_| panic!("{name} is required"))
}

fn node_id() -> String {
    env::var("VONK_HELPER_NODE_ID")
        .unwrap_or_else(|_| "spk_0123456789abcdef0123456789abcdef".to_owned())
}

fn socket_path() -> String {
    env::var("VONK_HELPER_SOCKET")
        .unwrap_or_else(|_| "/run/vonk-forge-package-helper/package-helper.sock".to_owned())
}

fn request_root() -> String {
    env::var("VONK_HELPER_REQUEST_ROOT")
        .unwrap_or_else(|_| "/run/vonk-forge-agent/runtime-requests".to_owned())
}

const AUTH_SEED: [u8; 32] = [42; 32];
const PROBE_RUN_ID: &str = "40000000-0000-4000-8000-000000000004";
const PROBE_INSTALLATION_ID: &str = "40000000-0000-4000-8000-000000000001";

fn main() {
    let mode = env::args().nth(1).expect("mode setup|pull|start|observe");
    if mode == "setup" {
        setup_files();
        return;
    }
    if mode == "observe" {
        observe();
        return;
    }
    let archive_sha = env_required("VONK_HELPER_ARCHIVE_SHA");
    let platform_digest = env_required("VONK_HELPER_PLATFORM_DIGEST");
    // The agent names the platform manifest for both digests, exactly as
    // production pulls from the Controller's layered store do.
    let registry_digest = platform_digest.clone();
    let image_ref = env_required("VONK_HELPER_IMAGE_REF");
    let action = match mode.as_str() {
        "pull" => HostRuntimeAction::ImagePull,
        "start" => HostRuntimeAction::Start,
        other => panic!("unsupported mode {other}"),
    };
    let (arguments, start_plan) = if action == HostRuntimeAction::ImagePull {
        (
            vec![
                env_required("VONK_HELPER_LOOPBACK_REGISTRY"),
                platform_digest.clone(),
                env_required("VONK_HELPER_CONFIG_ID"),
                image_ref.clone(),
            ],
            None,
        )
    } else {
        let (runtime_arguments, plan) = production_start_arguments();
        (
            start_request_arguments(
                &archive_sha,
                &registry_digest,
                &platform_digest,
                &image_ref,
                runtime_arguments,
            ),
            Some(plan),
        )
    };
    let fence = if action == HostRuntimeAction::ImagePull {
        Uuid::parse_str("60000000-0000-4000-8000-000000000005").unwrap()
    } else {
        Uuid::parse_str("60000000-0000-4000-8000-000000000006").unwrap()
    };
    let request = HostRuntimeRequest {
        action,
        fence,
        arguments: arguments.clone(),
        job_plan: None,
        installation_id: None,
        reconciliation_identity: None,
        run_generation: start_plan.as_ref().map(|plan| plan.run_generation),
        start_plan: start_plan.clone(),
        stop_plan: None,
    };
    request.validate().unwrap();
    let body = canonical_json(&request).unwrap();
    let request_sha = hex_sha256(&body);
    let request_path = Path::new(&request_root()).join(format!("{request_sha}.json"));
    fs::write(&request_path, &body).unwrap();
    fs::set_permissions(&request_path, fs::Permissions::from_mode(0o600)).unwrap();

    let keypair = Ed25519KeyPair::from_seed_unchecked(&AUTH_SEED).unwrap();
    let request_id = Uuid::new_v4();
    let issued_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let operation = HostOperation::ExecuteContainerRuntimeRequestOperation(
        vonk_agent_protocol::generated::ExecuteContainerRuntimeRequestOperation {
            type_: "execute-container-runtime-request".into(),
            action: match action {
                HostRuntimeAction::ImagePull => {
                    vonk_agent_helper::protocol::ContainerRuntimeAction::ImagePull
                }
                HostRuntimeAction::Start => {
                    vonk_agent_helper::protocol::ContainerRuntimeAction::Start
                }
                _ => unreachable!(),
            },
            fence,
            request_sha256: request_sha.clone(),
            installation_id: None,
            reconciliation_identity: None,
            start_plan_sha256: start_plan
                .as_ref()
                .map(|plan| hex_sha256(&canonical_json(plan).unwrap())),
            stop_plan_sha256: None,
            run_generation: start_plan.as_ref().map(|plan| plan.run_generation),
            runtime_run_id: start_plan.as_ref().map(|plan| plan.run_id),
            runtime_target_id: start_plan.as_ref().map(|plan| plan.run_id),
            runtime_installation_id: start_plan.as_ref().map(|plan| plan.installation_id),
        },
    );
    let claims = GrantClaims {
        schema_version: 1,
        authority: AUTHORITY.to_owned(),
        request_id,
        node_id: node_id(),
        issued_at,
        expires_at: issued_at + 120,
        operation,
    };
    let signature = keypair.sign(&canonical_signing_bytes(&claims).unwrap());
    let grant = SignedGrant {
        schema_version: 1,
        claims,
        signature: GrantSignature {
            algorithm: "ed25519".to_owned(),
            key_id: hex_sha256(keypair.public_key().as_ref()),
            value: hex::encode(signature.as_ref()),
        },
    };
    let grant_body = canonical_json(&grant).unwrap();
    let mut stream = UnixStream::connect(socket_path()).unwrap();
    write_frame(&mut stream, &grant_body).unwrap();
    let response = read_frame(&mut stream).unwrap();
    let response_value: Value = serde_json::from_slice(&response).unwrap();
    println!("mode={mode}");
    println!("request_sha256={request_sha}");
    println!("request={}", String::from_utf8(body).unwrap());
    println!("grant={}", String::from_utf8(grant_body).unwrap());
    println!("response={}", String::from_utf8(response).unwrap());
    if response_value.get("status").and_then(Value::as_str)
        != Some(HostHelperResponseStatus::ContainerRuntimeRequestExecuted.as_str())
    {
        std::process::exit(2);
    }
}

/// Observe the started run the way the agent's periodic report does: an
/// ungranted inspection of the exact run request.
fn observe() {
    let platform_digest = env_required("VONK_HELPER_PLATFORM_DIGEST");
    let (runtime_arguments, _) = production_start_arguments();
    let request = HostRuntimeRequest {
        action: HostRuntimeAction::RunInspect,
        fence: Uuid::new_v4(),
        arguments: start_request_arguments(
            &env_required("VONK_HELPER_ARCHIVE_SHA"),
            &platform_digest,
            &platform_digest,
            &env_required("VONK_HELPER_IMAGE_REF"),
            runtime_arguments,
        ),
        job_plan: None,
        installation_id: None,
        reconciliation_identity: None,
        run_generation: None,
        start_plan: None,
        stop_plan: None,
    };
    request.validate().unwrap();
    let body = canonical_json(&request).unwrap();
    let request_sha = hex_sha256(&body);
    let request_path = Path::new(&request_root()).join(format!("{request_sha}.json"));
    fs::write(&request_path, &body).unwrap();
    fs::set_permissions(&request_path, fs::Permissions::from_mode(0o600)).unwrap();
    let frame = canonical_json(&vonk_agent_protocol::RecipeRunInspectionRequest {
        request_id: Uuid::new_v4(),
        request_sha256: request_sha,
    })
    .unwrap();
    let mut stream = UnixStream::connect(socket_path()).unwrap();
    write_frame(&mut stream, &frame).unwrap();
    let response = read_frame(&mut stream).unwrap();
    let _ = fs::remove_file(&request_path);
    println!("response={}", String::from_utf8_lossy(&response));
    let value: Value = serde_json::from_slice(&response).unwrap();
    if value.get("process_running").and_then(Value::as_bool) != Some(true) {
        std::process::exit(2);
    }
}

fn setup_files() {
    fs::create_dir_all("/etc/vonk-forge-agent").unwrap();
    fs::create_dir_all("/var/lib/vonk-forge/helper/requests").unwrap();
    let authority = Ed25519KeyPair::from_seed_unchecked(&AUTH_SEED).unwrap();
    fs::write(
        "/etc/vonk-forge-agent/host-helper-authority.pub",
        hex::encode(authority.public_key().as_ref()),
    )
    .unwrap();
    fs::write(
        "/usr/share/keyrings/vonk-forge-release.pub",
        "0000000000000000000000000000000000000000000000000000000000000000",
    )
    .unwrap();
    fs::write(
        "/etc/vonk-forge-agent/agent.toml",
        "node_id = \"spk_0123456789abcdef0123456789abcdef\"\n",
    )
    .unwrap();
    fs::set_permissions(
        "/etc/vonk-forge-agent/host-helper-authority.pub",
        fs::Permissions::from_mode(0o644),
    )
    .unwrap();
    fs::set_permissions(
        "/usr/share/keyrings/vonk-forge-release.pub",
        fs::Permissions::from_mode(0o644),
    )
    .unwrap();
    fs::set_permissions(
        "/etc/vonk-forge-agent/agent.toml",
        fs::Permissions::from_mode(0o644),
    )
    .unwrap();
    println!("helper proof keys and configuration prepared");
}

fn production_start_arguments() -> (Vec<String>, RecipeStartRequest) {
    let fixture = fs::read_to_string(env_required("VONK_HELPER_FIXTURE")).unwrap();
    let mut value: Value = serde_json::from_str(&fixture).unwrap();
    value["runtime"]["placement"]["endpoint_address"] =
        json!(env_required("VONK_HELPER_PROBE_ENDPOINT_ADDRESS"));
    value["security"]["network_mode"] = json!("bridge");
    value["runtime"]["executable"] = json!("/opt/vonk/bin/vllm");
    value["runtime"]["argv"] = json!([
        "-c",
        "set -eu; printf 'uid=%s\\n' \"$(id -u)\"; touch \"$HOME/helper-entrypoint-ok\" \"$TMPDIR/helper-tmp-ok\"; if [ -e \"$XDG_CACHE_HOME/helper-cache-ok\" ]; then printf 'cache-reused\\n'; else printf 'cache-created\\n' >\"$XDG_CACHE_HOME/helper-cache-ok\"; printf 'cache-created\\n'; fi; if [ -e /tmp/helper-ephemeral-marker ]; then printf 'tmp-reused\\n'; exit 9; fi; touch /tmp/helper-ephemeral-marker; printf 'tmp-fresh\\n'; printf 'helper-argv-once\\n'; sleep 5"
    ]);
    let archive_sha = env_required("VONK_HELPER_ARCHIVE_SHA");
    let archive_bytes: u64 = env_required("VONK_HELPER_ARCHIVE_BYTES").parse().unwrap();
    let platform_digest = env_required("VONK_HELPER_PLATFORM_DIGEST");
    let config_id = env_required("VONK_HELPER_CONFIG_ID");
    value["security"]["gpu"] = json!(false);
    value["runtime_image"]["image_digest"] = json!(platform_digest);
    value["runtime_image"]["local_image_config_id"] = json!(config_id);
    value["runtime_image"]["oci_layout_sha256"] = json!(archive_sha);
    value["runtime_image"]["image_bytes"] = json!(archive_bytes);
    value["runtime_image"]["runtime_interface_label"] = json!("v1");
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let paths = CompiledOciPaths {
        model_root: PathBuf::from(format!(
            "/var/lib/vonk-forge-agent/installations/{PROBE_INSTALLATION_ID}/models"
        )),
        input_root: None,
        output_root: PathBuf::from(format!(
            "/var/lib/vonk-forge-agent/runs/{PROBE_RUN_ID}/outputs"
        )),
        cache_root: PathBuf::from(format!(
            "/var/lib/vonk-forge-agent/installations/{PROBE_INSTALLATION_ID}/runtime-cache"
        )),
        runtime_spec: PathBuf::from(format!(
            "/var/lib/vonk-forge-agent/run-metadata/{PROBE_RUN_ID}/runtime.json"
        )),
    };
    let arguments = start_arguments_for_paths(&plan, &paths, PROBE_RUN_ID).unwrap();
    let start_plan = typed_start_plan(&plan);
    (arguments, start_plan)
}

fn typed_start_plan(plan: &CompiledExecutionPlan) -> RecipeStartRequest {
    serde_json::from_value(json!({
        "run_id": PROBE_RUN_ID,
        "installation_id": PROBE_INSTALLATION_ID,
        "recipe_revision_id": "40000000-0000-4000-8000-000000000002",
        "mapping_id": "40000000-0000-4000-8000-000000000007",
        "plan_digest": plan.identity.model_artifact_set_sha256,
        "compiled_execution_plan": plan,
        "run_generation": 1
    }))
    .unwrap()
}

fn start_request_arguments(
    archive_sha: &str,
    registry_digest: &str,
    platform_digest: &str,
    image_ref: &str,
    docker_arguments: Vec<String>,
) -> Vec<String> {
    let mut arguments = vec![
        archive_sha.to_owned(),
        registry_digest.to_owned(),
        platform_digest.to_owned(),
        image_ref.to_owned(),
    ];
    arguments.extend(docker_arguments);
    arguments
}

#[cfg(test)]
mod tests {
    use super::start_request_arguments;

    #[test]
    fn start_request_matches_host_runtime_wire_shape() {
        let arguments = start_request_arguments(
            &"a".repeat(64),
            &format!("sha256:{}", "b".repeat(64)),
            &format!("sha256:{}", "c".repeat(64)),
            "localhost/vonk/compiled-runtime-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            vec!["run".to_owned(), "--network".to_owned(), "none".to_owned()],
        );
        assert_eq!(arguments.len(), 7);
        assert_eq!(&arguments[..4], [
            "a".repeat(64),
            format!("sha256:{}", "b".repeat(64)),
            format!("sha256:{}", "c".repeat(64)),
            "localhost/vonk/compiled-runtime-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc".to_owned(),
        ]);
        assert_eq!(&arguments[4..], ["run", "--network", "none"]);
    }
}
