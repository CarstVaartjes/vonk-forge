#![cfg(test)]
//! What the helper hands to `docker run` for an accepted workload.
use super::*;
use crate::operations::commands::captures_stdout;
use crate::operations::reobserve::REOBSERVE_FORMAT;
use crate::operations::test_support::*;

#[derive(Clone)]
struct LaunchRunner {
    calls: Arc<Mutex<Vec<Vec<String>>>>,
    config: String,
    /// The host's job container listing: an earlier job of the same run.
    earlier_job: String,
    /// What `docker run` answers.
    run: RunAnswer,
    /// The launched container's identity listing (`REOBSERVE_FORMAT`), once
    /// `docker run` has created it.
    launched: Option<String>,
    created: Arc<Mutex<bool>>,
}

/// What `docker run` gives back to the helper.
#[derive(Clone, Copy)]
enum RunAnswer {
    /// The container id, as Docker prints it.
    Id,
    /// Success without a readable id.
    NoId,
    /// The runner's deadline ended the command.
    Deadline,
}

impl CommandRunner for LaunchRunner {
    fn run(&self, executable: &Path, args: &[String]) -> Result<CommandOutput, String> {
        self.calls.lock().unwrap().push(args.to_vec());
        let first = args.first().map(String::as_str);
        if first == Some("run") {
            *self.created.lock().unwrap() = self.launched.is_some();
            if let RunAnswer::Deadline = self.run {
                return Err("compiled command exceeded its deadline".to_owned());
            }
        }
        if args.iter().any(|arg| arg == REOBSERVE_FORMAT) && *self.created.lock().unwrap() {
            return Ok(docker_output(true, self.launched.as_deref().unwrap(), 0));
        }
        // No container exists yet; the image is the accepted one.
        let missing =
            first == Some("container") && args.get(1).map(String::as_str) == Some("inspect");
        let stdout = match first {
            Some("image") => {
                format!("{}\tlinux\tarm64\tv1\t10001:10001\n", self.config).into_bytes()
            }
            Some("run") if matches!(self.run, RunAnswer::Id) => "f".repeat(64).into_bytes(),
            // The run's managed containers by id; each id's labels follow.
            Some("container")
                if args.iter().any(|arg| arg == "--last") && !self.earlier_job.is_empty() =>
            {
                format!("{}\n", "e".repeat(64)).into_bytes()
            }
            Some("container") if args.iter().any(|arg| arg.starts_with("{{.Names}}")) => {
                self.earlier_job.clone().into_bytes()
            }
            _ => Vec::new(),
        };
        Ok(CommandOutput {
            success: !missing,
            // The production runner discards what it does not capture.
            stdout: if captures_stdout(executable) {
                stdout
            } else {
                Vec::new()
            },
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
    plan: RecipeStartPayload,
    earlier_job: Option<uuid::Uuid>,
) -> (
    Vec<String>,
    Result<RuntimeStartLaunch, OperationError>,
    Vec<Vec<String>>,
) {
    launch_answered(plan, earlier_job, RunAnswer::Id, None)
}

/// The agent-side tree an accepted `plan` launches from, with its image
/// archive digest made consistent with its manifest.
fn start_fixture(plan: &mut RecipeStartPayload) -> (tempfile::TempDir, ManagedRoots) {
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
    (temp, roots)
}

/// Launches `plan` with `docker run` giving `answer`; `launched` says whether
/// the container it created, if it created one, is live.
fn launch_answered(
    mut plan: RecipeStartPayload,
    earlier_job: Option<uuid::Uuid>,
    answer: RunAnswer,
    launched: Option<&str>,
) -> (
    Vec<String>,
    Result<RuntimeStartLaunch, OperationError>,
    Vec<Vec<String>>,
) {
    let (_temp, roots) = start_fixture(&mut plan);
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
        run: answer,
        launched: launched.map(|live| {
            format!(
                "{live}\tfalse\t{}\ttrue\t{}\t{}\t{}\t{}\t{}\n",
                "f".repeat(64),
                plan.run_id,
                plan.run_id,
                plan.installation_id,
                plan.run_generation,
                plan.plan_digest
            )
        }),
        created: Arc::default(),
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

/// A detached job launch is confirmed by the container id `docker run`
/// prints; the helper must read it (production: every job reported
/// `compiled command failed` while its container ran on, unowned).
#[test]
fn a_job_launch_is_confirmed_by_the_container_id_it_prints() {
    let (_, launched, _) = launch(job_plan(recipe_start_plan_for_authority(1)), None);
    match launched {
        Ok(RuntimeStartLaunch::Job { .. }) => {}
        Ok(_) => panic!("a job launched as another kind"),
        Err(error) => panic!("job launch refused: {error:?}"),
    }
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

/// A launch whose answer was not read -- no id printed, or the runner's
/// deadline -- is read again by identity: the container it created is adopted
/// and waited for, never reported as a failed or timed-out job.
#[test]
fn an_unread_launch_adopts_the_container_it_created() {
    for answer in [RunAnswer::NoId, RunAnswer::Deadline] {
        let (_, launched, _) = launch_answered(
            job_plan(recipe_start_plan_for_authority(1)),
            None,
            answer,
            Some("true"),
        );
        assert!(matches!(launched, Ok(RuntimeStartLaunch::Job { .. })));
    }
    let (_, launched, _) = launch_answered(
        recipe_start_plan_for_authority(1),
        None,
        RunAnswer::Deadline,
        Some("true"),
    );
    assert!(matches!(launched, Ok(RuntimeStartLaunch::Service)));
}

/// A launch proven to have created nothing reports its own cause.
#[test]
fn an_unread_launch_with_no_container_reports_its_own_cause() {
    let (_, launched, _) = launch_answered(
        job_plan(recipe_start_plan_for_authority(1)),
        None,
        RunAnswer::NoId,
        None,
    );
    assert!(matches!(
        launched,
        Err(OperationError::CommandFailed {
            cause: CommandFailure::UnexpectedAnswer
        })
    ));
    let (_, launched, _) = launch_answered(
        job_plan(recipe_start_plan_for_authority(1)),
        None,
        RunAnswer::Deadline,
        None,
    );
    assert!(matches!(
        launched,
        Err(OperationError::CommandFailed {
            cause: CommandFailure::Runner(_)
        })
    ));
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

/// A Docker host whose one container answers every inspection with its line.
#[derive(Clone)]
struct InspectedRunner(String);

impl CommandRunner for InspectedRunner {
    fn run(&self, _executable: &Path, args: &[String]) -> Result<CommandOutput, String> {
        let inspect = args.first().map(String::as_str) == Some("container");
        Ok(docker_output(true, if inspect { &self.0 } else { "" }, 0))
    }
}

/// A Start's re-observation names its generation: the running container under
/// the name is this Start's only when its label carries that generation, as
/// the Start's own adopt reads it. Another generation, or no label, is not
/// this Start's. An inspection that names none reads it as before.
#[test]
fn an_inspection_naming_a_generation_refuses_another_generations_container() {
    use std::os::unix::fs::PermissionsExt;
    let inspect = |label: &str, named: Option<u64>| {
        let mut plan = recipe_start_plan_for_authority(2);
        let (_temp, roots) = start_fixture(&mut plan);
        let container = format!(
            "{}\ttrue\t{}\ttrue\t{}\t{label}\n",
            "f".repeat(64),
            "d".repeat(64),
            plan.run_id
        );
        let executor =
            OperationExecutor::new(roots.clone(), &[0; 32], InspectedRunner(container), None)
                .unwrap();
        let request = HostRuntimeRequest {
            action: HostRuntimeAction::RunInspect,
            fence: uuid::Uuid::new_v4(),
            arguments: executor
                .projected_runtime_arguments(
                    &plan.compiled_execution_plan,
                    plan.installation_id,
                    plan.run_id,
                )
                .unwrap(),
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: named,
            start_plan: None,
            stop_plan: None,
        };
        let body = canonical_json(&request).unwrap();
        let digest = hex_sha256(&body);
        fs::create_dir_all(&roots.runtime_requests).unwrap();
        let path = roots.runtime_requests.join(format!("{digest}.json"));
        fs::write(&path, &body).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        executor
            .inspect_recipe_run_with_logs(&digest, false)
            .map(|inspection| inspection.running)
    };
    assert!(matches!(inspect("2", Some(2)), Ok(true)));
    assert!(matches!(
        inspect("1", Some(2)),
        Err(OperationError::InvalidArtifact)
    ));
    assert!(matches!(
        inspect("", Some(2)),
        Err(OperationError::InvalidArtifact)
    ));
    assert!(matches!(inspect("1", None), Ok(true)));
}
