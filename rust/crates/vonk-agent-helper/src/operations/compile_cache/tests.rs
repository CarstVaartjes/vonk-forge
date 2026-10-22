use super::*;
use crate::operations::test_support::*;
use std::sync::{Arc, Mutex};

#[derive(Clone)]
struct Runner {
    host: Arc<Mutex<String>>,
}
impl CommandRunner for Runner {
    fn run(&self, executable: &Path, args: &[String]) -> Result<CommandOutput, String> {
        if executable == Path::new(gpu_clock::NVIDIA_SMI) {
            return Ok(docker_output(true, &self.host.lock().unwrap(), 0));
        }
        if executable == Path::new("/usr/bin/setfacl") {
            let status = Command::new(executable)
                .args(args)
                .status()
                .map_err(|e| e.to_string())?;
            return Ok(docker_output(
                status.success(),
                "",
                status.code().unwrap_or(1),
            ));
        }
        Err("unexpected command".into())
    }
}
fn fixture() -> (
    tempfile::TempDir,
    OperationExecutor<Runner>,
    ValidatedDockerRun,
) {
    fixture_for_gpu(true)
}
fn fixture_for_gpu(
    gpu: bool,
) -> (
    tempfile::TempDir,
    OperationExecutor<Runner>,
    ValidatedDockerRun,
) {
    let (root, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    fs::create_dir(&model).unwrap();
    let mut args = runtime_arguments(&roots, &[(model, "/models", true)]);
    if gpu {
        args.splice(
            1..1,
            ["--device".to_owned(), "nvidia.com/gpu=all".to_owned()],
        );
    }
    let run = validate_docker_run(&args, &roots, None).unwrap();
    let executor = OperationExecutor::new(
        roots,
        &[0; 32],
        Runner {
            host: Arc::new(Mutex::new("12.1, 590.48\n".into())),
        },
        None,
    )
    .unwrap();
    (root, executor, run)
}
fn source(run: &ValidatedDockerRun) -> PathBuf {
    let suffix = format!(
        ",dst={}",
        vonk_agent_protocol::generated::CompileCacheMount::OutputsCache.as_str()
    );
    run.arguments
        .iter()
        .find_map(|arg| {
            arg.strip_suffix(&suffix)
                .and_then(|s| s.strip_prefix("type=bind,src="))
                .map(PathBuf::from)
        })
        .unwrap()
}

#[test]
fn repeated_runs_reuse_bytes_but_image_driver_architecture_and_config_isolate_them() {
    // Catches per-run/per-installation keys, and stale reuse after a driver,
    // GPU architecture, image, or engine compile option change.
    let (_root, executor, mut first) = fixture();
    let key = executor.compile_cache_identity(&first).unwrap();
    executor.bind_compile_cache(&mut first).unwrap();
    let cache = source(&first);
    fs::write(cache.join("kernel"), b"compiled").unwrap();
    let (_next_root, _, mut next) = fixture();
    // A run's paths and names are not compilation inputs.
    next.run_id = uuid::Uuid::new_v4().to_string();
    assert_eq!(executor.compile_cache_identity(&next).unwrap(), key);
    executor.bind_compile_cache(&mut next).unwrap();
    assert_eq!(source(&next), cache);
    assert_eq!(fs::read(cache.join("kernel")).unwrap(), b"compiled");
    // A fresh dual-Spark rendezvous must not create another compile cache.
    let port_index = next.image_index + 1;
    next.arguments.splice(
        next.image_index..next.image_index,
        ["--env".to_owned(), "VONK_MASTER_PORT=29500".to_owned()],
    );
    next.image_index += 2;
    assert_eq!(executor.compile_cache_identity(&next).unwrap(), key);
    next.arguments[port_index] = "VONK_MASTER_PORT=29501".to_owned();
    assert_eq!(executor.compile_cache_identity(&next).unwrap(), key);
    for host in ["12.1, 591.1\n", "12.0, 590.48\n"] {
        *executor.runner.host.lock().unwrap() = host.into();
        assert_ne!(executor.compile_cache_identity(&next).unwrap(), key);
    }
    *executor.runner.host.lock().unwrap() = "12.1, 590.48\n".into();
    next.arguments.push("--different-compile-config".into());
    assert_ne!(executor.compile_cache_identity(&next).unwrap(), key);
    next.arguments.pop();
    let option_index = next.image_index;
    next.arguments.splice(
        option_index..option_index,
        [
            "--env".to_owned(),
            "VLLM_USE_STANDALONE_COMPILE=1".to_owned(),
        ],
    );
    next.image_index += 2;
    assert_ne!(executor.compile_cache_identity(&next).unwrap(), key);
    next.arguments.drain(option_index..option_index + 2);
    next.image_index -= 2;
    next.platform_manifest_digest = format!("sha256:{}", "f".repeat(64));
    assert_ne!(executor.compile_cache_identity(&next).unwrap(), key);
}

#[test]
fn corrupted_unwritable_or_unknown_cache_falls_back_without_poisoning_next_start() {
    // Catches a cache setup error vetoing Docker launch, following a cache
    // symlink, or reusing an unknown-host bucket.
    for fault in ["symlink", "unwritable", "host"] {
        let (_root, executor, mut run) = fixture();
        let key = executor.compile_cache_identity(&run).unwrap();
        let store = executor.roots.data.join(STORE);
        ensure_private_directory(&store, None).unwrap();
        let entry = store.join(&key);
        ensure_private_directory(&entry, None).unwrap();
        match fault {
            "symlink" => std::os::unix::fs::symlink(&run.outputs, entry.join("data")).unwrap(),
            "unwritable" => {
                fs::create_dir(entry.join("data")).unwrap();
                fs::set_permissions(entry.join("data"), fs::Permissions::from_mode(0o500)).unwrap();
            }
            _ => *executor.runner.host.lock().unwrap() = "N/A\n".into(),
        }
        executor.bind_compile_cache(&mut run).unwrap();
        let fallback = source(&run);
        assert!(fallback.starts_with(executor.roots.data.join(FALLBACK_STORE)));
        assert!(!fallback.starts_with(executor.roots.agent_data.join("runs")));
        assert!(
            fallback
                .parent()
                .unwrap()
                .file_name()
                .unwrap()
                .to_str()
                .unwrap()
                .starts_with(&run.run_id)
        );
        assert!(!fallback.starts_with(&store));
        assert!(fallback.join("home").is_dir());
        assert!(exact_runtime_acl(&fallback, run.uid));
        // Removing the fault lets the same identity populate normally.
        if entry.join("data").is_symlink() {
            fs::remove_file(entry.join("data")).unwrap();
        } else if entry.join("data").exists() {
            fs::remove_dir(entry.join("data")).unwrap();
        }
        *executor.runner.host.lock().unwrap() = "12.1, 590.48\n".into();
        run.cache_root = fallback;
        executor.bind_compile_cache(&mut run).unwrap();
        assert_eq!(source(&run), entry.join("data"));
    }
}

#[test]
fn unused_keys_expire_but_mounted_keys_get_a_full_day_after_last_use() {
    // Catches TTL measured from creation, deleting a mounted key, and orphan
    // caches with no retention owner.
    let (_root, executor, mut run) = fixture();
    executor.bind_compile_cache(&mut run).unwrap();
    let data = source(&run);
    let malformed = data
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("malformed-key");
    fs::write(&malformed, b"corrupted directory").unwrap();
    let now = SystemTime::now();
    let deadline = || Instant::now() + Duration::from_secs(10);
    executor.collect_compile_caches(now + GRACE * 2, deadline(), &HashSet::from([data.clone()]));
    assert!(data.exists());
    executor.collect_compile_caches(
        now + GRACE * 2 + Duration::from_secs(60),
        deadline(),
        &HashSet::new(),
    );
    assert!(data.exists());
    executor.collect_compile_caches(
        now + GRACE * 3 + Duration::from_secs(60),
        deadline(),
        &HashSet::new(),
    );
    assert!(!data.exists());
    assert!(!malformed.exists());
}

#[test]
fn pressure_reclaims_unused_keys_early_but_never_a_mounted_key() {
    // Catches a collector which ignores the shared low-space policy or lets
    // pressure override a live container's ownership.
    let (_root, executor, mut run) = fixture();
    executor.bind_compile_cache(&mut run).unwrap();
    let data = source(&run);
    let request = vonk_agent_protocol::generated::StoreRetentionRequest {
        issued_at: SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs(),
        expires_at: SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs()
            + 300,
        required_free_bytes: 16 * 1024_u64.pow(4),
        remove_model_objects: vec![],
        protected_model_objects: vec![],
        protected_image_addresses: vec![],
    };
    fs::write(
        executor.roots.agent_data.join("store-retention.json"),
        canonical_json(&request).unwrap(),
    )
    .unwrap();
    let now = SystemTime::now();
    executor.collect_compile_caches(
        now,
        Instant::now() + Duration::from_secs(10),
        &HashSet::from([data.clone()]),
    );
    assert!(data.exists());
    executor.collect_compile_caches(
        now,
        Instant::now() + Duration::from_secs(10),
        &HashSet::new(),
    );
    assert!(!data.exists());
}

#[test]
fn simultaneous_population_and_restart_reuse_the_same_cache() {
    // Catches destructive setup, racy mkdir refusal, and process-local cache
    // identity that cannot reconnect after helper restart.
    let (_root, executor, first) = fixture();
    let (_other, _, second) = fixture();
    let barrier = std::sync::Barrier::new(2);
    let populate = |mut run: ValidatedDockerRun| {
        barrier.wait();
        executor.bind_compile_cache(&mut run).unwrap();
        source(&run)
    };
    let (a, b) = std::thread::scope(|scope| {
        let a = scope.spawn(|| populate(first));
        let b = scope.spawn(|| populate(second));
        (a.join().unwrap(), b.join().unwrap())
    });
    assert_eq!(a, b);
    fs::write(a.join("compiled"), b"kernel").unwrap();
    let restarted = OperationExecutor::new(
        executor.roots.clone(),
        &[0; 32],
        executor.runner.clone(),
        None,
    )
    .unwrap();
    let (_third, _, mut third) = fixture();
    restarted.bind_compile_cache(&mut third).unwrap();
    assert_eq!(source(&third), a);
    assert_eq!(fs::read(a.join("compiled")).unwrap(), b"kernel");
}

#[test]
fn gpu_free_plans_persist_without_querying_an_unknown_nvidia_host() {
    // Catches unconditional NVIDIA probing: CPU-only acceptance/workloads
    // otherwise fall back on every start and cannot retain compile bytes.
    let (_root, executor, mut run) = fixture_for_gpu(false);
    *executor.runner.host.lock().unwrap() = "N/A".to_owned();
    executor.bind_compile_cache(&mut run).unwrap();
    let cache = source(&run);
    assert!(cache.starts_with(executor.roots.data.join(STORE)));
    fs::write(cache.join("kernel"), b"cpu compiled").unwrap();
    let (_next_root, _, mut next) = fixture_for_gpu(false);
    executor.bind_compile_cache(&mut next).unwrap();
    assert_eq!(source(&next), cache);
    assert_eq!(fs::read(cache.join("kernel")).unwrap(), b"cpu compiled");
    next.gpu = true;
    assert!(executor.compile_cache_identity(&next).is_err());
}

#[test]
fn fallback_scratch_is_collected_by_the_helper_even_if_the_primary_store_is_corrupt() {
    // Catches private helper-owned fallback trees being stranded beneath
    // agent-owned runs, and a broken primary root blocking fallback retention.
    let (_root, executor, mut run) = fixture();
    fs::write(executor.roots.data.join(STORE), b"not a cache directory").unwrap();
    executor.bind_compile_cache(&mut run).unwrap();
    let cache = source(&run);
    assert!(cache.starts_with(executor.roots.data.join(FALLBACK_STORE)));
    let now = SystemTime::now();
    let deadline = || Instant::now() + Duration::from_secs(10);
    executor.collect_compile_caches(now + GRACE * 2, deadline(), &HashSet::from([cache.clone()]));
    assert!(cache.exists());
    executor.collect_compile_caches(
        now + GRACE * 3 + Duration::from_secs(1),
        deadline(),
        &HashSet::new(),
    );
    assert!(
        !cache.exists(),
        "unused fallback escaped its privileged collector"
    );
}
