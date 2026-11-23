use std::{
    path::{Path, PathBuf},
    time::Duration,
};

use clap::Parser;
use vonk_agent::{
    client::AgentHttpClient,
    config::{AgentConfig, DEFAULT_CONFIG_PATH},
    process::SystemProcessRunner,
    telemetry::{
        SystemFileSystemProvider, TelemetryCollector, TelemetryPaths,
        engine_metrics::{RUNTIME_DIRECTORY, SCRAPE_DEADLINE, TARGETS_DIRECTORY, scrape_engines},
        read_boot_id,
    },
};

const INTERVAL: Duration = Duration::from_secs(2);

#[derive(Parser)]
#[command(name = "vonk-monitor", about = "Vonk Forge best-effort Spark monitor")]
struct Cli {
    #[arg(long, default_value = DEFAULT_CONFIG_PATH)]
    config: PathBuf,
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    run(&cli.config).await
}

async fn run(config_path: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let mut next_tick = tokio::time::Instant::now();
    let mut sampling = None;
    loop {
        tokio::time::sleep_until(next_tick).await;
        let tick_started = tokio::time::Instant::now();
        // Keep the same sample until it finishes, even after its first tick
        // budget. Slow driver failures carry evidence that must be uploaded.
        if sampling.is_none() {
            let config_path = config_path.to_owned();
            // Engines are read beside the host sample under their own short
            // deadline; a slow engine only goes missing from this sample.
            let task = tokio::spawn(async move {
                let scrape = tokio::spawn(async {
                    scrape_engines(
                        &Path::new(RUNTIME_DIRECTORY).join(TARGETS_DIRECTORY),
                        SCRAPE_DEADLINE,
                    )
                    .await
                });
                let sampled = tokio::task::spawn_blocking(move || {
                    // Initialization belongs to this sample, not service startup.
                    // Reload every tick so collector paths follow current config.
                    let config = match AgentConfig::load(&config_path) {
                        Ok(config) => config,
                        Err(error) => {
                            eprintln!(
                                "vonk-monitor: configuration observation unavailable: {error}"
                            );
                            return None;
                        }
                    };
                    let collector =
                        collector_for(&config, Path::new("/proc/sys/kernel/random/boot_id"))?;
                    Some((config, collector.sample()))
                })
                .await;
                let (config, mut sample) = match sampled {
                    Ok(observed) => observed?,
                    Err(error) => {
                        eprintln!("vonk-monitor: sample worker failed: {error}");
                        return None;
                    }
                };
                let engines = scrape.await.unwrap_or_default();
                sample.engines = (!engines.is_empty()).then_some(engines);
                Some((config, sample))
            });
            sampling = Some(task);
        }
        match completed_sample(&mut sampling, tick_started + INTERVAL).await {
            Some(result) => report_sample(result).await,
            None => eprintln!(
                "vonk-monitor: sample observation budget elapsed; retaining current sample"
            ),
        }
        next_tick = next_tick_after(tick_started, tokio::time::Instant::now());
    }
}

async fn completed_sample<T>(
    sampling: &mut Option<tokio::task::JoinHandle<T>>,
    deadline: tokio::time::Instant,
) -> Option<Result<T, tokio::task::JoinError>> {
    let result = tokio::time::timeout_at(deadline, sampling.as_mut()?)
        .await
        .ok()?;
    *sampling = None;
    Some(result)
}

async fn report_sample(
    result: Result<
        Option<(AgentConfig, vonk_agent::telemetry::TelemetrySample)>,
        tokio::task::JoinError,
    >,
) {
    let (config, sample) = match result {
        Ok(Some(observed)) => observed,
        Ok(None) => return, // The collector logged its initialization cause.
        Err(error) => {
            eprintln!("vonk-monitor: sample worker failed: {error}");
            return;
        }
    };
    let client = match AgentHttpClient::from_config(&config) {
        Ok(client) => client,
        Err(error) => {
            eprintln!("vonk-monitor: telemetry client unavailable: {error}");
            return;
        }
    };
    // Upload once, including slow observations with their failure causes.
    // The next tick observes current config, credentials and paths.
    match tokio::time::timeout(INTERVAL, client.report_telemetry(&[sample])).await {
        Ok(Ok(())) => {}
        Ok(Err(error)) => eprintln!("vonk-monitor: telemetry upload failed: {error}"),
        Err(error) => eprintln!("vonk-monitor: telemetry upload budget elapsed: {error}"),
    }
}

fn collector_for(
    config: &AgentConfig,
    boot_path: &Path,
) -> Option<TelemetryCollector<SystemProcessRunner, SystemFileSystemProvider>> {
    let boot_id = match read_boot_id(boot_path) {
        Ok(boot_id) => boot_id,
        Err(error) => {
            eprintln!("vonk-monitor: boot identity observation unavailable: {error}");
            return None;
        }
    };
    TelemetryCollector::new(
        SystemProcessRunner,
        SystemFileSystemProvider,
        telemetry_paths(config),
        boot_id,
    )
    .ok()
}

#[expect(
    clippy::cast_possible_truncation,
    reason = "the remainder is below the interval, which fits u64 nanoseconds"
)]
#[expect(
    clippy::unwrap_used,
    reason = "the remainder is below the interval, so the subtraction cannot underflow"
)]
fn next_tick_after(
    started: tokio::time::Instant,
    finished: tokio::time::Instant,
) -> tokio::time::Instant {
    finished
        + INTERVAL
            .checked_sub(Duration::from_nanos(
                (finished.saturating_duration_since(started).as_nanos() % INTERVAL.as_nanos())
                    as u64,
            ))
            .unwrap()
}

fn telemetry_paths(config: &AgentConfig) -> TelemetryPaths {
    TelemetryPaths {
        meminfo: PathBuf::from("/proc/meminfo"),
        uptime: PathBuf::from("/proc/uptime"),
        cpu_root: PathBuf::from("/sys/devices/system/cpu"),
        store: config.data_dir.clone(),
        host: vonk_agent::telemetry::host_readings::HostRoots::default(),
    }
}

#[cfg(test)]
#[path = "main_tests.rs"]
mod tests;
