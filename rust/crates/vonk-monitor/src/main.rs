#![forbid(unsafe_code)]

use std::{
    path::{Path, PathBuf},
    time::Duration,
};

use clap::Parser;
use vonk_agent::{
    client::AgentHttpClient,
    config::{AgentConfig, DEFAULT_CONFIG_PATH},
    process::SystemProcessRunner,
    telemetry::{SystemFileSystemProvider, TelemetryCollector, TelemetryPaths, read_boot_id},
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
    let boot_id = loop {
        match read_boot_id(Path::new("/proc/sys/kernel/random/boot_id")) {
            Ok(value) => break value,
            Err(error) => {
                eprintln!("vonk-monitor: boot identity unavailable: {error}");
                tokio::time::sleep(INTERVAL).await;
            }
        }
    };
    let initial_config = loop {
        match AgentConfig::load(config_path) {
            Ok(config) => break config,
            Err(error) => {
                eprintln!(
                    "vonk-monitor: operation=configuration.load endpoint={} "
                        "error={error}; decision=defer-until-next-interval",
                    config_path.display()
                );
                tokio::time::sleep(INTERVAL).await;
            }
        }
    };
    let mut collector = TelemetryCollector::new_ephemeral(
        SystemProcessRunner,
        SystemFileSystemProvider,
        telemetry_paths(&initial_config),
        boot_id,
    )?;
    let mut previous = None;
    let mut next_tick = tokio::time::Instant::now();

    loop {
        tokio::time::sleep_until(next_tick).await;
        let tick_started = tokio::time::Instant::now();
        let prior = previous.take();
        let collection = tokio::task::spawn_blocking(move || {
            let result = collector.sample(prior.as_ref());
            (collector, prior, result)
        })
        .await;
        let Ok((returned_collector, prior, result)) = collection else {
            eprintln!("vonk-monitor: collector task stopped; retrying next interval");
            next_tick = next_tick_after(tick_started, tokio::time::Instant::now());
            continue;
        };
        collector = returned_collector;
        match result {
            Ok(sample) => {
                previous = Some(sample.clone());
                match AgentConfig::load(config_path)
                    .ok()
                    .and_then(|config| AgentHttpClient::from_config(&config).ok())
                {
                    Some(client) => {
                        // This is intentionally one request for one fresh
                        // sample. A failed upload is dropped at this point;
                        // no retry queue or replay exists.
                        if let Err(error) = client.report_telemetry(&[sample]).await {
                            eprintln!(
                                "vonk-monitor: operation=telemetry.upload endpoint=/agent/v1/telemetry "
                                    "error={error}; decision=discard-and-collect-next-interval"
                            );
                        }
                    }
                    None => eprintln!(
                        "vonk-monitor: operation=telemetry.upload endpoint=/agent/v1/telemetry "
                            "error=active-credentials-unavailable; decision=discard-and-collect-next-interval"
                    ),
                }
            }
            Err(error) => {
                previous = prior;
                eprintln!("vonk-monitor: snapshot unavailable: {error}");
            }
        }
        next_tick = next_tick_after(tick_started, tokio::time::Instant::now());
    }
}

fn next_tick_after(
    started: tokio::time::Instant,
    finished: tokio::time::Instant,
) -> tokio::time::Instant {
    let mut next = started + INTERVAL;
    while next <= finished {
        next += INTERVAL;
    }
    next
}

fn telemetry_paths(config: &AgentConfig) -> TelemetryPaths {
    TelemetryPaths {
        stat: PathBuf::from("/proc/stat"),
        loadavg: PathBuf::from("/proc/loadavg"),
        uptime: PathBuf::from("/proc/uptime"),
        meminfo: PathBuf::from("/proc/meminfo"),
        net_dev: PathBuf::from("/proc/net/dev"),
        store: config.data_dir.clone(),
        sys_block: PathBuf::from("/sys/block"),
        sys_class_net: PathBuf::from("/sys/class/net"),
        thermal: PathBuf::from("/sys/class/thermal"),
        powercap: PathBuf::from("/sys/class/powercap"),
    }
}

#[cfg(test)]
mod tests {
    use super::{INTERVAL, next_tick_after};
    use std::time::Duration;

    #[test]
    fn missed_ticks_are_skipped_without_backlog() {
        let started = tokio::time::Instant::now();
        let finished = started + Duration::from_secs(7);
        assert_eq!(
            next_tick_after(started, finished),
            started + Duration::from_secs(8)
        );
    }

    #[test]
    fn normal_tick_is_exactly_two_seconds() {
        let started = tokio::time::Instant::now();
        assert_eq!(next_tick_after(started, started), started + INTERVAL);
    }
}
