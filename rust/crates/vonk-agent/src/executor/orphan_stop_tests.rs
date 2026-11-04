#![cfg(test)]
//! A running container nothing owns is stopped under the Controller's grant
//! and retired, end to end through the real observation sweep, the agent's
//! Controller client and helper client, and faithful fakes of both.
use super::test_support::*;
use super::*;
use std::os::unix::fs::PermissionsExt;

struct World {
    data: tempfile::TempDir,
    runtime: tempfile::TempDir,
    host: FakeHost,
    controller: FakeController,
}

fn world() -> World {
    let data = tempdir().unwrap();
    let runtime = tempdir().unwrap();
    let host = FakeHost::new(
        runtime.path().join("helper.sock"),
        runtime.path().join("runtime-requests"),
    );
    World {
        data,
        runtime,
        host,
        controller: FakeController::new(),
    }
}

impl World {
    fn executor<'a>(&'a self, runner: &'a NoProcess) -> RecipeExecutor<'a, NoProcess> {
        RecipeExecutor {
            client: &self.controller.client,
            helper_socket: &self.host.socket,
            runtime: OciRuntime {
                runner,
                data_root: self.data.path(),
            },
            runtime_root: self.runtime.path(),
        }
    }

    fn lifecycle(&self, run: Uuid) -> std::path::PathBuf {
        self.data
            .path()
            .join("run-metadata")
            .join(run.to_string())
            .join("lifecycle.json")
    }

    /// One run the agent holds a readable lifecycle for.
    fn prepared_run(&self, generation: u64) -> (Uuid, Uuid) {
        let mut plan: crate::workloads::CompiledExecutionPlan = serde_json::from_str(include_str!(
            "../../../../../control/tests/fixtures/compiled_workload_v2.json"
        ))
        .unwrap();
        let installation = Uuid::new_v4();
        let installed = self
            .data
            .path()
            .join("installations")
            .join(installation.to_string());
        fs::create_dir_all(&installed).unwrap();
        fs::write(
            installed.join("spec.json"),
            serde_json::to_vec(&plan).unwrap(),
        )
        .unwrap();
        fs::write(
            installed.join("recipe-content.sha256"),
            &plan.identity.recipe_revision_sha256,
        )
        .unwrap();
        plan.runtime.placement.endpoint_address = Some("192.168.1.211".parse().unwrap());
        plan.security.network_mode = "bridge".parse().unwrap();
        plan.validate().unwrap();
        let run = Uuid::new_v4();
        OciRuntime {
            runner: &NoProcess,
            data_root: self.data.path(),
        }
        .prepare_start_with_inspection_identity(
            &plan,
            &installation.to_string(),
            &run.to_string(),
            &plan.runtime.placement,
            &crate::oci::RecipeRunStartIdentity {
                run_generation: generation,
            },
        )
        .unwrap();
        (run, installation)
    }

    /// One run whose managed metadata can no longer be read.
    fn unreadable_run(&self) -> Uuid {
        let run = Uuid::new_v4();
        fs::create_dir_all(self.data.path().join("runs").join(run.to_string())).unwrap();
        let metadata = self.data.path().join("run-metadata").join(run.to_string());
        fs::create_dir_all(&metadata).unwrap();
        fs::set_permissions(&metadata, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(metadata.join("lifecycle.json"), br#"{"installation_id":1}"#).unwrap();
        run
    }
}

/// One complete observation cycle; a failed report is the next cycle's work.
async fn cycle(executor: &RecipeExecutor<'_, NoProcess>) {
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut checkpoint = None;
    loop {
        assert!(
            Instant::now() < deadline,
            "observation cycle budget expired"
        );
        let Ok(page) = executor
            .report_recipe_run_observation_page(checkpoint.as_ref())
            .await
        else {
            return;
        };
        checkpoint = page.checkpoint;
        if checkpoint.is_none() {
            return;
        }
    }
}

fn stops(host: &FakeHost) -> Vec<HelperCall> {
    host.calls()
        .into_iter()
        .filter(|call| matches!(call, HelperCall::Stop { .. }))
        .collect()
}

#[tokio::test]
async fn an_unowned_container_is_stopped_and_retired_and_a_known_one_is_left_alone() {
    // Catches the container keeping GPU memory forever because the agent only
    // retires a run whose process is already gone, and an agent that stops
    // whatever it sees instead of what the Controller does not want.
    let world = world();
    let (unowned, unowned_installation) = world.prepared_run(2);
    let (known, known_installation) = world.prepared_run(2);
    world
        .host
        .run(unowned, Some(labels_of(unowned, unowned_installation, 2)));
    world
        .host
        .run(known, Some(labels_of(known, known_installation, 2)));
    world.controller.say(unowned, FakeDisposition::Unowned);
    world.controller.say(known, FakeDisposition::Wanted(2));
    let runner = NoProcess;
    let executor = world.executor(&runner);

    for _ in 0..2 {
        cycle(&executor).await;
    }

    assert!(
        !world.host.is_running(unowned),
        "the orphan was not stopped"
    );
    assert!(world.host.is_running(known));
    assert_eq!(
        stops(&world.host),
        vec![HelperCall::Stop {
            name: format!("vonk-{unowned}"),
            removed: true
        }],
        "the known run must get no stop request"
    );
    assert_eq!(world.controller.grants(), vec![unowned.to_string()]);
    assert!(world.controller.refusals().is_empty());
    assert!(
        !world.lifecycle(unowned).exists(),
        "the claim was not retired"
    );
    assert!(world.lifecycle(known).exists());
    // Later pages carry the known run only.
    let before = world.controller.reports_seen();
    cycle(&executor).await;
    let later = world.controller.reported_since(before);
    assert!(later.contains(&known.to_string()));
    assert!(!later.contains(&unowned.to_string()));
    assert_eq!(stops(&world.host).len(), 1);
}

#[tokio::test]
async fn a_newer_generation_container_is_refused_by_the_helper_and_left_running() {
    // The helper, not the agent, decides what is the grant's own: a container
    // of a newer generation that took the name after the labels were read is
    // never removed, and its claim is not retired.
    let world = world();
    let (run, installation) = world.prepared_run(2);
    world.host.run(run, Some(labels_of(run, installation, 2)));
    world.controller.say(run, FakeDisposition::Unowned);
    world
        .host
        .replace_before_next_stop(run, labels_of(run, installation, 3));
    let runner = NoProcess;
    let executor = world.executor(&runner);

    cycle(&executor).await;
    cycle(&executor).await;

    assert!(world.host.is_running(run));
    assert_eq!(
        stops(&world.host),
        vec![HelperCall::Stop {
            name: format!("vonk-{run}"),
            removed: false
        }]
    );
    assert!(world.lifecycle(run).exists());
}

#[tokio::test]
async fn an_unreachable_or_refusing_controller_stops_nothing_and_the_next_cycle_heals() {
    // Catches a stop without the Controller's answer, a claim retired for a
    // container still running, and a refusal that is not retried.
    let world = world();
    let (run, installation) = world.prepared_run(2);
    world.host.run(run, Some(labels_of(run, installation, 2)));
    world.controller.say(run, FakeDisposition::Unowned);
    let runner = NoProcess;
    let executor = world.executor(&runner);

    world.controller.set_down(true);
    cycle(&executor).await;
    assert!(world.host.is_running(run) && world.lifecycle(run).exists());

    world.controller.set_down(false);
    world.controller.refuse_grants(true);
    cycle(&executor).await;
    assert_eq!(world.controller.refusals(), vec![run.to_string()]);
    assert!(stops(&world.host).is_empty());
    assert!(world.host.is_running(run) && world.lifecycle(run).exists());

    world.controller.refuse_grants(false);
    cycle(&executor).await;
    assert!(!world.host.is_running(run));
    assert!(!world.lifecycle(run).exists());
}

#[tokio::test]
async fn a_stop_whose_reply_is_lost_is_retired_next_cycle_without_a_second_stop() {
    // A crash between the stop and the retirement leaves a claim for a process
    // that is gone: the ordinary retirement applies, nothing is stopped twice.
    let world = world();
    let (run, installation) = world.prepared_run(2);
    world.host.run(run, Some(labels_of(run, installation, 2)));
    world.controller.say(run, FakeDisposition::Unowned);
    world.host.lose_next_stop_reply();
    let runner = NoProcess;
    let executor = world.executor(&runner);

    cycle(&executor).await;
    assert!(!world.host.is_running(run), "the stop landed");
    assert!(world.lifecycle(run).exists(), "but was never confirmed");

    cycle(&executor).await;
    assert!(!world.lifecycle(run).exists());
    assert_eq!(stops(&world.host).len(), 1);
    assert_eq!(world.controller.grants().len(), 1);
}

#[tokio::test]
async fn an_unreadable_lifecycle_does_not_keep_an_unowned_container_running() {
    // The same outcome for a run whose managed metadata cannot be read: the
    // container's own labels are its identity.
    let world = world();
    let run = world.unreadable_run();
    world.host.run(run, Some(labels_of(run, Uuid::new_v4(), 4)));
    world.controller.say(run, FakeDisposition::Unowned);
    let runner = NoProcess;
    let executor = world.executor(&runner);

    cycle(&executor).await;

    assert!(!world.host.is_running(run));
    assert!(!world.lifecycle(run).exists());
    assert_eq!(stops(&world.host).len(), 1);
}

#[tokio::test]
async fn a_container_without_a_readable_identity_is_not_stopped() {
    // Nothing but the container's own labels names what to stop; without them
    // the agent asks for nothing and keeps the claim.
    let world = world();
    let run = world.unreadable_run();
    world.host.run(run, None);
    world.controller.say(run, FakeDisposition::Unowned);
    let runner = NoProcess;
    let executor = world.executor(&runner);

    cycle(&executor).await;

    assert!(world.host.is_running(run) && world.lifecycle(run).exists());
    assert!(world.controller.grants().is_empty());
    assert!(stops(&world.host).is_empty());
}

#[tokio::test]
async fn a_slow_disposition_never_costs_a_running_run_its_report() {
    // The Controller answers dispositions after longer than the page's
    // inspection budget. A healthy running run is reported all the same: its
    // report never waits for a Controller call inside the budget.
    let world = world();
    let (known, installation) = world.prepared_run(2);
    world
        .host
        .run(known, Some(labels_of(known, installation, 2)));
    world.controller.say(known, FakeDisposition::Wanted(2));
    world.controller.delay_dispositions(Duration::from_secs(11));
    let runner = NoProcess;
    let executor = world.executor(&runner);

    cycle(&executor).await;

    assert!(
        world
            .controller
            .reported_runs()
            .contains(&known.to_string()),
        "the running run's report was dropped by the slow Controller"
    );
    assert!(world.host.is_running(known));
    assert!(stops(&world.host).is_empty());
}

#[tokio::test]
async fn a_stop_slower_than_the_page_budget_completes_in_its_own_cycle() {
    // The stop runs after the page's reports, outside the 10 s inspection
    // budget, so it is not cut off halfway: it lands once and is retired.
    let world = world();
    let (run, installation) = world.prepared_run(2);
    world.host.run(run, Some(labels_of(run, installation, 2)));
    world.controller.say(run, FakeDisposition::Unowned);
    world.host.slow_next_stop(Duration::from_secs(11));
    let runner = NoProcess;
    let executor = world.executor(&runner);

    cycle(&executor).await;

    assert!(!world.host.is_running(run));
    assert!(!world.lifecycle(run).exists());
    assert_eq!(stops(&world.host).len(), 1);
    assert_eq!(world.controller.grants().len(), 1);
    cycle(&executor).await;
    assert_eq!(stops(&world.host).len(), 1, "no double effect");
}

#[tokio::test]
async fn a_stop_that_never_finishes_cannot_hold_up_the_pass_and_is_not_repeated() {
    // Catches a stop phase with no bound: one container whose stop never
    // completes would add its whole timeout to every pass, and with it to
    // every claim, every cycle. The pass returns within the stop window; the
    // stop that outlasts it lands anyway and is retired next cycle without a
    // second effect.
    let world = world();
    let (run, installation) = world.prepared_run(2);
    world.host.run(run, Some(labels_of(run, installation, 2)));
    world.controller.say(run, FakeDisposition::Unowned);
    world.host.slow_next_stop(Duration::from_secs(22));
    let runner = NoProcess;
    let executor = world.executor(&runner);

    let started = Instant::now();
    cycle(&executor).await;
    assert!(
        started.elapsed() < Duration::from_secs(19),
        "the pass waited for the stop: {:?}",
        started.elapsed()
    );
    assert!(
        world.lifecycle(run).exists(),
        "not retired before the stop landed"
    );

    // While the stop is still running a second cycle sees the container
    // running and asks again; the helper turns that stop away at once.
    cycle(&executor).await;
    let deadline = Instant::now() + Duration::from_secs(30);
    while world.host.is_running(run) {
        assert!(Instant::now() < deadline, "the stop never landed");
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    // Once it has landed the next cycle only retires.
    cycle(&executor).await;
    assert!(!world.lifecycle(run).exists());
    let landed = world
        .host
        .calls()
        .into_iter()
        .filter(|call| matches!(call, HelperCall::Stop { removed: true, .. }))
        .count();
    assert_eq!(landed, 1, "exactly one effect");
    // The second ask was made, and refused while the first still ran.
    assert!(
        world
            .host
            .calls()
            .iter()
            .any(|call| matches!(call, HelperCall::Busy { .. }))
    );
}
