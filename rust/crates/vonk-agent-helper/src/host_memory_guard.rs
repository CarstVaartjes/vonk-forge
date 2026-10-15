//! Independent protection for the one physical GB10 memory pool. No kit tuning.
mod cgroup;

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
    COMMAND_TIMEOUT, EXHAUSTED_BYTES, FULL_PSI_PERCENT, JOURNAL_RETRY, PRELOAD_MARGIN_BYTES,
    SAMPLE_INTERVAL, STALL_AVAILABLE_BYTES, SUSTAINED_PRESSURE,
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
}

impl Decision {
    pub fn observe(
        &mut self,
        now: Instant,
        sample: Sample,
        driver_failure: bool,
        managed: bool,
    ) -> Option<&'static str> {
        // A sampler stall is no reset: thrash stalls the sampler itself. Pressure
        // stays continuous only while every observed sample shows it.
        if !managed {
            *self = Self::default();
            return None;
        }
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
        // Full PSI is a stall only while memory is actually running out: a load's
        // page-cache reclaim shows the same PSI with tens of GB available.
        let stalled = sustained(
            &mut self.stalled_since,
            sample.full_avg10.is_some_and(|psi| psi > FULL_PSI_PERCENT),
        ) && sample
            .available_bytes
            .is_some_and(|bytes| bytes < STALL_AVAILABLE_BYTES);
        // The line alone is non-fatal on GB10 (once per normal vLLM load, which can
        // briefly spike full PSI): it counts only with MemAvailable exhausted in this
        // sample or full PSI sustained as above. Unknown readings never do.
        let pressured = stalled
            || sample
                .available_bytes
                .is_some_and(|bytes| bytes < EXHAUSTED_BYTES);
        if driver_failure && pressured {
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
            "sustained_full_psi" | "nvidia_allocation_failure" => self.stalled_since,
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
    root: &Path,
    cgroup: Option<&Path>,
    id: &str,
    value: &FailureDiagnostics,
) -> Result<bool, String> {
    if !valid_id(id) {
        return Err("invalid container id".into());
    }
    let kernel = cgroup.map(|path| cgroup::kill(path, cgroup::sigkill));
    if let Err(error) = publish(root, id, value) {
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
    root: PathBuf,
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
            match stop_managed(&self.runner, &self.root, cgroup, id, value) {
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
                        && let Some(value) = load_evidence(&self.root, id)
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
            collect_absent_receipts(&self.runner, &self.root);
            self.next_collection = Instant::now()
                + vonk_agent_protocol::host_memory_guard_policy::RECEIPT_COLLECTION_INTERVAL;
        }
    }
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
    let active = Arc::new(Mutex::new(Known::new()));
    let (send, receive) = mpsc::sync_channel::<Trip>(1);
    let mut lane = Lane {
        runner: ProcessCommandRunner,
        root,
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
                let value = evidence(sample, trigger, decision.pressure_duration(now, trigger));
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
        // A sampler stall between two over-bar readings is pressure, not a reset.
        assert_eq!(
            decision.observe(now + Duration::from_secs(20), pressure, false, true),
            Some("sustained_full_psi")
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
    fn nvrm_line_kills_only_with_corroborating_pressure_and_unmanaged_is_never_a_victim() {
        let sample = |available_bytes, full_avg10| Sample {
            available_bytes,
            full_avg10,
        };
        let healthy = sample(Some(EXHAUSTED_BYTES * 100), Some(0.0));
        let low = sample(Some(EXHAUSTED_BYTES - 1), Some(0.0));
        let stalled = sample(Some(EXHAUSTED_BYTES * 100), Some(FULL_PSI_PERCENT + 1.0));
        assert!(nvidia_allocation_failure(
            "NVRM: Out of memory [NV_ERR_NO_MEMORY] _memdescAllocInternal"
        ));
        assert!(!nvidia_allocation_failure("unrelated out of memory"));
        let observe =
            |sample, managed| Decision::default().observe(Instant::now(), sample, true, managed);
        assert_eq!(observe(healthy, true), None);
        assert_eq!(observe(sample(None, None), true), None);
        assert_eq!(observe(low, true), Some("nvidia_allocation_failure"));
        // One PSI sample over the bar is a load's spike, not a wedge.
        assert_eq!(observe(stalled, true), None);
        assert_eq!(observe(low, false), None);
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
    // 2026-10-15 spark-3542, diffusiongemma vLLM: one NVRM line during its normal
    // KV-cache allocation, full avg10 30.43 for one kernel update, 34.5 GB free.
    const LOADING_AVAILABLE: u64 = 34_546_176_000;
    const LOAD_SPIKE_PSI: f64 = 30.43;
    const WINDOW_TICKS: u32 = (SUSTAINED_PRESSURE.as_millis() / SAMPLE_INTERVAL.as_millis()) as u32;

    #[test]
    fn nvrm_line_during_a_loads_brief_psi_spike_with_memory_available_never_kills() {
        let now = Instant::now();
        let mut decision = Decision::default();
        let sample = |full_avg10| Sample {
            available_bytes: Some(LOADING_AVAILABLE),
            full_avg10: Some(full_avg10),
        };
        // The line holds for its whole window; the spike lasts one 2 s PSI update.
        for tick in 0..WINDOW_TICKS * 4 {
            let psi = if (4..12).contains(&tick) {
                LOAD_SPIKE_PSI
            } else {
                12.0
            };
            let line = tick <= WINDOW_TICKS;
            assert_eq!(
                decision.observe(now + SAMPLE_INTERVAL * tick, sample(psi), line, true),
                None,
                "tick {tick}"
            );
        }
    }

    // 2026-10-13 spark-3542 wedge: last telemetry 120.95e9 used of 130.67e9.
    const WEDGE_AVAILABLE: u64 = 9_720_000_000;

    #[test]
    fn nvrm_line_with_full_psi_sustained_for_the_window_kills_with_its_duration() {
        let now = Instant::now();
        let mut decision = Decision::default();
        let pressure = Sample {
            available_bytes: Some(WEDGE_AVAILABLE),
            full_avg10: Some(LOAD_SPIKE_PSI),
        };
        for tick in 0..WINDOW_TICKS {
            let at = now + SAMPLE_INTERVAL * tick;
            assert_eq!(decision.observe(at, pressure, true, true), None);
        }
        let at = now + SAMPLE_INTERVAL * WINDOW_TICKS;
        let trigger = decision.observe(at, pressure, true, true);
        assert_eq!(trigger, Some("nvidia_allocation_failure"));
        assert_eq!(
            decision.pressure_duration(at, trigger.unwrap()),
            SUSTAINED_PRESSURE
        );
    }

    // 2026-10-15 12:05Z spark-3542, same 112.53e9 declaration: KV-cache allocation
    // after a fastsafetensors load; full avg10 46.68 for 3 s, 27.08 GB available,
    // NVRM lines 6.6 s before onset and 13 ms after the old guard's kill.
    const RECLAIM_AVAILABLE: u64 = 27_084_484_608;

    #[test]
    fn a_loads_sustained_page_cache_reclaim_with_memory_available_never_kills() {
        let now = Instant::now();
        let onset = Duration::from_secs(10);
        let lines = [
            onset - Duration::from_millis(6600),
            onset + Duration::from_millis(3013),
        ];
        let mut decision = Decision::default();
        // PSI 35-50 for 14 s from onset, with every NVRM line in its window.
        for tick in 0..(WINDOW_TICKS * 8) {
            let elapsed = SAMPLE_INTERVAL * tick;
            let psi = if elapsed >= onset {
                35.0 + f64::from(tick % 16)
            } else {
                12.0
            };
            let line = lines
                .iter()
                .any(|at| elapsed >= *at && elapsed - *at <= SUSTAINED_PRESSURE);
            let sample = Sample {
                available_bytes: Some(RECLAIM_AVAILABLE),
                full_avg10: Some(psi),
            };
            assert_eq!(
                decision.observe(now + elapsed, sample, line, true),
                None,
                "at {elapsed:?}"
            );
        }
    }

    #[test]
    fn sustained_psi_kills_once_memory_falls_below_the_low_water_bar() {
        let now = Instant::now();
        let mut decision = Decision::default();
        let gib: u64 = 1024 * 1024 * 1024;
        // Pressure holds while available memory collapses from the load's level.
        let mut killed = None;
        for tick in 0..400 {
            let available = RECLAIM_AVAILABLE.saturating_sub(u64::from(tick) * gib / 4);
            let sample = Sample {
                available_bytes: Some(available),
                full_avg10: Some(46.68),
            };
            let at = now + SAMPLE_INTERVAL * tick;
            if let Some(trigger) = decision.observe(at, sample, false, true) {
                killed = Some((
                    tick,
                    available,
                    trigger,
                    decision.pressure_duration(at, trigger),
                ));
                break;
            }
        }
        let (tick, available, trigger, duration) = killed.expect("never killed");
        assert_eq!(trigger, "sustained_full_psi");
        assert!(available < STALL_AVAILABLE_BYTES);
        assert!(
            available + gib / 4 >= STALL_AVAILABLE_BYTES,
            "late at {available}"
        );
        assert_eq!(duration, SAMPLE_INTERVAL * tick);
        // The 2026-10-13 level without any NVRM line: kill at the window's end.
        let mut decision = Decision::default();
        let wedge = Sample {
            available_bytes: Some(WEDGE_AVAILABLE),
            full_avg10: Some(45.0),
        };
        for tick in 0..WINDOW_TICKS {
            assert_eq!(
                decision.observe(now + SAMPLE_INTERVAL * tick, wedge, false, true),
                None
            );
        }
        assert_eq!(
            decision.observe(now + SUSTAINED_PRESSURE, wedge, false, true),
            Some("sustained_full_psi")
        );
    }

    #[test]
    fn repeated_nvrm_lines_under_rising_use_and_sustained_psi_kill_within_the_window() {
        // 2026-10-13 spark-3542: lines at 21:49Z and 21:54Z while use climbed past
        // the declared peak; the Spark wedged. Free memory never needs to reach
        // the exhaustion bar for the guard to act.
        let now = Instant::now();
        let mut decision = Decision::default();
        let lines = [Duration::ZERO, Duration::from_secs(300)];
        let onset = SAMPLE_INTERVAL * 1200 - SAMPLE_INTERVAL * 4;
        let gib: u64 = 1024 * 1024 * 1024;
        let mut killed = None;
        for tick in 0..1400 {
            let elapsed = SAMPLE_INTERVAL * tick;
            let sample = Sample {
                available_bytes: Some((20 * gib).saturating_sub(u64::from(tick) * 12_000_000)),
                full_avg10: Some(if elapsed >= onset { 45.0 } else { 5.0 }),
            };
            let line = lines
                .iter()
                .any(|at| elapsed >= *at && elapsed - *at <= SUSTAINED_PRESSURE);
            if let Some(trigger) = decision.observe(now + elapsed, sample, line, true) {
                assert!(sample.available_bytes.unwrap() > EXHAUSTED_BYTES);
                killed = Some((elapsed, trigger));
                break;
            }
        }
        assert_eq!(
            killed,
            Some((onset + SUSTAINED_PRESSURE, "nvidia_allocation_failure"))
        );
    }

    // Thrash stalls the sampler itself: 0.3-2 s between samples, deterministic.
    const MAX_STALL: Duration = Duration::from_millis(2000);
    fn stalls(seed: u64) -> impl Iterator<Item = Duration> {
        let mut state = seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1;
        std::iter::from_fn(move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            Some(Duration::from_millis(300 + state % 1701))
        })
    }

    #[test]
    fn sampler_stalls_neither_hide_a_wedge_nor_turn_a_load_spike_into_a_kill() {
        let gib: u64 = 1024 * 1024 * 1024;
        let lines = [Duration::ZERO, Duration::from_secs(300)];
        let onset = Duration::from_secs(299);
        // Bound: the first over-bar sample is at most one stall after onset, and
        // the kill at most one stall after the window closes.
        let bound = onset + SUSTAINED_PRESSURE + MAX_STALL * 2;
        for seed in 1..=32 {
            let now = Instant::now();
            // The 2026-10-13 pattern: lines minutes apart, use rising, full PSI
            // well over the bar from onset, free memory above the exhaustion bar.
            let mut decision = Decision::default();
            let mut elapsed = Duration::ZERO;
            let mut killed = None;
            for stall in stalls(seed).take(2000) {
                let sample = Sample {
                    available_bytes: Some(20 * gib - elapsed.as_secs() * 40_000_000),
                    full_avg10: Some(if elapsed >= onset { 45.0 } else { 5.0 }),
                };
                let line = lines
                    .iter()
                    .any(|at| elapsed >= *at && elapsed - *at <= SUSTAINED_PRESSURE);
                if let Some(trigger) = decision.observe(now + elapsed, sample, line, true) {
                    assert!(sample.available_bytes.unwrap() > EXHAUSTED_BYTES);
                    killed = Some((elapsed, trigger));
                    break;
                }
                elapsed += stall;
            }
            let (at, trigger) = killed.unwrap_or_else(|| panic!("seed {seed}: never killed"));
            assert!(at >= onset && at <= bound, "seed {seed}: killed at {at:?}");
            assert!(
                ["nvidia_allocation_failure", "sustained_full_psi"].contains(&trigger),
                "seed {seed}: {trigger}"
            );
            // A load's one-update PSI spike with its NVRM line stays harmless.
            let mut decision = Decision::default();
            let mut elapsed = Duration::ZERO;
            let spike = Duration::from_secs(10)..Duration::from_secs(12);
            for stall in stalls(seed).take(200) {
                let sample = Sample {
                    available_bytes: Some(LOADING_AVAILABLE),
                    full_avg10: Some(if spike.contains(&elapsed) {
                        LOAD_SPIKE_PSI
                    } else {
                        12.0
                    }),
                };
                let line = elapsed >= spike.start && elapsed - spike.start <= SUSTAINED_PRESSURE;
                assert_eq!(
                    decision.observe(now + elapsed, sample, line, true),
                    None,
                    "seed {seed} at {elapsed:?}"
                );
                elapsed += stall;
            }
        }
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
        // Without a recorded cgroup, the identity-checked Docker kill remains.
        assert!(!stop_managed(&runner, root.path(), None, &id, &value).unwrap());
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
        assert_eq!(calls[0][0], "inspect");
        assert_eq!(calls[1], ["kill", "--signal", "KILL", &id]);
        assert_eq!(calls.len(), 2);
        drop(calls);
        // Neither a fence nor a global busy/ban survives a safety ending.
        let fresh = "b".repeat(64);
        assert!(load_evidence(root.path(), &fresh).is_none());
        stop_managed(&runner, root.path(), None, &fresh, &value).unwrap();
        let foreign = FakeRunner {
            managed: false,
            calls: Mutex::new(vec![]),
        };
        assert!(stop_managed(&foreign, root.path(), None, &id, &value).is_err());
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
        let root = tempfile::tempdir().unwrap();
        let lane = Lane {
            runner: Scripted {
                answer,
                calls: Mutex::new(vec![]),
            },
            root: root.path().to_path_buf(),
            cgroups: root.path().join("cgroup"),
            shared: Arc::new(Mutex::new(known.clone())),
            known,
            pending: BTreeMap::new(),
            next_collection: Instant::now() + Duration::from_secs(3600),
        };
        (lane, root)
    }
    fn trip() -> FailureDiagnostics {
        evidence(
            Sample {
                available_bytes: Some(1),
                full_avg10: Some(99.0),
            },
            "nvidia_allocation_failure",
            Duration::ZERO,
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
        let (mut lane, root) = lane(
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
        assert!(load_evidence(root.path(), &id).is_some());
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
