#![forbid(unsafe_code)]

use std::{
    future::Future,
    io::{self, Read},
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use clap::{Parser, Subcommand};
use url::Url;
use vonk_agent::{
    agent_upgrade::AgentUpgradeExecutor,
    client::AgentHttpClient,
    config::{AgentConfig, DEFAULT_CONFIG_PATH, POLL_MAX_SECONDS, POLL_MIN_SECONDS},
    executor::{
        ControlExecutor, LoopError, RecipeExecutor, RecipeObservationError,
        run_once_with_claim_hook,
    },
    inventory::{
        Inventory, InventoryCollector, InventoryError, STATE_DATABASE_DISK_RESERVE_BYTES,
        disk_reserve_degraded, prepare_state_database_reserve,
    },
    oci::OciRuntime,
    pair::{collect_evidence, pair},
    process::SystemProcessRunner,
    readiness::{publish_current, verify_current},
    rotation::{RotationError, active_identity_is_valid, rotate_if_due},
    runtime_identity::AgentRuntimeIdentity,
    self_test,
    state::{StateStore, backoff_delay},
    systemd_notify,
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
    pair(config, enrollment, token, ca_sha256, evidence).await?;
    println!("paired {}", config.node_id);
    Ok(())
}

fn report_ready_after_self_test<T, E>(
    self_test: impl FnOnce() -> Result<T, E>,
    report_ready: impl FnOnce(),
) -> Result<T, E> {
    let identity = self_test()?;
    report_ready();
    Ok(identity)
}

async fn run_agent(config: &AgentConfig) -> Result<(), Box<dyn std::error::Error>> {
    // Self-test is the package rollback health boundary. A failed binary must
    // never report ready; after it passes, report readiness before retryable
    // identity, Controller, and host work so upgrades do not wait on the fleet.
    let runtime_identity = report_ready_after_self_test(
        || {
            self_test::run(
                config,
                &std::env::current_exe()?,
                Path::new("/run/vonk-forge-agent"),
            )
        },
        || systemd_notify::notify("READY=1\nSTATUS=Agent starting"),
    )?;
    let client = AgentHttpClient::from_config(config)?;
    ensure_startup_identity(
        || active_identity_is_valid(config),
        || rotate_if_due(config, &client),
        |failures| jittered_backoff(failures, POLL_MIN_SECONDS, POLL_MAX_SECONDS),
    )
    .await?;
    if !matches!(prepare_state_database_reserve(&config.data_dir), Ok(true)) {
        eprintln!(
            "vonk-agent: degraded: state database disk reserve is unavailable; attempting state recovery with measured free space"
        );
        systemd_notify::notify(
            "STATUS=Degraded: state database disk reserve is unavailable; attempting recovery",
        );
    }
    let mut failures = 0_u32;
    let state = loop {
        let opened = StateStore::open(&config.data_dir.join("state.sqlite"), &config.node_id)
            .and_then(|mut state| state.recover_interrupted().map(|()| state));
        match opened {
            Ok(state) => break state,
            Err(error) => {
                failures = failures.saturating_add(1);
                let delay = jittered_backoff(failures, POLL_MIN_SECONDS, POLL_MAX_SECONDS);
                eprintln!(
                    "vonk-agent: degraded: local state database unavailable ({error}); retrying in {} seconds",
                    delay.as_secs()
                );
                systemd_notify::progress("Degraded: local state database unavailable; retrying");
                tokio::time::sleep(delay).await;
            }
        }
    };
    systemd_notify::notify(
        "STATUS=Agent initialized; waiting for Controller and host prerequisites",
    );
    let control = run_control_lane(config, runtime_identity, client.clone(), state);
    let rotation = tokio::spawn(run_rotation_lane(config.clone(), client.clone()));
    match supervise_lanes_with_rotation(
        control,
        std::future::pending::<()>(),
        rotation,
        tokio::signal::ctrl_c(),
    )
    .await
    {
        LaneExitWithRotation::Control(result) => result,
        LaneExitWithRotation::Rotation(Ok(Ok(()))) => Ok(()),
        LaneExitWithRotation::Rotation(Ok(Err(error))) => Err(error.into()),
        LaneExitWithRotation::Rotation(Err(error)) => Err(error.into()),
        LaneExitWithRotation::Shutdown(signal) => {
            signal?;
            Ok(())
        }
    }
}

async fn run_control_lane(
    config: &AgentConfig,
    mut runtime_identity: AgentRuntimeIdentity,
    client: AgentHttpClient,
    mut state: StateStore,
) -> Result<(), Box<dyn std::error::Error>> {
    let runner = SystemProcessRunner;
    let mut failures = 0_u32;
    let mut inventory_reported_at = None;
    let mut readiness_published = false;
    loop {
        if !active_identity_is_valid(config)? {
            // The rotation lane is renewing it; never present an expired
            // certificate to the Controller for work in the meantime.
            failures = failures.saturating_add(1);
            systemd_notify::progress("Degraded: active certificate expired; control loop idle");
            tokio::time::sleep(jittered_backoff(
                failures,
                POLL_MIN_SECONDS,
                POLL_MAX_SECONDS,
            ))
            .await;
            continue;
        }
        if inventory_refresh_due(inventory_reported_at, Instant::now()) {
            let collector = InventoryCollector {
                runner: &runner,
                meminfo_path: Path::new("/proc/meminfo"),
                store_path: &config.data_dir,
                egress_binary_path: Path::new("/usr/lib/vonk-forge/vonk-build-egress"),
                fabric_address: config.fabric_address,
                fabric_bandwidth_mbps: config.fabric_bandwidth_mbps,
            };
            let mut inventory = collect_inventory_until_ready(
                || collector.collect(),
                &mut failures,
                POLL_MIN_SECONDS,
                POLL_MAX_SECONDS,
            )
            .await;
            let controller_url = config.controller_url.clone();
            // Resolving the NAS host may block on DNS; keep it off the reactor.
            let evidence = tokio::task::spawn_blocking(move || {
                vonk_agent::network::collect(
                    Path::new("/sys/class/net"),
                    Path::new("/proc/net/route"),
                    Path::new("/proc/net/ipv6_route"),
                    vonk_agent::network::nas_address(&controller_url),
                )
            })
            .await
            .unwrap_or_default();
            inventory.network_interfaces = evidence.interfaces;
            inventory.nas_route_interface = evidence.nas_route_interface;
            if disk_reserve_degraded(inventory.disk_available_bytes)
                || !inventory.state_database_reserve_held
            {
                eprintln!(
                    "vonk-agent: degraded: {} bytes remain on the state database filesystem; the {} byte reserve is not held",
                    inventory.disk_available_bytes, STATE_DATABASE_DISK_RESERVE_BYTES
                );
                systemd_notify::notify(&format!(
                    "STATUS=Degraded: {} bytes free on state database filesystem; 64 MiB reserve is not held",
                    inventory.disk_available_bytes,
                ));
            }
            match client.report_inventory(&inventory).await {
                Ok(()) => {
                    failures = 0;
                    inventory_reported_at = Some(Instant::now());
                    systemd_notify::watchdog();
                }
                Err(error) if error.fatal() => return Err(error.into()),
                Err(error) => {
                    failures = failures.saturating_add(1);
                    let delay = jittered_backoff(failures, POLL_MIN_SECONDS, POLL_MAX_SECONDS);
                    eprintln!(
                        "vonk-agent: inventory report failed ({error}); retrying in {} seconds",
                        delay.as_secs()
                    );
                    systemd_notify::progress("Controller inventory report retrying");
                    tokio::time::sleep(delay).await;
                    continue;
                }
            }
        }
        match vonk_agent::package_activation::acknowledge(&client, &runtime_identity).await {
            Ok(receipt) => runtime_identity.package_activation = receipt,
            Err(error) => eprintln!("vonk-agent: package activation not acknowledged: {error}"),
        }
        let executor = ControlExecutor {
            recipes: RecipeExecutor {
                client: &client,
                runtime_root: Path::new("/run/vonk-forge-agent"),
                runtime: OciRuntime {
                    runner: &runner,
                    data_root: &config.data_dir,
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
        let local_managed_runs = executor.recipes.managed_recipe_run_count().unwrap_or(0);
        let exact_observation_count =
            managed_run_count(&exact_observation_result, local_managed_runs);
        if let Err(error) = &exact_observation_result {
            // A failed report must not terminate the claim lane: a
            // stop/recovery operation may already be waiting for us.
            eprintln!("vonk-agent: exact recipe observation failed: {error}");
        }
        let wait_seconds = claim_wait_seconds(
            POLL_MAX_SECONDS,
            exact_observation_count,
            readiness_published,
        );
        let fingerprint = vonk_agent::runtime_preflight::host_fingerprint(
            &runner,
            &runtime_identity.build_digest,
            &config.data_dir,
            Path::new("/run/vonk-forge-agent"),
        )
        .ok();
        let operation = async {
            run_once_with_claim_hook(
                &client,
                &mut state,
                &executor,
                fingerprint.as_deref(),
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
                systemd_notify::progress("Agent control loop progressing");
            }
            Err(error) if loop_error_is_fatal(&error) => return Err(error.into()),
            Err(error) => {
                failures = failures.saturating_add(1);
                inventory_reported_at = None;
                let delay = jittered_backoff(failures, POLL_MIN_SECONDS, POLL_MAX_SECONDS);
                eprintln!(
                    "vonk-agent: control loop degraded ({error}); retrying in {} seconds",
                    delay.as_secs()
                );
                systemd_notify::progress("Control loop degraded; retrying");
                tokio::time::sleep(delay).await;
            }
        }
    }
}

/// Only a refused agent identity ends the control lane.  Controller
/// rejections of one request, local state or readiness failures, and protocol
/// mismatches are logged and retried with backoff.
fn loop_error_is_fatal(error: &LoopError) -> bool {
    matches!(error, LoopError::Client(inner) if inner.fatal())
}

fn inventory_retry_delay(failures: u32, minimum: u64, maximum: u64) -> Duration {
    jittered_backoff(failures, minimum, maximum)
}

async fn collect_inventory_until_ready<Collect>(
    mut collect: Collect,
    failures: &mut u32,
    minimum: u64,
    maximum: u64,
) -> Inventory
where
    Collect: FnMut() -> Result<Inventory, InventoryError>,
{
    loop {
        match collect() {
            Ok(inventory) => return inventory,
            Err(error) => {
                *failures = (*failures).saturating_add(1);
                let delay = inventory_retry_delay(*failures, minimum, maximum);
                eprintln!(
                    "vonk-agent: degraded: host inventory unavailable ({error}); retrying in {} seconds",
                    delay.as_secs()
                );
                systemd_notify::progress(&format!(
                    "Degraded: host inventory unavailable ({error}); retrying in {} seconds",
                    delay.as_secs()
                ));
                tokio::time::sleep(delay).await;
            }
        }
    }
}

fn jittered_backoff(failures: u32, minimum: u64, maximum: u64) -> Duration {
    let entropy = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |value| value.subsec_nanos() as u64);
    let minimum = minimum.max(1);
    backoff_delay(failures, entropy, minimum, maximum.max(minimum))
}

/// Start only once a usable identity exists.  An expired active certificate
/// is never used for work, but it is not a reason to exit either: renewal is
/// retried idle until it succeeds or the Controller refuses this identity.
async fn ensure_startup_identity<IdentityCheck, Rotate, RotateFuture, Delay>(
    mut active_identity_is_valid: IdentityCheck,
    rotate: Rotate,
    delay: Delay,
) -> Result<(), RotationError>
where
    IdentityCheck: FnMut() -> Result<bool, RotationError>,
    Rotate: FnMut() -> RotateFuture,
    RotateFuture: Future<Output = Result<bool, RotationError>>,
    Delay: FnMut(u32) -> Duration,
{
    if active_identity_is_valid()? {
        return Ok(());
    }
    rotate_until_settled(rotate, active_identity_is_valid, delay)
        .await
        .map(|_| ())
}

/// Attempt certificate rotation until it settles: a replacement was
/// activated, or none was due while the active identity remains valid.  Only
/// a fatal error (401/403, revocation, unusable or foreign credentials) ends
/// the loop with an error; everything else backs off and retries.
async fn rotate_until_settled<Rotate, RotateFuture, IdentityCheck, Delay>(
    mut rotate: Rotate,
    mut active_identity_is_valid: IdentityCheck,
    mut delay: Delay,
) -> Result<bool, RotationError>
where
    Rotate: FnMut() -> RotateFuture,
    RotateFuture: Future<Output = Result<bool, RotationError>>,
    IdentityCheck: FnMut() -> Result<bool, RotationError>,
    Delay: FnMut(u32) -> Duration,
{
    let mut failures = 0_u32;
    loop {
        let reason = match rotate().await {
            Ok(true) => return Ok(true),
            Ok(false) if active_identity_is_valid()? => return Ok(false),
            Ok(false) => "no replacement certificate was activated".to_owned(),
            Err(error) if error.fatal() => return Err(error),
            Err(error) => error.to_string(),
        };
        failures = failures.saturating_add(1);
        let wait = delay(failures);
        if active_identity_is_valid()? {
            eprintln!(
                "vonk-agent: certificate renewal unavailable ({reason}); retrying in {} seconds while the active certificate remains valid",
                wait.as_secs()
            );
        } else {
            eprintln!(
                "vonk-agent: active certificate has expired and is not used; renewal unavailable ({reason}); retrying in {} seconds",
                wait.as_secs()
            );
            systemd_notify::progress(
                "Degraded: active certificate expired; waiting for Controller renewal",
            );
        }
        tokio::time::sleep(wait).await;
    }
}

fn inventory_refresh_due(reported_at: Option<Instant>, now: Instant) -> bool {
    // Idle claims wait at most 60 seconds. Refresh after two minutes so the
    // next loop still reports comfortably inside the Controller's five-minute
    // admission window. Only a successful report advances this deadline.
    reported_at.is_none_or(|reported_at| {
        now.saturating_duration_since(reported_at) >= Duration::from_secs(120)
    })
}

async fn run_rotation_lane(
    config: AgentConfig,
    client: AgentHttpClient,
) -> Result<(), RotationError> {
    let minimum = POLL_MIN_SECONDS;
    let interval = Duration::from_secs(minimum);
    loop {
        rotate_until_settled(
            || rotate_if_due(&config, &client),
            || active_identity_is_valid(&config),
            |failures| jittered_backoff(failures, minimum, POLL_MAX_SECONDS),
        )
        .await?;
        tokio::time::sleep(interval).await;
    }
}

#[derive(Debug, PartialEq, Eq)]
enum LaneExitWithRotation<C, R, S> {
    Control(C),
    Rotation(R),
    Shutdown(S),
}

async fn supervise_lanes_with_rotation<C, T, R, S>(
    control: C,
    telemetry: T,
    mut rotation: tokio::task::JoinHandle<R>,
    shutdown: S,
) -> LaneExitWithRotation<C::Output, Result<R, tokio::task::JoinError>, S::Output>
where
    C: Future,
    T: Future<Output = ()>,
    S: Future,
{
    tokio::pin!(control);
    tokio::pin!(telemetry);
    tokio::pin!(shutdown);
    let mut telemetry_running = true;
    let outcome = loop {
        tokio::select! {
            result = &mut control => break LaneExitWithRotation::Control(result),
            result = &mut rotation => break LaneExitWithRotation::Rotation(result),
            signal = &mut shutdown => break LaneExitWithRotation::Shutdown(signal),
            () = &mut telemetry, if telemetry_running => telemetry_running = false,
        }
    };
    if !matches!(outcome, LaneExitWithRotation::Rotation(_)) {
        rotation.abort();
        let _ = rotation.await;
    }
    outcome
}

/// A refused sweep reports no count, but the runs it could not report are
/// still retained locally.  Counting them as absent made the agent fall back
/// to the idle claim cadence exactly when it had work to observe.
fn managed_run_count(
    result: &Result<usize, RecipeObservationError>,
    local_managed_runs: usize,
) -> usize {
    result.as_ref().copied().unwrap_or(local_managed_runs)
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
        LaneExitWithRotation, claim_wait_seconds, collect_inventory_until_ready,
        ensure_startup_identity, inventory_refresh_due, inventory_retry_delay, loop_error_is_fatal,
        managed_run_count, report_ready_after_self_test, rotate_until_settled,
        supervise_lanes_with_rotation,
    };
    use std::{
        cell::{Cell, RefCell},
        future,
        sync::{
            Arc, Barrier,
            atomic::{AtomicBool, AtomicUsize, Ordering},
        },
        time::{Duration, Instant},
    };
    use vonk_agent::client::{ClientError, ControllerError};
    use vonk_agent::{executor::RecipeObservationError, rotation::RotationError};
    use vonk_agent::{inventory::Inventory, inventory::InventoryError};

    #[test]
    fn startup_readiness_is_reported_only_after_self_test_succeeds() {
        let events = RefCell::new(Vec::new());
        let identity = report_ready_after_self_test(
            || {
                events.borrow_mut().push("self-test");
                Ok::<_, &'static str>("identity")
            },
            || events.borrow_mut().push("ready"),
        )
        .unwrap();
        assert_eq!(identity, "identity");
        assert_eq!(*events.borrow(), ["self-test", "ready"]);

        let error = report_ready_after_self_test(
            || Err::<(), _>("self-test failed"),
            || panic!("failed self-test must not report READY=1"),
        )
        .unwrap_err();
        assert_eq!(error, "self-test failed");
    }

    #[test]
    fn idle_agent_refreshes_inventory_before_controller_admission_expires() {
        let reported_at = Instant::now();
        assert!(!inventory_refresh_due(
            Some(reported_at),
            reported_at + Duration::from_secs(119),
        ));
        // A successful idle claim must not leave the startup inventory as the
        // only report. The Controller refuses admission after five minutes.
        assert!(inventory_refresh_due(
            Some(reported_at),
            reported_at + Duration::from_secs(120),
        ));
        let refreshed_at = reported_at + Duration::from_secs(180);
        assert!(!inventory_refresh_due(Some(refreshed_at), refreshed_at));
        assert!(inventory_refresh_due(None, refreshed_at));
    }

    #[test]
    fn startup_inventory_retry_backoff_is_bounded_by_agent_poll_limits() {
        for failure in [1, 2, 3, 20, u32::MAX] {
            let delay = inventory_retry_delay(failure, 2, 30);
            assert!(delay >= Duration::from_secs(2));
            assert!(delay <= Duration::from_secs(30));
        }
    }

    #[tokio::test(start_paused = true)]
    async fn startup_inventory_retries_host_prerequisite_failures_until_recovery() {
        let attempts = Cell::new(0_u32);
        let mut failures = 0_u32;
        let inventory = collect_inventory_until_ready(
            || {
                let attempt = attempts.get() + 1;
                attempts.set(attempt);
                if attempt <= 7 {
                    Err(InventoryError::PrerequisiteUnavailable("Podman"))
                } else {
                    Ok(Inventory {
                        memory_total_bytes: 100,
                        memory_available_bytes: 90,
                        disk_total_bytes: 1000,
                        disk_available_bytes: 900,
                        state_database_reserve_held: true,
                        gpu_count: 1,
                        gpu_memory_total_bytes: 100,
                        gpu_memory_free_bytes: 90,
                        memory_pool: vonk_agent_protocol::MemoryPool::Separate,
                        nvidia_driver_version: "test".to_owned(),
                        container_runtime_version: "test".to_owned(),
                        artifact_store_read_only: false,
                        capabilities: vec![],
                        fabric_address: None,
                        fabric_bandwidth_mbps: None,
                        network_interfaces: None,
                        nas_route_interface: None,
                    })
                }
            },
            &mut failures,
            1,
            1,
        )
        .await;

        assert_eq!(attempts.get(), 8);
        assert_eq!(failures, 7);
        assert_eq!(inventory.disk_available_bytes, 900);
    }

    #[test]
    fn refused_observation_sweep_keeps_the_managed_run_cadence() {
        let refused = Err(RecipeObservationError::Report(ClientError::Protocol));
        let count = managed_run_count(&refused, 1);
        assert_eq!(count, 1);
        assert_eq!(claim_wait_seconds(60, count, true), 10);
        assert_eq!(managed_run_count(&Ok(2), 0), 2);
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

    #[tokio::test]
    async fn rotation_lane_failure_is_supervised_independently_of_control_lane() {
        let outcome = supervise_lanes_with_rotation(
            future::pending::<()>(),
            future::pending::<()>(),
            tokio::spawn(future::ready(Err::<(), _>("rotation failed"))),
            future::pending::<()>(),
        )
        .await;
        assert!(matches!(
            outcome,
            LaneExitWithRotation::Rotation(Ok(Err("rotation failed")))
        ));
    }

    #[tokio::test]
    async fn valid_startup_identity_does_not_wait_for_controller_renewal() {
        let attempts = Arc::new(AtomicUsize::new(0));
        let operation_attempts = attempts.clone();
        ensure_startup_identity(
            || Ok(true),
            move || {
                operation_attempts.fetch_add(1, Ordering::SeqCst);
                future::ready(Err(RotationError::Client(ClientError::Retryable)))
            },
            |_| Duration::ZERO,
        )
        .await
        .unwrap();

        assert_eq!(attempts.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn background_certificate_rotation_recovers_after_controller_outage() {
        let attempts = Arc::new(AtomicUsize::new(0));
        let operation_attempts = attempts.clone();
        let rotated = rotate_until_settled(
            move || {
                let attempt = operation_attempts.fetch_add(1, Ordering::SeqCst);
                future::ready(match attempt {
                    0..3 => Err(RotationError::Client(ClientError::Controller(Box::new(
                        ControllerError::from_status(503),
                    )))),
                    // A request-level rejection is not a reason to exit.
                    3..6 => Err(RotationError::Client(ClientError::Controller(Box::new(
                        ControllerError::from_status(422),
                    )))),
                    _ => Ok(true),
                })
            },
            || Ok(true),
            |_| Duration::ZERO,
        )
        .await
        .unwrap();

        assert!(rotated);
        assert_eq!(attempts.load(Ordering::SeqCst), 7);
    }

    #[tokio::test]
    async fn expired_startup_identity_idles_until_renewal_instead_of_exiting() {
        let attempts = Arc::new(AtomicUsize::new(0));
        let operation_attempts = attempts.clone();
        let renewed = Arc::new(AtomicBool::new(false));
        let operation_renewed = renewed.clone();
        let identity_renewed = renewed.clone();
        ensure_startup_identity(
            move || Ok(identity_renewed.load(Ordering::SeqCst)),
            move || {
                let attempt = operation_attempts.fetch_add(1, Ordering::SeqCst);
                future::ready(match attempt {
                    0 => Err(RotationError::Client(ClientError::Retryable)),
                    1 => Ok(false),
                    2 => Err(RotationError::ActiveIdentityExpired),
                    _ => {
                        operation_renewed.store(true, Ordering::SeqCst);
                        Ok(true)
                    }
                })
            },
            |_| Duration::ZERO,
        )
        .await
        .unwrap();

        assert_eq!(attempts.load(Ordering::SeqCst), 4);
        assert!(renewed.load(Ordering::SeqCst));
    }

    #[tokio::test]
    async fn expired_startup_identity_exits_when_the_controller_refuses_it() {
        for status in [401, 403] {
            let attempts = Arc::new(AtomicUsize::new(0));
            let operation_attempts = attempts.clone();
            let denied = ensure_startup_identity(
                || Ok(false),
                move || {
                    operation_attempts.fetch_add(1, Ordering::SeqCst);
                    future::ready(Err(RotationError::Client(ClientError::Controller(
                        Box::new(ControllerError::from_status(status)),
                    ))))
                },
                |_| Duration::ZERO,
            )
            .await
            .unwrap_err();

            assert!(denied.fatal());
            assert_eq!(attempts.load(Ordering::SeqCst), 1);
        }
    }

    #[test]
    fn only_refused_identity_ends_the_control_lane() {
        use vonk_agent::executor::LoopError;
        for status in [401, 403] {
            assert!(loop_error_is_fatal(&LoopError::Client(
                ClientError::Controller(Box::new(ControllerError::from_status(status)))
            )));
        }
        assert!(loop_error_is_fatal(&LoopError::Client(
            ClientError::Identity
        )));
        assert!(loop_error_is_fatal(&LoopError::Client(ClientError::Pin)));
        for status in [400, 404, 409, 422] {
            assert!(!loop_error_is_fatal(&LoopError::Client(
                ClientError::Controller(Box::new(ControllerError::from_status(status)))
            )));
        }
        assert!(!loop_error_is_fatal(&LoopError::Client(
            ClientError::Protocol
        )));
        assert!(!loop_error_is_fatal(&LoopError::HeartbeatTask));
        assert!(!loop_error_is_fatal(&LoopError::Readiness(
            "readiness file unwritable".to_owned()
        )));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn spawned_rotation_progresses_while_control_thread_blocks() {
        let progressed = Arc::new(AtomicBool::new(false));
        let control_entered = Arc::new(AtomicBool::new(false));
        let barrier = Arc::new(Barrier::new(2));
        let rotation_progress = progressed.clone();
        let rotation_entered = control_entered.clone();
        let rotation_barrier = barrier.clone();
        let rotation = tokio::spawn(async move {
            while !rotation_entered.load(Ordering::SeqCst) {
                tokio::task::yield_now().await;
            }
            // The first barrier rendezvous proves control is blocked in its
            // synchronous section before rotation can make progress.
            rotation_barrier.wait();
            rotation_progress.store(true, Ordering::SeqCst);
            rotation_barrier.wait();
            future::pending::<Result<(), &'static str>>().await
        });
        let control_barrier = barrier.clone();
        let outcome = tokio::time::timeout(
            Duration::from_secs(1),
            supervise_lanes_with_rotation(
                async move {
                    control_entered.store(true, Ordering::SeqCst);
                    control_barrier.wait();
                    control_barrier.wait();
                    "control finished"
                },
                future::pending::<()>(),
                rotation,
                future::pending::<()>(),
            ),
        )
        .await
        .expect("independent rotation lane was starved by control");
        assert!(progressed.load(Ordering::SeqCst));
        assert!(matches!(
            outcome,
            LaneExitWithRotation::Control("control finished")
        ));
    }
}
