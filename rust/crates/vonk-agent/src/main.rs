#![forbid(unsafe_code)]

use std::{
    future::Future,
    io::{self, Read},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use clap::{Parser, Subcommand};
use url::Url;
use vonk_agent::{
    agent_upgrade::AgentUpgradeExecutor,
    client::{AgentHttpClient, ClientError},
    config::{AgentConfig, DEFAULT_CONFIG_PATH},
    executor::{
        ControlExecutor, LoopError, RecipeExecutor, RecipeObservationError,
        run_once_with_claim_hook,
    },
    inventory::InventoryCollector,
    oci::OciRuntime,
    pair::{collect_evidence, complete_pairing_with, pair},
    process::SystemProcessRunner,
    readiness::{publish_current, verify_current},
    rotation::rotate_if_due,
    runtime_identity::AgentRuntimeIdentity,
    self_test,
    state::{StateStore, backoff_delay},
    telemetry::{
        SystemFileSystemProvider, TelemetryCollector, TelemetryPaths, TelemetryQueue,
        TelemetrySchedule, read_boot_id,
    },
};

#[derive(Parser)]
#[command(
    name = "vonk-agent",
    version = env!("VONK_AGENT_SEMANTIC_VERSION"),
    about = "Vonk Forge outbound agent"
)]
struct Cli {
    #[arg(long, default_value = DEFAULT_CONFIG_PATH)]
    config: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Run,
    SelfTest,
    VerifyReadiness {
        #[arg(long, default_value = "/run/vonk-forge-agent/readiness.json")]
        receipt: PathBuf,
        #[arg(long)]
        pid: u32,
        #[arg(long, default_value_t = 90)]
        max_age_seconds: u64,
    },
    Pair {
        #[arg(long)]
        enrollment: Url,
        #[arg(long)]
        ca_sha256: String,
        #[arg(long, default_value_t = false)]
        token_stdin: bool,
    },
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    match cli.command {
        Command::Run => run_agent(&AgentConfig::load(&cli.config)?).await?,
        Command::SelfTest => {
            let config = AgentConfig::load(&cli.config)?;
            let identity = self_test::run(
                &config,
                &std::env::current_exe()?,
                Path::new("/run/vonk-forge-agent"),
            )?;
            println!("{}", serde_json::to_string(&identity)?);
        }
        Command::VerifyReadiness {
            receipt,
            pid,
            max_age_seconds,
        } => {
            let config = AgentConfig::load(&cli.config)?;
            let identity = self_test::run(
                &config,
                &std::env::current_exe()?,
                Path::new("/run/vonk-forge-agent"),
            )?;
            verify_current(
                &receipt,
                &identity,
                pid,
                std::time::Duration::from_secs(max_age_seconds),
            )?;
        }
        Command::Pair {
            enrollment,
            ca_sha256,
            token_stdin,
        } => {
            if !token_stdin {
                return Err("pairing token must be supplied through --token-stdin".into());
            }
            let mut token = String::new();
            io::stdin().take(4096).read_to_string(&mut token)?;
            pair_agent(
                &AgentConfig::load(&cli.config)?,
                &enrollment,
                token.trim(),
                &ca_sha256,
            )
            .await?;
        }
    }
    Ok(())
}

async fn pair_agent(
    config: &AgentConfig,
    enrollment: &Url,
    token: &str,
    ca_sha256: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let executable = std::env::current_exe()?;
    let evidence = collect_evidence(&executable)?;
    complete_pairing_with(
        180,
        std::time::Duration::from_secs(5),
        || {
            let evidence = evidence.clone();
            async move { pair(config, enrollment, token, ca_sha256, evidence).await }
        },
        |pending| {
            println!(
                "pairing {} is {}; waiting for approval",
                pending.id, pending.state
            )
        },
    )
    .await?;
    println!("paired {}", config.node_id);
    Ok(())
}

async fn run_agent(config: &AgentConfig) -> Result<(), Box<dyn std::error::Error>> {
    let runtime_identity = self_test::run(
        config,
        &std::env::current_exe()?,
        Path::new("/run/vonk-forge-agent"),
    )?;
    rotate_if_due(config).await?;
    let client = AgentHttpClient::from_config(config)?;
    let mut state = StateStore::open(&config.data_dir.join("state.sqlite"), &config.node_id)?;
    state.recover_interrupted()?;
    let (client_updates, telemetry_client) = tokio::sync::watch::channel(client.clone());
    let control = run_control_lane(config, runtime_identity, client, state, client_updates);
    let telemetry = run_telemetry_lane(
        config.data_dir.clone(),
        telemetry_client,
        config.poll_min_seconds,
        config.poll_max_seconds,
    );
    match supervise_lanes(control, telemetry, tokio::signal::ctrl_c()).await {
        LaneExit::Control(result) => result,
        LaneExit::Shutdown(signal) => {
            signal?;
            Ok(())
        }
    }
}

async fn run_control_lane(
    config: &AgentConfig,
    runtime_identity: AgentRuntimeIdentity,
    mut client: AgentHttpClient,
    mut state: StateStore,
    client_updates: tokio::sync::watch::Sender<AgentHttpClient>,
) -> Result<(), Box<dyn std::error::Error>> {
    let runner = SystemProcessRunner;
    let capabilities = [
        "agent.runtime.rust.v1",
        "runtime.vonk.v1",
        "agent.upgrade.v1",
        "recipe.build.v1",
        "recipe.image.import.v1",
        "recipe.job.run.v1",
        "recipe.install",
        "recipe.start",
        "recipe.start.two-phase.v1",
        "recipe.run.inspect.exact.v1",
        "recipe.stop",
        "recipe.uninstall",
        "recipe.model-uninstall.v1",
    ];
    let mut failures = 0_u32;
    let mut observation_failures = 0_u32;
    let mut next_inventory = tokio::time::Instant::now();
    let mut readiness_published = false;
    loop {
        if tokio::time::Instant::now() >= next_inventory {
            if rotate_if_due(config).await? {
                client = AgentHttpClient::from_config(config)?;
                client_updates.send_replace(client.clone());
            }
            let inventory = InventoryCollector {
                runner: &runner,
                meminfo_path: Path::new("/proc/meminfo"),
                store_path: &config.data_dir,
                egress_binary_path: Path::new("/usr/lib/vonk-forge/vonk-build-egress"),
                fabric_address: config.fabric_address,
                fabric_bandwidth_mbps: config.fabric_bandwidth_mbps,
            }
            .collect()?;
            match client.report_inventory(&inventory).await {
                Ok(()) => {
                    failures = 0;
                    next_inventory =
                        tokio::time::Instant::now() + std::time::Duration::from_secs(60);
                }
                Err(error) if error.retryable() => {
                    failures = failures.saturating_add(1);
                    let entropy =
                        SystemTime::now().duration_since(UNIX_EPOCH)?.subsec_nanos() as u64;
                    tokio::time::sleep(backoff_delay(
                        failures,
                        entropy,
                        config.poll_min_seconds,
                        config.poll_max_seconds,
                    ))
                    .await;
                    continue;
                }
                Err(error) => return Err(error.into()),
            }
        }
        let executor = ControlExecutor {
            recipes: RecipeExecutor {
                client: &client,
                runtime_root: Path::new("/run/vonk-forge-agent"),
                runtime: OciRuntime {
                    runner: &runner,
                    data_root: &config.data_dir,
                    huggingface_curl_config: config.huggingface_curl_config.as_deref(),
                },
            },
            upgrades: AgentUpgradeExecutor {
                client: &client,
                incoming: Path::new("/var/lib/vonk-forge/incoming"),
            },
        };
        let exact_observation_result = executor
            .recipes
            .report_exact_recipe_run_observations()
            .await;
        let exact_observation_disposition =
            exact_observation_disposition(&exact_observation_result);
        let exact_observation_count = exact_observation_disposition.managed_run_count;
        match exact_observation_result {
            Ok(_) => {
                observation_failures = 0;
            }
            Err(_) if exact_observation_disposition.transition_not_ready => {
                // A rank-launch lifecycle is retained before the Controller can
                // mark the run running.  Do not delay the collective-readiness
                // claim on that expected, explicitly typed transition.
            }
            Err(error) => {
                // Exact observation collection is fail-closed by its explicit
                // empty v2 report.  It must not terminate the claim lane: a
                // stop/recovery operation may already be waiting for us.
                observation_failures = observation_failures.saturating_add(1);
                eprintln!("vonk-agent: exact recipe observation failed: {error}");
            }
        }
        let observations = executor.recipes.runtime.recipe_run_observations()?;
        let wait_seconds = claim_wait_seconds(
            config.poll_max_seconds,
            observations.len() + exact_observation_count,
            readiness_published,
        );
        let observation_entropy =
            SystemTime::now().duration_since(UNIX_EPOCH)?.subsec_nanos() as u64;
        let operation = async {
            match client.report_recipe_run_observations(&observations).await {
                Ok(()) => observation_failures = 0,
                Err(error) => match observation_failure_action(&error) {
                    ObservationFailureAction::BackoffThenClaim => {
                        observation_failures = observation_failures.saturating_add(1);
                        tokio::time::sleep(backoff_delay(
                            observation_failures,
                            observation_entropy,
                            config.poll_min_seconds,
                            config.poll_max_seconds,
                        ))
                        .await;
                    }
                    ObservationFailureAction::Stop => return Err(LoopError::Client(error)),
                },
            }
            run_once_with_claim_hook(
                &client,
                &mut state,
                &executor,
                &capabilities,
                wait_seconds,
                Some(&runtime_identity),
                || {
                    publish_current(
                        Path::new("/run/vonk-forge-agent/readiness.json"),
                        &runtime_identity,
                    )
                    .map_err(|error| LoopError::Readiness(error.to_string()))
                },
            )
            .await
        };
        match operation.await {
            Ok(()) => {
                failures = 0;
                readiness_published = true;
            }
            Err(error) if matches!(&error, vonk_agent::executor::LoopError::Client(inner) if inner.retryable()) =>
            {
                failures = failures.saturating_add(1);
                let entropy = SystemTime::now().duration_since(UNIX_EPOCH)?.subsec_nanos() as u64;
                tokio::time::sleep(backoff_delay(
                    failures,
                    entropy,
                    config.poll_min_seconds,
                    config.poll_max_seconds,
                ))
                .await;
            }
            Err(error) => return Err(error.into()),
        }
    }
}

async fn run_telemetry_lane(
    data_dir: PathBuf,
    clients: tokio::sync::watch::Receiver<AgentHttpClient>,
    poll_min_seconds: u64,
    poll_max_seconds: u64,
) {
    let boot_id_path = PathBuf::from("/proc/sys/kernel/random/boot_id");
    let boot_id = loop {
        match read_boot_id(&boot_id_path) {
            Ok(boot_id) => break boot_id,
            Err(error) => {
                eprintln!("telemetry boot identity unavailable: {error}");
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            }
        }
    };
    let collector = TelemetryCollector::new(
        SystemProcessRunner,
        SystemFileSystemProvider,
        TelemetryPaths {
            stat: PathBuf::from("/proc/stat"),
            loadavg: PathBuf::from("/proc/loadavg"),
            meminfo: PathBuf::from("/proc/meminfo"),
            net_dev: PathBuf::from("/proc/net/dev"),
            store: data_dir,
        },
        boot_id,
    );
    let mut collector = match collector {
        Ok(collector) => collector,
        Err(error) => {
            eprintln!("telemetry durable state unavailable: {error}");
            return;
        }
    };
    let mut previous = None;
    let mut queue = TelemetryQueue::new();
    let mut schedule = TelemetrySchedule::new(tokio::time::Instant::now());
    let mut send_failures = 0_u32;

    loop {
        tokio::time::sleep_until(schedule.next_collection()).await;
        let collection_started = tokio::time::Instant::now();
        let prior = previous.take();
        let collection = tokio::task::spawn_blocking(move || {
            let result = collector.sample(prior.as_ref());
            (collector, prior, result)
        })
        .await;
        let Ok((returned_collector, prior, result)) = collection else {
            eprintln!("telemetry collector task stopped unexpectedly");
            return;
        };
        collector = returned_collector;
        match result {
            Ok(sample) => {
                previous = Some(sample.clone());
                queue.push(sample);
            }
            Err(error) => {
                previous = prior;
                eprintln!("telemetry sample unavailable: {error}");
            }
        }
        let now = tokio::time::Instant::now();
        schedule.collected(collection_started, now);

        if !schedule.send_due(now, !queue.is_empty()) {
            continue;
        }
        let batch = queue.batch();
        let client = clients.borrow().clone();
        match client.report_telemetry(&batch).await {
            Ok(()) => {
                queue
                    .acknowledge_prefix(batch.len())
                    .expect("reported telemetry prefix exists");
                send_failures = 0;
                schedule.send_succeeded(tokio::time::Instant::now());
            }
            Err(error) => {
                send_failures = send_failures.saturating_add(1);
                let entropy = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_or(0, |duration| duration.subsec_nanos() as u64);
                let retry_after = telemetry_retry_after(
                    &error,
                    send_failures,
                    entropy,
                    poll_min_seconds,
                    poll_max_seconds,
                );
                schedule.send_failed(tokio::time::Instant::now(), retry_after);
                eprintln!("telemetry report deferred: {error}");
            }
        }
    }
}

fn telemetry_retry_after(
    error: &ClientError,
    failures: u32,
    entropy: u64,
    poll_min_seconds: u64,
    poll_max_seconds: u64,
) -> std::time::Duration {
    if error.retryable() {
        backoff_delay(failures, entropy, poll_min_seconds, poll_max_seconds)
    } else {
        std::time::Duration::from_secs(60)
    }
}

#[derive(Debug, PartialEq, Eq)]
enum LaneExit<C, S> {
    Control(C),
    Shutdown(S),
}

async fn supervise_lanes<C, T, S>(
    control: C,
    telemetry: T,
    shutdown: S,
) -> LaneExit<C::Output, S::Output>
where
    C: Future,
    T: Future<Output = ()>,
    S: Future,
{
    tokio::pin!(control);
    tokio::pin!(telemetry);
    tokio::pin!(shutdown);
    let mut telemetry_running = true;
    loop {
        tokio::select! {
            result = &mut control => return LaneExit::Control(result),
            signal = &mut shutdown => return LaneExit::Shutdown(signal),
            () = &mut telemetry, if telemetry_running => telemetry_running = false,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ObservationFailureAction {
    BackoffThenClaim,
    Stop,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ExactObservationDisposition {
    managed_run_count: usize,
    transition_not_ready: bool,
}

fn exact_observation_disposition(
    result: &Result<usize, RecipeObservationError>,
) -> ExactObservationDisposition {
    ExactObservationDisposition {
        managed_run_count: result.as_ref().copied().unwrap_or(0),
        transition_not_ready: result.as_ref().is_err_and(|error| error.not_ready()),
    }
}

fn observation_failure_action(error: &ClientError) -> ObservationFailureAction {
    if error.retryable() {
        ObservationFailureAction::BackoffThenClaim
    } else {
        ObservationFailureAction::Stop
    }
}

fn claim_wait_seconds(
    configured_maximum: u64,
    managed_run_count: usize,
    readiness_published: bool,
) -> u64 {
    if !readiness_published {
        return 0;
    }
    let existing_wait = configured_maximum.min(60);
    if managed_run_count == 0 {
        existing_wait
    } else {
        existing_wait.min(10)
    }
}

#[cfg(test)]
mod tests {
    use super::{
        LaneExit, ObservationFailureAction, claim_wait_seconds, exact_observation_disposition,
        observation_failure_action, supervise_lanes, telemetry_retry_after,
    };
    use std::future;
    use vonk_agent::client::ClientError;
    use vonk_agent::{executor::RecipeObservationError, host_runtime::HostRuntimeError};

    #[test]
    fn exact_observation_failures_never_stop_the_next_claim() {
        let transition = Err(RecipeObservationError::Inspection(
            HostRuntimeError::Controller(ClientError::ObservationNotReady),
        ));
        let transition = exact_observation_disposition(&transition);
        assert_eq!(transition.managed_run_count, 0);
        assert!(transition.transition_not_ready);

        let denied = Err(RecipeObservationError::Inspection(
            HostRuntimeError::Controller(ClientError::Protocol),
        ));
        let denied = exact_observation_disposition(&denied);
        assert_eq!(denied.managed_run_count, 0);
        assert!(!denied.transition_not_ready);

        for cycle in [Ok(2), Ok(2)] {
            let complete = exact_observation_disposition(&cycle);
            assert_eq!(complete.managed_run_count, 2);
            assert!(!complete.transition_not_ready);
        }
    }

    #[test]
    fn managed_runs_cap_claim_long_poll_at_ten_seconds() {
        assert_eq!(claim_wait_seconds(60, 1, true), 10);
        assert_eq!(claim_wait_seconds(7, 1, true), 7);
    }

    #[test]
    fn no_managed_runs_retain_existing_claim_long_poll_behavior() {
        assert_eq!(claim_wait_seconds(300, 0, true), 60);
        assert_eq!(claim_wait_seconds(7, 0, true), 7);
    }

    #[test]
    fn first_claim_returns_immediately_to_publish_controller_readiness() {
        assert_eq!(claim_wait_seconds(60, 0, false), 0);
        assert_eq!(claim_wait_seconds(60, 1, false), 0);
    }

    #[test]
    fn retryable_observation_failure_backs_off_then_allows_claim() {
        assert_eq!(
            observation_failure_action(&ClientError::Retryable),
            ObservationFailureAction::BackoffThenClaim
        );
    }

    #[test]
    fn non_retryable_observation_failure_stops_the_loop() {
        assert_eq!(
            observation_failure_action(&ClientError::Protocol),
            ObservationFailureAction::Stop
        );
    }

    #[tokio::test]
    async fn retryable_telemetry_failure_schedules_retry_without_delaying_claim_lane() {
        let retry_after = telemetry_retry_after(&ClientError::Retryable, 1, 0, 5, 60);
        assert!(retry_after >= std::time::Duration::from_secs(5));
        let telemetry_lane = async move {
            tokio::time::sleep(retry_after).await;
        };
        let claim_lane = future::ready("claim attempted");

        let outcome = tokio::time::timeout(
            std::time::Duration::from_millis(100),
            supervise_lanes(claim_lane, telemetry_lane, future::pending::<()>()),
        )
        .await
        .expect("claim lane was gated by telemetry retry state");

        assert_eq!(outcome, LaneExit::Control("claim attempted"));
    }
}
