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

#[expect(
    clippy::needless_pass_by_value,
    reason = "the call takes ownership at this boundary; the caller's value is consumed"
)]
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

#[expect(
    clippy::expect_used,
    reason = "the evidence path is always built beneath a directory"
)]
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

/// The ids of the managed containers (every state with `all`, else the running
/// ones), read a line at a time: the list grows with the containers, so no
/// total size bounds it. An id that is not one makes the inventory incomplete,
/// and an incomplete inventory is never read as absence.
fn managed_container_ids(runner: &impl CommandRunner, all: bool) -> Result<Vec<String>, String> {
    let mut ids = Vec::new();
    let mut complete = true;
    let output = runner.run_lines(
        Path::new("/usr/bin/docker"),
        &INVENTORY
            .iter()
            .filter(|word| all || **word != "--all")
            .map(|word| (*word).to_owned())
            .collect::<Vec<_>>(),
        COMMAND_TIMEOUT,
        &mut |line| {
            if valid_id(line) {
                ids.push(line.to_owned());
            } else {
                complete = false;
            }
        },
    )?;
    if !output.success {
        return Err("docker command failed".into());
    }
    if !complete {
        return Err("inventory is incomplete".into());
    }
    Ok(ids)
}

fn collect_absent_receipts(runner: &impl CommandRunner, data_root: &Path) {
    let Ok(ids) = managed_container_ids(runner, true) else {
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
                #[expect(
                    clippy::let_underscore_must_use,
                    reason = "best-effort filesystem step: a failure leaves state the next pass reconciles"
                )]
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

const INVENTORY: [&str; 7] = [
    "ps",
    "--all",
    "--no-trunc",
    "--filter",
    "label=ai.vonkforge.managed=true",
    "--format",
    "{{.ID}}",
];

// Labels are immutable, so each container's identity is checked once, while
// Docker answers; its cgroup is recorded then and needs no daemon at kill time.
fn observe(runner: &impl CommandRunner, cgroups: &Path, known: &Known) -> Result<Known, String> {
    let ids = managed_container_ids(runner, false)?;
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
                if let Ok(Some(_)) = child.wait_timeout(COMMAND_TIMEOUT) {
                } else {
                    eprintln!(
                        "host_memory_guard: waiting for kernel follower pid={} deadline=1s resume=child-exited next_attempt=2s",
                        child.id()
                    );
                    #[expect(
                        clippy::let_underscore_must_use,
                        reason = "best-effort stop of a process that may already have ended"
                    )]
                    let _ = child.kill();
                    previous = Some(child);
                    thread::sleep(JOURNAL_RETRY);
                    continue;
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
                                #[expect(
                                    clippy::let_underscore_must_use,
                                    reason = "the receiving side may already be gone; nobody is waiting for the value"
                                )]
                                let _ = send.try_send(Instant::now());
                            }
                            Ok(_) => {}
                        }
                    }
                }
                #[expect(
                    clippy::let_underscore_must_use,
                    reason = "best-effort stop of a process that may already have ended"
                )]
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
                previous.clone_from(&ids);
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
                #[expect(
                    clippy::let_underscore_must_use,
                    reason = "the receiving side may already be gone; nobody is waiting for the value"
                )]
                let _ = send.try_send((targets, value));
            }
            thread::sleep(SAMPLE_INTERVAL);
        }
    });
}

#[cfg(test)]
mod tests;
