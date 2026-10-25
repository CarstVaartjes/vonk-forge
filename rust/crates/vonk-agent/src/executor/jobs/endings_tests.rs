#![cfg(test)]
//! A job never returns an unknown the Controller would observe without end:
//! a deterministic answer ends it at once, and the net at its one exit turns
//! an unknown with neither a receipt nor a cancellation's bound into a
//! failure.

use super::super::test_support::*;
use super::tests::job_claim;
use super::*;

#[test]
fn an_exit_code_out_of_range_ends_the_job_at_once_with_the_raw_value() {
    let (claim, request) = job_claim();
    let stop_plan = job_run_stop_plan(&claim, &request).unwrap();
    let data = tempdir().unwrap();
    let client = AgentHttpClient::for_http_test("http://127.0.0.1:9/", NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: std::path::Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
        runtime_root: data.path(),
    };
    let job_scope = request.job_id.to_string();
    let job = DispatchedJob {
        claim: &claim,
        request: &request,
        stop_plan: &stop_plan,
        job_scope: &job_scope,
        started: Instant::now(),
    };
    for (exit_code, raw) in [
        (Some(300), "Some(300)"),
        (Some(-1), "Some(-1)"),
        (None, "None"),
    ] {
        let outcome = crate::host_runtime::HostRuntimeOutcome {
            exit_code,
            stop_uncertain: false,
            diagnostic: None,
            process_logs: None,
        };
        let result = executor
            .job_exit_receipt(&job, outcome)
            .expect_err("no receipt for an exit code out of range");
        let failure = result
            .failure()
            .expect("a definite failure, not an unknown");
        assert_eq!(failure.stage, Some(FailureStage::JobState));
        assert_eq!(
            failure.diagnostic.as_deref(),
            Some(format!("exit_code_raw={raw}").as_str())
        );
    }
}

fn unbounded_unknown() -> ExecutionResult {
    ExecutionResult::unknown(
        WaitReason::JobStateUncertain,
        "job custody or delivery remains unobserved",
        UnknownEvidence::at(FailureStage::RuntimeAdapter).because("helper answer lost"),
    )
}

#[test]
fn the_net_ends_an_unknown_without_receipt_or_bound_and_keeps_bounded_ones() {
    // Neither a receipt nor a cancellation's bound: failed, with its evidence.
    let ended = bounded_job_result(unbounded_unknown());
    let failure = ended.failure().expect("an unbounded unknown ends failed");
    assert_eq!(failure.stage, Some(FailureStage::RuntimeAdapter));
    let diagnostic = failure.diagnostic.as_deref().unwrap_or_default();
    assert!(
        diagnostic.contains("unknown without receipt or bound")
            && diagnostic.contains("helper answer lost"),
        "{diagnostic}"
    );

    // A receipt bounds it: the Controller ends the order with that exit.
    let (_, request) = job_claim();
    let ExecutionResult::Unknown(mut receipted) = unbounded_unknown() else {
        unreachable!()
    };
    receipted.receipt = Some(job_receipt(
        &request,
        0,
        Instant::now(),
        empty_job_output_manifest(),
        None,
    ));
    assert!(matches!(
        bounded_job_result(ExecutionResult::Unknown(receipted)),
        ExecutionResult::Unknown(_)
    ));

    // A cancellation's unconfirmed stop is bounded by the cancel.
    let ExecutionResult::Unknown(mut cancelling) = unbounded_unknown() else {
        unreachable!()
    };
    cancelling.wait_reason = WaitReason::JobStopUnconfirmed;
    assert!(matches!(
        bounded_job_result(ExecutionResult::Unknown(cancelling)),
        ExecutionResult::Unknown(_)
    ));
}

#[test]
fn a_receipted_unknown_is_the_shared_vector_on_the_wire() {
    // The agent reports a receipted ending's stage and cause as the outcome's
    // evidence, beside the receipt; the Controller stores them with it.
    let vector: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-unknown-receipt-v1.json"
    ))
    .unwrap();
    let (claim, request) = job_claim();
    let stop_plan = job_run_stop_plan(&claim, &request).unwrap();
    let receipt: vonk_agent_protocol::RecipeJobRunResult =
        serde_json::from_value(vector["result"]["receipt"].clone()).unwrap();
    let job_scope = request.job_id.to_string();
    let job = DispatchedJob {
        claim: &claim,
        request: &request,
        stop_plan: &stop_plan,
        job_scope: &job_scope,
        started: Instant::now(),
    };
    let finished = job
        .unknown_after_exit(
            &receipt,
            FailureStage::JobState,
            "job output delivery remains unobserved",
            "artifact output upload was refused: HTTP 409".to_owned(),
        )
        .finish(&claim);
    assert_eq!(
        serde_json::to_value(finished.state).unwrap(),
        vector["state"]
    );
    assert_eq!(
        serde_json::to_value(&finished.result).unwrap(),
        vector["result"]
    );
    assert_eq!(claim.fence.to_string(), vector["fence"]);
}

#[test]
fn a_job_the_host_memory_guard_killed_fails_by_that_cause_not_by_what_it_left() {
    // Production (wan-dancer, 2026-10-25): the guard's SIGKILL skipped the
    // job's cleanup and left a scratch directory in its outputs. Wrong
    // implementation: the receipt dropped the helper's exit account, so the
    // output breach ("not a plain file") was the reported failure.
    let (claim, request) = job_claim();
    let data = tempdir().unwrap();
    let client = AgentHttpClient::for_http_test("http://127.0.0.1:9/", NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: std::path::Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
        runtime_root: data.path(),
    };
    let outputs = data
        .path()
        .join("runs")
        .join(request.job_id.to_string())
        .join("outputs");
    fs::create_dir_all(outputs.join("frames-scratch")).unwrap();
    executor.runtime.persist_job_intent(&request).unwrap();
    let guard_kill = "observed_at=2026-10-25T17:03:21+00:00 \
        reason=workload.host_memory_exhausted exit_cause=host_memory_exhausted \
        mem_available_bytes=1 memory_full_avg10=99 trigger=sustained_full_psi \
        sustained_ms=3000";
    let outcome = crate::host_runtime::HostRuntimeOutcome {
        exit_code: Some(137),
        stop_uncertain: false,
        diagnostic: Some(guard_kill.to_owned()),
        process_logs: None,
    };
    let judged = |observation| {
        let super::super::operation_observation::OperationObservation::Confirmed(result) =
            observation
        else {
            panic!("a guard kill is a confirmed ending");
        };
        let failure = result.failure().expect("a definite failure").clone();
        assert_eq!(failure.code, Some(FailureCode::WorkloadHostMemoryExhausted));
        assert_ne!(
            failure.failure_kind,
            Some(AgentFailureKind::InvalidContract)
        );
        let diagnostic = failure.diagnostic.clone().unwrap_or_default();
        assert!(
            diagnostic.contains("exit_cause=host_memory_exhausted"),
            "{diagnostic}"
        );
        assert!(!diagnostic.contains("rule="), "{diagnostic}");
        let evidence = crate::failure_evidence::from_failure(
            &vonk_agent_protocol::generated::AgentOperation::RecipeJobRunV1,
            &failure,
        );
        assert_eq!(
            evidence.category,
            crate::failure_evidence::FailureCategory::Capacity
        );
    };
    judged(executor.observe_job_exit(&claim, &request, outcome));

    // After a restart only the persisted receipt is left: same judgement.
    let receipt = executor
        .runtime
        .retained_job_completion(&request)
        .unwrap()
        .expect("the exit account was persisted");
    judged(executor.observe_job_outputs(&claim, &request, receipt));
}

#[test]
fn a_job_that_ended_by_its_own_exception_is_still_judged_by_its_outputs() {
    // Only a kill excuses what a job left: a job that raised and also broke
    // its output contract reports the breach (rule, bound, entries) with its
    // exit cause beside it.
    let (claim, request) = job_claim();
    let data = tempdir().unwrap();
    let client = AgentHttpClient::for_http_test("http://127.0.0.1:9/", NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: std::path::Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
        runtime_root: data.path(),
    };
    let outputs = data
        .path()
        .join("runs")
        .join(request.job_id.to_string())
        .join("outputs");
    fs::create_dir_all(&outputs).unwrap();
    let max_files = request.output_limits.max_files;
    for index in 0..=max_files {
        fs::write(outputs.join(format!("frame{index}.png")), b"1").unwrap();
    }
    executor.runtime.persist_job_intent(&request).unwrap();
    let outcome = crate::host_runtime::HostRuntimeOutcome {
        exit_code: Some(1),
        stop_uncertain: false,
        diagnostic: Some(
            "exit_code=1 oom_killed=false exit_cause=python_exception \
             exception=\"RuntimeError: decoder failed\""
                .to_owned(),
        ),
        process_logs: None,
    };
    let super::super::operation_observation::OperationObservation::Confirmed(result) =
        executor.observe_job_exit(&claim, &request, outcome)
    else {
        panic!("a breach is a confirmed ending");
    };
    let failure = result.failure().expect("a definite failure");
    assert_eq!(failure.code, Some(FailureCode::RecipeJobRunFailed));
    assert_eq!(
        failure.failure_kind,
        Some(AgentFailureKind::InvalidContract)
    );
    let diagnostic = failure.diagnostic.as_deref().unwrap_or_default();
    let (limit, observed) = (
        format!("limit={max_files}"),
        format!("observed={}", max_files + 1),
    );
    for fact in [
        "exit_code=1",
        "exit_cause=python_exception",
        "rule=job_output_max_files",
        limit.as_str(),
        observed.as_str(),
        "frame0.png",
    ] {
        assert!(
            diagnostic.contains(fact),
            "{fact} missing from {diagnostic}"
        );
    }
}
