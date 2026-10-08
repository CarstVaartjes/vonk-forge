//! Independent protection for the one physical GB10 memory pool. No kit tuning.
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        Arc, Mutex,
        mpsc::{self, Receiver},
    },
    thread,
    time::{Duration, Instant},
};

use crate::operations::{CommandRunner, ProcessCommandRunner};
use vonk_agent_protocol::failure_evidence::{
    FailureCategory, FailureDiagnostics, FailureLogTail, FailureProperty,
};
use wait_timeout::ChildExt;

pub use vonk_agent_protocol::host_memory_guard_policy::{
    COMMAND_TIMEOUT, EXHAUSTED_BYTES, FULL_PSI_PERCENT, JOURNAL_RETRY, PRELOAD_MARGIN_BYTES,
    SAMPLE_INTERVAL, SUSTAINED_PRESSURE,
};
fn failure_reason() -> &'static str {
    vonk_agent_protocol::generated::FailureCode::WorkloadHostMemoryExhausted.as_str()
}
const EXIT_CAUSE: &str = "host_memory_exhausted";

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Sample {
    pub available_bytes: Option<u64>,
    pub full_avg10: Option<f64>,
}

pub fn read_sample(mut read: impl FnMut(&str) -> std::io::Result<String>) -> Sample {
    let available_bytes = read("/proc/meminfo").ok().and_then(|text| {
        text.lines().find_map(|line| {
            let mut fields = line.split_whitespace();
            (fields.next()? == "MemAvailable:")
                .then(|| fields.next()?.parse::<u64>().ok()?.checked_mul(1024))?
        })
    });
    let full_avg10 = read("/proc/pressure/memory").ok().and_then(|text| {
        text.lines().find_map(|line| {
            let mut fields = line.split_whitespace();
            if fields.next()? != "full" {
                return None;
            }
            let value = fields
                .find_map(|field| field.strip_prefix("avg10="))?
                .parse::<f64>()
                .ok()?;
            (value.is_finite() && (0.0..=100.0).contains(&value)).then_some(value)
        })
    });
    Sample {
        available_bytes,
        full_avg10,
    }
}

fn nvidia_allocation_failure(line: &str) -> bool {
    line.contains("NVRM:") && line.contains("Out of memory") && line.contains("NV_ERR_NO_MEMORY")
}

#[derive(Default)]
pub struct Decision {
    exhausted_since: Option<Instant>,
    stalled_since: Option<Instant>,
    last_sample: Option<Instant>,
}

impl Decision {
    pub fn observe(
        &mut self,
        now: Instant,
        sample: Sample,
        driver_failure: bool,
        managed: bool,
    ) -> Option<&'static str> {
        if !managed
            || self
                .last_sample
                .is_some_and(|last| now.duration_since(last) > SAMPLE_INTERVAL * 2)
        {
            *self = Self::default();
        }
        if !managed {
            return None;
        }
        self.last_sample = Some(now);
        let sustained = |since: &mut Option<Instant>, active: bool| {
            if !active {
                *since = None;
                return false;
            }
            now.duration_since(*since.get_or_insert(now)) >= SUSTAINED_PRESSURE
        };
        let exhausted = sustained(
            &mut self.exhausted_since,
            sample
                .available_bytes
                .is_some_and(|bytes| bytes < EXHAUSTED_BYTES),
        );
        let stalled = sustained(
            &mut self.stalled_since,
            sample.full_avg10.is_some_and(|psi| psi > FULL_PSI_PERCENT),
        );
        if driver_failure {
            Some("nvidia_allocation_failure")
        } else if stalled {
            Some("sustained_full_psi")
        } else if exhausted {
            Some("sustained_memavailable_exhaustion")
        } else {
            None
        }
    }
    fn pressure_duration(&self, now: Instant, trigger: &str) -> Duration {
        let since = match trigger {
            "sustained_full_psi" => self.stalled_since,
            "sustained_memavailable_exhaustion" => self.exhausted_since,
            _ => None,
        };
        since.map_or(Duration::ZERO, |since| now.duration_since(since))
    }
}

fn property(name: &str, value: impl ToString) -> FailureProperty {
    FailureProperty {
        name: name.into(),
        value: value.to_string(),
    }
}

fn evidence(sample: Sample, trigger: &str, duration: Duration) -> FailureDiagnostics {
    let observed_at = chrono::Utc::now();
    let began_at = observed_at - chrono::Duration::from_std(duration).unwrap_or_default();
    let empty = || FailureLogTail {
        text: String::new(),
        truncated: false,
        dropped_bytes: Some(0),
        dropped_lines: Some(0),
    };
    FailureDiagnostics {
        schema_version: 1,
        collected_at: observed_at.to_rfc3339(),
        phase: "workload.host_memory_guard".into(),
        category: FailureCategory::Capacity,
        stdout: empty(),
        stderr: empty(),
        versions: vec![],
        sandbox: vec![],
        storage: vec![],
        collector_errors: vec![],
        preflight: vec![
            property("reason", failure_reason()),
            property("exit_cause", EXIT_CAUSE),
            property("trigger", trigger),
            property(
                "mem_available_bytes",
                sample
                    .available_bytes
                    .map_or_else(|| "unknown".into(), |v| v.to_string()),
            ),
            property(
                "memory_full_avg10",
                sample
                    .full_avg10
                    .map_or_else(|| "unknown".into(), |v| v.to_string()),
            ),
            property("sustained_ms", duration.as_millis()),
            property("pressure_started_at", began_at.to_rfc3339()),
        ],
    }
}

fn evidence_path(root: &Path, id: &str) -> Option<PathBuf> {
    valid_id(id).then(|| root.join("host-memory-evidence").join(format!("{id}.json")))
}
fn valid_id(id: &str) -> bool {
    id.len() == 64
        && id
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

pub fn load_evidence(root: &Path, id: &str) -> Option<FailureDiagnostics> {
    let path = evidence_path(root, id)?;
    let mut bytes = Vec::new();
    File::open(path)
        .ok()?
        .take(16 * 1024 + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() > 16 * 1024 {
        return None;
    }
    let value: FailureDiagnostics = vonk_agent_protocol::parse_strict(&bytes).ok()?;
    value.validate().ok()?;
    Some(value)
}

fn publish(root: &Path, id: &str, value: &FailureDiagnostics) -> std::io::Result<()> {
    let path = evidence_path(root, id)
        .ok_or_else(|| std::io::Error::other("invalid container identity"))?;
    fs::create_dir_all(path.parent().expect("evidence parent"))?;
    let temporary = path.with_extension("tmp");
    let mut file = File::create(&temporary)?;
    let bytes =
        vonk_agent_protocol::canonical_generated_json(value).map_err(std::io::Error::other)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    fs::rename(temporary, &path)?;
    File::open(path.parent().expect("evidence parent"))?.sync_all()
}

const IDENTITY_FORMAT: &str = "{{.Id}}\t{{.State.Running}}\t{{index .Config.Labels \"ai.vonkforge.managed\"}}\t{{index .Config.Labels \"ai.vonkforge.target-id\"}}\t{{index .Config.Labels \"ai.vonkforge.runtime-request-sha256\"}}\t{{.State.Pid}}";

fn managed_identity(line: &str, expected: &str) -> Option<i32> {
    let fields: Vec<_> = line.trim().split('\t').collect();
    let [id, running, managed, target, digest, pid] = fields.as_slice() else {
        return None;
    };
    if *id != expected
        || !valid_id(id)
        || *running != "true"
        || *managed != "true"
        || uuid::Uuid::parse_str(target).is_err()
        || !valid_id(digest)
    {
        return None;
    }
    pid.parse::<i32>().ok().filter(|pid| *pid > 1)
}

fn inventory_ids(bytes: &[u8]) -> Result<Vec<String>, String> {
    let text = std::str::from_utf8(bytes).map_err(|_| "inventory is not UTF-8")?;
    let ids: Vec<_> = text.lines().collect();
    if ids.iter().any(|id| !valid_id(id)) {
        return Err("inventory is incomplete".into());
    }
    Ok(ids.into_iter().map(str::to_owned).collect())
}

fn collect_absent_receipts(runner: &impl CommandRunner, root: &Path) {
    let inventory = docker(
        runner,
        &[
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            "label=ai.vonkforge.managed=true",
            "--format",
            "{{.ID}}",
        ],
    )
    .and_then(|bytes| inventory_ids(&bytes));
    let Ok(ids) = inventory else {
        return;
    }; // unavailable is never absent
    if let Ok(entries) = fs::read_dir(root.join("host-memory-evidence")) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path
                .extension()
                .is_some_and(|extension| extension == "json")
                && let Some(id) = path.file_stem().and_then(|id| id.to_str())
                && valid_id(id)
                && !ids.iter().any(|existing| existing == id)
            {
                let _ = fs::remove_file(path);
            }
        }
    }
}

fn docker(runner: &impl CommandRunner, args: &[&str]) -> Result<Vec<u8>, String> {
    let output = runner.run_with_timeout(
        Path::new("/usr/bin/docker"),
        &args.iter().map(|a| (*a).into()).collect::<Vec<_>>(),
        COMMAND_TIMEOUT,
    )?;
    if output.success {
        Ok(output.stdout)
    } else {
        Err("docker command failed".into())
    }
}

// Only the exact, freshly identity-checked container can be killed. No lifecycle
// locks/claims/fences are acquired: a fresh authorized attempt remains admissible.
fn stop_managed(
    runner: &impl CommandRunner,
    root: &Path,
    id: &str,
    value: &FailureDiagnostics,
) -> Result<(), String> {
    if !valid_id(id) {
        return Err("invalid container id".into());
    }
    let output = docker(runner, &["inspect", "--format", IDENTITY_FORMAT, id])?;
    managed_identity(&String::from_utf8_lossy(&output), id)
        .ok_or("not a managed running container")?;
    // The typed event is already captured in the pending command. Emergency
    // KILL precedes filesystem sync/logging: pressure can stall those
    // bookkeeping paths, and preserving host responsiveness takes priority.
    // Signalling goes through the existing Docker kill/stop actions, so the
    // helper needs no CAP_KILL beyond its declared capability boundary.
    let killed = docker(runner, &["kill", "--signal", "KILL", id]);
    let stopped = docker(runner, &["stop", "--time", "0", id]);
    if let Err(error) = publish(root, id, value) {
        eprintln!("host_memory_guard: evidence unavailable: {error}");
    }
    eprintln!("host_memory_guard: container={id} {}", summary(value));
    stopped.or(killed).map(|_| ())
}

pub fn summary(value: &FailureDiagnostics) -> String {
    let mut tokens = vec![format!("observed_at={}", value.collected_at)];
    tokens.extend(
        value
            .preflight
            .iter()
            .map(|p| format!("{}={}", p.name, p.value)),
    );
    tokens.join(" ")
}

// Journal follow runs in its own thread and restarts with bounded backoff. The
// bounded channel retains the precursor without letting log floods consume RAM.
fn kernel_events() -> Receiver<Instant> {
    let (send, receive) = mpsc::sync_channel(1);
    thread::spawn(move || {
        let mut previous: Option<std::process::Child> = None;
        loop {
            if let Some(mut child) = previous.take() {
                match child.wait_timeout(COMMAND_TIMEOUT) {
                    Ok(Some(_)) => {}
                    _ => {
                        eprintln!(
                            "host_memory_guard: waiting for kernel follower pid={} deadline=1s resume=child-exited next_attempt=2s",
                            child.id()
                        );
                        let _ = child.kill();
                        previous = Some(child);
                        thread::sleep(JOURNAL_RETRY);
                        continue;
                    }
                }
            }
            let child = Command::new("/usr/bin/journalctl")
                .args(["-k", "-f", "-n", "0", "--no-pager", "-o", "cat"])
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn();
            if let Ok(mut child) = child {
                if let Some(stdout) = child.stdout.take() {
                    // read_until would permit an unbounded line. The kernel limits
                    // records, but cap the reader too; discarded suffixes cannot grow.
                    let mut reader = BufReader::new(stdout);
                    loop {
                        let mut line = Vec::new();
                        match Read::by_ref(&mut reader)
                            .take(4096)
                            .read_until(b'\n', &mut line)
                        {
                            Ok(0) | Err(_) => break,
                            Ok(_) if nvidia_allocation_failure(&String::from_utf8_lossy(&line)) => {
                                let _ = send.try_send(Instant::now());
                            }
                            Ok(_) => {}
                        }
                    }
                }
                let _ = child.kill();
                previous = Some(child);
            }
            eprintln!("host_memory_guard: kernel follow unavailable; retry in 2s");
            thread::sleep(JOURNAL_RETRY);
        }
    });
    receive
}

pub fn spawn(root: PathBuf) {
    let kernel = kernel_events();
    let active = Arc::new(Mutex::new(Vec::<String>::new()));
    let inventory = Arc::clone(&active);
    let (send, receive) = mpsc::sync_channel::<(Vec<String>, FailureDiagnostics)>(1);
    // Separate command lane: Docker delays never delay /proc sampling. Targets
    // are captured at observation time, so an old event cannot kill a new kit.
    thread::spawn(move || {
        let mut pending = BTreeMap::<String, FailureDiagnostics>::new();
        let mut next_collection = Instant::now();
        loop {
            match docker(
                &ProcessCommandRunner,
                &[
                    "ps",
                    "--no-trunc",
                    "--filter",
                    "label=ai.vonkforge.managed=true",
                    "--format",
                    "{{.ID}}",
                ],
            ) {
                Ok(ids) => {
                    let Ok(ids) = inventory_ids(&ids) else {
                        thread::sleep(SAMPLE_INTERVAL);
                        continue;
                    };
                    for id in &ids {
                        if !pending.contains_key(id)
                            && let Some(value) = load_evidence(&root, id)
                        {
                            pending.insert(id.clone(), value);
                        }
                    }
                    // Successful enumeration reconciles exact effects. A failed
                    // enumeration retains pending work, never assumes a stop.
                    pending.retain(|id, _| ids.contains(id));
                    if let Ok(mut current) = inventory.lock() {
                        *current = ids;
                    }
                }
                Err(error) => eprintln!(
                    "host_memory_guard: inventory unavailable: {error}; retry on next interval"
                ),
            }
            if let Ok((ids, value)) = receive.recv_timeout(SAMPLE_INTERVAL) {
                for id in ids {
                    pending.entry(id).or_insert_with(|| value.clone());
                }
            }
            for (id, value) in &pending {
                if let Err(error) = stop_managed(&ProcessCommandRunner, &root, id, value) {
                    eprintln!(
                        "host_memory_guard: stop unresolved container={id}: {error}; retry after reconciliation"
                    );
                }
            }
            if Instant::now() >= next_collection {
                collect_absent_receipts(&ProcessCommandRunner, &root);
                next_collection = Instant::now()
                    + vonk_agent_protocol::host_memory_guard_policy::RECEIPT_COLLECTION_INTERVAL;
            }
            // No hot retry loop, even when a channel contains another trip.
            thread::sleep(SAMPLE_INTERVAL);
        }
    });
    thread::spawn(move || {
        let mut decision = Decision::default();
        let mut previous = Vec::new();
        let mut driver_failure: Option<(Instant, Vec<String>)> = None;
        loop {
            let now = Instant::now();
            let ids = active.lock().map(|ids| ids.clone()).unwrap_or_default();
            if ids != previous {
                decision = Decision::default();
                previous = ids.clone();
            }
            while let Ok(event) = kernel.try_recv() {
                driver_failure = Some((event, ids.clone()));
            }
            let driver_targets = driver_failure
                .as_ref()
                .filter(|(event, _)| now.duration_since(*event) <= SUSTAINED_PRESSURE)
                .map(|(_, targets)| {
                    targets
                        .iter()
                        .filter(|id| ids.contains(id))
                        .cloned()
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let sample = read_sample(|path| fs::read_to_string(path));
            if let Some(trigger) =
                decision.observe(now, sample, !driver_targets.is_empty(), !ids.is_empty())
            {
                let targets = if trigger == "nvidia_allocation_failure" {
                    driver_targets
                } else {
                    ids
                };
                let _ = send.try_send((
                    targets,
                    evidence(sample, trigger, decision.pressure_duration(now, trigger)),
                ));
            }
            thread::sleep(SAMPLE_INTERVAL);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tight_kit_and_bursts_are_allowed_but_sustained_exhaustion_is_not() {
        let now = Instant::now();
        let mut decision = Decision::default();
        let tight = Sample {
            available_bytes: Some(3 * 1024 * 1024 * 1024),
            full_avg10: Some(0.0),
        };
        for tick in 0..40 {
            assert_eq!(
                decision.observe(now + SAMPLE_INTERVAL * tick, tight, false, true),
                None
            );
        }
        let exhausted = Sample {
            available_bytes: Some(EXHAUSTED_BYTES - 1),
            ..tight
        };
        for tick in 40..52 {
            assert_eq!(
                decision.observe(now + SAMPLE_INTERVAL * tick, exhausted, false, true),
                None
            );
        }
        assert_eq!(
            decision.observe(now + SAMPLE_INTERVAL * 52, exhausted, false, true),
            Some("sustained_memavailable_exhaustion")
        );
        // Recovery and a fresh operation never inherit a permanent gate.
        assert_eq!(
            decision.observe(now + SAMPLE_INTERVAL * 53, tight, false, true),
            None
        );
    }
    #[test]
    fn psi_requires_continuity_and_unknown_resets_it() {
        let now = Instant::now();
        let mut decision = Decision::default();
        let pressure = Sample {
            available_bytes: Some(EXHAUSTED_BYTES),
            full_avg10: Some(FULL_PSI_PERCENT + 0.1),
        };
        for tick in 0..12 {
            assert_eq!(
                decision.observe(now + SAMPLE_INTERVAL * tick, pressure, false, true),
                None
            );
        }
        assert_eq!(
            decision.observe(now + SAMPLE_INTERVAL * 12, pressure, false, true),
            Some("sustained_full_psi")
        );
        assert_eq!(
            decision.observe(
                now + SAMPLE_INTERVAL * 13,
                Sample {
                    full_avg10: None,
                    ..pressure
                },
                false,
                true
            ),
            None
        );
        assert_eq!(
            decision.observe(now + SAMPLE_INTERVAL * 14, pressure, false, true),
            None
        );
        assert_eq!(
            decision.observe(now + Duration::from_secs(20), pressure, false, true),
            None
        );
    }
    #[test]
    fn fake_readers_use_available_and_full_never_free_or_some() {
        let sample = read_sample(|path| {
            Ok(if path.ends_with("meminfo") {
                "MemFree: 1 kB\nMemAvailable: 3145728 kB\n"
            } else {
                "some avg10=99.00 avg60=0 total=0\nfull avg10=0.00 avg60=0 total=0\n"
            }
            .into())
        });
        assert_eq!(sample.available_bytes, Some(3 * 1024 * 1024 * 1024));
        assert_eq!(sample.full_avg10, Some(0.0));
        assert_eq!(
            read_sample(|_| Err(std::io::Error::other("unavailable"))).available_bytes,
            None
        );
    }
    #[test]
    fn driver_failure_is_immediate_and_unmanaged_is_never_a_victim() {
        let sample = Sample {
            available_bytes: None,
            full_avg10: None,
        };
        let mut decision = Decision::default();
        assert!(nvidia_allocation_failure(
            "NVRM: Out of memory [NV_ERR_NO_MEMORY] _memdescAllocInternal"
        ));
        assert!(!nvidia_allocation_failure("unrelated out of memory"));
        assert_eq!(decision.observe(Instant::now(), sample, true, false), None);
        assert_eq!(
            decision.observe(Instant::now(), sample, true, true),
            Some("nvidia_allocation_failure")
        );
        let id = "a".repeat(64);
        let digest = "b".repeat(64);
        let target = "10000000-0000-4000-8000-000000000001";
        assert_eq!(
            managed_identity(&format!("{id}\ttrue\tfalse\t{target}\t{digest}\t123"), &id),
            None
        );
        assert_eq!(
            managed_identity(&format!("{id}\ttrue\ttrue\t{target}\t{digest}\t123"), &id),
            Some(123)
        );
    }
    #[test]
    fn stop_path_records_hardware_failure_and_leaves_fresh_attempt_admissible() {
        use crate::operations::{CommandOutput, OperationError};
        use std::sync::Mutex;
        struct FakeRunner {
            managed: bool,
            calls: Mutex<Vec<Vec<String>>>,
        }
        impl CommandRunner for FakeRunner {
            fn run(&self, _: &Path, args: &[String]) -> Result<CommandOutput, String> {
                self.calls.lock().unwrap().push(args.to_vec());
                let stdout = if args[0] == "inspect" {
                    format!(
                        "{}\ttrue\t{}\t10000000-0000-4000-8000-000000000001\t{}\t2147483647",
                        args.last().unwrap(),
                        self.managed,
                        "c".repeat(64)
                    )
                    .into_bytes()
                } else {
                    vec![]
                };
                Ok(CommandOutput {
                    success: true,
                    stdout,
                    stderr: vec![],
                    exit_code: Some(0),
                })
            }
        }
        let root = tempfile::tempdir().unwrap();
        let runner = FakeRunner {
            managed: true,
            calls: Mutex::new(vec![]),
        };
        let value = evidence(
            Sample {
                available_bytes: Some(1),
                full_avg10: Some(99.0),
            },
            "sustained_full_psi",
            SUSTAINED_PRESSURE,
        );
        let id = "a".repeat(64);
        stop_managed(&runner, root.path(), &id, &value).unwrap();
        let recorded = load_evidence(root.path(), &id).unwrap();
        let state = crate::runtime_logs::ContainerExit {
            exit_code: Some(137),
            oom_killed: Some(false),
            error: String::new(),
            host_memory: Some(recorded),
        };
        assert_eq!(
            crate::runtime_logs::classify(Some(&state), None),
            crate::runtime_logs::ExitCause::HostMemoryExhausted
        );
        let failed = crate::operations::process_exited(Err("no log".into()), Ok(state));
        let OperationError::RuntimeProcessExited { exit_summary, .. } = failed else {
            panic!("wrong failure type");
        };
        assert!(exit_summary.contains("reason=workload.host_memory_exhausted"));
        assert!(exit_summary.contains("exit_cause=host_memory_exhausted"));
        assert!(exit_summary.contains("mem_available_bytes=1"));
        let calls = runner.calls.lock().unwrap();
        assert_eq!(calls[1], ["kill", "--signal", "KILL", &id]);
        assert_eq!(calls[2], ["stop", "--time", "0", &id]);
        drop(calls);
        // Neither a fence nor a global busy/ban survives a safety ending.
        let fresh = "b".repeat(64);
        assert!(load_evidence(root.path(), &fresh).is_none());
        stop_managed(&runner, root.path(), &fresh, &value).unwrap();
        let foreign = FakeRunner {
            managed: false,
            calls: Mutex::new(vec![]),
        };
        assert!(stop_managed(&foreign, root.path(), &id, &value).is_err());
        assert_eq!(foreign.calls.lock().unwrap().len(), 1);
    }
    #[test]
    fn equality_bursts_and_partial_inventory_never_trigger_false_stops() {
        let now = Instant::now();
        let mut decision = Decision::default();
        let boundary = Sample {
            available_bytes: Some(EXHAUSTED_BYTES),
            full_avg10: Some(FULL_PSI_PERCENT),
        };
        for tick in 0..20 {
            assert_eq!(
                decision.observe(now + SAMPLE_INTERVAL * tick, boundary, false, true),
                None
            );
        }
        let burst = Sample {
            available_bytes: Some(1),
            full_avg10: Some(99.0),
        };
        for tick in 20..31 {
            assert_eq!(
                decision.observe(now + SAMPLE_INTERVAL * tick, burst, false, true),
                None
            );
        }
        assert_eq!(
            decision.observe(now + SAMPLE_INTERVAL * 31, boundary, false, true),
            None
        );
        assert_eq!(
            decision.observe(now + SAMPLE_INTERVAL * 32, burst, false, true),
            None
        );
        assert!(inventory_ids(format!("{}\npartial", "a".repeat(64)).as_bytes()).is_err());
        assert!(inventory_ids(b"daemon error").is_err());
        assert_eq!(inventory_ids(b"").unwrap(), Vec::<String>::new());
    }
}
