#![forbid(unsafe_code)]

use std::{
    io::{self, Read},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use clap::{Parser, Subcommand};
use url::Url;
use vonk_agent::{
    client::{AgentHttpClient, ClientError},
    config::{AgentConfig, DEFAULT_CONFIG_PATH},
    executor::{LoopError, RecipeExecutor, run_once},
    inventory::InventoryCollector,
    oci::OciRuntime,
    pair::{EnrollmentOutcome, collect_evidence, pair},
    process::SystemProcessRunner,
    rotation::rotate_if_due,
    state::{StateStore, backoff_delay},
    supervisor_readiness::SupervisorReadiness,
};

#[derive(Parser)]
#[command(name = "vonk-agent", version, about = "Vonk Forge outbound agent")]
struct Cli {
    #[arg(long, default_value = DEFAULT_CONFIG_PATH)]
    config: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Run,
    /// Import terminal receipts from a stopped legacy Python agent.
    MigratePythonState {
        #[arg(long)]
        source: PathBuf,
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
    let config = AgentConfig::load(&cli.config)?;
    match cli.command {
        Command::Run => run_agent(&config).await?,
        Command::MigratePythonState { source } => {
            let mut state =
                StateStore::open(&config.data_dir.join("state.sqlite"), &config.node_id)?;
            let imported = state.import_python_receipts(&source)?;
            println!("imported {imported} terminal Python receipts");
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
            let executable = std::env::current_exe()?;
            let evidence = collect_evidence(&executable)?;
            match pair(&config, &enrollment, token.trim(), &ca_sha256, evidence).await? {
                EnrollmentOutcome::Pending(pending) => {
                    println!("pairing {} is {}", pending.id, pending.state);
                }
                EnrollmentOutcome::Issued => println!("paired {}", config.node_id),
            }
        }
    }
    Ok(())
}

async fn run_agent(config: &AgentConfig) -> Result<(), Box<dyn std::error::Error>> {
    let mut supervisor_readiness = SupervisorReadiness::from_process_environment()?;
    rotate_if_due(config).await?;
    let mut client = AgentHttpClient::from_config(config)?;
    let mut state = StateStore::open(&config.data_dir.join("state.sqlite"), &config.node_id)?;
    state.recover_interrupted()?;
    let runner = SystemProcessRunner;
    let capabilities = [
        "agent.runtime.rust.v1",
        "recipe.build.v1",
        "recipe.image.import.v1",
        "recipe.install",
        "recipe.start",
        "recipe.stop",
        "recipe.uninstall",
    ];
    let mut failures = 0_u32;
    let mut observation_failures = 0_u32;
    let mut next_inventory = tokio::time::Instant::now();
    loop {
        if tokio::time::Instant::now() >= next_inventory {
            if rotate_if_due(config).await? {
                client = AgentHttpClient::from_config(config)?;
            }
            let inventory = InventoryCollector {
                runner: &runner,
                meminfo_path: Path::new("/proc/meminfo"),
                store_path: &config.data_dir,
                fabric_address: config.fabric_address,
                fabric_bandwidth_mbps: config.fabric_bandwidth_mbps,
            }
            .collect()?;
            match client.report_inventory(&inventory).await {
                Ok(()) => {
                    supervisor_readiness.report()?;
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
        let executor = RecipeExecutor {
            client: &client,
            runtime_root: Path::new("/run/vonk-forge-agent"),
            runtime: OciRuntime {
                runner: &runner,
                data_root: &config.data_dir,
                huggingface_curl_config: config.huggingface_curl_config.as_deref(),
            },
        };
        let observations = executor.runtime.recipe_run_observations()?;
        let wait_seconds = claim_wait_seconds(config.poll_max_seconds, observations.len());
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
            run_once(
                &client,
                &mut state,
                &executor,
                &capabilities,
                wait_seconds,
                supervisor_readiness.runtime_identity(),
            )
            .await
        };
        tokio::select! {
            result = operation => match result {
                Ok(()) => failures = 0,
                Err(error) if matches!(&error, vonk_agent::executor::LoopError::Client(inner) if inner.retryable()) => {
                    failures = failures.saturating_add(1);
                    let entropy = SystemTime::now().duration_since(UNIX_EPOCH)?.subsec_nanos() as u64;
                    tokio::time::sleep(backoff_delay(
                        failures,
                        entropy,
                        config.poll_min_seconds,
                        config.poll_max_seconds,
                    )).await;
                }
                Err(error) => return Err(error.into()),
            },
            signal = tokio::signal::ctrl_c() => {
                signal?;
                return Ok(());
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ObservationFailureAction {
    BackoffThenClaim,
    Stop,
}

fn observation_failure_action(error: &ClientError) -> ObservationFailureAction {
    if error.retryable() {
        ObservationFailureAction::BackoffThenClaim
    } else {
        ObservationFailureAction::Stop
    }
}

fn claim_wait_seconds(configured_maximum: u64, managed_run_count: usize) -> u64 {
    let existing_wait = configured_maximum.min(60);
    if managed_run_count == 0 {
        existing_wait
    } else {
        existing_wait.min(10)
    }
}

#[cfg(test)]
mod tests {
    use super::{ObservationFailureAction, claim_wait_seconds, observation_failure_action};
    use vonk_agent::client::ClientError;

    #[test]
    fn managed_runs_cap_claim_long_poll_at_ten_seconds() {
        assert_eq!(claim_wait_seconds(60, 1), 10);
        assert_eq!(claim_wait_seconds(7, 1), 7);
    }

    #[test]
    fn no_managed_runs_retain_existing_claim_long_poll_behavior() {
        assert_eq!(claim_wait_seconds(300, 0), 60);
        assert_eq!(claim_wait_seconds(7, 0), 7);
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
}
