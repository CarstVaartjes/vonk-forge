//! Executor operation boundaries and shared context.

use async_trait::async_trait;

use chrono::{DateTime, FixedOffset, Utc};

use futures_util::{StreamExt, stream};

use sha2::{Digest, Sha256};

use std::{
    fs::{self, File},
    future::Future,
    io::Read,
    path::Path,
    time::{Duration, Instant},
};

use crate::runtime_identity::AgentRuntimeIdentity;

use crate::{
    agent_upgrade::AgentUpgradeExecutor,
    client::{
        AgentHttpClient, ClientError, ControllerError, DistributionDownloadEvidence,
        ExactRecipeRunObservation, RecipeRunDisposition,
    },
    health::{wait_ready, wait_ready_until},
    host_runtime::{
        BACKGROUND_RUN_INSPECTION_CONCURRENCY, HostRuntimeBoundary, HostRuntimeOutcome,
        HostRuntimePlan,
    },
    oci::{
        MAX_RECIPE_RUN_OBSERVATIONS_PER_BATCH, OciError, OciRuntime,
        RecipeRunObservationCheckpoint, RecipeRunStartIdentity,
    },
    outcome::{ExecutionResult, Failure, RefusalBound, UnknownEvidence},
    process::ProcessRunner,
    recipe_builder::RecipeBuilder,
    state::{BeginDecision, StateError, StateStore},
    vocabulary,
    workloads::{
        CompiledExecutionPlan, CompiledRuntimePlacement, WorkloadError, same_installed_workload,
    },
};

use vonk_agent_protocol::generated::{
    AgentFailureKind, AgentInstallResult, AgentOperation, ArtifactDistributionResult,
    DistributionCode, FailureCode, HelperErrorCode, RecipeReconcileResult, RecipeStartResult,
    RecipeStopResult, RecipeUninstallResult, RuntimePreflightFindingCode, WaitReason,
};

use vonk_agent_protocol::generated::{FailureStage, ProgressPhase};

use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, HostRuntimeAction, OperationProgress,
    ProtocolError, RecipeJobEvidence, RecipeJobFile, RecipeJobOutputLimits,
    RecipeJobOutputManifest, RecipeJobOutputMapping, RecipeJobRunResult, RecipeOperationRequest,
    RecipeReconciliationIdentity, RecipeStartPhase, RecipeStartRequest, RecipeStopRequest,
    canonical_json, hex_sha256,
};

const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);

/// Prompt retry after a transient heartbeat failure.  Sleeping a whole
/// renewal interval here is what let one lost request schedule the next
/// attempt after the accepted lease had already expired.
const HEARTBEAT_RETRY_INTERVAL: Duration = Duration::from_secs(2);

/// Smallest delay between two renewal attempts.  A lease that is nearly spent
/// still gets one bounded attempt instead of a busy loop.
const HEARTBEAT_RETRY_FLOOR: Duration = Duration::from_millis(50);

/// When a renewal loop attempts its next heartbeat.
///
/// ``interval`` is the steady-state cadence once a renewal is accepted and
/// ``retry_interval`` is the prompt delay after a transient failure.
#[derive(Clone, Copy)]
struct HeartbeatSchedule {
    interval: Duration,
    retry_interval: Duration,
    /// Called after each accepted renewal: proof the process is alive and the
    /// Controller still holds this attempt. Production feeds the systemd
    /// watchdog, which an operation longer than its period would otherwise
    /// starve (see [`crate::systemd_notify::watchdog`]).
    renewed: fn(),
}

const JOB_CANCEL_EXIT_CODE: u32 = 130;

const JOB_CANCEL_DRAIN_TIMEOUT: Duration = Duration::from_secs(20);

struct JobScopeCleanup<'runtime, 'data, R: ProcessRunner> {
    runtime: &'runtime OciRuntime<'data, R>,
    job_scope: &'runtime str,
    active: bool,
}

#[async_trait]
pub trait LoopClient: Clone + Send + Sync + 'static {
    async fn claim(
        &self,
        preflight_fingerprint: Option<&str>,
        wait_seconds: u64,
        runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError>;
    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError>;
    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError>;
}

#[async_trait(?Send)]
pub trait Executor {
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult;
}

/// A test stand-in for an agent build that does not run an operation.
///
/// No production path builds it: the agent binary runs `ControlExecutor`, so an
/// operation is either executed or reported with its own typed outcome. Were a
/// build ever to claim an operation it cannot run, the answer is a definite
/// failure (retrying cannot make the build grow the capability), never a wait
/// for an operator.
#[cfg(test)]
pub struct RejectingExecutor;

pub struct RecipeExecutor<'a, R> {
    pub client: &'a AgentHttpClient,
    pub runtime: OciRuntime<'a, R>,
    pub runtime_root: &'a Path,
}

#[derive(Debug, thiserror::Error)]
pub enum RecipeObservationError {
    #[error("retained recipe run was skipped because its metadata is invalid")]
    SkippedRun,
    /// The Controller has no record of this run, so it is never reported.
    /// A run proved stopped is retired locally; a running one is left alone.
    #[error("retained recipe run is unknown to the Controller")]
    UnownedRun,
    #[error("managed recipe run observation failed ({})", .0.safe_category())]
    Runtime(#[from] crate::oci::OciError),
    #[error("exact recipe run inspection failed ({})", .0.preflight_code())]
    Inspection(#[from] crate::host_runtime::HostRuntimeError),
    #[error("exact recipe run observation could not be reported: {0}")]
    Report(#[from] ClientError),
}

pub struct ControlExecutor<'a, R> {
    pub recipes: RecipeExecutor<'a, R>,
    pub upgrades: AgentUpgradeExecutor<'a>,
}

pub struct RecipeObservationSweep {
    pub reported: usize,
    pub checkpoint: Option<RecipeRunObservationCheckpoint>,
    /// Only an authoritative, complete empty snapshot permits idle cadence.
    pub empty_snapshot_safe: bool,
}

/// Why a readiness wait ended.
///
/// The runtime guard exists so a start stops observing a workload the agent can
/// no longer inspect.  Collapsing its error into `false` made that failure
/// indistinguishable from an expired readiness deadline, so a start whose
/// privileged inspection failed was reported as "the workload did not become
/// ready before its deadline" and the Controller's existing
/// `runtime_observation_unavailable` retry could never fire.  Observed live on
/// 2026-09-17, a two-Spark GLM start ended 51 s in -- five ten-second inspection
/// ticks -- with 30 s still on the attempt lease and an hour on the start budget.
enum ReadinessOutcome {
    Ready,
    Deadline,
    Cancelled,
    GuardFailed(crate::host_runtime::HostRuntimeError),
}

/// An exact stop that could not be confirmed.
struct StopStall {
    /// The unknown outcome the stop reports when nothing more is known.
    result: ExecutionResult,
    /// The helper refused because the container's identity is not this order's.
    identity_refused: bool,
}

/// How soon a start refused for a foreign container looks at the name again.
const RETAINED_FOREIGN_RETRY_SECONDS: u32 = 30;

/// The transient storage errors of the model-custody step that are retried
/// before a start reports it unconfirmed or failed.
const ACL_SETTLE_RETRIES: u32 = 3;

enum InterruptibleJob<T> {
    Completed(T),
    Cancelled { stopped: bool },
}

#[derive(Debug, thiserror::Error)]
pub enum LoopError {
    #[error(transparent)]
    Client(#[from] ClientError),
    #[error(transparent)]
    State(#[from] StateError),
    #[error("agent heartbeat task failed")]
    HeartbeatTask,
    #[error("agent readiness publication failed: {0}")]
    Readiness(String),
}

/// A failed, panicked, or aborted heartbeat lane must stop the synchronous
/// executor too. Its parent cannot poll a select while Podman blocks that
/// thread; cancellation therefore belongs to the independent heartbeat task.
struct CancelExecutionOnDrop(tokio::sync::watch::Sender<bool>);

#[derive(Clone, Copy)]
struct RunOncePolicy<'a> {
    preflight_fingerprint: Option<&'a str>,
    wait_seconds: u64,
    runtime_identity: Option<&'a AgentRuntimeIdentity>,
    heartbeat_interval: Duration,
    heartbeat_retry_interval: Duration,
    lease_renewed: fn(),
}

/// Whether a failed renewal may be attempted again.
///
/// A renewal loop that terminates on an unclassified error is a one-way
/// ratchet: the first failure it does not recognise ends all future renewals,
/// and with them the agent's ability to observe cancellation.  Classifying every
/// `ClientError` variant names the closed set of conditions a renewal can never
/// repair.
#[derive(Debug, PartialEq, Eq)]
enum HeartbeatFailure {
    /// The same request may be attempted again on the retry schedule.
    Retryable,
    /// The Controller cancelled this exact superseded command.
    SupersededCancellation,
    /// Renewal can never succeed again; stop the loop and cancel the work.
    Terminal,
}

mod plans;
pub use plans::*;
mod readiness;
pub use readiness::*;
mod jobs;
pub use jobs::*;
mod failures;
pub use failures::*;
mod observations;
#[cfg(test)]
use observations::*;
mod agent_loop;
mod build;
mod dispatch;
mod distribution;
mod installation;
mod preflight;
mod runtime;
mod start;
mod stop;
pub use agent_loop::*;
mod heartbeat;
use heartbeat::*;

#[cfg(test)]
mod test_support;
