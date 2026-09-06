use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt, symlink};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use ring::signature::{Ed25519KeyPair, KeyPair};
use tempfile::TempDir;
use uuid::Uuid;
use vonk_agent_helper::operations::{
    CommandOutput, CommandRunner, ManagedRoots, OperationExecutor,
};
use vonk_agent_helper::protocol::{
    ContainerRuntimeAction, GrantClaims, GrantSignature, GrantVerifier, HostOperation, ManagedArea,
    PeerIdentity, RestartUnit, SignedGrant, canonical_signing_bytes, parse_request,
};
use vonk_agent_protocol::{HostRuntimeAction, HostRuntimeRequest, canonical_json, hex_sha256};

const NOW: i64 = 2_100_000_000;
const NODE_ID: &str = "spk_11111111111111111111111111111111";

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../agent_protocol/fixtures")
}

#[test]
fn python_fixture_is_the_same_strict_canonical_grant() {
    let raw = fs::read(fixtures().join("host-helper-grant.json")).unwrap();
    let raw = raw.strip_suffix(b"\n").unwrap_or(&raw);
    let request = parse_request(raw).unwrap();
    assert_eq!(request.claims.node_id, NODE_ID);
    assert_eq!(vonk_agent_protocol::canonical_json(&request).unwrap(), raw);
    let public_key =
        hex::decode("8b237d788e8eaaef550c6d125823fa45f1fd5fc29b2c88bdf871119471fc1312").unwrap();
    GrantVerifier::new(&public_key, 971)
        .unwrap()
        .authorize(
            &request,
            &PeerIdentity {
                uid: 1001,
                primary_gid: 971,
                supplementary_gids: Vec::new(),
            },
            2_100_000_000,
        )
        .unwrap();
}

fn signer(seed: u8) -> Ed25519KeyPair {
    Ed25519KeyPair::from_seed_unchecked(&[seed; 32]).unwrap()
}

fn signed(operation: HostOperation, signer: &Ed25519KeyPair) -> SignedGrant {
    let claims = GrantClaims {
        schema_version: 1,
        authority: "vonk.host-maintenance-helper".to_owned(),
        request_id: Uuid::parse_str("10000000-0000-4000-8000-000000000001").unwrap(),
        node_id: NODE_ID.to_owned(),
        issued_at: NOW - 1,
        expires_at: NOW + 60,
        operation,
    };
    let signature = signer.sign(&canonical_signing_bytes(&claims).unwrap());
    SignedGrant {
        schema_version: 1,
        claims,
        signature: GrantSignature {
            algorithm: "ed25519".to_owned(),
            key_id: vonk_agent_protocol::hex_sha256(signer.public_key().as_ref()),
            value: hex::encode(signature.as_ref()),
        },
    }
}

fn grant_verifier(signer: &Ed25519KeyPair) -> GrantVerifier {
    GrantVerifier::new(signer.public_key().as_ref(), 971).unwrap()
}

fn runtime_archive_config_id() -> String {
    format!(
        "sha256:{}",
        hex_sha256(br#"{"config":{"User":"10001:10001"}}"#)
    )
}

fn runtime_image_archive() -> (Vec<u8>, String) {
    let config = br#"{"config":{"User":"10001:10001"}}"#;
    let config_digest = hex_sha256(config);
    let config_member = format!("{config_digest}.json");
    let manifest = serde_json::to_vec(&serde_json::json!([{
        "Config": config_member,
        "RepoTags": ["localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002:latest"],
        "Layers": [],
    }]))
    .unwrap();
    let mut builder = tar::Builder::new(Vec::new());
    let mut header = tar::Header::new_gnu();
    header.set_size(manifest.len() as u64);
    header.set_mode(0o600);
    header.set_cksum();
    builder
        .append_data(&mut header, "manifest.json", manifest.as_slice())
        .unwrap();
    let mut header = tar::Header::new_gnu();
    header.set_size(config.len() as u64);
    header.set_mode(0o600);
    header.set_cksum();
    builder
        .append_data(&mut header, config_member, &config[..])
        .unwrap();
    (
        builder.into_inner().unwrap(),
        format!("sha256:{config_digest}"),
    )
}

#[test]
fn every_permitted_operation_has_an_exact_typed_shape() {
    let signer = signer(7);
    let operations = [
        HostOperation::CreateManagedDirectory {
            area: ManagedArea::Models,
            relative_path: "sha256/aa".to_owned(),
        },
        HostOperation::InstallVonkDeb {
            package_sha256: "c".repeat(64),
            package_signature: "d".repeat(128),
        },
        HostOperation::RestartVonkUnit {
            unit: RestartUnit::Agent,
        },
        HostOperation::ScheduleReboot { delay_seconds: 120 },
        HostOperation::ExecuteContainerRuntimeRequest {
            action: ContainerRuntimeAction::Start,
            job_id: Uuid::parse_str("20000000-0000-4000-8000-000000000002").unwrap(),
            operation_id: Uuid::parse_str("30000000-0000-4000-8000-000000000003").unwrap(),
            attempt: 2,
            fence: Uuid::parse_str("40000000-0000-4000-8000-000000000004").unwrap(),
            request_sha256: "a".repeat(64),
            observation_identity_sha256: None,
        },
    ];

    for operation in operations {
        let request = signed(operation, &signer);
        let raw = vonk_agent_protocol::canonical_json(&request).unwrap();
        assert_eq!(parse_request(&raw).unwrap(), request);
    }
}

#[test]
fn rejects_unknown_fields_and_untyped_process_control() {
    let signer = signer(7);
    let request = signed(
        HostOperation::RestartVonkUnit {
            unit: RestartUnit::Agent,
        },
        &signer,
    );
    let raw = vonk_agent_protocol::canonical_json(&request).unwrap();
    let mut document: serde_json::Value = serde_json::from_slice(&raw).unwrap();
    for (field, value) in [
        ("executable", serde_json::json!("/bin/sh")),
        ("environment", serde_json::json!({"LD_PRELOAD": "/tmp/x"})),
        (
            "arguments",
            serde_json::json!(["--force", "../../etc/shadow"]),
        ),
    ] {
        document["claims"]["operation"]
            .as_object_mut()
            .unwrap()
            .insert(field.to_owned(), value);
        let invalid = vonk_agent_protocol::canonical_json(&document).unwrap();
        assert!(parse_request(&invalid).is_err(), "accepted {field}");
        document["claims"]["operation"]
            .as_object_mut()
            .unwrap()
            .remove(field);
    }
}

#[test]
fn protocol_rejects_removed_slot_and_supervisor_operations() {
    for operation in [
        serde_json::json!({
            "type": "activate-agent-slot",
            "slot": "a",
            "artifact_sha256": "a".repeat(64),
            "artifact_signature": "b".repeat(128),
        }),
        serde_json::json!({
            "type": "restart-vonk-unit",
            "unit": "supervisor",
        }),
    ] {
        assert!(serde_json::from_value::<HostOperation>(operation).is_err());
    }
}

#[test]
fn authority_rejects_expiry_bad_signature_and_users_outside_agent_group() {
    let signer = signer(7);
    let verifier = grant_verifier(&signer);
    let request = signed(
        HostOperation::RestartVonkUnit {
            unit: RestartUnit::Agent,
        },
        &signer,
    );

    assert!(
        verifier
            .authorize(
                &request,
                &PeerIdentity {
                    uid: 1001,
                    primary_gid: 1001,
                    supplementary_gids: vec![971],
                },
                NOW,
            )
            .is_ok()
    );
    assert!(
        verifier
            .authorize(
                &request,
                &PeerIdentity {
                    uid: 1001,
                    primary_gid: 1001,
                    supplementary_gids: vec![999],
                },
                NOW,
            )
            .is_err()
    );
    assert!(
        verifier
            .authorize(
                &request,
                &PeerIdentity {
                    uid: 1001,
                    primary_gid: 971,
                    supplementary_gids: vec![],
                },
                NOW + 61,
            )
            .is_err()
    );

    let mut forged = request;
    forged.signature.value = "0".repeat(128);
    assert!(
        verifier
            .authorize(
                &forged,
                &PeerIdentity {
                    uid: 1001,
                    primary_gid: 971,
                    supplementary_gids: vec![],
                },
                NOW,
            )
            .is_err()
    );
}

#[derive(Clone, Default)]
struct RecordingRunner {
    calls: SharedCalls,
    runtime_container: Arc<Mutex<Option<(String, String)>>>,
    runtime_running: Arc<Mutex<bool>>,
    fail_systemd_run: Arc<Mutex<bool>>,
    docker_load_stdout: Arc<Mutex<Option<Vec<u8>>>>,
}

#[derive(Debug)]
struct CandidateEvidence {
    path: PathBuf,
    bytes: Vec<u8>,
    uid: u32,
    gid: u32,
    mode: u32,
    links: u64,
}

#[derive(Clone, Default)]
struct AdversarialPackageRunner {
    calls: SharedCalls,
    observed_candidates: Arc<Mutex<Vec<CandidateEvidence>>>,
    source_swap: Arc<Mutex<Option<(PathBuf, PathBuf)>>>,
    fail_dpkg: Arc<Mutex<bool>>,
}

impl CommandRunner for AdversarialPackageRunner {
    fn run(
        &self,
        executable: &std::path::Path,
        arguments: &[String],
    ) -> Result<CommandOutput, String> {
        self.calls
            .lock()
            .unwrap()
            .push((executable.to_path_buf(), arguments.to_vec()));
        if matches!(
            executable.to_str(),
            Some("/usr/bin/dpkg-deb" | "/usr/bin/dpkg")
        ) {
            let candidate_index = if executable == std::path::Path::new("/usr/bin/dpkg-deb") {
                1
            } else {
                2
            };
            let candidate = PathBuf::from(&arguments[candidate_index]);
            let metadata = fs::metadata(&candidate).map_err(|error| error.to_string())?;
            self.observed_candidates
                .lock()
                .unwrap()
                .push(CandidateEvidence {
                    path: candidate,
                    bytes: fs::read(&arguments[candidate_index])
                        .map_err(|error| error.to_string())?,
                    uid: metadata.uid(),
                    gid: metadata.gid(),
                    mode: metadata.mode() & 0o777,
                    links: metadata.nlink(),
                });
        }
        if executable == std::path::Path::new("/usr/bin/dpkg-deb") {
            if let Some((replacement, incoming)) = self.source_swap.lock().unwrap().take() {
                fs::rename(replacement, incoming).map_err(|error| error.to_string())?;
            }
            let stdout = if arguments.get(2).is_some_and(|field| field == "Package") {
                b"vonk-forge-agent\n".to_vec()
            } else {
                b"arm64\n".to_vec()
            };
            return Ok(CommandOutput {
                success: true,
                stdout,
                exit_code: Some(0),
            });
        }
        let success = !*self.fail_dpkg.lock().unwrap();
        Ok(CommandOutput {
            success,
            stdout: Vec::new(),
            exit_code: Some(if success { 0 } else { 1 }),
        })
    }
}

type SharedCalls = Arc<Mutex<Vec<(PathBuf, Vec<String>)>>>;

impl CommandRunner for RecordingRunner {
    fn run(
        &self,
        executable: &std::path::Path,
        arguments: &[String],
    ) -> Result<CommandOutput, String> {
        self.calls
            .lock()
            .unwrap()
            .push((executable.to_path_buf(), arguments.to_vec()));
        let mut success = true;
        let stdout = if executable == std::path::Path::new("/usr/bin/docker")
            && arguments.first().is_some_and(|value| value == "load")
        {
            self.docker_load_stdout
                .lock()
                .unwrap()
                .clone()
                .unwrap_or_else(|| {
                    format!(
                        "Loaded image: localhost/vonk/recipe-build-{}:latest\n",
                        "20000000-0000-4000-8000-000000000002"
                    )
                    .into_bytes()
                })
        } else if executable == std::path::Path::new("/usr/bin/docker")
            && arguments.get(..2) == Some(&["image".to_owned(), "inspect".to_owned()])
        {
            format!(
                "{}\tlinux\tarm64\tv1\t10001:10001\n",
                runtime_archive_config_id()
            )
            .into_bytes()
        } else if executable == std::path::Path::new("/usr/bin/docker")
            && arguments.get(..2) == Some(&["container".to_owned(), "inspect".to_owned()])
        {
            match self.runtime_container.lock().unwrap().as_ref() {
                Some((digest, run_id))
                    if arguments
                        .get(3)
                        .is_some_and(|format| format.contains(".State.Running")) =>
                {
                    format!(
                        "{}\t{digest}\ttrue\t{run_id}\n",
                        self.runtime_running.lock().unwrap()
                    )
                    .into_bytes()
                }
                Some((_digest, run_id)) => format!("true\t{run_id}\n").into_bytes(),
                None => {
                    success = false;
                    Vec::new()
                }
            }
        } else if executable == std::path::Path::new("/usr/bin/docker")
            && arguments.first().is_some_and(|value| value == "run")
        {
            let digest = arguments.windows(2).find_map(|pair| {
                (pair[0] == "--label")
                    .then_some(pair[1].as_str())
                    .and_then(|value| value.strip_prefix("ai.vonkforge.runtime-request-sha256="))
            });
            let run_id = arguments.windows(2).find_map(|pair| {
                (pair[0] == "--label")
                    .then_some(pair[1].as_str())
                    .and_then(|value| value.strip_prefix("ai.vonkforge.run-id="))
            });
            if let (Some(digest), Some(run_id)) = (digest, run_id) {
                *self.runtime_container.lock().unwrap() =
                    Some((digest.to_owned(), run_id.to_owned()));
                *self.runtime_running.lock().unwrap() = true;
            }
            "e".repeat(64).into_bytes()
        } else if executable == std::path::Path::new("/usr/bin/docker")
            && arguments.first().is_some_and(|value| value == "rm")
        {
            *self.runtime_container.lock().unwrap() = None;
            *self.runtime_running.lock().unwrap() = false;
            Vec::new()
        } else if arguments.get(2).is_some_and(|value| value == "Package") {
            b"vonk-forge-agent\n".to_vec()
        } else if arguments
            .get(2)
            .is_some_and(|value| value == "Architecture")
        {
            b"arm64\n".to_vec()
        } else {
            Vec::new()
        };
        if executable == std::path::Path::new("/usr/bin/systemd-run")
            && *self.fail_systemd_run.lock().unwrap()
        {
            success = false;
        }
        Ok(CommandOutput {
            success,
            stdout,
            exit_code: Some(if success { 0 } else { 1 }),
        })
    }
}

impl RecordingRunner {
    fn set_docker_load_stdout(&self, output: impl Into<Vec<u8>>) {
        *self.docker_load_stdout.lock().unwrap() = Some(output.into());
    }
}

fn fixture() -> (TempDir, ManagedRoots, RecordingRunner, Ed25519KeyPair) {
    let temp = tempfile::tempdir().unwrap();
    let data = temp.path().join("data");
    let agent_data = temp.path().join("agent-data");
    let roots = ManagedRoots::under(&data).with_agent_data(&agent_data);
    fs::create_dir_all(&roots.models).unwrap();
    fs::create_dir_all(&roots.state).unwrap();
    fs::create_dir_all(&roots.incoming).unwrap();
    fs::create_dir_all(&roots.agent_data).unwrap();
    fs::create_dir_all(
        roots
            .agent_data
            .join("installations")
            .join("installation-1")
            .join("runtime-cache"),
    )
    .unwrap();
    fs::create_dir_all(roots.package_custody.parent().unwrap()).unwrap();
    (temp, roots, RecordingRunner::default(), signer(9))
}

fn write_runtime_request(roots: &ManagedRoots, request: &HostRuntimeRequest) -> String {
    fs::create_dir_all(&roots.runtime_requests).unwrap();
    let body = canonical_json(request).unwrap();
    let digest = hex_sha256(&body);
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(roots.runtime_requests.join(format!("{digest}.json")))
        .unwrap();
    file.write_all(&body).unwrap();
    file.sync_all().unwrap();
    digest
}

fn runtime_operation(request: &HostRuntimeRequest, digest: String) -> HostOperation {
    HostOperation::ExecuteContainerRuntimeRequest {
        action: match request.action {
            HostRuntimeAction::ImageImport => ContainerRuntimeAction::ImageImport,
            HostRuntimeAction::ImageInspect => ContainerRuntimeAction::ImageInspect,
            HostRuntimeAction::RunInspect => ContainerRuntimeAction::RunInspect,
            HostRuntimeAction::Start => ContainerRuntimeAction::Start,
            HostRuntimeAction::Stop => ContainerRuntimeAction::Stop,
        },
        job_id: request.job_id,
        operation_id: request.operation_id,
        attempt: request.attempt,
        fence: request.fence,
        request_sha256: digest,
        observation_identity_sha256: request.observation.as_ref().map(|_| "e".repeat(64)),
    }
}

fn runtime_request(action: HostRuntimeAction, arguments: Vec<String>) -> HostRuntimeRequest {
    HostRuntimeRequest {
        schema_version: 1,
        action,
        job_id: Uuid::parse_str("10000000-0000-4000-8000-000000000001").unwrap(),
        operation_id: Uuid::parse_str("20000000-0000-4000-8000-000000000002").unwrap(),
        attempt: 1,
        fence: Uuid::parse_str("30000000-0000-4000-8000-000000000003").unwrap(),
        arguments,
        observation: None,
    }
}

#[test]
fn accepted_docker_archive_is_loaded_and_receipted_by_exact_digest() {
    let (_temp, roots, runner, release) = fixture();
    let image = "localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002";
    let (body, config_id) = runtime_image_archive();
    let archive_sha256 = hex_sha256(&body);
    let archive_root = roots.agent_data.join("oci-archives");
    fs::create_dir_all(&archive_root).unwrap();
    let archive = archive_root.join(&archive_sha256);
    fs::write(&archive, &body).unwrap();
    fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();
    let registry_index_digest = format!("sha256:{}", "a".repeat(64));
    let platform_manifest_digest = format!("sha256:{}", "c".repeat(64));
    let image_reference = format!("{image}@{platform_manifest_digest}");
    let request = runtime_request(
        HostRuntimeAction::ImageImport,
        vec![
            archive.display().to_string(),
            archive_sha256.clone(),
            body.len().to_string(),
            registry_index_digest.clone(),
            platform_manifest_digest.clone(),
            image_reference.clone(),
        ],
    );
    let request_digest = write_runtime_request(&roots, &request);
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        None,
    )
    .unwrap();

    executor
        .execute(&runtime_operation(&request, request_digest))
        .unwrap();

    let receipt: serde_json::Value = serde_json::from_slice(
        &fs::read(roots.runtime_image_receipts.join(&archive_sha256)).unwrap(),
    )
    .unwrap();
    assert_eq!(
        receipt,
        serde_json::json!({
            "archive_bytes": body.len(),
            "archive_sha256": archive_sha256,
            "image_config_id": config_id,
            "local_image_reference": image_reference,
            "platform_manifest_digest": platform_manifest_digest,
            "registry_index_digest": registry_index_digest,
            "schema_version": 2,
        })
    );
    let calls = runner.calls.lock().unwrap();
    assert!(calls.iter().any(|(program, arguments)| {
        program == std::path::Path::new("/usr/bin/docker")
            && arguments == &["load", "--input", archive.to_str().unwrap()]
    }));
    assert!(calls.iter().any(|(program, arguments)| {
        program == std::path::Path::new("/usr/bin/docker")
            && arguments
                == &[
                    "tag",
                    "localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002:latest",
                    "localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002",
                ]
    }));
}

fn assert_archive_import_accepts_load_output(load_output: Vec<u8>, expected_source: Option<&str>) {
    let (_temp, roots, runner, release) = fixture();
    runner.set_docker_load_stdout(load_output);
    let image = "localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002";
    let (body, config_id) = runtime_image_archive();
    let archive_sha256 = hex_sha256(&body);
    let archive_root = roots.agent_data.join("oci-archives");
    fs::create_dir_all(&archive_root).unwrap();
    let archive = archive_root.join(&archive_sha256);
    fs::write(&archive, &body).unwrap();
    fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();
    let registry_index_digest = format!("sha256:{}", "a".repeat(64));
    let platform_manifest_digest = format!("sha256:{}", "c".repeat(64));
    let image_reference = format!("{image}@{platform_manifest_digest}");
    let request = runtime_request(
        HostRuntimeAction::ImageImport,
        vec![
            archive.display().to_string(),
            archive_sha256.clone(),
            body.len().to_string(),
            registry_index_digest.clone(),
            platform_manifest_digest.clone(),
            image_reference,
        ],
    );
    let request_digest = write_runtime_request(&roots, &request);
    let calls = runner.calls.clone();
    let executor =
        OperationExecutor::new(roots.clone(), release.public_key().as_ref(), runner, None).unwrap();

    executor
        .execute(&runtime_operation(&request, request_digest))
        .unwrap();

    let receipt: serde_json::Value = serde_json::from_slice(
        &fs::read(roots.runtime_image_receipts.join(&archive_sha256)).unwrap(),
    )
    .unwrap();
    assert_eq!(receipt["schema_version"], 2);
    assert_eq!(receipt["archive_sha256"], archive_sha256);
    assert_eq!(receipt["archive_bytes"], body.len());
    assert_eq!(
        receipt["platform_manifest_digest"],
        platform_manifest_digest
    );
    assert_eq!(receipt["image_config_id"], config_id);
    assert_eq!(
        receipt["local_image_reference"],
        format!("{image}@sha256:{}", "c".repeat(64))
    );
    if let Some(source) = expected_source {
        assert!(calls.lock().unwrap().iter().any(|(program, arguments)| {
            program == std::path::Path::new("/usr/bin/docker")
                && arguments == &["tag", source, image].map(str::to_owned)
        }));
    }
}

#[test]
fn accepted_docker_archive_does_not_depend_on_load_output_format() {
    assert_archive_import_accepts_load_output(
        b"Loaded image: localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002\n"
            .to_vec(),
        Some("localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002"),
    );
    assert_archive_import_accepts_load_output(
        b"Loaded image ID: sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662\n"
            .to_vec(),
        Some("sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"),
    );
}

#[test]
fn accepted_runtime_is_compiled_to_hardened_docker_without_socket_authority() {
    let (_temp, roots, runner, release) = fixture();
    let run_id = "40000000-0000-4000-8000-000000000004";
    let image = "localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002";
    let registry_index_digest = format!("sha256:{}", "a".repeat(64));
    let platform_manifest_digest = format!("sha256:{}", "c".repeat(64));
    let image_reference = format!("{image}@{platform_manifest_digest}");
    let (archive_body, image_id) = runtime_image_archive();
    let archive_sha256 = hex_sha256(&archive_body);
    let state = roots.agent_data.join("runs").join(run_id);
    let outputs = state.join("outputs");
    let metadata = roots.agent_data.join("run-metadata").join(run_id);
    let model = roots
        .agent_data
        .join("installations")
        .join("installation-1")
        .join("models")
        .join("sha256")
        .join("a".repeat(64));
    fs::create_dir_all(&outputs).unwrap();
    fs::create_dir_all(&metadata).unwrap();
    fs::create_dir_all(&model).unwrap();
    fs::write(metadata.join("runtime.json"), b"{}").unwrap();
    let archive_root = roots.agent_data.join("oci-archives");
    fs::create_dir_all(&archive_root).unwrap();
    let archive = archive_root.join(&archive_sha256);
    fs::write(&archive, &archive_body).unwrap();
    fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();
    fs::create_dir_all(&roots.runtime_image_receipts).unwrap();
    fs::write(
        roots.runtime_image_receipts.join(&archive_sha256),
        serde_json::to_vec(&serde_json::json!({
            "schema_version": 2,
            "registry_index_digest": registry_index_digest,
            "platform_manifest_digest": platform_manifest_digest,
            "archive_sha256": archive_sha256,
            "archive_bytes": archive_body.len(),
            "image_config_id": image_id,
            "local_image_reference": image_reference,
        }))
        .unwrap(),
    )
    .unwrap();
    let docker_arguments = vec![
        "run".to_owned(),
        "--detach".to_owned(),
        "--name".to_owned(),
        format!("vonk-{run_id}"),
        "--entrypoint".to_owned(),
        "/opt/vonk/bin/vllm".to_owned(),
        "--restart".to_owned(),
        "no".to_owned(),
        "--read-only".to_owned(),
        "--tmpfs".to_owned(),
        "/tmp:rw,nosuid,nodev,mode=1777,size=1073741824".to_owned(),
        "--init".to_owned(),
        "--pull".to_owned(),
        "never".to_owned(),
        "--log-driver".to_owned(),
        "local".to_owned(),
        "--log-opt".to_owned(),
        "max-size=10m".to_owned(),
        "--log-opt".to_owned(),
        "max-file=3".to_owned(),
        "--cap-drop=ALL".to_owned(),
        "--security-opt=no-new-privileges".to_owned(),
        "--network".to_owned(),
        "bridge".to_owned(),
        "--pids-limit".to_owned(),
        "4096".to_owned(),
        "--memory".to_owned(),
        "1000000000".to_owned(),
        "--memory-swap".to_owned(),
        "1000000000".to_owned(),
        "--shm-size".to_owned(),
        "134217728".to_owned(),
        "--user".to_owned(),
        "10001:10001".to_owned(),
        "--env".to_owned(),
        "VONK_RANK=0".to_owned(),
        "--env".to_owned(),
        "VONK_LISTEN_PORT=8000".to_owned(),
        "--env".to_owned(),
        "HOME=/outputs/cache/home".to_owned(),
        "--env".to_owned(),
        "XDG_CACHE_HOME=/outputs/cache".to_owned(),
        "--env".to_owned(),
        "TMPDIR=/outputs/tmp".to_owned(),
        "--publish".to_owned(),
        "192.168.1.211:8101:8000".to_owned(),
        "--mount".to_owned(),
        format!("type=bind,src={},dst=/models,readonly", model.display()),
        "--mount".to_owned(),
        format!("type=bind,src={},dst=/outputs", outputs.display()),
        "--mount".to_owned(),
        format!(
            "type=bind,src={},dst=/outputs/cache",
            roots
                .agent_data
                .join("installations")
                .join("installation-1")
                .join("runtime-cache")
                .display()
        ),
        "--mount".to_owned(),
        format!(
            "type=bind,src={},dst=/run/vonk/runtime.json,readonly",
            metadata.join("runtime.json").display()
        ),
        image_reference.clone(),
        "/opt/vonk/bin/vllm".to_owned(),
    ];
    let mut arguments = vec![
        archive_sha256.clone(),
        registry_index_digest.clone(),
        platform_manifest_digest.clone(),
        image_reference.clone(),
    ];
    arguments.extend(docker_arguments);
    let request = runtime_request(HostRuntimeAction::Start, arguments);
    let digest = write_runtime_request(&roots, &request);
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        None,
    )
    .unwrap();

    executor
        .execute(&runtime_operation(&request, digest))
        .unwrap();
    executor
        .execute(&runtime_operation(
            &request,
            hex_sha256(&canonical_json(&request).unwrap()),
        ))
        .unwrap();

    for forbidden_target in ["/state", "/scratch"] {
        let mut forbidden = request.clone();
        let writable = forbidden
            .arguments
            .iter_mut()
            .find(|value| value.contains("dst=/outputs"))
            .unwrap();
        *writable = format!("type=bind,src={},dst={forbidden_target}", outputs.display());
        let forbidden_digest = write_runtime_request(&roots, &forbidden);
        assert!(
            executor
                .execute(&runtime_operation(&forbidden, forbidden_digest))
                .is_err()
        );
    }
    {
        let calls = runner.calls.lock().unwrap();
        let docker_run = calls
            .iter()
            .find(|(program, arguments)| {
                program == std::path::Path::new("/usr/bin/docker")
                    && arguments.first().is_some_and(|value| value == "run")
            })
            .unwrap();
        assert_eq!(
            calls
                .iter()
                .filter(|(program, arguments)| {
                    program == std::path::Path::new("/usr/bin/docker")
                        && arguments.first().is_some_and(|value| value == "run")
                })
                .count(),
            1,
            "{calls:?}"
        );
        assert!(docker_run.1.contains(&image_reference));
        assert!(docker_run.1.windows(2).any(|values| {
            values == ["--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=1073741824"]
        }));
        assert!(
            docker_run
                .1
                .contains(&"ai.vonkforge.managed=true".to_owned())
        );
        assert!(
            docker_run
                .1
                .contains(&format!("ai.vonkforge.run-id={run_id}"))
        );
        assert!(!docker_run.1.iter().any(|value| {
            value.contains("docker.sock")
                || value == "--privileged"
                || value == "host"
                || value == "/dev/infiniband:/dev/infiniband"
        }));
        assert!(
            docker_run
                .1
                .windows(2)
                .any(|values| values == ["--network", "bridge"])
        );
        assert!(
            docker_run
                .1
                .windows(2)
                .any(|values| { values == ["--publish", "192.168.1.211:8101:8000"] })
        );
    }
    let inspect = runtime_request(HostRuntimeAction::RunInspect, request.arguments.clone());
    let inspect_digest = write_runtime_request(&roots, &inspect);
    executor
        .execute(&runtime_operation(&inspect, inspect_digest.clone()))
        .unwrap();
    let accepted_container = runner.runtime_container.lock().unwrap().clone().unwrap();
    *runner.runtime_running.lock().unwrap() = false;
    assert!(
        executor
            .execute(&runtime_operation(&inspect, inspect_digest.clone()))
            .is_err()
    );
    *runner.runtime_container.lock().unwrap() = None;
    assert!(
        executor
            .execute(&runtime_operation(&inspect, inspect_digest))
            .is_err()
    );
    assert_eq!(
        runner
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(program, arguments)| {
                program == std::path::Path::new("/usr/bin/docker")
                    && arguments.first().is_some_and(|value| value == "run")
            })
            .count(),
        1
    );
    *runner.runtime_container.lock().unwrap() = Some(accepted_container);

    let stop = runtime_request(
        HostRuntimeAction::Stop,
        vec![run_id.to_owned(), "30".to_owned()],
    );
    let stop_digest = write_runtime_request(&roots, &stop);
    executor
        .execute(&runtime_operation(&stop, stop_digest.clone()))
        .unwrap();
    executor
        .execute(&runtime_operation(&stop, stop_digest))
        .unwrap();
    *runner.runtime_container.lock().unwrap() = Some((
        "f".repeat(64),
        "50000000-0000-4000-8000-000000000005".to_owned(),
    ));
    assert!(
        executor
            .execute(&runtime_operation(
                &stop,
                hex_sha256(&canonical_json(&stop).unwrap()),
            ))
            .is_err()
    );
    let calls = runner.calls.lock().unwrap();
    let docker_stops = calls
        .iter()
        .filter(|(program, arguments)| {
            program == std::path::Path::new("/usr/bin/docker")
                && arguments.first().is_some_and(|value| value == "stop")
        })
        .map(|(_, arguments)| arguments.clone())
        .collect::<Vec<_>>();
    assert_eq!(
        docker_stops,
        vec![vec![
            "stop".to_owned(),
            "--timeout".to_owned(),
            "30".to_owned(),
            format!("vonk-{run_id}"),
        ]]
    );
    let docker_removes = calls
        .iter()
        .filter(|(program, arguments)| {
            program == std::path::Path::new("/usr/bin/docker")
                && arguments.first().is_some_and(|value| value == "rm")
        })
        .map(|(_, arguments)| arguments.clone())
        .collect::<Vec<_>>();
    assert_eq!(
        docker_removes,
        vec![vec!["rm".to_owned(), format!("vonk-{run_id}")]]
    );
}

#[test]
fn runtime_rejects_bridge_host_and_direct_fabric_networks_before_docker() {
    let (_temp, roots, runner, release) = fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        None,
    )
    .unwrap();
    for docker_arguments in [
        vec![
            "run".to_owned(),
            "--network".to_owned(),
            "bridge".to_owned(),
        ],
        vec!["run".to_owned(), "--network".to_owned(), "host".to_owned()],
        vec![
            "run".to_owned(),
            "--network".to_owned(),
            "host".to_owned(),
            "--ipc".to_owned(),
            "host".to_owned(),
            "--device".to_owned(),
            "/dev/infiniband:/dev/infiniband".to_owned(),
            "--ulimit".to_owned(),
            "memlock=-1:-1".to_owned(),
            "--ulimit".to_owned(),
            "stack=67108864:67108864".to_owned(),
        ],
    ] {
        let mut request_arguments = vec![format!("sha256:{}", "c".repeat(64))];
        request_arguments.extend(docker_arguments);
        let request = runtime_request(HostRuntimeAction::Start, request_arguments);
        let digest = write_runtime_request(&roots, &request);
        assert!(
            executor
                .execute(&runtime_operation(&request, digest))
                .is_err()
        );
    }
    assert!(runner.calls.lock().unwrap().is_empty());
}

#[test]
fn runtime_rejects_privilege_and_unmanaged_mounts_before_docker() {
    let (_temp, roots, runner, release) = fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        None,
    )
    .unwrap();
    for arguments in [
        vec!["run".to_owned(), "--privileged".to_owned()],
        vec![
            "run".to_owned(),
            "--mount".to_owned(),
            "type=bind,src=/run/docker.sock,dst=/run/docker.sock".to_owned(),
        ],
    ] {
        let mut request_arguments = vec![format!("sha256:{}", "c".repeat(64))];
        request_arguments.extend(arguments);
        let request = runtime_request(HostRuntimeAction::Start, request_arguments);
        let digest = write_runtime_request(&roots, &request);
        assert!(
            executor
                .execute(&runtime_operation(&request, digest))
                .is_err()
        );
    }
    assert!(runner.calls.lock().unwrap().is_empty());
}

#[test]
fn managed_directory_creation_rejects_traversal_and_symlink_escape() {
    let (temp, roots, runner, release) = fixture();
    let executor =
        OperationExecutor::new(roots.clone(), release.public_key().as_ref(), runner, None).unwrap();

    assert!(
        executor
            .execute(&HostOperation::CreateManagedDirectory {
                area: ManagedArea::Models,
                relative_path: "../escape".to_owned(),
            })
            .is_err()
    );
    symlink(temp.path(), roots.models.join("link")).unwrap();
    assert!(
        executor
            .execute(&HostOperation::CreateManagedDirectory {
                area: ManagedArea::Models,
                relative_path: "link/escape".to_owned(),
            })
            .is_err()
    );
    assert!(!temp.path().join("escape").exists());
}

#[test]
fn artifacts_are_verified_before_package_mutation() {
    let (_temp, roots, runner, release) = fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        None,
    )
    .unwrap();

    let bad_package = "e".repeat(64);
    let incoming = roots.incoming.join(format!("{bad_package}.deb"));
    fs::write(&incoming, b"not that digest").unwrap();
    fs::set_permissions(&incoming, fs::Permissions::from_mode(0o600)).unwrap();
    assert!(
        executor
            .execute(&HostOperation::InstallVonkDeb {
                package_sha256: bad_package,
                package_signature: "0".repeat(128),
            })
            .is_err()
    );
    assert!(runner.calls.lock().unwrap().is_empty());
    assert!(
        fs::read_dir(&roots.package_custody)
            .unwrap()
            .next()
            .is_none()
    );
}

#[test]
fn package_restart_and_reboot_commands_are_compiled_not_caller_supplied() {
    let (_temp, roots, runner, release) = fixture();
    let package_owner = fs::metadata(&roots.incoming).unwrap().uid();
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        Some(package_owner),
    )
    .unwrap()
    .with_package_owner(package_owner);
    let package = b"signed deb";
    let digest = vonk_agent_protocol::hex_sha256(package);
    let incoming = roots.incoming.join(format!("{digest}.deb"));
    fs::write(&incoming, package).unwrap();
    fs::set_permissions(&incoming, fs::Permissions::from_mode(0o600)).unwrap();
    let signature = release.sign(
        vonk_agent_helper::protocol::artifact_signing_bytes("deb", &digest)
            .unwrap()
            .as_slice(),
    );

    executor
        .execute(&HostOperation::InstallVonkDeb {
            package_sha256: digest.clone(),
            package_signature: hex::encode(signature.as_ref()),
        })
        .unwrap();
    executor
        .execute(&HostOperation::RestartVonkUnit {
            unit: RestartUnit::Agent,
        })
        .unwrap();
    executor
        .execute(&HostOperation::ScheduleReboot { delay_seconds: 300 })
        .unwrap();
    assert!(
        executor
            .execute(&HostOperation::ScheduleReboot { delay_seconds: 5 })
            .is_err()
    );

    let calls = runner.calls.lock().unwrap();
    assert_eq!(calls[0].0, PathBuf::from("/usr/bin/dpkg-deb"));
    assert_eq!(calls[0].1[0], "--field");
    assert_eq!(calls[1].1[2], "Architecture");
    assert_eq!(calls[2].0, PathBuf::from("/usr/bin/dpkg"));
    assert_eq!(
        calls[3],
        (
            PathBuf::from("/usr/bin/systemctl"),
            vec!["restart".to_owned(), "vonk-forge-agent.service".to_owned()],
        )
    );
    assert_eq!(calls[4].0, PathBuf::from("/usr/bin/systemd-run"));
    assert!(calls[4].1.contains(&"--on-active=300s".to_owned()));
}

#[test]
fn helper_restart_transient_unit_collision_fails_closed_without_fallback() {
    let (_temp, roots, runner, release) = fixture();
    *runner.fail_systemd_run.lock().unwrap() = true;
    let package_owner = fs::metadata(&roots.incoming).unwrap().uid();
    let executor = OperationExecutor::new(
        roots,
        release.public_key().as_ref(),
        runner.clone(),
        Some(package_owner),
    )
    .unwrap()
    .with_package_owner(package_owner);

    assert!(
        executor
            .execute(&HostOperation::RestartVonkUnit {
                unit: RestartUnit::Helper,
            })
            .is_err()
    );
    assert_eq!(
        *runner.calls.lock().unwrap(),
        vec![(
            PathBuf::from("/usr/bin/systemd-run"),
            vec![
                "--quiet".to_owned(),
                "--collect".to_owned(),
                "--unit=vonk-forge-helper-restart.service".to_owned(),
                "--on-active=1s".to_owned(),
                "/usr/bin/systemctl".to_owned(),
                "restart".to_owned(),
                "vonk-forge-package-helper.service".to_owned(),
            ],
        )]
    );
}

fn signed_package(
    roots: &ManagedRoots,
    release: &Ed25519KeyPair,
    body: &[u8],
) -> (String, String, PathBuf) {
    let digest = hex_sha256(body);
    let incoming = roots.incoming.join(format!("{digest}.deb"));
    fs::write(&incoming, body).unwrap();
    fs::set_permissions(&incoming, fs::Permissions::from_mode(0o600)).unwrap();
    let signature = release.sign(
        vonk_agent_helper::protocol::artifact_signing_bytes("deb", &digest)
            .unwrap()
            .as_slice(),
    );
    (digest, hex::encode(signature.as_ref()), incoming)
}

#[test]
fn startup_sweeps_only_exact_root_custody_shapes() {
    let (temp, roots, runner, release) = fixture();
    let owner = fs::metadata(&roots.data).unwrap().uid();
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner,
        Some(owner),
    )
    .unwrap();
    executor.prepare_package_custody().unwrap();

    let stale = roots.package_custody.join("a".repeat(32));
    fs::create_dir(&stale).unwrap();
    fs::set_permissions(&stale, fs::Permissions::from_mode(0o700)).unwrap();
    let candidate = stale.join(format!("{}.deb", "b".repeat(64)));
    fs::write(&candidate, b"stale root-owned candidate").unwrap();
    fs::set_permissions(&candidate, fs::Permissions::from_mode(0o600)).unwrap();

    executor.prepare_package_custody().unwrap();
    assert!(
        fs::read_dir(&roots.package_custody)
            .unwrap()
            .next()
            .is_none()
    );

    let hostile = roots.package_custody.join("c".repeat(32));
    fs::create_dir(&hostile).unwrap();
    fs::set_permissions(&hostile, fs::Permissions::from_mode(0o700)).unwrap();
    let outside = temp.path().join("must-not-delete");
    fs::write(&outside, b"outside custody").unwrap();
    symlink(&outside, hostile.join(format!("{}.deb", "d".repeat(64)))).unwrap();

    assert!(executor.prepare_package_custody().is_err());
    assert_eq!(fs::read(outside).unwrap(), b"outside custody");
}

#[test]
fn package_custody_rejects_symlinks_hardlinks_and_non_private_modes() {
    for attack in ["symlink", "hardlink", "mode"] {
        let (temp, roots, runner, release) = fixture();
        let package_owner = fs::metadata(&roots.incoming).unwrap().uid();
        let package = b"signed package";
        let digest = hex_sha256(package);
        let incoming = roots.incoming.join(format!("{digest}.deb"));
        match attack {
            "symlink" => {
                let outside = temp.path().join("outside.deb");
                fs::write(&outside, package).unwrap();
                symlink(outside, &incoming).unwrap();
            }
            "hardlink" => {
                fs::write(&incoming, package).unwrap();
                fs::set_permissions(&incoming, fs::Permissions::from_mode(0o600)).unwrap();
                fs::hard_link(&incoming, temp.path().join("linked.deb")).unwrap();
            }
            "mode" => {
                fs::write(&incoming, package).unwrap();
                fs::set_permissions(&incoming, fs::Permissions::from_mode(0o640)).unwrap();
            }
            _ => unreachable!(),
        }
        let signature = release.sign(
            vonk_agent_helper::protocol::artifact_signing_bytes("deb", &digest)
                .unwrap()
                .as_slice(),
        );
        let executor = OperationExecutor::new(
            roots.clone(),
            release.public_key().as_ref(),
            runner.clone(),
            Some(package_owner),
        )
        .unwrap()
        .with_package_owner(package_owner);

        assert!(
            executor
                .execute(&HostOperation::InstallVonkDeb {
                    package_sha256: digest,
                    package_signature: hex::encode(signature.as_ref()),
                })
                .is_err(),
            "accepted {attack} package"
        );
        assert!(runner.calls.lock().unwrap().is_empty());
        assert!(
            !roots.package_custody.exists()
                || fs::read_dir(&roots.package_custody)
                    .unwrap()
                    .next()
                    .is_none()
        );
    }
}

#[test]
fn root_custody_closes_the_agent_path_swap_race_and_compiles_exact_dpkg_argv() {
    let (_temp, roots, _runner, release) = fixture();
    let package_owner = fs::metadata(&roots.incoming).unwrap().uid();
    let body = b"exact signed package bytes";
    let (digest, signature, incoming) = signed_package(&roots, &release, body);
    let replacement = roots.incoming.join("agent-controlled-replacement.tmp");
    fs::write(&replacement, b"different bytes after verification").unwrap();
    fs::set_permissions(&replacement, fs::Permissions::from_mode(0o600)).unwrap();
    let runner = AdversarialPackageRunner::default();
    *runner.source_swap.lock().unwrap() = Some((replacement, incoming.clone()));
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        Some(package_owner),
    )
    .unwrap()
    .with_package_owner(package_owner);

    executor
        .execute(&HostOperation::InstallVonkDeb {
            package_sha256: digest.clone(),
            package_signature: signature,
        })
        .unwrap();

    let observed = runner.observed_candidates.lock().unwrap();
    assert_eq!(observed.len(), 3);
    let candidate = observed[0].path.clone();
    for evidence in observed.iter() {
        assert_eq!(evidence.path, candidate);
        assert_eq!(evidence.bytes, body);
        assert_eq!(
            (evidence.uid, evidence.gid),
            (package_owner, fs::metadata(&roots.data).unwrap().gid())
        );
        assert_eq!((evidence.mode, evidence.links), (0o600, 1));
    }
    let relative = candidate.strip_prefix(&roots.package_custody).unwrap();
    let components = relative
        .iter()
        .map(|part| part.to_str().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(components.len(), 2);
    assert_eq!(components[0].len(), 32);
    assert!(
        components[0]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    );
    assert_eq!(components[1], format!("{digest}.deb"));
    let calls = runner.calls.lock().unwrap();
    assert_eq!(
        calls.as_slice(),
        [
            (
                PathBuf::from("/usr/bin/dpkg-deb"),
                vec![
                    "--field".to_owned(),
                    candidate.display().to_string(),
                    "Package".to_owned()
                ],
            ),
            (
                PathBuf::from("/usr/bin/dpkg-deb"),
                vec![
                    "--field".to_owned(),
                    candidate.display().to_string(),
                    "Architecture".to_owned()
                ],
            ),
            (
                PathBuf::from("/usr/bin/dpkg"),
                vec![
                    "--install".to_owned(),
                    "--force-confold".to_owned(),
                    candidate.display().to_string(),
                ],
            ),
        ]
    );
    assert_eq!(
        fs::read(&incoming).unwrap(),
        b"different bytes after verification"
    );
    assert!(!candidate.exists());
    assert!(
        fs::read_dir(&roots.package_custody)
            .unwrap()
            .next()
            .is_none()
    );
}

#[test]
fn root_custody_is_cleaned_when_dpkg_fails_without_deleting_the_source() {
    let (_temp, roots, _runner, release) = fixture();
    let package_owner = fs::metadata(&roots.incoming).unwrap().uid();
    let body = b"signed package that dpkg rejects";
    let (digest, signature, incoming) = signed_package(&roots, &release, body);
    let runner = AdversarialPackageRunner::default();
    *runner.fail_dpkg.lock().unwrap() = true;
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner,
        Some(package_owner),
    )
    .unwrap()
    .with_package_owner(package_owner);

    assert!(
        executor
            .execute(&HostOperation::InstallVonkDeb {
                package_sha256: digest,
                package_signature: signature,
            })
            .is_err()
    );
    assert_eq!(fs::read(incoming).unwrap(), body);
    assert!(
        fs::read_dir(&roots.package_custody)
            .unwrap()
            .next()
            .is_none()
    );
}
