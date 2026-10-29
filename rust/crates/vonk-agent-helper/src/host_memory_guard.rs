//! Independent protection for the one physical GB10 memory pool. No kit tuning.
mod cgroup;
mod decision;
pub use decision::{Counters, Decision, Rates, Sample, Trigger, meminfo_bytes, read_sample};

use std::{
    collections::{BTreeMap, BTreeSet},
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
    COMMAND_TIMEOUT, JOURNAL_RETRY, PRELOAD_MARGIN_BYTES, SAMPLE_INTERVAL, SUSTAINED_PRESSURE,
};

fn nvidia_allocation_failure(line: &str) -> bool {
    line.contains("NVRM:") && line.contains("Out of memory") && line.contains("NV_ERR_NO_MEMORY")
}

fn property(name: &str, value: impl ToString) -> FailureProperty {
    FailureProperty {
        name: name.into(),
        value: value.to_string(),
    }
}

/// Every kill's account: the trigger, the sample that decided it, its rates
/// and phase, and the recent samples before it (within the 8 preflight slots).
fn evidence(
    sample: Sample,
    trigger: Trigger,
    duration: Duration,
    readings: (String, String),
    recent: (String, String),
    observed_at: chrono::DateTime<chrono::Utc>,
) -> FailureDiagnostics {
    let empty = || FailureLogTail {
        text: String::new(),
        truncated: false,
        dropped_bytes: Some(0),
        dropped_lines: Some(0),
    };
    FailureDiagnostics {
        model_store_layout: None,
        page_cache: None,
        process_exit_code: None,
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
            // Eight facts with observed_at (the agent keeps at most eight):
            // the failure code follows from exit_cause, so no `reason`.
            property(
                vonk_agent_protocol::failure_evidence::EXIT_CAUSE_FIELD,
                vonk_agent_protocol::failure_evidence::ExitCause::HostMemoryExhausted.token(),
            ),
            property("trigger", trigger.as_str()),
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
            // The sample's readings early: the agent's diagnostic paths bound
            // the summary's length.
            property(&readings.0, readings.1),
            property("sustained_ms", duration.as_millis()),
            property(&recent.0, recent.1),
        ],
    }
}

fn evidence_path(data_root: &Path, id: &str) -> Option<PathBuf> {
    valid_id(id).then(|| {
        data_root
            .join("host-memory-evidence")
            .join(format!("{id}.json"))
    })
}
fn valid_id(id: &str) -> bool {
    id.len() == 64
        && id
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

pub fn load_evidence(data_root: &Path, id: &str) -> Option<FailureDiagnostics> {
    let path = evidence_path(data_root, id)?;
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

fn publish(data_root: &Path, id: &str, value: &FailureDiagnostics) -> std::io::Result<()> {
    let path = evidence_path(data_root, id)
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

fn collect_absent_receipts(runner: &impl CommandRunner, data_root: &Path) {
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
    if let Ok(entries) = fs::read_dir(data_root.join("host-memory-evidence")) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path
                .extension()
                .is_some_and(|extension| extension == "json" || extension == "tmp")
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

/// Managed containers last seen running, each with the cgroup it ran in. Only
/// identity-checked containers enter, so only cgroups the helper launched are
/// ever killed; a failed observation keeps the previous set.
type Known = BTreeMap<String, Option<PathBuf>>;
type Trip = (Vec<String>, FailureDiagnostics);

const INVENTORY: [&str; 6] = [
    "ps",
    "--no-trunc",
    "--filter",
    "label=ai.vonkforge.managed=true",
    "--format",
    "{{.ID}}",
];

// Labels are immutable, so each container's identity is checked once, while
// Docker answers; its cgroup is recorded then and needs no daemon at kill time.
fn observe(runner: &impl CommandRunner, cgroups: &Path, known: &Known) -> Result<Known, String> {
    let ids = inventory_ids(&docker(runner, &INVENTORY)?)?;
    let mut next = Known::new();
    for id in ids {
        // An unverified container is never a target; it is checked again next cycle.
        if !known.contains_key(&id) && identity_checked(runner, &id).is_err() {
            continue;
        }
        let recorded = known.get(&id).cloned().flatten();
        let cgroup = recorded.or_else(|| cgroup::directory(cgroups, &id));
        next.insert(id, cgroup);
    }
    Ok(next)
}

fn describe(outcome: &std::io::Result<cgroup::Outcome>) -> String {
    match outcome {
        Ok(cgroup::Outcome::Killed) => "cgroup.kill".into(),
        Ok(cgroup::Outcome::Signalled(count)) => format!("sigkill_processes={count}"),
        Ok(cgroup::Outcome::Gone) => "already_gone".into(),
        Err(error) => format!("failed({error})"),
    }
}

// The safety action itself, on the sampling thread: every target's cgroup is
// killed before anything is logged or handed to the Docker lane.
fn kill_now(
    known: &Known,
    targets: &[String],
    value: &FailureDiagnostics,
    reported: &mut BTreeSet<String>,
) {
    let outcomes: Vec<_> = targets
        .iter()
        .filter_map(|id| Some((id, known.get(id)?.as_deref()?)))
        .map(|(id, path)| (id, path, cgroup::kill(path, cgroup::sigkill)))
        .collect();
    for (id, path, outcome) in outcomes {
        if reported.insert(id.clone()) {
            eprintln!(
                "host_memory_guard: kill container={id} cgroup={} result={} {}",
                path.display(),
                describe(&outcome),
                summary(value)
            );
        }
    }
}

// Kernel kill first; Docker is consulted only when no cgroup could be killed.
// Returns whether the container is gone. No lifecycle locks/claims/fences are
// acquired: a fresh authorized attempt remains admissible.
fn stop_managed(
    runner: &impl CommandRunner,
    data_root: &Path,
    cgroup: Option<&Path>,
    id: &str,
    value: &FailureDiagnostics,
) -> Result<bool, String> {
    if !valid_id(id) {
        return Err("invalid container id".into());
    }
    let kernel = cgroup.map(|path| cgroup::kill(path, cgroup::sigkill));
    if let Err(error) = publish(data_root, id, value) {
        eprintln!("host_memory_guard: evidence unavailable: {error}");
    }
    let kernel = match kernel {
        Some(Ok(outcome)) => return Ok(outcome == cgroup::Outcome::Gone),
        Some(Err(error)) => format!("cgroup kill failed: {error}"),
        None => "no cgroup recorded".into(),
    };
    identity_checked(runner, id)
        .and_then(|()| docker(runner, &["kill", "--signal", "KILL", id]))
        .map_err(|error| format!("{kernel}; {error}"))?;
    eprintln!("host_memory_guard: container={id} {}", summary(value));
    Ok(false)
}

fn identity_checked(runner: &impl CommandRunner, id: &str) -> Result<(), String> {
    let output = docker(runner, &["inspect", "--format", IDENTITY_FORMAT, id])?;
    managed_identity(&String::from_utf8_lossy(&output), id)
        .ok_or("not a managed running container")?;
    Ok(())
}

// Separate command lane: Docker delays never delay /proc sampling or kills.
struct Lane<R> {
    runner: R,
    data_root: PathBuf,
    cgroups: PathBuf,
    known: Known,
    shared: Arc<Mutex<Known>>,
    pending: BTreeMap<String, FailureDiagnostics>,
    next_collection: Instant,
}

impl<R: CommandRunner> Lane<R> {
    fn cycle(&mut self, trip: Option<Trip>) {
        // Targets are captured at observation time, so an old event cannot
        // kill a new kit.
        if let Some((ids, value)) = trip {
            for id in ids {
                self.pending.entry(id).or_insert_with(|| value.clone());
            }
        }
        // Kernel kills run before any Docker call of this cycle.
        let mut gone = Vec::new();
        for (id, value) in &self.pending {
            let cgroup = self.known.get(id).and_then(Option::as_deref);
            match stop_managed(&self.runner, &self.data_root, cgroup, id, value) {
                Ok(true) => gone.push(id.clone()),
                Ok(false) => {}
                Err(error) => eprintln!(
                    "host_memory_guard: stop unresolved container={id}: {error}; retry after reconciliation"
                ),
            }
        }
        for id in gone {
            self.pending.remove(&id);
        }
        match observe(&self.runner, &self.cgroups, &self.known) {
            Ok(known) => {
                for id in known.keys() {
                    if !self.pending.contains_key(id)
                        && let Some(value) = load_evidence(&self.data_root, id)
                    {
                        self.pending.insert(id.clone(), value);
                    }
                }
                // Successful enumeration reconciles exact effects. A failed
                // enumeration retains pending work, never assumes a stop.
                self.pending.retain(|id, _| known.contains_key(id));
                self.known = known;
                if let Ok(mut shared) = self.shared.lock() {
                    shared.clone_from(&self.known);
                }
            }
            Err(error) => eprintln!(
                "host_memory_guard: inventory unavailable: {error}; retry on next interval"
            ),
        }
        if Instant::now() >= self.next_collection {
            collect_absent_receipts(&self.runner, &self.data_root);
            self.next_collection = Instant::now()
                + vonk_agent_protocol::host_memory_guard_policy::RECEIPT_COLLECTION_INTERVAL;
        }
    }
}

/// The facts first, the observation time last: a bounded reader keeps the
/// readings (the time is also the evidence's `collected_at`).
pub fn summary(value: &FailureDiagnostics) -> String {
    let mut tokens: Vec<_> = value
        .preflight
        .iter()
        .map(|p| format!("{}={}", p.name, p.value))
        .collect();
    tokens.push(format!("observed_at={}", value.collected_at));
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

pub fn spawn(data_root: PathBuf) {
    let kernel = kernel_events();
    let active = Arc::new(Mutex::new(Known::new()));
    let (send, receive) = mpsc::sync_channel::<Trip>(1);
    let mut lane = Lane {
        runner: ProcessCommandRunner,
        data_root,
        cgroups: PathBuf::from(cgroup::ROOT),
        known: Known::new(),
        shared: Arc::clone(&active),
        pending: BTreeMap::new(),
        next_collection: Instant::now(),
    };
    thread::spawn(move || {
        loop {
            lane.cycle(receive.recv_timeout(SAMPLE_INTERVAL).ok());
            // No hot retry loop, even when a channel contains another trip.
            thread::sleep(SAMPLE_INTERVAL);
        }
    });
    thread::spawn(move || {
        let mut decision = Decision::default();
        let mut previous = Vec::new();
        let mut driver_failure: Option<(Instant, Vec<String>)> = None;
        let mut reported = BTreeSet::new();
        loop {
            let now = Instant::now();
            let known = active.lock().map(|known| known.clone()).unwrap_or_default();
            let ids: Vec<String> = known.keys().cloned().collect();
            reported.retain(|id| known.contains_key(id));
            if ids != previous {
                decision = Decision::default();
                previous = ids.clone();
            }
            while let Ok(event) = kernel.try_recv() {
                driver_failure = Some((event, ids.clone()));
            }
            let driver_targets = driver_failure
                .as_ref()
                .filter(|(event, _)| decision::driver_line_counts(*event, now))
                .map(|(_, targets)| {
                    targets
                        .iter()
                        .filter(|id| ids.contains(id))
                        .cloned()
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let sample = read_sample(|path| fs::read_to_string(path));
            let loading = crate::page_cache::loading(now);
            if let Some(trigger) = decision.observe(
                now,
                sample,
                !driver_targets.is_empty(),
                !ids.is_empty(),
                loading,
            ) {
                let targets = if trigger == Trigger::DriverAllocationFailure {
                    driver_targets
                } else {
                    ids
                };
                let value = evidence(
                    sample,
                    trigger,
                    decision.pressure_duration(now, trigger),
                    decision::readings(&sample, decision.rates(), loading),
                    decision.recent(),
                    chrono::Utc::now(),
                );
                kill_now(&known, &targets, &value, &mut reported);
                let _ = send.try_send((targets, value));
            }
            thread::sleep(SAMPLE_INTERVAL);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn absent_container_receipts_and_interrupted_writes_leave_after_inventory_recovers() {
        // Catches collecting only JSON while per-container .tmp files leak.
        struct Inventory(bool);
        impl CommandRunner for Inventory {
            fn run(
                &self,
                _: &Path,
                _: &[String],
            ) -> Result<crate::operations::CommandOutput, String> {
                if !self.0 {
                    return Err("injected inventory outage".into());
                }
                Ok(crate::operations::CommandOutput {
                    success: true,
                    stdout: vec![],
                    stderr: vec![],
                    exit_code: Some(0),
                })
            }
        }
        let root = tempfile::tempdir().unwrap();
        let receipt = evidence_path(root.path(), &"a".repeat(64)).unwrap();
        fs::create_dir_all(receipt.parent().unwrap()).unwrap();
        let temporary = receipt.with_extension("tmp");
        fs::write(&receipt, b"receipt").unwrap();
        fs::write(&temporary, b"partial").unwrap();
        collect_absent_receipts(&Inventory(false), root.path());
        assert!(receipt.exists() && temporary.exists());
        collect_absent_receipts(&Inventory(true), root.path());
        assert!(!receipt.exists() && !temporary.exists());
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
        let data_root = tempfile::tempdir().unwrap();
        let runner = FakeRunner {
            managed: true,
            calls: Mutex::new(vec![]),
        };
        let value = evidence(
            Sample {
                available_bytes: Some(1),
                full_avg10: Some(99.0),
                ..Sample::default()
            },
            Trigger::SustainedPressure,
            SUSTAINED_PRESSURE,
            decision::readings(&Sample::default(), None, false),
            Decision::default().recent(),
            chrono::Utc::now(),
        );
        let id = "a".repeat(64);
        // Without a recorded cgroup, the identity-checked Docker kill remains.
        assert!(!stop_managed(&runner, data_root.path(), None, &id, &value).unwrap());
        let recorded = load_evidence(data_root.path(), &id).unwrap();
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
        assert!(exit_summary.contains("exit_cause=host_memory_exhausted"));
        assert!(exit_summary.contains("mem_available_bytes=1"));
        let calls = runner.calls.lock().unwrap();
        assert_eq!(calls[0][0], "inspect");
        assert_eq!(calls[1], ["kill", "--signal", "KILL", &id]);
        assert_eq!(calls.len(), 2);
        drop(calls);
        // Neither a fence nor a global busy/ban survives a safety ending.
        let fresh = "b".repeat(64);
        assert!(load_evidence(data_root.path(), &fresh).is_none());
        stop_managed(&runner, data_root.path(), None, &fresh, &value).unwrap();
        let foreign = FakeRunner {
            managed: false,
            calls: Mutex::new(vec![]),
        };
        assert!(stop_managed(&foreign, data_root.path(), None, &id, &value).is_err());
        assert_eq!(foreign.calls.lock().unwrap().len(), 1);
    }
    /// The shared vector's evidence: a swapping load stopped by R2, with six
    /// recent samples.
    pub(crate) fn vector_summary() -> String {
        let start = Instant::now();
        let mut decision = Decision::default();
        let mut sample = Sample {
            available_bytes: Some(3 << 30),
            free_bytes: Some(1536 << 20),
            swap_total_bytes: Some(64 << 30),
            swap_free_bytes: Some(5 << 30),
            full_avg10: Some(45.5),
            full_avg60: Some(50.25),
            counters: None,
        };
        for second in 0..6u64 {
            sample.available_bytes = Some((3 << 30) - second * (64 << 20));
            decision.observe(
                start + Duration::from_secs(second),
                sample,
                false,
                true,
                true,
            );
        }
        let rates = Some(Rates {
            swap_in: 300.0,
            swap_out: 20_000.0,
            refault: 2_000.0,
        });
        summary(&evidence(
            sample,
            Trigger::SwapFilling,
            Duration::from_millis(3_250),
            decision::readings(&sample, rates, true),
            decision.recent(),
            chrono::DateTime::parse_from_rfc3339("2026-10-28T12:00:00+00:00")
                .unwrap()
                .to_utc(),
        ))
    }

    /// Every trigger's summary at worst-case values: 13-digit bytes, 7-digit
    /// MiB, rates and durations, PSI at 100.
    pub(crate) fn worst_case_summaries() -> Vec<(Trigger, String)> {
        let start = Instant::now();
        let mut decision = Decision::default();
        let big = 9_999_999_999_999_u64;
        let sample = Sample {
            available_bytes: Some(big),
            free_bytes: Some(big),
            swap_total_bytes: Some(big),
            swap_free_bytes: Some(big),
            full_avg10: Some(100.0),
            full_avg60: Some(99.99),
            counters: None,
        };
        for second in 0..6u64 {
            decision.observe(
                start + Duration::from_secs(second),
                sample,
                false,
                true,
                true,
            );
        }
        let rates = Some(Rates {
            swap_in: 9_999_999.0,
            swap_out: 9_999_999.0,
            refault: 9_999_999.0,
        });
        Trigger::ALL
            .into_iter()
            .map(|trigger| {
                let text = summary(&evidence(
                    sample,
                    trigger,
                    Duration::from_millis(9_999_999),
                    decision::readings(&sample, rates, true),
                    decision.recent(),
                    chrono::DateTime::parse_from_rfc3339("2026-12-31T23:59:59.999999999+00:00")
                        .unwrap()
                        .to_utc(),
                ));
                (trigger, text)
            })
            .collect()
    }

    #[test]
    fn every_trigger_at_worst_case_values_is_the_shared_vector() {
        let vectors: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/host-memory-guard-summary-v1.json"
        ))
        .unwrap();
        let expected: Vec<&str> = vectors["worst_case"]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_str().unwrap())
            .collect();
        let actual: Vec<String> = worst_case_summaries()
            .into_iter()
            .map(|(_, text)| text)
            .collect();
        assert_eq!(actual, expected);
    }

    #[test]
    fn the_kill_summary_is_the_shared_vector_the_agent_reads() {
        // The agent's side reads this same vector through from_failure.
        let vectors: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/host-memory-guard-summary-v1.json"
        ))
        .unwrap();
        assert_eq!(vector_summary(), vectors["summary"].as_str().unwrap());
        // The exited-workload reply keeps every fact (no 480-character cut),
        // and an unread log never glues itself to the last fact.
        let reply = crate::runtime_logs::exited_diagnostic(
            &vector_summary(),
            Some("the container log command failed"),
        );
        assert_eq!(reply, vectors["diagnostic"].as_str().unwrap());
        assert!(reply.contains(" observed_at=2026-10-28T12:00:00+00:00 ; "));
        // Every other exit summary keeps its bound.
        let other = format!(
            "exit_code=1 exit_cause=python_exception {}",
            "x".repeat(600)
        );
        assert_eq!(
            crate::runtime_logs::exited_diagnostic(&other, None)
                .chars()
                .count(),
            vonk_agent_protocol::failure_evidence::EXIT_SUMMARY_CHARS
        );
    }

    #[test]
    fn partial_inventory_never_triggers_false_stops() {
        assert!(inventory_ids(format!("{}\npartial", "a".repeat(64)).as_bytes()).is_err());
        assert!(inventory_ids(b"daemon error").is_err());
        assert_eq!(inventory_ids(b"").unwrap(), Vec::<String>::new());
    }

    /// Docker under thrash: every call answers only at its deadline, with an error.
    struct Scripted<F> {
        answer: F,
        calls: Mutex<Vec<Vec<String>>>,
    }
    impl<F> CommandRunner for Scripted<F>
    where
        F: Fn(&[String], Duration) -> Result<crate::operations::CommandOutput, String>
            + Send
            + Sync,
    {
        fn run(&self, _: &Path, _: &[String]) -> Result<crate::operations::CommandOutput, String> {
            unreachable!("every guard Docker call carries a deadline")
        }
        fn run_with_timeout(
            &self,
            _: &Path,
            args: &[String],
            timeout: Duration,
        ) -> Result<crate::operations::CommandOutput, String> {
            assert!(timeout <= COMMAND_TIMEOUT);
            self.calls.lock().unwrap().push(args.to_vec());
            (self.answer)(args, timeout)
        }
    }
    fn lane<F>(answer: F, known: Known) -> (Lane<Scripted<F>>, tempfile::TempDir) {
        let data_root = tempfile::tempdir().unwrap();
        let lane = Lane {
            runner: Scripted {
                answer,
                calls: Mutex::new(vec![]),
            },
            data_root: data_root.path().to_path_buf(),
            cgroups: data_root.path().join("cgroup"),
            shared: Arc::new(Mutex::new(known.clone())),
            known,
            pending: BTreeMap::new(),
            next_collection: Instant::now() + Duration::from_secs(3600),
        };
        (lane, data_root)
    }
    fn trip() -> FailureDiagnostics {
        evidence(
            Sample {
                available_bytes: Some(1),
                full_avg10: Some(99.0),
                ..Sample::default()
            },
            Trigger::DriverAllocationFailure,
            Duration::ZERO,
            decision::readings(&Sample::default(), None, false),
            Decision::default().recent(),
            chrono::Utc::now(),
        )
    }

    #[test]
    fn hung_docker_neither_precedes_nor_blocks_the_kernel_kill() {
        let cgroups = tempfile::tempdir().unwrap();
        let id = "d".repeat(64);
        let scope = cgroups
            .path()
            .join("system.slice")
            .join(format!("docker-{id}.scope"));
        fs::create_dir_all(&scope).unwrap();
        let kill_file = scope.join("cgroup.kill");
        fs::write(&kill_file, "").unwrap();
        let known = Known::from([(id.clone(), Some(scope.clone()))]);
        // The sampler kills from the last known set alone: no Docker at all.
        let started = Instant::now();
        let mut reported = BTreeSet::new();
        kill_now(&known, std::slice::from_ref(&id), &trip(), &mut reported);
        assert!(started.elapsed() < COMMAND_TIMEOUT);
        assert_eq!(fs::read_to_string(&kill_file).unwrap(), "1");
        assert!(reported.contains(&id));
        fs::write(&kill_file, "").unwrap();
        let observed = kill_file.clone();
        let (mut lane, data_root) = lane(
            move |_: &[String], timeout: Duration| {
                // Each Docker call starts only after the kernel kill landed.
                assert!(fs::read_to_string(&observed).map_or(true, |text| text == "1"));
                thread::sleep(timeout);
                Err("compiled command exceeded its deadline".into())
            },
            known.clone(),
        );
        lane.cycle(Some((vec![id.clone()], trip())));
        assert_eq!(lane.runner.calls.lock().unwrap().len(), 1);
        assert!(load_evidence(data_root.path(), &id).is_some());
        // The failed inventory keeps the last known set for the next decision.
        assert_eq!(*lane.shared.lock().unwrap(), known);
        assert!(lane.pending.contains_key(&id));
        // The kernel shows the container gone: the stop retires without Docker.
        fs::remove_dir_all(&scope).unwrap();
        lane.cycle(None);
        assert!(lane.pending.is_empty());
    }

    #[test]
    fn refused_kernel_kill_falls_back_to_docker_and_retries_each_cycle() {
        let id = "e".repeat(64);
        let (mut lane, _root) = lane(
            |_: &[String], _| Err("compiled command exceeded its deadline".into()),
            Known::new(),
        );
        let scope = lane.cgroups.join(format!("docker/{id}"));
        // A cgroup that refuses both cgroup.kill and the member listing.
        fs::create_dir_all(scope.join("cgroup.kill")).unwrap();
        fs::create_dir_all(scope.join("cgroup.procs")).unwrap();
        lane.known.insert(id.clone(), Some(scope));
        lane.cycle(Some((vec![id.clone()], trip())));
        lane.cycle(None);
        let calls = lane.runner.calls.lock().unwrap();
        let verbs: Vec<_> = calls.iter().map(|call| call[0].as_str()).collect();
        assert_eq!(verbs, ["inspect", "ps", "inspect", "ps"]);
        assert!(lane.pending.contains_key(&id));
    }

    #[test]
    fn observation_records_each_managed_cgroup_once() {
        use crate::operations::CommandOutput;
        let id = "f".repeat(64);
        let reply = id.clone();
        let (lane, _root) = lane(
            move |args: &[String], _| {
                let stdout = if args[0] == "ps" {
                    format!("{reply}\n")
                } else {
                    format!(
                        "{reply}\ttrue\ttrue\t10000000-0000-4000-8000-000000000001\t{}\t4242",
                        "c".repeat(64)
                    )
                };
                Ok(CommandOutput {
                    success: true,
                    stdout: stdout.into_bytes(),
                    stderr: vec![],
                    exit_code: Some(0),
                })
            },
            Known::new(),
        );
        let scope = lane.cgroups.join(format!("system.slice/docker-{id}.scope"));
        fs::create_dir_all(&scope).unwrap();
        let known = observe(&lane.runner, &lane.cgroups, &Known::new()).unwrap();
        assert_eq!(known, Known::from([(id.clone(), Some(scope))]));
        // Known containers are not inspected again.
        assert_eq!(observe(&lane.runner, &lane.cgroups, &known).unwrap(), known);
        let calls = lane.runner.calls.lock().unwrap();
        let verbs: Vec<_> = calls.iter().map(|call| call[0].as_str()).collect();
        assert_eq!(verbs, ["ps", "inspect", "ps"]);
    }
}
