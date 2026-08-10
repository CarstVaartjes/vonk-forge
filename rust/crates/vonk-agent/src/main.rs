#![forbid(unsafe_code)]

use std::{
    io::{self, Read},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use clap::{Parser, Subcommand};
use url::Url;
use vonk_agent::{
    client::AgentHttpClient,
    config::{AgentConfig, DEFAULT_CONFIG_PATH},
    executor::{RecipeExecutor, run_once},
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

#[tokio::main]
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
            runtime: OciRuntime {
                runner: &runner,
                data_root: &config.data_dir,
                huggingface_curl_config: config.huggingface_curl_config.as_deref(),
            },
        };
        let operation = run_once(
            &client,
            &mut state,
            &executor,
            &capabilities,
            config.poll_max_seconds.min(60),
            supervisor_readiness.runtime_identity(),
        );
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
