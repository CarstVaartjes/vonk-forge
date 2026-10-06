use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt, symlink};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use ring::signature::{Ed25519KeyPair, KeyPair};
use tempfile::TempDir;
use uuid::Uuid;
use vonk_agent_helper::operations::{
    CommandOutput, CommandRunner, ManagedRoots, OperationError, OperationExecutor,
};
use vonk_agent_helper::protocol::{
    ContainerRuntimeAction, GrantClaims, GrantSignature, GrantVerifier, HostOperation,
    PeerIdentity, SignedGrant, canonical_signing_bytes, parse_inspection_request, parse_request,
};
use vonk_agent_protocol::generated::{
    ConfirmPackageActivationOperation, ExecuteContainerRuntimeRequestOperation,
    InstallVonkDebOperation,
};
use vonk_agent_protocol::{
    HostRuntimeAction, HostRuntimeRequest, RecipeRunInspectionRequest, canonical_json, hex_sha256,
};

const NOW: i64 = 2_100_000_000;
const NODE_ID: &str = "spk_11111111111111111111111111111111";

fn rollback_authority() -> vonk_agent_protocol::PackageRollbackAuthority {
    let source_sha = hex_sha256(b"signed source deb");
    let signature = signer(9)
        .sign(&vonk_agent_helper::protocol::artifact_signing_bytes("deb", &source_sha).unwrap());
    vonk_agent_protocol::PackageRollbackAuthority {
        source: vonk_agent_protocol::PackageRollbackSource {
            package_sha256: source_sha,
            package_signature: hex::encode(signature.as_ref()),
            package_version: "0.1.0".into(),
            binary_sha256: "a".repeat(64),
            helper_sha256: "b".repeat(64),
        },
        attempt_nonce: "c".repeat(64),
        activation_deadline: NOW + 300,
    }
}

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../agent_protocol/fixtures")
}

#[test]
fn python_issuer_fixture_is_verified_by_the_rust_helper() {
    let raw = fs::read(fixtures().join("host-helper-grant-python-issued.json")).unwrap();
    let raw = raw.strip_suffix(b"\n").unwrap_or(&raw);
    let request = parse_request(raw).unwrap();
    assert_eq!(vonk_agent_protocol::canonical_json(&request).unwrap(), raw);
    let public_key =
        hex::decode("66cd608b928b88e50e0efeaa33faf1c43cefe07294b0b87e9fe0aba6a3cf7633").unwrap();
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

fn confirm_activation() -> HostOperation {
    HostOperation::ConfirmPackageActivationOperation(ConfirmPackageActivationOperation {
        type_: "confirm-package-activation".into(),
        package_sha256: "a".repeat(64),
        attempt_nonce: "b".repeat(64),
    })
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

fn runtime_config_id() -> String {
    format!(
        "sha256:{}",
        hex_sha256(br#"{"config":{"User":"10001:10001"}}"#)
    )
}

#[test]
fn every_permitted_operation_has_an_exact_typed_shape() {
    let signer = signer(7);
    let operations = [
        HostOperation::InstallVonkDebOperation(InstallVonkDebOperation {
            type_: "install-vonk-deb".into(),
            rollback: rollback_authority(),
            package_sha256: "c".repeat(64),
            package_signature: "d".repeat(128),
        }),
        confirm_activation(),
        HostOperation::ExecuteContainerRuntimeRequestOperation(
            ExecuteContainerRuntimeRequestOperation {
                type_: "execute-container-runtime-request".into(),
                action: ContainerRuntimeAction::Start,
                fence: Uuid::parse_str("40000000-0000-4000-8000-000000000004").unwrap(),
                request_sha256: "a".repeat(64),
                installation_id: None,
                reconciliation_identity: None,
                run_generation: Some(1),
                runtime_installation_id: Some(
                    Uuid::parse_str("50000000-0000-4000-8000-000000000005").unwrap(),
                ),
                runtime_run_id: Some(
                    Uuid::parse_str("60000000-0000-4000-8000-000000000006").unwrap(),
                ),
                runtime_target_id: Some(
                    Uuid::parse_str("60000000-0000-4000-8000-000000000006").unwrap(),
                ),
                start_plan_sha256: Some("b".repeat(64)),
                stop_plan_sha256: None,
            },
        ),
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
    let request = signed(confirm_activation(), &signer);
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
fn protocol_rejects_removed_host_operations() {
    for operation in [
        serde_json::json!({
            "type": "create-managed-directory",
            "area": "models",
            "relative_path": "sha256/aa",
        }),
        serde_json::json!({
            "type": "activate-agent-slot",
            "slot": "a",
            "artifact_sha256": "a".repeat(64),
            "artifact_signature": "b".repeat(128),
        }),
        serde_json::json!({
            "type": "restart-vonk-unit",
            "unit": "agent",
        }),
    ] {
        assert!(serde_json::from_value::<HostOperation>(operation).is_err());
    }
}

#[test]
fn authority_rejects_expiry_bad_signature_and_users_outside_agent_group() {
    let signer = signer(7);
    let verifier = grant_verifier(&signer);
    let request = signed(confirm_activation(), &signer);

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
    deny_container_listing: Arc<Mutex<bool>>,
    runtime_output: Arc<Mutex<Option<PathBuf>>>,
    calls: SharedCalls,
    runtime_container: Arc<Mutex<Option<(String, String)>>>,
    runtime_running: Arc<Mutex<bool>>,
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
    fn arm_package_rollback(
        &self,
        _node: &str,
        _source: &std::path::Path,
        _candidate: &std::path::Path,
        _digest: &str,
        _authority: &vonk_agent_protocol::PackageRollbackAuthority,
    ) -> Result<(), String> {
        Ok(())
    }
    fn package_activation_failed(&self) -> Result<(), String> {
        Ok(())
    }
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
                stderr: Vec::new(),
            });
        }
        let success = !*self.fail_dpkg.lock().unwrap();
        Ok(CommandOutput {
            success,
            stdout: Vec::new(),
            exit_code: Some(if success { 0 } else { 1 }),
            stderr: Vec::new(),
        })
    }
}

type SharedCalls = Arc<Mutex<Vec<(PathBuf, Vec<String>)>>>;

impl CommandRunner for RecordingRunner {
    fn arm_package_rollback(
        &self,
        _node: &str,
        _source: &std::path::Path,
        _candidate: &std::path::Path,
        _digest: &str,
        _authority: &vonk_agent_protocol::PackageRollbackAuthority,
    ) -> Result<(), String> {
        Ok(())
    }
    fn package_activation_failed(&self) -> Result<(), String> {
        Ok(())
    }
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
            && arguments.get(..2) == Some(&["container".to_owned(), "ls".to_owned()])
            && *self.deny_container_listing.lock().unwrap()
        {
            success = false;
            Vec::new()
        } else if executable == std::path::Path::new("/usr/bin/docker")
            && arguments.get(..2) == Some(&["image".to_owned(), "inspect".to_owned()])
        {
            format!("{}\tlinux\tarm64\tv1\t10001:10001\n", runtime_config_id()).into_bytes()
        } else if executable == std::path::Path::new("/usr/bin/docker")
            && arguments.get(..2) == Some(&["container".to_owned(), "inspect".to_owned()])
        {
            match self.runtime_container.lock().unwrap().as_ref() {
                Some(_)
                    if arguments
                        .get(3)
                        .is_some_and(|format| format.contains(".State.ExitCode")) =>
                {
                    b"137\ttrue\t\n".to_vec()
                }
                Some((digest, run_id))
                    if arguments
                        .get(3)
                        .is_some_and(|format| format.contains(".State.Running")) =>
                {
                    let prefix = if arguments[3].contains(".Id") {
                        format!("{}\t", "e".repeat(64))
                    } else {
                        String::new()
                    };
                    let installation = if arguments[3].contains("installation-id") {
                        "\tinstallation-1"
                    } else {
                        ""
                    };
                    format!(
                        "{prefix}{}\t{digest}\ttrue\t{run_id}{installation}\n",
                        self.runtime_running.lock().unwrap(),
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
            && arguments.first().is_some_and(|value| value == "logs")
        {
            b"fixture startup stderr: missing runtime module\n".to_vec()
        } else if executable == std::path::Path::new("/usr/bin/docker")
            && arguments.first().is_some_and(|value| value == "run")
        {
            if let Some(path) = self.runtime_output.lock().unwrap().take() {
                fs::write(path, b"prepared").map_err(|error| error.to_string())?;
            }
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
        Ok(CommandOutput {
            success,
            stdout,
            exit_code: Some(if success { 0 } else { 1 }),
            stderr: Vec::new(),
        })
    }
}

fn fixture() -> (TempDir, ManagedRoots, RecordingRunner, Ed25519KeyPair) {
    let temp = tempfile::tempdir().unwrap();
    let data = temp.path().join("data");
    let agent_data = temp.path().join("agent-data");
    let roots = ManagedRoots::under(&data).with_agent_data(&agent_data);
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
    signed_package(&roots, &signer(9), b"signed source deb");
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
    let (
        start_plan_sha256,
        stop_plan_sha256,
        run_generation,
        runtime_run_id,
        runtime_target_id,
        runtime_installation_id,
    ) = match request.action {
        HostRuntimeAction::Start => {
            if let Some(plan) = request.start_plan.as_ref() {
                (
                    Some(hex_sha256(&canonical_json(plan).unwrap())),
                    None,
                    Some(plan.run_generation),
                    Some(plan.run_id),
                    Some(plan.run_id),
                    Some(plan.installation_id),
                )
            } else if let Some(plan) = request.job_plan.as_ref() {
                (
                    Some(hex_sha256(&canonical_json(plan).unwrap())),
                    None,
                    Some(plan.run_generation),
                    Some(plan.run_id),
                    Some(plan.job_id),
                    Some(plan.installation_id),
                )
            } else {
                (None, None, request.run_generation, None, None, None)
            }
        }
        HostRuntimeAction::Stop => {
            if let Some(plan) = request.stop_plan.as_ref() {
                (
                    None,
                    Some(hex_sha256(&canonical_json(plan).unwrap())),
                    Some(plan.run_generation),
                    Some(plan.run_id),
                    Some(plan.target_runtime_id),
                    Some(plan.installation_id),
                )
            } else {
                (None, None, request.run_generation, None, None, None)
            }
        }
        _ => (None, None, None, None, None, None),
    };
    HostOperation::ExecuteContainerRuntimeRequestOperation(
        ExecuteContainerRuntimeRequestOperation {
            type_: "execute-container-runtime-request".into(),
            action: match request.action {
                HostRuntimeAction::RuntimePreflight => ContainerRuntimeAction::RuntimePreflight,
                HostRuntimeAction::ImagePull => ContainerRuntimeAction::ImagePull,
                HostRuntimeAction::ImageInspect => ContainerRuntimeAction::ImageInspect,
                HostRuntimeAction::RunInspect => ContainerRuntimeAction::RunInspect,
                HostRuntimeAction::Start => ContainerRuntimeAction::Start,
                HostRuntimeAction::Stop => ContainerRuntimeAction::Stop,
                HostRuntimeAction::InstallationCleanup => {
                    ContainerRuntimeAction::InstallationCleanup
                }
            },
            fence: request.fence,
            request_sha256: digest,
            installation_id: request.installation_id,
            reconciliation_identity: None,
            run_generation,
            runtime_installation_id,
            runtime_run_id,
            runtime_target_id,
            start_plan_sha256,
            stop_plan_sha256,
        },
    )
}

fn runtime_request(action: HostRuntimeAction, arguments: Vec<String>) -> HostRuntimeRequest {
    HostRuntimeRequest {
        action,
        fence: Uuid::parse_str("30000000-0000-4000-8000-000000000003").unwrap(),
        arguments,
        installation_id: None,
        reconciliation_identity: None,
        job_plan: None,
        run_generation: None,
        start_plan: None,
        stop_plan: None,
    }
}

#[test]
fn granted_image_pull_receipts_the_pinned_manifest() {
    let (_temp, roots, runner, release) = fixture();
    let address = "c".repeat(64);
    let manifest = format!("sha256:{address}");
    let config = runtime_config_id();
    let image_reference = format!("localhost/vonk/compiled-runtime-{address}@{manifest}");
    let request = runtime_request(
        HostRuntimeAction::ImagePull,
        vec![
            "127.0.0.1:41000".to_owned(),
            manifest.clone(),
            config.clone(),
            image_reference.clone(),
        ],
    );
    let request_digest = write_runtime_request(&roots, &request);
    let executor =
        OperationExecutor::new(roots.clone(), release.public_key().as_ref(), runner, None).unwrap();

    executor
        .execute(&runtime_operation(&request, request_digest))
        .unwrap();

    let receipt: serde_json::Value =
        serde_json::from_slice(&fs::read(roots.runtime_image_receipts.join(&address)).unwrap())
            .unwrap();
    assert_eq!(
        receipt,
        serde_json::json!({
            "image_config_id": config,
            "local_image_reference": image_reference,
            "platform_manifest_digest": manifest,
            "schema_version": 3,
        })
    );
}

#[test]
fn installation_cleanup_is_bound_to_the_signed_request_identity() {
    let (_temp, roots, runner, release) = fixture();
    let installation_id = Uuid::parse_str("10000000-0000-4000-8000-000000000001").unwrap();
    let mut request = runtime_request(HostRuntimeAction::InstallationCleanup, vec![]);
    request.installation_id = Some(installation_id);
    request.validate().unwrap();
    let request_digest = write_runtime_request(&roots, &request);
    let operation = runtime_operation(&request, request_digest.clone());
    let executor =
        OperationExecutor::new(roots.clone(), release.public_key().as_ref(), runner, None).unwrap();

    executor.execute(&operation).unwrap();
    let mut mismatched = runtime_operation(&request, request_digest);
    let HostOperation::ExecuteContainerRuntimeRequestOperation(operation) = &mut mismatched else {
        unreachable!();
    };
    operation.installation_id =
        Some(Uuid::parse_str("20000000-0000-4000-8000-000000000002").unwrap());

    assert!(matches!(
        executor.execute(&mismatched),
        Err(OperationError::InvalidOperation)
    ));
}

#[test]
fn helper_rejects_argv_only_start_and_stop_before_container_mutation() {
    let (_temp, roots, runner, release) = fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        None,
    )
    .unwrap();

    let start = runtime_request(
        HostRuntimeAction::Start,
        vec!["run".to_owned(), "--privileged".to_owned()],
    );
    let start_digest = write_runtime_request(&roots, &start);
    assert!(
        executor
            .execute(&runtime_operation(&start, start_digest))
            .is_err()
    );

    let stop = runtime_request(HostRuntimeAction::Stop, Vec::new());
    let stop_digest = write_runtime_request(&roots, &stop);
    assert!(
        executor
            .execute(&runtime_operation(&stop, stop_digest))
            .is_err()
    );
    assert!(
        runner.calls.lock().unwrap().is_empty(),
        "untyped argv must never authorize Docker"
    );
}

#[test]
fn ungranted_inspection_frame_can_only_inspect() {
    let (_temp, roots, runner, release) = fixture();
    let executor = OperationExecutor::new(
        roots.clone(),
        release.public_key().as_ref(),
        runner.clone(),
        None,
    )
    .unwrap();
    let mut cleanup = runtime_request(HostRuntimeAction::InstallationCleanup, Vec::new());
    cleanup.installation_id =
        Some(Uuid::parse_str("10000000-0000-4000-8000-000000000001").unwrap());
    let mut granted_only = Vec::new();
    for request in [
        runtime_request(HostRuntimeAction::Start, vec!["run".to_owned()]),
        runtime_request(HostRuntimeAction::Stop, Vec::new()),
        runtime_request(HostRuntimeAction::ImagePull, vec!["registry".to_owned()]),
        cleanup,
    ] {
        let digest = write_runtime_request(&roots, &request);
        assert!(executor.inspect_recipe_run(&digest).is_err());
        granted_only.push(runtime_operation(&request, digest));
    }
    assert!(
        runner.calls.lock().unwrap().is_empty(),
        "an ungranted frame must never reach the container runtime for another action"
    );

    let frame = canonical_json(&RecipeRunInspectionRequest {
        include_logs: None,
        request_id: Uuid::new_v4(),
        request_sha256: "a".repeat(64),
    })
    .unwrap();
    assert!(parse_inspection_request(&frame).is_ok());
    assert!(parse_request(&frame).is_err());
    let mut widened: serde_json::Value = serde_json::from_slice(&frame).unwrap();
    widened["action"] = serde_json::json!("stop");
    assert!(parse_inspection_request(&canonical_json(&widened).unwrap()).is_err());
    let grant = signed(granted_only.remove(0), &signer(1));
    assert!(parse_inspection_request(&canonical_json(&grant).unwrap()).is_err());
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
            .execute_for_node(
                &HostOperation::InstallVonkDebOperation(InstallVonkDebOperation {
                    type_: "install-vonk-deb".into(),
                    rollback: rollback_authority(),
                    package_sha256: bad_package,
                    package_signature: "0".repeat(128),
                }),
                Some(NODE_ID)
            )
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
        .execute_for_node(
            &HostOperation::InstallVonkDebOperation(InstallVonkDebOperation {
                type_: "install-vonk-deb".into(),
                rollback: rollback_authority(),
                package_sha256: digest.clone(),
                package_signature: hex::encode(signature.as_ref()),
            }),
            Some(NODE_ID),
        )
        .unwrap();
    let calls = runner.calls.lock().unwrap();
    assert_eq!(calls[0].0, PathBuf::from("/usr/bin/dpkg-deb"));
    assert_eq!(calls[0].1[0], "--field");
    assert_eq!(calls[1].1[2], "Architecture");
    assert_eq!(calls[2].0, PathBuf::from("/usr/bin/dpkg"));
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
                .execute_for_node(
                    &HostOperation::InstallVonkDebOperation(InstallVonkDebOperation {
                        type_: "install-vonk-deb".into(),
                        rollback: rollback_authority(),
                        package_sha256: digest,
                        package_signature: hex::encode(signature.as_ref()),
                    }),
                    Some(NODE_ID)
                )
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
        .execute_for_node(
            &HostOperation::InstallVonkDebOperation(InstallVonkDebOperation {
                type_: "install-vonk-deb".into(),
                rollback: rollback_authority(),
                package_sha256: digest.clone(),
                package_signature: signature,
            }),
            Some(NODE_ID),
        )
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
            .execute_for_node(
                &HostOperation::InstallVonkDebOperation(InstallVonkDebOperation {
                    type_: "install-vonk-deb".into(),
                    rollback: rollback_authority(),
                    package_sha256: digest,
                    package_signature: signature,
                }),
                Some(NODE_ID)
            )
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
