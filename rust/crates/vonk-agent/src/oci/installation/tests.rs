#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn releasing_a_models_pages_keeps_its_bytes_and_refuses_a_directory() {
    // The hint must be exactly that: the artifact stays on disk, unchanged,
    // and only its resident pages are dropped. A directory is a caller
    // mistake rather than a silently ignored no-op.
    let directory = tempdir().unwrap();
    let path = directory.path().join("model.safetensors");
    let content = vec![7_u8; 4 * 1024 * 1024];
    fs::write(&path, &content).unwrap();
    // Read it once so the pages are resident before the release.
    let observed = fs::read(&path).unwrap();
    assert_eq!(observed.len(), content.len());
    release_page_cache(&path).unwrap();
    assert_eq!(fs::read(&path).unwrap(), content);
    assert!(release_page_cache(directory.path()).is_err());
}

#[test]
fn preload_estimates_never_refuse_tight_kits_or_unknown_readings() {
    let directory = tempdir().unwrap();
    let meminfo = directory.path().join("meminfo");
    let runtime = OciRuntime {
        runner: &Gb10MemoryRunner,
        data_root: directory.path(),
    };
    fs::write(&meminfo, "MemAvailable: 1 kB\n").unwrap();
    let warning = runtime.report_preload_memory(128 * 1024_u64.pow(3), &meminfo);
    assert!(
        warning
            .preflight
            .iter()
            .any(|p| p.name == "below_estimated_peak" && p.value == "true")
    );
    // A new attempt remains admitted; absent bookkeeping is informational.
    let unknown =
        runtime.report_preload_memory(128 * 1024_u64.pow(3), &directory.path().join("absent"));
    assert!(
        unknown
            .preflight
            .iter()
            .any(|p| p.name == "mem_available_bytes"
                && p.value
                    == vonk_agent_protocol::generated::ResourceTermProblem::Unknown.as_str())
    );
}

#[test]
fn routine_recipe_uninstall_retains_shared_model_cache() {
    let data = tempdir().unwrap();
    let (installation_id, installation, plan) = persisted_installation(data.path());
    let recipe_digest = plan.identity.recipe_revision_sha256.clone();
    authorize_installation(&installation, &recipe_digest);
    let cached = data
        .path()
        .join("distribution")
        .join("models")
        .join(&plan.artifacts[0].sha256);
    fs::create_dir_all(cached.parent().unwrap()).unwrap();
    fs::write(&cached, b"primary").unwrap();

    let runner = NoProcess;
    runtime(data.path(), &runner)
        .uninstall(&installation_id, &recipe_digest)
        .unwrap();

    assert!(!installation.exists());
    assert_eq!(fs::read(cached).unwrap(), b"primary");
}

#[test]
fn compiled_models_reject_duplicate_final_target() {
    let mut value = compiled_plan();
    let duplicate = value["artifacts"][0].clone();
    value["artifacts"] = json!([duplicate.clone(), duplicate]);
    let plan: crate::workloads::CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    let result = materialize_compiled_models(
        Path::new("/tmp/vonk-agent-test-data"),
        &plan,
        "cb555393-764b-4eb6-8f15-b416d289428f",
    );
    assert!(matches!(result, Err(OciError::Workload(_))));
}

#[test]
fn installed_bytes_sum_file_lengths_and_refuse_anything_but_files_and_directories() {
    let data = tempdir().unwrap();
    let (installation_id, installation, _) = persisted_installation(data.path());
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    let expected: u64 = [
        "spec.json",
        "models/primary/config.json",
        "models/secondary/config.json",
        "model-metadata.json",
    ]
    .iter()
    .map(|name| fs::metadata(installation.join(name)).unwrap().len())
    .sum();
    assert_eq!(runtime.installed_bytes(&installation_id).unwrap(), expected);

    symlink(installation.join("spec.json"), installation.join("link")).unwrap();
    assert!(runtime.installed_bytes(&installation_id).is_err());
    fs::remove_file(installation.join("link")).unwrap();
    assert_eq!(runtime.installed_bytes(&installation_id).unwrap(), expected);
}
