#![cfg(test)]

use super::*;

pub(in crate::operations) const RUN_ID: &str = "40000000-0000-4000-8000-000000000004";

#[derive(Clone, Copy)]
pub(in crate::operations) struct MissingContainerRunner;

impl CommandRunner for MissingContainerRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        assert_eq!(executable, Path::new("/usr/bin/docker"));
        let daemon_probe = arguments.first().map(String::as_str) == Some("version");
        let missing_listing = arguments.first().map(String::as_str) == Some("container")
            && arguments.get(1).map(String::as_str) == Some("ls");
        Ok(CommandOutput {
            success: daemon_probe || missing_listing,
            stdout: Vec::new(),
            stderr: Vec::new(),
            exit_code: Some(if daemon_probe || missing_listing {
                0
            } else {
                1
            }),
        })
    }
}

pub(in crate::operations) struct ExistingRuntimeGenerationRunner {
    pub(in crate::operations) logical_run_id: uuid::Uuid,
    pub(in crate::operations) target_id: uuid::Uuid,
    pub(in crate::operations) installation_id: uuid::Uuid,
    pub(in crate::operations) run_generation: u64,
    pub(in crate::operations) plan_digest: String,
    pub(in crate::operations) calls: Arc<Mutex<Vec<Vec<String>>>>,
}

impl CommandRunner for ExistingRuntimeGenerationRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        assert_eq!(executable, Path::new("/usr/bin/docker"));
        self.calls.lock().unwrap().push(arguments.to_vec());
        let (success, stdout, exit_code) = match arguments.first().map(String::as_str) {
            Some("container") if arguments.get(1).map(String::as_str) == Some("inspect") => (
                true,
                format!(
                    "true\t{}\t{}\t{}\t{}\t{}\n",
                    self.logical_run_id,
                    self.target_id,
                    self.installation_id,
                    self.run_generation,
                    self.plan_digest
                )
                .into_bytes(),
                0,
            ),
            Some("stop" | "rm") => (true, b"container-id\n".to_vec(), 0),
            _ => return Err("unexpected command".to_owned()),
        };
        Ok(CommandOutput {
            success,
            stdout,
            stderr: Vec::new(),
            exit_code: Some(exit_code),
        })
    }
}

#[derive(Clone)]
pub(in crate::operations) struct ReconciliationListingRunner {
    pub(in crate::operations) response: CommandOutput,
    pub(in crate::operations) calls: Arc<Mutex<Vec<Vec<String>>>>,
}

impl ReconciliationListingRunner {
    pub(in crate::operations) fn new(response: CommandOutput) -> Self {
        Self {
            response,
            calls: Arc::new(Mutex::new(Vec::new())),
        }
    }
}

impl CommandRunner for ReconciliationListingRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        assert_eq!(executable, Path::new("/usr/bin/docker"));
        self.calls.lock().unwrap().push(arguments.to_vec());
        assert_eq!(arguments.first().map(String::as_str), Some("container"));
        assert_eq!(arguments.get(1).map(String::as_str), Some("ls"));
        Ok(self.response.clone())
    }
}

pub(in crate::operations) fn helper_reconciliation_fixture() -> (
    TempDir,
    ManagedRoots,
    RecipeReconciliationIdentity,
    PathBuf,
    PathBuf,
) {
    let temp = tempfile::tempdir().unwrap();
    let data = temp.path().join("controller-data");
    let agent_data = temp.path().join("spark-agent-data");
    let roots = ManagedRoots::under(&data).with_agent_data(&agent_data);
    fs::create_dir_all(&roots.data).unwrap();
    let installation_id = uuid::Uuid::new_v4();
    let installation = agent_data
        .join("installations")
        .join(installation_id.to_string());
    let runtime_cache = installation.join("runtime-cache");
    let shared_cache = agent_data.join("shared-model-cache").join("model.bin");
    fs::create_dir_all(runtime_cache.join("home/private")).unwrap();
    fs::create_dir_all(shared_cache.parent().unwrap()).unwrap();
    fs::set_permissions(&installation, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(&runtime_cache, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(
        runtime_cache.join("home"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    fs::set_permissions(
        runtime_cache.join("home/private"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    fs::write(
        runtime_cache.join("home/private/private-cache.bin"),
        b"private",
    )
    .unwrap();
    fs::write(&shared_cache, b"shared model cache").unwrap();

    let recipe_digest = "e".repeat(64);
    let spec = serde_json::json!({
        "identity": {"recipe_revision_sha256": recipe_digest},
        "invalid legacy-shaped plan": {"opaque": [false, null]},
    });
    let identity = RecipeReconciliationIdentity {
        installation_id,
        plan_digest: "c".repeat(64),
    };
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(&spec).unwrap(),
    )
    .unwrap();
    fs::set_permissions(
        installation.join("spec.json"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    fs::write(
        installation.join("recipe-content.sha256"),
        recipe_digest.as_bytes(),
    )
    .unwrap();
    fs::set_permissions(
        installation.join("recipe-content.sha256"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    (temp, roots, identity, runtime_cache, shared_cache)
}

#[derive(Clone, Copy)]
pub(in crate::operations) struct DeniedContainerInspectRunner;

impl CommandRunner for DeniedContainerInspectRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        assert_eq!(executable, Path::new("/usr/bin/docker"));
        let daemon_probe = arguments.first().map(String::as_str) == Some("version");
        Ok(CommandOutput {
            success: daemon_probe,
            stdout: Vec::new(),
            stderr: Vec::new(),
            exit_code: Some(if daemon_probe { 0 } else { 1 }),
        })
    }
}

#[derive(Clone, Default)]
pub(in crate::operations) struct RecordingAclRunner {
    pub(in crate::operations) calls: Arc<Mutex<Vec<Vec<String>>>>,
}

impl CommandRunner for RecordingAclRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        assert_eq!(executable, Path::new("/usr/bin/setfacl"));
        self.calls.lock().unwrap().push(arguments.to_vec());
        Ok(CommandOutput {
            success: true,
            stdout: Vec::new(),
            stderr: Vec::new(),
            exit_code: Some(0),
        })
    }
}

/// Docker as seen by an image pull: `image` holds what the daemon has
/// under each reference, and every call is recorded.
#[derive(Clone, Default)]
pub(in crate::operations) struct PullRunner {
    pub(in crate::operations) images: Arc<Mutex<std::collections::BTreeMap<String, String>>>,
    pub(in crate::operations) calls: Arc<Mutex<Vec<Vec<String>>>>,
    pub(in crate::operations) pulled_config: String,
    pub(in crate::operations) pull_fails: bool,
}

impl CommandRunner for PullRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        assert_eq!(executable, Path::new("/usr/bin/docker"));
        self.calls.lock().unwrap().push(arguments.to_vec());
        let mut images = self.images.lock().unwrap();
        let ok = |stdout: Vec<u8>| CommandOutput {
            success: true,
            stdout,
            stderr: Vec::new(),
            exit_code: Some(0),
        };
        let missing = CommandOutput {
            success: false,
            stdout: b"\n".to_vec(),
            stderr: Vec::new(),
            exit_code: Some(1),
        };
        let words = arguments.iter().map(String::as_str).collect::<Vec<_>>();
        Ok(match words.as_slice() {
            ["pull", .., reference] if self.pull_fails => {
                let _ = reference;
                missing
            }
            ["pull", .., reference] => {
                images.insert((*reference).to_owned(), self.pulled_config.clone());
                ok(Vec::new())
            }
            ["image", "inspect", _, _, reference] => match images.get(*reference) {
                Some(config) => {
                    ok(format!("{config}\tlinux\tarm64\tv1\t10001:10001\n").into_bytes())
                }
                None => missing,
            },
            ["tag", source, target] => {
                let config = images.get(*source).cloned().expect("tag source exists");
                images.insert((*target).to_owned(), config);
                ok(Vec::new())
            }
            ["image", "rm", reference] => {
                images.remove(*reference);
                ok(Vec::new())
            }
            other => panic!("unexpected docker call {other:?}"),
        })
    }
}

pub(in crate::operations) fn pull_arguments(manifest: &str, config: &str) -> Vec<String> {
    vec![
        "127.0.0.1:41000".to_owned(),
        format!("sha256:{manifest}"),
        format!("sha256:{config}"),
        format!("localhost/vonk/compiled-runtime-{manifest}@sha256:{manifest}"),
    ]
}

#[derive(Clone, Default)]
pub(in crate::operations) struct KernelAclRunner {
    pub(in crate::operations) calls: Arc<Mutex<Vec<Vec<String>>>>,
}

impl CommandRunner for KernelAclRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        if executable != Path::new("/usr/bin/setfacl") {
            return Err("unexpected command".to_owned());
        }
        self.calls.lock().unwrap().push(arguments.to_vec());
        if arguments.first().map(String::as_str) == Some("-R") {
            let uid = arguments
                .get(2)
                .and_then(|value| value.split(':').nth(1))
                .and_then(|value| value.parse::<u32>().ok())
                .ok_or("invalid ACL user")?;
            let root = Path::new(arguments.last().ok_or("missing ACL path")?);
            apply_kernel_read_acl(root, uid)?;
        }
        Ok(CommandOutput {
            success: true,
            stdout: Vec::new(),
            stderr: Vec::new(),
            exit_code: Some(0),
        })
    }
}

pub(in crate::operations) fn apply_kernel_read_acl(path: &Path, uid: u32) -> Result<(), String> {
    let metadata = fs::symlink_metadata(path).map_err(|error| error.to_string())?;
    let mode = metadata.mode() & 0o777;
    let user_object = ((mode >> 6) & 0o7) as u16;
    let group_object = ((mode >> 3) & 0o7) as u16;
    let other = (mode & 0o7) as u16;
    let runtime = if metadata.is_dir() || mode & 0o111 != 0 {
        0o5
    } else {
        0o4
    };
    let mut acl = 2_u32.to_le_bytes().to_vec();
    for (tag, permissions, identifier) in [
        (0x0001_u16, user_object, u32::MAX),
        (0x0002, runtime, uid),
        (0x0004, group_object, u32::MAX),
        (0x0010, group_object | runtime, u32::MAX),
        (0x0020, other, u32::MAX),
    ] {
        acl.extend_from_slice(&tag.to_le_bytes());
        acl.extend_from_slice(&permissions.to_le_bytes());
        acl.extend_from_slice(&identifier.to_le_bytes());
    }
    xattr::set(path, "system.posix_acl_access", &acl).map_err(|error| error.to_string())?;
    if metadata.is_dir() {
        for entry in fs::read_dir(path).map_err(|error| error.to_string())? {
            apply_kernel_read_acl(&entry.map_err(|error| error.to_string())?.path(), uid)?;
        }
    }
    Ok(())
}

pub(in crate::operations) fn runtime_effect_identity(run_generation: u64) -> RuntimeEffectIdentity {
    RuntimeEffectIdentity {
        runtime_id: uuid::Uuid::parse_str(RUN_ID).unwrap(),
        installation_id: uuid::Uuid::parse_str("50000000-0000-4000-8000-000000000005").unwrap(),
        run_generation,
    }
}

pub(in crate::operations) fn compiled_plan_for_runtime_authority() -> CompiledExecutionPlan {
    let mut value: serde_json::Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    value["runtime"]["placement"]["endpoint_address"] = serde_json::json!("100.100.20.30");
    value["security"]["network_mode"] = serde_json::json!("bridge");
    let compiled: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    compiled.validate().unwrap();
    compiled
}

pub(in crate::operations) fn recipe_start_plan_for_authority(
    run_generation: u64,
) -> RecipeStartPayload {
    let compiled = compiled_plan_for_runtime_authority();
    RecipeStartPayload {
        compiled_execution_plan: compiled,
        installation_id: runtime_effect_identity(run_generation).installation_id,
        mapping_id: uuid::Uuid::parse_str("70000000-0000-4000-8000-000000000007").unwrap(),
        phase: None,
        plan_digest: "c".repeat(64),
        recipe_revision_id: uuid::Uuid::parse_str("60000000-0000-4000-8000-000000000006").unwrap(),
        run_generation,
        run_id: uuid::Uuid::parse_str(RUN_ID).unwrap(),
        start_deadline: None,
    }
}

pub(in crate::operations) fn recipe_stop_plan_for_authority(
    run_generation: u64,
    cancel_pending_start: bool,
) -> RecipeStopPayload {
    let compiled = compiled_plan_for_runtime_authority();
    RecipeStopPayload {
        cancel_pending_start,
        rank: compiled.runtime.placement.rank.clone(),
        role: compiled.runtime.placement.role.clone(),
        recipe_content_sha256: compiled.identity.recipe_revision_sha256.clone(),
        stop_timeout_seconds: compiled.lifecycle.stop_timeout_seconds,
        installation_id: runtime_effect_identity(run_generation).installation_id,
        mapping_id: uuid::Uuid::parse_str("70000000-0000-4000-8000-000000000007").unwrap(),
        plan_digest: "c".repeat(64),
        recipe_revision_id: uuid::Uuid::parse_str("60000000-0000-4000-8000-000000000006").unwrap(),
        run_generation,
        run_id: uuid::Uuid::parse_str(RUN_ID).unwrap(),
        target_runtime_id: uuid::Uuid::parse_str(RUN_ID).unwrap(),
    }
}

pub(in crate::operations) struct RuntimeRequestTestParts {
    pub(in crate::operations) action: HostRuntimeAction,
    pub(in crate::operations) arguments: Vec<String>,
    pub(in crate::operations) run_generation: u64,
    pub(in crate::operations) start_plan: Option<RecipeStartPayload>,
    pub(in crate::operations) stop_plan: Option<RecipeStopPayload>,
}

pub(in crate::operations) fn runtime_request_identity(
    fence: &uuid::Uuid,
    parts: RuntimeRequestTestParts,
) -> HostRuntimeRequest {
    HostRuntimeRequest {
        action: parts.action,
        fence: *fence,
        arguments: parts.arguments,
        installation_id: None,
        reconciliation_identity: None,
        job_plan: None,
        run_generation: Some(parts.run_generation),
        start_plan: parts.start_plan,
        stop_plan: parts.stop_plan,
    }
}

pub(in crate::operations) struct NameOnlyRunner {
    pub(in crate::operations) inspect: CommandOutput,
    pub(in crate::operations) listing: CommandOutput,
}

impl CommandRunner for NameOnlyRunner {
    fn run(&self, executable: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
        assert_eq!(executable, Path::new("/usr/bin/docker"));
        match arguments.get(1).map(String::as_str) {
            Some("inspect") => Ok(self.inspect.clone()),
            Some("ls") => Ok(self.listing.clone()),
            _ => Err("unexpected command".to_owned()),
        }
    }
}

pub(in crate::operations) fn docker_output(
    success: bool,
    stdout: &str,
    exit_code: i32,
) -> CommandOutput {
    CommandOutput {
        success,
        stdout: stdout.as_bytes().to_vec(),
        stderr: Vec::new(),
        exit_code: Some(exit_code),
    }
}

pub(in crate::operations) fn runtime_fixture() -> (TempDir, ManagedRoots) {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(&temp.path().join("data"));
    initialize_runtime_fixture(&roots);
    (temp, roots)
}

pub(in crate::operations) fn runtime_fixture_with_separate_agent_data() -> (TempDir, ManagedRoots) {
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(&temp.path().join("data"))
        .with_agent_data(&temp.path().join("agent-data"));
    initialize_runtime_fixture(&roots);
    (temp, roots)
}

pub(in crate::operations) fn initialize_runtime_fixture(roots: &ManagedRoots) {
    fs::create_dir_all(runtime_models(roots).join("primary")).unwrap();
    fs::create_dir_all(
        roots
            .agent_data
            .join("installations")
            .join("installation-1")
            .join("runtime-cache"),
    )
    .unwrap();
    fs::create_dir_all(roots.agent_data.join("runs").join(RUN_ID).join("outputs")).unwrap();
    fs::create_dir_all(roots.agent_data.join("runs").join(RUN_ID).join("inputs")).unwrap();
    let metadata = roots.agent_data.join("run-metadata").join(RUN_ID);
    fs::create_dir_all(&metadata).unwrap();
    fs::write(metadata.join("runtime.json"), b"{}").unwrap();
}

pub(in crate::operations) fn artifact_path(roots: &ManagedRoots, key: char) -> PathBuf {
    runtime_models(roots)
        .join("primary")
        .join(format!("artifact-{key}.bin"))
}

pub(in crate::operations) fn runtime_models(roots: &ManagedRoots) -> PathBuf {
    roots
        .agent_data
        .join("installations")
        .join("installation-1")
        .join("models")
}

pub(in crate::operations) fn runtime_arguments(
    roots: &ManagedRoots,
    mounts: &[(PathBuf, &str, bool)],
) -> Vec<String> {
    let mut arguments = vec![
        "run".to_owned(),
        "--detach".to_owned(),
        "--name".to_owned(),
        format!("vonk-{RUN_ID}"),
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
        "none".to_owned(),
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
        "HOME=/outputs/cache/home".to_owned(),
        "--env".to_owned(),
        "XDG_CACHE_HOME=/outputs/cache".to_owned(),
        "--env".to_owned(),
        "TMPDIR=/outputs/tmp".to_owned(),
        "--env".to_owned(),
        "VONK_RUNTIME_SPEC=/run/vonk/runtime.json".to_owned(),
    ];
    for (source, target, readonly) in mounts {
        arguments.extend([
            "--mount".to_owned(),
            format!(
                "type=bind,src={},dst={target}{}",
                source.display(),
                if *readonly { ",readonly" } else { "" }
            ),
        ]);
    }
    arguments.extend([
        "--mount".to_owned(),
        format!(
            "type=bind,src={},dst=/outputs",
            roots
                .agent_data
                .join("runs")
                .join(RUN_ID)
                .join("outputs")
                .display()
        ),
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
            roots
                .agent_data
                .join("run-metadata")
                .join(RUN_ID)
                .join("runtime.json")
                .display()
        ),
        format!(
            "localhost/vonk/recipe-build-20000000-0000-4000-8000-000000000002@sha256:{}",
            "c".repeat(64)
        ),
        "/opt/vonk/bin/vllm".to_owned(),
    ]);
    arguments
}

pub(in crate::operations) fn job_runtime_arguments(
    roots: &ManagedRoots,
    model: PathBuf,
) -> Vec<String> {
    let mut arguments = runtime_arguments(roots, &[(model, "/models", true)]);
    arguments.remove(
        arguments
            .iter()
            .position(|value| value == "--detach")
            .unwrap(),
    );
    let image = arguments
        .iter()
        .position(|value| value.starts_with("localhost/vonk/"))
        .unwrap();
    arguments.splice(
        image..image,
        [
            "--env".to_owned(),
            "VONK_JOB_TIMEOUT_SECONDS=3600".to_owned(),
            "--mount".to_owned(),
            format!(
                "type=bind,src={},dst=/inputs,readonly",
                roots
                    .agent_data
                    .join("runs")
                    .join(RUN_ID)
                    .join("inputs")
                    .display()
            ),
        ],
    );
    arguments
}
