//! Page-cache drops while workloads load: the agent asks, the root helper acts.
//!
//! A drop is host-wide, so every run loading on this host shares one cadence.
//! A start holds its run's load phase from just before its container starts
//! until it is ready or the start ends; a rank launch, whose readiness another
//! phase waits for, leaves its run loading until its accepted start deadline.
//! Nothing here can fail or delay a start beyond one bounded ask.
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::{Arc, Mutex, OnceLock, Weak},
    time::Duration,
};

use chrono::Utc;
use futures_util::future::BoxFuture;
use tokio::time::Instant;
use uuid::Uuid;
use vonk_agent_protocol::canonical_generated_json;
use vonk_agent_protocol::generated::{
    PageCacheDropEvidence, PageCacheDropKind, PageCacheDropProblem, PageCacheDropReport,
    PageCacheDropRequest,
};
use vonk_agent_protocol::host_memory_guard_policy::{
    PAGE_CACHE_DROP_INTERVAL, PAGE_CACHE_DROP_REPLY_TIMEOUT,
};

/// Ask the helper for one drop. An ask it does not answer is the report's
/// problem, never an error.
pub(crate) fn ask(socket: &Path) -> PageCacheDropReport {
    let request_id = Uuid::new_v4();
    let request = PageCacheDropRequest {
        kind: PageCacheDropKind::PageCacheDrop,
        request_id,
    };
    let reply = crate::stall_window::Inactivity::new(PAGE_CACHE_DROP_REPLY_TIMEOUT);
    canonical_generated_json(&request)
        .ok()
        .and_then(|body| {
            crate::host_runtime::inspect_helper::<PageCacheDropReport>(socket, &body, &reply).ok()
        })
        .filter(|report| report.request_id == request_id)
        .unwrap_or_else(|| unanswered(request_id))
}

type Helpers = BTreeMap<PathBuf, Weak<LoadPhases>>;

fn helpers() -> std::sync::MutexGuard<'static, Helpers> {
    static HELPERS: OnceLock<Mutex<Helpers>> = OnceLock::new();
    HELPERS
        .get_or_init(Default::default)
        .lock()
        .unwrap_or_else(|error| error.into_inner())
}

/// A test's own cadence for the helper at `socket`, held by the caller.
#[cfg(test)]
pub(crate) fn install_for_helper(socket: &Path, phases: &Arc<LoadPhases>) {
    helpers().insert(socket.to_owned(), Arc::downgrade(phases));
}

fn unanswered(request_id: Uuid) -> PageCacheDropReport {
    PageCacheDropReport {
        request_id,
        dropped_at: Utc::now().fixed_offset(),
        cached_before_bytes: None,
        cached_after_bytes: None,
        mem_available_after_bytes: None,
        sync: None,
        problem: Some(PageCacheDropProblem::HelperUnavailable),
    }
}

/// One drop, however it is made.
pub type Dropper = Arc<dyn Fn() -> BoxFuture<'static, PageCacheDropReport> + Send + Sync>;

/// Fold one report into a run's bounded evidence.
pub fn record(evidence: &mut PageCacheDropEvidence, report: &PageCacheDropReport) {
    if report.problem.is_some() {
        evidence.problems = evidence.problems.saturating_add(1);
        evidence.last_problem = Some(report.clone());
    } else {
        evidence.drops = evidence.drops.saturating_add(1);
        let reclaimed = report
            .cached_before_bytes
            .zip(report.cached_after_bytes)
            .map_or(0, |(before, after)| before.saturating_sub(after));
        evidence.reclaimed_bytes = evidence
            .reclaimed_bytes
            .saturating_add(reclaimed)
            .min(i64::MAX as u64);
    }
    if evidence.first.is_none() {
        evidence.first = Some(report.clone());
    }
    evidence.last = Some(report.clone());
}

struct Run {
    evidence: PageCacheDropEvidence,
    /// `None` while a start holds it; else when its drops end.
    deadline: Option<Instant>,
}

#[derive(Default)]
struct State {
    runs: BTreeMap<Uuid, Run>,
    ticking: bool,
    last_problem: Option<PageCacheDropProblem>,
}

/// The loading runs of one helper and their shared drop cadence.
pub struct LoadPhases {
    dropper: Dropper,
    interval: Duration,
    state: Mutex<State>,
}

impl LoadPhases {
    pub fn new(dropper: Dropper, interval: Duration) -> Arc<Self> {
        Arc::new(Self {
            dropper,
            interval,
            state: Mutex::default(),
        })
    }

    /// The one cadence for the helper at `socket`.
    pub fn for_helper(socket: &Path) -> Arc<Self> {
        let mut phases = helpers();
        phases.retain(|_, phase| phase.strong_count() != 0);
        if let Some(existing) = phases.get(socket).and_then(Weak::upgrade) {
            return existing;
        }
        let owned = socket.to_owned();
        let dropper: Dropper = Arc::new(move || {
            let socket = owned.clone();
            Box::pin(async move {
                tokio::task::spawn_blocking(move || ask(&socket))
                    .await
                    .unwrap_or_else(|_| unanswered(Uuid::new_v4()))
            })
        });
        let created = Self::new(dropper, PAGE_CACHE_DROP_INTERVAL);
        phases.insert(socket.to_owned(), Arc::downgrade(&created));
        created
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|error| error.into_inner())
    }

    /// `run` loads from now until its phase is finished, released or dropped.
    pub fn begin(self: &Arc<Self>, run: Uuid) -> LoadPhase {
        let mut state = self.lock();
        state
            .runs
            .entry(run)
            .or_insert_with(|| Run {
                evidence: PageCacheDropEvidence {
                    drops: 0,
                    problems: 0,
                    reclaimed_bytes: 0,
                    first: None,
                    last: None,
                    last_problem: None,
                },
                deadline: None,
            })
            .deadline = None;
        if !state.ticking {
            state.ticking = true;
            let phases = Arc::clone(self);
            tokio::spawn(async move { phases.tick().await });
        }
        LoadPhase {
            phases: Arc::clone(self),
            run,
            settled: false,
        }
    }

    async fn drop_once(&self) {
        let report = (self.dropper)().await;
        let mut state = self.lock();
        if report.problem != state.last_problem {
            if let Some(problem) = report.problem {
                eprintln!(
                    "vonk-agent: page cache drop not made: {problem}; the start proceeds and the next interval asks again"
                );
            }
            state.last_problem = report.problem;
        }
        for run in state.runs.values_mut() {
            record(&mut run.evidence, &report);
        }
    }

    async fn tick(self: Arc<Self>) {
        loop {
            tokio::time::sleep(self.interval).await;
            {
                let mut state = self.lock();
                let now = Instant::now();
                state
                    .runs
                    .retain(|_, run| run.deadline.is_none_or(|deadline| now < deadline));
                if state.runs.is_empty() {
                    state.ticking = false;
                    return;
                }
            }
            self.drop_once().await;
        }
    }

    /// `run` is stopped: its drops end, whoever held them.
    pub fn end(&self, run: Uuid) {
        self.settle(run, None);
    }

    fn settle(&self, run: Uuid, until: Option<Instant>) -> Option<PageCacheDropEvidence> {
        let mut state = self.lock();
        let evidence = match until {
            Some(until) => state.runs.get_mut(&run).map(|entry| {
                entry.deadline = Some(until);
                entry.evidence.clone()
            }),
            None => state.runs.remove(&run).map(|entry| entry.evidence),
        }?;
        evidence.first.as_ref()?;
        eprintln!(
            "vonk-agent: run {run} page cache drops={} problems={} reclaimed_bytes={}{}",
            evidence.drops,
            evidence.problems,
            evidence.reclaimed_bytes,
            if until.is_some() {
                "; drops continue until its start deadline"
            } else {
                "; drops for it ended"
            }
        );
        Some(evidence)
    }
}

/// One run's hold on the shared cadence. Dropping it unsettled (an abandoned
/// start) ends the run's drops like [`LoadPhase::finish`].
pub struct LoadPhase {
    phases: Arc<LoadPhases>,
    run: Uuid,
    settled: bool,
}

impl LoadPhase {
    /// One drop now: just before the container starts.
    pub async fn drop_now(&self) {
        self.phases.drop_once().await;
    }

    /// The run is ready or its start ended: its drops end; its evidence.
    pub fn finish(mut self) -> Option<PageCacheDropEvidence> {
        self.settled = true;
        self.phases.settle(self.run, None)
    }

    /// Another phase waits for readiness: drops continue until `until`.
    pub fn release_until(mut self, until: Instant) -> Option<PageCacheDropEvidence> {
        self.settled = true;
        self.phases.settle(self.run, Some(until))
    }
}

impl Drop for LoadPhase {
    fn drop(&mut self) {
        if !self.settled {
            self.phases.settle(self.run, None);
        }
    }
}

#[cfg(test)]
mod tests;
