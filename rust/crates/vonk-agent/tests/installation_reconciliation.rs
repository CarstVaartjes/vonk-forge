#![forbid(unsafe_code)]

use std::{
    env, fs,
    io::{BufRead, BufReader, Write},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};

use rustix::fs::{FlockOperation, flock};
use serde_json::Value;
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::{
    oci::{OciError, OciRuntime},
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    workloads::CompiledExecutionPlan,
};
use vonk_agent_protocol::{RecipeReconciliationIdentity, canonical_json, hex_sha256};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";
const CHILD_ROOT_ENV: &str = "VONK_RECONCILIATION_CHILD_ROOT";
const CHILD_IDENTITY_ENV: &str = "VONK_RECONCILIATION_CHILD_IDENTITY";
const LOCK_PATH_ENV: &str = "VONK_RECONCILIATION_CHILD_LOCK_PATH";

struct NoProcess;

impl ProcessRunner for NoProcess {
    fn run(&self, _: Program, _: &[String], _: Duration) -> Result<ProcessOutput, ProcessError> {
        panic!("reconciliation must not launch a container process");
    }
}

fn runtime<'a>(data_root: &'a Path, runner: &'a NoProcess) -> OciRuntime<'a, NoProcess> {
    OciRuntime {
        runner,
        data_root,
        huggingface_curl_config: None,
    }
}

fn opaque_legacy_spec() -> (Value, String) {
    let mut spec: Value = serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    // Model the live blocker: an installation written before these required
    // placement fields were present. Reconciliation treats the retained
    // document as opaque; current executable-plan parsing must fail closed.
    let placement = spec["runtime"]["placement"].as_object_mut().unwrap();
    assert!(placement.remove("memory_floor_bytes").is_some());
    assert!(placement.remove("memory_kind").is_some());
    assert!(serde_json::from_value::<CompiledExecutionPlan>(spec.clone()).is_err());
    let recipe_digest = spec["identity"]["recipe_revision_sha256"]
        .as_str()
        .unwrap()
        .to_owned();
    (spec, recipe_digest)
}

fn identity_and_spec(installation_id: Uuid) -> (RecipeReconciliationIdentity, Vec<u8>) {
    let (spec, recipe_content_sha256) = opaque_legacy_spec();
    let spec_bytes = canonical_json(&spec).unwrap();
    let identity = RecipeReconciliationIdentity {
        schema_version: 1,
        node_id: NODE_ID.to_owned(),
        installation_id,
        install_operation_id: Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap(),
        install_operation_payload_sha256: "b".repeat(64),
        plan_digest: "c".repeat(64),
        recipe_revision_id: Uuid::parse_str("00000000-0000-4000-8000-000000000003").unwrap(),
        recipe_content_sha256,
        compiled_spec_canonical_sha256: hex_sha256(&spec_bytes),
    };
    (identity, spec_bytes)
}

fn seed_installation(data_root: &Path, identity: &RecipeReconciliationIdentity, spec_bytes: &[u8]) {
    let installation = installation_path(data_root, identity.installation_id);
    fs::create_dir_all(&installation).unwrap();
    fs::set_permissions(&installation, fs::Permissions::from_mode(0o700)).unwrap();
    write_private_file(&installation.join("spec.json"), spec_bytes);
    write_private_file(
        &installation.join("recipe-content.sha256"),
        identity.recipe_content_sha256.as_bytes(),
    );
}

fn write_private_file(path: &Path, bytes: &[u8]) {
    fs::write(path, bytes).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}

fn installation_path(data_root: &Path, installation_id: Uuid) -> PathBuf {
    data_root
        .join("installations")
        .join(installation_id.to_string())
}

fn child_command(test_name: &str) -> Command {
    let mut command = Command::new(env::current_exe().unwrap());
    command
        .arg("--exact")
        .arg(test_name)
        .arg("--nocapture")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

#[test]
fn opaque_install_is_removed_while_shared_model_and_image_cache_survive_and_receipt_replays() {
    let data = tempdir().unwrap();
    let runner = NoProcess;
    let (identity, spec_bytes) = identity_and_spec(Uuid::new_v4());
    seed_installation(data.path(), &identity, &spec_bytes);

    // These are the shared distribution cache and imported OCI archive. They
    // are outside the exact installation being reconciled.
    let shared_model = data.path().join("distribution/models").join("d".repeat(64));
    let shared_image = data.path().join("oci-archives").join("e".repeat(64));
    fs::create_dir_all(shared_model.parent().unwrap()).unwrap();
    fs::create_dir_all(shared_image.parent().unwrap()).unwrap();
    fs::write(&shared_model, b"shared model bytes").unwrap();
    fs::write(&shared_image, b"shared image bytes").unwrap();

    let runtime = runtime(data.path(), &runner);
    let prepared = runtime.prepare_reconciliation(&identity).unwrap();
    assert!(!prepared.complete);
    assert!(prepared.cleanup_receipt_sha256.is_none());

    let completed = runtime.finalize_reconciliation(&identity).unwrap();
    assert!(completed.complete);
    let receipt = completed
        .cleanup_receipt_sha256
        .as_deref()
        .expect("completed cleanup has a durable receipt");
    assert!(!installation_path(data.path(), identity.installation_id).exists());
    assert_eq!(fs::read(&shared_model).unwrap(), b"shared model bytes");
    assert_eq!(fs::read(&shared_image).unwrap(), b"shared image bytes");

    let replayed_prepare = runtime.prepare_reconciliation(&identity).unwrap();
    let replayed_finalize = runtime.finalize_reconciliation(&identity).unwrap();
    assert!(replayed_prepare.complete);
    assert!(replayed_finalize.complete);
    assert_eq!(
        replayed_prepare.cleanup_receipt_sha256.as_deref(),
        Some(receipt)
    );
    assert_eq!(
        replayed_finalize.cleanup_receipt_sha256.as_deref(),
        Some(receipt)
    );

    let plan: CompiledExecutionPlan = serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    assert!(matches!(
        runtime.install(
            &plan,
            &identity.installation_id.to_string(),
            &identity.recipe_content_sha256
        ),
        Err(OciError::Artifact)
    ));
    assert!(
        !installation_path(data.path(), identity.installation_id).exists(),
        "a completed cleanup receipt prevents an old install attempt from recreating the installation"
    );
}

#[test]
fn cleanup_accounting_excludes_the_runtime_cache_but_counts_other_installation_files() {
    let data = tempdir().unwrap();
    let runner = NoProcess;
    let (identity, spec_bytes) = identity_and_spec(Uuid::new_v4());
    seed_installation(data.path(), &identity, &spec_bytes);

    let installation = installation_path(data.path(), identity.installation_id);
    let private_cache = installation.join("runtime-cache");
    fs::create_dir_all(&private_cache).unwrap();
    write_private_file(
        &private_cache.join("root-owned-cache-state"),
        b"private cache",
    );
    let agent_owned_sidecar = b"agent-owned sidecar";
    write_private_file(&installation.join("agent-state"), agent_owned_sidecar);

    let runtime = runtime(data.path(), &runner);
    let prepared = runtime.prepare_reconciliation(&identity).unwrap();
    assert!(!prepared.complete);
    assert_eq!(
        prepared.removed_bytes,
        (spec_bytes.len() + identity.recipe_content_sha256.len() + agent_owned_sidecar.len())
            as u64,
        "only the exact helper-managed runtime-cache subtree is excluded from byte accounting"
    );
}

#[test]
fn opaque_filesystem_entry_inside_installation_refuses_preparation_until_removed() {
    let data = tempdir().unwrap();
    let runner = NoProcess;
    let (identity, spec_bytes) = identity_and_spec(Uuid::new_v4());
    seed_installation(data.path(), &identity, &spec_bytes);
    let installation = installation_path(data.path(), identity.installation_id);
    let outside = data.path().join("outside-private-file");
    write_private_file(&outside, b"must not be traversed");
    let link = installation.join("opaque-link");
    std::os::unix::fs::symlink(&outside, &link).unwrap();

    let runtime = runtime(data.path(), &runner);
    assert!(runtime.prepare_reconciliation(&identity).is_err());
    assert!(installation.exists());
    assert_eq!(fs::read(&outside).unwrap(), b"must not be traversed");

    fs::remove_file(link).unwrap();
    assert!(!runtime.prepare_reconciliation(&identity).unwrap().complete);
    assert!(runtime.finalize_reconciliation(&identity).unwrap().complete);
    assert_eq!(fs::read(outside).unwrap(), b"must not be traversed");
}

#[test]
fn prepared_checkpoint_survives_process_exit_and_resumes_in_a_new_process() {
    if let (Some(data_root), Some(encoded_identity)) =
        (env::var_os(CHILD_ROOT_ENV), env::var_os(CHILD_IDENTITY_ENV))
    {
        let identity: RecipeReconciliationIdentity =
            serde_json::from_str(&encoded_identity.to_string_lossy()).unwrap();
        let data_root = PathBuf::from(data_root);
        let runner = NoProcess;
        let prepared = runtime(&data_root, &runner)
            .prepare_reconciliation(&identity)
            .unwrap();
        assert!(!prepared.complete);
        assert!(prepared.cleanup_receipt_sha256.is_none());
        println!("PREPARED_CHECKPOINT");
        std::io::stdout().flush().unwrap();

        // The parent kills this process now, before finalization can run.
        let mut release = String::new();
        std::io::stdin().read_line(&mut release).unwrap();
        return;
    }

    let data = tempdir().unwrap();
    let (identity, spec_bytes) = identity_and_spec(Uuid::new_v4());
    seed_installation(data.path(), &identity, &spec_bytes);

    let mut child_command =
        child_command("prepared_checkpoint_survives_process_exit_and_resumes_in_a_new_process");
    child_command
        .env(CHILD_ROOT_ENV, data.path().as_os_str())
        .env(
            CHILD_IDENTITY_ENV,
            serde_json::to_string(&identity).unwrap(),
        )
        .stdin(Stdio::piped());
    let mut child = child_command.spawn().unwrap();
    let stdout = child.stdout.take().unwrap();
    let mut output = BufReader::new(stdout);
    let mut line = String::new();
    let mut checkpoint_confirmed = false;
    while output.read_line(&mut line).unwrap() != 0 {
        if line.contains("PREPARED_CHECKPOINT") {
            checkpoint_confirmed = true;
            break;
        }
        line.clear();
    }
    // Kill only after the child reports that prepare returned from its synced
    // checkpoint write. This injects process death at a deterministic boundary.
    let _ = child.kill();
    let status = child.wait().unwrap();
    assert!(
        checkpoint_confirmed,
        "prepare child did not confirm durable state: {line}"
    );
    assert!(
        !status.success(),
        "the child should be killed before finalization"
    );
    assert!(installation_path(data.path(), identity.installation_id).exists());

    let runner = NoProcess;
    let restarted_runtime = runtime(data.path(), &runner);
    let resumed = restarted_runtime.prepare_reconciliation(&identity).unwrap();
    assert!(!resumed.complete);
    assert!(resumed.cleanup_receipt_sha256.is_none());
    let completed = restarted_runtime
        .finalize_reconciliation(&identity)
        .unwrap();
    assert!(completed.complete);
    assert!(!installation_path(data.path(), identity.installation_id).exists());
    assert!(completed.cleanup_receipt_sha256.is_some());
}

#[test]
fn changed_source_identity_cannot_resume_an_existing_checkpoint() {
    let data = tempdir().unwrap();
    let runner = NoProcess;
    let (identity, spec_bytes) = identity_and_spec(Uuid::new_v4());
    seed_installation(data.path(), &identity, &spec_bytes);
    let runtime = runtime(data.path(), &runner);
    assert!(!runtime.prepare_reconciliation(&identity).unwrap().complete);

    let mut changed_identity = identity.clone();
    changed_identity.plan_digest = "f".repeat(64);
    assert!(runtime.prepare_reconciliation(&changed_identity).is_err());
    assert!(runtime.finalize_reconciliation(&changed_identity).is_err());
    assert!(installation_path(data.path(), identity.installation_id).exists());

    assert!(runtime.finalize_reconciliation(&identity).unwrap().complete);
}

#[test]
fn replaced_installation_directory_is_refused_even_when_its_files_match() {
    let data = tempdir().unwrap();
    let runner = NoProcess;
    let (identity, spec_bytes) = identity_and_spec(Uuid::new_v4());
    seed_installation(data.path(), &identity, &spec_bytes);
    let runtime = runtime(data.path(), &runner);
    runtime.prepare_reconciliation(&identity).unwrap();

    let original = installation_path(data.path(), identity.installation_id);
    let displaced_original = data.path().join("displaced-original-installation");
    fs::rename(&original, &displaced_original).unwrap();
    seed_installation(data.path(), &identity, &spec_bytes);

    assert!(runtime.finalize_reconciliation(&identity).is_err());
    assert!(
        original.exists(),
        "the replacement directory must be left intact"
    );
    assert!(
        displaced_original.exists(),
        "the original inode remains available for comparison"
    );
}

#[test]
fn missing_installation_without_a_checkpoint_does_not_poison_a_later_prepare() {
    let data = tempdir().unwrap();
    let runner = NoProcess;
    let (identity, spec_bytes) = identity_and_spec(Uuid::new_v4());
    let runtime = runtime(data.path(), &runner);

    assert!(runtime.prepare_reconciliation(&identity).is_err());
    seed_installation(data.path(), &identity, &spec_bytes);
    assert!(!runtime.prepare_reconciliation(&identity).unwrap().complete);
    assert!(runtime.finalize_reconciliation(&identity).unwrap().complete);
}

#[test]
fn separate_process_lock_contention_is_retryable_after_the_owner_exits() {
    if let Some(lock_path) = env::var_os(LOCK_PATH_ENV) {
        let file = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(lock_path)
            .unwrap();
        flock(&file, FlockOperation::NonBlockingLockExclusive).unwrap();
        println!("LOCK_HELD");
        std::io::stdout().flush().unwrap();

        let mut release = String::new();
        std::io::stdin().read_line(&mut release).unwrap();
        assert_eq!(release.trim(), "release");
        return;
    }

    let data = tempdir().unwrap();
    let runner = NoProcess;
    let (identity, spec_bytes) = identity_and_spec(Uuid::new_v4());
    seed_installation(data.path(), &identity, &spec_bytes);
    let runtime = runtime(data.path(), &runner);
    runtime.prepare_reconciliation(&identity).unwrap();

    let lock_path = data
        .path()
        .join("installation-reconciliation")
        .join(format!("{}.lock", identity.installation_id));
    let mut child =
        child_command("separate_process_lock_contention_is_retryable_after_the_owner_exits");
    child.env(LOCK_PATH_ENV, lock_path.as_os_str());
    let mut child = child.spawn().unwrap();
    let stdout = child.stdout.take().unwrap();
    let mut output = BufReader::new(stdout);
    let mut line = String::new();
    let mut lock_confirmed = false;
    while output.read_line(&mut line).unwrap() != 0 {
        if line.contains("LOCK_HELD") {
            lock_confirmed = true;
            break;
        }
        line.clear();
    }
    assert!(
        lock_confirmed,
        "lock-holder child did not confirm its lock: {line}"
    );

    assert!(matches!(
        runtime.prepare_reconciliation(&identity),
        Err(OciError::ReconciliationBusy)
    ));
    child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(b"release\n")
        .unwrap();
    drop(child.stdin.take());
    let result = child.wait_with_output().unwrap();
    assert!(
        result.status.success(),
        "lock-holder child failed: {}",
        String::from_utf8_lossy(&result.stderr)
    );

    assert!(runtime.finalize_reconciliation(&identity).unwrap().complete);
}
