#![cfg(test)]
#![allow(unused_imports)]

pub(super) use super::{
    ExecutionResult, Executor, HEARTBEAT_RETRY_FLOOR, HeartbeatFailure, HelperErrorCode,
    InterruptibleJob, LoopClient, ReadinessOutcome, RecipeExecutor, RecipeObservationError,
    RejectingExecutor, RunOncePolicy, classify_heartbeat_failure, controller_denial_diagnostic,
    distribution_failure_result, distribution_success, exact_stop_plan_from_claim,
    first_report_of_run, output_media_type, parse_compiled_execution_plan, readiness_identity,
    recipe_build_client_failure_result, recipe_install_success,
    report_complete_recipe_run_observations, report_recipe_run_observation_page,
    run_interruptible_job, run_once_with_claim_hook, run_once_with_heartbeat_interval,
    runtime_observation_failure, temporary_observation_error,
    temporary_runtime_observation_failure, wait_for_cancellation, wait_for_launch_stability,
    wait_ready_with_runtime_guard_and_cancellation,
};

pub(super) use crate::{
    client::{
        AgentHttpClient, ClientError, ControllerError, DistributionDownloadEvidence,
        ExactRecipeRunObservation,
    },
    oci::OciRuntime,
    outcome::Failure,
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    runtime_identity::AgentRuntimeIdentity,
    state::{BeginDecision, StateStore},
};

pub(super) use async_trait::async_trait;

pub(super) use chrono::{DateTime, Duration as ChronoDuration, FixedOffset, Utc};

pub(super) use serde_json::{Value, json};

pub(super) use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

pub(super) use tempfile::tempdir;

pub(super) use uuid::Uuid;

pub(super) use vonk_agent_protocol::generated::AgentClaimPayload;

pub(super) use vonk_agent_protocol::generated::{
    AgentFailureKind, AgentOperation, AgentResultResult, AgentResultState, FailureCode,
    FailureStage, OutcomeDoneResult, OutcomeEvidence, OutcomeFailed,
};

pub(super) use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, RecipeJobOutputMapping,
    RecipeOperationRequest,
};

pub(super) static WATCHDOG_FEEDS: std::sync::atomic::AtomicUsize =
    std::sync::atomic::AtomicUsize::new(0);

mod fixtures_0;
pub(super) use fixtures_0::*;
