use super::*;
use crate::protocol::{GrantClaims, GrantSignature, GrantVerifier, PeerIdentity, SignedGrant};
use ring::signature::{Ed25519KeyPair, KeyPair};
use std::sync::{Arc, Mutex, mpsc};
use vonk_agent_protocol::generated::{HostOperationKind, PackageRollbackSource};

#[derive(Clone, Default)]
struct InstallRunner(Arc<Mutex<Vec<PathBuf>>>);
impl CommandRunner for InstallRunner {
    fn arm_package_rollback(
        &self,
        _node: &str,
        _source: &Path,
        _candidate: &Path,
        _digest: &str,
        _authority: &PackageRollbackAuthority,
    ) -> Result<(), String> {
        Ok(())
    }
    fn run(&self, executable: &Path, args: &[String]) -> Result<CommandOutput, String> {
        self.0.lock().unwrap().push(executable.to_owned());
        let stdout = if executable == Path::new("/usr/bin/docker") {
            format!(
                "{}\ttrue\ttrue\t{}\n",
                "f".repeat(64),
                super::super::test_support::RUN_ID
            )
            .into_bytes()
        } else if executable == Path::new("/usr/bin/dpkg-deb") {
            if args[2] == "Package" {
                b"vonk-forge-agent\n".to_vec()
            } else {
                b"arm64\n".to_vec()
            }
        } else {
            Vec::new()
        };
        Ok(CommandOutput {
            success: true,
            stdout,
            stderr: Vec::new(),
            exit_code: Some(0),
        })
    }
}

#[test]
fn scan_overlap_and_expired_copy_release_custody_before_fresh_signed_install() {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(temp.path());
    fs::create_dir_all(&roots.incoming).unwrap();
    fs::create_dir_all(roots.package_custody.parent().unwrap()).unwrap();
    let release = Ed25519KeyPair::from_seed_unchecked(&[9; 32]).unwrap();
    let candidate = b"candidate";
    let source = b"source";
    let candidate_digest = hex_sha256(candidate);
    let source_digest = hex_sha256(source);
    let sign_artifact = |digest: &str| {
        hex::encode(
            release
                .sign(&artifact_signing_bytes("deb", digest).unwrap())
                .as_ref(),
        )
    };
    for (digest, bytes) in [
        (&candidate_digest, candidate.as_slice()),
        (&source_digest, source.as_slice()),
    ] {
        let path = roots.incoming.join(format!("{digest}.deb"));
        fs::write(&path, bytes).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }
    fs::create_dir_all(&roots.package_custody).unwrap();
    fs::set_permissions(&roots.package_custody, fs::Permissions::from_mode(0o700)).unwrap();
    let malformed = roots.package_custody.join("unclassified-neighbor");
    fs::create_dir(&malformed).unwrap();
    fs::write(malformed.join("retained"), b"unproven").unwrap();
    fs::create_dir_all(&roots.runtime_requests).unwrap();
    let observation = HostRuntimeRequest {
        action: HostRuntimeAction::RunInspect,
        fence: uuid::Uuid::new_v4(),
        arguments: vec![super::super::test_support::RUN_ID.to_owned()],
        installation_id: None,
        reconciliation_identity: None,
        job_plan: None,
        run_generation: None,
        start_plan: None,
        stop_plan: None,
    };
    let bytes = canonical_json(&observation).unwrap();
    let observation_digest = hex_sha256(&bytes);
    let observation_path = roots
        .runtime_requests
        .join(format!("{observation_digest}.json"));
    fs::write(&observation_path, bytes).unwrap();
    fs::set_permissions(observation_path, fs::Permissions::from_mode(0o600)).unwrap();
    let runner = InstallRunner::default();
    let executor = Arc::new(
        OperationExecutor::new(
            roots.clone(),
            release.public_key().as_ref(),
            runner.clone(),
            None,
        )
        .unwrap(),
    );
    let rollback = PackageRollbackAuthority {
        source: PackageRollbackSource {
            package_sha256: source_digest.clone(),
            package_signature: sign_artifact(&source_digest),
            package_version: "0.1.0".into(),
            binary_sha256: "a".repeat(64),
            helper_sha256: "b".repeat(64),
        },
        attempt_nonce: "c".repeat(64),
        activation_deadline: 2_100_000_120,
    };
    let operation = HostOperation::InstallVonkDebOperation(InstallVonkDebOperation {
        type_: HostOperationKind::InstallVonkDeb.as_str().to_owned(),
        package_sha256: candidate_digest.clone(),
        package_signature: sign_artifact(&candidate_digest),
        rollback,
    });
    let authority = Ed25519KeyPair::from_seed_unchecked(&[7; 32]).unwrap();
    let signed_execute = || {
        let claims = GrantClaims {
            schema_version: 1,
            authority: crate::protocol::AUTHORITY.into(),
            request_id: uuid::Uuid::new_v4(),
            node_id: format!("spk_{}", "1".repeat(32)),
            issued_at: 2_100_000_000,
            expires_at: 2_100_000_120,
            operation: operation.clone(),
        };
        let signature = authority.sign(&crate::protocol::canonical_signing_bytes(&claims).unwrap());
        let grant = SignedGrant {
            schema_version: 1,
            claims,
            signature: GrantSignature {
                algorithm: "ed25519".into(),
                key_id: hex_sha256(authority.public_key().as_ref()),
                value: hex::encode(signature.as_ref()),
            },
        };
        let parsed = crate::protocol::parse_request(&canonical_json(&grant).unwrap()).unwrap();
        GrantVerifier::new(authority.public_key().as_ref(), 971)
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
    };
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let scan_executor = executor.clone();
    let (finished_tx, finished_rx) = mpsc::channel();
    let scan = thread::spawn(move || {
        let result = scan_executor.observe_package_custody(|| {
            entered_tx.send(()).unwrap();
            release_rx.recv_timeout(Duration::from_secs(3)).unwrap();
        });
        // The real scanner has released its custody owner before completion.
        finished_tx.send(result).unwrap();
    });
    entered_rx.recv_timeout(Duration::from_secs(3)).unwrap();
    assert!(signed_execute().is_err());
    assert!(executor.inspect_recipe_run(&observation_digest).unwrap());
    assert!(
        !runner
            .0
            .lock()
            .unwrap()
            .iter()
            .any(|path| path == Path::new("/usr/bin/dpkg"))
    );
    release_tx.send(()).unwrap();
    finished_rx
        .recv_timeout(Duration::from_secs(3))
        .unwrap()
        .unwrap();
    drop(scan);
    // Expiry is a copy observation, never an ingress digest refusal. Its
    // partial candidate and mutex owner are gone before the next request.
    assert!(
        executor
            .take_package_custody_until(
                &roots.incoming.join(format!("{candidate_digest}.deb")),
                &candidate_digest,
                &sign_artifact(&candidate_digest),
                Instant::now()
            )
            .is_err()
    );
    assert_eq!(fs::read_dir(&roots.package_custody).unwrap().count(), 1);
    assert_eq!(fs::read(malformed.join("retained")).unwrap(), b"unproven");
    assert!(executor.inspect_recipe_run(&observation_digest).unwrap());
    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _guard = executor.package_install.lock().unwrap();
        panic!("interrupted package owner");
    }));
    signed_execute().unwrap();
    assert_eq!(
        runner
            .0
            .lock()
            .unwrap()
            .iter()
            .filter(|path| path.as_path() == Path::new("/usr/bin/dpkg"))
            .count(),
        1
    );
    assert_eq!(fs::read_dir(&roots.package_custody).unwrap().count(), 1);
}
