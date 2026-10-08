#![forbid(unsafe_code)]

use std::{
    env, fs,
    io::{BufRead, BufReader, Write},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{Mutex, MutexGuard},
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
use vonk_agent_protocol::{RecipeReconciliationIdentity, canonical_json};

const CHILD_ROOT_ENV: &str = "VONK_RECONCILIATION_CHILD_ROOT";
const CHILD_IDENTITY_ENV: &str = "VONK_RECONCILIATION_CHILD_IDENTITY";
const LOCK_PATH_ENV: &str = "VONK_RECONCILIATION_CHILD_LOCK_PATH";

// Reconciliation locks are flock(2) locks on an open file description. When
// one test thread spawns a child process, the forked child briefly shares every
// open descriptor of this process until exec closes it, so a lock another test
// thread just released can still be held by that child and a same-process
// relock reports `ReconciliationBusy`. The product treats that as retryable;
// these tests assert exact outcomes, so they run one at a time.
static SERIAL: Mutex<()> = Mutex::new(());

fn serial() -> MutexGuard<'static, ()> {
    SERIAL
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

struct NoProcess;

impl ProcessRunner for NoProcess {
    fn run(&self, _: Program, _: &[String], _: Duration) -> Result<ProcessOutput, ProcessError> {
        panic!("reconciliation must not launch a container process");
    }
}

fn runtime<'a>(data_root: &'a Path, runner: &'a NoProcess) -> OciRuntime<'a, NoProcess> {
    OciRuntime { runner, data_root }
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
    assert!(serde_json::from_value::<CompiledExecutionPlan>(spec.clone()).is_err());
    let recipe_digest = spec["identity"]["recipe_revision_sha256"]
        .as_str()
        .unwrap()
        .to_owned();
    (spec, recipe_digest)
}

fn identity_and_spec(installation_id: Uuid) -> (RecipeReconciliationIdentity, Vec<u8>) {
    let (spec, _) = opaque_legacy_spec();
    let spec_bytes = canonical_json(&spec).unwrap();
    let identity = RecipeReconciliationIdentity {
        installation_id,
        plan_digest: "c".repeat(64),
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
        opaque_legacy_spec().1.as_bytes(),
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
fn opaque_install_is_removed_while_shared_model_cache_survives_and_receipt_replays() {
    let _serial = serial();
    let data = tempdir().unwrap();
    let runner = NoProcess;
    let (identity, spec_bytes) = identity_and_spec(Uuid::new_v4());
    seed_installation(data.path(), &identity, &spec_bytes);

    // The shared distribution cache is outside the exact installation being
    // reconciled.
    let shared_model = data.path().join("distribution/models").join("d".repeat(64));
    fs::create_dir_all(shared_model.parent().unwrap()).unwrap();
    fs::write(&shared_model, b"shared model bytes").unwrap();

    let runtime = runtime(data.path(), &runner);
    let prepared = runtime.prepare_reconciliation(&identity).unwrap();
    assert!(!prepared.complete);

    let completed = runtime.finalize_reconciliation(&identity).unwrap();
    assert!(completed.complete);
    assert!(!installation_path(data.path(), identity.installation_id).exists());
    assert_eq!(fs::read(&shared_model).unwrap(), b"shared model bytes");

    let replayed_prepare = runtime.prepare_reconciliation(&identity).unwrap();
    let replayed_finalize = runtime.finalize_reconciliation(&identity).unwrap();
    assert!(replayed_prepare.complete);
    assert!(replayed_finalize.complete);

    let plan: CompiledExecutionPlan = serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    let store = data.path().join("distribution/models");
    fs::create_dir_all(&store).unwrap();
    for artifact in &plan.artifacts {
        write_private_file(
            &store.join(&artifact.sha256),
            &vec![0; artifact.size_bytes as usize],
        );
    }
    runtime
        .install(
            &plan,
            &identity.installation_id.to_string(),
            &plan.identity.recipe_revision_sha256,
        )
        .unwrap();
    runtime
        .verify_installation(&identity.installation_id.to_string())
        .unwrap();
    assert!(installation_path(data.path(), identity.installation_id).exists());
}

#[test]
fn prepared_checkpoint_survives_process_exit_and_resumes_in_a_new_process() {
    let _serial = serial();
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
    let completed = restarted_runtime
        .finalize_reconciliation(&identity)
        .unwrap();
    assert!(completed.complete);
    assert!(!installation_path(data.path(), identity.installation_id).exists());
}

#[test]
fn current_request_supersedes_stored_checkpoint_identity() {
    let _serial = serial();
    let data = tempdir().unwrap();
    let runner = NoProcess;
    let (identity, spec_bytes) = identity_and_spec(Uuid::new_v4());
    seed_installation(data.path(), &identity, &spec_bytes);
    let runtime = runtime(data.path(), &runner);
    assert!(!runtime.prepare_reconciliation(&identity).unwrap().complete);

    let mut changed_identity = identity.clone();
    changed_identity.plan_digest = "f".repeat(64);
    assert!(
        !runtime
            .prepare_reconciliation(&changed_identity)
            .unwrap()
            .complete
    );
    assert!(
        runtime
            .finalize_reconciliation(&changed_identity)
            .unwrap()
            .complete
    );
    assert!(!installation_path(data.path(), identity.installation_id).exists());
    seed_installation(data.path(), &identity, &spec_bytes);
    assert!(!runtime.prepare_reconciliation(&identity).unwrap().complete);
}

#[test]
fn replaced_directory_is_preserved_until_current_request_reobserves_it() {
    let _serial = serial();
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
    assert!(!runtime.prepare_reconciliation(&identity).unwrap().complete);
    assert!(runtime.finalize_reconciliation(&identity).unwrap().complete);
    assert!(!original.exists());
    assert!(displaced_original.exists());
    seed_installation(data.path(), &identity, &spec_bytes);
    assert!(!runtime.prepare_reconciliation(&identity).unwrap().complete);
}

#[test]
fn missing_installation_without_a_checkpoint_does_not_poison_a_later_prepare() {
    let _serial = serial();
    let data = tempdir().unwrap();
    let runner = NoProcess;
    let (identity, spec_bytes) = identity_and_spec(Uuid::new_v4());
    let runtime = runtime(data.path(), &runner);

    assert!(runtime.prepare_reconciliation(&identity).unwrap().complete);
    seed_installation(data.path(), &identity, &spec_bytes);
    assert!(!runtime.prepare_reconciliation(&identity).unwrap().complete);
    assert!(runtime.finalize_reconciliation(&identity).unwrap().complete);
}

#[test]
fn separate_process_lock_contention_is_retryable_after_the_owner_exits() {
    let _serial = serial();
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
