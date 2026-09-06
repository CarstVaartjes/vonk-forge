use std::env;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use ring::rand::SystemRandom;
use ring::signature::{Ed25519KeyPair, KeyPair};
use serde_json::{Value, json};
use uuid::Uuid;
use vonk_agent::compiled_oci::{CompiledOciPaths, project};
use vonk_agent::workloads::CompiledExecutionPlan;
use vonk_agent_helper::protocol::{
    AUTHORITY, GrantClaims, GrantSignature, HostOperation, SignedGrant, canonical_signing_bytes,
    read_frame, write_frame,
};
use vonk_agent_protocol::{HostRuntimeAction, HostRuntimeRequest, canonical_json, hex_sha256};

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

fn main() {
    let mode = env::args().nth(1).expect("mode setup|import|start");
    if mode == "setup" {
        setup_files();
        return;
    }
    let archive_sha = env_required("VONK_HELPER_ARCHIVE_SHA");
    let archive_bytes: u64 = env_required("VONK_HELPER_ARCHIVE_BYTES").parse().unwrap();
    let registry_digest = env_required("VONK_HELPER_REGISTRY_DIGEST");
    let platform_digest = env_required("VONK_HELPER_PLATFORM_DIGEST");
    let image_ref = env_required("VONK_HELPER_IMAGE_REF");
    let action = match mode.as_str() {
        "import" => HostRuntimeAction::ImageImport,
        "start" => HostRuntimeAction::Start,
        other => panic!("unsupported mode {other}"),
    };
    let arguments = if action == HostRuntimeAction::ImageImport {
        vec![
            format!("/var/lib/vonk-forge/oci-archives/{archive_sha}"),
            archive_sha.clone(),
            archive_bytes.to_string(),
            registry_digest.clone(),
            platform_digest.clone(),
            image_ref.clone(),
        ]
    } else {
        production_start_arguments()
    };
    let job_id = Uuid::parse_str("40000000-0000-4000-8000-000000000004").unwrap();
    let operation_id = if action == HostRuntimeAction::ImageImport {
        Uuid::parse_str("50000000-0000-4000-8000-000000000005").unwrap()
    } else {
        Uuid::parse_str("50000000-0000-4000-8000-000000000006").unwrap()
    };
    let fence = if action == HostRuntimeAction::ImageImport {
        Uuid::parse_str("60000000-0000-4000-8000-000000000005").unwrap()
    } else {
        Uuid::parse_str("60000000-0000-4000-8000-000000000006").unwrap()
    };
    let request = HostRuntimeRequest {
        schema_version: 1,
        action,
        job_id,
        operation_id,
        attempt: 1,
        fence,
        arguments: arguments.clone(),
        observation: None,
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
    let operation = HostOperation::ExecuteContainerRuntimeRequest {
        action: match action {
            HostRuntimeAction::ImageImport => {
                vonk_agent_helper::protocol::ContainerRuntimeAction::ImageImport
            }
            HostRuntimeAction::Start => vonk_agent_helper::protocol::ContainerRuntimeAction::Start,
            _ => unreachable!(),
        },
        job_id,
        operation_id,
        attempt: 1,
        fence,
        request_sha256: request_sha.clone(),
        observation_identity_sha256: None,
    };
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
        != Some("container-runtime-request-executed")
    {
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
    let observation = Ed25519KeyPair::generate_pkcs8(&SystemRandom::new()).unwrap();
    let observation_pair = Ed25519KeyPair::from_pkcs8(observation.as_ref()).unwrap();
    fs::write(
        "/var/lib/vonk-forge/helper/observation-receipt.pk8",
        observation.as_ref(),
    )
    .unwrap();
    fs::write(
        "/etc/vonk-forge-agent/observation-receipt.pub",
        observation_pair.public_key().as_ref(),
    )
    .unwrap();
    fs::write(
        "/etc/vonk-forge-agent/agent.toml",
        "node_id = \"spk_0123456789abcdef0123456789abcdef\"\n",
    )
    .unwrap();
    fs::set_permissions(
        "/var/lib/vonk-forge/helper/observation-receipt.pk8",
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    fs::set_permissions(
        "/etc/vonk-forge-agent/observation-receipt.pub",
        fs::Permissions::from_mode(0o640),
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

fn production_start_arguments() -> Vec<String> {
    let fixture = fs::read_to_string(env_required("VONK_HELPER_FIXTURE")).unwrap();
    let mut value: Value = serde_json::from_str(&fixture).unwrap();
    value["runtime"]["executable"] = json!("/bin/sh");
    value["runtime"]["argv"] = json!([
        "-c",
        "set -eu; printf 'uid=%s\\n' \"$(id -u)\"; touch \"$HOME/helper-entrypoint-ok\" \"$TMPDIR/helper-tmp-ok\"; if [ -e \"$XDG_CACHE_HOME/helper-cache-ok\" ]; then printf 'cache-reused\\n'; else printf 'cache-created\\n' >\"$XDG_CACHE_HOME/helper-cache-ok\"; printf 'cache-created\\n'; fi; if [ -e /tmp/helper-ephemeral-marker ]; then printf 'tmp-reused\\n'; exit 9; fi; touch /tmp/helper-ephemeral-marker; printf 'tmp-fresh\\n'; printf 'helper-argv-once\\n'; sleep 5"
    ]);
    let archive_sha = env_required("VONK_HELPER_ARCHIVE_SHA");
    let archive_bytes: u64 = env_required("VONK_HELPER_ARCHIVE_BYTES").parse().unwrap();
    let registry_digest = env_required("VONK_HELPER_REGISTRY_DIGEST");
    let platform_digest = env_required("VONK_HELPER_PLATFORM_DIGEST");
    let config_id = env_required("VONK_HELPER_CONFIG_ID");
    let image_ref = env_required("VONK_HELPER_IMAGE_REF");
    value["runtime"]["image_digest"] = json!(platform_digest);
    value["security"]["devices"] = json!([]);
    value["runtime_image"]["image_digest"] = json!(platform_digest);
    value["runtime_image"]["registry_manifest_digest"] = json!(registry_digest);
    value["runtime_image"]["platform_manifest_digest"] = json!(platform_digest);
    value["runtime_image"]["local_image_config_id"] = json!(config_id);
    value["runtime_image"]["local_image_reference"] = json!(image_ref);
    value["runtime_image"]["oci_layout_sha256"] = json!(archive_sha);
    value["runtime_image"]["image_bytes"] = json!(archive_bytes);
    value["runtime_image"]["distribution_object"]["sha256"] = json!(archive_sha);
    value["runtime_image"]["distribution_object"]["bytes"] = json!(archive_bytes);
    value["runtime_image"]["runtime_interface_label"] = json!("v1");
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let invocation = project(
        &plan,
        &CompiledOciPaths {
            image_archive: PathBuf::from(format!("/var/lib/vonk-forge/oci-archives/{archive_sha}")),
            model_root: PathBuf::from("/var/lib/vonk-forge/models"),
            input_root: None,
            output_root: PathBuf::from("/var/lib/vonk-forge-agent/runs/proof-run/outputs"),
            cache_root: PathBuf::from(
                "/var/lib/vonk-forge-agent/installations/proof-install/runtime-cache",
            ),
            runtime_spec: PathBuf::from(
                "/var/lib/vonk-forge-agent/run-metadata/proof-run/runtime.json",
            ),
        },
    )
    .unwrap();
    let mut arguments = invocation.podman_arguments();
    arguments.splice(
        1..1,
        [
            "--name".to_owned(),
            "vonk-proof-run".to_owned(),
            "--restart".to_owned(),
            "no".to_owned(),
        ],
    );
    arguments
}
