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
        // Keep the same sample until it finishes, even after its first tick
        // budget. Slow driver failures carry evidence that must be uploaded.
        if sampling.is_none() {
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
                let collector =
                    collector_for(&config, Path::new("/proc/sys/kernel/random/boot_id"))?;
                Some((config, collector.sample()))
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

    #[tokio::test]
    async fn slow_sample_is_reportable_after_its_first_tick_budget() {
        // Catches discarding the finished worker after a driver query times out.
        let (release, receiver) = tokio::sync::oneshot::channel();
        let mut sampling = Some(tokio::spawn(async move { receiver.await.unwrap() }));
        assert!(
            super::completed_sample(&mut sampling, tokio::time::Instant::now())
                .await
                .is_none()
        );
        assert!(sampling.is_some());
        release.send(42).unwrap();
        assert_eq!(
            super::completed_sample(&mut sampling, tokio::time::Instant::now() + INTERVAL)
                .await
                .unwrap()
                .unwrap(),
            42
        );
        assert!(sampling.is_none());
    }

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
