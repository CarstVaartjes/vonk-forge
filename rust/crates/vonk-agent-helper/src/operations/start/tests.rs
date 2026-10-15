#![cfg(test)]
//! What the helper hands to `docker run` for an accepted workload.
use super::*;
use crate::operations::test_support::*;

#[derive(Clone)]
struct LaunchRunner {
    calls: Arc<Mutex<Vec<Vec<String>>>>,
    config: String,
    /// The host's job container listing: an earlier job of the same run.
    earlier_job: String,
}

impl CommandRunner for LaunchRunner {
    fn run(&self, _executable: &Path, args: &[String]) -> Result<CommandOutput, String> {
        self.calls.lock().unwrap().push(args.to_vec());
        let first = args.first().map(String::as_str);
        // No container exists yet; the image is the accepted one.
        let missing =
            first == Some("container") && args.get(1).map(String::as_str) == Some("inspect");
        let stdout = match first {
            Some("image") => {
                format!("{}\tlinux\tarm64\tv1\t10001:10001\n", self.config).into_bytes()
            }
            Some("run") => "f".repeat(64).into_bytes(),
            Some("container") if args.iter().any(|arg| arg.starts_with("{{.Names}}")) => {
                self.earlier_job.clone().into_bytes()
            }
            _ => Vec::new(),
        };
        Ok(CommandOutput {
            success: !missing,
            stdout,
            stderr: Vec::new(),
            exit_code: Some(i32::from(missing)),
        })
    }
}

fn job_plan(mut plan: RecipeStartPayload) -> RecipeStartPayload {
    let mut value = serde_json::to_value(&plan.compiled_execution_plan).unwrap();
    value["endpoint"] = serde_json::Value::Null;
    value["runtime"]["placement"]["port"] = serde_json::Value::Null;
    value["runtime"]["placement"]["endpoint_address"] = serde_json::Value::Null;
    value["security"]["network_mode"] = serde_json::json!("none");
    value["job"] = serde_json::json!({
        "interface": "image-job",
        "input": {
            "required": true,
            "media_types": ["application/octet-stream"],
            "max_bytes": 1024,
            "slots": null
        },
        "timeout_seconds": 90
    });
    let mounts = value["security"]["mounts"].as_array_mut().unwrap();
    mounts.push(serde_json::json!({"source": "inputs", "target": "/inputs"}));
    plan.compiled_execution_plan = serde_json::from_value(value).unwrap();
    plan
}

/// Launches `plan` through the helper and returns the `docker run` argv.
fn launched_run(plan: RecipeStartPayload) -> Vec<String> {
    launched(plan).1
}

/// Launches `plan` through the helper and returns the signed request argv and
/// the `docker run` argv.
fn launched(plan: RecipeStartPayload) -> (Vec<String>, Vec<String>) {
    let (arguments, launched, calls) = launch(plan, None);
    launched.unwrap();
    let run = calls
        .iter()
        .find(|call| call.first().map(String::as_str) == Some("run"))
        .expect("the workload reached docker run")
        .clone();
    (arguments, run)
}

/// Launches `plan` on a host that may still hold an earlier job of its run,
/// owned by a live helper Start, and returns the signed request argv, the
/// launch result and the Docker calls.
fn launch(
    mut plan: RecipeStartPayload,
    earlier_job: Option<uuid::Uuid>,
) -> (
    Vec<String>,
    Result<RuntimeStartLaunch, OperationError>,
    Vec<Vec<String>>,
) {
    let image = &mut plan.compiled_execution_plan.runtime_image;
    image.oci_layout_sha256 = image
        .image_digest
        .strip_prefix("sha256:")
        .unwrap()
        .to_owned();
    let temp = tempfile::tempdir().unwrap();
    let roots = ManagedRoots::under(&temp.path().join("helper"))
        .with_agent_data(&temp.path().join("agent"));
    fs::create_dir_all(&roots.data).unwrap();
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
    fs::create_dir_all(run.join("inputs")).unwrap();
    fs::create_dir_all(&metadata).unwrap();
    fs::write(metadata.join("runtime.json"), b"{}").unwrap();
    let image = &plan.compiled_execution_plan.runtime_image;
    let runner = LaunchRunner {
        calls: Arc::default(),
        config: image.local_image_config_id.clone(),
        earlier_job: earlier_job.map_or_else(String::new, |job| {
            format!(
                "vonk-{job}\trunning\t{}\t{job}\t{}\t{}\t{}\n",
                plan.run_id, plan.installation_id, plan.run_generation, plan.plan_digest
            )
        }),
    };
    let executor = OperationExecutor::new(roots, &[0; 32], runner.clone(), None).unwrap();
    let arguments = executor
        .projected_runtime_arguments(
            &plan.compiled_execution_plan,
            plan.installation_id,
            plan.run_id,
        )
        .unwrap();
    let identity = RuntimeEffectIdentity {
        installation_id: plan.installation_id,
        runtime_id: plan.run_id,
        run_generation: plan.run_generation,
    };
    let _earlier_owner = earlier_job.map(|job| {
        executor
            .job_cancellation
            .begin(RuntimeEffectIdentity {
                runtime_id: job,
                ..identity
            })
            .unwrap()
    });
    let launched = executor.runtime_start_locked(
        &arguments,
        identity,
        plan.run_id,
        &plan.plan_digest,
        &image.local_image_config_id,
    );
    let calls = runner.calls.lock().unwrap().clone();
    (arguments, launched, calls)
}

/// Returns the image position after checking that no helper option split a
/// Docker flag from its value.
fn image_after_intact_options(run: &[String]) -> usize {
    let image = run
        .iter()
        .position(|argument| argument.starts_with("localhost/vonk/"))
        .expect("the accepted image is launched");
    let entrypoint = run.iter().position(|a| a == "--entrypoint").unwrap();
    assert!(run[entrypoint + 1].starts_with('/'), "{run:?}");
    for (index, argument) in run[..image].iter().enumerate() {
        if argument == "--label" {
            assert!(run[index + 1].starts_with("ai.vonkforge."), "{run:?}");
        }
    }
    image
}

#[test]
fn a_job_launches_with_its_entrypoint_and_labels_intact() {
    let run = launched_run(job_plan(recipe_start_plan_for_authority(1)));
    image_after_intact_options(&run);
    assert_eq!(run.iter().filter(|a| *a == "--detach").count(), 1);
}

#[test]
fn a_runs_next_job_never_launches_beside_its_earlier_job() {
    let earlier = uuid::Uuid::parse_str("e0000000-0000-4000-8000-00000000000e").unwrap();
    let (_, launched, calls) = launch(job_plan(recipe_start_plan_for_authority(1)), Some(earlier));
    assert!(matches!(launched, Err(OperationError::StopUncertain)));
    assert!(
        !calls
            .iter()
            .any(|call| call.first().map(String::as_str) == Some("run")),
        "a second job container of one run was launched"
    );
}

/// Kernel OOM picks the workload before any host process (agent and helper
/// run at -900), whatever share of its memory shows as RSS.
fn assert_workload_is_the_oom_victim(run: &[String]) {
    let image = image_after_intact_options(run);
    let option = run
        .iter()
        .position(|argument| argument == "--oom-score-adj=1000")
        .expect("the workload carries a positive OOM score");
    assert!(
        option < image,
        "a Docker option precedes the image: {run:?}"
    );
}

#[test]
fn a_service_workload_is_launched_as_the_oom_victim() {
    assert_workload_is_the_oom_victim(&launched_run(recipe_start_plan_for_authority(1)));
}

#[test]
fn a_job_workload_is_launched_as_the_oom_victim() {
    assert_workload_is_the_oom_victim(&launched_run(job_plan(recipe_start_plan_for_authority(1))));
}

/// Unified memory: GPU allocations are not charged to the container's memory
/// cgroup, so the workload runs without a container memory limit. The signed
/// argv, and so the identity label, still names it: a run launched before
/// this change keeps the identity the new helper computes.
#[test]
fn a_workload_is_launched_without_a_container_memory_limit() {
    for plan in [
        recipe_start_plan_for_authority(1),
        job_plan(recipe_start_plan_for_authority(1)),
    ] {
        let (signed, run) = launched(plan);
        image_after_intact_options(&run);
        assert!(
            !run.iter().any(|a| a == "--memory" || a == "--memory-swap"),
            "{run:?}"
        );
        assert!(run.iter().any(|a| a == "--shm-size"), "{run:?}");
        assert!(signed.iter().any(|a| a == "--memory"), "{signed:?}");
        let identity = format!(
            "ai.vonkforge.runtime-request-sha256={}",
            hex_sha256(&canonical_json(&signed[4..].to_vec()).unwrap())
        );
        assert!(run.contains(&identity), "{run:?}");
    }
}
