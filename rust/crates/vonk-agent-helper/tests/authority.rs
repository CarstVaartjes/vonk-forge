use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt, symlink};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use ring::signature::{Ed25519KeyPair, KeyPair};
use tempfile::TempDir;
use uuid::Uuid;
use vonk_agent_helper::operations::{
    CommandOutput, CommandRunner, ManagedRoots, OperationExecutor,
};
use vonk_agent_helper::protocol::{
    AgentSlot, ContainerRuntimeAction, GrantClaims, GrantSignature, GrantVerifier, HostOperation,
    ManagedArea, PeerIdentity, RestartUnit, SignedGrant, canonical_signing_bytes, parse_request,
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

#[test]
fn every_permitted_operation_has_an_exact_typed_shape() {
    let signer = signer(7);
    let operations = [
        HostOperation::CreateManagedDirectory {
            area: ManagedArea::Models,
            relative_path: "sha256/aa".to_owned(),
        },
        HostOperation::ActivateAgentSlot {
            slot: AgentSlot::B,
            artifact_sha256: "a".repeat(64),
            artifact_signature: "b".repeat(128),
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
fn authority_rejects_expiry_bad_signature_and_users_outside_agent_group() {
    let signer = signer(7);
    let verifier = grant_verifier(&signer);
    let request = signed(
        HostOperation::RestartVonkUnit {
            unit: RestartUnit::Supervisor,
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
            format!(
                "Loaded image: localhost/vonk/recipe-build-{}:latest\n",
                "20000000-0000-4000-8000-000000000002"
            )
            .into_bytes()
        } else if executable == std::path::Path::new("/usr/bin/docker")
            && arguments.get(..2) == Some(&["image".to_owned(), "inspect".to_owned()])
        {
            format!("sha256:{}\tlinux\tarm64\tv1\t10001:10001\n", "d".repeat(64)).into_bytes()
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
        Ok(CommandOutput { success, stdout })
    }
}

fn fixture() -> (TempDir, ManagedRoots, RecordingRunner, Ed25519KeyPair) {
    let temp = tempfile::tempdir().unwrap();
    let data = temp.path().join("data");
    let roots = ManagedRoots::under(&data);
    fs::create_dir_all(&roots.models).unwrap();
    fs::create_dir_all(&roots.state).unwrap();
    fs::create_dir_all(&roots.slots).unwrap();
    fs::create_dir_all(&roots.incoming).unwrap();
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
            HostRuntimeAction::Start => ContainerRuntimeAction::Start,
            HostRuntimeAction::Stop => ContainerRuntimeAction::Stop,
        },
        job_id: request.job_id,
        operation_id: request.operation_id,
        attempt: request.attempt,
        fence: request.fence,
        request_sha256: digest,
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
    }
}

#[test]
fn accepted_docker_archive_is_loaded_and_receipted_by_exact_digest() {
    let (_temp, roots, runner, release) = fixture();
    let operation_id = Uuid::parse_str("20000000-0000-4000-8000-000000000002").unwrap();
    let image = format!("localhost/vonk/recipe-build-{operation_id}");
    let archive_root = roots
        .agent_data
        .join("image-imports")
        .join(operation_id.to_string());
    fs::create_dir_all(&archive_root).unwrap();
    let archive = archive_root.join("image.docker.tar");
    let body = b"exact docker archive";
    fs::write(&archive, body).unwrap();
    fs::set_permissions(&archive, fs::Permissions::from_mode(0o600)).unwrap();
    let image_digest = format!("sha256:{}", "c".repeat(64));
    let request = runtime_request(
        HostRuntimeAction::ImageImport,
        vec![
            archive.display().to_string(),
            hex_sha256(body),
            body.len().to_string(),
            image_digest.clone(),
            image.clone(),
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

    assert_eq!(
        fs::read_to_string(
            roots
                .runtime_image_receipts
                .join(image_digest.trim_start_matches("sha256:"))
        )
        .unwrap(),
        format!("{image}\nsha256:{}\n", "d".repeat(64))
    );
    let calls = runner.calls.lock().unwrap();
    assert!(calls.iter().any(|(program, arguments)| {
        program == std::path::Path::new("/usr/bin/docker")
            && arguments == &["load", "--input", archive.to_str().unwrap()]
    }));
}

#[test]
fn accepted_runtime_is_compiled_to_hardened_docker_without_socket_authority() {
    let (_temp, roots, runner, release) = fixture();
    let run_id = "40000000-0000-4000-8000-000000000004";
    let image = "localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002";
    let image_digest = format!("sha256:{}", "c".repeat(64));
    let image_reference = format!("{image}@{image_digest}");
    let image_id = format!("sha256:{}", "d".repeat(64));
    let state = roots.agent_data.join("runs").join(run_id);
    let metadata = roots.agent_data.join("run-metadata").join(run_id);
    fs::create_dir_all(&state).unwrap();
    fs::create_dir_all(&metadata).unwrap();
    fs::write(metadata.join("runtime.json"), b"{}").unwrap();
    fs::create_dir_all(&roots.runtime_image_receipts).unwrap();
    fs::write(
        roots
            .runtime_image_receipts
            .join(image_digest.trim_start_matches("sha256:")),
        format!("{image}\n{image_id}\n"),
    )
    .unwrap();
    let docker_arguments = vec![
        "run".to_owned(),
        "--detach".to_owned(),
        "--name".to_owned(),
        format!("vonk-{run_id}"),
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
        "--publish".to_owned(),
        "192.168.1.211:8101:8000".to_owned(),
        "--publish".to_owned(),
        "192.168.100.10:29500:29500".to_owned(),
        "--env".to_owned(),
        "VONK_RANK=0".to_owned(),
        "--mount".to_owned(),
        format!(
            "type=bind,src={},dst=/models,readonly",
            roots.agent_data.join("models").display()
        ),
        "--mount".to_owned(),
        format!("type=bind,src={},dst=/state", state.display()),
        "--mount".to_owned(),
        format!(
            "type=bind,src={},dst=/run/vonk/runtime.json,readonly",
            metadata.join("runtime.json").display()
        ),
        "--gpus".to_owned(),
        "all".to_owned(),
        image_reference.clone(),
        "python".to_owned(),
        "/app/server.py".to_owned(),
    ];
    let mut arguments = vec![image_digest.clone()];
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
        assert!(docker_run.1.contains(&image.to_owned()));
        assert!(!docker_run.1.contains(&image_reference));
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
                || value.starts_with("--device")
        }));
    }
    *runner.runtime_running.lock().unwrap() = false;
    assert!(
        executor
            .execute(&runtime_operation(
                &request,
                hex_sha256(&canonical_json(&request).unwrap()),
            ))
            .is_err()
    );

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
fn runtime_rejects_host_network_privilege_and_unmanaged_mounts_before_docker() {
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
        vec!["run".to_owned(), "--network".to_owned(), "host".to_owned()],
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
fn artifacts_are_verified_before_slot_or_package_mutation() {
    let (_temp, roots, runner, release) = fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        None,
    )
    .unwrap();

    let slot = roots.slots.join("a");
    fs::create_dir_all(&slot).unwrap();
    let agent = slot.join("vonk-agent");
    fs::write(&agent, b"trusted agent").unwrap();
    let digest = vonk_agent_protocol::hex_sha256(b"trusted agent");
    let signature = release.sign(
        vonk_agent_helper::protocol::artifact_signing_bytes("agent", &digest)
            .unwrap()
            .as_slice(),
    );
    executor
        .execute(&HostOperation::ActivateAgentSlot {
            slot: AgentSlot::A,
            artifact_sha256: digest.clone(),
            artifact_signature: hex::encode(signature.as_ref()),
        })
        .unwrap();
    assert_eq!(
        runner.calls.lock().unwrap()[0],
        (
            PathBuf::from("/usr/lib/vonk-forge/vonk-agent-supervisor"),
            vec![
                "activate".to_owned(),
                "--slot".to_owned(),
                "a".to_owned(),
                "--sha256".to_owned(),
                digest,
            ],
        )
    );

    let bad_package = "e".repeat(64);
    fs::write(
        roots.incoming.join(format!("{bad_package}.deb")),
        b"not that digest",
    )
    .unwrap();
    assert!(
        executor
            .execute(&HostOperation::InstallVonkDeb {
                package_sha256: bad_package,
                package_signature: "0".repeat(128),
            })
            .is_err()
    );
    assert_eq!(runner.calls.lock().unwrap().len(), 1);
}

#[test]
fn package_restart_and_reboot_commands_are_compiled_not_caller_supplied() {
    let (_temp, roots, runner, release) = fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        None,
    )
    .unwrap();
    let package = b"signed deb";
    let digest = vonk_agent_protocol::hex_sha256(package);
    fs::write(roots.incoming.join(format!("{digest}.deb")), package).unwrap();
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
