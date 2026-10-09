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
    let mut next_tick = tokio::time::Instant::now();
    let mut sampling = None;
    loop {
        tokio::time::sleep_until(next_tick).await;
        let tick_started = tokio::time::Instant::now();
        // A timed-out native sample retains its one worker until it settles;
        // ticks never accumulate workers or invent a boot identity.
        if sampling
            .as_ref()
            .is_some_and(|task: &tokio::task::JoinHandle<_>| !task.is_finished())
        {
            next_tick = next_tick_after(tick_started, tokio::time::Instant::now());
            continue;
        }
        if let Some(task) = sampling.take() {
            let _ = task.await;
        }
        let config_path = config_path.to_owned();
        let task = tokio::task::spawn_blocking(move || {
            // Initialization belongs to this sample, not service startup.
            // Reload every tick so collector paths follow current config.
            let config = match AgentConfig::load(&config_path) {
                Ok(config) => config,
                Err(error) => {
                    eprintln!("vonk-monitor: configuration observation unavailable: {error}");
                    return None;
                }
            };
            let collector = collector_for(&config, Path::new("/proc/sys/kernel/random/boot_id"))?;
            Some((config, collector.sample()))
        });
        sampling = Some(task);
        if let Some(task) = sampling.as_mut() {
            match tokio::time::timeout_at(tick_started + INTERVAL, task).await {
                Ok(Ok(Some((config, sample)))) => {
                    sampling = None;
                    if let Ok(client) = AgentHttpClient::from_config(&config) {
                        // One bounded upload; a miss ends this sample. The next
                        // tick observes current config, credentials and paths.
                        let _ = tokio::time::timeout(INTERVAL, client.report_telemetry(&[sample]))
                            .await;
                    }
                }
                Ok(_) => sampling = None,
                Err(_) => eprintln!("vonk-monitor: sample observation budget elapsed"),
            }
        }
        next_tick = next_tick_after(tick_started, tokio::time::Instant::now());
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

fn next_tick_after(
    started: tokio::time::Instant,
    finished: tokio::time::Instant,
) -> tokio::time::Instant {
    finished
        + (INTERVAL
            - Duration::from_nanos(
                (finished.saturating_duration_since(started).as_nanos() % INTERVAL.as_nanos())
                    as u64,
            ))
}

fn telemetry_paths(config: &AgentConfig) -> TelemetryPaths {
    TelemetryPaths {
        meminfo: PathBuf::from("/proc/meminfo"),
        cpu_root: PathBuf::from("/sys/devices/system/cpu"),
        store: config.data_dir.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::{INTERVAL, next_tick_after};
    use std::time::Duration;

    #[test]
    fn boot_projection_loss_ends_a_sample_and_the_next_tick_can_initialize() {
        let root = std::env::temp_dir().join(format!(
            "vonk-monitor-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&root).unwrap();
        let config = vonk_agent::config::AgentConfig::parse(&format!(
            "enrollment_url = \"https://enroll.example/\"\ncontroller_url = \"https://control.example/\"\nca_path = \"/etc/ca.pem\"\nca_sha256 = \"{}\"\ndata_dir = \"{}\"\nnode_id = \"spk_0123456789abcdef0123456789abcdef\"\n",
            "a".repeat(64), root.display(),
        )).unwrap();
        let boot = root.join("boot-id");
        assert!(super::collector_for(&config, &boot).is_none());
        std::fs::write(&boot, b"damaged projection").unwrap();
        assert!(super::collector_for(&config, &boot).is_none());
        std::fs::write(&boot, b"10000000-0000-4000-8000-000000000001\n").unwrap();
        assert!(super::collector_for(&config, &boot).is_some());
        std::fs::remove_file(boot).unwrap();
        std::fs::remove_dir(root).unwrap();
    }

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
