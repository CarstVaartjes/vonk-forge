//! Delayed signed authority must never reach Docker after installation cleanup.
use super::*;
use crate::operations::test_support::*;
use crate::protocol::{GrantClaims, GrantSignature, GrantVerifier, PeerIdentity, SignedGrant};
use ring::signature::{Ed25519KeyPair, KeyPair};
use std::sync::{Arc, Mutex};

#[derive(Clone)]
struct IntentRunner {
    calls: Arc<Mutex<Vec<Vec<String>>>>,
    config: String,
}
impl CommandRunner for IntentRunner {
    fn run(&self, executable: &Path, args: &[String]) -> Result<CommandOutput, String> {
        self.calls.lock().unwrap().push(args.to_vec());
        let image = args.first().map(String::as_str) == Some("image");
        let missing = args.get(1).map(String::as_str) == Some("inspect") && !image;
        let stdout = if image {
            format!("{}\tlinux\tarm64\tv1\t10001:10001\n", self.config).into_bytes()
        } else if args.first().map(String::as_str) == Some("run") {
            "f".repeat(64).into_bytes()
        } else {
            Vec::new()
        };
        assert!(
            executable == Path::new("/usr/bin/docker")
                || executable == Path::new("/usr/bin/setfacl")
                || executable == Path::new(DOCKER_FIREWALL)
        );
        Ok(CommandOutput {
            success: !missing,
            stdout,
            stderr: Vec::new(),
            exit_code: Some(if missing { 1 } else { 0 }),
        })
    }
}

fn signed(operation: HostOperation, signer: &Ed25519KeyPair) -> SignedGrant {
    let claims = GrantClaims {
        schema_version: 1,
        authority: crate::protocol::AUTHORITY.to_owned(),
        request_id: uuid::Uuid::new_v4(),
        node_id: format!("spk_{}", "1".repeat(32)),
        issued_at: 2_100_000_000,
        expires_at: 2_100_000_120,
        operation,
    };
    let signature = signer.sign(&crate::protocol::canonical_signing_bytes(&claims).unwrap());
    SignedGrant {
        schema_version: 1,
        claims,
        signature: GrantSignature {
            algorithm: "ed25519".to_owned(),
            key_id: hex_sha256(signer.public_key().as_ref()),
            value: hex::encode(signature.as_ref()),
        },
    }
}

fn execute_signed(
    executor: &OperationExecutor<IntentRunner>,
    grant: &SignedGrant,
    signer: &Ed25519KeyPair,
) -> Result<OperationOutcome, OperationError> {
    // Serialize/consume the authenticated privileged ingress, not an internal
    // generation method or a manually deleted receipt presence check.
    let (mut peer, mut helper) = std::os::unix::net::UnixStream::pair().unwrap();
    crate::protocol::write_frame(&mut peer, &canonical_json(grant).unwrap()).unwrap();
    let parsed =
        crate::protocol::parse_request(&crate::protocol::read_frame(&mut helper).unwrap()).unwrap();
    GrantVerifier::new(signer.public_key().as_ref(), 971)
        .unwrap()
        .authorize(
            &parsed,
            &PeerIdentity {
                uid: 1001,
                primary_gid: 971,
                supplementary_gids: Vec::new(),
            },
            2_100_000_001,
        )
        .unwrap();
    executor.execute_for_node(&parsed.claims.operation, Some(&parsed.claims.node_id))
}

fn challenge(result: Result<OperationOutcome, OperationError>) -> String {
    match result {
        Err(OperationError::InstallationIntentObservationRequired { nonce }) => nonce,
        other => panic!("expected an authority challenge without effects: {other:?}"),
    }
}

#[test]
fn signed_cleanup_fences_never_executed_start_and_newer_intent_launches_after_repair() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(&temp.path().join("helper"))
        .with_agent_data(&temp.path().join("agent"));
    fs::create_dir_all(&roots.data).unwrap();
    fs::create_dir_all(&roots.runtime_requests).unwrap();
    let mut plan = recipe_start_plan_for_authority(1);
    plan.compiled_execution_plan.runtime_image.oci_layout_sha256 = plan
        .compiled_execution_plan
        .runtime_image
        .image_digest
        .strip_prefix("sha256:")
        .unwrap()
        .to_owned();
    let installation = roots
        .agent_data
        .join("installations")
        .join(plan.installation_id.to_string());
    let cache = installation.join("runtime-cache");
    fs::create_dir_all(&cache).unwrap();
    fs::set_permissions(&installation, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(&cache, fs::Permissions::from_mode(0o700)).unwrap();
    for artifact in &plan.compiled_execution_plan.artifacts {
        let path = installation
            .join("models")
            .join(&artifact.selection_id)
            .join(&artifact.path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, b"accepted model").unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }
    let run = roots.agent_data.join("runs").join(plan.run_id.to_string());
    let metadata = roots
        .agent_data
        .join("run-metadata")
        .join(plan.run_id.to_string());
    fs::create_dir_all(run.join("outputs")).unwrap();
    fs::create_dir_all(&metadata).unwrap();
    fs::write(metadata.join("runtime.json"), b"{}").unwrap();
    let runner = IntentRunner {
        calls: Arc::new(Mutex::new(Vec::new())),
        config: plan
            .compiled_execution_plan
            .runtime_image
            .local_image_config_id
            .clone(),
    };
    let executor = OperationExecutor::new(roots.clone(), &[0; 32], runner.clone(), None).unwrap();
    let signer = Ed25519KeyPair::from_seed_unchecked(&[7; 32]).unwrap();
    let arguments = executor
        .projected_runtime_arguments(
            &plan.compiled_execution_plan,
            plan.installation_id,
            plan.run_id,
        )
        .unwrap();
    let request = runtime_request_identity(
        &uuid::Uuid::new_v4(),
        RuntimeRequestTestParts {
            action: HostRuntimeAction::Start,
            arguments,
            run_generation: 1,
            start_plan: Some(plan.clone()),
            stop_plan: None,
        },
    );
    let make_operation = |request: &HostRuntimeRequest, nonce: Option<String>, ordinal| {
        let bytes = canonical_json(request).unwrap();
        let digest = hex_sha256(&bytes);
        fs::write(roots.runtime_requests.join(format!("{digest}.json")), bytes).unwrap();
        fs::set_permissions(
            roots.runtime_requests.join(format!("{digest}.json")),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        HostOperation::ExecuteContainerRuntimeRequestOperation(ExecuteContainerRuntimeRequestOperation {
            type_: vonk_agent_protocol::generated::HostOperationKind::ExecuteContainerRuntimeRequest.as_str().to_owned(),
            action: if request.action == HostRuntimeAction::Start { ContainerRuntimeAction::Start } else { ContainerRuntimeAction::InstallationCleanup },
            fence: request.fence, request_sha256: digest, installation_id: request.installation_id, reconciliation_identity: None,
            start_plan_sha256: request.start_plan.as_ref().map(|plan| hex_sha256(&canonical_json(plan).unwrap())), stop_plan_sha256: None,
            run_generation: request.run_generation,
            runtime_run_id: request.start_plan.as_ref().map(|plan| plan.run_id), runtime_target_id: request.start_plan.as_ref().map(|plan| plan.run_id), runtime_installation_id: request.start_plan.as_ref().map(|plan| plan.installation_id),
            installation_intent_nonce: nonce, installation_intent_ordinal: Some(ordinal),
        })
    };
    let nonce = challenge(execute_signed(
        &executor,
        &signed(make_operation(&request, None, 1), &signer),
        &signer,
    ));
    let delayed = signed(make_operation(&request, Some(nonce.clone()), 1), &signer);
    assert!(roots.data.join(RUNTIME_GENERATION_FENCE_DIRECTORY).exists());
    let cleanup = HostRuntimeRequest {
        action: HostRuntimeAction::InstallationCleanup,
        fence: uuid::Uuid::new_v4(),
        arguments: Vec::new(),
        installation_id: Some(plan.installation_id),
        reconciliation_identity: None,
        job_plan: None,
        run_generation: None,
        start_plan: None,
        stop_plan: None,
    };
    execute_signed(
        &executor,
        &signed(make_operation(&cleanup, Some(nonce), 1), &signer),
        &signer,
    )
    .unwrap();
    assert!(!cache.exists());
    let fresh_nonce = challenge(execute_signed(&executor, &delayed, &signer));
    assert!(
        execute_signed(
            &executor,
            &signed(
                make_operation(&request, Some(fresh_nonce.clone()), 1),
                &signer
            ),
            &signer
        )
        .is_err()
    );
    assert!(runner.calls.lock().unwrap().is_empty());
    // The agent prepares the accepted installation for the explicit newer
    // intent. The signed request itself must now reach and launch Docker.
    fs::create_dir(&cache).unwrap();
    fs::set_permissions(&cache, fs::Permissions::from_mode(0o700)).unwrap();
    execute_signed(
        &executor,
        &signed(make_operation(&request, Some(fresh_nonce), 2), &signer),
        &signer,
    )
    .unwrap();
    assert_eq!(
        runner
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|args| args.first().map(String::as_str) == Some("run"))
            .count(),
        1
    );
    // A damaged fence requires a new authenticated current-intent challenge,
    // and never revives the outstanding old signed grant.
    fs::write(
        roots
            .data
            .join("installation-intent-fences")
            .join(format!("{}.json", plan.installation_id)),
        b"damaged",
    )
    .unwrap();
    let repaired_nonce = challenge(execute_signed(&executor, &delayed, &signer));
    assert_eq!(
        runner
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|args| args.first().map(String::as_str) == Some("run"))
            .count(),
        1
    );
    execute_signed(
        &executor,
        &signed(make_operation(&request, Some(repaired_nonce), 2), &signer),
        &signer,
    )
    .unwrap();
    assert_eq!(
        runner
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|args| args.first().map(String::as_str) == Some("run"))
            .count(),
        2
    );
    let run_fence = roots
        .data
        .join(RUNTIME_GENERATION_FENCE_DIRECTORY)
        .join(runtime_generation_fence_filename(
            plan.installation_id,
            plan.run_id,
        ));
    let mut generation = 2;
    for damage in [Some(b"broken fence".as_slice()), None] {
        if let Some(bytes) = damage {
            fs::write(&run_fence, bytes).unwrap();
        } else {
            fs::remove_file(&run_fence).unwrap();
        }
        let before = runner.calls.lock().unwrap().len();
        let nonce = challenge(execute_signed(&executor, &delayed, &signer));
        assert_eq!(runner.calls.lock().unwrap().len(), before);
        // The delayed signed request cannot use authority from before repair.
        let current_nonce = challenge(execute_signed(&executor, &delayed, &signer));
        assert_ne!(nonce, current_nonce);
        generation += 1;
        let mut fresh = request.clone();
        fresh.fence = uuid::Uuid::new_v4();
        fresh.run_generation = Some(generation);
        fresh.start_plan.as_mut().unwrap().run_generation = generation;
        ensure_private_directory(&roots.runtime_image_receipts, None).unwrap();
        let receipt = roots.runtime_image_receipts.join(
            plan.compiled_execution_plan
                .runtime_image
                .image_digest
                .strip_prefix("sha256:")
                .unwrap(),
        );
        fs::write(&receipt, b"damaged disposable image receipt").unwrap();
        execute_signed(
            &executor,
            &signed(
                make_operation(&fresh, Some(current_nonce), generation),
                &signer,
            ),
            &signer,
        )
        .unwrap();
        let repaired: RuntimeImageReceipt = parse_strict(&fs::read(receipt).unwrap()).unwrap();
        assert_eq!(repaired.image_config_id, runner.config);
        let launched = runner
            .calls
            .lock()
            .unwrap()
            .iter()
            .filter(|args| args.first().map(String::as_str) == Some("run"))
            .count();
        assert_eq!(launched, generation as usize);
    }
}
